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
from pathlib import Path
from re import escape
from unittest.mock import call, Mock

import pytest

from pytorch_lightning import Callback, Trainer
from pytorch_lightning.callbacks import ModelCheckpoint
from tests.helpers import BoringModel
from tests.helpers.utils import no_warning_call


def test_callbacks_configured_in_model(tmpdir):
    """Test the callback system with callbacks added through the model hook."""

    model_callback_mock = Mock(spec=Callback, model=Callback())
    trainer_callback_mock = Mock(spec=Callback, model=Callback())

    class TestModel(BoringModel):
        def configure_callbacks(self):
            return [model_callback_mock]

    model = TestModel()
    trainer_options = dict(
        default_root_dir=tmpdir, enable_checkpointing=False, fast_dev_run=True, enable_progress_bar=False
    )

    def assert_expected_calls(_trainer, model_callback, trainer_callback):
        # some methods in callbacks configured through model won't get called
        uncalled_methods = [call.on_init_start(_trainer), call.on_init_end(_trainer)]
        for uncalled in uncalled_methods:
            assert uncalled not in model_callback.method_calls

        # assert that the rest of calls are the same as for trainer callbacks
        expected_calls = [m for m in trainer_callback.method_calls if m not in uncalled_methods]
        assert expected_calls
        assert model_callback.method_calls == expected_calls

    # .fit()
    trainer_options.update(callbacks=[trainer_callback_mock])
    trainer = Trainer(**trainer_options)

    assert trainer_callback_mock in trainer.callbacks
    assert model_callback_mock not in trainer.callbacks
    trainer.fit(model)

    assert model_callback_mock in trainer.callbacks
    assert trainer.callbacks[-1] == model_callback_mock
    assert_expected_calls(trainer, model_callback_mock, trainer_callback_mock)

    # .test()
    for fn in ("test", "validate"):
        model_callback_mock.reset_mock()
        trainer_callback_mock.reset_mock()

        trainer_options.update(callbacks=[trainer_callback_mock])
        trainer = Trainer(**trainer_options)

        trainer_fn = getattr(trainer, fn)
        trainer_fn(model)

        assert model_callback_mock in trainer.callbacks
        assert trainer.callbacks[-1] == model_callback_mock
        assert_expected_calls(trainer, model_callback_mock, trainer_callback_mock)


def test_configure_callbacks_hook_multiple_calls(tmpdir):
    """Test that subsequent calls to `configure_callbacks` do not change the callbacks list."""
    model_callback_mock = Mock(spec=Callback, model=Callback())

    class TestModel(BoringModel):
        def configure_callbacks(self):
            return model_callback_mock

    model = TestModel()
    trainer = Trainer(default_root_dir=tmpdir, fast_dev_run=True, enable_checkpointing=False)

    callbacks_before_fit = trainer.callbacks.copy()
    assert callbacks_before_fit

    trainer.fit(model)
    callbacks_after_fit = trainer.callbacks.copy()
    assert callbacks_after_fit == callbacks_before_fit + [model_callback_mock]

    for fn in ("test", "validate"):
        trainer_fn = getattr(trainer, fn)
        trainer_fn(model)

        callbacks_after = trainer.callbacks.copy()
        assert callbacks_after == callbacks_after_fit

        trainer_fn(model)
        callbacks_after = trainer.callbacks.copy()
        assert callbacks_after == callbacks_after_fit


class OldStatefulCallback(Callback):
    def __init__(self, state):
        self.state = state

    @property
    def state_key(self):
        return type(self)

    def state_dict(self):
        return {"state": self.state}

    def load_state_dict(self, state_dict) -> None:
        self.state = state_dict["state"]


def test_resume_callback_state_saved_by_type_stateful(tmpdir):
    """Test that a legacy checkpoint that didn't use a state key before can still be loaded, using
    state_dict/load_state_dict."""
    model = BoringModel()
    callback = OldStatefulCallback(state=111)
    trainer = Trainer(default_root_dir=tmpdir, max_steps=1, callbacks=[callback])
    trainer.fit(model)
    ckpt_path = Path(trainer.checkpoint_callback.best_model_path)
    assert ckpt_path.exists()

    callback = OldStatefulCallback(state=222)
    trainer = Trainer(default_root_dir=tmpdir, max_steps=2, callbacks=[callback])
    trainer.fit(model, ckpt_path=ckpt_path)
    assert callback.state == 111


