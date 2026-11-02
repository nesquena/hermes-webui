"""A tolerant display read must never authorize replacing an unreadable config.

These exercise the real YAML parser, reasoning setter and atomic writer against
synthetic files, not mocked loader/save return values. The malformed fixture has
both a four-space plugin item and a stray two-space item, matching the reported
indentation failure without including anyone's config or credentials.
"""

from concurrent.futures import ThreadPoolExecutor
import os
import stat
import threading

import pytest

from api import config, yaml_compat


MALFORMED = (
    "model:\n  default: example-model\n"
    "plugins:\n  enabled:\n    - moshi-hooks\n  - moshi-hooks\n"
    "agent:\n  max_turns: 42\n"
)


@pytest.fixture(params=["pyyaml", "ruamel"])
def backend(request, monkeypatch):
    """Run the actual adapter's parser/dumper on both supported backends."""
    if request.param == "ruamel":
        pytest.importorskip("ruamel.yaml")
        monkeypatch.setattr(yaml_compat, "_pyyaml", None)
    else:
        monkeypatch.setattr(yaml_compat, "_pyyaml", pytest.importorskip("yaml"))
    return yaml_compat


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "_get_config_path", lambda: path)
    monkeypatch.setattr(config, "_yaml_file_cache", {})
    # Keep real reloads isolated without leaking defaults/cache across tests.
    cache = {}
    monkeypatch.setattr(config, "_cfg_cache", cache)
    monkeypatch.setattr(config, "cfg", cache)
    monkeypatch.setattr(config, "_cfg_path", None)
    monkeypatch.setattr(config, "_cfg_mtime", 0.0)
    monkeypatch.setattr(config, "_cfg_fingerprint", None)
    return path


@pytest.mark.parametrize("effort", ["xhigh", ""])
def test_reasoning_does_not_replace_malformed_config(config_file, backend, effort):
    config_file.write_text(MALFORMED, encoding="utf-8")
    before = config_file.read_bytes()
    # Display/status reads intentionally remain tolerant.
    assert config._load_yaml_config_file(config_file) == {}
    assert config.get_reasoning_status()["reasoning_effort"] == ""
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        config.set_reasoning_effort(effort)
    assert config_file.read_bytes() == before


@pytest.mark.parametrize("text", ["- plugin\n", "[]\n", "42\n", "false\n", "0\n", "settings\n"])
def test_central_writer_rejects_non_mapping_config(config_file, backend, text):
    config_file.write_text(text, encoding="utf-8")
    # A read may even cache a non-mapping as {}; that cannot authorize a save.
    assert config._load_yaml_config_file(config_file) == {}
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        config._save_yaml_config_file(config_file, {"agent": {"reasoning_effort": "xhigh"}})
    assert config_file.read_text(encoding="utf-8") == text


