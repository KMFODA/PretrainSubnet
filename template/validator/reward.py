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
from typing import List

import bittensor as bt
import torch
from template.data.dataset import SubsetFalconLoader
from template.utils.uids import get_random_uids
import time
import asyncio 

def score_gradients(self, response, uid):
    
    # Create Dataloader
    dataloader = SubsetFalconLoader(
        batch_size=self.config.neuron.local_batch_size_train, sequence_length=1024, rows=response.dataset_indices
    )

    # Train data for one epoch
    for index, batch in enumerate(dataloader):

        inputs = batch.to(self.device)

        # Forward pass
        outputs = self.model(input_ids=inputs, labels=inputs)

        loss = outputs.loss

        # Backward Pass
        loss.backward()

        # Copy gradients
        gradients = tuple(param.grad.detach().cpu().clone() if param.grad is not None else torch.zeros_like(param) for param in self.model.parameters())

        # Accumulate Gradients
        self.grad_averager.accumulate_grads_(batch_size=len(inputs))
        
        # Zero Gradients
        self.opt.zero_grad()
    
        if not self.config.neuron.dont_wandb_log:
            self.wandb.log({"loss": outputs.loss.detach().item()})

    # Store summed random gradients in the synapse
    gradients = float(sum(gradients[response.gradient_test_index]))
        
    bt.logging.info(f"Local Validator Sum of Layer {response.gradient_test_index}'s Gradients are: {gradients}")
    bt.logging.info(f"UID {uid} Sum of Layer {response.gradient_test_index}'s Gradients are: {response.gradients}")

    score = 1-(abs(gradients-response.gradients))

    return score


async def score_blacklist(self, uids, scores):
    
    peer_ids = []

    for i, uid in enumerate(uids):

        peer_id = await self.map_uid_to_peerid(uid)
        if peer_id == None:
            scores[i] = 0.0
        else:
            scores[i] = 1.0
        peer_ids.append(peer_id)

    return peer_ids, scores

async def score_bandwidth(self, peer_ids, scores):

    for i, peer in enumerate(peer_ids):
        
        if peer is None:
            scores[i] = 0
        
        else:
        
            try:
                start_time = time.perf_counter()

                metadata, tensors = await asyncio.wait_for(self.load_state_from_miner(peer), timeout=60)
                end_time = time.perf_counter()

                if (metadata is None) or (tensors is None):
                    scores[i] = 0
                else:
                    scores[i] = 1 / (end_time - start_time)

                bt.logging.info(f"Reward for peer {peer} is {scores[i]}")

            except Exception as e:

                bt.logging.info(f"Failed to download state from {peer} - {repr(e)}")
                scores[i] = 0
                bt.logging.info(f"Reward for peer {peer} is {scores[i]}")

    return scores
                     
async def get_rewards(
    self,
    uids: List[int],
    responses: list,
    all_reduce: bool,
) -> torch.FloatTensor:
    """
    Returns a tensor of rewards for the given query and responses.

    Args:
    - uids (List[int]): A list of uids that were queried.
    - responses (List): A list of all the responses from the queried uids.
    - all_reduce (bool): A boolean representing wether the all_reduce synapse was called.
    - responses (List[float]): A list of responses from the miners.

    Returns:
    - torch.FloatTensor: A tensor of rewards for the given query and responses.
    """
    # scores = torch.FloatTensor([0 for _ in uids]).to(self.device)
    # # Check if peer is connected to DHT & run_id and blacklist them if they are not
    # peer_ids, scores = await score_blacklist(self, uids, scores)
    # bt.logging.info(f"DHT Blacklist Scores: {scores}")
    # breakpoint()
    # # Score miners bandwidth
    # scores = await score_bandwidth(self, peer_ids, scores)
    # bt.logging.info(f"Bandwidth Scores: {scores}")

    if (responses == [[]]) or ([response[0] for response in responses if response[0].dendrite.status_code == 200 and response[0].loss != []] == []):
        
        if all_reduce:
            # Now that we've called all_reduce on all available UIDs only score a sample of them to spread scoring burden across all validators
            uids = await get_random_uids(self, dendrite=self.dendrite, k=self.config.neuron.sample_size)
            # Set up the scores tensor
            scores = torch.FloatTensor([0 for _ in uids]).to(self.device)
            # Check if peer is connected to DHT & run_id and blacklist them if they are not
            peer_ids, scores = await score_blacklist(self, uids, scores)
            bt.logging.info(f"DHT Blacklist Scores: {scores}")
            # Score miners bandwidth
            scores = await score_bandwidth(self, peer_ids, scores)
            bt.logging.info(f"Bandwidth Scores: {scores}")
        else:
            # Set up the scores tensor
            scores = torch.FloatTensor([0 for _ in uids]).to(self.device)
    else:
        scores = torch.FloatTensor([1 if response.dendrite.status_code == 200 and response.loss != [] else 0 for _, response in zip(uids, responses[0])]).to(self.device)
        bt.logging.info(f"Timeout Scores: {scores}")

        if ((self.step % 10)==0):
            # Periodically check if peer is connected to DHT & run_id and blacklist them if they are not
            peer_ids, scores = await score_blacklist(self, uids, scores)
            bt.logging.info(f"DHT Blacklist Scores: {scores}")

        # Re-calculate gradients for a subset of uids and score the difference between local gradients and the miner's gradients
        test_uids_index = [uid_index for uid_index, _ in enumerate(uids) if responses[0][uid_index].dendrite.status_code == 200]
        
        test_uids_sample_index = random.sample(test_uids_index, k = min(4, len(test_uids_index)))
        
        scores = torch.FloatTensor([scores[uid_index] * score_gradients(self, responses[0][uid_index], uid_index) 
                                    if uid_index in test_uids_sample_index else scores[uid_index] 
                                    for uid_index,_ in enumerate(uids)]).to(self.device)
        bt.logging.info(f"Gradient Scores: {scores}")

    return scores

