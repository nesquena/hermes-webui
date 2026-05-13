"""Tests for the worktree remove functionality (Issue #2057 Slice 2)."""

from types import SimpleNamespace
from pathlib import Path

import pytest

import api.models as models
import api.routes as routes
import api.worktrees as worktrees


def _capture_post(monkeypatch, body):
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    # Monkeypatch both helpers.j and routes.j — bad() lives in helpers but calls the module-global j
    import api.helpers as helpers
    def _fake_j(handler, payload, status=200, extra_headers=None):
        captured.update(payload=payload, status=status)
        return True
    monkeypatch.setattr(routes, "j", _fake_j)
    monkeypatch.setattr(helpers, "j", _fake_j)
    return captured


def _isolate_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return session_dir


def _make_minimal_git_repo(tmp_path):
    import subprocess
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(main)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "test@test.test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "Test"], check=True, capture_output=True)
    (main / "file.txt").write_text("content")
    subprocess.run(["git", "-C", str(main), "add", "file.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), "commit", "-m", "init"], check=True, capture_output=True)
    return main


# ── Function-level tests ─────────────────────────────────────────────────────


def test_remove_clean_worktree_succeeds(tmp_path):
    import subprocess
    from api.models import Session

    main = _make_minimal_git_repo(tmp_path)
    wt_path = tmp_path / "wt_clean"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", str(wt_path), "-b", "hermes/testclean"],
        check=True, capture_output=True,
    )
    assert wt_path.exists()

    s = Session(
        session_id="testclean",
        title="Clean",
        workspace=str(wt_path),
        worktree_path=str(wt_path),
        worktree_branch="hermes/testclean",
        worktree_repo_root=str(main),
    )

    result = worktrees.remove_worktree_for_session(s, force=False)
    assert result["ok"] is True
    assert result["removed_path"] == str(wt_path.resolve())
    assert not wt_path.exists()


def test_remove_clean_worktree_does_not_force(tmp_path, monkeypatch):
    from api.models import Session

    worktree_path = tmp_path / "wt_clean"
    worktree_path.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    s = Session(
        session_id="testcleanforce",
        title="Clean",
        workspace=str(worktree_path),
        worktree_path=str(worktree_path),
        worktree_branch="hermes/testcleanforce",
        worktree_repo_root=str(repo_root),
    )
    monkeypatch.setattr(worktrees, "worktree_status_for_session", lambda session: {
        "exists": True,
        "dirty": False,
        "untracked_count": 0,
        "ahead_behind": {"ahead": 0, "behind": 0, "available": False, "upstream": None},
        "locked_by_stream": False,
        "locked_by_terminal": False,
    })
    calls = []

    def fake_run_git(args, cwd, timeout=2):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worktrees, "_run_git", fake_run_git)

    result = worktrees.remove_worktree_for_session(s, force=False)
    assert result["ok"] is True
    assert calls[0] == ["worktree", "remove", str(worktree_path.resolve())]


def test_remove_dirty_worktree_without_force_is_rejected(tmp_path, monkeypatch):
    from api.models import Session

    worktree_path = tmp_path / "wt_dirty"
    worktree_path.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    s = Session(
        session_id="testdirty",
        title="Dirty",
        workspace=str(worktree_path),
        worktree_path=str(worktree_path),
        worktree_branch="hermes/testdirty",
        worktree_repo_root=str(repo_root),
    )
    monkeypatch.setattr(worktrees, "worktree_status_for_session", lambda session: {
        "exists": True,
        "dirty": True,
        "untracked_count": 0,
        "ahead_behind": {"ahead": 0, "behind": 0, "available": False, "upstream": None},
        "locked_by_stream": False,
        "locked_by_terminal": False,
    })
    monkeypatch.setattr(worktrees, "_run_git", lambda *args, **kwargs: pytest.fail("git remove should not run"))

    with pytest.raises(ValueError, match="uncommitted changes"):
        worktrees.remove_worktree_for_session(s, force=False)


def test_remove_untracked_worktree_without_force_is_rejected(tmp_path, monkeypatch):
    from api.models import Session

    worktree_path = tmp_path / "wt_untracked"
    worktree_path.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    s = Session(
        session_id="testuntracked",
        title="Untracked",
        workspace=str(worktree_path),
        worktree_path=str(worktree_path),
        worktree_branch="hermes/testuntracked",
        worktree_repo_root=str(repo_root),
    )
    monkeypatch.setattr(worktrees, "worktree_status_for_session", lambda session: {
        "exists": True,
        "dirty": False,
        "untracked_count": 2,
        "ahead_behind": {"ahead": 0, "behind": 0, "available": False, "upstream": None},
        "locked_by_stream": False,
        "locked_by_terminal": False,
    })
    monkeypatch.setattr(worktrees, "_run_git", lambda *args, **kwargs: pytest.fail("git remove should not run"))

    with pytest.raises(ValueError, match="untracked"):
        worktrees.remove_worktree_for_session(s, force=False)


