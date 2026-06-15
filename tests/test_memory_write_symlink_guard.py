import os

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
    assert outside.read_text(encoding="utf-8") == "important"


def test_memory_write_rejects_symlinked_memories_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    outside_dir = tmp_path / "outside-memories"
    outside_dir.mkdir()
    link = home / "memories"
    try:
        os.symlink(str(outside_dir), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap = _patch_memory_routes(monkeypatch, home)
    routes._handle_memory_write(
        _FakeHandler(),
        {"section": "user", "content": "changed"},
    )

    assert "bad" in cap, f"expected 400, got {cap}"
    assert cap["bad"][1] == 400
    assert "Cannot write through a symlinked memories directory" in cap["bad"][0]
    assert not (outside_dir / "USER.md").exists()


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
