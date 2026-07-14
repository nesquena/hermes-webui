"""Tests for the config-level worktree default in /api/session/new (Issue #6022).

Three-value contract:
  - body ``worktree`` ABSENT   -> agent config ``worktree:`` default applies
  - body ``worktree`` explicit -> honored verbatim (explicit always wins)

Config-default requests degrade to a plain session (+ ``worktree_skipped`` in
the payload) on non-git workspaces; explicit requests keep the hard 400.
"""

from types import SimpleNamespace

import pytest

import api.models as models
import api.routes as routes
import api.worktrees as worktrees
from api.models import SESSIONS


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent.resolve()))
    SESSIONS.clear()
    yield session_dir
    SESSIONS.clear()


def _post_session_new(tmp_path, monkeypatch, body, *, config_default, workspace_dir=None, fake_worktree=None):
    """Drive POST /api/session/new with a stubbed transport and worktree factory."""
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    monkeypatch.setattr(
        routes, "_worktree_default_from_config", lambda profile: config_default
    )
    if workspace_dir is not None:
        monkeypatch.setattr(
            routes, "resolve_trusted_workspace", lambda raw: workspace_dir
        )
    if fake_worktree is not None:
        monkeypatch.setattr(
            worktrees, "create_worktree_for_workspace", lambda workspace: fake_worktree
        )
    captured = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        )
        or True,
    )
    import api.helpers as helpers

    monkeypatch.setattr(
        helpers,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        )
        or True,
    )
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/new")) is True
    return captured


def _fake_worktree_info(repo, worktree):
    return {
        "path": str(worktree),
        "branch": "hermes/hermes-6022",
        "repo_root": str(repo),
        "created_at": 321.0,
    }


def _mk_repo_dirs(tmp_path):
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / "hermes-6022"
    repo.mkdir()
    worktree.mkdir(parents=True)
    return repo, worktree


# ── Route matrix: absent key ─────────────────────────────────────────────────


def test_absent_key_with_config_default_off_creates_plain_session(tmp_path, monkeypatch):
    repo, _ = _mk_repo_dirs(tmp_path)
    captured = _post_session_new(
        tmp_path,
        monkeypatch,
        {"workspace": str(repo), "profile": "default"},
        config_default=False,
        workspace_dir=repo,
    )
    assert captured["status"] == 200
    session = captured["payload"]["session"]
    assert session.get("worktree_path") in (None, "")
    assert "worktree_skipped" not in captured["payload"]


def test_absent_key_with_config_default_on_creates_worktree_session(tmp_path, monkeypatch):
    repo, worktree = _mk_repo_dirs(tmp_path)
    captured = _post_session_new(
        tmp_path,
        monkeypatch,
        {"workspace": str(repo), "profile": "default"},
        config_default=True,
        workspace_dir=repo,
        fake_worktree=_fake_worktree_info(repo, worktree),
    )
    assert captured["status"] == 200
    session = captured["payload"]["session"]
    assert session["workspace"] == str(worktree.resolve())
    assert session["worktree_path"] == str(worktree.resolve())
    assert session["worktree_branch"] == "hermes/hermes-6022"


# ── Route matrix: explicit wins ──────────────────────────────────────────────


def test_explicit_false_beats_config_default_true(tmp_path, monkeypatch):
    repo, worktree = _mk_repo_dirs(tmp_path)

    def _fail(workspace):
        pytest.fail("worktree must not be created when body says worktree=false")

    monkeypatch.setattr(worktrees, "create_worktree_for_workspace", _fail)
    captured = _post_session_new(
        tmp_path,
        monkeypatch,
        {"workspace": str(repo), "profile": "default", "worktree": False},
        config_default=True,
        workspace_dir=repo,
    )
    assert captured["status"] == 200
    session = captured["payload"]["session"]
    assert session.get("worktree_path") in (None, "")


