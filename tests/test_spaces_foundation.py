import base64
import importlib
import io
import json
import zipfile
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


def test_space_tool_adapter_create_list_and_get_are_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    created = spaces.run_space_tool(
        "space.create",
        {
            "space_id": "tool-lab",
            "name": "Tool Lab",
            "description": "Created through Hermes tool adapter",
            "widgets": [
                {
                    "id": "unsafe",
                    "title": "Unsafe body",
                    "renderer": "<script>steal()</script>",
                    "api_key": "SECRET_VALUE_DO_NOT_LEAK",
                }
            ],
        },
    )

    assert created["ok"] is True
    assert created["action"] == "space.create"
    assert created["space"]["space_id"] == "tool-lab"
    assert created["space"]["widget_count"] == 0
    assert spaces.read_space("tool-lab")["widgets"] == []

    spaces.upsert_widget(
        "tool-lab",
        {
            "id": "unsafe",
            "kind": "html",
            "title": "Unsafe body",
            "renderer": "<script>steal()</script>",
            "data": {"api_key": "SECRET_VALUE_DO_NOT_LEAK"},
        },
    )

    listed = spaces.run_space_tool("space.list", {})
    loaded = spaces.run_space_tool("space.get", {"space_id": "tool-lab"})
    serialized = json.dumps({"listed": listed, "loaded": loaded}).lower()

    assert listed["ok"] is True
    assert listed["spaces"][0]["space_id"] == "tool-lab"
    assert loaded["space"]["widgets"][0]["id"] == "unsafe"
    assert "steal" not in serialized
    assert "<script" not in serialized
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "secret_value_do_not_leak" not in serialized


def test_space_tool_adapter_supports_source_style_current_and_spaces_aliases(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space(
        {
            "space_id": "source-style-lab",
            "name": "Source Style Lab",
            "description": "Expose Space Agent-style helper aliases safely",
        }
    )
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "unsafe-widget",
            "kind": "html",
            "title": "Unsafe Widget",
            "renderer": "<script>steal()</script>",
            "html": "<img src=x onerror=steal()>",
            "data": {"api_key": "***"},
        },
    )

    spaces_list = spaces.run_space_tool("space.spaces.list", {"renderer": "<script>ignore()</script>"})
    current_space = spaces.run_space_tool("space.current.get", {"active_space_id": created["space_id"]})
    current_widgets = spaces.run_space_tool("space.current.widgets", {"space_id": created["space_id"]})
    no_current = spaces.run_space_tool("space.current.get", {})
    serialized = json.dumps(
        {
            "spaces_list": spaces_list,
            "current_space": current_space,
            "current_widgets": current_widgets,
            "no_current": no_current,
        }
    ).lower()

    assert spaces_list["ok"] is True
    assert spaces_list["spaces"][0]["space_id"] == created["space_id"]
    assert current_space["space"]["space_id"] == created["space_id"]
    assert current_widgets["widgets"][0]["id"] == "unsafe-widget"
    assert no_current == {"ok": True, "action": "space.current.get", "active_space_id": None, "space": None}
    assert "steal" not in serialized
    assert "<script" not in serialized
    assert "onerror" not in serialized
    assert "renderer" not in serialized
    assert '"html":' not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_space_tool_adapter_supports_space_agent_widget_aliases_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"space_id": "widget-alias-lab", "name": "Widget Alias Lab"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "research-card",
            "kind": "markdown",
            "title": "Research <Card>",
            "layout": {"x": 2, "y": 3, "w": 6, "h": 4},
            "renderer": "<script>steal()</script>",
            "html": "<img src=x onerror=steal()>",
            "data": {"api_key": "SECRET...LEAK"},
        },
    )

    listed = spaces.run_space_tool("space.widget.list", {"space_id": created["space_id"]})
    read = spaces.run_space_tool(
        "space.widget.read",
        {"space_id": created["space_id"], "widget_id": "research-card", "renderer": "<script>ignore()</script>"},
    )
    patched = spaces.run_space_tool(
        "space.current.widget.patch",
        {
            "active_space_id": created["space_id"],
            "widget_id": "research-card",
            "patch": {
                "title": "Research Patched",
                "layout": {"x": 4, "y": 5, "w": 8, "h": 5},
                "renderer": "<script>bad()</script>",
                "data": {"api_key": "SHOULD_NOT_LEAK"},
            },
        },
    )
    queued = spaces.run_space_tool(
        "space.widget.event",
        {
            "space_id": created["space_id"],
            "widget_id": "research-card",
            "event_name": "agent.prompt",
            "prompt": "summarize this widget",
            "payload": {"query": "Claude Mythos", "renderer": "<script>bad()</script>", "api_key": "***"},
        },
    )
    event_list = spaces.run_space_tool(
        "space.widget.events",
        {"space_id": created["space_id"], "widget_id": "research-card", "limit": 5},
    )
    serialized = json.dumps({"listed": listed, "read": read, "patched": patched, "queued": queued, "event_list": event_list}).lower()

    assert listed["ok"] is True
    assert listed["action"] == "space.widget.list"
    assert listed["widgets"][0]["id"] == "research-card"
    assert read["ok"] is True
    assert read["widget"]["id"] == "research-card"
    assert patched["widget"]["title"] == "Research Patched"
    assert patched["widget"]["layout"] == {"x": 4, "y": 5, "w": 8, "h": 5, "minimized": False}
    assert queued["queued"] is True
    assert queued["payload_summary"] == {"query": "Claude Mythos"}
    assert event_list["ok"] is True
    assert event_list["action"] == "space.widget.events"
    assert event_list["events"][0]["widget_id"] == "research-card"
    assert event_list["events"][0]["event_name"] == "agent.prompt"
    assert event_list["events"][0]["payload_summary"] == {"query": "Claude Mythos"}
    assert "steal" not in serialized
    assert "<script" not in serialized
    assert "onerror" not in serialized
    assert "renderer" not in serialized
    assert '"html":' not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_widget_detail_exposes_typed_template_metadata_without_generated_or_secret_fields(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"space_id": "typed-detail-lab", "name": "Typed Detail Lab"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "dashboard-weather",
            "kind": "weather",
            "title": "Weather Detail",
            "weather": {"location": "Prague", "unit": "celsius", "api_key": "SECRET_VALUE_DO_NOT_LEAK"},
            "chart": {"series": ["NVDA", "AAPL"], "refresh": "agent-mediated"},
            "table": {"columns": ["title", "url", "notes"], "token": "SECRET_VALUE_DO_NOT_LEAK"},
            "notes": {"folders": ["Inbox"], "mode": "metadata-only"},
            "renderer": "<script>steal()</script>",
            "html": "<img src=x onerror=steal()>",
            "data": {"api_key": "SECRET_VALUE_DO_NOT_LEAK"},
        },
    )

    detail = spaces.read_widget_detail(created["space_id"], "dashboard-weather")
    serialized = json.dumps(detail).lower()

    assert detail["metadata"]["weather"] == {"location": "Prague", "unit": "celsius"}
    assert detail["metadata"]["chart"] == {"series": ["NVDA", "AAPL"], "refresh": "agent-mediated"}
    assert detail["metadata"]["table"] == {"columns": ["title", "url", "notes"]}
    assert detail["metadata"]["notes"] == {"folders": ["Inbox"], "mode": "metadata-only"}
    assert "steal" not in serialized
    assert "<script" not in serialized
    assert "onerror" not in serialized
    assert "renderer" not in serialized
    assert '"html":' not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized
    assert "secret_value_do_not_leak" not in serialized


def test_space_tool_adapter_exposes_metadata_only_current_context(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space(
        {
            "space_id": "context-lab",
            "name": "Context Lab",
            "description": "Agent prompt context bridge",
            "agent_instructions": "Patch widgets through typed APIs only.",
        }
    )
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "research-summary",
            "kind": "markdown",
            "title": "Summary",
            "renderer": "<script>steal()</script>",
            "data": {"api_key": "unsafe_marker_do_not_leak"},
        },
    )

    result = spaces.run_space_tool("space.current.context", {"active_space_id": created["space_id"]})
    no_current = spaces.run_space_tool("space.current.context", {})
    serialized = json.dumps(result).lower()

    assert result["ok"] is True
    assert result["action"] == "space.current.context"
    assert result["active_space_id"] == created["space_id"]
    assert result["context"].startswith("## Active Capy Space")
    assert "id: context-lab" in result["context"]
    assert "research-summary|Summary|markdown" in result["context"]
    assert "Patch widgets through typed APIs only." in result["context"]
    assert no_current == {"ok": True, "action": "space.current.context", "active_space_id": None, "context": ""}
    assert "steal" not in serialized
    assert "<script" not in serialized
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "unsafe_marker_do_not_leak" not in serialized


