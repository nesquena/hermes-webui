"""#7217 P1-b: local git consumers resolve through the translation tier.

Drives the REAL ``_git_local_workspace`` / ``_git_session_and_workspace`` and
the REAL ``GET /api/git-info`` route. Seams stubbed: the profile-home resolver
and saved-workspace list (temp dirs), plus ``routes.get_session`` for the
route-level tests (the workspace/profile attributes are the contract under
test, not session loading).

Covered:
  1. a mapped profile's git workspace is the translated WebUI-local root;
  2. an unmapped (single-container) profile keeps the pre-existing direct path;
  3. a legacy session without a profile keeps the direct path (explicitly
     deferred in the PR, pinned here so the deferral cannot drift silently);
  4. a workspace that fails translation fails closed with 400, never a
     half-translated path;
  5. /api/git-info translates for WebUI sessions and for CLI sessions that
     carry a profile, and reports {"git": null} when translation fails.
"""

import io
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

routes = pytest.importorskip("api.routes")
workspace = pytest.importorskip("api.workspace")
profiles = pytest.importorskip("api.profiles")


class _FakeHandler:
    def __init__(self, path="/api/git-info"):
        self.status = None
        self.response_headers = {}
        self.headers = {}
        self.command = "GET"
        self.path = path
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


@pytest.fixture
def git_world(tmp_path, monkeypatch):
    """alice maps /workspace -> <home>/webui-ws; bravo has no map."""
    profiles_root = tmp_path / "profiles"
    fake_home = tmp_path / "home"
    webui_ws = fake_home / "webui-ws"
    webui_ws.mkdir(parents=True)
    (profiles_root / "alice").mkdir(parents=True)
    (profiles_root / "alice" / "config.yaml").write_text(
        "webui:\n"
        "  workspace_path_maps:\n"
        "    - agent: /workspace\n"
        f"      webui: {webui_ws}\n",
        encoding="utf-8",
    )
    (profiles_root / "bravo").mkdir(parents=True)
    (profiles_root / "bravo" / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    monkeypatch.setattr(workspace, "_home_path", lambda: fake_home)
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile",
        lambda name: profiles_root / (name or "alice"),
    )
    monkeypatch.setattr(
        workspace, "load_workspaces",
        lambda profile=None: [{"path": str(webui_ws)}] if profile == "alice" else [],
    )
    return {"webui_ws": webui_ws, "profiles_root": profiles_root}


class TestGitLocalWorkspace:
    def test_mapped_profile_translates(self, git_world):
        handler = _FakeHandler()
        session = SimpleNamespace(workspace="/workspace", profile="alice")
        got = routes._git_local_workspace(handler, session)
        assert got == git_world["webui_ws"].resolve()
        # The session object itself is never rewritten.
        assert session.workspace == "/workspace"

    def test_unmapped_profile_keeps_direct_path(self, git_world, tmp_path):
        direct = tmp_path / "plain-repo"
        direct.mkdir()
        handler = _FakeHandler()
        session = SimpleNamespace(workspace=str(direct), profile="bravo")
        got = routes._git_local_workspace(handler, session)
        assert got == Path(direct)

    def test_legacy_session_without_profile_keeps_direct_path(self, tmp_path):
        direct = tmp_path / "legacy-repo"
        direct.mkdir()
        handler = _FakeHandler()
        session = SimpleNamespace(workspace=str(direct), profile=None)
        got = routes._git_local_workspace(handler, session)
        assert got == Path(direct)

    def test_unresolvable_workspace_fails_closed_400(self, git_world):
        handler = _FakeHandler()
        # /workspace is mapped for alice, but the agent-side path /nowhere is
        # not mapped and does not exist locally -> fail closed, not pass-through.
        session = SimpleNamespace(workspace="/nowhere/deep", profile="alice")
        got = routes._git_local_workspace(handler, session)
        assert got is None
        assert handler.status == 400


