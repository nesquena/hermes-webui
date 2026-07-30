import subprocess
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import api.models as models
from api.models import SESSIONS, Session


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    SESSIONS.clear()
    yield session_dir
    SESSIONS.clear()


@pytest.fixture
def git_worktree(tmp_path):
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    worktree = tmp_path / "hermes-status"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "branch", "-M", "main")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(remote.parent, "init", "--bare", remote.name)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "worktree", "add", "-b", "hermes/status", str(worktree), "main")
    _git(worktree, "push", "-u", "origin", "hermes/status")
    return repo, worktree


def _session_for_worktree(repo, worktree, **kwargs):
    return Session(
        session_id=kwargs.pop("session_id", "wtstatus001"),
        workspace=str(worktree),
        worktree_path=str(worktree),
        worktree_branch="hermes/status",
        worktree_repo_root=str(repo),
        worktree_created_at=123.0,
        **kwargs,
    )


def test_worktree_status_reports_clean_existing_worktree(git_worktree):
    from api.worktrees import worktree_status_for_session

    repo, worktree = git_worktree
    status = worktree_status_for_session(_session_for_worktree(repo, worktree))

    assert status["path"] == str(worktree.resolve())
    assert status["exists"] is True
    assert status["listed"] is True
    assert status["dirty"] is False
    assert status["untracked_count"] == 0
    assert status["ahead_behind"]["available"] is True
    assert status["ahead_behind"]["ahead"] == 0
    assert status["ahead_behind"]["behind"] == 0
    assert status["locked_by_stream"] is False
    assert status["locked_by_terminal"] is False


def test_worktree_status_reports_dirty_untracked_and_ahead(git_worktree):
    from api.worktrees import worktree_status_for_session

    repo, worktree = git_worktree
    (worktree / "README.md").write_text("hello\nedited\n", encoding="utf-8")
    (worktree / "scratch.txt").write_text("local-only\n", encoding="utf-8")
    status = worktree_status_for_session(_session_for_worktree(repo, worktree))

    assert status["dirty"] is True
    assert status["untracked_count"] == 1
    assert status["ahead_behind"]["ahead"] == 0

    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "local change")
    status = worktree_status_for_session(_session_for_worktree(repo, worktree))

    assert status["dirty"] is True
    assert status["untracked_count"] == 1
    assert status["ahead_behind"]["available"] is True
    assert status["ahead_behind"]["ahead"] == 1
    assert status["ahead_behind"]["behind"] == 0


def test_worktree_status_handles_missing_path_without_git_mutation(tmp_path):
    from api.worktrees import worktree_status_for_session

    missing = tmp_path / "missing-worktree"
    status = worktree_status_for_session(
        SimpleNamespace(
            session_id="missing",
            worktree_path=str(missing),
            worktree_repo_root=str(tmp_path / "repo"),
            active_stream_id=None,
        )
    )

    assert status["path"] == str(missing.resolve())
    assert status["exists"] is False
    assert status["dirty"] is False
    assert status["untracked_count"] == 0
    assert status["ahead_behind"]["ahead"] == 0
    assert status["ahead_behind"]["behind"] == 0


def test_worktree_status_uses_live_stream_registry(git_worktree):
    from api.config import STREAMS, STREAMS_LOCK
    from api.worktrees import worktree_status_for_session

    repo, worktree = git_worktree
    session = _session_for_worktree(
        repo,
        worktree,
        active_stream_id="live-stream",
    )

    with STREAMS_LOCK:
        STREAMS["live-stream"] = object()
    try:
        assert worktree_status_for_session(session)["locked_by_stream"] is True
    finally:
        with STREAMS_LOCK:
            STREAMS.pop("live-stream", None)

    assert worktree_status_for_session(session)["locked_by_stream"] is False


def test_worktree_status_reports_live_terminal_lock(git_worktree, monkeypatch):
    import api.terminal as terminal
    from api.worktrees import worktree_status_for_session

    repo, worktree = git_worktree

    class FakeTerminal:
        workspace = str(worktree.resolve())

        def is_alive(self):
            return True

    monkeypatch.setattr(terminal, "get_terminal", lambda session_id: FakeTerminal())

    status = worktree_status_for_session(_session_for_worktree(repo, worktree))

    assert status["locked_by_terminal"] is True


def test_worktree_status_endpoint_returns_session_owned_status(git_worktree, monkeypatch):
    import api.routes as routes

    repo, worktree = git_worktree
    session = _session_for_worktree(repo, worktree, session_id="route_wt")
    session.save()
    captured = {}

    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        ) or True,
    )

    handled = routes.handle_get(
        object(),
        urlparse("/api/session/worktree/status?session_id=route_wt"),
    )

    assert handled is True
    assert captured["status"] == 200
    assert captured["payload"]["status"]["path"] == str(worktree.resolve())
    assert captured["payload"]["status"]["exists"] is True


def test_worktree_status_endpoint_rejects_non_worktree_session(tmp_path, monkeypatch):
    import api.routes as routes

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = Session(session_id="plain", workspace=str(workspace))
    session.save()
    captured = {}

    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, message, status=400: captured.update(
            message=message,
            status=status,
        ) or True,
    )

    handled = routes.handle_get(
        object(),
        urlparse("/api/session/worktree/status?session_id=plain"),
    )

    assert handled is True
    assert captured["status"] == 400
    assert "not worktree-backed" in captured["message"]
