"""Regression tests — atomic JSON writers fsync and use collision-safe temps.

Three writers wrote to a temp file and os.replace'd it into place but never
fsynced, so a power loss between the write and the physical flush could leave a
zero-length/garbage file where durable JSON should be:
  - session_discoverability._atomic_write_json (also used a pid-only temp name,
    which collides between two request threads of the same ThreadingHTTPServer
    process writing the same _index.json),
  - passkeys._atomic_write_json (credentials),
  - oauth._write_auth_json (access/refresh tokens).

Each now fsyncs before the rename; session_discoverability additionally puts the
thread id in the temp name and cleans it up on failure. These pin the fsync, the
per-thread temp name, and that content/permissions are unchanged.
"""
import json
import os
from pathlib import Path

import pytest


def _spy_fsync(monkeypatch, module):
    calls = []
    real = os.fsync
    monkeypatch.setattr(module.os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    return calls


def test_session_discoverability_writer_fsyncs_and_roundtrips(tmp_path, monkeypatch):
    import api.session_discoverability as sd

    calls = _spy_fsync(monkeypatch, sd)
    path = tmp_path / "_index.json"
    payload = [{"session_id": "s1", "title": "Grüße 🌍"}]

    sd._atomic_write_json(path, payload)

    assert calls, "atomic write did not fsync before rename"
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert [p.name for p in tmp_path.iterdir()] == ["_index.json"]  # no temp debris


def test_session_discoverability_temp_name_is_thread_unique(monkeypatch):
    """Two threads (same pid) must not collide on one temp path."""
    import api.session_discoverability as sd

    names = set()
    real_replace = os.replace

    def capture(src, dst):
        names.add(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(sd.os, "replace", capture)

    import tempfile as _tf
    d = Path(_tf.mkdtemp())
    idents = iter([1111, 2222])
    monkeypatch.setattr(sd.threading, "get_ident", lambda: next(idents))
    sd._atomic_write_json(d / "_index.json", [{"a": 1}])
    sd._atomic_write_json(d / "_index.json", [{"a": 2}])

    assert len(names) == 2, f"temp names collided across threads: {names}"


def test_session_discoverability_failed_write_leaves_no_debris(tmp_path, monkeypatch):
    import api.session_discoverability as sd

    original = tmp_path / "_index.json"
    original.write_text('["keep"]', encoding="utf-8")

    def boom(src, dst):
        raise RuntimeError("simulated crash before rename")

    monkeypatch.setattr(sd.os, "replace", boom)
    with pytest.raises(RuntimeError):
        sd._atomic_write_json(original, [{"new": True}])

    assert original.read_text(encoding="utf-8") == '["keep"]'  # original intact
    assert [p.name for p in tmp_path.iterdir()] == ["_index.json"]  # temp cleaned up


def test_passkeys_writer_fsyncs_and_keeps_0600(tmp_path, monkeypatch):
    import api.passkeys as passkeys

    calls = _spy_fsync(monkeypatch, passkeys)
    path = tmp_path / "passkeys.json"
    passkeys._atomic_write_json(path, [{"id": "cred-1"}])

    assert calls, "passkeys write did not fsync"
    assert json.loads(path.read_text(encoding="utf-8")) == [{"id": "cred-1"}]
    assert (os.stat(path).st_mode & 0o777) == 0o600  # owner-only preserved


def test_oauth_writer_fsyncs_and_keeps_0600(tmp_path, monkeypatch):
    import api.oauth as oauth

    calls = _spy_fsync(monkeypatch, oauth)
    path = tmp_path / "auth.json"
    oauth._write_auth_json({"access_token": "x"}, auth_path=path)

    assert calls, "oauth auth.json write did not fsync"
    assert json.loads(path.read_text(encoding="utf-8")) == {"access_token": "x"}
    assert (os.stat(path).st_mode & 0o777) == 0o600  # owner-only preserved
