from __future__ import annotations

import pathlib
import stat

import pytest

import tests.conftest as _conftest


def _make_skill_tree(root: pathlib.Path) -> None:
    skill_dir = root / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo\n", encoding="utf-8")


def _fail_unexpected_call(message: str):
    def _fail(*_args, **_kwargs):
        raise AssertionError(message)

    return _fail


def _assert_not_writable(path: pathlib.Path) -> None:
    assert path.stat().st_mode & stat.S_IWUSR == 0
    assert path.stat().st_mode & stat.S_IWGRP == 0
    assert path.stat().st_mode & stat.S_IWOTH == 0


def test_seed_test_skills_uses_symlink_path_when_available(monkeypatch, tmp_path):
    real_skills = tmp_path / "real-skills"
    test_skills = tmp_path / "test-skills"
    _make_skill_tree(real_skills)

    seen = {}

    def fake_symlink(self, target):
        seen["args"] = (self, target)
        self.mkdir(parents=True)
        (self / "marker.txt").write_text("symlink path", encoding="utf-8")

    monkeypatch.setattr(pathlib.Path, "symlink_to", fake_symlink)
    monkeypatch.setattr(_conftest.shutil, "copytree", _fail_unexpected_call("copytree should not run"))

    _conftest._seed_test_skills(real_skills, test_skills)

    assert seen["args"] == (test_skills, real_skills)
    assert (test_skills / "marker.txt").read_text(encoding="utf-8") == "symlink path"


def test_seed_test_skills_windows_symlink_error_falls_back_to_copy(monkeypatch, tmp_path):
    real_skills = tmp_path / "real-skills"
    test_skills = tmp_path / "test-skills"
    _make_skill_tree(real_skills)
    source_skill = real_skills / "demo-skill" / "SKILL.md"
    source_mode = source_skill.stat().st_mode

    def fail_symlink(self, target):
        exc = OSError("[WinError 1314] A required privilege is not held by the client")
        exc.winerror = 1314
        raise exc

    monkeypatch.setattr(pathlib.Path, "symlink_to", fail_symlink)
    monkeypatch.setattr(_conftest, "WINDOWS", True)

    _conftest._seed_test_skills(real_skills, test_skills)

    assert test_skills.is_dir()
    assert not test_skills.is_symlink()
    copied_skill = test_skills / "demo-skill" / "SKILL.md"
    assert copied_skill.read_text(encoding="utf-8") == "# Demo\n"
    assert source_skill.stat().st_mode == source_mode
    _assert_not_writable(copied_skill)
    _conftest._rmtree_retry(test_skills)
    assert not test_skills.exists()


def test_seed_test_skills_windows_non_privilege_error_reraises(monkeypatch, tmp_path):
    real_skills = tmp_path / "real-skills"
    test_skills = tmp_path / "test-skills"
    _make_skill_tree(real_skills)

    def fail_symlink(self, target):
        exc = OSError("unexpected symlink failure")
        exc.winerror = 5
        raise exc

    monkeypatch.setattr(pathlib.Path, "symlink_to", fail_symlink)
    monkeypatch.setattr(_conftest, "WINDOWS", True)

    with pytest.raises(OSError, match="unexpected symlink failure"):
        _conftest._seed_test_skills(real_skills, test_skills)


def test_seed_test_skills_reraises_symlink_error_off_windows(monkeypatch, tmp_path):
    real_skills = tmp_path / "real-skills"
    test_skills = tmp_path / "test-skills"
    _make_skill_tree(real_skills)

    def fail_symlink(self, target):
        raise OSError("symlink blocked")

    monkeypatch.setattr(pathlib.Path, "symlink_to", fail_symlink)
    monkeypatch.setattr(_conftest, "WINDOWS", False)

    with pytest.raises(OSError, match="symlink blocked"):
        _conftest._seed_test_skills(real_skills, test_skills)


def test_seed_test_skills_existing_target_is_noop(monkeypatch, tmp_path):
    real_skills = tmp_path / "real-skills"
    test_skills = tmp_path / "test-skills"
    _make_skill_tree(real_skills)
    test_skills.mkdir()
    (test_skills / "existing.txt").write_text("keep", encoding="utf-8")

    monkeypatch.setattr(pathlib.Path, "symlink_to", _fail_unexpected_call("symlink_to should not run"))
    monkeypatch.setattr(_conftest.shutil, "copytree", _fail_unexpected_call("copytree should not run"))

    _conftest._seed_test_skills(real_skills, test_skills)

    assert (test_skills / "existing.txt").read_text(encoding="utf-8") == "keep"