class OldStatefulCallbackHooks(Callback):
    def __init__(self, state):
        self.state = state

    @property
    def state_key(self):
        return type(self)

    def on_save_checkpoint(self, trainer, pl_module, checkpoint):
        return {"state": self.state}

    def on_load_checkpoint(self, trainer, pl_module, callback_state):
        self.state = callback_state["state"]


def test_resume_callback_state_saved_by_type_hooks(tmpdir):
    """Test that a legacy checkpoint that didn't use a state key before can still be loaded, using deprecated
    on_save/load_checkpoint signatures."""
    # TODO: remove old on_save/load_checkpoint signature support in v1.8
    # in favor of Stateful and new on_save/load_checkpoint signatures
    # on_save_checkpoint() -> dict, on_load_checkpoint(callback_state)
    # will become
    # on_save_checkpoint() -> None and on_load_checkpoint(checkpoint)
    model = BoringModel()
    callback = OldStatefulCallbackHooks(state=111)
    trainer = Trainer(default_root_dir=tmpdir, max_steps=1, callbacks=[callback])
    # TODO: catch deprecated call after deprecations introduced (see reference PR #11887)
    trainer.fit(model)
    ckpt_path = Path(trainer.checkpoint_callback.best_model_path)
    assert ckpt_path.exists()

    callback = OldStatefulCallbackHooks(state=222)
    trainer = Trainer(default_root_dir=tmpdir, max_steps=2, callbacks=[callback])
    # TODO: catch deprecated call after deprecations introduced (see reference PR #11887)
    trainer.fit(model, ckpt_path=ckpt_path)
    assert callback.state == 111


def test_resume_incomplete_callbacks_list_warning(tmpdir):
    model = BoringModel()
    callback0 = ModelCheckpoint(monitor="epoch")
    callback1 = ModelCheckpoint(monitor="global_step")
    trainer = Trainer(
        default_root_dir=tmpdir,
        max_steps=1,
        callbacks=[callback0, callback1],
    )
    trainer.fit(model)
    ckpt_path = trainer.checkpoint_callback.best_model_path

    trainer = Trainer(
        default_root_dir=tmpdir,
        max_steps=1,
        callbacks=[callback1],  # one callback is missing!
    )
    with pytest.warns(UserWarning, match=escape(f"Please add the following callbacks: [{repr(callback0.state_key)}]")):
        trainer.fit(model, ckpt_path=ckpt_path)

    trainer = Trainer(
        default_root_dir=tmpdir,
        max_steps=1,
        callbacks=[callback1, callback0],  # all callbacks here, order switched
    )
    with no_warning_call(UserWarning, match="Please add the following callbacks:"):
        trainer.fit(model, ckpt_path=ckpt_path)


class AllStatefulCallback(Callback):
    def __init__(self, state):
        self.state = state

    @property
    def state_key(self):
        return type(self)

    def state_dict(self):
        return {"new_state": self.state}

    def load_state_dict(self, state_dict):
        assert state_dict == {"old_state_precedence": 10}
        self.state = state_dict["old_state_precedence"]

    def on_save_checkpoint(self, trainer, pl_module, checkpoint):
        return {"old_state_precedence": 10}

    def on_load_checkpoint(self, trainer, pl_module, callback_state):
        assert callback_state == {"old_state_precedence": 10}
        self.old_state_precedence = callback_state["old_state_precedence"]


def test_resume_callback_state_all(tmpdir):
    """Test on_save/load_checkpoint state precedence over state_dict/load_state_dict until v1.8 removal."""
    # TODO: remove old on_save/load_checkpoint signature support in v1.8
    # in favor of Stateful and new on_save/load_checkpoint signatures
    # on_save_checkpoint() -> dict, on_load_checkpoint(callback_state)
    # will become
    # on_save_checkpoint() -> None and on_load_checkpoint(checkpoint)
    model = BoringModel()
    callback = AllStatefulCallback(state=111)
    trainer = Trainer(default_root_dir=tmpdir, max_steps=1, callbacks=[callback])
    # TODO: catch deprecated call after deprecations introduced (see reference PR #11887)
    trainer.fit(model)
    ckpt_path = Path(trainer.checkpoint_callback.best_model_path)
    assert ckpt_path.exists()

    callback = AllStatefulCallback(state=222)
    trainer = Trainer(default_root_dir=tmpdir, max_steps=2, callbacks=[callback])
    # TODO: catch deprecated call after deprecations introduced (see reference PR #11887)
    trainer.fit(model, ckpt_path=ckpt_path)
    assert callback.state == 10
    assert callback.old_state_precedence == 10
