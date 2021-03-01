# Copyright 2021 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Any, Dict, List, Optional, Tuple, Union
import warnings
import os

import torch
from torch import nn
from torch.utils.data.dataset import Dataset
from torch.utils.data.distributed import DistributedSampler

from ..trainer import Trainer
from ..trainer_pt_utils import (
    DistributedLengthGroupedSampler,
    SequentialDistributedSampler,
    nested_detach,
    nested_numpify,
    reissue_pt_warnings,
)
from ..file_utils import (
    WEIGHTS_NAME,
)
from ..utils import logging
from .training_args_sm import is_smdistributed_available
import collections
from ..trainer_utils import (
    PREFIX_CHECKPOINT_DIR
)
from ..modeling_utils import PreTrainedModel


logger = logging.get_logger(__name__)


if is_smdistributed_available():
    import smdistributed.modelparallel.torch as smp

    @smp.step()
    def forward_backward(model, inputs, gradient_accumulation_steps=1):
        outputs = model(**inputs)
        loss = outputs["loss"] if isinstance(outputs, dict) else outputs[0]
        loss = loss / gradient_accumulation_steps
        model.backward(loss)
        return loss

    @smp.step()
    def forward_only(model, inputs):
        return model(**inputs)

    def smp_gather(tensor):
        if isinstance(tensor, (list, tuple)):
            return type(tensor)(smp_gather(t) for t in tensor)
        elif isinstance(tensor, dict):
            return type(tensor)({k: smp_gather(v) for k, v in tensor.items()})
        elif not isinstance(tensor, torch.Tensor):
            raise TypeError(
                f"Can't gather the values of type {type(tensor)}, only of nested list/tuple/dicts of tensors."
            )
        all_tensors = smp.allgather(tensor, smp.CommGroup.DP_GROUP)
        return torch.cat([t.cpu() for t in all_tensors], dim=0)

    def nested_smp_concat(tensor):
        if isinstance(tensor, (list, tuple)):
            return type(tensor)(nested_smp_concat(t) for t in tensor)
        elif isinstance(tensor, dict):
            return type(tensor)({k: nested_smp_concat(v) for k, v in tensor.items()})
        # It doesn't seem possible to check here if `tensor` is a StepOutput because StepOutput lives in `smp.step`
        # which is also the name of the decorator so Python is confused.
        return tensor.concat().detach().cpu()