def test_remove_ahead_worktree_without_force_is_rejected(tmp_path, monkeypatch):
    from api.models import Session

    worktree_path = tmp_path / "wt_ahead"
    worktree_path.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    s = Session(
        session_id="testahead",
        title="Ahead",
        workspace=str(worktree_path),
        worktree_path=str(worktree_path),
        worktree_branch="hermes/testahead",
        worktree_repo_root=str(repo_root),
    )
    monkeypatch.setattr(worktrees, "worktree_status_for_session", lambda session: {
        "exists": True,
        "dirty": False,
        "untracked_count": 0,
        "ahead_behind": {"ahead": 1, "behind": 0, "available": True, "upstream": "origin/main"},
        "locked_by_stream": False,
        "locked_by_terminal": False,
    })
    monkeypatch.setattr(worktrees, "_run_git", lambda *args, **kwargs: pytest.fail("git remove should not run"))

    with pytest.raises(ValueError, match="unpushed"):
        worktrees.remove_worktree_for_session(s, force=False)


def test_remove_force_warns_and_uses_git_force(tmp_path, monkeypatch):
    from api.models import Session

    worktree_path = tmp_path / "wt_force"
    worktree_path.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    s = Session(
        session_id="testforce",
        title="Force",
        workspace=str(worktree_path),
        worktree_path=str(worktree_path),
        worktree_branch="hermes/testforce",
        worktree_repo_root=str(repo_root),
    )
    monkeypatch.setattr(worktrees, "worktree_status_for_session", lambda session: {
        "exists": True,
        "dirty": True,
        "untracked_count": 3,
        "ahead_behind": {"ahead": 2, "behind": 0, "available": True, "upstream": "origin/main"},
        "locked_by_stream": False,
        "locked_by_terminal": False,
    })
    calls = []

    def fake_run_git(args, cwd, timeout=2):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worktrees, "_run_git", fake_run_git)

    result = worktrees.remove_worktree_for_session(s, force=True)
    assert result["ok"] is True
    assert calls[0] == ["worktree", "remove", "--force", str(worktree_path.resolve())]
    assert "untracked file" in " ".join(result["warnings"])
    assert "unpushed commit" in " ".join(result["warnings"])


def test_remove_worktree_not_exists(tmp_path):
    from api.models import Session

    s = Session(
        session_id="testgone",
        title="Gone",
        workspace=str(tmp_path / "gone"),
        worktree_path=str(tmp_path / "gone"),
        worktree_branch="hermes/gone",
        worktree_repo_root=str(tmp_path / "repo"),
    )

    result = worktrees.remove_worktree_for_session(s, force=False)
    assert result["ok"] is True
    assert len(result.get("warnings", [])) >= 1


def test_remove_worktree_no_path_raises(tmp_path):
    from api.models import Session

    s = Session(
        session_id="testnowt",
        title="No worktree",
        workspace=str(tmp_path),
    )

    try:
        worktrees.remove_worktree_for_session(s, force=False)
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "not worktree-backed" in str(e)


# ── Route-level tests ────────────────────────────────────────────────────────


def test_remove_worktree_route_succeeds(tmp_path, monkeypatch):
    import subprocess
    from api.models import Session

    main = _make_minimal_git_repo(tmp_path)
    wt_path = tmp_path / "wt_route"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", str(wt_path), "-b", "hermes/testroute"],
        check=True, capture_output=True,
    )

    _isolate_session_store(tmp_path, monkeypatch)

    s = Session(
        session_id="testroute1",
        title="Route",
        workspace=str(wt_path),
        worktree_path=str(wt_path),
        worktree_branch="hermes/testroute",
        worktree_repo_root=str(main),
    )
    s.save()

    body = {"session_id": "testroute1"}
    captured = _capture_post(monkeypatch, body)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/worktree/remove")) is True
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["removed_path"] == str(wt_path.resolve())
    assert not wt_path.exists()


def test_remove_missing_session_returns_404(tmp_path, monkeypatch):
    from api.models import Session

    _isolate_session_store(tmp_path, monkeypatch)

    s = Session(
        session_id="someother",
        title="Other",
        workspace=str(tmp_path),
    )
    s.save()

    body = {"session_id": "nonexistent"}
    captured = _capture_post(monkeypatch, body)

    routes.handle_post(object(), SimpleNamespace(path="/api/session/worktree/remove"))
    assert captured["status"] == 404
    assert "not found" in captured["payload"].get("error", "").lower()


def test_post_router_does_not_expose_read_only_worktree_or_compress_status():
    src = Path("api/routes.py").read_text(encoding="utf-8")
    post_body = src[src.index("def handle_post"):src.index('if parsed.path == "/api/session/worktree/remove"')]
    assert '"/api/session/worktree/status"' not in post_body
    assert '"/api/session/compress/status"' not in post_body
