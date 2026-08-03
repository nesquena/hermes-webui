"""Durable single-writer authority for linked Git worktrees."""
from __future__ import annotations

import concurrent.futures
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _capture_post(monkeypatch, body):
    import api.routes as routes

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
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, message, status=400: captured.update(
            payload={"error": message},
            status=status,
        )
        or True,
    )
    return captured


def _isolate_session_store(tmp_path, monkeypatch):
    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return session_dir


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


@pytest.fixture
def linked(tmp_path):
    repo = tmp_path / "repo"
    wt = tmp_path / "linked"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("x\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "worktree", "add", "-q", "-b", "linked", str(wt))
    return repo, wt


def test_identity_is_same_for_root_subdir_and_symlink(linked, tmp_path):
    from api.worktree_authority import canonical_worktree_identity
    _repo, wt = linked
    sub = wt / "a" / "b"
    sub.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(wt, target_is_directory=True)
    assert canonical_worktree_identity(wt) == canonical_worktree_identity(sub) == canonical_worktree_identity(alias)


def test_identity_ignores_git_selector_environment(linked, monkeypatch):
    from api.worktree_authority import is_linked_worktree
    repo, wt = linked
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    monkeypatch.setenv("GIT_COMMON_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(repo))
    assert is_linked_worktree(wt) is True


def test_plain_non_git_workspace_is_not_linked(tmp_path):
    from api.worktree_authority import is_linked_worktree
    plain = tmp_path / "plain"
    plain.mkdir()
    assert is_linked_worktree(plain) is False


def test_claim_is_durable_exclusive_and_transferable(linked, tmp_path):
    from api.worktree_authority import WorktreeAuthority, WorktreeOwnershipError
    _repo, wt = linked
    db = tmp_path / "claims.sqlite3"
    first = WorktreeAuthority(db)
    first.claim(wt, "owner")
    second = WorktreeAuthority(db)
    second.assert_owner(wt, "owner")
    with pytest.raises(WorktreeOwnershipError, match="owned by another session"):
        second.claim(wt, "intruder")
    second.transfer(wt, "owner", "rotated")
    second.assert_owner(wt, "rotated")
    with pytest.raises(WorktreeOwnershipError):
        second.assert_owner(wt, "owner")


def _race_claim(args):
    db, wt, sid = args
    from api.worktree_authority import WorktreeAuthority, WorktreeOwnershipError
    try:
        WorktreeAuthority(db).claim(wt, sid)
        return sid
    except WorktreeOwnershipError:
        return None


def test_concurrent_independent_connections_have_exactly_one_winner(linked, tmp_path):
    _repo, wt = linked
    db = str(tmp_path / "claims.sqlite3")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        winners = list(pool.map(_race_claim, [(db, str(wt), f"s{i}") for i in range(8)]))
    assert len([winner for winner in winners if winner]) == 1


def test_preexisting_linked_worktree_without_claim_fails_closed(linked, tmp_path):
    from api.worktree_authority import WorktreeAuthority, WorktreeOwnershipError
    _repo, wt = linked
    with pytest.raises(WorktreeOwnershipError, match="has no owner"):
        WorktreeAuthority(tmp_path / "claims.sqlite3").assert_owner(wt, "legacy")


def test_archive_ownerless_linked_worktree_session_mutates_only_session_metadata(
    linked, tmp_path, monkeypatch
):
    import api.routes as routes
    import api.worktree_authority as authority_module
    from api.models import Session
    from api.worktree_authority import WorktreeAuthority

    repo, wt = linked
    _isolate_session_store(tmp_path, monkeypatch)
    authority = WorktreeAuthority(tmp_path / "claims.sqlite3")
    monkeypatch.setattr(authority_module, "default_authority", lambda: authority)
    session = Session(
        session_id="ownerless-archive",
        title="Ownerless archive",
        workspace=str(wt),
        worktree_path=str(wt),
        worktree_branch="linked",
        worktree_repo_root=str(repo),
        messages=[{"role": "user", "content": "archive me"}],
    )
    session.save()
    captured = _capture_post(
        monkeypatch,
        {"session_id": session.session_id, "archived": True},
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True

    assert captured["status"] == 200
    assert captured["payload"]["session"]["archived"] is True
    assert Session.load(session.session_id).archived is True
    assert wt.exists()
    with authority._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0

    restored = _capture_post(
        monkeypatch,
        {"session_id": session.session_id, "archived": False},
    )
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True
    assert restored["status"] == 200
    assert restored["payload"]["session"]["archived"] is False
    loaded = Session.load(session.session_id)
    assert loaded is not None
    assert loaded.archived is False
    with authority._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0

    refused = _capture_post(
        monkeypatch,
        {"session_id": session.session_id, "title": "Must stay unchanged"},
    )
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/rename")) is True
    assert refused["status"] == 409
    assert refused["payload"]["error"] == (
        "Worktree write refused: linked worktree has no owner"
    )
    loaded = Session.load(session.session_id)
    assert loaded is not None
    assert loaded.title == "Ownerless archive"


def test_failed_claim_leaves_no_in_memory_ghost(linked, tmp_path, monkeypatch):
    import api.models as models
    import api.worktree_authority as authority_module
    from api.worktree_authority import WorktreeAuthority, WorktreeOwnershipError

    repo, wt = linked
    auth = WorktreeAuthority(tmp_path / "claims.sqlite3")
    auth.claim(wt, "existing-owner")
    monkeypatch.setattr(authority_module, "default_authority", lambda: auth)
    before = set(models.SESSIONS)
    with pytest.raises(WorktreeOwnershipError, match="owned by another session"):
        models.new_session(
            workspace=str(wt),
            worktree_info={
                "path": str(wt),
                "branch": "linked",
                "repo_root": str(repo),
                "created_at": 1.0,
            },
        )
    assert set(models.SESSIONS) == before


def test_failed_initial_save_compensates_claim_and_cache(linked, tmp_path, monkeypatch):
    import api.models as models
    import api.worktree_authority as authority_module
    from api.worktree_authority import WorktreeAuthority, WorktreeOwnershipError

    repo, wt = linked
    auth = WorktreeAuthority(tmp_path / "claims.sqlite3")
    monkeypatch.setattr(authority_module, "default_authority", lambda: auth)
    monkeypatch.setattr(models.Session, "save", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    before = set(models.SESSIONS)
    with pytest.raises(OSError, match="disk full"):
        models.new_session(
            workspace=str(wt),
            worktree_info={
                "path": str(wt),
                "branch": "linked",
                "repo_root": str(repo),
                "created_at": 1.0,
            },
        )
    assert set(models.SESSIONS) == before
    with pytest.raises(WorktreeOwnershipError, match="has no owner"):
        auth.assert_owner(wt, "any")


def test_release_requires_verified_worktree_removal(linked, tmp_path):
    from api.worktree_authority import WorktreeAuthority, WorktreeOwnershipError
    repo, wt = linked
    auth = WorktreeAuthority(tmp_path / "claims.sqlite3")
    identity = auth.claim(wt, "owner")
    with pytest.raises(WorktreeOwnershipError, match="not been positively verified"):
        auth.release_after_removal(identity, "owner", repo)
    _git(repo, "worktree", "remove", str(wt))
    auth.release_after_removal(identity, "owner", repo)


def test_source_contract_covers_lifecycle_chokepoints():
    models = Path("api/models.py").read_text()
    routes = Path("api/routes.py").read_text()
    streaming = Path("api/streaming.py").read_text()
    worktrees = Path("api/worktrees.py").read_text()
    assert models.index("default_authority().claim") < models.index("SESSIONS[s.session_id] = s")
    assert "_guard_request_worktree_ownership(handler, body=body)" in routes
    assert "/btw cannot create a second writer in a linked worktree" in routes
    assert "/background cannot create a second writer in a linked worktree" in routes
    assert "Import into a linked worktree is refused" in routes
    assert "assert_workspace_owner(get_last_workspace(), sid)" in routes
    assert "default_authority().transfer(s.workspace, old_sid, new_sid)" in streaming
    assert streaming.index("default_authority().transfer") < streaming.index("s.session_id = new_sid")
    assert "release_after_removal" in worktrees
