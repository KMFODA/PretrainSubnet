import copy
import gc
import os
import tempfile
import threading
import time
from functools import partial

import bittensor as bt
import hivemind
import psutil
import torch
from distributed_training.averaging.averagers import DTGradAverager, DTStateAverager
from distributed_training.utils.progress_tracker import get_global_epoch
from huggingface_hub import create_tag, hf_hub_download, scan_cache_dir, upload_folder
from transformers import AutoModelForCausalLM


class ModelLoadingManager:
    def __init__(self):
        self.loading_lock = threading.Lock()
        self._is_loading = False
        self._last_loaded_epoch = None

    @property
    def is_loading(self):
        with self.loading_lock:
            return self._is_loading

    @property
    def last_loaded_epoch(self):
        with self.loading_lock:
            return self._last_loaded_epoch

    def set_loading_state(self, is_loading, epoch=None):
        with self.loading_lock:
            self._is_loading = is_loading
            if not is_loading and epoch is not None:
                self._last_loaded_epoch = epoch


def load_model_optimizer_gradient_averager(self, model_name, epoch):
    bt.logging.info(
        f"CPU Memory Before Loading State {psutil.virtual_memory().available / 10**9} GB"
    )
    # Delete existing model
    if hasattr(self, "model"):
        if hasattr(self.model.model.transformer.wte, "weight"):
            del self.model.model.transformer.wte.weight
        if hasattr(self.model.model.transformer.wte, "norm"):
            del self.model.model.transformer.wte.norm
        if hasattr(self.model.model.transformer.wpe, "weight"):
            del self.model.model.transformer.wpe.weight
        if hasattr(self.model.model.transformer.wpe, "norm"):
            del self.model.model.transformer.wpe.norm
        if hasattr(self.model.model.transformer, "wte"):
            del self.model.model.transformer.wte
        if hasattr(self.model.model.transformer, "wpe"):
            del self.model.model.transformer.wpe
        del self.model
        gc.collect()
        torch.cuda.empty_cache()

    # Load a new model
    self.model = (
        AutoModelForCausalLM.from_pretrained(
            model_name, revision=str(epoch), trust_remote_code=True
        )
        if epoch
        else AutoModelForCausalLM.from_pretrained(model_name, trust_remote_code=True)
    )
    # Move the model to the appropriate device
    self.model = self.model.to(self.device)
    self.model.config.block_list = []

    # Delete any historic model references in GlobalOptimManager # TODO Do we still need this in here?
    if hasattr(self, "opt") and (len(self.opt.mng.module_weight_config_triple) > 2):
        self.inner_optimizer.mng.module_weight_config_triple = (
            self.inner_optimizer.mng.module_weight_config_triple[-2:]
        )

    # Load a new optimizer
    param_dict = {pn: p for pn, p in self.model.named_parameters()}
    param_dict = {pn: p for pn, p in param_dict.items() if p.requires_grad}
    # create optim groups. Any parameters that is 2D will be weight decayed, otherwise no.
    # i.e. all weight tensors in matmuls + embeddings decay, all biases and layernorms don't.
    decay_params = [p for n, p in param_dict.items() if p.dim() >= 2]
    nodecay_params = [p for n, p in param_dict.items() if p.dim() < 2]
    optim_groups = [
        {"params": decay_params, "weight_decay": self.weight_decay},
        {"params": nodecay_params, "weight_decay": 0.0},
    ]
    # Try to load optimizer state if it exists
    try:
        optimizer_state = torch.load(
            hf_hub_download(
                repo_id=model_name,
                filename="optimizer.pt",
                revision=str(epoch),
            ),
            weights_only=True,
            map_location="cpu",
        )

        # Delete existing optimizer
        if hasattr(self, "opt"):
            self.inner_optimizer.param_groups = optim_groups
        else:
            self.inner_optimizer = torch.optim.AdamW(
                optim_groups,
                lr=optimizer_state["learning_rate"],
                betas=(0.9, 0.95),
                eps=1e-8,
                weight_decay=0.1,
            )

        # self.inner_optimizer.load_state_dict(optimizer_state["optimizer_state_dict"])

        del optimizer_state
        gc.collect()
        torch.cuda.empty_cache()

        bt.logging.info(f"Successfully loaded optimizer state for epoch {epoch}")

    except Exception as e:
        bt.logging.warning(
            f"No optimizer state found or failed to load: {str(e)}. Initializing fresh optimizer."
        )
        # Initialize fresh optimizer
        self.inner_optimizer = torch.optim.AdamW(
            optim_groups,
            lr=self.learning_rate_maximum,
            betas=(0.9, 0.95),
            eps=1e-8,
            weight_decay=0.1,
        )

    del (
        param_dict,
        decay_params,
        nodecay_params,
        optim_groups,
    )
    gc.collect()
    torch.cuda.empty_cache()

    self.outer_optimizer = partial(torch.optim.SGD, lr=0.7, momentum=0.9, nesterov=True)

    # Delete existing gradient averager
    if hasattr(self, "grad_averager"):
        for i in self.grad_averager.main_parameters:
            del i
        gc.collect()
        torch.cuda.empty_cache()

    else:
        self.state_averager = DTStateAverager(
            dht=self.dht,
            prefix=f"{self.config.neuron.run_id}_state_averager",
            optimizer=self.outer_optimizer,
            params=self.model.parameters(),
            initialize_optimizer=True,
            offload_optimizer=self.offload_optimizer,
            custom_gradients=self.offload_optimizer,
            min_group_size=self.config.neuron.min_group_size,
            min_matchmaking_time=30.0,
            request_timeout=10.0,
            next_chunk_timeout=45.0,
            allreduce_timeout=self.allreduce_timeout - 30.0 - 15.0,
            start=True,
        )

    # Delete existing gradient averager
    if hasattr(self, "grad_averager"):
        for i in self.grad_averager.main_parameters:
            del i
        gc.collect()
        torch.cuda.empty_cache()

        # Reset gradient buffers and parameters
        self.grad_averager.parameters = tuple(self.model.parameters())

        # self.grad_averager.reset_accumulated_grads_()

    else:
        # Load a new gradient averager
        self.grad_averager = DTGradAverager(
            dht=self.dht,
            main_parameters=self.state_averager.main_parameters,
            offloaded_optimizer=self.state_averager.optimizer,
            prefix=f"{self.config.neuron.run_id}_grad_averager",
            compression=hivemind.Uniform8BitQuantization(),
            state_compression=hivemind.Uniform8BitQuantization(),  # TODO Not sure this is necessary when we have the above
            min_group_size=self.config.neuron.min_group_size,
            min_matchmaking_time=30.0,
            request_timeout=10.0,
            next_chunk_timeout=45.0,
            allreduce_timeout=self.allreduce_timeout - 30.0 - 15.0,
            start=True,
        )

    bt.logging.info(
        f"CPU Memory After Loading State {psutil.virtual_memory().available / 10**9} GB"
    )