def test_space_tool_adapter_installs_templates_as_safe_metadata(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    result = spaces.run_space_tool(
        "space.template.install",
        {
            "template": "game",
            "space_id": "tool-game-demo",
            "renderer": "<script>steal()</script>",
            "api_key": "unsafe-value-marker",
            "widgets": [{"id": "unsafe", "html": "<img src=x onerror=steal()>"}],
        },
    )
    serialized = json.dumps(result).lower()

    assert result["ok"] is True
    assert result["action"] == "space.template.install"
    assert result["template"] == "game"
    assert result["space"]["space_id"] == "tool-game-demo"
    assert result["space"]["name"] == "Game Sandbox"
    assert [widget["id"] for widget in result["installed_widgets"]] == [
        "game-canvas",
        "game-controls",
        "game-repair-notes",
    ]
    assert "steal" not in serialized
    assert "<script" not in serialized
    assert "onerror" not in serialized
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "unsafe-value-marker" not in serialized
    assert "secret" not in serialized


def test_space_tool_adapter_resets_big_bang_onboarding_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    installed = spaces.install_template("big-bang", space_id="big-bang-onboarding")
    spaces.upsert_widget(
        installed["space"]["space_id"],
        {
            "id": "unsafe-extra",
            "kind": "html",
            "title": "Unsafe extra",
            "renderer": "<script>steal()</script>",
            "api_key": "unsafe-extra-value-marker",
        },
    )

    result = spaces.run_space_tool(
        "space.template.reset",
        {
            "template": "big-bang",
            "space_id": "big-bang-onboarding",
            "renderer": "<script>ignore()</script>",
            "api_key": "unsafe-reset-value-marker",
        },
    )
    serialized = json.dumps(result).lower()

    assert result["ok"] is True
    assert result["action"] == "space.template.reset"
    assert result["template"] == "big-bang"
    assert result["reset"] is True
    assert result["space"]["space_id"] == "big-bang-onboarding"
    assert result["space"]["name"] == "Big Bang Onboarding"
    assert [widget["id"] for widget in result["installed_widgets"]] == [
        "bigbang-welcome",
        "bigbang-demo-launcher",
        "bigbang-safety",
        "bigbang-next-steps",
    ]
    assert [widget["id"] for widget in spaces.list_widgets("big-bang-onboarding")] == [
        "bigbang-welcome",
        "bigbang-demo-launcher",
        "bigbang-safety",
        "bigbang-next-steps",
    ]
    assert "unsafe-extra" not in serialized
    assert "steal" not in serialized
    assert "<script" not in serialized
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "unsafe-reset-value-marker" not in serialized
    assert "unsafe-extra-value-marker" not in serialized
    assert "secret" not in serialized


def test_space_tool_adapter_imports_and_exports_space_agent_packages_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    imported = spaces.run_space_tool(
        "space.import",
        {
            "space_yaml": """
id: tool-import-demo
name: Tool Import Demo
description: Imported through the Hermes tool adapter
instructions: Patch through safe Capy APIs only.
""",
            "widgets": {
                "widgets/panel.yaml": """
id: unsafe-panel
title: Unsafe Panel
type: html
renderer: "<script>window.SECRET_VALUE_DO_NOT_LEAK=***</script>"
source: SECRET_SOURCE_VALUE_DO_NOT_LEAK
data:
  api_key: SECRET_VALUE_DO_NOT_LEAK
layout:
  x: 1
  y: 2
  w: 5
  h: 4
""",
            },
        },
    )
    exported = spaces.run_space_tool("space.export", {"space_id": "tool-import-demo", "format": "yaml"})
    serialized = json.dumps({"imported": imported, "exported": exported}).lower()

    assert imported["ok"] is True
    assert imported["action"] == "space.import"
    assert imported["source"] == "space-agent-yaml"
    assert imported["space"]["space_id"] == "tool-import-demo"
    assert imported["imported_widgets"] == [
        {"id": "unsafe-panel", "kind": "html", "title": "Unsafe Panel", "layout": {"x": 1, "y": 2, "w": 5, "h": 4, "minimized": False}}
    ]
    assert spaces.read_widget("tool-import-demo", "unsafe-panel")["recovery"]["disabled"] is True
    assert exported["ok"] is True
    assert exported["action"] == "space.export"
    assert exported["source"] == "capy-space"
    assert exported["format"] == "space-agent-yaml"
    assert "tool-import-demo" in exported["space_yaml"]
    assert sorted(exported["widgets"].keys()) == ["widgets/unsafe-panel.yaml"]
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "secret_source_value_do_not_leak" not in serialized


def test_space_tool_adapter_lists_and_restores_revisions_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"space_id": "tool-rollback", "name": "Tool Rollback"})
    original_event_id = created["revision_event_id"]
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "unsafe-card",
            "kind": "html",
            "title": "Unsafe Card",
            "renderer": "<script>steal()</script>",
            "data": {"api_key": "SECRET_VALUE_DO_NOT_LEAK"},
        },
    )
    spaces.update_space(created["space_id"], {"description": "Updated for restore"})

    listed = spaces.run_space_tool("space.revisions", {"space_id": created["space_id"], "limit": 5})
    restored = spaces.run_space_tool(
        "space.revision.restore",
        {"space_id": created["space_id"], "event_id": original_event_id, "renderer": "<script>ignore()</script>"},
    )
    serialized = json.dumps({"listed": listed, "restored": restored}).lower()

    assert listed["ok"] is True
    assert listed["action"] == "space.revisions"
    assert [event["event_type"] for event in listed["revisions"]][:3] == [
        "space.updated",
        "widget.created",
        "space.created",
    ]
    assert restored["ok"] is True
    assert restored["action"] == "space.revision.restore"
    assert restored["restored_event_id"] == original_event_id
    assert restored["space"]["space_id"] == created["space_id"]
    assert restored["space"]["widgets"] == []
    assert "steal" not in serialized
    assert "<script" not in serialized
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized
    assert "secret_value_do_not_leak" not in serialized


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


def test_revision_and_widget_event_summaries_redact_secret_looking_values(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Secret Revision Lab"})
    spaces.upsert_widget(created["space_id"], {"id": "run", "kind": "button", "title": "Run"})

    queued = spaces.queue_widget_event(
        created["space_id"],
        "run",
        "agent.prompt",
        {
            "safe_note": "refresh weather",
            "operator_note": "Authorization: Bearer SECRET_VALUE_DO_NOT_LEAK",
            "nested": {"safe": "ok", "comment": "token=SECRET_VALUE_DO_NOT_LEAK"},
            "items": ["ok", "api_key=SECRET_VALUE_DO_NOT_LEAK"],
        },
        prompt="Use token SECRET_VALUE_DO_NOT_LEAK to refresh the widget",
        session_id="session-123",
    )

    assert queued["payload_summary"]["safe_note"] == "refresh weather"
    assert queued["payload_summary"]["operator_note"] == "[REDACTED]"
    assert queued["payload_summary"]["nested"]["safe"] == "ok"
    assert queued["payload_summary"]["nested"]["comment"] == "[REDACTED]"
    assert queued["payload_summary"]["items"] == ["ok", "[REDACTED]"]
    assert queued["prompt_preview"] == "[REDACTED]"

    revisions = spaces.list_revision_events(created["space_id"])
    serialized = json.dumps({"queued": queued, "revisions": revisions})
    assert "SECRET_VALUE_DO_NOT_LEAK" not in serialized
    assert "Bearer" not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized.lower()


def test_restore_revision_reverts_to_safe_snapshot_without_leaking_sources(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Rollback Lab"})
    original = spaces.upsert_widget(
        created["space_id"],
        {
            "id": "weather",
            "kind": "html",
            "title": "Weather original",
            "renderer": "<script>keptButNeverReturned()</script>",
            "data": {"api_key": "SECRET...LEAK"},
        },
    )
    spaces.patch_widget(created["space_id"], "weather", {"title": "Weather patched"})

    restored = spaces.restore_revision(created["space_id"], original["revision_event_id"])

    assert restored["ok"] is True
    assert restored["restored_event_id"] == original["revision_event_id"]
    assert restored["space"]["widgets"] == [
        {
            "id": "weather",
            "kind": "html",
            "title": "Weather original",
            "layout": {"x": 0, "y": 0, "w": 6, "h": 4, "minimized": False},
        }
    ]
    stored = spaces.read_widget(created["space_id"], "weather")
    assert stored["renderer"] == "<script>keptButNeverReturned()</script>"

    revisions = spaces.list_revision_events(created["space_id"])
    assert revisions[0]["event_type"] == "space.restored"
    assert revisions[0]["details"] == {"restored_event_id": original["revision_event_id"]}
    serialized = json.dumps({"restored": restored, "revisions": revisions}).lower()
    assert "keptbutneverreturned" not in serialized
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


def test_recovery_disable_widget_marks_safe_metadata_without_deleting_or_leaking_bodies(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Recovery Disable"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "bad-widget",
            "kind": "html",
            "title": "Bad Widget",
            "renderer": "<script>breakNormalRoute()</script>",
            "html": "<img src=x onerror=stealSecret()>",
            "data": {"api_key": "SECRET...LEAK"},
        },
    )

    disabled = spaces.disable_widget_for_recovery(created["space_id"], "bad-widget", reason="render failure")

    assert disabled["disabled"] is True
    assert disabled["space_id"] == created["space_id"]
    assert disabled["widget_id"] == "bad-widget"
    assert disabled["revision_event_id"]
    stored = spaces.read_widget(created["space_id"], "bad-widget")
    assert stored["recovery"]["disabled"] is True
    assert stored["recovery"]["disabled_reason"] == "render failure"
    assert stored["renderer"] == "<script>breakNormalRoute()</script>"

    recovery = spaces.recovery_snapshot()
    serialized = json.dumps(recovery)
    assert recovery["spaces"][0]["widgets"] == [
        {
            "id": "bad-widget",
            "kind": "html",
            "title": "Bad Widget",
            "disabled": True,
            "disabled_reason": "render failure",
        }
    ]
    assert "breakNormalRoute" not in serialized
    assert "stealSecret" not in serialized
    assert "SECRET_VALUE_DO_NOT_LEAK" not in serialized
    assert "renderer" not in serialized
    assert "<img" not in serialized
    assert "onerror" not in serialized


def test_recovery_enable_widget_restores_safe_metadata_without_rendering_bodies(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Recovery Enable"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "bad-widget",
            "kind": "html",
            "title": "Bad Widget",
            "renderer": "<script>breakNormalRoute()</script>",
            "html": "<img src=x onerror=stealSecret()>",
            "data": {"api_key": "***"},
        },
    )
    spaces.disable_widget_for_recovery(created["space_id"], "bad-widget", reason="render failure")

    enabled = spaces.enable_widget_for_recovery(created["space_id"], "bad-widget")

    assert enabled["disabled"] is False
    assert enabled["space_id"] == created["space_id"]
    assert enabled["widget_id"] == "bad-widget"
    assert enabled["revision_event_id"]
    stored = spaces.read_widget(created["space_id"], "bad-widget")
    assert stored["recovery"]["disabled"] is False
    assert stored["renderer"] == "<script>breakNormalRoute()</script>"

    recovery = spaces.recovery_snapshot()
    serialized = json.dumps(recovery)
    assert recovery["spaces"][0]["widgets"] == [
        {
            "id": "bad-widget",
            "kind": "html",
            "title": "Bad Widget",
            "disabled": False,
            "disabled_reason": "",
        }
    ]
    assert "breakNormalRoute" not in serialized
    assert "stealSecret" not in serialized
    assert "renderer" not in serialized
    assert "<img" not in serialized
    assert "onerror" not in serialized


