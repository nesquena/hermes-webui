"""Backend half of the #4650 fix: _load_yaml_config_file memoizes the parsed
config keyed on (path, mtime_ns, size).

GET /api/reasoning -> get_reasoning_status() calls _load_yaml_config_file on
every request, and yaml.safe_load on an ~800-line config costs ~125ms. Under
the (pre-fix) request storm that became a YAML-reparse storm. This test pins
the cache contract:

  * a second read of an unchanged file does NOT re-run yaml.safe_load,
  * an on-disk edit (new mtime/size) DOES trigger a fresh parse,
  * env-var expansion still runs per call (so ${VAR} stays live and callers
    that mutate the returned dict never corrupt the cached copy).
"""
import importlib
import time

import pytest

config = importlib.import_module("api.config")


@pytest.fixture
def clean_cache():
    config._yaml_file_cache.clear()
    yield
    config._yaml_file_cache.clear()


def _write(p, text):
    p.write_text(text, encoding="utf-8")


def test_second_read_uses_cache_no_reparse(tmp_path, monkeypatch, clean_cache):
    cfg = tmp_path / "config.yaml"
    _write(cfg, "agent:\n  reasoning_effort: high\n")

    calls = {"n": 0}
    import yaml as _yaml
    real_load = _yaml.safe_load

    def counting_load(text):
        calls["n"] += 1
        return real_load(text)

    monkeypatch.setattr(_yaml, "safe_load", counting_load)

    first = config._load_yaml_config_file(cfg)
    second = config._load_yaml_config_file(cfg)

    assert first == {"agent": {"reasoning_effort": "high"}}
    assert second == first
    assert calls["n"] == 1, "second read of an unchanged file must hit the cache, not reparse"


def test_mtime_change_invalidates_cache(tmp_path, monkeypatch, clean_cache):
    cfg = tmp_path / "config.yaml"
    _write(cfg, "agent:\n  reasoning_effort: high\n")

    calls = {"n": 0}
    import yaml as _yaml
    real_load = _yaml.safe_load

    def counting_load(text):
        calls["n"] += 1
        return real_load(text)

    monkeypatch.setattr(_yaml, "safe_load", counting_load)

    assert config._load_yaml_config_file(cfg)["agent"]["reasoning_effort"] == "high"
    # Rewrite with different content + bump mtime so (mtime_ns, size) changes.
    time.sleep(0.01)
    _write(cfg, "agent:\n  reasoning_effort: low\n")
    import os
    st = cfg.stat()
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    reloaded = config._load_yaml_config_file(cfg)
    assert reloaded["agent"]["reasoning_effort"] == "low", "an on-disk edit must be picked up"
    assert calls["n"] == 2, "changed file must trigger exactly one fresh parse"


def test_env_expansion_runs_per_call_even_from_cache(tmp_path, monkeypatch, clean_cache):
    cfg = tmp_path / "config.yaml"
    _write(cfg, "agent:\n  api_key: ${MY_TEST_KEY}\n")

    monkeypatch.setenv("MY_TEST_KEY", "first")
    first = config._load_yaml_config_file(cfg)
    assert first["agent"]["api_key"] == "first"

    # Cache hit (file unchanged) but env changed -> expansion must reflect it.
    monkeypatch.setenv("MY_TEST_KEY", "second")
    second = config._load_yaml_config_file(cfg)
    assert second["agent"]["api_key"] == "second", "env expansion must run per call, not be frozen in the cache"


def test_caller_mutation_does_not_corrupt_cache(tmp_path, clean_cache):
    cfg = tmp_path / "config.yaml"
    _write(cfg, "display:\n  show_reasoning: true\n")

    first = config._load_yaml_config_file(cfg)
    # Mirror the read-modify-save callers (set_reasoning_display etc.).
    first["display"]["show_reasoning"] = False
    first["injected"] = "mutation"

    second = config._load_yaml_config_file(cfg)
    assert second["display"]["show_reasoning"] is True, "caller mutation must not leak into the cache"
    assert "injected" not in second


def test_missing_file_returns_empty(tmp_path, clean_cache):
    assert config._load_yaml_config_file(tmp_path / "nope.yaml") == {}


def test_save_evicts_cache_so_next_read_is_fresh(tmp_path, clean_cache):
    """#4650 review: a WebUI save that preserves (mtime_ns, size) could otherwise
    serve a stale cached parse. _save_yaml_config_file must evict the cache entry
    for the path it writes, so the next read reflects the saved content."""
    cfg = tmp_path / "config.yaml"
    _write(cfg, "agent:\n  reasoning_effort: high\n")

    first = config._load_yaml_config_file(cfg)
    assert first["agent"]["reasoning_effort"] == "high"
    assert str(cfg) in config._yaml_file_cache, "first read should populate the cache"

    # Save new content through the WebUI writer.
    config._save_yaml_config_file(cfg, {"agent": {"reasoning_effort": "low"}})
    assert str(cfg) not in config._yaml_file_cache, (
        "_save_yaml_config_file must evict the memoized parse for the written path"
    )

    # Next read must reflect the saved value even if mtime/size happened to collide.
    fresh = config._load_yaml_config_file(cfg)
    assert fresh["agent"]["reasoning_effort"] == "low", (
        "read after save must return the freshly-written config, not a stale cache hit"
    )
