"""Synthetic checks for private auth-store temporary files."""

import json
import os
import stat

from types import SimpleNamespace

import pytest

import api.oauth as oauth


@pytest.mark.skipif(os.name == "nt", reason="POSIX creation-mode semantics")
@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077])
def test_auth_store_temporary_is_private_before_fsync(monkeypatch, tmp_path, umask):
    path = tmp_path / "auth.json"
    observed_modes = []
    real_fsync = os.fsync

    def inspect_before_fsync(fd):
        observed_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(oauth.os, "fsync", inspect_before_fsync)
    previous_umask = os.umask(umask)
    try:
        oauth._write_auth_json({"access_token": "synthetic-token"}, path)
    finally:
        os.umask(previous_umask)

    assert observed_modes == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"access_token": "synthetic-token"}
    assert list(tmp_path.iterdir()) == [path]


def test_auth_store_temporary_collision_preserves_unowned_file(monkeypatch, tmp_path):
    path = tmp_path / "auth.json"
    monkeypatch.setattr(oauth.uuid, "uuid4", lambda: SimpleNamespace(hex="collision"))
    temporary = path.with_name(f"auth.json.tmp.{os.getpid()}.collision")
    temporary.write_text("UNOWNED")
    with pytest.raises(FileExistsError):
        oauth._write_auth_json({"access_token": "synthetic-token"}, path)
    assert temporary.read_text() == "UNOWNED"
    assert not path.exists()


@pytest.mark.parametrize("failure", ["serialize", "fsync", "replace"])
def test_auth_store_write_failure_keeps_previous_store_and_cleans_temporary(monkeypatch, tmp_path, failure):
    path = tmp_path / "auth.json"
    previous = '{"previous": true}\n'
    path.write_text(previous)

    def fail(*args, **kwargs):
        raise OSError("synthetic write failure")

    if failure == "serialize":
        monkeypatch.setattr(oauth.json, "dumps", fail)
    elif failure == "fsync":
        monkeypatch.setattr(oauth.os, "fsync", fail)
    else:
        monkeypatch.setattr(type(path), "replace", fail)

    with pytest.raises(OSError, match="synthetic write failure"):
        oauth._write_auth_json({"access_token": "synthetic-token"}, path)

    assert path.read_text() == previous
    assert list(tmp_path.iterdir()) == [path]