class TestGitInfoRoute:
    def test_webui_session_translates(self, git_world, monkeypatch):
        session = SimpleNamespace(workspace="/workspace", profile="alice")
        monkeypatch.setattr(routes, "get_session", lambda sid, **kw: session)
        handler = _FakeHandler("/api/git-info?session_id=sess_a")
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200

    def test_translation_failure_reports_git_null(self, git_world, monkeypatch):
        session = SimpleNamespace(workspace="/nowhere/deep", profile="alice")
        monkeypatch.setattr(routes, "get_session", lambda sid, **kw: session)
        handler = _FakeHandler("/api/git-info?session_id=sess_a")
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        assert b'"git"' in handler.wfile.getvalue() and b'null' in handler.wfile.getvalue()

    def test_missing_session_id_is_400(self, git_world):
        handler = _FakeHandler("/api/git-info")
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 400

# ── #8450 re-gate round 4: a CLI session's workspace keeps TRUST validation ───
#
# `state.db` rows always carry a profile, so the git-info route's CLI branch
# took the "has a profile" arm for EVERY CLI session -- and that arm's no-map
# branch is a lexical resolve with no trust, existence or block-list check.
# Master called `resolve_trusted_workspace()` there, so a workspace outside
# the home (or revoked from the saved list) was refused. This head answered
# it: the branch and file counts came back for a repository the operator had
# removed from the saved workspaces, and a deleted directory produced a
# misleading 400 ("Git is not installed") instead of "no git info".
#
# The fix is the same shape as the upload / list-dir handlers: translate only
# when the OWNER profile's declared map actually translates the workspace, and
# keep the trusted resolution otherwise.


def _init_git_repo(path: Path) -> Path:
    """A real git repository, so a leaked workspace really answers git-info."""
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git not installed")
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(
        ["git", "-C", str(path), *a], check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    (path / "a.txt").write_text("a", encoding="utf-8")
    run("add", "a.txt")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return path


@pytest.fixture
def cli_world(tmp_path, monkeypatch):
    """A world with a mapped profile (alice) and a map-less one (bravo).

    ``bravo`` has no declared map, so its sessions keep the pre-existing
    direct-path semantics -- and therefore the TRUSTED resolution.
    """
    profiles_root = tmp_path / "profiles"
    fake_home = tmp_path / "home"
    webui_ws = fake_home / "webui-ws"
    webui_ws.mkdir(parents=True)
    (profiles_root / "alice").mkdir(parents=True)
    (profiles_root / "alice" / "config.yaml").write_text(
        "webui:\n"
        "  workspace_path_maps:\n"
        "    - agent: /workspace\n"
        f"      webui: {webui_ws}\n",
        encoding="utf-8",
    )
    (profiles_root / "bravo").mkdir(parents=True)
    (profiles_root / "bravo" / "config.yaml").write_text(
        "terminal:\n  backend: local\n", encoding="utf-8"
    )
    monkeypatch.setattr(workspace, "_home_path", lambda: fake_home)
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile",
        lambda name: profiles_root / (name or "alice"),
    )
    # bravo has no saved workspaces at all: the repo was revoked.
    monkeypatch.setattr(
        workspace, "load_workspaces",
        lambda profile=None: [{"path": str(webui_ws)}] if profile == "alice" else [],
    )
    return {
        "webui_ws": webui_ws,
        "profiles_root": profiles_root,
        "home": fake_home,
    }


def _cli_session(monkeypatch, sid, ws, profile):
    """Make *sid* a state.db-only CLI session with the given workspace."""
    def _missing(*_a, **_kw):
        raise KeyError(sid)

    monkeypatch.setattr(routes, "get_session", _missing)
    monkeypatch.setattr(
        routes, "get_cli_sessions",
        lambda *_a, **_kw: [
            {"session_id": sid, "workspace": str(ws), "profile": profile}
        ],
    )


