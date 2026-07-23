"""Regression tests for #5457: project default workspace for new chats.

Covers:
- POST /api/projects/create: accepts and validates optional default_workspace
- POST /api/projects/rename: sets or clears default_workspace
- GET /api/projects?all_profiles=1: redacts foreign default_workspace values
- POST /api/session/new: uses project default workspace when project_id supplied
  and no explicit workspace provided
"""

import io
import json
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest


# ── Fake HTTP handler (mirrors test_auth_settings_safety.py pattern) ──────────

class _FakeHandler:
    def __init__(self, body_dict):
        raw = json.dumps(body_dict).encode("utf-8")
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(raw)
        self.headers = {"Content-Length": str(len(raw))}
        self.client_address = ("127.0.0.1", 0)
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _post(path, body_dict):
    from api.routes import handle_post
    handler = _FakeHandler(body_dict)
    parsed = urlparse(f"http://example.com{path}")
    handle_post(handler, parsed)
    return handler


def _get(path):
    from api.routes import handle_get

    handler = _FakeHandler({})
    parsed = urlparse(f"http://example.com{path}")
    handle_get(handler, parsed)
    return handler


# ── Shared fixtures ────────────────────────────────────────────────────────────

def _seed_project(projects_file, proj_id="proj1", name="TestProj", dw=None, profile="default"):
    p = {
        "project_id": proj_id,
        "name": name,
        "color": None,
        "profile": profile,
        "created_at": 1.0,
    }
    if dw is not None:
        p["default_workspace"] = dw
    projects_file.write_text(json.dumps([p]))


@pytest.fixture()
def project_env(tmp_path, monkeypatch):
    """Monkeypatch storage + profile so project CRUD routes work in isolation."""
    import api.config as cfg
    import api.models as models
    import api.profiles as profiles

    projects_file = tmp_path / "projects.json"
    projects_file.write_text("[]")
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "_PROJECTS_MIGRATION_LOCK", threading.Lock())
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [])
    profiles._invalidate_root_profile_cache()
    return projects_file


# ── Project create ─────────────────────────────────────────────────────────────

def test_project_create_stores_valid_default_workspace(project_env, monkeypatch, tmp_path):
    """A valid default_workspace on create is normalized and persisted."""
    import api.routes as routes

    ws_path = str(tmp_path / "myworkspace")
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda p: Path(str(p)))

    h = _post("/api/projects/create", {"name": "TestProj", "default_workspace": ws_path})
    assert h.status == 200, h.json_body()
    resp = h.json_body()
    assert resp["ok"] is True
    assert resp["project"]["default_workspace"] == ws_path

    saved = json.loads(project_env.read_text())
    assert saved[0]["default_workspace"] == ws_path


def test_project_create_omits_default_workspace_key_when_field_absent(project_env):
    """Legacy create with no default_workspace field leaves the key absent."""
    h = _post("/api/projects/create", {"name": "TestProj"})
    assert h.status == 200
    assert "default_workspace" not in h.json_body()["project"]

    saved = json.loads(project_env.read_text())
    assert "default_workspace" not in saved[0]


def test_project_create_rejects_invalid_default_workspace(project_env, monkeypatch):
    """An untrusted default_workspace returns 400 and nothing is persisted."""
    import api.routes as routes

    def _reject(p):
        raise ValueError("path not trusted")

    monkeypatch.setattr(routes, "resolve_trusted_workspace", _reject)

    h = _post("/api/projects/create", {"name": "TestProj", "default_workspace": "/bad/path"})
    assert h.status == 400
    assert "invalid default_workspace" in h.json_body().get("error", "")

    saved = json.loads(project_env.read_text())
    assert saved == [], "No project should be stored on validation failure"


# ── Project rename ─────────────────────────────────────────────────────────────

def test_project_rename_sets_default_workspace(project_env, monkeypatch, tmp_path):
    """Rename with a valid default_workspace stores the normalized path."""
    import api.routes as routes

    _seed_project(project_env)
    ws_path = str(tmp_path / "ws")
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda p: Path(str(p)))

    h = _post("/api/projects/rename", {
        "project_id": "proj1",
        "name": "TestProj",
        "default_workspace": ws_path,
    })
    assert h.status == 200, h.json_body()
    saved = json.loads(project_env.read_text())
    assert saved[0]["default_workspace"] == ws_path


def test_project_rename_clears_default_workspace_on_empty_string(project_env, monkeypatch, tmp_path):
    """Rename with default_workspace='' removes the key from the stored record."""
    ws_path = str(tmp_path / "ws")
    _seed_project(project_env, dw=ws_path)

    h = _post("/api/projects/rename", {
        "project_id": "proj1",
        "name": "TestProj",
        "default_workspace": "",
    })
    assert h.status == 200, h.json_body()
    saved = json.loads(project_env.read_text())
    assert "default_workspace" not in saved[0]


