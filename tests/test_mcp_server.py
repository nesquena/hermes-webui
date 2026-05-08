"""Tests for mcp_server.py — Option A rewrite (Issue #1616).

Covers: project CRUD, profile scoping, title collision, color validation,
session listing, cross-profile isolation.

Uses HERMES_WEBUI_STATE_DIR env var to point to a temp directory,
so tests don't touch the real webui state. Module is re-imported
per test class to ensure clean state.
"""

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

# ── Ensure repo root on path ──────────────────────────────────────────────
_REPO = Path(__file__).parent.parent.resolve()
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _fresh_state_dir():
    """Create a clean temp state dir and set HERMES_WEBUI_STATE_DIR."""
    td = tempfile.mkdtemp()
    state_dir = Path(td)
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (state_dir / "projects.json").write_text("[]", encoding="utf-8")
    (sessions_dir / "_index.json").write_text("[]", encoding="utf-8")
    os.environ["HERMES_WEBUI_STATE_DIR"] = str(state_dir)
    return state_dir


def _cleanup_state_dir(state_dir: Path):
    """Remove temp state dir and clear env var."""
    import shutil
    shutil.rmtree(state_dir, ignore_errors=True)
    os.environ.pop("HERMES_WEBUI_STATE_DIR", None)


def _reimport_mcp():
    """Re-import mcp_server with current env vars and profile.

    Returns (mcp_module, profiles_module) — profiles_module is the
    live api.profiles reference that the re-imported mcp_server uses.
    """
    # Clear cached module and api submodules that cache paths
    for key in list(sys.modules.keys()):
        if key == 'mcp_server' or key.startswith('mcp_server.') or \
           key == 'api.config' or key == 'api.models' or key == 'api.profiles':
            del sys.modules[key]

    import importlib
    import api.config as cfg
    importlib.reload(cfg)

    # Re-acquire api.profiles reference (old one is stale after sys.modules clear)
    import api.profiles as fresh_profiles
    fresh_profiles._active_profile = 'default'

    import mcp_server as mod
    return mod, fresh_profiles


async def _call(mod, tool_name, **kwargs):
    """Call a tool handler and return parsed JSON."""
    handler = mod.HANDLERS[tool_name]
    result = await handler(kwargs)
    return json.loads(result[0].text)


# ═══════════════════════════════════════════════════════════════════════════
#  Project CRUD
# ═══════════════════════════════════════════════════════════════════════════

class TestCreateProject:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.state_dir = _fresh_state_dir()
        self.mod, self.profiles = _reimport_mcp()
        yield
        _cleanup_state_dir(self.state_dir)

    async def test_create_basic(self):
        result = await _call(self.mod, "create_project", name="Test Project")
        assert "project_id" in result
        assert result["name"] == "Test Project"
        assert result["profile"] == "default"
        assert result["session_count"] == 0

    async def test_create_with_color(self):
        result = await _call(self.mod, "create_project",
                             name="Colored", color="#ff6600")
        assert result["color"] == "#ff6600"

    async def test_create_duplicate_exact_match(self):
        await _call(self.mod, "create_project", name="My Project")
        result = await _call(self.mod, "create_project", name="My Project")
        assert "error" in result
        assert "already exists" in result["error"]

    async def test_create_case_sensitive_no_collision(self):
        """Exact match: 'MY project' and 'My Project' are different."""
        await _call(self.mod, "create_project", name="My Project")
        result = await _call(self.mod, "create_project", name="MY project")
        assert "project_id" in result

    async def test_create_empty_name(self):
        result = await _call(self.mod, "create_project", name="")
        assert "error" in result

    async def test_create_invalid_color(self):
        result = await _call(self.mod, "create_project",
                             name="Bad", color="not-a-color")
        assert "error" in result
        assert "Invalid color" in result["error"]

    async def test_create_valid_color_formats(self):
        for color in ["#fff", "#ff6600", "#ff6600aa"]:
            result = await _call(self.mod, "create_project",
                                 name=f"Color-{color}", color=color)
            assert result["color"] == color


