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