def _git_info_call(sid):
    handler = _FakeHandler(f"/api/git-info?session_id={sid}")
    routes.handle_get(handler, urlparse(handler.path))
    return handler


class TestGitInfoCliSessionTrust:
    def test_a_cli_repo_outside_home_and_revoked_is_not_inspected(
        self, cli_world, tmp_path, monkeypatch
    ):
        """Row 1: a real repo outside the home, revoked from the saved list.

        Master: ``{"git": null}`` and git never runs.
        """
        repo = _init_git_repo(tmp_path / "outside-repo")
        _cli_session(monkeypatch, "cli_revoked", repo, "bravo")

        calls = []
        import api.workspace_git as workspace_git

        monkeypatch.setattr(
            workspace_git, "git_status",
            lambda ws: calls.append(ws) or {"is_git": True, "branch": "main"},
        )

        handler = _git_info_call("cli_revoked")

        assert handler.status == 200
        assert b'"git"' in handler.wfile.getvalue() and b"null" in handler.wfile.getvalue()
        assert calls == [], f"git ran on an untrusted workspace: {calls}"

    def test_a_deleted_cli_workspace_reports_no_git_info(
        self, cli_world, tmp_path, monkeypatch
    ):
        """Row 2: a workspace that no longer exists.

        Master: ``{"git": null}``. This head answered 400 ``missing_git``
        ("Git is not installed") -- a visible sidebar regression with a
        misleading error, since git was never the problem.
        """
        gone = tmp_path / "deleted-repo"
        gone.mkdir()
        _cli_session(monkeypatch, "cli_deleted", gone, "bravo")
        gone.rmdir()  # the directory is gone, but the session still points at it

        handler = _git_info_call("cli_deleted")

        assert handler.status == 200, handler.wfile.getvalue()
        assert b'"git"' in handler.wfile.getvalue() and b"null" in handler.wfile.getvalue()

    def test_an_unknown_owner_profile_is_not_inspected(
        self, cli_world, tmp_path, monkeypatch
    ):
        """Row 3: the session names a profile that does not exist.

        Master: ``{"git": null}`` and git never runs.
        """
        repo = _init_git_repo(tmp_path / "ghost-repo")
        _cli_session(monkeypatch, "cli_ghost", repo, "ghost")

        calls = []
        import api.workspace_git as workspace_git

        monkeypatch.setattr(
            workspace_git, "git_status",
            lambda ws: calls.append(ws) or {"is_git": True, "branch": "main"},
        )

        handler = _git_info_call("cli_ghost")

        assert handler.status == 200
        assert b'"git"' in handler.wfile.getvalue() and b"null" in handler.wfile.getvalue()
        assert calls == [], f"git ran for an unknown owner profile: {calls}"

    def test_a_mapped_cli_session_still_translates(
        self, cli_world, tmp_path, monkeypatch
    ):
        """The mapped case must keep working: this is the whole point of the map.

        Alice's CLI session points at the agent-visible ``/workspace``; the
        WebUI-local root is what the local git reader has to see. If the fix
        degraded every CLI session to the trusted resolution, a mapped
        multi-container profile would break instead of being fixed.
        """
        _cli_session(monkeypatch, "cli_mapped", "/workspace", "alice")

        seen = []
        import api.workspace_git as workspace_git

        def _status(ws):
            seen.append(ws)
            return {"is_git": True, "branch": "main"}

        monkeypatch.setattr(workspace_git, "git_status", _status)

        handler = _git_info_call("cli_mapped")

        assert handler.status == 200, handler.wfile.getvalue()
        assert seen, "the mapped profile's workspace must reach the git reader"
        assert seen[0] == cli_world["webui_ws"].resolve(), (
            f"the mapped session must read the WebUI-local root, got {seen[0]}"
        )