def load_state_from_peer(self, epoch=None):
    try:
        state_loaded = False
        if epoch is None:
            self.global_progress.epoch = get_global_epoch(self)
            epoch = self.global_progress.epoch

        bt.logging.info("Model Weights Before Loading State")
        current_model_weights_sample = copy.copy(
            [layer for layer in self.model.parameters()][-2][-10:].tolist()
        )
        bt.logging.info(current_model_weights_sample)

        bt.logging.info(f"Old Model Tag: {self.local_progress.epoch}")

        if self.global_progress.epoch is not None:
            bt.logging.info(
                f"Latest Model State Found On The HF Hub With The Tag: {self.global_progress.epoch}. Loading That Model State."
            )

            # Load model state with max retries
            MAX_ATTEMPTS = 3
            attempt = 0

            while attempt < MAX_ATTEMPTS:
                try:
                    load_model_optimizer_gradient_averager(
                        self, self.config.neuron.model_name, epoch
                    )
                    break

                except Exception as e:
                    attempt += 1
                    if attempt == MAX_ATTEMPTS:
                        raise Exception(
                            f"Failed to load model after {MAX_ATTEMPTS} attempts: {str(e)}"
                        )
                    bt.logging.warning(
                        f"Failed to load model, retrying. Attempt {attempt}/{MAX_ATTEMPTS}. Error {str(e)}"
                    )

            state_loaded = True

            bt.logging.info("Model Weights After Loading State")
            new_model_weights_sample = copy.copy(
                [layer for layer in self.model.parameters()][-2][-10:].tolist()
            )
            bt.logging.info(new_model_weights_sample)

            self.local_progress.epoch = self.global_progress.epoch
            self.local_progress.samples_accumulated = 0
            bt.logging.info(f"New Model Tag: {self.global_progress.epoch}")

            # Clean up old cache
            try:
                cleanup_old_cache(self)
            except Exception as e:
                bt.logging.warning(f"Failed to cleanup cache: {str(e)}")

        else:
            bt.logging.info(f"Model With Tag: {epoch} Does Not Exist")

        return state_loaded

    except Exception as e:
        bt.logging.error(f"Error loading state: {str(e)}")
        return False


