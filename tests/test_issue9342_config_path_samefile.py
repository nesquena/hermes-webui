"""Tests for issue #9342: non-lexical config path comparisons against _cfg_path."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from api import config


def test_same_config_file_handles_none_and_distinct_files(tmp_path: Path) -> None:
    cfg_a = tmp_path / "a.yaml"
    cfg_b = tmp_path / "b.yaml"
    cfg_a.write_text("model: {}\n", encoding="utf-8")
    cfg_b.write_text("model: {}\n", encoding="utf-8")

    assert config._same_config_file(None, cfg_a) is False
    assert config._same_config_file(cfg_a, None) is False
    assert config._same_config_file(None, None) is False
    assert config._same_config_file(cfg_a, cfg_a) is True
    assert config._same_config_file(cfg_a, cfg_b) is False


def test_same_config_file_handles_relative_and_dotdot_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("model: {}\n", encoding="utf-8")

    monkeypatch.chdir(nested)
    rel_path = Path("..") / "config.yaml"
    dotdot_abs = nested / ".." / "config.yaml"

    assert config._same_config_file(cfg_file, rel_path) is True
    assert config._same_config_file(cfg_file, dotdot_abs) is True
    assert config._same_config_file(rel_path, dotdot_abs) is True


def test_same_config_file_handles_nonexistent_paths_via_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    missing_abs = tmp_path / "future-config.yaml"

    monkeypatch.chdir(nested)
    missing_rel = Path("..") / "future-config.yaml"

    assert config._same_config_file(missing_abs, missing_rel) is True


def test_same_config_file_handles_symlinked_path(tmp_path: Path) -> None:
    target = tmp_path / "real-config.yaml"
    target.write_text("model: {}\n", encoding="utf-8")
    link = tmp_path / "link-config.yaml"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"Symlinks not permitted in this environment: {exc}")

    assert config._same_config_file(target, link) is True
    assert config._same_config_file(link, target) is True


def test_relative_hermes_config_path_does_not_trigger_spurious_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "sub"
    nested.mkdir()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("model:\n  default: test-model\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg_file))
    config.reload_config()

    monkeypatch.chdir(nested)
    monkeypatch.setenv("HERMES_CONFIG_PATH", os.path.join("..", "config.yaml"))

    with patch.object(config, "_refresh_config_cache", wraps=config._refresh_config_cache) as spy:
        loaded = config.get_config()
        snap = config.get_config_snapshot()
        config.reload_config_if_stale()

    assert spy.call_count == 0
    assert loaded.get("model", {}).get("default") == "test-model"
    assert snap.get("model", {}).get("default") == "test-model"


def test_symlinked_hermes_config_path_does_not_trigger_spurious_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("model:\n  default: symlink-model\n", encoding="utf-8")
    link = tmp_path / "symlink-config.yaml"
    try:
        link.symlink_to(cfg_file)
    except OSError as exc:
        pytest.skip(f"Symlinks not permitted in this environment: {exc}")

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg_file))
    config.reload_config()

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(link))

    with patch.object(config, "_refresh_config_cache", wraps=config._refresh_config_cache) as spy:
        loaded = config.get_config()
        snap = config.get_config_snapshot()
        config.reload_config_if_stale()

    assert spy.call_count == 0
    assert loaded.get("model", {}).get("default") == "symlink-model"
    assert snap.get("model", {}).get("default") == "symlink-model"