def test_central_writer_guards_other_callers(config_file, backend):
    config_file.write_text(MALFORMED, encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        config.set_reasoning_display(False)
    assert config_file.read_text(encoding="utf-8") == MALFORMED
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        config._save_yaml_config_file(config_file, {"model": {"default": "new"}})
    assert config_file.read_text(encoding="utf-8") == MALFORMED


@pytest.mark.parametrize("text", [None, "", "# Fresh config\n", "{}\n", "null\n"])
def test_missing_empty_or_null_config_can_be_initialized(config_file, backend, text):
    if text is not None:
        config_file.write_text(text, encoding="utf-8")
    result = config.set_reasoning_effort("xhigh")
    assert backend.safe_load(config_file.read_text(encoding="utf-8")) == {
        "agent": {"reasoning_effort": "xhigh"}
    }
    assert result["reasoning_effort"] == "xhigh"


@pytest.mark.parametrize("effort", ["xhigh", ""])
def test_valid_update_preserves_unrelated_settings_and_mode(config_file, backend, effort):
    original = {
        "model": {"default": "example-model", "provider": "example-provider"},
        "plugins": {"enabled": ["moshi-hooks", "other-plugin"]},
        "agent": {"max_turns": 42, "reasoning_effort": "low"},
        "display": {"show_reasoning": False},
        "custom": {"unicode": "café", "items": [1, 2]},
    }
    config_file.write_text(backend.safe_dump(original), encoding="utf-8")
    config_file.chmod(0o640)
    result = config.set_reasoning_effort(effort)
    if effort:
        original["agent"]["reasoning_effort"] = effort
    else:
        original["agent"].pop("reasoning_effort")
    assert backend.safe_load(config_file.read_text(encoding="utf-8")) == original
    assert result["reasoning_effort"] == effort
    if os.name != "nt":
        assert stat.S_IMODE(config_file.stat().st_mode) == 0o640


def test_raw_writer_preserves_environment_references(config_file, backend, monkeypatch):
    monkeypatch.setenv("CONFIG_GUARD_TEST_TOKEN", "synthetic-expanded-value")
    config_file.write_text(
        "model:\n  api_key: ${CONFIG_GUARD_TEST_TOKEN}\n"
        "plugins:\n  enabled: [moshi-hooks]\n",
        encoding="utf-8",
    )
    data = config._load_yaml_config_file_raw(config_file)
    data["max_tokens"] = 123
    config._save_yaml_config_file(config_file, data)
    saved = backend.safe_load(config_file.read_text(encoding="utf-8"))
    assert saved["model"]["api_key"] == "${CONFIG_GUARD_TEST_TOKEN}"
    assert saved["plugins"]["enabled"] == ["moshi-hooks"]
    assert saved["max_tokens"] == 123
    assert "synthetic-expanded-value" not in config_file.read_text(encoding="utf-8")


def test_guard_does_not_trust_cached_success_with_same_stat_key(config_file, backend):
    good = "key: safe\n"
    bad = "key: [bad\n"
    assert len(good) == len(bad)
    config_file.write_text(good, encoding="utf-8")
    before_stat = config_file.stat()
    assert config._load_yaml_config_file_raw(config_file) == {"key": "safe"}
    config_file.write_text(bad, encoding="utf-8")
    os.utime(config_file, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    # Existing read cache legitimately still sees the success. Write guard must
    # read actual disk bytes rather than trusting this display optimization.
    assert config._load_yaml_config_file_raw(config_file) == {"key": "safe"}
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        config._save_yaml_config_file(config_file, {"changed": True})
    assert config_file.read_text(encoding="utf-8") == bad


def test_changed_to_malformed_before_save_is_not_overwritten(config_file, backend, monkeypatch):
    config_file.write_text("marker: keep\n", encoding="utf-8")
    original_save = config._save_yaml_config_file

    def edit_then_save(path, data):
        path.write_text(MALFORMED, encoding="utf-8")
        return original_save(path, data)

    monkeypatch.setattr(config, "_save_yaml_config_file", edit_then_save)
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        config.set_reasoning_effort("xhigh")
    assert config_file.read_text(encoding="utf-8") == MALFORMED


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 0)() == 0,
                    reason="POSIX permissions; root bypasses read permission checks")
def test_unreadable_existing_config_is_not_overwritten(config_file, backend):
    original = b"marker: keep\n"
    config_file.write_bytes(original)
    config_file.chmod(0o200)  # writable but unreadable, unlike a missing config
    try:
        with pytest.raises((OSError, ValueError)):
            config.set_reasoning_effort("xhigh")
    finally:
        config_file.chmod(0o600)
    assert config_file.read_bytes() == original


def test_failed_atomic_replace_preserves_original(config_file, backend, monkeypatch):
    original = b"marker: keep\n"
    config_file.write_bytes(original)

    def fail_replace(src, dst):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        config.set_reasoning_effort("xhigh")
    assert config_file.read_bytes() == original
    assert list(config_file.parent.iterdir()) == [config_file]


def test_threaded_read_modify_writes_keep_both_settings(config_file, backend, monkeypatch):
    config_file.write_text("marker: keep\n", encoding="utf-8")
    entered, release, second_started = (threading.Event() for _ in range(3))
    original_save = config._save_yaml_config_file
    first = True

    def pause_first_save(path, data):
        nonlocal first
        assert config._cfg_lock.locked()
        if first:
            first = False
            entered.set()
            assert release.wait(5)
        return original_save(path, data)

    monkeypatch.setattr(config, "_save_yaml_config_file", pause_first_save)

    def change_display():
        second_started.set()
        return config.set_reasoning_display(False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        effort = pool.submit(config.set_reasoning_effort, "xhigh")
        try:
            assert entered.wait(5)
            display = pool.submit(change_display)
            assert second_started.wait(5)
        finally:
            release.set()
        effort.result(timeout=10)
        display.result(timeout=10)
    assert backend.safe_load(config_file.read_text(encoding="utf-8")) == {
        "marker": "keep",
        "agent": {"reasoning_effort": "xhigh"},
        "display": {"show_reasoning": False},
    }