class TestGitCommitMessageTranslates:
    """The commit-message handlers are local git readers.

    ``Path(session.workspace)`` is the AGENT-visible path. In a multi-container
    deployment that directory does not exist on the WebUI host, so the prompt
    was built from the wrong tree -- or from nothing at all. The local git
    readers have to work on the WebUI-local root, the same tier the file
    manager and the Git badge use.
    """

    def _session(self, monkeypatch, workspace, profile):
        class _S:
            pass

        session = _S()
        session.session_id = "sid-cmm"
        session.workspace = workspace
        session.profile = profile
        monkeypatch.setattr(routes, "get_session", lambda sid, **kw: session)
        return session

    def _post(self, monkeypatch, path, body):
        handler = _FakeHandler(path)
        monkeypatch.setattr(routes, "_check_csrf", lambda _h: True)
        monkeypatch.setattr(routes, "read_body", lambda _h: body)
        routes.handle_post(handler, urlparse(path))
        return handler

    def test_the_staged_commit_message_reads_the_webui_local_root(
        self, cli_world, monkeypatch
    ):
        seen = []
        import api.workspace_git as workspace_git

        monkeypatch.setattr(
            workspace_git, "staged_commit_message_prompt",
            lambda ws: seen.append(ws) or {
                "system_prompt": "sys", "user_prompt": "user", "truncated": False,
            },
        )
        monkeypatch.setattr(
            routes, "_llm_git_commit_message",
            lambda system_prompt, user_prompt, session=None: "chore: touch",
        )
        self._session(monkeypatch, "/workspace", "alice")

        handler = self._post(
            monkeypatch, "/api/git/commit-message", {"session_id": "sid-cmm"}
        )

        assert handler.status == 200, handler.wfile.getvalue()
        assert seen, "the commit-message prompt must be built from a workspace"
        assert seen[0] == cli_world["webui_ws"].resolve(), (
            f"the commit message must read the WebUI-local root, got {seen[0]}"
        )

    def test_the_selected_commit_message_reads_the_webui_local_root(
        self, cli_world, monkeypatch
    ):
        seen = []
        import api.workspace_git as workspace_git

        monkeypatch.setattr(
            workspace_git, "selected_commit_message_prompt",
            lambda ws, paths: seen.append(ws) or {
                "system_prompt": "sys", "user_prompt": "user", "truncated": False,
            },
        )
        monkeypatch.setattr(
            routes, "_llm_git_commit_message",
            lambda system_prompt, user_prompt, session=None: "chore: touch",
        )
        self._session(monkeypatch, "/workspace", "alice")

        handler = self._post(
            monkeypatch, "/api/git/commit-message-selected",
            {"session_id": "sid-cmm", "paths": ["a.txt"]},
        )

        assert handler.status == 200, handler.wfile.getvalue()
        assert seen, "the commit-message prompt must be built from a workspace"
        assert seen[0] == cli_world["webui_ws"].resolve(), (
            f"the commit message must read the WebUI-local root, got {seen[0]}"
        )

    def test_a_profile_less_session_keeps_its_direct_path(
        self, cli_world, tmp_path, monkeypatch
    ):
        """A legacy row without a profile keeps the pre-existing behaviour."""
        plain = tmp_path / "plain-repo"
        plain.mkdir()
        seen = []
        import api.workspace_git as workspace_git

        monkeypatch.setattr(
            workspace_git, "staged_commit_message_prompt",
            lambda ws: seen.append(ws) or {
                "system_prompt": "sys", "user_prompt": "user", "truncated": False,
            },
        )
        monkeypatch.setattr(
            routes, "_llm_git_commit_message",
            lambda system_prompt, user_prompt, session=None: "chore: touch",
        )
        self._session(monkeypatch, str(plain), None)

        handler = self._post(
            monkeypatch, "/api/git/commit-message", {"session_id": "sid-cmm"}
        )

        assert handler.status == 200, handler.wfile.getvalue()
        assert seen[0] == Path(str(plain)), (
            f"a profile-less session must keep the direct path, got {seen[0]}"
        )
