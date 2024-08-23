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

import asyncio
import base64
import copy
import random
import time
from typing import List, Dict, Tuple

import bittensor as bt
import numpy as np
import torch
from hivemind.averaging.group_info import GroupInfo
from hivemind.dht import DHTID
from huggingface_hub import create_tag, list_repo_refs
from huggingface_hub.utils import HfHubHTTPError

import template
from template.utils.misc import get_bandwidth
from template.utils.progress_tracker import update_global_tracker_state
from template.utils.state_loader import load_state_from_peer
from template.utils.uids import get_random_uids, map_uid_to_peerid
from template.validator.reward import get_rewards, score_blacklist

# def validate_peers(self, group_peerids: Dict[int, str], blacklist_scores: List[float]) -> Tuple[bool, str]:
#     if not group_peerids:
#         return False, "Empty group_peerids"
#     if blacklist_scores is None:
#         return False, "Blacklist scores are None"
#     if len(group_peerids) != len(blacklist_scores):
#         return False, f"Mismatch in lengths: group_peerids ({len(group_peerids)}) vs blacklist_scores ({len(blacklist_scores)})"

#     invalid_peers = []
#     blacklisted_peers = []

#     for (uid, peer_id), score in zip(group_peerids.items(), blacklist_scores):
#         if uid != self.uid:
#             if peer_id is None:
#                 invalid_peers.append(uid)
#             elif score == 0:
#                 blacklisted_peers.append(uid)

#     if invalid_peers:
#         return False, f"Invalid peer IDs for UIDs: {invalid_peers}"
#     if blacklisted_peers:
#         return False, f"Blacklisted peers (score 0) for UIDs: {blacklisted_peers}"

#     return True, "All peers valid"

def validate_peers(self, group_peerids: Dict[int, str], blacklist_scores: List[float]) -> bool:
    if not group_peerids or blacklist_scores is None:
        return False
    return all(
        peer_id is not None and score != 0
        for (uid, peer_id), score in zip(group_peerids.items(), blacklist_scores)
        if uid != self.uid
    )

async def perform_all_reduce(self, start_time):
    
    #while time.perf_counter() - start_time < self.all_reduce_timeout:

    group_peerids = None
    blacklist_scores = None
    new_group_peerids = {}
    new_miner_uids = []

    # Map UIDs to PeerIds
    while (
        (group_peerids is None)
        or (blacklist_scores is None)
        # or (blacklist_scores.sum().item() == 0)
        or any(
            scores_ids_tuple[1] is None
            for index, scores_ids_tuple in enumerate(
                zip(blacklist_scores, group_peerids.values())
            )
            if (index != self.uid) and (scores_ids_tuple[0] != 0)
        )
    ):
        group_peerids = await map_uid_to_peerid(self, self.miner_uids.tolist())
        self.uids_to_peerids = group_peerids
        blacklist_scores = await score_blacklist(self, group_peerids.keys())
        bt.logging.info(f"group_peerids: {group_peerids}")
        bt.logging.info(f"blacklist_scores: {blacklist_scores}")
    
    bt.logging.info(f"While loop finished...")

    # Filter any UIDs not connected to the DHT
    group_peerids = {key: group_peerids[key] 
                     for i, key in enumerate(group_peerids.keys()) 
                     if blacklist_scores[i] != 0.0}
    
    self.miner_uids = torch.tensor(list(group_peerids.keys())).to(self.device)

    group_id = DHTID.generate().to_bytes()

    bt.logging.info("DHT ID:", self.dht.peer_id)
    bt.logging.info("Group Peer IDs:", list(group_peerids.values()))

    ordered_peer_ids = [self.dht.peer_id] + list(group_peerids.values())

    group = template.protocol.Group(
        peer_count=len(group_peerids) + 1,  # Including the local peer
        peer_ids=[peer_id.to_string() for peer_id in ordered_peer_ids],
        group_id=base64.b64encode(group_id),
    )

    # Define a custom group for all-reduce
    custom_group = GroupInfo(group_id, tuple(ordered_peer_ids), gathered=None)

    bt.logging.info("Performing Gradient Averaging")
    gradient_averaging_step = self.grad_averager.step(
        custom_group_info=custom_group, wait=False, 
        peerids_to_uids=self.peerids_to_uids
    )

    learning_rate = self.get_learning_rate()
    bt.logging.info(f"Current Learning Rate: {learning_rate}")

    queries = [
        template.protocol.AllReduce(
            group=group,
            timeout=self.all_reduce_timeout - (time.perf_counter() - start_time), # Subtracting this step from the timeout
            learning_rate=learning_rate,
        )
        for _ in self.miner_uids
    ]

    return gradient_averaging_step, queries

        # else:
        #     bt.logging.warning(f"Only local peer found. Retrying...")
        #     await asyncio.sleep(0.5)

    # bt.logging.error("Failed to find other peers for gradient averaging within the timeout period")
    # return None, None