class TestRenameProject:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.state_dir = _fresh_state_dir()
        self.mod, self.profiles = _reimport_mcp()
        yield
        _cleanup_state_dir(self.state_dir)

    async def test_rename_basic(self):
        created = await _call(self.mod, "create_project", name="Old")
        pid = created["project_id"]
        result = await _call(self.mod, "rename_project",
                             project_id=pid, name="New")
        assert result["name"] == "New"
        assert result["project_id"] == pid

    async def test_rename_with_color(self):
        created = await _call(self.mod, "create_project", name="X")
        result = await _call(self.mod, "rename_project",
                             project_id=created["project_id"],
                             name="X", color="#000")
        assert result["color"] == "#000"

    async def test_rename_not_found(self):
        result = await _call(self.mod, "rename_project",
                             project_id="nonexistent", name="Nope")
        assert "error" in result

    async def test_rename_wrong_profile(self):
        created = await _call(self.mod, "create_project", name="DefaultOwned")
        pid = created["project_id"]
        self.profiles._active_profile = 'other'
        result = await _call(self.mod, "rename_project",
                             project_id=pid, name="Stolen")
        assert "error" in result
        assert "not found" in result["error"].lower()


class TestDeleteProject:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.state_dir = _fresh_state_dir()
        self.mod, self.profiles = _reimport_mcp()
        yield
        _cleanup_state_dir(self.state_dir)

    async def test_delete_basic(self):
        created = await _call(self.mod, "create_project", name="ToDelete")
        pid = created["project_id"]
        result = await _call(self.mod, "delete_project", project_id=pid)
        assert result["ok"] is True
        assert result["deleted"] == "ToDelete"

    async def test_delete_not_found(self):
        result = await _call(self.mod, "delete_project",
                             project_id="nonexistent")
        assert "error" in result

    async def test_delete_wrong_profile(self):
        created = await _call(self.mod, "create_project", name="Owned")
        pid = created["project_id"]
        self.profiles._active_profile = 'other'
        result = await _call(self.mod, "delete_project", project_id=pid)
        assert "error" in result

    async def test_delete_no_auth_refuses_unassign(self):
        """Without HERMES_WEBUI_PASSWORD, delete_project must NOT touch
        session JSONs. Direct FS writes would bypass _write_session_index()
        and leave _index.json holding the stale project_id, causing a
        running WebUI to keep grouping sessions under the deleted project.

        The handler should: delete the project from projects.json, leave
        every session JSON untouched, leave the index untouched, and
        surface a `warning` field telling the operator to set the env var.
        """
        from api.config import SESSION_DIR, SESSION_INDEX_FILE
        os.environ.pop("HERMES_WEBUI_PASSWORD", None)

        # Create project + a session JSON that points at it
        created = await _call(self.mod, "create_project", name="ToDelete")
        pid = created["project_id"]
        sid = "test_sess_001"
        session_path = SESSION_DIR / f"{sid}.json"
        session_payload = {
            "session_id": sid,
            "title": "T",
            "project_id": pid,
            "messages": [],
        }
        session_path.write_text(json.dumps(session_payload), encoding="utf-8")
        # Index references the session under the project
        SESSION_INDEX_FILE.write_text(
            json.dumps([{"session_id": sid, "project_id": pid, "title": "T"}]),
            encoding="utf-8")
        index_before = SESSION_INDEX_FILE.read_text(encoding="utf-8")
        session_before = session_path.read_text(encoding="utf-8")

        result = await _call(self.mod, "delete_project", project_id=pid)

        assert result["ok"] is True
        assert result["unassigned_sessions"] == 0
        assert "warning" in result
        assert "HERMES_WEBUI_PASSWORD" in result["warning"]
        # Session JSON untouched
        assert session_path.read_text(encoding="utf-8") == session_before
        # Index untouched
        assert SESSION_INDEX_FILE.read_text(encoding="utf-8") == index_before


# ═══════════════════════════════════════════════════════════════════════════
#  Profile Scoping
# ═══════════════════════════════════════════════════════════════════════════

