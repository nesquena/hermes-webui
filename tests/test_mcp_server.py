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