async def forward(self):
    """
    The forward function is called by the validator every time step.

    It is responsible for querying the network and scoring the responses.

    Args:
        self (:obj:`bittensor.neuron.Neuron`): The neuron object which contains all the necessary state for the validator.

    """
    update_global_tracker_state(self)
    if self.global_progress.epoch is not None:
        if self.local_progress.epoch < self.global_progress.epoch:
            bt.logging.info("Local Epoch Behind Global Epoch. Loading Latest Model State.")
            load_state_from_peer(self)

    # Evaluate wether to run an AllReduce or a Train synapse based on the global samples accumulated
    if self.local_progress.samples_accumulated>=25:
    # if (
    #     (
    #         (
    #             self.config.neuron.global_batch_size_train
    #             - self.global_progress.samples_accumulated
    #         )
    #         <= 25
    #     )
    #     and (not self.step_scheduled)
        #and (self.global_progress.epoch == self.local_progress.epoch)
    #):
        # If running an AllReduce synapse, call as many miners as possible
        sample_size = int(self.metagraph.n) #TODO Set to a fixed All-reduce size
        all_reduce = True
        self.event.update({"synapse_type": "all_reduce"})

    else:
        # If running a Train synapse call, only call the sample_size
        sample_size = self.config.neuron.sample_size
        all_reduce = False
        self.event.update({"synapse_type": "train"})

    # Get active miners
    self.miner_uids = await get_random_uids(
        self,
        dendrite=self.dendrite,
        k=sample_size,
        epoch=self.local_progress.epoch if all_reduce else None,
    )

    self.event.update({"uids": self.miner_uids})
    bt.logging.info(f"UIDs:  {self.miner_uids}")

    if self.miner_uids.tolist() == []:
        responses = [[]]
        bt.logging.info("No Active Miners Found This Step.")
    else:
        query_tasks = []
        
        ## AllReduce synapse
        if all_reduce:
            start_time = time.perf_counter()
            gradient_averaging_step, queries = await perform_all_reduce(self, start_time)

        ## Train synapse
        else:
            # Get a random layer to check gradients against
            gradient_test_index = random.choice(self.test_layer_indices)
            queries = [
                template.protocol.Train(
                    model_name=self.model.name_or_path,
                    gradient_test_index=gradient_test_index,
                )
                for _ in self.miner_uids
            ]

        # Query the network
        query_tasks.append(
            self.dendrite_pool.async_forward(
                self.miner_uids, queries, timeout=self.all_reduce_timeout
            )
        )
        bt.logging.info("Query Sent Out")
        responses = await asyncio.gather(*query_tasks)
        bt.logging.info("Query Responses Received")

        # Process the AllReduce query responses
        # if all_reduce and responses != [[]]:
        if all_reduce:
            failed_gradient_all_reduce = False
            # Wait for gradient averaging to finish
            while (not gradient_averaging_step.done()) and (
                (time.perf_counter() - start_time) <= self.all_reduce_timeout
            ):
                await asyncio.sleep(1)

            if gradient_averaging_step.done():
                # Optimizer Step
                with self.grad_averager.use_averaged_gradients():
                    # Log Model Weight Before Optimizer Step
                    bt.logging.info("Model Weights Before Optimizer Step")
                    current_model_weights_sample = copy.copy(
                        [layer for layer in self.model.parameters()][-1][-10:].tolist()
                    )
                    bt.logging.info(current_model_weights_sample)
                    bt.logging.info("Model Gradients Before Optimizer Step")
                    # Copy gradients
                    gradients = tuple(
                        (
                            param.grad.detach().cpu().clone()
                            if param.grad is not None
                            else torch.zeros_like(param)
                        )
                        for param in self.model.parameters()
                    )
                    bt.logging.info(gradients[-1][-10:].tolist())
                    bt.logging.info("Performing Optimizer Step")
                    # Update model parameters using averaged gradients
                    self.opt.step()

                # Log Model Weight After Optimizer Step
                bt.logging.info("Model Weights After Optimizer Step")
                new_model_weights_sample = copy.copy(
                    [layer for layer in self.model.parameters()][-1][-10:].tolist()
                )
                bt.logging.info(new_model_weights_sample)

                if new_model_weights_sample == current_model_weights_sample:
                    bt.logging.info(
                        "Averaging Failed. Model Weights Haven't Changed. Loading Latest Model State."
                    )
                    failed_gradient_all_reduce = True
                    load_state_from_peer(self, epoch=self.local_progress.epoch + 1)

                elif sum(np.isnan(new_model_weights_sample)) > 1:
                    bt.logging.info(
                        "Averaging Failed. Model Weights Corrupted With NaNs After Running The Optimizer Step. Loading Latest Model State."
                    )
                    failed_gradient_all_reduce = True
                    state_loaded = load_state_from_peer(
                        self, epoch=self.local_progress.epoch + 1
                    )
                    if not state_loaded:
                        state_loaded = load_state_from_peer(
                            self, epoch=self.local_progress.epoch
                        )

                else:
                    # Reset gradients and update local progress
                    self.grad_averager.reset_accumulated_grads_()
                    self.local_progress.epoch += 1
                    self.local_progress.samples_accumulated = 0

                    # Push to HF Hub if the current validator is the first to update
                    refs = list_repo_refs(
                        self.config.neuron.model_name, repo_type="model"
                    )
                    tag_name = (
                        max([int(tag.name) for tag in refs.tags]) if refs.tags else None
                    )
                    bt.logging.info(f"Old Model Tag {tag_name}")
                    if (tag_name is not None) and tag_name < self.local_progress.epoch:
                        attempt = 0
                        while attempt < self.model_upload_retry_limit:
                            try:
                                bt.logging.info("Pushing New Model Weights To HF Hub.")
                                self.model.push_to_hub(self.config.neuron.model_name)
                                create_tag(
                                    self.config.neuron.model_name,
                                    repo_type="model",
                                    tag=str(self.local_progress.epoch),
                                    tag_message=f"Epcoh {self.local_progress.epoch}",
                                )
                                refs = list_repo_refs(
                                    self.config.neuron.model_name, repo_type="model"
                                )
                                tag_name = max([int(tag.name) for tag in refs.tags])
                                bt.logging.info(f"New Model Tag {tag_name}")
                                break

                            except HfHubHTTPError:
                                bt.logging.info(
                                    f"Model With Tag {tag_name} Already Uploaded to HF Hub. Loading That Model."
                                )
                                state_loaded = load_state_from_peer(
                                    self, epoch=tag_name
                                )
                                if state_loaded:
                                    break
                            except Exception as e:
                                attempt += 1
                                bt.logging.warning(
                                    f"Failed To Upload Model To HF hub, Retrying. Attempt {attempt}/{self.model_upload_retry_limit}."
                                )
                                if attempt < self.model_upload_retry_limit:
                                    # Wait before the next retry
                                    time.sleep(self.model_upload_retry_delay)
                                else:
                                    bt.logging.error(
                                        "Maximum Retry Limit Reached. Unable To Upload Model To HF Hub."
                                    )
                                    raise

            else:
                bt.logging.info("Averaging Failed. Loading Latest Model State.")
                failed_gradient_all_reduce = True
                load_state_from_peer(self)

            if failed_gradient_all_reduce:
                gradient_averaging_step.cancel()
                bt.logging.info("Gradient Step Cancelled")
                with self.grad_averager.use_averaged_gradients():
                    self.opt.zero_grad()
                bt.logging.info("Optimizer Gradients Zeroed")

            self.step_scheduled = False
            
        # Process the Train query responses
        else:
            bt.logging.info(
                "Received Responses: "
                + str(
                    [
                        {
                            "Loss": response.loss,
                            "Dataset Indices": (
                                min(response.dataset_indices),
                                max(response.dataset_indices),
                            ),
                            "IP": self.metagraph.axons[uid].ip,
                            "Port": self.metagraph.axons[uid].port,
                            "Hotkey": self.metagraph.axons[uid].hotkey,
                        }
                        for response, uid in zip(responses[0], self.miner_uids)
                        if response.dendrite.status_code == 200
                        and (response.dataset_indices is not None)
                    ]
                )
            )
            average_loss = np.array(
                [
                    response.loss
                    for response, uid in zip(responses[0], self.miner_uids)
                    if response.dendrite.status_code == 200
                    and (response.dataset_indices is not None)
                ]
            ).mean()
            bt.logging.info(f"Current Average Miner Loss: {average_loss}")

    rewards = await get_rewards(
        self, uids=self.miner_uids, responses=responses, all_reduce=all_reduce
    )

    # Normalise Rewards
    if rewards.sum() != 0:
        rewards = rewards / rewards.sum()

    bt.logging.info(f"Final Scores: {rewards}")

    # Update the tracker based on the rewards
    if not all_reduce:
        self.update_local_tracker_state(rewards, responses)
    self.event.update(
        {
            "local_samples_accumulated": self.local_progress.samples_accumulated,
            "local_epoch": self.local_progress.epoch,
            "global_samples_accumulated": self.global_progress.samples_accumulated,
            "global_epoch": self.global_progress.epoch,
        }
    )

    # Update the scores based on the rewards.
    self.update_scores(rewards, self.miner_uids)

    self.event.update(self.get_validator_info())
    try:
        self.event.update(get_bandwidth())
    except:
        bt.logging.info("Error getting bandwidth metrics")

    return responses