class TestProfileScoping:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.state_dir = _fresh_state_dir()
        self.mod, self.profiles = _reimport_mcp()
        yield
        _cleanup_state_dir(self.state_dir)

    async def test_projects_tagged_with_profile(self):
        result = await _call(self.mod, "create_project", name="Tagged")
        assert result["profile"] == "default"

    async def test_list_projects_respects_profile(self):
        # Create under default
        await _call(self.mod, "create_project", name="DefaultProject")

        # Switch to other
        self.profiles._active_profile = 'other'
        await _call(self.mod, "create_project", name="OtherProject")

        # List should only show current profile's projects
        projects = await _call(self.mod, "list_projects")
        names = [p["name"] for p in projects]
        assert "OtherProject" in names
        assert "DefaultProject" not in names

        # Switch back
        self.profiles._active_profile = 'default'
        projects = await _call(self.mod, "list_projects")
        names = [p["name"] for p in projects]
        assert "DefaultProject" in names
        assert "OtherProject" not in names

    async def test_cross_profile_isolation_create(self):
        """Same name in different profiles should be allowed."""
        await _call(self.mod, "create_project", name="Shared")
        self.profiles._active_profile = 'other'
        result = await _call(self.mod, "create_project", name="Shared")
        assert "project_id" in result

    async def test_legacy_untagged_hidden_from_non_root_profile(self):
        """Untagged projects (no `profile` field) belong to the root profile.

        Mirrors api/routes.py:_profiles_match where a missing profile coerces
        to 'default'. A non-root profile must NOT see legacy untagged rows.
        """
        # Manually write a legacy untagged project (pre-#1614 schema)
        from api.config import PROJECTS_FILE
        legacy = [{
            "project_id": "legacy000001",
            "name": "LegacyUntagged",
            "color": None,
            "created_at": 1700000000.0,
            # No "profile" field on purpose
        }]
        PROJECTS_FILE.write_text(json.dumps(legacy), encoding="utf-8")

        # Non-root profile must NOT see it
        self.profiles._active_profile = 'other'
        projects = await _call(self.mod, "list_projects")
        names = [p["name"] for p in projects]
        assert "LegacyUntagged" not in names

        # Root profile still sees it (load_projects backfills `profile`
        # to 'default', so visibility is preserved for the root).
        self.profiles._active_profile = 'default'
        projects = await _call(self.mod, "list_projects")
        names = [p["name"] for p in projects]
        assert "LegacyUntagged" in names

    async def test_legacy_untagged_rename_blocked_from_non_root(self):
        """Non-root profile cannot rename a legacy untagged project."""
        from api.config import PROJECTS_FILE
        legacy = [{
            "project_id": "legacy000002",
            "name": "Legacy",
            "color": None,
            "created_at": 1700000000.0,
        }]
        PROJECTS_FILE.write_text(json.dumps(legacy), encoding="utf-8")
        self.profiles._active_profile = 'other'
        result = await _call(self.mod, "rename_project",
                             project_id="legacy000002", name="Stolen")
        assert "error" in result


# ═══════════════════════════════════════════════════════════════════════════
#  Session listing
# ═══════════════════════════════════════════════════════════════════════════

class TestListSessions:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.state_dir = _fresh_state_dir()
        self.mod, self.profiles = _reimport_mcp()
        yield
        _cleanup_state_dir(self.state_dir)

    async def test_list_empty(self):
        result = await _call(self.mod, "list_sessions")
        assert result == []

    async def test_list_with_limit(self):
        result = await _call(self.mod, "list_sessions", limit=10)
        assert isinstance(result, list)

    async def test_list_unassigned(self):
        result = await _call(self.mod, "list_sessions", unassigned=True)
        assert isinstance(result, list)


# ═══════════════════════════════════════════════════════════════════════════
#  Session mutations (HTTP API — basic validation only)
# ═══════════════════════════════════════════════════════════════════════════

