import argparse
import asyncio
import base64
import logging
import os
import random
import time
from contextlib import contextmanager

import hivemind
import torch
from hivemind.averaging.group_info import GroupInfo
from hivemind.dht import DHTID
from hivemind.utils.logging import use_hivemind_log_handler
from transformers import AutoModelForCausalLM, AutoTokenizer

from template.base.neuron import BaseNeuron
from template.data.dataset import SubsetFalconLoader
from template.utils.hivemind import DTGradientAverager
from template.utils.misc import init_dht, setup_logging

# Parse command-line arguments
parser = argparse.ArgumentParser(description="Run a distributed training script with Hivemind.")
parser.add_argument("--prefix", type=str, required=True, help="Prefix for DHT and gradient averager.")
args = parser.parse_args()

# Logging
logger = logging.getLogger()
logger.setLevel(logging.DEBUG) 

use_hivemind_log_handler("nowhere")

# Delete the logfile if it exists
logfile = 'logfile.log'
if os.path.exists(logfile):
    os.remove(logfile)

# Create a file handler
handler = logging.FileHandler(logfile)

# Create a formatter and add it to the handler
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)

# Add the handler to the logger
logger.addHandler(handler)

# DHT
version = "4"
address = "104.167.17.11"

announce_maddrs = [f"/ip{version}/{address}/tcp/46629"]

dht = hivemind.DHT(
    host_maddrs=[
                f"/ip4/0.0.0.0/tcp/46629",
                f"/ip4/0.0.0.0/udp/46629/quic",
                ],
    #initial_peers=[""], 
    
    announce_maddrs=announce_maddrs,
    start=True
)
print(dht.get_visible_maddrs())

# Write the visible_maddrs to a text file
with open('visible_maddrs.txt', 'w') as f:
    for maddr in dht.get_visible_maddrs():
        f.write(str(maddr) + "\n")

time.sleep(16)

model = AutoModelForCausalLM.from_pretrained("kmfoda/gpt2-250m")
# Move the model to the appropriate device
model = model.to("cuda")

# Set up a decentralized optimizer that will average with peers in background
opt = torch.optim.AdamW(model.parameters(), lr=0.001)

global_target_batch_size = 600  # set your target batch size
grad_averager = DTGradientAverager(
    model.parameters(), 
    dht=dht, 
    prefix=args.prefix,
    start=True,
    compression=hivemind.Uniform8BitQuantization(),
)

tracker = hivemind.optim.progress_tracker.ProgressTracker(
    dht=dht, 
    prefix=f"{args.prefix}_tracker", 
    target_batch_size=global_target_batch_size,
    start=True
)

#total_batch_size = 0
step_scheduled = False
local_epoch, local_samples = 0, 0

#* Make custom group:
time.sleep(5)
loop = asyncio.new_event_loop()
group_is_set = False
# _p2p = loop.run_until_complete(dht.replicate_p2p())

while True:
    print("Starting training..")
    # for i in range(0, 1):
    print("Getting new data..")
    dataloader = SubsetFalconLoader(
        batch_size=5, sequence_length=1024, rows=random.choices(range(0,968000015), k = 1000)
    )

    for i, batch in enumerate(dataloader):
        
        inputs = batch.to("cuda")

        # Forward pass
        outputs = model(input_ids=inputs, labels=inputs)
        
        loss = outputs.loss
        scaled_loss = loss / global_target_batch_size / 5 # Minus batch size (in this case 1)
        print(loss)
        scaled_loss.backward()
        
        # Only use this if reuse_grad_buffers=False
        grad_averager.accumulate_grads_(batch_size=5)
        
        local_samples += 5  # increment the total batch size
        
        tracker.report_local_progress(local_epoch, local_samples)
        print("local samples:", tracker.local_progress.samples_accumulated, "global_samples:", tracker.global_progress.samples_accumulated)
        print("local epoch:", tracker.local_progress.epoch, "global epoch", tracker.global_progress.epoch)

        # aggregate gradients and perform optimizer step when target batch size is reached
        if tracker.global_progress.samples_accumulated >= global_target_batch_size:
            if not group_is_set:
                _p2p = loop.run_until_complete(dht.replicate_p2p())

                group_id = base64.b64decode(b'akGgUCKXywtpOCU76x9Ncxzi2qk=')
                ordered_peer_ids = [dht.peer_id] 
                remote_peer = loop.run_until_complete(_p2p.list_peers())
                remote_peer = [peer.peer_id for peer in remote_peer]
                ordered_peer_ids += remote_peer
                ordered_peer_ids.sort(key=lambda peer: peer.xor_id)
                custom_group = GroupInfo(group_id, tuple(ordered_peer_ids), gathered=None)
                print(custom_group)
                group_is_set = True
                
            with tracker.pause_updates():
                print("grad stepping..")
                #grad_averager.step(custom_group_info=custom_group)
                grad_step = grad_averager.step(allow_retries=False, wait=False, custom_group_info=custom_group)
                if gradient_averaging_step.done():
                    with grad_averager.use_averaged_gradients():  # this will fill param.grads with aggregated gradients
                        print("opt stepping..")
                        opt.step()  # update model parameters using averaged gradients
                    grad_averager.reset_accumulated_grads_()  # prepare for next step
                    local_epoch = tracker.update_epoch(local_epoch + 1)
                    local_samples = 0  