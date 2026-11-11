"""The per-test config disk-load state restore (conftest._restore_config_load_state).

A test that points ``config._get_config_path`` at a temp file and calls
``get_config()`` leaves ``_cfg_path`` (and the cache loaded from that file)
behind after monkeypatch undoes the patch. The guard snapshots that state before
a test and puts it back after, so the next test's ``get_config()`` does not see
a path change and reload over its own ``cfg`` override.
"""
import pytest

from api import config
from tests.conftest import _config_load_state_guard


def _load_state():
    return (
        config._cfg_path,
        config._cfg_mtime,
        config._cfg_fingerprint,
        dict(config._cfg_cache),
    )


def test_guard_puts_back_the_state_a_temp_config_path_reload_replaced(monkeypatch, tmp_path):
    guard = _config_load_state_guard()
    next(guard)  # the fixture's setup: snapshot
    before = _load_state()
    missing = tmp_path / "missing-config.yaml"

    monkeypatch.setattr(config, "_get_config_path", lambda: missing)
    config.get_config()
    assert config._cfg_path == missing  # the reload recorded the temp path
    monkeypatch.undo()
    assert config._cfg_path == missing  # undoing the patch does not put it back

    with pytest.raises(StopIteration):
        next(guard)  # the fixture's teardown
    assert _load_state() == before
    assert config.cfg is config._cfg_cache or config.cfg == before[3]


def test_guard_changes_nothing_for_a_test_that_left_the_state_alone():
    before = _load_state()
    guard = _config_load_state_guard()
    next(guard)
    with pytest.raises(StopIteration):
        next(guard)
    assert _load_state() == before