class TestSessionMutations:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.state_dir = _fresh_state_dir()
        self.mod, self.profiles = _reimport_mcp()
        yield
        _cleanup_state_dir(self.state_dir)

    async def test_rename_missing_args(self):
        result = await _call(self.mod, "rename_session",
                             session_id="", title="")
        assert "error" in result

    async def test_move_missing_args(self):
        result = await _call(self.mod, "move_session",
                             session_id="", project_id="x")
        assert "error" in result

    async def test_move_project_not_found(self):
        result = await _call(self.mod, "move_session",
                             session_id="s1", project_id="nonexistent")
        assert "error" in result

    async def test_move_target_owned_by_other_profile_rejected(self):
        """A project owned by profile A is invisible to profile B (#1614)."""
        created = await _call(self.mod, "create_project", name="ATarget")
        pid = created["project_id"]
        self.profiles._active_profile = 'other'
        result = await _call(self.mod, "move_session",
                             session_id="any", project_id=pid)
        assert "error" in result
        assert "not found" in result["error"].lower()


# ═══════════════════════════════════════════════════════════════════════════
#  Auth helper
# ═══════════════════════════════════════════════════════════════════════════

class TestApiPassword:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.state_dir = _fresh_state_dir()
        # Ensure env var is unset for the test
        os.environ.pop("HERMES_WEBUI_PASSWORD", None)
        self.mod, self.profiles = _reimport_mcp()
        yield
        _cleanup_state_dir(self.state_dir)

    async def test_no_env_no_settings_returns_none(self):
        assert self.mod._api_password() is None

    async def test_password_hash_in_settings_is_ignored(self):
        """settings.json holds a hash, not a plaintext password — must NOT
        be returned as if it were a usable password."""
        from api.config import STATE_DIR as _SD
        (_SD / "settings.json").write_text(
            json.dumps({"password_hash": "$2b$12$abcdefghijk"}),
            encoding="utf-8")
        assert self.mod._api_password() is None

    async def test_env_var_returned(self):
        os.environ["HERMES_WEBUI_PASSWORD"] = "secret123"
        try:
            assert self.mod._api_password() == "secret123"
        finally:
            os.environ.pop("HERMES_WEBUI_PASSWORD", None)


# ═══════════════════════════════════════════════════════════════════════════
#  _profiles_match parity (mcp_server vs api.routes vs api.profiles)
# ═══════════════════════════════════════════════════════════════════════════
#
# Locks the canonical-helper relocation: mcp_server.py and api/routes.py both
# now import _profiles_match from api/profiles.py. If anyone re-introduces a
# local copy in either module, both the identity check and the input-matrix
# parametrize trip immediately.

async def test_profiles_match_single_source_of_truth():
    """All three module names resolve to the same canonical object.

    This locks the relocation: mcp_server.py and api/routes.py both import
    _profiles_match from api/profiles.py rather than carrying a local copy.
    Re-introducing a local definition in either module trips this test
    immediately.

    Imported here in a clean module-import context (not via _reimport_mcp,
    which would re-execute api/profiles.py and produce a distinct function
    object that's behaviorally identical but fails the `is` check).
    """
    # Make sure no test fixture left a re-import side-effect on these modules.
    for k in ('mcp_server', 'api.routes', 'api.profiles'):
        sys.modules.pop(k, None)
    import api.profiles as _profiles_mod
    import api.routes as _routes_mod
    import mcp_server as _mcp_mod
    canonical = _profiles_mod._profiles_match
    assert _routes_mod._profiles_match is canonical
    assert _mcp_mod._profiles_match is canonical


@pytest.mark.parametrize("a, b", [
    (None, None),
    (None, ''),
    ('', None),
    ('', ''),
    (None, 'default'),
    ('default', None),
    ('default', 'default'),
    ('foo', 'foo'),
    ('foo', 'bar'),
    ('foo', None),
    (None, 'foo'),
    ('default', 'foo'),
    ('foo', 'default'),
])
async def test_profiles_match_input_matrix(a, b):
    """mcp_server._profiles_match agrees with api.routes._profiles_match
    on every (row, active) pair across the visibility matrix.

    Note: function-object identity is checked separately in
    test_profiles_match_single_source_of_truth — here we only assert
    behavioral parity, which is robust to test-fixture re-imports that
    clear and re-execute api.profiles."""
    from mcp_server import _profiles_match as mcp_match
    from api.routes import _profiles_match as routes_match
    assert mcp_match(a, b) == routes_match(a, b)