def test_explicit_null_beats_config_default_true(tmp_path, monkeypatch):
    """Sending the key at all — even ``worktree: null`` — is an explicit
    statement and must never fall through to the config default."""
    repo, worktree = _mk_repo_dirs(tmp_path)

    def _fail(workspace):
        pytest.fail("worktree must not be created when body sends worktree=null")

    monkeypatch.setattr(worktrees, "create_worktree_for_workspace", _fail)
    captured = _post_session_new(
        tmp_path,
        monkeypatch,
        {"workspace": str(repo), "profile": "default", "worktree": None},
        config_default=True,
        workspace_dir=repo,
    )
    assert captured["status"] == 200
    session = captured["payload"]["session"]
    assert session.get("worktree_path") in (None, "")


def test_explicit_true_with_config_default_off_creates_worktree(tmp_path, monkeypatch):
    repo, worktree = _mk_repo_dirs(tmp_path)
    captured = _post_session_new(
        tmp_path,
        monkeypatch,
        {"workspace": str(repo), "profile": "default", "worktree": True},
        config_default=False,
        workspace_dir=repo,
        fake_worktree=_fake_worktree_info(repo, worktree),
    )
    assert captured["status"] == 200
    session = captured["payload"]["session"]
    assert session["worktree_path"] == str(worktree.resolve())


# ── Route matrix: non-git workspace ──────────────────────────────────────────


def test_config_default_on_non_git_workspace_falls_back_to_plain_session(tmp_path, monkeypatch):
    non_git = tmp_path / "plain"
    non_git.mkdir()
    captured = _post_session_new(
        tmp_path,
        monkeypatch,
        {"workspace": str(non_git), "profile": "default"},
        config_default=True,
        workspace_dir=non_git,
        # real create_worktree_for_workspace -> find_git_repo_root raises ValueError
    )
    assert captured["status"] == 200
    session = captured["payload"]["session"]
    assert session.get("worktree_path") in (None, "")
    assert "not inside a git repository" in captured["payload"]["worktree_skipped"]


def test_explicit_true_on_non_git_workspace_still_hard_400(tmp_path, monkeypatch):
    non_git = tmp_path / "plain"
    non_git.mkdir()
    captured = _post_session_new(
        tmp_path,
        monkeypatch,
        {"workspace": str(non_git), "profile": "default", "worktree": True},
        config_default=False,
        workspace_dir=non_git,
    )
    assert captured["status"] == 400
    assert "not inside a git repository" in captured["payload"].get("error", "")


# ── Config helper ────────────────────────────────────────────────────────────


def test_worktree_default_reads_ambient_config_when_no_profile(monkeypatch):
    monkeypatch.setattr(
        routes, "get_config_for_profile_home", lambda home: {"worktree": True}
    )
    assert routes._worktree_default_from_config(None) is True
    monkeypatch.setattr(
        routes, "get_config_for_profile_home", lambda home: {"worktree": False}
    )
    assert routes._worktree_default_from_config(None) is False
    monkeypatch.setattr(routes, "get_config_for_profile_home", lambda home: {})
    assert routes._worktree_default_from_config(None) is False


def test_worktree_default_resolves_named_profile_home(monkeypatch, tmp_path):
    import api.profiles as profiles

    seen = {}
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", lambda name: tmp_path / name
    )

    def fake_cfg(home):
        seen["home"] = home
        return {"worktree": True}

    monkeypatch.setattr(routes, "get_config_for_profile_home", fake_cfg)
    assert routes._worktree_default_from_config("work") is True
    assert seen["home"] == tmp_path / "work"


def test_worktree_default_requires_strict_boolean_true(monkeypatch):
    """Only a real YAML ``true`` opts in — malformed shapes (quoted strings,
    ints, lists, dicts, null) must fall to the safe no-worktree default, not
    truthiness-coerce into minting worktrees."""
    for malformed in ("true", "yes", 1, [True], {"enabled": True}, None, 0, ""):
        monkeypatch.setattr(
            routes,
            "get_config_for_profile_home",
            lambda home, _v=malformed: {"worktree": _v},
        )
        assert routes._worktree_default_from_config(None) is False, (
            f"non-boolean config value {malformed!r} must not enable worktrees"
        )
    monkeypatch.setattr(
        routes, "get_config_for_profile_home", lambda home: {"worktree": True}
    )
    assert routes._worktree_default_from_config(None) is True


def test_worktree_default_is_fail_soft_on_config_errors(monkeypatch):
    def boom(home):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(routes, "get_config_for_profile_home", boom)
    assert routes._worktree_default_from_config(None) is False
