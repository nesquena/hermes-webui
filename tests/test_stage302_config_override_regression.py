"""Regression tests for stage-302 in-release fix — config.cfg test override.

PR #1728 introduced path/mtime-aware reload in `get_config()`. The
new `cache_stale = current_mtime != _cfg_mtime or _cfg_path != config_path`
check correctly bypasses reload when in-memory overrides exist, but the
existing `_cfg_has_in_memory_overrides()` helper only inspected
`_cfg_cache`, missing the common test idiom:

    monkeypatch.setattr(config, "cfg", {...test override...})

Because `cfg = _cfg_cache` is an alias bound at import time, the rebinding
only changes the module attribute — `_cfg_cache` itself stays untouched.
The fingerprint check returned False, the reload fired, and tests that
assert against a forced provider/default lost their override silently.
v0.51.7 stage-302 caught this on `test_issue1426_openrouter_*` and
`test_issue1680_codex_*` failing in the full suite while passing
standalone.

Fix:
  1. `_cfg_has_in_memory_overrides()` now ALSO returns True when
     `cfg is not _cfg_cache` (module attr rebound).
  2. `get_config()` now returns `cfg` (the override) rather than
     `_cfg_cache` when they're not the same object.

These tests pin both prongs.
"""
from __future__ import annotations

import api.config as config


def test_get_config_respects_module_attr_rebind(monkeypatch, tmp_path):
    """monkeypatch.setattr(config, 'cfg', X) must survive get_config()."""
    config.reload_config()
    test_override = {
        "model": {"provider": "openrouter", "default": "test/model-x"},
        "providers": {"openrouter": {"api_key": "***"}},
    }
    monkeypatch.setattr(config, "cfg", test_override, raising=False)

    result = config.get_config()
    # The override must survive — get_config() must not silently fall
    # through to _cfg_cache.
    assert result is test_override, (
        f"get_config() returned _cfg_cache instead of the override; "
        f"override has provider={test_override['model']['provider']}, "
        f"result has provider={result.get('model', {}).get('provider')}"
    )
    assert result["model"]["provider"] == "openrouter"
    assert result["model"]["default"] == "test/model-x"


def test_cfg_has_in_memory_overrides_detects_attr_rebind(monkeypatch):
    """The helper must report True when cfg is rebound away from _cfg_cache."""
    config.reload_config()
    # No override yet — fingerprint matches, attr is the alias.
    assert config._cfg_has_in_memory_overrides() is False

    # Rebind cfg.
    monkeypatch.setattr(config, "cfg", {"model": {"provider": "openrouter"}}, raising=False)
    assert config._cfg_has_in_memory_overrides() is True


def test_cfg_has_in_memory_overrides_detects_in_place_mutation(monkeypatch):
    """The helper must still detect the original in-place mutation case."""
    config.reload_config()
    assert config._cfg_has_in_memory_overrides() is False

    # Mutate _cfg_cache directly (NOT a rebind).
    config._cfg_cache["__test_key"] = "test_value"
    try:
        assert config._cfg_has_in_memory_overrides() is True
    finally:
        config._cfg_cache.pop("__test_key", None)


def test_get_config_does_not_reload_when_only_in_memory_override(monkeypatch, tmp_path):
    """A test that sets cfg + leaves disk untouched must not trigger reload."""
    config.reload_config()
    # Fake a config path that will have a different mtime than what's cached
    fake_path = tmp_path / "missing.yaml"
    monkeypatch.setattr(config, "_get_config_path", lambda: fake_path)

    # Override cfg via attr rebind.
    test_override = {
        "model": {"provider": "openai", "default": "gpt-test"},
        "providers": {},
    }
    monkeypatch.setattr(config, "cfg", test_override, raising=False)

    # The path-aware reload would normally trigger reload (path changed),
    # but the override-detection should suppress it.
    result = config.get_config()
    assert result is test_override
    assert result["model"]["provider"] == "openai"
