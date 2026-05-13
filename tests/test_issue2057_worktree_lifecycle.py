from types import SimpleNamespace

import api.models as models
import api.routes as routes
from api.models import SESSIONS, Session


def _capture_post(monkeypatch, body):
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )
    return captured


def _isolate_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    SESSIONS.clear()
    return session_dir


def _worktree_session(tmp_path, session_id):
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / f"hermes-{session_id}"
    worktree.mkdir(parents=True)
    s = Session(
        session_id=session_id,
        title="Worktree session",
        workspace=str(worktree),
        worktree_path=str(worktree),
        worktree_branch=f"hermes/{session_id}",
        worktree_repo_root=str(repo),
    )
    s.save()
    return s, worktree


def test_delete_worktree_session_reports_retained_worktree_without_cleanup(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    session, worktree = _worktree_session(tmp_path, "wtdelete1")
    captured = _capture_post(monkeypatch, {"session_id": session.session_id})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: None)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["worktree_retained"] is True
    assert captured["payload"]["worktree_path"] == str(worktree.resolve())
    assert captured["payload"]["worktree_branch"] == "hermes/wtdelete1"
    assert not (session_dir / "wtdelete1.json").exists()
    assert worktree.exists(), "session delete must not remove the git worktree directory"


def test_archive_worktree_session_reports_retained_worktree_without_cleanup(tmp_path, monkeypatch):
    _isolate_session_store(tmp_path, monkeypatch)
    session, worktree = _worktree_session(tmp_path, "wtarchive1")
    captured = _capture_post(
        monkeypatch,
        {"session_id": session.session_id, "archived": True},
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["session"]["archived"] is True
    assert captured["payload"]["worktree_retained"] is True
    assert captured["payload"]["worktree_path"] == str(worktree.resolve())
    assert worktree.exists(), "session archive must not remove the git worktree directory"
    assert Session.load("wtarchive1").archived is True