# ═══════════════════════════════════════════════════════════════════════════
#  --profile CLI ordering regression
# ═══════════════════════════════════════════════════════════════════════════
#
# Maintainer ask: verify that --profile is applied to _active_profile *before*
# any api.models / api.profiles consumer reads the active profile. The risk
# is that if the canonical helpers cached the profile on first read at import
# time, a --profile foo flag passed at startup would bind too late.
#
# Today the helpers read _active_profile lazily (api/profiles.py:173 reads
# the module global at every call) so the override is safe. This test locks
# the behaviour: setting _active_profile = 'foo' before the first list call
# produces results filtered to 'foo', not the default.

class TestProfileCliOrdering:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.state_dir = _fresh_state_dir()
        self.mod, self.profiles = _reimport_mcp()
        yield
        _cleanup_state_dir(self.state_dir)

    async def test_active_profile_override_takes_effect_before_first_read(self):
        """--profile foo must filter list_projects to foo's rows immediately.

        Simulates the CLI override path (mcp_server.py:62-64 sets
        _profiles._active_profile = _profile_arg right after import). If a
        helper had latched the profile at import time, the override here
        would be too late and the test would see 'default'-tagged rows."""
        from api.config import PROJECTS_FILE
        # Pre-seed two projects: one for default, one for foo.
        seeded = [
            {"project_id": "p_default_0001", "name": "DefaultRow",
             "color": None, "profile": "default", "created_at": 1.0},
            {"project_id": "p_foo_0001", "name": "FooRow",
             "color": None, "profile": "foo", "created_at": 2.0},
        ]
        PROJECTS_FILE.write_text(json.dumps(seeded), encoding="utf-8")

        # Apply the override BEFORE the first list call. This is what
        # mcp_server.py:62-64 does after argparse.
        self.profiles._active_profile = 'foo'

        projects = await _call(self.mod, "list_projects")
        names = [p["name"] for p in projects]
        assert "FooRow" in names
        assert "DefaultRow" not in names


# ═══════════════════════════════════════════════════════════════════════════
#  HTTP wire-format coverage for rename_session / move_session
# ═══════════════════════════════════════════════════════════════════════════
#
# Maintainer ask: exercise the actual HTTP path so a typo in WEBUI_URL or in
# the request body shape can't slip through validation-only tests. We stand
# up a tiny http.server stub on a free localhost port, point WEBUI_URL at it,
# and capture (path, body) from the requests our handlers issue. This is
# the thing that would have caught the original 8788 vs 8787 mismatch.