def test_recovery_disable_space_marks_manifest_without_deleting_or_leaking_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Broken Space"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "bad-widget",
            "kind": "html",
            "title": "Bad Widget",
            "renderer": "<script>breakSpace()</script>",
            "data": {"api_key": "***"},
        },
    )

    disabled = spaces.disable_space_for_recovery(created["space_id"], reason="space shell failed")

    assert disabled["disabled"] is True
    assert disabled["space_id"] == created["space_id"]
    assert disabled["revision_event_id"]
    stored = spaces.read_space(created["space_id"])
    assert stored["recovery"]["disabled"] is True
    assert stored["recovery"]["disabled_reason"] == "space shell failed"
    assert stored["widgets"][0]["renderer"] == "<script>breakSpace()</script>"

    recovery = spaces.recovery_snapshot()
    serialized = json.dumps(recovery).lower()
    assert recovery["spaces"][0]["disabled"] is True
    assert recovery["spaces"][0]["disabled_reason"] == "space shell failed"
    assert "breakspace" not in serialized
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "<script" not in serialized


def test_recovery_enable_space_restores_safe_metadata_without_rendering_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Disabled Space"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "bad-widget",
            "kind": "html",
            "title": "Bad Widget",
            "renderer": "<script>breakSpace()</script>",
            "data": {"api_key": "***"},
        },
    )
    spaces.disable_space_for_recovery(created["space_id"], reason="space shell failed")

    enabled = spaces.enable_space_for_recovery(created["space_id"])

    assert enabled["disabled"] is False
    assert enabled["space_id"] == created["space_id"]
    stored = spaces.read_space(created["space_id"])
    assert stored["recovery"]["disabled"] is False
    assert stored["recovery"]["disabled_reason"] == ""
    assert stored["widgets"][0]["renderer"] == "<script>breakSpace()</script>"
    recovery = spaces.recovery_snapshot()
    serialized = json.dumps(recovery).lower()
    assert recovery["spaces"][0]["disabled"] is False
    assert recovery["spaces"][0]["disabled_reason"] == ""
    assert "breakspace" not in serialized
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "<script" not in serialized


def test_import_space_agent_yaml_package_quarantines_generated_sources(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    imported = spaces.import_space_agent_package(
        {
            "space_yaml": """
id: unsafe-demo
name: Imported <Space>
description: Space Agent YAML package
instructions: Use safe typed APIs only.
""",
            "widgets": {
                "widgets/weather.yaml": """
id: weather-panel
title: Weather <Panel>
type: html
renderer: "<script>window.SECRET_VALUE_DO_NOT_LEAK=1</script>"
html: "<img src=x onerror=stealSecret()>"
data:
  api_key: SECRET_VALUE_DO_NOT_LEAK
layout:
  x: -5
  y: 2
  w: 99
  h: 0
""",
            },
        }
    )

    assert imported["source"] == "space-agent-yaml"
    assert imported["space"]["space_id"] == "unsafe-demo"
    assert imported["space"]["name"] == "Imported <Space>"
    assert imported["space"]["agent_instructions"] == "Use safe typed APIs only."
    assert imported["imported_widgets"] == [
        {
            "id": "weather-panel",
            "kind": "html",
            "title": "Weather <Panel>",
            "layout": {"x": 0, "y": 2, "w": 24, "h": 1, "minimized": False},
        }
    ]
    stored = spaces.read_widget("unsafe-demo", "weather-panel")
    assert stored["recovery"]["disabled"] is True
    assert stored["recovery"]["disabled_reason"] == "imported generated source disabled pending sandbox review"
    assert stored["untrusted_artifact"]["status"] == "quarantined"
    assert stored["untrusted_artifact"]["omitted_field_count"] >= 3
    serialized = json.dumps(imported).lower()
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "onerror" not in serialized
    assert "api_key" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "untrusted_artifact" not in json.dumps(spaces.read_space_detail("unsafe-demo"))


def test_import_space_agent_zip_b64_route_returns_safe_metadata(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("exported/space.yaml", "id: zip-demo\nname: Zip Demo\ndescription: From ZIP\n")
        zf.writestr(
            "exported/widgets/chart.yaml",
            "id: chart\ntitle: Unsafe Chart\ntype: chart\nrenderer: '<script>bad()</script>'\nsource: SECRET_SOURCE\n",
        )
    archive_b64 = base64.b64encode(bundle.getvalue()).decode("ascii")

    handled, status, body = _route_post("/api/spaces/import", {"archive_b64": archive_b64})

    assert handled is None
    assert status == 200
    assert body["source"] == "space-agent-zip"
    assert body["space"]["space_id"] == "zip-demo"
    assert body["space"]["name"] == "Zip Demo"
    assert body["imported_widgets"] == [
        {"id": "chart", "kind": "chart", "title": "Unsafe Chart", "layout": {"x": 0, "y": 0, "w": 6, "h": 4, "minimized": False}}
    ]
    assert spaces.read_widget("zip-demo", "chart")["recovery"]["disabled"] is True
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "secret_source" not in serialized
    assert "source" not in serialized.replace('"source": "space-agent-zip"', "")


def test_export_space_agent_yaml_package_omits_generated_sources(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space(
        {
            "space_id": "export-demo",
            "name": "Export Demo",
            "description": "Metadata-only export",
            "agent_instructions": "Use typed APIs.",
        }
    )
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "chart",
            "kind": "chart",
            "title": "Safe Chart",
            "layout": {"x": 1, "y": 2, "w": 8, "h": 4},
            "renderer": "<script>window.SECRET_VALUE_DO_NOT_LEAK=1</script>",
            "html": "<img src=x onerror=stealSecret()>",
            "data": {"api_key": "SECRET_VALUE_DO_NOT_LEAK"},
            "source": "SECRET_SOURCE",
            "permissions": {"network": "agent-mediated"},
        },
    )

    exported = spaces.export_space_agent_package(created["space_id"])

    assert exported["source"] == "capy-space"
    assert exported["format"] == "space-agent-yaml"
    assert exported["space_id"] == "export-demo"
    assert sorted(exported["widgets"].keys()) == ["widgets/chart.yaml"]
    import yaml
    space_doc = yaml.safe_load(exported["space_yaml"])
    widget_doc = yaml.safe_load(exported["widgets"]["widgets/chart.yaml"])
    assert space_doc == {
        "id": "export-demo",
        "name": "Export Demo",
        "description": "Metadata-only export",
        "instructions": "Use typed APIs.",
        "template": None,
    }
    assert widget_doc == {
        "id": "chart",
        "title": "Safe Chart",
        "type": "chart",
        "layout": {"x": 1, "y": 2, "w": 8, "h": 4, "minimized": False},
        "permissions": {"network": "agent-mediated"},
    }
    serialized = json.dumps(exported).lower()
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "onerror" not in serialized
    assert "api_key" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "secret_source" not in serialized


def test_export_space_agent_zip_b64_route_returns_safe_archive(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"space_id": "zip-export", "name": "ZIP Export"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "notes",
            "kind": "markdown",
            "title": "Notes",
            "renderer": "<script>bad()</script>",
            "data": {"token": "SECRET_TOKEN"},
        },
    )

    handled, status, body = _route_post("/api/spaces/export", {"space_id": created["space_id"], "format": "zip"})

    assert handled is None
    assert status == 200
    assert body["source"] == "capy-space"
    assert body["format"] == "space-agent-zip"
    assert body["space_id"] == "zip-export"
    assert body["archive_b64"]
    archive_bytes = base64.b64decode(body["archive_b64"])
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        assert sorted(zf.namelist()) == ["space.yaml", "widgets/notes.yaml"]
        space_yaml = zf.read("space.yaml").decode("utf-8")
        widget_yaml = zf.read("widgets/notes.yaml").decode("utf-8")
    archive_text = (space_yaml + widget_yaml).lower()
    assert "zip-export" in archive_text
    assert "notes" in archive_text
    assert "renderer" not in archive_text
    assert "<script" not in archive_text
    assert "token" not in archive_text
    assert "secret_token" not in archive_text
    assert "data" not in archive_text
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "secret_token" not in serialized


