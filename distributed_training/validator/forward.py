# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 KMFODA

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import random
import time

import bittensor as bt
import torch
from distributed_training.utils.misc import get_bandwidth
from distributed_training.utils.progress_tracker import (
    get_global_epoch,
    update_global_tracker_state,
)
from distributed_training.utils.state_loader import load_state_from_peer
from distributed_training.utils.uids import get_hf_validation_uid, get_random_uids
from distributed_training.validator.reward import score_uid


async def forward(self):
    """
    The forward function is called by the validator every time step.

    It is responsible for querying the network and scoring the responses.

    Args:
        self (:obj:`bittensor.neuron.Neuron`): The neuron object which contains all the necessary state for the validator.

    """
    update_global_tracker_state(self)
    if self.local_progress.epoch != self.global_progress.epoch:
        bt.logging.info(
            f"Local Epoch {self.local_progress.epoch} Behind Global Epoch {self.global_progress.epoch}. Loading Latest Model State."
        )
        load_state_from_peer(self, epoch=self.global_progress.epoch)

    # Evaluate wether to run an AllReduce or validate HF miner states
    blocks_since_allreduce = self.block - self.last_allreduce_block
    should_allreduce = blocks_since_allreduce >= self.config.neuron.blocks_per_allreduce
    should_allreduce = True

    responses = [[]]
    self.miner_uids = []
    rewards = torch.tensor([])

    if should_allreduce:
        self.event.update({"synapse_type": "all_reduce"})
        all_reduce = True
        self.peerids_to_uids = {
            str(value["peer_id"]): key
            for key, value in self.uid_metadata_tracker.items()
        }
        if self.uid == self.master_uid:
            # Master validator coordinates AllReduce and queries miners
            try:
                sample_size = int(self.metagraph.n)

                # Get active miners
                while len(self.miner_uids) < (self.config.neuron.min_group_size - 1):
                    bt.logging.info(
                        f"Found {len(self.miner_uids)} UIDs. Attempting to find {self.config.neuron.min_group_size - len(self.miner_uids) - 1} more UIDs."
                    )
                    self.miner_uids = await get_random_uids(
                        self,
                        dendrite=self.dendrite,
                        k=sample_size,
                        epoch=self.local_progress.epoch if all_reduce else None,
                    )

                self.event.update({"UIDs": self.miner_uids})
                bt.logging.info(f"UIDs:  {self.miner_uids}")

                (
                    all_reduce_success_status,
                    results,
                ) = await self.avg_handler.run_validator_allreduce(
                    timeout=self.allreduce_timeout,
                    dendrite_pool=self.dendrite_pool,
                    peerids_to_uids=self.peerids_to_uids,
                    miner_uids=self.miner_uids,
                    # bandwidth=self.bandwidth,
                    epoch=self.global_progress.epoch,
                )
                if all_reduce_success_status:
                    # Update scoring based on allreduce participation
                    (
                        self.allreduce_scores,
                        self.event,
                    ) = self.avg_handler.calculate_allreduce_scores(
                        participating_peers=results["participating_peers"],
                        failed_peers=results["failed_peers"],
                        modes=results["modes"],
                        bandwidths=results["bandwidths"],
                        peerids_to_uids=self.peerids_to_uids,
                        event=self.event,
                        metagraph=self.metagraph,
                    )
                    # Update state after successful allreduce
                    self.local_progress.epoch += 1
                    self.local_progress.samples_accumulated = 0
                    # Upload new state
                    self.upload_new_state(
                        epoch=self.local_progress.epoch,
                        results=results,
                    )
                else:
                    raise Exception

            except Exception as e:
                bt.logging.error(f"AllReduce failed: {e}")
                self.model_loading_manager.set_loading_state(False)
                self.global_progress.epoch = get_global_epoch(self)
                load_state_from_peer(self, epoch=self.global_progress.epoch)

        else:
            # Non-master validators participate in AllReduce to help spread the load and update local model
            (
                all_reduce_success_status,
                results,
            ) = await self.avg_handler.run_validator_allreduce(
                timeout=self.allreduce_timeout,
                dendrite_pool=self.dendrite_pool,
                peerids_to_uids=self.peerids_to_uids,
                miner_uids=self.miner_uids,
                # bandwidth=self.bandwidth,
                epoch=self.global_progress.epoch,
            )
            if all_reduce_success_status:
                # Update scoring based on allreduce participation
                (
                    self.allreduce_scores,
                    self.event,
                ) = self.avg_handler.calculate_allreduce_scores(
                    participating_peers=results["participating_peers"],
                    failed_peers=results["failed_peers"],
                    modes=results["modes"],
                    bandwidths=results["bandwidths"],
                    peerids_to_uids=self.peerids_to_uids,
                    event=self.event,
                    metagraph=self.metagraph,
                )
                # Update state after successful allreduce
                self.local_progress.epoch += 1
                self.local_progress.samples_accumulated = 0

    else:
        # If running HF validation round, only call one UID each step
        all_reduce = False
        self.event.update({"synapse_type": "train"})

        self.miner_uids = await get_hf_validation_uid(
            self,
        )

        # Early return if no active miners found
        if len(self.miner_uids) == 0:
            bt.logging.info("No Active Miners Found This Step.")
            return responses

        self.event.update({"UIDs": self.miner_uids})
        bt.logging.info(f"UIDs:  {self.miner_uids}")

        uid = self.miner_uids[0]
        rewards = await score_uid(self, uid)
        self.uid_metadata_tracker[uid]["score"] = rewards
        self.uid_metadata_tracker[uid]["last_updated_score"] = time.time()

    self.event.update(
        {
            "learning_rate": self.learning_rate,
            "average_miner_loss": self.average_loss,
            "local_epoch": self.local_progress.epoch,
            "global_epoch": self.global_progress.epoch,
            "local_samples_accumulated": self.local_progress.samples_accumulated,
            "global_samples_accumulated": self.global_progress.samples_accumulated,
        }
    )
    # Update scores
    if len(rewards) > 0:
        self.update_scores(rewards.detach().cpu().numpy(), self.miner_uids)

    self.event.update(self.get_validator_info())

    try:
        self.event.update(get_bandwidth())
    except Exception:
        bt.logging.info("Error getting bandwidth metrics")

    return responses