def cleanup_old_cache(self, repo_id=None, current_revision=None):
    """Helper method to clean up old cache files"""
    if repo_id is None:
        repo_id = self.config.neuron.model_name
        current_revision = self.model.config._commit_hash

    cache_info = scan_cache_dir()
    bt.logging.info("Cache clearing warnings:")
    bt.logging.info(f"{cache_info.warnings}")

    for repo in cache_info.repos:
        if repo.repo_id == repo_id:
            revisions = sorted(
                repo.revisions, key=lambda r: r.last_modified, reverse=True
            )

            bt.logging.info(
                f"Found {len(revisions)} model revisions in .cache folder. Proceeding to delete all non-current revision."
            )
            for revision in revisions:
                if (current_revision is not None) and (
                    revision.commit_hash == current_revision
                ):
                    bt.logging.info(
                        f"Skipping cache for current revision {revision.commit_hash}"
                    )
                    continue
                else:
                    bt.logging.info(
                        f"Deleting cache for revision {revision.commit_hash}"
                    )
                    cache_info.delete_revisions(revision.commit_hash).execute()
            break


def save_and_upload_state(self, epoch, results):
    """Unified function to save and upload both model and optimizer state"""
    batch_size = sum(
        [result for result in results[1]["gathered"].values() if result is not None]
    )
    participating_peers = results[1]["participating_peers"]
    failed_peers = results[1]["failed_peers"]
    attempt = 0
    while attempt < self.model_upload_retry_limit:
        try:
            with tempfile.TemporaryDirectory() as tmp_folder:
                bt.logging.info(
                    f"Preparing model and optimizer state for epoch {epoch}"
                )
                self.model.save_pretrained(tmp_folder)
                # Save optimizer state
                optimizer_state = {
                    "optimizer_state_dict": self.state_averager.optimizer.state_dict(),
                    "learning_rate": self.learning_rate_maximum,
                    "epoch": epoch,
                }
                torch.save(optimizer_state, os.path.join(tmp_folder, "optimizer.pt"))

                bt.logging.info(
                    f"Uploading model and optimizer states to repo: {self.config.neuron.model_name}"
                )

                # Upload everything in one go
                commit_message = f"Epoch {epoch}. Batch Size {batch_size}. Peers {len(participating_peers) - len(failed_peers)}."
                upload_folder(
                    folder_path=tmp_folder,
                    repo_id=self.config.neuron.model_name,
                    repo_type="model",
                    commit_message=commit_message,
                )

                # Create a tag for this version
                create_tag(
                    self.config.neuron.model_name,
                    repo_type="model",
                    tag=str(epoch),
                    tag_message=commit_message,
                )

                bt.logging.info(
                    f"Successfully pushed new model and optimizer state with tag {epoch} to repo: {self.config.neuron.model_name}"
                )
                return True

        except Exception as e:
            attempt += 1
            bt.logging.warning(
                f"Failed to upload state to HF hub, Retrying. Attempt {attempt}/{self.model_upload_retry_limit}. Error: {str(e)}"
            )
            if attempt < self.model_upload_retry_limit:
                time.sleep(self.model_upload_retry_delay)
            else:
                bt.logging.error(
                    "Maximum retry limit reached. Unable to upload state to HF Hub."
                )
                raise
    return False
