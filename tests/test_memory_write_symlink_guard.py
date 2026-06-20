"""Regression test for the memory-write symlinked-target guard (#4242).

`_handle_memory_write` refuses to write through a symlinked target file so a
symlink planted at MEMORY.md / USER.md / SOUL.md (e.g. via a restored or imported
workspace) cannot redirect a memory write to clobber an arbitrary file. This
mirrors the symlink-rejection hardening shipped for skills/plugins
(#4217/#4234/#4240).

Per maintainer decision, a symlinked *parent memories directory* is deliberately
NOT rejected here (symlinking the whole .hermes/memories dir is a legitimate
setup); only the concrete target file is guarded.
"""

import os
import errno
from pathlib import Path

import pytest

import api.profiles as profiles
import api.routes as routes


class _FakeHandler:
    pass


def _patch_memory_routes(monkeypatch, home):
    cap = {}
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(routes, "j", lambda h, o: (cap.__setitem__("ok", o), True)[1])
    monkeypatch.setattr(
        routes,
        "bad",
        lambda h, m, c=400: (cap.__setitem__("bad", (m, c)), True)[1],
    )
    return cap


def test_memory_write_rejects_symlinked_memory_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    mem_dir = home / "memories"
    mem_dir.mkdir(parents=True)
    outside = tmp_path / "outside-memory.md"
    outside.write_text("important", encoding="utf-8")
    link = mem_dir / "MEMORY.md"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap = _patch_memory_routes(monkeypatch, home)
    routes._handle_memory_write(
        _FakeHandler(),
        {"section": "memory", "content": "changed"},
    )

    assert "bad" in cap, f"expected 400, got {cap}"
    assert cap["bad"][1] == 400
    assert "Cannot write to a symlinked memory file" in cap["bad"][0]
    # The symlink target outside the memories dir must be untouched.
    assert outside.read_text(encoding="utf-8") == "important"


def test_memory_write_read_only_soul_returns_403(tmp_path, monkeypatch):
    """A read-only SOUL.md write must return an actionable 403, not bubble as 500."""
    home = tmp_path / "home"
    home.mkdir()
    target = home / "SOUL.md"
    target.write_text("original", encoding="utf-8")
    original_write_text = Path.write_text

    def fake_write_text(self, *args, **kwargs):
        if self == target:
            raise PermissionError("read-only test file")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fake_write_text)

    cap = _patch_memory_routes(monkeypatch, home)
    routes._handle_memory_write(
        _FakeHandler(),
        {"section": "soul", "content": "# Soul\n"},
    )

    assert "bad" in cap, f"expected 403, got {cap}"
    assert cap["bad"][1] == 403
    assert "SOUL.md" in cap["bad"][0]
    assert "writable" in cap["bad"][0].lower()
    assert "chmod 644" in cap["bad"][0]


def test_memory_write_read_only_filesystem_returns_403(tmp_path, monkeypatch):
    """Docker read-only volume writes can raise EROFS instead of PermissionError."""
    home = tmp_path / "home"
    home.mkdir()
    target = home / "SOUL.md"
    target.write_text("original", encoding="utf-8")
    original_write_text = Path.write_text

    def fake_write_text(self, *args, **kwargs):
        if self == target:
            raise OSError(errno.EROFS, "read-only file system")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fake_write_text)

    cap = _patch_memory_routes(monkeypatch, home)
    routes._handle_memory_write(
        _FakeHandler(),
        {"section": "soul", "content": "# Soul\n"},
    )

    assert "bad" in cap, f"expected 403, got {cap}"
    assert cap["bad"][1] == 403
    assert "SOUL.md" in cap["bad"][0]
    assert "chmod 644" in cap["bad"][0]


def test_memory_write_allows_symlinked_memories_directory(tmp_path, monkeypatch):
    """A symlinked parent memories directory is allowed (deliberate decision) as
    long as the concrete target file itself is not a symlink."""
    home = tmp_path / "home"
    home.mkdir()
    real_dir = tmp_path / "real-memories"
    real_dir.mkdir()
    link = home / "memories"
    try:
        os.symlink(str(real_dir), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap = _patch_memory_routes(monkeypatch, home)
    routes._handle_memory_write(
        _FakeHandler(),
        {"section": "user", "content": "# User\n"},
    )

    assert "ok" in cap, f"expected success, got {cap}"
    assert cap["ok"]["ok"] is True
    assert (real_dir / "USER.md").read_text(encoding="utf-8") == "# User\n"


def test_memory_write_real_file_still_works(tmp_path, monkeypatch):
    home = tmp_path / "home"

    cap = _patch_memory_routes(monkeypatch, home)
    routes._handle_memory_write(
        _FakeHandler(),
        {"section": "memory", "content": "# Memory\n"},
    )

    target = home / "memories" / "MEMORY.md"
    assert "ok" in cap, f"expected success, got {cap}"
    assert cap["ok"]["ok"] is True
    assert cap["ok"]["section"] == "memory"
    assert target.read_text(encoding="utf-8") == "# Memory\n"