def test_install_weather_template_creates_safe_persistent_weather_widget(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("weather")

    assert installed["template"] == "weather"
    assert installed["space"]["template"] == "weather-demo"
    assert installed["space"]["name"] == "Weather Demo"
    assert installed["installed_widgets"] == [
        {
            "id": "weather-current",
            "kind": "weather",
            "title": "Weather in Prague",
            "layout": {"x": 0, "y": 0, "w": 8, "h": 5, "minimized": False},
        }
    ]
    full = spaces.read_widget(installed["space"]["space_id"], "weather-current")
    assert full["weather"]["location"] == "Prague"
    assert full["weather"]["status"] == "ready-for-agent-refresh"
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_weather_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "weather"})

    assert handled is None
    assert status == 200
    assert body["template"] == "weather"
    assert body["space"]["name"] == "Weather Demo"
    assert body["installed_widgets"][0]["id"] == "weather-current"
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_install_research_template_creates_safe_harness_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("research")

    assert installed["template"] == "research"
    assert installed["space"]["template"] == "research-harness"
    assert installed["space"]["name"] == "Research Harness"
    assert [widget["id"] for widget in installed["installed_widgets"]] == [
        "research-query",
        "research-plan",
        "research-sources",
        "research-notes",
        "research-summary",
    ]
    assert [widget["kind"] for widget in installed["installed_widgets"]] == [
        "prompt",
        "status",
        "table",
        "markdown",
        "markdown",
    ]
    query_widget = spaces.read_widget(installed["space"]["space_id"], "research-query")
    assert query_widget["event_bridge"] == {"event_name": "agent.prompt", "status": "ready-for-user-confirmation"}
    sources_widget = spaces.read_widget(installed["space"]["space_id"], "research-sources")
    assert sources_widget["columns"] == ["title", "url", "notes"]
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert '"script"' not in serialized
    assert '"data"' not in serialized
    assert '"source"' not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_research_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "research"})

    assert handled is None
    assert status == 200
    assert body["template"] == "research"
    assert body["space"]["name"] == "Research Harness"
    assert body["installed_widgets"][0]["id"] == "research-query"
    assert len(body["installed_widgets"]) == 5
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_install_dashboard_template_creates_safe_dashboard_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("dashboard")

    assert installed["template"] == "dashboard"
    assert installed["space"]["template"] == "daily-dashboard"
    assert installed["space"]["name"] == "Daily Dashboard"
    assert [widget["id"] for widget in installed["installed_widgets"]] == [
        "dashboard-prices",
        "dashboard-news",
        "dashboard-agenda",
        "dashboard-brief",
    ]
    assert [widget["kind"] for widget in installed["installed_widgets"]] == [
        "chart",
        "news",
        "checklist",
        "markdown",
    ]
    prices_widget = spaces.read_widget(installed["space"]["space_id"], "dashboard-prices")
    assert prices_widget["permissions"] == {"network": "agent-mediated"}
    assert prices_widget["series"] == ["NVDA", "AAPL", "GOOGL"]
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert '"script"' not in serialized
    assert '"data"' not in serialized
    assert '"source"' not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_dashboard_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "dashboard"})

    assert handled is None
    assert status == 200
    assert body["template"] == "dashboard"
    assert body["space"]["name"] == "Daily Dashboard"
    assert body["installed_widgets"][0]["id"] == "dashboard-prices"
    assert len(body["installed_widgets"]) == 4
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_install_kanban_template_creates_safe_board_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("kanban")

    assert installed["template"] == "kanban"
    assert installed["space"]["template"] == "kanban-board"
    assert installed["space"]["name"] == "Kanban Board"
    assert [widget["id"] for widget in installed["installed_widgets"]] == [
        "kanban-backlog",
        "kanban-doing",
        "kanban-done",
        "kanban-notes",
    ]
    assert [widget["kind"] for widget in installed["installed_widgets"]] == [
        "kanban-column",
        "kanban-column",
        "kanban-column",
        "markdown",
    ]
    backlog_widget = spaces.read_widget(installed["space"]["space_id"], "kanban-backlog")
    assert backlog_widget["cards"] == [
        {"id": "card-plan", "title": "Plan the first task", "status": "todo"},
    ]
    assert backlog_widget["interaction"] == {"drag_drop": "planned", "edit_cards": "metadata-only"}
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert '"script"' not in serialized
    assert '"data"' not in serialized
    assert '"source"' not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_kanban_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "kanban"})

    assert handled is None
    assert status == 200
    assert body["template"] == "kanban"
    assert body["space"]["name"] == "Kanban Board"
    assert body["installed_widgets"][0]["id"] == "kanban-backlog"
    assert len(body["installed_widgets"]) == 4
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_install_notes_template_creates_safe_notes_app_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("notes")

    assert installed["template"] == "notes"
    assert installed["space"]["template"] == "notes-app"
    assert installed["space"]["name"] == "Notes App"
    assert [widget["id"] for widget in installed["installed_widgets"]] == [
        "notes-folders",
        "notes-editor",
        "notes-preview",
        "notes-attachments",
    ]
    assert [widget["kind"] for widget in installed["installed_widgets"]] == [
        "folder-list",
        "rich-text-editor",
        "markdown",
        "attachment-list",
    ]
    editor_widget = spaces.read_widget(installed["space"]["space_id"], "notes-editor")
    assert editor_widget["editing"] == {
        "wysiwyg": "planned",
        "markdown_mode": "planned",
        "copy_paste": "metadata-only",
    }
    attachments_widget = spaces.read_widget(installed["space"]["space_id"], "notes-attachments")
    assert attachments_widget["attachments"] == {"images": "planned", "files": "planned", "storage": "agent-mediated"}
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert '"script"' not in serialized
    assert '"data"' not in serialized
    assert '"source"' not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_notes_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "notes"})

    assert handled is None
    assert status == 200
    assert body["template"] == "notes"
    assert body["space"]["name"] == "Notes App"
    assert body["installed_widgets"][0]["id"] == "notes-folders"
    assert len(body["installed_widgets"]) == 4
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_install_browser_surface_template_creates_safe_browser_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("browser")

    assert installed["template"] == "browser"
    assert installed["space"]["template"] == "browser-surface"
    assert installed["space"]["name"] == "Browser Surface"
    assert [widget["id"] for widget in installed["installed_widgets"]] == [
        "browser-panel",
        "browser-controls",
        "browser-notes",
    ]
    assert [widget["kind"] for widget in installed["installed_widgets"]] == [
        "browser-surface",
        "browser-controls",
        "markdown",
    ]
    panel_widget = spaces.read_widget(installed["space"]["space_id"], "browser-panel")
    assert panel_widget["browser_surface"] == {
        "target": "about:blank",
        "control": "user-and-agent",
        "inspection": "metadata-only",
        "bridge": "planned-cdp",
    }
    controls_widget = spaces.read_widget(installed["space"]["space_id"], "browser-controls")
    assert controls_widget["actions"] == ["open_url", "snapshot", "click_ref", "type_ref"]
    assert controls_widget["permissions"] == {"network": "explicit-approval", "browser_control": "agent-mediated"}
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert '\"script\"' not in serialized
    assert '\"data\"' not in serialized
    assert '\"source\"' not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_browser_surface_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "browser"})

    assert handled is None
    assert status == 200
    assert body["template"] == "browser"
    assert body["space"]["name"] == "Browser Surface"
    assert body["installed_widgets"][0]["id"] == "browser-panel"
    assert len(body["installed_widgets"]) == 3
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_install_stock_chart_template_creates_safe_market_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("stock")

    assert installed["template"] == "stock"
    assert installed["space"]["template"] == "stock-chart"
    assert installed["space"]["name"] == "Stock Chart"
    assert [widget["id"] for widget in installed["installed_widgets"]] == [
        "stock-chart",
        "stock-watchlist",
        "stock-notes",
    ]
    assert [widget["kind"] for widget in installed["installed_widgets"]] == [
        "chart",
        "table",
        "markdown",
    ]
    chart_widget = spaces.read_widget(installed["space"]["space_id"], "stock-chart")
    assert chart_widget["series"] == ["NVDA", "AAPL", "GOOGL"]
    assert chart_widget["market_data"] == {
        "provider": "agent-mediated",
        "status": "ready-for-agent-refresh",
        "range": "1mo",
    }
    watchlist_widget = spaces.read_widget(installed["space"]["space_id"], "stock-watchlist")
    assert watchlist_widget["columns"] == ["symbol", "last", "change", "notes"]
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert '\"script\"' not in serialized
    assert '\"data\"' not in serialized
    assert '\"source\"' not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_stock_chart_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "stock"})

    assert handled is None
    assert status == 200
    assert body["template"] == "stock"
    assert body["space"]["name"] == "Stock Chart"
    assert body["installed_widgets"][0]["id"] == "stock-chart"
    assert len(body["installed_widgets"]) == 3
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_install_big_bang_template_creates_safe_onboarding_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("big-bang")

    assert installed["template"] == "big-bang"
    assert installed["space"]["template"] == "big-bang-onboarding"
    assert installed["space"]["name"] == "Big Bang Onboarding"
    assert [widget["id"] for widget in installed["installed_widgets"]] == [
        "bigbang-welcome",
        "bigbang-demo-launcher",
        "bigbang-safety",
        "bigbang-next-steps",
    ]
    assert [widget["kind"] for widget in installed["installed_widgets"]] == [
        "markdown",
        "checklist",
        "status",
        "checklist",
    ]
    launcher_widget = spaces.read_widget(installed["space"]["space_id"], "bigbang-demo-launcher")
    assert launcher_widget["demo_templates"] == [
        "weather",
        "research",
        "dashboard",
        "camera",
        "kanban",
        "notes",
        "browser",
        "stock",
        "game",
        "music",
    ]
    assert launcher_widget["interaction"] == {"install_templates": "agent-mediated", "preview": "metadata-only"}
    safety_widget = spaces.read_widget(installed["space"]["space_id"], "bigbang-safety")
    assert safety_widget["safety"] == {
        "generated_code": "disabled-by-default",
        "recovery": "available",
        "rollback": "revision-history-planned",
    }
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert '\"script\"' not in serialized
    assert '\"data\"' not in serialized
    assert '\"source\"' not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_big_bang_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "big-bang"})

    assert handled is None
    assert status == 200
    assert body["template"] == "big-bang"
    assert body["space"]["name"] == "Big Bang Onboarding"
    assert body["installed_widgets"][0]["id"] == "bigbang-welcome"
    assert len(body["installed_widgets"]) == 4
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_reset_big_bang_template_restores_canonical_metadata_and_removes_extra_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    installed = spaces.install_template("big-bang")
    space_id = installed["space"]["space_id"]
    spaces.update_space(
        space_id,
        {
            "name": "Broken <script>name</script>",
            "description": "Contains api_key SECRET",
            "agent_instructions": "Leaky token SECRET",
        },
    )
    spaces.upsert_widget(
        space_id,
        {
            "id": "custom-generated",
            "kind": "html",
            "title": "Custom generated",
            "renderer": "<script>steal()</script>",
            "api_key": "SECRET",
        },
    )

    reset = spaces.reset_template("big-bang", space_id=space_id)

    assert reset["template"] == "big-bang"
    assert reset["reset"] is True
    assert reset["space"]["space_id"] == space_id
    assert reset["space"]["name"] == "Big Bang Onboarding"
    assert reset["space"]["template"] == "big-bang-onboarding"
    assert [widget["id"] for widget in reset["installed_widgets"]] == [
        "bigbang-welcome",
        "bigbang-demo-launcher",
        "bigbang-safety",
        "bigbang-next-steps",
    ]
    assert "custom-generated" not in [widget["id"] for widget in reset["installed_widgets"]]
    stored = spaces.read_space(space_id)
    assert stored["name"] == "Big Bang Onboarding"
    assert stored["description"] == "Metadata-only first-run tour for Capy Spaces demos, safety guardrails, and next steps."
    assert stored["agent_instructions"].startswith("Use this onboarding space")
    assert [widget["id"] for widget in stored["widgets"]] == [
        "bigbang-welcome",
        "bigbang-demo-launcher",
        "bigbang-safety",
        "bigbang-next-steps",
    ]
    assert spaces.list_revision_events(space_id)[0]["event_type"] == "template.reset"
    serialized = json.dumps(reset).lower()
    assert "custom-generated" not in serialized
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_big_bang_template_reset_route_returns_safe_metadata(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    installed = spaces.install_template("big-bang")
    space_id = installed["space"]["space_id"]
    spaces.upsert_widget(
        space_id,
        {"id": "unsafe-extra", "kind": "html", "title": "Unsafe", "renderer": "<script>bad()</script>", "api_key": "SECRET"},
    )

    handled, status, body = _route_post("/api/spaces/templates/reset", {"template": "big-bang", "space_id": space_id})

    assert handled is None
    assert status == 200
    assert body["template"] == "big-bang"
    assert body["reset"] is True
    assert body["space"]["name"] == "Big Bang Onboarding"
    assert [widget["id"] for widget in body["installed_widgets"]] == [
        "bigbang-welcome",
        "bigbang-demo-launcher",
        "bigbang-safety",
        "bigbang-next-steps",
    ]
    serialized = json.dumps(body).lower()
    assert "unsafe-extra" not in serialized
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_install_game_template_creates_safe_canvas_game_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("game")

    assert installed["template"] == "game"
    assert installed["space"]["template"] == "game-sandbox"
    assert installed["space"]["name"] == "Game Sandbox"
    assert [widget["id"] for widget in installed["installed_widgets"]] == [
        "game-canvas",
        "game-controls",
        "game-repair-notes",
    ]
    assert [widget["kind"] for widget in installed["installed_widgets"]] == [
        "canvas-game",
        "status",
        "markdown",
    ]
    canvas_widget = spaces.read_widget(installed["space"]["space_id"], "game-canvas")
    assert canvas_widget["game"] == "snake"
    assert canvas_widget["input_policy"] == {
        "keyboard_focus": "explicit-click",
        "global_keys": "blocked",
        "cleanup": "planned",
    }
    assert canvas_widget["permissions"] == {"generated_rendering": "disabled", "keyboard": "explicit-focus"}
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert '\"script\"' not in serialized
    assert '\"data\"' not in serialized
    assert '\"source\"' not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_game_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "game"})

    assert handled is None
    assert status == 200
    assert body["template"] == "game"
    assert body["space"]["name"] == "Game Sandbox"
    assert body["installed_widgets"][0]["id"] == "game-canvas"
    assert len(body["installed_widgets"]) == 3
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_install_music_sequencer_template_creates_safe_audio_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("music")

    assert installed["template"] == "music"
    assert installed["space"]["template"] == "music-sequencer"
    assert installed["space"]["name"] == "Music Sequencer"
    assert [widget["id"] for widget in installed["installed_widgets"]] == [
        "music-sequencer-grid",
        "music-synth-controls",
        "music-piano-roll",
        "music-notes",
    ]
    assert [widget["kind"] for widget in installed["installed_widgets"]] == [
        "step-sequencer",
        "audio-controls",
        "piano-roll",
        "markdown",
    ]
    sequencer_widget = spaces.read_widget(installed["space"]["space_id"], "music-sequencer-grid")
    assert sequencer_widget["audio_policy"] == {
        "permission": "explicit-user-gesture",
        "webaudio": "disabled-until-approved",
        "cleanup": "planned-on-rerender",
    }
    assert sequencer_widget["pattern_status"] == "metadata-only-empty"
    piano_widget = spaces.read_widget(installed["space"]["space_id"], "music-piano-roll")
    assert piano_widget["interaction"] == {"keyboard": "explicit-focus", "editing": "planned-metadata"}
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert '\"script\"' not in serialized
    assert '\"data\"' not in serialized
    assert '\"source\"' not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_music_sequencer_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "music"})

    assert handled is None
    assert status == 200
    assert body["template"] == "music"
    assert body["space"]["name"] == "Music Sequencer"
    assert body["installed_widgets"][0]["id"] == "music-sequencer-grid"
    assert len(body["installed_widgets"]) == 4
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_install_camera_dashboard_template_creates_safe_camera_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("camera")

    assert installed["template"] == "camera"
    assert installed["space"]["template"] == "camera-dashboard"
    assert installed["space"]["name"] == "Camera Dashboard"
    assert [widget["id"] for widget in installed["installed_widgets"]] == [
        "camera-grid",
        "camera-permissions",
        "camera-incidents",
    ]
    assert [widget["kind"] for widget in installed["installed_widgets"]] == [
        "camera-grid",
        "status",
        "table",
    ]
    grid_widget = spaces.read_widget(installed["space"]["space_id"], "camera-grid")
    assert grid_widget["streams"] == []
    assert grid_widget["stream_policy"] == {
        "network": "explicit-approval",
        "private_urls": "approval-required",
        "mixed_content": "blocked-by-default",
    }
    permissions_widget = spaces.read_widget(installed["space"]["space_id"], "camera-permissions")
    assert permissions_widget["permissions"] == {"network": "explicit-approval", "camera_urls": "agent-mediated"}
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert '\"script\"' not in serialized
    assert '\"data\"' not in serialized
    assert '\"source\"' not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_camera_dashboard_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "camera"})

    assert handled is None
    assert status == 200
    assert body["template"] == "camera"
    assert body["space"]["name"] == "Camera Dashboard"
    assert body["installed_widgets"][0]["id"] == "camera-grid"
    assert len(body["installed_widgets"]) == 3
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_camera_stream_tool_rejects_private_urls_without_approval_and_does_not_store_them(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    installed = spaces.install_template("camera")
    space_id = installed["space"]["space_id"]

    with pytest.raises(PermissionError, match="explicit approval"):
        spaces.run_space_tool(
            "space.camera.add_stream",
            {
                "space_id": space_id,
                "title": "Garage",
                "url": "http://192.168.1.55:8080/live?token=SECRET_VALUE_DO_NOT_LEAK",
            },
        )

    grid_widget = spaces.read_widget(space_id, "camera-grid")
    assert grid_widget["streams"] == []


def test_camera_stream_tool_rejects_secret_like_approval_id_as_approval(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    installed = spaces.install_template("camera")
    space_id = installed["space"]["space_id"]

    with pytest.raises(PermissionError, match="explicit approval"):
        spaces.run_space_tool(
            "space.camera.add_stream",
            {
                "space_id": space_id,
                "title": "Garage",
                "url": "http://192.168.1.55:8080/live?token=SECRET_VALUE_DO_NOT_LEAK",
                "approval_id": "token=SECRET_VALUE_DO_NOT_LEAK",
            },
        )

    assert spaces.read_widget(space_id, "camera-grid")["streams"] == []


def test_camera_stream_tool_records_approved_private_stream_as_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    installed = spaces.install_template("camera")
    space_id = installed["space"]["space_id"]

    result = spaces.run_space_tool(
        "space.camera.add_stream",
        {
            "space_id": space_id,
            "title": "Garage <script>ignored</script>",
            "url": "http://192.168.1.55:8080/live?token=SECRET_VALUE_DO_NOT_LEAK",
            "approved": True,
            "approval_id": "approval-123",
            "renderer": "<script>steal()</script>",
            "api_key": "SECRET_VALUE_DO_NOT_LEAK",
        },
    )

    assert result["ok"] is True
    assert result["action"] == "space.camera.add_stream"
    assert result["stream"]["title"] == "Garage ignored"
    assert result["stream"]["host_class"] == "private"
    assert result["stream"]["approved"] is True
    assert result["stream"]["url_digest"]
    assert spaces.read_widget(space_id, "camera-grid")["streams"] == [result["stream"]]
    serialized = json.dumps(result).lower()
    assert "192.168.1.55" not in serialized
    assert "8080" not in serialized
    assert "token" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized


def test_camera_stream_tool_route_rejects_unapproved_url_with_403(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    installed = spaces.install_template("camera")

    handled, status, body = _route_post(
        "/api/spaces/tool",
        {
            "action": "space.camera.add_stream",
            "space_id": installed["space"]["space_id"],
            "title": "Garage",
            "url": "http://192.168.1.55:8080/live?token=SECRET_VALUE_DO_NOT_LEAK",
        },
    )

    assert handled is None
    assert status == 403
    assert "explicit approval" in body["error"]
    serialized = json.dumps(body).lower()
    assert "192.168.1.55" not in serialized
    assert "token" not in serialized
    assert "secret_value_do_not_leak" not in serialized


def test_install_local_service_template_creates_safe_service_dashboard_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("service")

    assert installed["template"] == "service"
    assert installed["space"]["template"] == "local-service-dashboard"
    assert installed["space"]["name"] == "Local Service Dashboard"
    assert [widget["id"] for widget in installed["installed_widgets"]] == [
        "service-api-chat",
        "service-browser-panel",
        "service-health",
        "service-settings-review",
    ]
    assert [widget["kind"] for widget in installed["installed_widgets"]] == [
        "api-connector",
        "browser-surface",
        "status",
        "table",
    ]
    api_widget = spaces.read_widget(installed["space"]["space_id"], "service-api-chat")
    assert api_widget["connector"] == {
        "target": "local-service",
        "auth": "configured-outside-widget",
        "mode": "agent-mediated",
    }
    assert api_widget["permissions"] == {"network": "explicit-approval", "secrets": "never-store-in-widget"}
    browser_widget = spaces.read_widget(installed["space"]["space_id"], "service-browser-panel")
    assert browser_widget["browser_surface"] == {
        "target": "about:blank",
        "control": "user-and-agent",
        "inspection": "metadata-only",
        "bridge": "planned-cdp",
    }
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert '\"script\"' not in serialized
    assert '\"data\"' not in serialized
    assert '\"source\"' not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized


def test_local_service_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "service"})

    assert handled is None
    assert status == 200
    assert body["template"] == "service"
    assert body["space"]["name"] == "Local Service Dashboard"
    assert body["installed_widgets"][0]["id"] == "service-api-chat"
    assert len(body["installed_widgets"]) == 4
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized


def test_install_model_setup_template_creates_safe_provider_setup_widgets(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    installed = spaces.install_template("model-setup")

    assert installed["template"] == "model-setup"
    assert installed["space"]["template"] == "model-provider-setup"
    assert installed["space"]["name"] == "Model Provider Setup"
    assert [widget["id"] for widget in installed["installed_widgets"]] == [
        "model-provider-status",
        "model-local-runtime",
        "model-settings-review",
        "model-next-steps",
    ]
    assert [widget["kind"] for widget in installed["installed_widgets"]] == [
        "status",
        "local-runtime",
        "table",
        "checklist",
    ]
    status_widget = spaces.read_widget(installed["space"]["space_id"], "model-provider-status")
    assert status_widget["provider_setup"] == {
        "mode": "configured-outside-widget",
        "secret_storage": "never-store-in-widget",
        "targets": ["Hermes profiles", "LM Studio", "OpenAI-compatible providers"],
    }
    runtime_widget = spaces.read_widget(installed["space"]["space_id"], "model-local-runtime")
    assert runtime_widget["local_runtime"] == {
        "engine": "LM Studio",
        "status": "external-service-review",
        "model_loading": "agent-mediated-with-approval",
    }
    serialized = json.dumps(installed).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert '\"script\"' not in serialized
    assert '\"data\"' not in serialized
    assert '\"source\"' not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized


def test_model_setup_template_install_route_returns_safe_metadata(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    handled, status, body = _route_post("/api/spaces/templates/install", {"template": "model-setup"})

    assert handled is None
    assert status == 200
    assert body["template"] == "model-setup"
    assert body["space"]["name"] == "Model Provider Setup"
    assert body["installed_widgets"][0]["id"] == "model-provider-status"
    assert len(body["installed_widgets"]) == 4
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized


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
    panels_js = (repo / "static" / "panels.js").read_text(encoding="utf-8")
    ui_js = (repo / "static" / "ui.js").read_text(encoding="utf-8")
    spaces_js = (repo / "static" / "spaces.js").read_text(encoding="utf-8")

    assert '"/api/spaces"' in routes_src
    assert '"/api/spaces/current"' in routes_src
    assert '"/api/spaces/recovery"' in routes_src
    assert '"/api/spaces/recovery/disable-space"' in routes_src
    assert '"/api/spaces/recovery/enable-space"' in routes_src
    assert '"/api/spaces/recovery/disable-widget"' in routes_src
    assert '"/api/spaces/recovery/enable-widget"' in routes_src
    assert '"/api/spaces/import"' in routes_src
    assert '"/api/spaces/export"' in routes_src
    assert '"/api/spaces/revisions"' in routes_src
    assert '"/api/spaces/widget/events"' in routes_src
    assert '"/api/spaces/revision/restore"' in routes_src
    assert '"/api/spaces/widget/patch"' in routes_src
    assert '"/api/spaces/system-widget/upsert"' in routes_src
    assert '"/api/spaces/create"' in routes_src
    assert '"/api/spaces/templates/install"' in routes_src
    assert '"/api/spaces/deactivate"' in routes_src
    assert 'static/spaces.js' in index_html
    assert 'static/spaces.css' in index_html
    assert 'id="mainCapySpaces"' in index_html
    assert 'id="capySpacesRecovery"' in index_html
    assert 'id="capyActiveSpaceContext"' in index_html
    assert 'id="capyActiveSpaceLabel"' in index_html
    assert 'id="capyActiveSpaceClear"' in index_html
    assert 'data-panel="capy-spaces"' in index_html
    assert "switchPanel('capy-spaces')" in index_html
    assert "'capy-spaces': 'Capy Spaces'" in panels_js
    assert "'capy-spaces'" in panels_js
    assert "loadCapySpaces()" in panels_js
    assert "loadCapySpacesRecovery()" in panels_js
    assert "function syncCapyActiveSpaceContext" in ui_js
    assert "async function clearCapyActiveSpace" in ui_js
    assert "active_space_id" in ui_js
    assert "capyActiveSpaceLabel" in ui_js
    assert "system.chat" in spaces_js
    assert "system.settings" in spaces_js
    assert "data-capy-action=\"openSystemPanel\"" in spaces_js
    assert "data-capy-action=\"moveWidget\"" in spaces_js
    assert "moveWidgetBy" in spaces_js
    assert "data-capy-action=\"resizeWidget\"" in spaces_js
    assert "resizeWidgetBy" in spaces_js
    assert "data-capy-action=\"toggleWidgetMinimized\"" in spaces_js
    assert "toggleWidgetMinimized" in spaces_js


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

    handled, status, body = _route_post(
        "/api/spaces/revision/restore",
        {"space_id": space_id, "event_id": body["revisions"][0]["event_id"]},
    )
    assert handled is None
    assert status == 200
    assert body["ok"] is True
    assert body["space"]["space_id"] == space_id
    assert body["restored_event_id"]

    handled, status, body = _route_get("/api/spaces/recovery")
    assert handled is None
    assert status == 200
    assert body["generated_widgets_rendered"] is False


def test_space_tool_route_patches_widget_metadata_without_leaking_sources(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Tool Route"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "unsafe-widget",
            "kind": "html",
            "title": "Original",
            "renderer": "<script>persistButDoNotReturn()</script>",
            "data": {"api_key": "SECRET_VALUE_DO_NOT_LEAK"},
        },
    )

    handled, status, body = _route_post(
        "/api/spaces/tool",
        {
            "action": "widget.patch",
            "space_id": created["space_id"],
            "widget_id": "unsafe-widget",
            "patch": {
                "title": "Patched safely",
                "renderer": "<script>newLeak()</script>",
                "api_key": "SECRET_VALUE_DO_NOT_LEAK",
            },
        },
    )

    assert handled is None
    assert status == 200
    assert body["ok"] is True
    assert body["action"] == "widget.patch"
    assert body["widget"]["title"] == "Patched safely"
    stored = spaces.read_widget(created["space_id"], "unsafe-widget")
    assert stored["renderer"] == "<script>persistButDoNotReturn()</script>"
    serialized = json.dumps(body).lower()
    assert "persistbutdonotreturn" not in serialized
    assert "newleak" not in serialized
    assert "<script" not in serialized
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "secret_value_do_not_leak" not in serialized


def test_recovery_enable_widget_route_restores_widget_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Route Recovery"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "bad-widget",
            "kind": "html",
            "title": "Bad Widget",
            "renderer": "<script>breakNormalRoute()</script>",
            "data": {"api_key": "***"},
        },
    )
    spaces.disable_widget_for_recovery(created["space_id"], "bad-widget", reason="render failure")

    handled, status, body = _route_post(
        "/api/spaces/recovery/enable-widget",
        {"space_id": created["space_id"], "widget_id": "bad-widget"},
    )

    assert handled is None
    assert status == 200
    assert body["disabled"] is False
    assert body["space_id"] == created["space_id"]
    assert body["widget_id"] == "bad-widget"
    stored = spaces.read_widget(created["space_id"], "bad-widget")
    assert stored["recovery"]["disabled"] is False
    recovery = spaces.recovery_snapshot()
    serialized = json.dumps(recovery).lower()
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "<script" not in serialized


