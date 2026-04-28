import importlib
import io
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest


def _load_spaces(monkeypatch, tmp_path, enabled=True):
    import api.config as config
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    if enabled:
        monkeypatch.setenv("HERMES_WEBUI_SPACES_ENABLED", "1")
    else:
        monkeypatch.delenv("HERMES_WEBUI_SPACES_ENABLED", raising=False)
    import api.spaces as spaces
    return importlib.reload(spaces)


def test_spaces_feature_flag_disabled_is_safe(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=False)

    assert spaces.spaces_enabled() is False
    assert spaces.list_spaces() == []
    recovery = spaces.recovery_snapshot()
    assert recovery["enabled"] is False
    assert recovery["generated_widgets_rendered"] is False


def test_create_read_list_space_with_schema_version_and_revision_event(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    created = spaces.create_space({"name": "Research Harness", "description": "First safe space"})

    assert created["schema_version"] == 1
    assert created["space_id"] == "research-harness"
    assert created["name"] == "Research Harness"
    assert created["widgets"] == []
    assert created["revision_event_id"]

    loaded = spaces.read_space("research-harness")
    assert loaded["space_id"] == created["space_id"]
    assert spaces.list_spaces()[0]["space_id"] == "research-harness"

    event_path = spaces.events_dir() / f"{created['revision_event_id']}.json"
    assert event_path.exists()
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["event_type"] == "space.created"
    assert event["space_id"] == "research-harness"


def test_space_id_validation_rejects_traversal_and_unsafe_names(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    for bad_id in ["../escape", "bad/id", ".hidden", "", "a" * 80]:
        with pytest.raises(ValueError):
            spaces.read_space(bad_id)

    with pytest.raises(ValueError):
        spaces.create_space({"space_id": "../escape", "name": "Escape"})


def test_update_space_creates_new_revision_event_and_preserves_widget_specs(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Demo"})

    updated = spaces.update_space(
        created["space_id"],
        {
            "name": "Demo Updated",
            "widgets": [
                {
                    "id": "widget-1",
                    "kind": "markdown",
                    "title": "Unsafe renderer should not appear in recovery",
                    "renderer": "<script>alert('bad')</script>",
                }
            ],
        },
    )

    assert updated["name"] == "Demo Updated"
    assert updated["revision_event_id"] != created["revision_event_id"]
    assert (spaces.events_dir() / f"{updated['revision_event_id']}.json").exists()
    assert spaces.read_space(created["space_id"])["widgets"][0]["id"] == "widget-1"


def test_list_revision_events_returns_safe_metadata_newest_first(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Revision Lab"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "weather",
            "kind": "markdown",
            "title": "Weather",
            "renderer": "<script>doNotExpose()</script>",
            "data": {"api_key": "SECRET...LEAK"},
        },
    )
    updated = spaces.update_space(created["space_id"], {"description": "Ready for rollback UI"})

    revisions = spaces.list_revision_events(created["space_id"])

    assert [event["event_type"] for event in revisions] == ["space.updated", "widget.created", "space.created"]
    assert revisions[0]["event_id"] == updated["revision_event_id"]
    assert revisions[0]["space_id"] == created["space_id"]
    assert revisions[0]["details"] == {"fields": ["description"]}
    serialized = json.dumps(revisions).lower()
    assert "donotexpose" not in serialized
    assert "<script" not in serialized
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_recovery_snapshot_never_returns_generated_widget_renderers(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Broken Widgets"})
    spaces.update_space(
        created["space_id"],
        {"widgets": [{"id": "w1", "renderer": "<script>breakUI()</script>", "html": "<b>bad</b>"}]},
    )

    recovery = spaces.recovery_snapshot()
    serialized = json.dumps(recovery)

    assert recovery["enabled"] is True
    assert recovery["generated_widgets_rendered"] is False
    assert recovery["spaces"][0]["widget_count"] == 1
    assert "breakUI" not in serialized
    assert "<script" not in serialized
    assert "renderer" not in serialized


def test_delete_space_removes_manifest_but_keeps_global_revision_event(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Disposable"})

    result = spaces.delete_space(created["space_id"])

    assert result["deleted"] is True
    assert result["space_id"] == created["space_id"]
    assert result["revision_event_id"]
    assert (spaces.events_dir() / f"{result['revision_event_id']}.json").exists()
    with pytest.raises(FileNotFoundError):
        spaces.read_space(created["space_id"])


def test_session_active_space_id_is_optional_and_persists(monkeypatch, tmp_path):
    import api.config as config
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / "sessions")
    config.SESSION_DIR.mkdir(parents=True, exist_ok=True)

    import api.models as models
    monkeypatch.setattr(models, "SESSION_DIR", config.SESSION_DIR)
    models.SESSIONS.clear()

    legacy = models.Session(session_id="legacy_session", workspace=str(tmp_path))
    assert legacy.active_space_id is None
    assert "active_space_id" in legacy.compact()

    legacy.active_space_id = "research-harness"
    legacy.save(skip_index=True)
    loaded = models.Session.load("legacy_session")

    assert loaded.active_space_id == "research-harness"
    assert loaded.compact()["active_space_id"] == "research-harness"


def test_spaces_routes_and_static_shell_are_registered():
    repo = Path(__file__).resolve().parents[1]
    routes_src = (repo / "api" / "routes.py").read_text(encoding="utf-8")
    index_html = (repo / "static" / "index.html").read_text(encoding="utf-8")

    assert '"/api/spaces"' in routes_src
    assert '"/api/spaces/recovery"' in routes_src
    assert '"/api/spaces/revisions"' in routes_src
    assert '"/api/spaces/create"' in routes_src
    assert 'static/spaces.js' in index_html
    assert 'static/spaces.css' in index_html
    assert 'id="mainCapySpaces"' in index_html
    assert 'id="capySpacesRecovery"' in index_html


class _RouteHandler:
    def __init__(self, body=None):
        raw = json.dumps(body or {}).encode("utf-8")
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.headers = {
            "Content-Length": str(len(raw)),
            "Accept-Encoding": "",
            "Host": "127.0.0.1:8787",
        }
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _route_get(path):
    import api.routes as routes

    handler = _RouteHandler()
    handled = routes.handle_get(handler, urlparse(path))
    return handled, handler.status, handler.json_body()


def _route_post(path, body):
    import api.routes as routes

    handler = _RouteHandler(body)
    handled = routes.handle_post(handler, urlparse(path))
    return handled, handler.status, handler.json_body()


def test_spaces_routes_create_list_get_and_recovery(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/create", {"name": "Route Space"})
    assert handled is None
    assert status == 200
    space_id = body["space"]["space_id"]

    handled, status, body = _route_get("/api/spaces")
    assert handled is None
    assert status == 200
    assert body["enabled"] is True
    assert body["spaces"][0]["space_id"] == space_id

    handled, status, body = _route_get(f"/api/spaces/get?space_id={space_id}")
    assert handled is None
    assert status == 200
    assert body["space"]["name"] == "Route Space"

    handled, status, body = _route_get(f"/api/spaces/revisions?space_id={space_id}")
    assert handled is None
    assert status == 200
    assert body["revisions"][0]["event_type"] == "space.created"
    assert body["revisions"][0]["space_id"] == space_id

    handled, status, body = _route_get("/api/spaces/recovery")
    assert handled is None
    assert status == 200
    assert body["generated_widgets_rendered"] is False


def test_spaces_get_route_returns_metadata_only_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Safe Detail"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "weather",
            "kind": "markdown",
            "title": "Weather",
            "layout": {"x": 2, "y": 3, "w": 8, "h": 5},
            "renderer": "<script>secret()</script>",
            "html": "<img src=x onerror=stealSecret()>",
            "data": {"api_key": "SECRET_VALUE_DO_NOT_LEAK"},
        },
    )

    handled, status, body = _route_get(f"/api/spaces/get?space_id={created['space_id']}")

    assert handled is None
    assert status == 200
    assert body["space"]["widgets"] == [
        {"id": "weather", "kind": "markdown", "title": "Weather", "layout": {"x": 2, "y": 3, "w": 8, "h": 5, "minimized": False}}
    ]
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "data" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "stealsecret" not in serialized
    assert "<script" not in serialized


def test_disabled_spaces_get_route_does_not_return_manifest_details(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Disabled Leak"})
    spaces.update_space(
        created["space_id"],
        {"widgets": [{"id": "w1", "renderer": "<script>secret()</script>"}]},
    )
    _load_spaces(monkeypatch, tmp_path, enabled=False)

    handled, status, body = _route_get(f"/api/spaces/get?space_id={created['space_id']}")

    assert handled is None
    assert status == 403
    assert "space" not in body
    assert "secret" not in json.dumps(body)


def test_disabled_spaces_activate_route_does_not_attach_space_to_session(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Disabled Activate"})

    import api.config as config
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / "sessions")
    config.SESSION_DIR.mkdir(parents=True, exist_ok=True)

    import api.models as models
    monkeypatch.setattr(models, "SESSION_DIR", config.SESSION_DIR)
    models.SESSIONS.clear()
    session = models.Session(session_id="session-activate", workspace=str(tmp_path))
    session.save(skip_index=True)

    _load_spaces(monkeypatch, tmp_path, enabled=False)

    handled, status, body = _route_post(
        "/api/spaces/activate",
        {"space_id": created["space_id"], "session_id": "session-activate"},
    )

    assert handled is None
    assert status == 403
    assert created["space_id"] not in json.dumps(body)
    assert session.active_space_id is None


def test_widget_upsert_list_read_and_delete_are_revisioned(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Widget Lab"})

    upserted = spaces.upsert_widget(
        created["space_id"],
        {
            "id": "weather",
            "kind": "markdown",
            "title": "Weather",
            "renderer": "# {{temperature}}",
            "data": {"temperature": "72F"},
        },
    )

    assert upserted["space_id"] == created["space_id"]
    assert upserted["widget"]["id"] == "weather"
    assert upserted["revision_event_id"] != created["revision_event_id"]
    assert (spaces.events_dir() / f"{upserted['revision_event_id']}.json").exists()

    widgets = spaces.list_widgets(created["space_id"])
    assert widgets == [
        {"id": "weather", "kind": "markdown", "title": "Weather", "layout": {"x": 0, "y": 0, "w": 6, "h": 4, "minimized": False}}
    ]
    assert "renderer" not in json.dumps(widgets)

    full = spaces.read_widget(created["space_id"], "weather")
    assert full["renderer"] == "# {{temperature}}"

    deleted = spaces.delete_widget(created["space_id"], "weather")
    assert deleted["deleted"] is True
    assert deleted["revision_event_id"] != upserted["revision_event_id"]
    assert spaces.list_widgets(created["space_id"]) == []


def test_widget_validation_rejects_pathlike_ids_and_non_object_specs(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Widget Validation"})

    for bad_id in ["../escape", "bad/id", ".hidden", "", "a" * 80]:
        with pytest.raises(ValueError):
            spaces.upsert_widget(created["space_id"], {"id": bad_id, "kind": "markdown"})

    with pytest.raises(ValueError):
        spaces.upsert_widget(created["space_id"], ["not", "a", "dict"])


def test_widget_layout_is_normalized_for_canvas_metadata(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Layout Lab"})

    upserted = spaces.upsert_widget(
        created["space_id"],
        {
            "id": "chart",
            "kind": "chart",
            "title": "Chart",
            "layout": {"x": "12", "y": -9, "w": 99, "h": 0, "minimized": "yes", "renderer": "<script>bad()</script>"},
            "renderer": "<script>doNotExpose()</script>",
        },
    )

    assert upserted["widget"]["layout"] == {"x": 12, "y": 0, "w": 24, "h": 1, "minimized": True}
    listed = spaces.list_widgets(created["space_id"])
    assert listed == [
        {"id": "chart", "kind": "chart", "title": "Chart", "layout": {"x": 12, "y": 0, "w": 24, "h": 1, "minimized": True}}
    ]
    assert "renderer" not in json.dumps(listed)


def test_widget_routes_upsert_list_read_and_delete(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Route Widgets"})
    space_id = created["space_id"]

    handled, status, body = _route_post(
        "/api/spaces/widget/upsert",
        {"space_id": space_id, "widget": {"id": "notes", "kind": "markdown", "title": "Notes"}},
    )
    assert handled is None
    assert status == 200
    assert body["widget"]["id"] == "notes"

    handled, status, body = _route_get(f"/api/spaces/widgets?space_id={space_id}")
    assert handled is None
    assert status == 200
    assert body["widgets"] == [
        {"id": "notes", "kind": "markdown", "title": "Notes", "layout": {"x": 0, "y": 0, "w": 6, "h": 4, "minimized": False}}
    ]

    handled, status, body = _route_get(f"/api/spaces/widget?space_id={space_id}&widget_id=notes")
    assert handled is None
    assert status == 200
    assert body["widget"]["title"] == "Notes"

    handled, status, body = _route_post(
        "/api/spaces/widget/delete",
        {"space_id": space_id, "widget_id": "notes"},
    )
    assert handled is None
    assert status == 200
    assert body["deleted"] is True
    assert spaces.list_widgets(space_id) == []


def test_widget_event_queues_agent_bridge_request_without_widget_bodies_or_secret_values(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Research Harness"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "research-form",
            "kind": "form",
            "title": "Research Form",
            "renderer": "<script>doNotPersistIntoEvent()</script>",
            "html": "<button onclick=steal()>Run</button>",
        },
    )

    queued = spaces.queue_widget_event(
        created["space_id"],
        "research-form",
        "agent.prompt",
        {
            "query": "Summarize Claude Mythos",
            "renderer": "<script>leak()</script>",
            "api_key": "SECRET_VALUE_DO_NOT_LEAK",
            "nested": {"password": "also-secret", "safe": "ok"},
        },
        prompt="Research this topic and update the widgets.",
        session_id="session-123",
    )

    assert queued["queued"] is True
    assert queued["event_id"]
    assert queued["space_id"] == created["space_id"]
    assert queued["widget_id"] == "research-form"
    assert queued["event_name"] == "agent.prompt"
    assert queued["payload_summary"]["query"] == "Summarize Claude Mythos"
    serialized = json.dumps(queued)
    assert "SECRET_VALUE_DO_NOT_LEAK" not in serialized
    assert "also-secret" not in serialized
    assert "doNotPersistIntoEvent" not in serialized
    assert "<script" not in serialized
    assert "renderer" not in serialized

    event = json.loads((spaces.events_dir() / f"{queued['event_id']}.json").read_text(encoding="utf-8"))
    assert event["event_type"] == "widget.event.queued"
    assert event["details"]["widget_id"] == "research-form"
    assert event["details"]["session_id"] == "session-123"
    assert "SECRET_VALUE_DO_NOT_LEAK" not in json.dumps(event)


def test_widget_event_route_validates_widget_and_returns_queued_metadata(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Route Events"})
    spaces.upsert_widget(created["space_id"], {"id": "run", "kind": "button", "title": "Run"})

    handled, status, body = _route_post(
        "/api/spaces/widget/event",
        {
            "space_id": created["space_id"],
            "widget_id": "run",
            "event_name": "agent.prompt",
            "prompt": "Refresh this widget",
            "payload": {"unsafe_html": "<img src=x onerror=bad()>", "q": "weather"},
        },
    )
    assert handled is None
    assert status == 200
    assert body["queued"] is True
    assert body["widget_id"] == "run"
    assert body["event_name"] == "agent.prompt"
    assert body["payload_summary"]["q"] == "weather"
    assert "onerror" not in json.dumps(body)

    handled, status, body = _route_post(
        "/api/spaces/widget/event",
        {"space_id": created["space_id"], "widget_id": "missing", "event_name": "agent.prompt"},
    )
    assert handled is None
    assert status == 404


def test_active_space_context_is_compact_and_omits_widget_bodies(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space(
        {
            "space_id": "lab",
            "name": "Research Lab",
            "description": "Demo space",
            "agent_instructions": "Prefer small widget patches and preserve rollback points.",
        }
    )
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "weather",
            "kind": "markdown",
            "title": "Weather",
            "renderer": "<script>renderSecret()</script>",
            "html": "<img src=x onerror=stealSecret()>",
            "data": {"api_key": "SECRET_VALUE_DO_NOT_LEAK"},
        },
    )

    context = spaces.build_agent_context("lab")

    assert "## Active Capy Space" in context
    assert "id: lab" in context
    assert "name: Research Lab" in context
    assert "Prefer small widget patches" in context
    assert "weather|Weather|markdown" in context
    assert "Use Capy space APIs/tools for mutations" in context
    serialized = context.lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "data" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "rendersecret" not in serialized
    assert "stealsecret" not in serialized


def test_streaming_agent_prompt_includes_active_space_context(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space(
        {
            "space_id": "lab",
            "name": "Research Lab",
            "agent_instructions": "Use widget IDs and read before patching.",
        }
    )
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "sources",
            "kind": "table",
            "title": "Sources",
            "renderer": "<script>doNotExpose()</script>",
        },
    )
    from types import SimpleNamespace
    from api.streaming import _build_agent_prompt_inputs

    session = SimpleNamespace(workspace=str(tmp_path), active_space_id="lab")
    user_message, system_message = _build_agent_prompt_inputs(session, "Update the source list")

    assert user_message.startswith(f"[Workspace: {tmp_path}]")
    assert "[Capy Space: lab]" in user_message
    assert "Update the source list" in user_message
    assert "## Active Capy Space" in system_message
    assert "sources|Sources|table" in system_message
    assert "Use widget IDs and read before patching." in system_message
    assert "doNotExpose" not in system_message
    assert "renderer" not in system_message