class SageMakerTrainer(Trainer):
    def __init__(self, args=None, **kwargs):
        self.is_model_parallel_enabled = is_smdistributed_available() and args.mp_parameters != ""
        super().__init__(args=args, **kwargs)
        self.state.is_local_process_zero = self.is_local_process_zero()

    def is_local_process_zero(self) -> bool:
        """
        Whether or not this process is the local (e.g., on one machine if training in a distributed fashion on several
        machines) main process.
        """
        if self.is_model_parallel_enabled:
            print("smp.rank() ", smp.rank(), "smp.local_rank() ", smp.local_rank(), "smp.mp_rank() ", smp.mp_rank()," smp.dp_rank()",  smp.dp_rank())
            return smp.local_rank() == 0
        else:
            return super.is_local_process_zero()

    def is_world_process_zero(self) -> bool:
        """
        Whether or not this process is the global main process (when training in a distributed fashion on several
        machines, this is only going to be :obj:`True` for one process).
        """
        if self.is_model_parallel_enabled:
            return smp.rank() == 0 and smp.local_rank() == 0 and smp.mp_rank() == 0 and smp.dp_rank() == 0
        else:
            return super.is_world_process_zero()

    def _get_train_sampler(self):
        if self.is_model_parallel_enabled:
            if isinstance(self.train_dataset, torch.utils.data.IterableDataset) or not isinstance(
                    self.train_dataset, collections.abc.Sized
            ):
                return None
            if self.args.group_by_length:
                return DistributedLengthGroupedSampler(
                    self.train_dataset, self.args.train_batch_size, num_replicas=smp.dp_size(), rank=smp.dp_rank()
                )
            else:
                return DistributedSampler(self.train_dataset, num_replicas=smp.dp_size(), rank=smp.dp_rank())
        else:
            return super()._get_train_sampler()

    def _get_eval_sampler(self, eval_dataset: Dataset) -> Optional[torch.utils.data.sampler.Sampler]:
        if self.is_model_parallel_enabled:
            return SequentialDistributedSampler(eval_dataset, num_replicas=smp.dp_size(), rank=smp.dp_rank())
        else:
            return super()._get_eval_sampler(eval_dataset)

    def _wrap_model(self, model, training=True):
        if self.is_model_parallel_enabled:
            # Wrapping the base model twice in a DistributedModel will raise an error.
            if isinstance(self.model_wrapped, smp.model.DistributedModel):
                return self.model_wrapped
            return smp.DistributedModel(model)
        else:
            return super()._wrap_model(model)

    def create_optimizer_and_scheduler(self, num_training_steps: int):
        super().create_optimizer_and_scheduler(num_training_steps)
        if self.is_model_parallel_enabled:
            self.optimizer = smp.DistributedOptimizer(self.optimizer)

    def training_step(self, model: nn.Module, inputs: Dict[str, Union[torch.Tensor, Any]]) -> torch.Tensor:
        if self.is_model_parallel_enabled:
            model.train()
            inputs = self._prepare_inputs(inputs)
            loss_mb = forward_backward(model, inputs, self.args.gradient_accumulation_steps)
            return loss_mb.reduce_mean().detach().to(self.args.device)
        else:
            return super().training_step(model, inputs)

    def _gather_and_numpify(self, tensors, name):
        if self.is_model_parallel_enabled:
            tensors = smp_gather(tensors)
            return nested_numpify(tensors)
        else:
            return super()._gather_and_numpify(tensors, name)

    def _save(self, output_dir: Optional[str] = None):
        output_dir = output_dir if output_dir is not None else self.args.output_dir
        os.makedirs(output_dir, exist_ok=True)
        logger.info("Saving model checkpoint to %s", output_dir)
        # Save a trained model and configuration using `save_pretrained()`.
        # They can then be reloaded using `from_pretrained()`
        if not isinstance(self.model, PreTrainedModel):
            logger.info("Trainer.model is not a `PreTrainedModel`, only saving its state dict.")
            model_dict = self.model.local_state_dict() # save the partial model
            smp.save(
                model_dict,
                os.path.join(output_dir, WEIGHTS_NAME),
                partial=True,
            )
        else:
            ## TODO this needs to similar to save_pretrained in modelling_utils.py
            print("Saving pretrained model")
            import pdb;pdb.set_trace()
            model_dict = self.model.local_state_dict() # save the partial model
            smp.save(
                model_dict,
                os.path.join(output_dir, WEIGHTS_NAME),
                partial=True,
            )

            print("Only supporting non pretraining models")
        # Good practice: save your training arguments together with the trained model
        torch.save(self.args, os.path.join(output_dir, "training_args.bin"))
        print("Exiting from _save method")


    def save_model(self, output_dir: Optional[str] = None):
        """
        Will save the model, so you can reload it using :obj:`from_pretrained()`.

        Will only save from the world_master process (unless in TPUs).
        """

        if self.is_model_parallel_enabled:
            if smp.dp_rank() == 0:
                self._save(output_dir)
        else:
            super.save_model()

        # If on sagemaker and we are saving the main model (not a checkpoint so output_dir=None), save a copy to
        # SM_MODEL_DIR for easy deployment.
        if output_dir is None and os.getenv("SM_MODEL_DIR") is not None:
            self.save_model(output_dir=os.getenv("SM_MODEL_DIR"))

    def _save_checkpoint(self, model, trial, metrics=None):
        # Save model checkpoint
        # Does not have support for rotate_checkpoints method like main trainer
        checkpoint_folder = f"{PREFIX_CHECKPOINT_DIR}-{self.state.global_step}"
        output_dir = os.path.join(self.args.output_dir, checkpoint_folder)
        self.save_model(output_dir)

        if smp.dp_rank() == 0:
            print("Saving optimizer state")
            opt_dict = self.optimizer.state_dict()
            smp.save(opt_dict,
                     os.path.join(output_dir, "optimizer.pt"),
                     partial=True)
            if self.is_world_process_zero():
                print("Saving Learning Rate state")
                with warnings.catch_warnings(record=True) as caught_warnings:
                    torch.save(self.lr_scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt"))
                reissue_pt_warnings(caught_warnings)
        print("Saved optimizer state")
        # Save the Trainer state
        if self.is_world_process_zero():
            print("Saving trainer  state")
            self.state.save_to_json(os.path.join(output_dir, "trainer_state.json"))

    def _load_optimizer_and_scheduler(self, checkpoint):
        """If optimizer and scheduler states exist, load them."""
        if checkpoint is None:
            return

        if os.path.isfile(os.path.join(checkpoint, "optimizer.pt")) and os.path.isfile(
            os.path.join(checkpoint, "scheduler.pt")
        ):
            self.optimizer.load_state_dict(
                smp.load(os.path.join(checkpoint, "optimizer.pt"), partial=True)
            )
            with warnings.catch_warnings(record=True) as caught_warnings:
                self.lr_scheduler.load_state_dict(torch.load(os.path.join(checkpoint, "scheduler.pt")))
            reissue_pt_warnings(caught_warnings)

    def prediction_step(
        self,
        model: nn.Module,
        inputs: Dict[str, Union[torch.Tensor, Any]],
        prediction_loss_only: bool,
        ignore_keys: Optional[List[str]] = None,
    ) -> Tuple[Optional[float], Optional[torch.Tensor], Optional[torch.Tensor]]:
        if self.is_model_parallel_enabled:
            has_labels = all(inputs.get(k) is not None for k in self.label_names)
            inputs = self._prepare_inputs(inputs)

            if ignore_keys is None:
                if hasattr(self.model, "config"):
                    ignore_keys = getattr(self.model.config, "keys_to_ignore_at_inference", [])
                else:
                    ignore_keys = []

            with torch.no_grad():
                raw_outputs = forward_only(model, inputs)
                if has_labels:
                    if isinstance(raw_outputs, dict):
                        loss_mb = raw_outputs["loss"]
                        logits_mb = tuple(v for k, v in raw_outputs.items() if k not in ignore_keys + ["loss"])
                    else:
                        loss_mb = raw_outputs[0]
                        logits_mb = raw_outputs[1:]

                    loss = loss_mb.reduce_mean().detach().cpu()
                    logits = nested_smp_concat(logits_mb)
                else:
                    loss = None
                    if isinstance(raw_outputs, dict):
                        logits_mb = tuple(v for k, v in raw_outputs.items() if k not in ignore_keys)
                    else:
                        logits_mb = raw_outputs
                    logits = nested_smp_concat(logits_mb)

            if prediction_loss_only:
                return (loss, None, None)

            if len(logits) == 1:
                logits = logits[0]

            if has_labels:
                labels = nested_detach(tuple(inputs.get(name) for name in self.label_names))
                if len(labels) == 1:
                    labels = labels[0]
            else:
                labels = None

            return (loss, logits, labels)
        else:
            return super().prediction_step(model, inputs, prediction_loss_only, ignore_keys=ignore_keys)