def test_project_rename_clears_default_workspace_on_null(project_env, monkeypatch, tmp_path):
    """Rename with default_workspace=null removes the key from the stored record."""
    ws_path = str(tmp_path / "ws")
    _seed_project(project_env, dw=ws_path)

    h = _post("/api/projects/rename", {
        "project_id": "proj1",
        "name": "TestProj",
        "default_workspace": None,
    })
    assert h.status == 200, h.json_body()
    saved = json.loads(project_env.read_text())
    assert "default_workspace" not in saved[0]


def test_project_rename_preserves_existing_default_when_field_absent(project_env, tmp_path):
    """Rename without the default_workspace field leaves the existing value untouched."""
    ws_path = str(tmp_path / "ws")
    _seed_project(project_env, dw=ws_path)

    h = _post("/api/projects/rename", {"project_id": "proj1", "name": "Renamed"})
    assert h.status == 200, h.json_body()
    saved = json.loads(project_env.read_text())
    assert saved[0]["default_workspace"] == ws_path


def test_project_rename_rejects_invalid_default_workspace(project_env, monkeypatch):
    """An invalid path on rename returns 400 and leaves stored record unchanged."""
    import api.routes as routes

    _seed_project(project_env)

    def _reject(p):
        raise ValueError("path not trusted")

    monkeypatch.setattr(routes, "resolve_trusted_workspace", _reject)

    h = _post("/api/projects/rename", {
        "project_id": "proj1",
        "name": "TestProj",
        "default_workspace": "/bad/path",
    })
    assert h.status == 400
    assert "invalid default_workspace" in h.json_body().get("error", "")


def test_projects_all_profiles_redacts_foreign_default_workspace(project_env):
    """Aggregate project rows keep foreign projects visible but hide their default workspace path."""
    project_env.write_text(json.dumps([
        {
            "project_id": "proj-active",
            "name": "Active",
            "color": None,
            "profile": "default",
            "created_at": 1.0,
            "default_workspace": "/active/workspace",
        },
        {
            "project_id": "proj-foreign",
            "name": "Foreign",
            "color": None,
            "profile": "other",
            "created_at": 2.0,
            "default_workspace": "/foreign/workspace",
        },
    ]))

    h = _get("/api/projects?all_profiles=1")
    assert h.status == 200, h.json_body()

    projects = {proj["project_id"]: proj for proj in h.json_body()["projects"]}
    assert projects["proj-active"]["default_workspace"] == "/active/workspace"
    assert "default_workspace" not in projects["proj-foreign"]


# ── Session new: project default workspace ─────────────────────────────────────

class _FakeSession:
    """Minimal session object accepted by the /api/session/new response path."""

    def __init__(self, workspace=None):
        self.messages = []
        self.workspace = workspace
        self.profile = "default"
        self.session_id = "s-fake-1"
        self.active_stream_id = None

    def compact(self):
        return {"session_id": self.session_id, "workspace": self.workspace}


def test_session_new_uses_project_default_workspace(project_env, monkeypatch, tmp_path):
    """Direct /api/session/new with project_id but no workspace uses the project's stored default."""
    import api.routes as routes

    ws_path = str(tmp_path / "projws")
    _seed_project(project_env, dw=ws_path)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda p: Path(str(p)))

    captured = []

    def fake_new_session(**kwargs):
        captured.append(kwargs)
        return _FakeSession(workspace=kwargs.get("workspace"))

    monkeypatch.setattr(routes, "new_session", fake_new_session)

    h = _post("/api/session/new", {"project_id": "proj1"})
    assert h.status == 200, h.json_body()
    assert len(captured) == 1, "new_session must be called once"
    assert captured[0]["workspace"] == ws_path, (
        f"Expected project default {ws_path!r}, got {captured[0]['workspace']!r}"
    )


