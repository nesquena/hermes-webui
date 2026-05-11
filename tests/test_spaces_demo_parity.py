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
    events = spaces.list_widget_events(result["space"]["space_id"], "weather-current")

    assert result["action"] == "weather-observation-recorded"
    assert result["queued_event_count"] == 1
    assert events[0]["event_name"] == "widget.refresh"
    assert events[0]["status"] == "queued"
    assert events[0]["widget_id"] == "weather-current"
    assert events[0]["payload_summary"] == {"demo": "demo_weather_widget", "location": "Prague", "units": "metric"}
    assert result["prompt_flow"] == {
        "blank_space": True,
        "query": "What is the weather in Prague?",
        "chat_answer_status": "recorded",
        "answer_preview": "Prague is partly cloudy at 18 °C; the answer is now saved as safe widget metadata.",
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
    expected_notes_flow = {
        "folders_ready": True,
        "editor_saved": True,
        "markdown_preview_saved": True,
        "attachments_agent_mediated": True,
    }
    assert {key: result["notes_flow"].get(key) for key in expected_notes_flow} == expected_notes_flow
    assert result["notes_flow"]["folders_ready"] is True
    assert result["notes_flow"]["editor_saved"] is True
    assert result["notes_flow"]["markdown_preview_saved"] is True
    assert result["notes_flow"]["attachments_agent_mediated"] is True
    assert result["notes_flow"]["folder_count"] == 2
    assert result["notes_flow"]["active_folder"] == "Demo Project"
    assert result["notes_flow"]["attachment_count"] == 2
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


def test_music_demo_smoke_records_safe_sequencer_and_piano_roll_metadata(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    result = spaces.space_demo_run("demo_step_sequencer_piano_roll")
    sequencer = spaces.read_widget_detail(result["space"]["space_id"], "music-sequencer-grid")
    piano = spaces.read_widget_detail(result["space"]["space_id"], "music-piano-roll")
    events = spaces.list_widget_events(result["space"]["space_id"], "music-sequencer-grid")

    assert result["action"] == "music-pattern-seeded"
    assert result["queued_event_count"] == 1
    assert result["music_flow"] == {
        "sequencer_ready": True,
        "pattern_steps": 16,
        "piano_roll_ready": True,
        "webaudio_permission": "explicit-user-gesture",
        "cleanup": "planned-on-rerender",
    }
    assert sequencer["metadata"]["status"] == {"pattern": "demo-pattern-saved", "steps": "16"}
    assert sequencer["metadata"]["audio_policy"]["webaudio"] == "disabled-until-approved"
    assert piano["metadata"]["interaction"] == {"keyboard": "explicit-focus", "editing": "metadata-only"}
    assert events[0]["event_name"] == "audio.pattern.save"
    assert events[0]["status"] == "queued"
    assert events[0]["widget_id"] == "music-sequencer-grid"
    assert events[0]["payload_summary"] == {
        "demo": "demo_step_sequencer_piano_roll",
        "pattern_steps": "16",
        "target": "sequencer-and-piano-roll",
    }
    _assert_safe_payload(result)
    _assert_safe_payload({"sequencer": sequencer, "piano": piano, "events": events})


def test_local_service_demo_smoke_queues_health_check_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    result = spaces.space_demo_run("demo_local_agent_control_dashboard")
    health = spaces.read_widget_detail(result["space"]["space_id"], "service-health")
    events = spaces.list_widget_events(result["space"]["space_id"], "service-health")

    assert result["action"] == "local-service-dashboard-seeded"
    assert result["queued_event_count"] == 1
    assert result["service_flow"] == {
        "api_chat": "metadata-only",
        "browser_panel": "about:blank",
        "health_checks": "queued",
        "settings_review": "metadata-only",
        "network_mode": "explicit-approval",
    }
    assert health["metadata"]["refresh"]["status"] == "health-check-queued"
    assert events[0]["event_name"] == "service.status.check"
    assert events[0]["status"] == "queued"
    assert events[0]["widget_id"] == "service-health"
    assert events[0]["payload_summary"] == {"demo": "demo_local_agent_control_dashboard", "checks": ["/health", "api/status"]}
    _assert_safe_payload(result)
    _assert_safe_payload({"health": health, "events": events})


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


def test_time_travel_demo_smoke_records_restore_check_metadata_only(monkeypatch, tmp_path):
    spaces = _load_spaces(monkeypatch, tmp_path, enabled=True)

    result = spaces.space_demo_run("demo_time_travel_restore")
    detail = spaces.read_space_detail(result["space"]["space_id"])
    check = result["time_travel_restore_check"]
    timeline_ids = [event["event_id"] for event in spaces.list_revision_events(result["space"]["space_id"], limit=10)]

    assert result["action"] == "restored"
    assert check == {
        "patch_applied": True,
        "restored": True,
        "patch_cleared": True,
        "history_preserved": True,
        "return_to_present_preserved": True,
        "restored_widget_count": result["widget_count"],
    }
    assert result["widget_count"] == 1
    assert result["revision_event_count"] >= 3
    assert result["rollback_point"] is True
    assert len(timeline_ids) >= 3
    assert detail["widgets"][0]["title"] == "Weather in Prague"
    assert "smoke patch" not in json.dumps(result).lower()
    assert "smoke patch" not in json.dumps(detail).lower()
    _assert_safe_payload(result)
    _assert_safe_payload(detail)


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