def test_recovery_disable_enable_space_routes_return_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Route Space Recovery"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "bad-widget",
            "kind": "html",
            "title": "Bad Widget",
            "renderer": "<script>breakRouteSpace()</script>",
            "data": {"api_key": "***"},
        },
    )

    handled, status, body = _route_post(
        "/api/spaces/recovery/disable-space",
        {"space_id": created["space_id"], "reason": "safe-mode disable"},
    )

    assert handled is None
    assert status == 200
    assert body["disabled"] is True
    assert body["space_id"] == created["space_id"]
    assert spaces.read_space(created["space_id"])["recovery"]["disabled"] is True
    assert "breakRouteSpace" not in json.dumps(body)

    handled, status, body = _route_post(
        "/api/spaces/recovery/enable-space",
        {"space_id": created["space_id"]},
    )

    assert handled is None
    assert status == 200
    assert body["disabled"] is False
    assert body["space_id"] == created["space_id"]
    assert spaces.read_space(created["space_id"])["recovery"]["disabled"] is False
    serialized = json.dumps(spaces.recovery_snapshot()).lower()
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "<script" not in serialized


def test_current_space_helper_and_route_return_metadata_only_active_space(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space(
        {
            "space_id": "route-current",
            "name": "Route Current",
            "description": "Current-space bridge",
            "agent_instructions": "Patch widgets through typed APIs.",
        }
    )
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "unsafe-card",
            "kind": "html",
            "title": "Unsafe Card",
            "renderer": "<script>window.SECRET_VALUE_DO_NOT_LEAK=1</script>",
            "data": {"api_key": "SECRET...LEAK"},
        },
    )

    import api.config as config
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / "sessions")
    config.SESSION_DIR.mkdir(parents=True, exist_ok=True)

    import api.models as models
    monkeypatch.setattr(models, "SESSION_DIR", config.SESSION_DIR)
    models.SESSIONS.clear()
    session = models.Session(session_id="session_current", workspace=str(tmp_path))
    session.active_space_id = created["space_id"]
    session.save(skip_index=True)

    helper_payload = spaces.current_space_for_session(session)
    assert helper_payload["enabled"] is True
    assert helper_payload["active_space_id"] == created["space_id"]
    assert helper_payload["space"]["space_id"] == created["space_id"]
    assert helper_payload["space"]["widgets"] == [
        {"id": "unsafe-card", "kind": "html", "title": "Unsafe Card", "layout": {"x": 0, "y": 0, "w": 6, "h": 4, "minimized": False}}
    ]

    handled, status, body = _route_get("/api/spaces/current?session_id=session_current")
    assert handled is None
    assert status == 200
    assert body == helper_payload
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "data" not in serialized
    assert "api_key" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "<script" not in serialized