def test_session_new_rejects_foreign_body_profile_project(project_env, monkeypatch, tmp_path):
    """SECURITY (#5510 Codex re-gate): a body-supplied profile that differs from the
    request-scoped active profile must NOT be trusted. A profile-`default` request
    that POSTs {"profile":"alice","project_id":"proj1"} must have project_id dropped
    and must NEVER receive alice's project default_workspace (profile-isolation)."""
    import api.routes as routes

    ws_path = str(tmp_path / "alice-project")
    _seed_project(project_env, dw=ws_path, profile="alice")
    # Authenticated/active profile is `default`; the attacker asks for alice's project.
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda p: Path(str(p)))

    captured = []

    def fake_new_session(**kwargs):
        captured.append(kwargs)
        return _FakeSession(workspace=kwargs.get("workspace"))

    monkeypatch.setattr(routes, "new_session", fake_new_session)

    h = _post("/api/session/new", {"project_id": "proj1", "profile": "alice"})
    assert h.status == 200, h.json_body()
    assert len(captured) == 1, "new_session must be called once"
    # The foreign project_id is dropped, and alice's workspace is never exposed.
    assert captured[0].get("project_id") in (None, ""), (
        f"foreign project_id must be dropped, got {captured[0].get('project_id')!r}"
    )
    assert captured[0]["workspace"] != ws_path, (
        f"alice's default_workspace {ws_path!r} must NOT leak to a default-profile request; "
        f"got {captured[0]['workspace']!r}"
    )


def test_session_new_uses_active_profile_project_default(project_env, monkeypatch, tmp_path):
    """A project owned by the ACTIVE profile still supplies its default workspace.
    (The legitimate path: the client switches the active profile first, so by the
    time /api/session/new runs the active profile is the project's owner.)"""
    import api.routes as routes

    ws_path = str(tmp_path / "alice-project")
    _seed_project(project_env, dw=ws_path, profile="alice")
    # Active profile IS alice (client already switched); body profile agrees.
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "alice")
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda p: Path(str(p)))

    captured = []

    def fake_new_session(**kwargs):
        captured.append(kwargs)
        return _FakeSession(workspace=kwargs.get("workspace"))

    monkeypatch.setattr(routes, "new_session", fake_new_session)

    h = _post("/api/session/new", {"project_id": "proj1", "profile": "alice"})
    assert h.status == 200, h.json_body()
    assert captured[0]["workspace"] == ws_path


def test_session_new_explicit_workspace_overrides_project_default(project_env, monkeypatch, tmp_path):
    """An explicit workspace in the body takes priority over the project's default."""
    import api.routes as routes

    ws_path = str(tmp_path / "projws")
    explicit_ws = str(tmp_path / "explicit")
    _seed_project(project_env, dw=ws_path)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda p: Path(str(p)))

    captured = []

    def fake_new_session(**kwargs):
        captured.append(kwargs)
        return _FakeSession(workspace=kwargs.get("workspace"))

    monkeypatch.setattr(routes, "new_session", fake_new_session)

    h = _post("/api/session/new", {"project_id": "proj1", "workspace": explicit_ws})
    assert h.status == 200, h.json_body()
    assert str(captured[0]["workspace"]) == explicit_ws, (
        f"Explicit workspace must win; got {captured[0]['workspace']!r}"
    )


def test_session_new_explicit_workspace_beats_stale_fallback_workspace(
    project_env, monkeypatch, tmp_path
):
    """A stale fallback must not be validated when explicit workspace already won."""
    import api.routes as routes

    explicit_ws = str(tmp_path / "explicit")
    stale_fallback = str(tmp_path / "stale-fallback")
    _seed_project(project_env, dw=str(tmp_path / "project-default"))

    def fake_resolve(path):
        path = str(path)
        if path == stale_fallback:
            raise ValueError("stale fallback")
        return Path(path)

    monkeypatch.setattr(routes, "resolve_trusted_workspace", fake_resolve)

    captured = []

    def fake_new_session(**kwargs):
        captured.append(kwargs)
        return _FakeSession(workspace=kwargs.get("workspace"))

    monkeypatch.setattr(routes, "new_session", fake_new_session)

    h = _post("/api/session/new", {
        "project_id": "proj1",
        "workspace": explicit_ws,
        "fallback_workspace": stale_fallback,
    })
    assert h.status == 200, h.json_body()
    assert str(captured[0]["workspace"]) == explicit_ws


def test_session_new_valid_project_default_beats_valid_fallback_workspace(
    project_env, monkeypatch, tmp_path
):
    """A valid project default must still outrank a valid lower-priority fallback."""
    import api.routes as routes

    project_ws = str(tmp_path / "project-default")
    fallback_ws = str(tmp_path / "profile-fallback")
    _seed_project(project_env, dw=project_ws)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda p: Path(str(p)))

    captured = []

    def fake_new_session(**kwargs):
        captured.append(kwargs)
        return _FakeSession(workspace=kwargs.get("workspace"))

    monkeypatch.setattr(routes, "new_session", fake_new_session)

    h = _post("/api/session/new", {
        "project_id": "proj1",
        "fallback_workspace": fallback_ws,
    })
    assert h.status == 200, h.json_body()
    assert captured[0]["workspace"] == project_ws


