# Copyright The PyTorch Lightning team.
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
r"""
BatchSizeFinder
===============

Finds optimal batch size
"""

import os
import uuid
from copy import deepcopy
from typing import List, Optional, Tuple, Union

from torch.utils.data.dataloader import DataLoader
from typing_extensions import TypedDict

import pytorch_lightning as pl
from pytorch_lightning.callbacks.base import Callback
from pytorch_lightning.utilities.cloud_io import get_filesystem
from pytorch_lightning.utilities.distributed import rank_zero_info
from pytorch_lightning.utilities.exceptions import _TunerExitException, MisconfigurationException
from pytorch_lightning.utilities.memory import garbage_collection_cuda, is_oom_error
from pytorch_lightning.utilities.parsing import lightning_getattr, lightning_hasattr, lightning_setattr
from pytorch_lightning.utilities.warnings import rank_zero_warn


class BatchSizeFinder(Callback):
    def __init__(
        self,
        mode: str = "power",
        steps_per_trial: int = 3,
        init_val: int = 2,
        max_trials: int = 25,
        batch_arg_name: str = "batch_size",
    ) -> None:
        """Callback try to find the largest batch size for a given model that does not give an out of memory (OOM)
        error. It works with both training and evalation. All you need to do is add it as a callback inside Trainer
        and call ``trainer.fit/validate/test/predict()``. Internally it calls the respective step function
        ``steps_per_trial`` times for each batch size until one of the batch size generates and OOM error.

        Args:
            mode: search strategy to update the batch size:

                - ``'power'``: Keep multiplying the batch size by 2, until we get an OOM error.
                - ``'binsearch'``: Initially keep multiplying by 2 and after encountering an OOM error
                    do a binary search between the last successful batch size and the batch size that failed.

            steps_per_trial: number of steps to run with a given batch size.
                Ideally 1 should be enough to test if a OOM error occurs,
                however in practice a few are needed.

            init_val: initial batch size to start the search with.

            max_trials: max number of increase in batch size done before
               algorithm is terminated

            batch_arg_name: name of the attribute that stores the batch size.
                It is expected that the user has provided a model or datamodule that has a hyperparameter
                with that name. We will look for this attribute name in the following places

                - ``model``
                - ``model.hparams``
                - ``trainer.datamodule`` (the datamodule passed to the tune method)
        """
        supported_modes = ("power", "binsearch")
        mode = mode.lower()
        if mode not in supported_modes:
            raise MisconfigurationException(f"`mode` should be one of {supported_modes}")

        self.mode = mode
        self.steps_per_trial = steps_per_trial
        self.init_val = init_val
        self.max_trials = max_trials
        self.batch_arg_name = batch_arg_name
        self.optimal_batch_size = init_val

        self._early_exit = False

        from pytorch_lightning.loggers.base import LightningLoggerBase

        class _BatchSizeFinderDumpedParams(TypedDict):
            callbacks: List[Callback]
            logger: Optional[LightningLoggerBase]
            max_steps: int
            global_step: Optional[None]
            limit_val_batches: Union[int, float]
            limit_eval_batches: Union[int, float]

        self._dumped_params: _BatchSizeFinderDumpedParams = {}

    def scale_batch_size(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        if trainer.fast_dev_run:
            rank_zero_warn("Skipping batch size scaler since `fast_dev_run` is enabled.")
            return

        if not lightning_hasattr(pl_module, self.batch_arg_name):
            raise MisconfigurationException(
                f"Field {self.batch_arg_name} not found in both `model` and `model.hparams`"
            )

        if not lightning_hasattr(pl_module, self.batch_arg_name):
            raise MisconfigurationException(
                f"Field {self.batch_arg_name} not found in both `model` and `model.hparams`"
            )

        if (
            hasattr(pl_module, self.batch_arg_name)
            and hasattr(pl_module, "hparams")
            and self.batch_arg_name in pl_module.hparams
        ):
            rank_zero_warn(
                f"Field `model.{self.batch_arg_name}` and `model.hparams.{self.batch_arg_name}` are mutually exclusive!"
                f" `model.{self.batch_arg_name}` will be used as the initial batch size for scaling."
                " If this is not the intended behavior, please remove either one."
            )

        if not trainer._data_connector._train_dataloader_source.is_module():
            raise MisconfigurationException(
                "The batch scaling feature cannot be used with dataloaders passed directly to `.fit()`."
                " Please disable the feature or incorporate the dataloader into the model."
            )

        # Arguments we adjust during the batch size finder, save for restoring
        self._dump_params(trainer)

        # Set to values that are required by the algorithm
        self._reset_params(trainer)

        # Save initial model, that is loaded after batch size is found
        save_path = os.path.join(trainer.default_root_dir, f".scale_batch_size_temp_model_{uuid.uuid4()}.ckpt")
        trainer.save_checkpoint(save_path)

        if trainer.progress_bar_callback:
            trainer.progress_bar_callback.disable()

        new_size, _ = self._adjust_batch_size(trainer, value=self.init_val)

        if self.mode == "power":
            new_size = self._run_power_scaling(trainer, pl_module, new_size)
        elif self.mode == "binsearch":
            new_size = self._run_binary_scaling(trainer, pl_module, new_size)

        garbage_collection_cuda()

        if trainer.is_global_zero:
            trainer.checkpoint_connector.restore(save_path)
            fs = get_filesystem(save_path)
            if fs.exists(save_path):
                fs.rm(save_path)

        # global step and current epoch are incremented before saved in checkpoint
        trainer.fit_loop.global_step -= 1
        trainer.fit_loop.current_epoch -= 1

        self._restore_params(trainer)

        if trainer.progress_bar_callback:
            trainer.progress_bar_callback.enable()

        print(f"New batch size: {new_size}")
        self.optimal_batch_size = new_size

        if self._early_exit:
            raise _TunerExitException()

    def _run_power_scaling(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule", new_size: int) -> int:
        """Batch scaling mode where the size is doubled at each iteration until an OOM error is encountered."""
        for _ in range(self.max_trials):
            garbage_collection_cuda()

            try:
                self._try_loop_run(trainer)
                new_size, changed = self._adjust_batch_size(trainer, factor=2.0, desc="succeeded")

                if changed:
                    # Force the dataloaders to reset as the batch size has changed
                    self._reset_dataloaders(trainer, pl_module)
                else:
                    break
            except RuntimeError as exception:
                if is_oom_error(exception):
                    garbage_collection_cuda()
                    new_size, _ = self._adjust_batch_size(trainer)
                    break
                else:
                    raise  # some other error not memory related

        return new_size

    def _run_binary_scaling(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule", new_size: int) -> int:
        """Batch scaling mode where the size is initially is doubled at each iteration until an OOM error is
        encountered.

        Hereafter, the batch size is further refined using a binary search
        """
        low = 1
        high = None
        count = 0
        while True:
            garbage_collection_cuda()
            try:
                # run loop
                self._try_loop_run(trainer)
                count += 1
                if count > self.max_trials:
                    break
                # Double in size
                low = new_size
                if high:
                    if high - low <= 1:
                        break
                    midval = (high + low) // 2
                    new_size, changed = self._adjust_batch_size(trainer, value=midval, desc="succeeded")
                else:
                    new_size, changed = self._adjust_batch_size(trainer, factor=2.0, desc="succeeded")

                if changed:
                    # Force the dataloaders to reset as the batch size has changed
                    self._reset_dataloaders(trainer, pl_module)
                else:
                    break

            except RuntimeError as exception:
                # Only these errors should trigger an adjustment
                if is_oom_error(exception):
                    # If we fail in power mode, half the size and return
                    garbage_collection_cuda()
                    high = new_size
                    midval = (high + low) // 2
                    new_size, changed = self._adjust_batch_size(trainer, value=midval, desc="failed")

                    if changed:
                        # Force the dataloaders to reset as the batch size has changed
                        self._reset_dataloaders(trainer, pl_module)

                    if high - low <= 1:
                        break
                else:
                    raise  # some other error not memory related

        return new_size

    def _try_loop_run(self, trainer: "pl.Trainer") -> None:
        from pytorch_lightning.trainer.states import TrainerFn

        if trainer.state.fn == TrainerFn.FITTING:
            trainer.fit_loop.global_step = self._dumped_params["global_step"]
            loop = trainer.fit_loop
        elif trainer.state.fn == TrainerFn.VALIDATING:
            loop = trainer.validate_loop
        elif trainer.state.fn == TrainerFn.TESTING:
            loop = trainer.test_loop
        elif trainer.state.fn == TrainerFn.PREDICTING:
            loop = trainer.predict_loop

        loop.load_state_dict(deepcopy(self._dumped_params["loop_state_dict"]))
        loop.run()

    @staticmethod
    def _reset_dataloaders(trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        from pytorch_lightning.trainer.states import TrainerFn

        if trainer.state.fn == TrainerFn.FITTING:
            trainer.reset_train_dataloader(pl_module)
            trainer.reset_val_dataloader(pl_module)
        elif trainer.state.fn == TrainerFn.VALIDATING:
            trainer.reset_val_dataloader(pl_module)
        elif trainer.state.fn == TrainerFn.TESTING:
            trainer.reset_test_dataloader(pl_module)
        elif trainer.state.fn == TrainerFn.PREDICTING:
            trainer.reset_predict_dataloader(pl_module)

    def _dump_params(self, trainer: "pl.Trainer") -> None:
        from pytorch_lightning.trainer.states import TrainerFn

        self._dumped_params = {
            "logger": trainer.logger,
            "callbacks": trainer.callbacks,
        }

        if trainer.state.fn == TrainerFn.FITTING:
            loop = trainer.fit_loop
            self._dumped_params["global_step"] = trainer.global_step
            self._dumped_params["max_steps"] = trainer.max_steps
            self._dumped_params["limit_val_batches"] = trainer.limit_val_batches
        elif trainer.state.fn == TrainerFn.VALIDATING:
            loop = trainer.validate_loop
            self._dumped_params["limit_eval_batches"] = trainer.limit_val_batches
        elif trainer.state.fn == TrainerFn.TESTING:
            loop = trainer.test_loop
            self._dumped_params["limit_eval_batches"] = trainer.limit_test_batches
        elif trainer.state.fn == TrainerFn.PREDICTING:
            loop = trainer.predict_loop
            self._dumped_params["limit_eval_batches"] = trainer.limit_predict_batches

        self._dumped_params["loop_state_dict"] = deepcopy(loop.state_dict(force_save_progress=True))
        if hasattr(loop, "verbose"):
            self._dumped_params["loop_verbose"] = loop.verbose

    def _reset_params(self, trainer: "pl.Trainer") -> None:
        from pytorch_lightning.loggers.base import DummyLogger
        from pytorch_lightning.trainer.states import TrainerFn

        trainer.logger = DummyLogger() if trainer.logger is not None else None
        trainer.callbacks = []

        if trainer.state.fn == TrainerFn.FITTING:
            trainer.limit_val_batches = self.steps_per_trial
            trainer.fit_loop.max_steps = self.steps_per_trial
        elif trainer.state.fn == TrainerFn.VALIDATING:
            trainer.limit_val_batches = self.steps_per_trial
            trainer.validate_loop.verbose = False
        elif trainer.state.fn == TrainerFn.TESTING:
            trainer.limit_test_batches = self.steps_per_trial
            trainer.test_loop.verbose = False
        elif trainer.state.fn == TrainerFn.PREDICTING:
            trainer.limit_predict_batches = self.steps_per_trial

    def _restore_params(self, trainer: "pl.Trainer") -> None:
        from pytorch_lightning.trainer.states import TrainerFn

        trainer.logger = self._dumped_params["logger"]
        trainer.callbacks = self._dumped_params["callbacks"]

        if trainer.state.fn == TrainerFn.FITTING:
            trainer.fit_loop.global_step = self._dumped_params["global_step"]
            loop = trainer.fit_loop
            loop.max_steps = self._dumped_params["max_steps"]
            trainer.limit_val_batches = self._dumped_params["limit_val_batches"]
        elif trainer.state.fn == TrainerFn.VALIDATING:
            loop = trainer.validate_loop
            trainer.limit_val_batches = self._dumped_params["limit_eval_batches"]
        elif trainer.state.fn == TrainerFn.TESTING:
            loop = trainer.test_loop
            trainer.limit_test_batches = self._dumped_params["limit_eval_batches"]
        elif trainer.state.fn == TrainerFn.PREDICTING:
            loop = trainer.predict_loop
            trainer.limit_predict_batches = self._dumped_params["limit_eval_batches"]

        loop.load_state_dict(deepcopy(self._dumped_params["loop_state_dict"]))
        if "loop_verbose" in self._dumped_params:
            loop.verbose = self._dumped_params["loop_verbose"]

    def on_fit_start(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        self.scale_batch_size(trainer, pl_module)

    def on_validation_start(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        from pytorch_lightning.trainer.states import TrainerFn

        if trainer.sanity_checking or trainer.state.fn != TrainerFn.VALIDATING:
            return

        self.scale_batch_size(trainer, pl_module)

    def on_test_start(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        self.scale_batch_size(trainer, pl_module)

    def on_predict_start(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        self.scale_batch_size(trainer, pl_module)

    def _adjust_batch_size(
        self,
        trainer: "pl.Trainer",
        factor: float = 1.0,
        value: Optional[int] = None,
        desc: Optional[str] = None,
    ) -> Tuple[int, bool]:
        """Helper function for adjusting the batch size.

        Args:
            trainer: instance of pytorch_lightning.Trainer
            factor: value which the old batch size is multiplied by to get the
                new batch size
            value: if a value is given, will override the batch size with this value.
                Note that the value of `factor` will not have an effect in this case
            desc: either ``"succeeded"`` or ``"failed"``. Used purely for logging

        Returns:
            The new batch size for the next trial and a bool that signals whether the
            new value is different than the previous batch size.
        """
        from pytorch_lightning.trainer.states import TrainerFn

        model = trainer.lightning_module
        batch_size = lightning_getattr(model, self.batch_arg_name)
        new_size = value if value is not None else int(batch_size * factor)
        if desc:
            rank_zero_info(f"Batch size {batch_size} {desc}, trying batch size {new_size}")

        # TODO improve this for eval CombinedLoader and multi dataloaders
        if trainer.state.fn == TrainerFn.FITTING:
            if not self._is_valid_batch_size(new_size, trainer.train_dataloader, trainer):
                new_size = min(new_size, len(trainer.train_dataloader.dataset))
        if trainer.state.fn == TrainerFn.VALIDATING:
            if not self._is_valid_batch_size(new_size, trainer.val_dataloaders[0], trainer):
                new_size = min(new_size, len(trainer.val_dataloaders[0].dataset))
        if trainer.state.fn == TrainerFn.TESTING:
            if not self._is_valid_batch_size(new_size, trainer.test_dataloaders[0], trainer):
                new_size = min(new_size, len(trainer.test_dataloaders[0].dataset))
        if trainer.state.fn == TrainerFn.PREDICTING:
            if not self._is_valid_batch_size(new_size, trainer.predict_dataloaders[0], trainer):
                new_size = min(new_size, len(trainer.predict_dataloaders[0].dataset))

        changed = new_size != batch_size
        lightning_setattr(model, self.batch_arg_name, new_size)
        return new_size, changed

    @staticmethod
    def _is_valid_batch_size(batch_size: int, dataloader: DataLoader, trainer: "pl.Trainer") -> bool:
        from pytorch_lightning.utilities.data import has_len_all_ranks

        module = trainer.lightning_module or trainer.datamodule
        return not has_len_all_ranks(dataloader, trainer.strategy, module) or batch_size <= len(dataloader)