def test_current_space_route_handles_no_active_space_without_manifest_lookup(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    import api.config as config
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / "sessions")
    config.SESSION_DIR.mkdir(parents=True, exist_ok=True)

    import api.models as models
    monkeypatch.setattr(models, "SESSION_DIR", config.SESSION_DIR)
    models.SESSIONS.clear()
    session = models.Session(session_id="session_no_space", workspace=str(tmp_path))
    session.save(skip_index=True)

    handled, status, body = _route_get("/api/spaces/current?session_id=session_no_space")

    assert handled is None
    assert status == 200
    assert body == {"enabled": True, "active_space_id": None, "space": None}


def test_system_widget_route_adds_allowlisted_trusted_widget_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "System Widget Lab"})

    handled, status, body = _route_post(
        "/api/spaces/system-widget/upsert",
        {
            "space_id": created["space_id"],
            "panel": "chat",
            "layout": {"x": 2, "y": -4, "w": 99, "h": 0, "minimized": False},
            "renderer": "<script>doNotStore()</script>",
            "api_key": "SECRET_VALUE_DO_NOT_LEAK",
        },
    )

    assert handled is None
    assert status == 200
    assert body["widget"] == {
        "id": "system-chat",
        "kind": "system",
        "title": "Chat",
        "layout": {"x": 2, "y": 0, "w": 24, "h": 1, "minimized": False},
        "system_panel": "chat",
    }
    assert body["revision_event_id"]
    stored = spaces.read_widget(created["space_id"], "system-chat")
    assert stored["system"] == {"panel": "chat", "trusted": True}
    assert "renderer" not in stored
    assert "api_key" not in stored

    detail = spaces.read_space_detail(created["space_id"])
    assert detail["widgets"] == [body["widget"]]
    serialized = json.dumps(body).lower() + json.dumps(detail).lower()
    assert "donotstore" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret_value_do_not_leak" not in serialized


