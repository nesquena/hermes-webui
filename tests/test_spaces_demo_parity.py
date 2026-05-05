import importlib
import json


DEMO_NAMES = [
    "demo_weather_widget",
    "demo_daily_dashboard",
    "demo_notes_app",
    "demo_camera_dashboard",
    "demo_local_agent_control_dashboard",
    "demo_browser_cocontrol_google_or_test_site",
    "demo_research_harness_pdf_export",
    "demo_kanban_board",
    "demo_stock_chart",
    "demo_snake_iterative_repair",
    "demo_step_sequencer_piano_roll",
    "demo_provider_setup",
    "demo_big_bang_onboarding",
    "demo_time_travel_restore",
    "demo_safe_admin_recovery",
]


UNSAFE_MARKERS = [
    "renderer",
    "<script",
    "</script",
    "javascript:",
    "onerror",
    "api_key",
    "token",
    "password",
    "secret",
    "authorization",
    "bearer",
    "cookie",
]


def _load_spaces(monkeypatch, tmp_path, enabled=True):
    import api.config as config

    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    if enabled:
        monkeypatch.setenv("HERMES_WEBUI_SPACES_ENABLED", "1")
    else:
        monkeypatch.delenv("HERMES_WEBUI_SPACES_ENABLED", raising=False)
    import api.spaces as spaces

    return importlib.reload(spaces)


def _assert_safe_payload(payload):
    serialized = json.dumps(payload, sort_keys=True).lower()
    for marker in UNSAFE_MARKERS:
        assert marker not in serialized


