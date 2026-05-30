import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import api.models as models
import api.routes as routes
import api.runtime_contract as runtime_contract
from api.models import Session
from api.runtime_contract import (
    PROJECT_NARROW_SESSION_MODE,
    build_project_narrow_runtime_contract,
    clamp_workspace_to_contract,
)
from tests._pytest_port import BASE


def _get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as response:
            return json.loads(response.read()), response.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read()), exc.code


def _post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read()), response.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read()), exc.code


def _narrow_session(root: Path, *, profile: str = "default", workspace: Path | None = None):
    root = root.resolve()
    return SimpleNamespace(
        session_id="sess-narrow",
        profile=profile,
        workspace=str((workspace or root).resolve()),
        session_mode=PROJECT_NARROW_SESSION_MODE,
        runtime_contract={
            "workspace_root": str(root),
            "profile_policy": "pinned",
        },
        messages=[],
        context_messages=[],
        pending_user_message=None,
        model="gpt-test",
        model_provider=None,
    )


def test_project_narrow_contract_builder_pins_workspace_root(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_contract, "resolve_trusted_workspace", lambda path: Path(path).resolve())
    child = tmp_path / "repo"
    child.mkdir()
    contract = build_project_narrow_runtime_contract({}, workspace_root=child, profile="default")
    assert contract["workspace_root"] == str(child.resolve())
    assert contract["profile_policy"] == "pinned"


def test_project_narrow_workspace_clamps_implicit_stale_workspace_to_root(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_contract, "resolve_trusted_workspace", lambda path: Path(path).resolve())
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    session = _narrow_session(root, workspace=outside)
    assert clamp_workspace_to_contract(session, None, fallback_to_root=True) == str(root.resolve())


def test_project_narrow_workspace_rejects_explicit_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_contract, "resolve_trusted_workspace", lambda path: Path(path).resolve())
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    session = _narrow_session(root)
    try:
        clamp_workspace_to_contract(session, outside)
    except ValueError as exc:
        assert str(root.resolve()) in str(exc)
    else:
        raise AssertionError("expected explicit workspace escape to be rejected")


def test_session_save_load_round_trips_project_narrow_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "_write_session_index", lambda *args, **kwargs: None)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    session = Session(
        session_id="sessrt",
        workspace=str(workspace),
        profile="default",
        session_mode=PROJECT_NARROW_SESSION_MODE,
        runtime_contract={
            "workspace_root": str(workspace.resolve()),
            "profile_policy": "pinned",
        },
    )
    session.save(skip_index=True)
    loaded = Session.load("sessrt")
    assert loaded.session_mode == PROJECT_NARROW_SESSION_MODE
    assert loaded.runtime_contract["workspace_root"] == str(workspace.resolve())
    assert loaded.runtime_contract["profile_policy"] == "pinned"
    assert loaded.compact()["session_mode"] == PROJECT_NARROW_SESSION_MODE


def test_chat_start_rejects_profile_switch_for_project_narrow_session(monkeypatch, tmp_path):
    session = _narrow_session(tmp_path)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "bad", lambda _handler, msg, status=400: {"error": msg, "status": status})
    response = routes._handle_chat_start(object(), {
        "session_id": session.session_id,
        "message": "hello",
        "profile": "other-profile",
    })
    assert response["status"] == 400
    assert "pinned to 'default'" in response["error"]


def test_chat_sync_rejects_project_narrow_profile_switch(monkeypatch, tmp_path):
    session = _narrow_session(tmp_path)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "bad", lambda _handler, msg, status=400: {"error": msg, "status": status})
    response = routes._handle_chat_sync(object(), {
        "session_id": session.session_id,
        "message": "hello",
        "profile": "other-profile",
    })
    assert response["status"] == 400
    assert "pinned to 'default'" in response["error"]


def test_chat_sync_runs_under_pinned_project_profile_home(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "get_session", models.get_session)
    monkeypatch.setattr(routes, "title_from", models.title_from)
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: Path(value).resolve())
    monkeypatch.setattr(runtime_contract, "resolve_trusted_workspace", lambda value: Path(value).resolve())
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])

    root = tmp_path / "repo"
    root.mkdir()
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    session = Session(
        session_id="syncnarrow",
        workspace=str(root),
        profile="project-profile",
        session_mode=PROJECT_NARROW_SESSION_MODE,
        runtime_contract={
            "workspace_root": str(root.resolve()),
            "profile_policy": "pinned",
        },
        model="test-model",
        model_provider="test-provider",
    )
    session.save(touch_updated_at=False)

    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda _name: profile_home)
    monkeypatch.setattr("api.profiles.get_profile_runtime_env", lambda _home: {"PROFILE_SENTINEL": "expected"})
    monkeypatch.setattr("api.config.resolve_model_provider", lambda _model: ("test-model", "test-provider", None))
    monkeypatch.setattr("api.config.resolve_custom_provider_connection", lambda _provider: (None, None))
    monkeypatch.setattr(
        "api.oauth.resolve_runtime_provider_with_anthropic_env_lock",
        lambda _fn, requested=None: {"provider": requested, "api_key": None, "base_url": None},
    )

    seen = {}

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **_kwargs):
            seen["cwd"] = os.environ.get("TERMINAL_CWD")
            seen["home"] = os.environ.get("HERMES_HOME")
            seen["sentinel"] = os.environ.get("PROFILE_SENTINEL")
            return {
                "messages": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "ok"},
                ],
                "final_response": "ok",
                "completed": True,
            }

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    previous_home = os.environ.get("HERMES_HOME")

    class _FakeHandler:
        def __init__(self):
            self.status = None
            self.headers = {}
            self.body = bytearray()

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.headers[name] = value

        def end_headers(self):
            pass

        @property
        def wfile(self):
            return self

        def write(self, data):
            self.body.extend(data)

    handler = _FakeHandler()
    routes._handle_chat_sync(handler, {"session_id": session.session_id, "message": "hello"})

    assert handler.status == 200
    assert seen == {
        "cwd": str(root.resolve()),
        "home": str(profile_home),
        "sentinel": "expected",
    }
    assert os.environ.get("HERMES_HOME") == previous_home