def test_system_widget_route_rejects_unknown_panels(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "System Widget Reject"})

    handled, status, body = _route_post(
        "/api/spaces/system-widget/upsert",
        {"space_id": created["space_id"], "panel": "../../settings", "layout": {"x": 0, "y": 0, "w": 12, "h": 6}},
    )

    assert handled is None
    assert status == 400
    assert spaces.read_space_detail(created["space_id"])["widgets"] == []
    assert "../../settings" not in json.dumps(body)


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


def test_spaces_deactivate_route_clears_active_space_from_session(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    import api.config as config
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / "sessions")
    config.SESSION_DIR.mkdir(parents=True, exist_ok=True)

    import api.models as models
    monkeypatch.setattr(models, "SESSION_DIR", config.SESSION_DIR)
    models.SESSIONS.clear()
    session = models.Session(session_id="session_deactivate", workspace=str(tmp_path))
    session.active_space_id = "lab"
    session.save(skip_index=True)

    handled, status, body = _route_post(
        "/api/spaces/deactivate",
        {"session_id": "session_deactivate"},
    )

    assert handled is None
    assert status == 200
    assert body["ok"] is True
    assert body["session"]["active_space_id"] is None
    assert body["session"]["messages"] == []
    loaded = models.Session.load("session_deactivate")
    assert loaded.active_space_id is None


def test_create_space_from_session_route_creates_metadata_only_active_space(monkeypatch, tmp_path):
    _load_spaces(monkeypatch, tmp_path, enabled=True)

    import api.config as config
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / "sessions")
    config.SESSION_DIR.mkdir(parents=True, exist_ok=True)

    import api.models as models
    monkeypatch.setattr(models, "SESSION_DIR", config.SESSION_DIR)
    models.SESSIONS.clear()
    session = models.Session(session_id="session_context", workspace=str(tmp_path / "safe-workspace"))
    session.title = "Research Chat"
    session.messages = [
        {"role": "user", "content": "Use API_KEY=SECRET_VALUE_DO_NOT_LEAK for the weather call"},
        {"role": "assistant", "content": "I will keep the token hidden."},
    ]
    session.save(skip_index=True)

    handled, status, body = _route_post(
        "/api/spaces/create-from-session",
        {"session_id": "session_context"},
    )

    assert handled is None
    assert status == 200
    assert body["ok"] is True
    assert body["space"]["template"] == "chat-context"
    assert body["space"]["name"] == "Research Chat Space"
    assert body["space"]["widgets"] == [
        {
            "id": "chat-context",
            "kind": "status",
            "title": "Linked chat context",
            "layout": {"x": 0, "y": 0, "w": 8, "h": 4, "minimized": False},
        }
    ]
    assert body["session"]["active_space_id"] == body["space"]["space_id"]
    assert "messages" not in body["session"]
    loaded = models.Session.load("session_context")
    assert loaded.active_space_id == body["space"]["space_id"]
    serialized = json.dumps(body).lower()
    assert "api_key" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "token hidden" not in serialized
    assert "<script" not in serialized


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