def test_demo_parity_catalog_covers_video_named_smokes(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    catalog = spaces.list_space_demo_runs()

    assert [item["demo"] for item in catalog] == DEMO_NAMES
    assert {item["mode"] for item in catalog} == {"metadata-only-smoke"}
    _assert_safe_payload(catalog)


def test_demo_parity_smoke_runner_launches_each_demo_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    results = [spaces.space_demo_run(name) for name in DEMO_NAMES]

    assert [result["demo"] for result in results] == DEMO_NAMES
    assert all(result["ok"] is True for result in results)
    assert all(result["space"]["space_id"] for result in results)
    assert all(result["widget_count"] >= 1 for result in results)
    assert all(result["rollback_point"] for result in results)
    assert all(result["persistence_checked"] is True for result in results)
    assert all(result["persisted_widget_count"] == result["widget_count"] for result in results)
    assert len(spaces.list_spaces()) == len(DEMO_NAMES)
    _assert_safe_payload(results)


def test_weather_demo_smoke_records_visible_weather_observation(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    result = spaces.space_demo_run("demo_weather_widget")
    detail = spaces.read_widget_detail(result["space"]["space_id"], "weather-current")

    assert result["action"] == "weather-observation-recorded"
    assert result["prompt_flow"] == {
        "blank_space": True,
        "query": "What is the weather in Prague?",
        "chat_answer_status": "recorded",
        "widget_request": "show it to me in a widget",
        "widget_created": True,
        "reload_verified": True,
        "network_mode": "agent-mediated",
    }
    assert result["weather_observation"]["widget"]["metadata"]["weather"]["status"] == "observation-ready"
    assert result["weather_observation"]["widget"]["metadata"]["weather"]["current"] == {
        "condition": "partly cloudy",
        "temperature_c": "18",
        "feels_like_c": "17",
    }
    assert detail["metadata"]["weather"]["location"] == "Prague"
    assert detail["metadata"]["weather"]["current"]["temperature_c"] == "18"
    assert detail["metadata"]["weather"]["summary"] == "Partly cloudy in Prague; refreshed through agent-mediated weather metadata."
    _assert_safe_payload(result)
    _assert_safe_payload(detail)


def test_notes_demo_smoke_saves_editable_note_preview_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    result = spaces.space_demo_run("demo_notes_app")
    editor = spaces.read_widget_detail(result["space"]["space_id"], "notes-editor")
    preview = spaces.read_widget_detail(result["space"]["space_id"], "notes-preview")

    assert result["action"] == "notes-draft-saved"
    assert result["notes_artifact"]["editor"]["metadata"]["notes"]["status"] == "draft-saved"
    assert result["notes_artifact"]["preview"]["metadata"]["notes"]["format"] == "markdown"
    assert editor["metadata"]["notes"]["body"] == "Demo note draft saved through typed Capy Spaces metadata."
    assert preview["metadata"]["notes"]["body"] == "# Demo note This markdown preview was saved as metadata-only state."
    _assert_safe_payload(result)
    _assert_safe_payload(editor)
    _assert_safe_payload(preview)


def test_kanban_demo_smoke_records_visible_board_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    result = spaces.space_demo_run("demo_kanban_board")
    backlog = spaces.read_widget_detail(result["space"]["space_id"], "kanban-backlog")
    doing = spaces.read_widget_detail(result["space"]["space_id"], "kanban-doing")
    done = spaces.read_widget_detail(result["space"]["space_id"], "kanban-done")

    assert result["action"] == "kanban-board-seeded"
    assert result["kanban_board"]["status"] == "board-ready"
    assert result["kanban_board"]["column_count"] == 3
    assert [column["metadata"]["kanban"]["column"] for column in result["kanban_board"]["columns"]] == [
        "Backlog",
        "Doing",
        "Done",
    ]
    assert backlog["metadata"]["kanban"]["cards"] == [
        {"id": "card-plan", "title": "Plan the first task", "status": "todo"}
    ]
    assert doing["metadata"]["kanban"]["cards"] == [
        {"id": "card-build", "title": "Build metadata-only board preview", "status": "doing"}
    ]
    assert done["metadata"]["kanban"]["cards"] == [
        {"id": "card-install", "title": "Install board template", "status": "done"}
    ]
    assert backlog["metadata"]["kanban"]["interaction"] == {"drag_drop": "planned", "edit_cards": "metadata-only"}
    _assert_safe_payload(result)
    _assert_safe_payload({"backlog": backlog, "doing": doing, "done": done})


def test_research_demo_smoke_advances_progress_artifact_pdf_export_and_rollback_check(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    result = spaces.space_demo_run("demo_research_harness_pdf_export")
    events = spaces.list_widget_events(result["space"]["space_id"], "research-summary")

    assert result["action"] == "pdf-export-requested"
    assert result["queued_event_count"] == 1
    assert result["research_progress"]["widgets"]["plan"]["metadata"]["status"]["phase"] == "summary"
    assert result["research_artifact"]["artifact"]["metadata_summary"]["export_pdf"] == "ready-for-user-request"
    assert result["research_rollback_check"]["verified"] is True
    assert result["research_rollback_check"]["restored_event_id"]
    assert result["research_rollback_check"]["replayed_after_restore"] is True
    assert result["research_rollback_check"]["restored_widget_count"] == 5
    assert events[0]["event_name"] == "widget.export.pdf"
    assert events[0]["status"] == "queued"
    assert events[0]["widget_id"] == "research-summary"
    _assert_safe_payload(result)
    _assert_safe_payload(events)


def test_demo_parity_smoke_runner_exposes_tool_adapter_action(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    listed = spaces.run_space_tool("space.demo.list", {})
    ran = spaces.run_space_tool(
        "space.demo.run",
        {"demo": "demo_weather_widget", "renderer": "<script>bad()</script>", "api_key": "UNTRUSTED_VALUE"},
    )

    assert listed["ok"] is True
    assert listed["demos"][0]["demo"] == "demo_weather_widget"
    assert ran["ok"] is True
    assert ran["demo"] == "demo_weather_widget"
    assert ran["template"] == "weather"
    _assert_safe_payload({"listed": listed, "ran": ran})


def test_demo_parity_smoke_runner_exposes_run_all_tool_action(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    result = spaces.run_space_tool(
        "space.demo.run_all",
        {"renderer": "<script>bad()</script>", "api_key": "UNTRUSTED_VALUE"},
    )

    assert result["ok"] is True
    assert result["action"] == "space.demo.run_all"
    assert result["total"] == len(DEMO_NAMES)
    assert result["passed"] == len(DEMO_NAMES)
    assert result["failed"] == 0
    assert [item["demo"] for item in result["results"]] == DEMO_NAMES
    assert all(item["ok"] is True for item in result["results"])
    assert all(item["rollback_point"] is True for item in result["results"])
    assert all(item["persistence_checked"] is True for item in result["results"])
    assert all(item["persisted_widget_count"] == item["widget_count"] for item in result["results"])
    assert len(spaces.list_spaces()) == len(DEMO_NAMES)
    _assert_safe_payload(result)