import http.server
import socket
import threading


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Captures POST path + body, returns canned JSON. Class-level state is
    set by the fixture before each test so handlers can cross-reference."""
    captured = None  # populated per-test as a list of (path, body, headers)
    canned_response = None  # populated per-test: dict to be JSON-encoded

    def log_message(self, *args, **kwargs):  # noqa: D401 — silence stderr
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {"_raw": raw.decode("utf-8", errors="replace")}
        type(self).captured.append({
            "path": self.path,
            "body": body,
            "cookie": self.headers.get("Cookie"),
            "content_type": self.headers.get("Content-Type"),
        })
        payload = json.dumps(type(self).canned_response or {}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestApiWireFormat:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.state_dir = _fresh_state_dir()
        # Stand up a recording HTTP server on a free port. We override
        # WEBUI_URL on the imported mcp_server module to point at it.
        self.port = _free_port()
        _RecordingHandler.captured = []
        _RecordingHandler.canned_response = {}
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port),
                                            _RecordingHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

        # Disable auth so _api_post() does not attempt a real /api/auth/login.
        os.environ.pop("HERMES_WEBUI_PASSWORD", None)

        self.mod, self.profiles = _reimport_mcp()
        # Override AFTER import so the value sticks in the loaded module.
        self.mod.WEBUI_URL = f"http://127.0.0.1:{self.port}"
        yield
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        _cleanup_state_dir(self.state_dir)

    async def test_rename_session_posts_to_canonical_path(self):
        """rename_session must POST {session_id, title} to /api/session/rename."""
        _RecordingHandler.canned_response = {
            "session": {"session_id": "abc123", "title": "Renamed"}
        }
        result = await _call(self.mod, "rename_session",
                             session_id="abc123", title="Renamed")
        assert len(_RecordingHandler.captured) == 1
        req = _RecordingHandler.captured[0]
        assert req["path"] == "/api/session/rename"
        assert req["body"] == {"session_id": "abc123", "title": "Renamed"}
        assert req["content_type"] == "application/json"
        # Handler returns success-shaped result on 200.
        assert result["ok"] is True
        assert result["session_id"] == "abc123"
        assert result["title"] == "Renamed"
        assert result["method"] == "api"

    async def test_move_session_posts_to_canonical_path(self):
        """move_session (with a project_id) POSTs to /api/session/move
        after confirming the project exists locally."""
        # Need a real project so the pre-flight profile check passes.
        created = await _call(self.mod, "create_project", name="MoveTarget")
        pid = created["project_id"]
        _RecordingHandler.canned_response = {
            "ok": True,
            "session": {"session_id": "s1", "title": "T", "project_id": pid}
        }
        result = await _call(self.mod, "move_session",
                             session_id="s1", project_id=pid)
        assert len(_RecordingHandler.captured) == 1
        req = _RecordingHandler.captured[0]
        assert req["path"] == "/api/session/move"
        assert req["body"] == {"session_id": "s1", "project_id": pid}
        assert result["ok"] is True
        assert result["session_id"] == "s1"
        assert result["project_id"] == pid
        assert result["method"] == "api"

    async def test_move_session_unassign_sends_null_project_id(self):
        """Passing project_id=None must serialize as JSON null (not omitted)."""
        _RecordingHandler.canned_response = {
            "ok": True, "session": {"session_id": "s1", "project_id": None}
        }
        result = await _call(self.mod, "move_session",
                             session_id="s1", project_id=None)
        assert len(_RecordingHandler.captured) == 1
        req = _RecordingHandler.captured[0]
        assert req["path"] == "/api/session/move"
        assert req["body"] == {"session_id": "s1", "project_id": None}
        assert result["ok"] is True

    async def test_url_built_from_env_vars(self):
        """HERMES_WEBUI_HOST / HERMES_WEBUI_PORT govern WEBUI_URL.

        Locks the maintainer-suggested env-var contract from #1895 review:
        the MCP must track the same env vars api/config.py:32-33 reads, so
        a non-default WebUI port (e.g. 8788 when 8787 is held by another
        service on the host) does not require a code edit."""
        os.environ["HERMES_WEBUI_HOST"] = "10.0.0.42"
        os.environ["HERMES_WEBUI_PORT"] = "9999"
        try:
            mod, _ = _reimport_mcp()
            assert mod.WEBUI_HOST == "10.0.0.42"
            assert mod.WEBUI_PORT == "9999"
            assert mod.WEBUI_URL == "http://10.0.0.42:9999"
        finally:
            os.environ.pop("HERMES_WEBUI_HOST", None)
            os.environ.pop("HERMES_WEBUI_PORT", None)

    async def test_url_default_when_env_unset(self):
        """Default upstream port is 8787, matching api/config.py:33."""
        os.environ.pop("HERMES_WEBUI_HOST", None)
        os.environ.pop("HERMES_WEBUI_PORT", None)
        mod, _ = _reimport_mcp()
        assert mod.WEBUI_HOST == "127.0.0.1"
        assert mod.WEBUI_PORT == "8787"
        assert mod.WEBUI_URL == "http://127.0.0.1:8787"