def test_widget_detail_includes_allowlisted_declarative_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Detail Metadata Lab"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "research-summary",
            "kind": "markdown",
            "title": "Research Summary",
            "layout": {"x": 1, "y": 2, "w": 7, "h": 5},
            "content_status": "agent-managed-empty",
            "status": "draft",
            "export": {"pdf": "planned", "api_key": "SECRET_VALUE_DO_NOT_LEAK"},
            "interaction": {"refresh": "agent-mediated", "dangerous_html": "<script>bad()</script>"},
            "permissions": {"network": "agent-mediated", "token": "SECRET...LEAK", "credential": "SECRET...LEAK"},
            "renderer": "<script>steal()</script>",
            "html": "<img src=x onerror=steal()>",
            "data": {"api_key": "SECRET_VALUE_DO_NOT_LEAK"},
        },
    )

    detail = spaces.read_widget_detail(created["space_id"], "research-summary")
    serialized = json.dumps(detail).lower()

    assert detail["metadata"]["content_status"] == "agent-managed-empty"
    assert detail["metadata"]["status"] == "draft"
    assert detail["metadata"]["export"] == {"pdf": "planned"}
    assert detail["metadata"]["interaction"] == {"refresh": "agent-mediated"}
    assert detail["metadata"]["permissions"] == {"network": "agent-mediated"}
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "onerror" not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized
    assert "credential" not in serialized
    assert "secret" not in serialized


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


def test_widget_patch_updates_fields_preserves_source_and_returns_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Patch Lab"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "weather",
            "kind": "html",
            "title": "Weather",
            "layout": {"x": 1, "y": 2, "w": 7, "h": 3},
            "renderer": "<script>keepButDoNotExpose()</script>",
            "data": {"api_key": "SECRET...LEAK", "status": "draft"},
        },
    )

    patched = spaces.patch_widget(
        created["space_id"],
        "weather",
        {
            "title": "Weather patched",
            "kind": "markdown",
            "layout": {"x": "4", "y": -9, "w": 99, "h": 0, "minimized": "yes"},
            "renderer": "<script>attemptedReplacement()</script>",
            "data": {"api_key": "ATTEMPTED_LEAK"},
        },
    )

    assert patched["widget"] == {
        "id": "weather",
        "kind": "markdown",
        "title": "Weather patched",
        "layout": {"x": 4, "y": 0, "w": 24, "h": 1, "minimized": True},
    }
    assert patched["revision_event_id"]
    stored = spaces.read_widget(created["space_id"], "weather")
    assert stored["renderer"] == "<script>keepButDoNotExpose()</script>"
    assert stored["data"] == {"api_key": "SECRET...LEAK", "status": "draft"}
    assert stored["title"] == "Weather patched"
    assert stored["kind"] == "markdown"
    serialized = json.dumps(patched).lower()
    assert "renderer" not in serialized
    assert "data" not in serialized
    assert "secret" not in serialized
    assert "attemptedreplacement" not in serialized


def test_widget_patch_route_updates_metadata_and_omits_generated_fields(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Route Patch"})
    spaces.upsert_widget(
        created["space_id"],
        {
            "id": "card",
            "kind": "html",
            "title": "Card",
            "renderer": "<script>storedSource()</script>",
            "data": {"token": "SECRET"},
        },
    )

    handled, status, body = _route_post(
        "/api/spaces/widget/patch",
        {
            "space_id": created["space_id"],
            "widget_id": "card",
            "patch": {"title": "Card patched", "layout": {"x": 3, "y": 4, "w": 5, "h": 6}, "source": "SECRET_SOURCE"},
        },
    )

    assert handled is None
    assert status == 200
    assert body["widget"] == {
        "id": "card",
        "kind": "html",
        "title": "Card patched",
        "layout": {"x": 3, "y": 4, "w": 5, "h": 6, "minimized": False},
    }
    assert spaces.read_widget(created["space_id"], "card")["renderer"] == "<script>storedSource()</script>"
    serialized = json.dumps(body).lower()
    assert "renderer" not in serialized
    assert "source" not in serialized
    assert "secret" not in serialized


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


def test_widget_routes_return_metadata_only_even_when_widget_stores_generated_bodies(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Unsafe Route Widget"})
    space_id = created["space_id"]
    unsafe_widget = {
        "id": "custom-card",
        "kind": "custom",
        "title": "Custom Card",
        "layout": {"x": 1, "y": 2, "w": 7, "h": 3},
        "renderer": "<script>window.SECRET_VALUE_DO_NOT_LEAK='x'</script>",
        "html": "<img src=x onerror=stealSecret()>",
        "script": "stealSecret()",
        "data": {"api_key": "SECRET_VALUE_DO_NOT_LEAK"},
        "source": "SECRET_SOURCE",
    }

    handled, status, body = _route_post(
        "/api/spaces/widget/upsert",
        {"space_id": space_id, "widget": unsafe_widget},
    )

    assert handled is None
    assert status == 200
    assert body["widget"] == {
        "id": "custom-card",
        "kind": "custom",
        "title": "Custom Card",
        "layout": {"x": 1, "y": 2, "w": 7, "h": 3, "minimized": False},
    }
    assert spaces.read_widget(space_id, "custom-card")["renderer"].startswith("<script>")

    handled, status, body = _route_get(f"/api/spaces/widget?space_id={space_id}&widget_id=custom-card")

    assert handled is None
    assert status == 200
    assert body["widget"] == {
        "id": "custom-card",
        "kind": "custom",
        "title": "Custom Card",
        "layout": {"x": 1, "y": 2, "w": 7, "h": 3, "minimized": False},
    }
    serialized = json.dumps(body).lower()
    assert "secret_value_do_not_leak" not in serialized
    assert "secret_source" not in serialized
    assert "stealsecret" not in serialized
    assert "<script" not in serialized
    assert "renderer" not in serialized
    assert "html" not in serialized
    assert "script" not in serialized
    assert "data" not in serialized
    assert "api_key" not in serialized


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


def test_list_widget_events_and_route_return_safe_newest_first_inbox(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Event Inbox"})
    spaces.upsert_widget(created["space_id"], {"id": "weather", "kind": "markdown", "title": "Weather"})
    spaces.upsert_widget(created["space_id"], {"id": "notes", "kind": "markdown", "title": "Notes"})

    first = spaces.queue_widget_event(
        created["space_id"],
        "weather",
        "agent.prompt",
        {"query": "forecast", "note": "token=SECRET_VALUE_DO_NOT_LEAK"},
        prompt="Use Authorization Bearer SECRET_VALUE_DO_NOT_LEAK",
        session_id="session-123",
    )
    second = spaces.queue_widget_event(
        created["space_id"],
        "notes",
        "widget.refresh",
        {"action": "refresh", "renderer": "<script>bad()</script>"},
        session_id="session-123",
    )

    events = spaces.list_widget_events(created["space_id"])

    assert [event["event_id"] for event in events] == [second["event_id"], first["event_id"]]
    assert events[0]["widget_id"] == "notes"
    assert events[0]["event_name"] == "widget.refresh"
    assert events[0]["status"] == "queued"
    assert events[1]["widget_id"] == "weather"
    assert events[1]["payload_summary"]["query"] == "forecast"
    assert events[1]["payload_summary"]["note"] == "[REDACTED]"
    assert events[1]["prompt_preview"] == "[REDACTED]"

    weather_events = spaces.list_widget_events(created["space_id"], widget_id="weather")
    assert [event["event_id"] for event in weather_events] == [first["event_id"]]

    handled, status, body = _route_get(f"/api/spaces/widget/events?space_id={created['space_id']}&widget_id=weather")
    assert handled is None
    assert status == 200
    assert [event["event_id"] for event in body["events"]] == [first["event_id"]]
    serialized = json.dumps(body).lower()
    assert "secret_value_do_not_leak" not in serialized
    assert "authorization" not in serialized
    assert "bearer" not in serialized
    assert "<script" not in serialized
    assert "renderer" not in serialized


def test_recovery_disable_widget_route_marks_widget_disabled(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)
    created = spaces.create_space({"name": "Route Recovery"})
    spaces.upsert_widget(
        created["space_id"],
        {"id": "broken", "kind": "html", "title": "Broken", "renderer": "<script>bad()</script>"},
    )

    handled, status, body = _route_post(
        "/api/spaces/recovery/disable-widget",
        {"space_id": created["space_id"], "widget_id": "broken", "reason": "safe-mode disable"},
    )

    assert handled is None
    assert status == 200
    assert body["disabled"] is True
    assert body["widget_id"] == "broken"
    assert spaces.read_widget(created["space_id"], "broken")["recovery"]["disabled"] is True
    assert "bad()" not in json.dumps(body)


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