def test_session_new_valid_project_default_beats_stale_fallback_workspace(
    project_env, monkeypatch, tmp_path
):
    """A valid project default must win before a stale lower-priority fallback is validated."""
    import api.routes as routes

    project_ws = str(tmp_path / "project-default")
    stale_fallback = str(tmp_path / "stale-fallback")
    _seed_project(project_env, dw=project_ws)

    def fake_resolve(path):
        path = str(path)
        if path == stale_fallback:
            raise ValueError("stale fallback")
        return Path(path)

    monkeypatch.setattr(routes, "resolve_trusted_workspace", fake_resolve)

    captured = []

    def fake_new_session(**kwargs):
        captured.append(kwargs)
        return _FakeSession(workspace=kwargs.get("workspace"))

    monkeypatch.setattr(routes, "new_session", fake_new_session)

    h = _post("/api/session/new", {
        "project_id": "proj1",
        "fallback_workspace": stale_fallback,
    })
    assert h.status == 200, h.json_body()
    assert captured[0]["workspace"] == project_ws
    assert captured[0]["project_id"] == "proj1"


def test_session_new_stale_fallback_workspace_is_rejected_when_it_wins(
    project_env, monkeypatch, tmp_path
):
    """A stale fallback must still raise once it becomes the chosen workspace."""
    import api.routes as routes

    stale_fallback = str(tmp_path / "stale-fallback")

    def fake_resolve(path):
        path = str(path)
        if path == stale_fallback:
            raise ValueError("stale fallback")
        return Path(path)

    monkeypatch.setattr(routes, "resolve_trusted_workspace", fake_resolve)

    h = _post("/api/session/new", {
        "fallback_workspace": stale_fallback,
    })
    assert h.status == 400
    assert h.json_body()["error"] == "stale fallback"


def test_session_new_stale_project_default_falls_back_to_fallback_workspace(
    project_env, monkeypatch, tmp_path
):
    """An invalid stored project default must fall through to fallback_workspace instead of 400ing."""
    import api.routes as routes

    stale_path = str(tmp_path / "missing-project-ws")
    fallback_path = str(tmp_path / "profile-fallback")
    _seed_project(project_env, dw=stale_path)

    def fake_resolve(path):
        path = str(path)
        if path == stale_path:
            raise ValueError("stale workspace")
        return Path(path)

    monkeypatch.setattr(routes, "resolve_trusted_workspace", fake_resolve)

    captured = []

    def fake_new_session(**kwargs):
        captured.append(kwargs)
        return _FakeSession(workspace=kwargs.get("workspace"))

    monkeypatch.setattr(routes, "new_session", fake_new_session)

    h = _post("/api/session/new", {
        "project_id": "proj1",
        "fallback_workspace": fallback_path,
    })
    assert h.status == 200, h.json_body()
    assert captured[0]["workspace"] == fallback_path
    assert captured[0]["project_id"] == "proj1"


def test_session_new_foreign_project_id_fails_closed_to_fallback_workspace(
    project_env, monkeypatch, tmp_path
):
    """A foreign project_id must not survive into session creation and must fall back cleanly."""
    import api.routes as routes

    fallback_path = str(tmp_path / "profile-fallback")
    _seed_project(project_env, dw=str(tmp_path / "foreign-project"), profile="other")
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda p: Path(str(p)))

    captured = []

    def fake_new_session(**kwargs):
        captured.append(kwargs)
        return _FakeSession(workspace=kwargs.get("workspace"))

    monkeypatch.setattr(routes, "new_session", fake_new_session)

    h = _post("/api/session/new", {
        "project_id": "proj1",
        "fallback_workspace": fallback_path,
    })
    assert h.status == 200, h.json_body()
    assert captured[0]["project_id"] is None
    assert captured[0]["workspace"] == fallback_path


def test_session_new_no_project_no_workspace_keeps_none(project_env, monkeypatch):
    """Without project_id or workspace the session gets workspace=None (existing behavior)."""
    import api.routes as routes

    captured = []

    def fake_new_session(**kwargs):
        captured.append(kwargs)
        return _FakeSession()

    monkeypatch.setattr(routes, "new_session", fake_new_session)

    h = _post("/api/session/new", {})
    assert h.status == 200, h.json_body()
    assert captured[0]["workspace"] is None


def test_session_new_project_without_default_workspace_does_not_set_workspace(
    project_env, monkeypatch
):
    """A project with no default_workspace leaves session workspace as None."""
    import api.routes as routes

    _seed_project(project_env)  # no dw

    captured = []

    def fake_new_session(**kwargs):
        captured.append(kwargs)
        return _FakeSession()

    monkeypatch.setattr(routes, "new_session", fake_new_session)

    h = _post("/api/session/new", {"project_id": "proj1"})
    assert h.status == 200, h.json_body()
    assert captured[0]["workspace"] is None