def test_streaming_bootstrap_revalidates_project_narrow_contract_before_env_setup():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    clamp_idx = src.index("s.workspace = clamp_workspace_to_contract(s, workspace, fallback_to_root=True)")
    env_idx = src.index("_thread_env = _build_agent_thread_env(")
    profile_idx = src.index("enforce_pinned_profile(s, getattr(s, 'profile', None))")
    assert profile_idx < clamp_idx < env_idx


def test_skills_ui_requests_are_session_scoped():
    commands_src = Path("static/commands.js").read_text(encoding="utf-8")
    panels_src = Path("static/panels.js").read_text(encoding="utf-8")
    assert "_sessionScopedSkillsApiPath('/api/skills')" in commands_src
    assert "session_id=${encodeURIComponent(sid)}" in commands_src
    assert "_sessionScopedSkillsPath('/api/skills')" in panels_src
    assert "_sessionScopedSkillsPath(`/api/skills/content?name=${encodeURIComponent(name)}`)" in panels_src


def test_session_toolsets_route_rejects_project_narrow_widening(cleanup_test_sessions):
    all_skills, status = _get("/api/skills")
    assert status == 200
    skills = all_skills.get("skills", [])
    if len(skills) < 1:
        import pytest

        pytest.skip("No skills visible on test server")
    body = {
        "profile": "default",
        "session_mode": PROJECT_NARROW_SESSION_MODE,
        "runtime_contract": {
            "toolsets": ["file", "terminal"],
            "allowed_skills": [skills[0]["name"]],
        },
    }
    created, status = _post("/api/session/new", body)
    assert status == 200, created
    sid = created["session"]["session_id"]
    cleanup_test_sessions.append(sid)

    result, status = _post("/api/session/toolsets", {"session_id": sid, "toolsets": ["file", "browser"]})
    assert status == 400
    assert "persisted allowlist" in result.get("error", "")


def test_notes_sources_route_uses_project_narrow_session_scope(monkeypatch, tmp_path):
    session = _narrow_session(tmp_path)
    session.runtime_contract = {
        "workspace_root": str(tmp_path.resolve()),
        "profile_policy": "pinned",
        "prefill_policy": "disabled",
        "allowed_note_sources": ["joplin"],
    }

    monkeypatch.setattr(routes, "get_config", lambda: {
        "webui_external_notes_sources": True,
        "mcp_servers": {"joplin": {"enabled": True}},
    })
    monkeypatch.setattr(routes, "_runtime_contract_session_from_query", lambda _query: session)
    monkeypatch.setattr(routes, "_mcp_runtime_status_by_name", lambda: {})
    monkeypatch.setattr(routes, "_mcp_tools_from_runtime_status", lambda _runtime, _summaries: [])
    monkeypatch.setattr(routes, "_mcp_tools_from_registry", lambda _summaries: [])
    expected_session = session
    monkeypatch.setattr(routes, "_joplin_recent_ai_notes", lambda limit=6, session=None: [] if session is expected_session else [{"id": "leak"}])

    class _FakeHandler:
        def __init__(self):
            self.status = None
            self.headers = {}
            self.body = bytearray()

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.headers[name] = value

        def end_headers(self):
            pass

        @property
        def wfile(self):
            return self

        def write(self, data):
            self.body.extend(data)

    handler = _FakeHandler()
    parsed = urllib.parse.urlparse("/api/notes/sources?session_id=sess-narrow")
    routes._handle_notes_sources_list(handler, parsed)

    payload = json.loads(handler.body.decode("utf-8"))
    assert handler.status == 200
    assert payload["recent_ai_notes"] == []



def test_skills_api_filters_allowed_skills_for_project_narrow_session(cleanup_test_sessions):
    unrestricted, status = _get("/api/skills")
    assert status == 200
    skills = unrestricted.get("skills", [])
    if len(skills) < 2:
        import pytest

        pytest.skip("Need at least two visible skills to prove allowlist filtering")
    allowed_name = skills[0]["name"]
    blocked_name = next(skill["name"] for skill in skills if skill["name"] != allowed_name)
    created, status = _post(
        "/api/session/new",
        {
            "profile": "default",
            "session_mode": PROJECT_NARROW_SESSION_MODE,
            "runtime_contract": {
                "toolsets": ["file"],
                "allowed_skills": [allowed_name],
            },
        },
    )
    assert status == 200, created
    sid = created["session"]["session_id"]
    cleanup_test_sessions.append(sid)

    filtered, status = _get(f"/api/skills?session_id={urllib.parse.quote(sid)}")
    assert status == 200
    filtered_names = {skill["name"] for skill in filtered.get("skills", [])}
    assert allowed_name in filtered_names
    assert blocked_name not in filtered_names

    allowed_detail, status = _get(
        f"/api/skills/content?session_id={urllib.parse.quote(sid)}&name={urllib.parse.quote(allowed_name)}"
    )
    assert status == 200
    assert allowed_detail.get("content")

    blocked_detail, status = _get(
        f"/api/skills/content?session_id={urllib.parse.quote(sid)}&name={urllib.parse.quote(blocked_name)}"
    )
    assert status == 403
    assert "not available in this session" in blocked_detail.get("error", "")
