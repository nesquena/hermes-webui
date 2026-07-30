import os

import pytest

import api.routes as routes


class _FakeHandler:
    pass


def _patch_skill_routes(monkeypatch, skills_dir):
    cap = {}
    monkeypatch.setattr(routes, "_active_skills_dir", lambda: skills_dir)
    monkeypatch.setattr(routes, "j", lambda h, o: (cap.__setitem__("ok", o), True)[1])
    monkeypatch.setattr(
        routes,
        "bad",
        lambda h, m, c=400: (cap.__setitem__("bad", (m, c)), True)[1],
    )
    return cap


def test_skill_save_rejects_symlinked_skill_file(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "demo"
    skill_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("important", encoding="utf-8")
    link = skill_dir / "SKILL.md"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap = _patch_skill_routes(monkeypatch, skills_dir)
    routes._handle_skill_save(
        _FakeHandler(),
        {"name": "demo", "content": "changed"},
    )

    assert "bad" in cap, f"expected 400, got {cap}"
    assert cap["bad"][1] == 400
    assert "Cannot save to a symlinked skill file" in cap["bad"][0]
    assert outside.read_text(encoding="utf-8") == "important"


def test_skill_save_real_file_still_works(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"

    cap = _patch_skill_routes(monkeypatch, skills_dir)
    routes._handle_skill_save(
        _FakeHandler(),
        {"name": "Demo Skill", "content": "# Demo\n"},
    )

    skill_file = skills_dir / "demo-skill" / "SKILL.md"
    assert "ok" in cap, f"expected success, got {cap}"
    assert cap["ok"]["ok"] is True
    assert skill_file.read_text(encoding="utf-8") == "# Demo\n"
