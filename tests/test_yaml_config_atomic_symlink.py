"""Atomic YAML writes preserve symlink binding and durability."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

import api.config as config


def _temp_names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.name.endswith(".tmp"))


def test_symlink_write_preserves_inode_mode_fsyncs_referent_parent_and_evicts_aliases(
    tmp_path, monkeypatch
):
    referent_dir = tmp_path / "referent"
    link_dir = tmp_path / "links"
    referent_dir.mkdir()
    link_dir.mkdir()
    target = referent_dir / "config.yaml"
    target.write_text("tts:\n  provider: edge\n", encoding="utf-8")
    os.chmod(target, 0o640)
    link = link_dir / "config.yaml"
    link.symlink_to(target)
    link_inode = os.lstat(link).st_ino

    config._load_yaml_config_file_raw(link)
    config._load_yaml_config_file_raw(target)
    assert str(link) in config._yaml_file_cache
    assert str(target.resolve()) in config._yaml_file_cache

    fsynced_directories = []
    real_fsync = config.os.fsync

    def recording_fsync(fd):
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            fsynced_directories.append(Path(f"/proc/self/fd/{fd}").resolve())
        return real_fsync(fd)

    monkeypatch.setattr(config.os, "fsync", recording_fsync)

    config._save_yaml_config_file(link, {"tts": {"provider": "openai"}})

    assert link.is_symlink()
    assert os.lstat(link).st_ino == link_inode
    assert link.resolve() == target.resolve()
    assert os.stat(target).st_mode & 0o777 == 0o640
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["tts"]["provider"] == "openai"
    assert target.parent.resolve() in fsynced_directories
    assert str(link) not in config._yaml_file_cache
    assert str(target.resolve()) not in config._yaml_file_cache
    assert _temp_names(target.parent) == []
    assert _temp_names(link.parent) == []


def test_symlink_retarget_before_replace_fails_closed_and_cleans_temp(
    tmp_path, monkeypatch
):
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text("value: first\n", encoding="utf-8")
    second.write_text("value: second\n", encoding="utf-8")
    link = tmp_path / "config.yaml"
    link.symlink_to(first)
    real_replace = config.os.replace

    def retarget_then_replace(source, destination):
        link.unlink()
        link.symlink_to(second)
        return real_replace(source, destination)

    monkeypatch.setattr(config.os, "replace", retarget_then_replace)

    with pytest.raises(RuntimeError, match="binding changed"):
        config._save_yaml_config_file(link, {"value": "new"})

    assert first.read_text(encoding="utf-8") == "value: new\n"
    assert second.read_text(encoding="utf-8") == "value: second\n"
    assert _temp_names(tmp_path) == []


def test_replace_failure_keeps_old_referent_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "config.yaml"
    target.write_text("value: old\n", encoding="utf-8")
    monkeypatch.setattr(
        config.os,
        "replace",
        lambda source, destination: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        config._save_yaml_config_file(target, {"value": "new"})

    assert target.read_text(encoding="utf-8") == "value: old\n"
    assert _temp_names(tmp_path) == []
