import json
import time
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]


def test_pet_attention_uses_latest_visible_assistant_segment(monkeypatch):
    import api.pet_routes as pet_routes
    import api.run_journal

    def fake_read_run_events(session_id, run_id):
        assert session_id == "sid-1"
        assert run_id == "run-1"
        return {
            "events": [
                {"event": "token", "payload": {"text": "贡献规则确认："}},
                {"event": "token", "payload": {"text": "PR 必须六段 body。"}},
                {
                    "event": "interim_assistant",
                    "payload": {
                        "text": "贡献规则确认：PR 必须六段 body。",
                        "already_streamed": True,
                    },
                },
                {"event": "tool", "payload": {"name": "shell"}},
                {"event": "token", "payload": {"text": "验证结果对了："}},
                {"event": "token", "payload": {"text": "同一个 active run 现在返回的是最新 interim 人话，而不是旧消息。"}},
                {"event": "token", "payload": {"text": "第二句也保留，交给气泡两行省略。"}},
            ]
        }

    monkeypatch.setattr(api.run_journal, "read_run_events", fake_read_run_events)

    text = pet_routes._pet_latest_visible_assistant_process_text({
        "session_id": "sid-1",
        "active_stream_id": "run-1",
        "is_streaming": True,
    })

    assert text == "验证结果对了：同一个 active run 现在返回的是最新 interim 人话，而不是旧消息。第二句也保留，交给气泡两行省略。"
    assert "贡献规则确认" not in text


def test_pet_attention_stale_stream_cleanup_is_display_only(monkeypatch):
    import api.pet_routes as pet_routes

    class FakeStreamLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    rows = [
        {
            "session_id": "sid-stale",
            "active_stream_id": "missing-run",
            "is_streaming": False,
            "pending_user_message": "queued",
            "has_pending_user_message": True,
        }
    ]
    monkeypatch.setattr(pet_routes, "STREAMS", {})
    monkeypatch.setattr(pet_routes, "STREAMS_LOCK", FakeStreamLock())

    display_rows = pet_routes._display_rows_without_stale_pet_streams(rows)

    assert display_rows == [
        {
            "session_id": "sid-stale",
            "active_stream_id": None,
            "is_streaming": False,
            "pending_user_message": None,
            "has_pending_user_message": False,
        }
    ]
    assert rows[0]["active_stream_id"] == "missing-run"
    assert rows[0]["pending_user_message"] == "queued"


def test_pet_message_text_ignores_user_messages_for_final_bubbles(monkeypatch):
    import api.pet_routes as pet_routes

    class FakeSession:
        messages = [
            {"role": "user", "content": "用户刚刚发的消息不应该出现"},
            {"role": "assistant", "content": "Cerebras System 是什么。第二句也应该交给气泡省略。"},
        ]

    monkeypatch.setattr(pet_routes.Session, "load", staticmethod(lambda sid: FakeSession()))

    text = pet_routes._pet_latest_assistant_final_text("sid-1")
    assert text.startswith("Cerebras System 是什么")
    assert "用户刚刚发的消息" not in text


def test_pet_attention_marks_pending_approval_action_required(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(
        pet_routes,
        "_pet_pending_approval",
        lambda sid: ({
            "description": "Dangerous command detected",
            "command": "rm -rf /tmp/test",
            "approval_id": "approval-123",
        }, 2),
    )
    monkeypatch.setattr(pet_routes, "_pet_pending_clarify", lambda sid: (None, 0))

    item = pet_routes._pet_attention_session({
        "session_id": "sid-approval",
        "display_title": "Needs approval",
        "is_streaming": True,
        "message_count": 4,
    })

    assert item["action_required"] is True
    assert item["action_required_type"] == "approval"
    assert item["action_required_key"] == "approval:approval-123"
    assert item["action_required_count"] == 2
    assert item["action_required_command"] == "rm -rf /tmp/test"
    assert item["action_required_description"] == "Dangerous command detected"
    assert item["action_required_approval_id"] == "approval-123"
    assert item["action_required_choices"] == []
    assert item["action_required_clarify_id"] == ""
    assert item["process_text"] == "Dangerous command detected: rm -rf /tmp/test"


def test_pet_attention_truncates_long_action_required_copy(monkeypatch):
    import api.pet_routes as pet_routes

    long_command = "run " + ("very-long-argument-" * 30)
    monkeypatch.setattr(
        pet_routes,
        "_pet_pending_approval",
        lambda sid: ({"description": "Dangerous command detected", "command": long_command}, 1),
    )
    monkeypatch.setattr(pet_routes, "_pet_pending_clarify", lambda sid: (None, 0))

    item = pet_routes._pet_attention_session({
        "session_id": "sid-long-approval",
        "display_title": "Needs approval",
        "is_streaming": True,
        "message_count": 4,
    })

    assert item["process_text"].startswith("Dangerous command detected:")
    assert item["process_text"].endswith("...")
    assert len(item["process_text"]) <= pet_routes._PET_ACTION_TEXT_MAX_CHARS


def test_pet_attention_marks_pending_clarify_action_required(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes, "_pet_pending_approval", lambda sid: (None, 0))
    monkeypatch.setattr(
        pet_routes,
        "_pet_pending_clarify",
        lambda sid: ({
            "question": "Which environment should I deploy this to?",
            "choices_offered": ["Staging", "Production", "Cancel"],
            "clarify_id": "clarify-123",
        }, 1),
    )

    item = pet_routes._pet_attention_session({
        "session_id": "sid-clarify",
        "display_title": "Needs choice",
        "is_streaming": True,
        "message_count": 4,
    })

    assert item["action_required"] is True
    assert item["action_required_type"] == "clarify"
    assert item["action_required_key"] == "clarify:clarify-123"
    assert item["action_required_choices"] == ["Staging", "Production", "Cancel"]
    assert item["action_required_clarify_id"] == "clarify-123"
    assert item["action_required_command"] == ""
    assert item["action_required_approval_id"] == ""
    assert item["process_text"] == "Which environment should I deploy this to?"


def test_pet_action_required_prefers_stable_ids_for_dismiss_key(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(
        pet_routes,
        "_pet_pending_approval",
        lambda sid: ({"approval_id": "approval-123", "description": "Approve deploy"}, 1),
    )
    monkeypatch.setattr(pet_routes, "_pet_pending_clarify", lambda sid: (None, 0))

    item = pet_routes._pet_attention_session({"session_id": "sid-approval", "display_title": "Needs approval"})

    assert item["action_required_key"] == "approval:approval-123"


def test_pet_clarify_dismiss_key_changes_when_question_changes(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes, "_pet_pending_approval", lambda sid: (None, 0))
    monkeypatch.setattr(pet_routes, "_pet_pending_clarify", lambda sid: ({"question": "First?", "choices": ["A", "B"]}, 1))
    first = pet_routes._pet_attention_session({"session_id": "sid-clarify", "display_title": "Needs choice"})["action_required_key"]

    monkeypatch.setattr(pet_routes, "_pet_pending_clarify", lambda sid: ({"question": "Second?", "choices": ["A", "B"]}, 1))
    second = pet_routes._pet_attention_session({"session_id": "sid-clarify", "display_title": "Needs choice"})["action_required_key"]

    assert first.startswith("clarify:sid-clarify:")
    assert second.startswith("clarify:sid-clarify:")
    assert first != second


def test_pet_attention_hides_external_sessions_when_setting_is_off(monkeypatch):
    import api.pet_routes as pet_routes

    class Parsed:
        query = ""

    rows = [
        {
            "session_id": "webui-1",
            "display_title": "WebUI",
            "profile": "default",
            "message_count": 1,
        },
        {
            "session_id": "telegram-1",
            "display_title": "Telegram",
            "profile": "default",
            "message_count": 2,
            "is_cli_session": True,
            "session_source": "messaging",
            "source_tag": "telegram",
            "source_label": "Telegram",
        },
        {
            "session_id": "cli-1",
            "display_title": "CLI",
            "profile": "default",
            "message_count": 3,
            "is_cli_session": True,
            "session_source": "cli",
            "source_tag": "cli",
            "source_label": "CLI",
        },
    ]
    monkeypatch.setattr(pet_routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(pet_routes, "load_settings", lambda: {"show_cli_sessions": False})

    filtered, _profile = pet_routes._filter_pet_attention_rows(rows, Parsed())

    assert [row["session_id"] for row in filtered] == ["webui-1"]


def test_pet_attention_respects_external_visibility_setting(monkeypatch):
    import api.pet_routes as pet_routes

    class Parsed:
        query = ""

    rows = [
        {
            "session_id": "telegram-1",
            "display_title": "Telegram",
            "profile": "default",
            "message_count": 2,
            "actual_user_message_count": 2,
            "is_cli_session": True,
            "session_source": "messaging",
            "source_tag": "telegram",
            "source_label": "Telegram",
        },
    ]
    monkeypatch.setattr(pet_routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(pet_routes, "load_settings", lambda: {"show_cli_sessions": True})

    filtered, _profile = pet_routes._filter_pet_attention_rows(rows, Parsed())

    assert [row["session_id"] for row in filtered] == ["telegram-1"]


def test_pet_preference_persists_through_shared_settings(monkeypatch):
    import api.pet_routes as pet_routes

    saved = []

    monkeypatch.setattr(
        pet_routes,
        "load_settings",
        lambda: {"show_cli_sessions": True, "desktop_pet_enabled": False},
    )
    monkeypatch.setattr(
        pet_routes,
        "save_settings",
        lambda payload: saved.append(dict(payload)) or dict(payload),
    )

    assert pet_routes._desktop_pet_preference_payload() == {
        "ok": True,
        "enabled": False,
        "configured": True,
    }

    updated = pet_routes._set_desktop_pet_preference_enabled(True)

    assert updated == {"ok": True, "enabled": True, "configured": True}
    assert saved == [{"desktop_pet_enabled": True}]


def test_pet_attention_ready_uses_webui_unread_state(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes, "_pet_pending_approval", lambda sid: (None, 0))
    monkeypatch.setattr(pet_routes, "_pet_pending_clarify", lambda sid: (None, 0))
    monkeypatch.setattr(pet_routes, "_pet_latest_assistant_final_text", lambda sid: "Final assistant reply")

    row = {"session_id": "sid-ready", "display_title": "Ready", "message_count": 5, "last_message_at": 10}
    idle = pet_routes._pet_attention_session(row, {"viewed_counts": {"sid-ready": 5}, "completion_unread": {}})
    ready = pet_routes._pet_attention_session(row, {"viewed_counts": {"sid-ready": 4}, "completion_unread": {}})
    completion_unread = pet_routes._pet_attention_session(row, {"viewed_counts": {"sid-ready": 5}, "completion_unread": {"sid-ready": {"message_count": 5}}})

    assert idle["status"] == "idle"
    assert ready["status"] == "ready"
    assert ready["process_text"] == "Final assistant reply"
    assert completion_unread["status"] == "ready"


def test_pet_attention_ready_uses_recent_completion_fallback(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes, "_pet_pending_approval", lambda sid: (None, 0))
    monkeypatch.setattr(pet_routes, "_pet_pending_clarify", lambda sid: (None, 0))
    monkeypatch.setattr(pet_routes, "_pet_latest_assistant_final_text", lambda sid: "Final assistant reply")

    pet_routes._PET_ATTENTION_COMPLETED_STATE.clear()
    try:
        pet_routes._PET_ATTENTION_COMPLETED_STATE["sid-ready"] = {"ready_at": time.time(), "message_count": 5}
        row = {"session_id": "sid-ready", "display_title": "Ready", "message_count": 5, "last_message_at": 10}
        result = pet_routes._pet_attention_session(
            row, {"viewed_counts": {}, "completion_unread": {}}
        )
        assert result["status"] == "ready"
    finally:
        pet_routes._PET_ATTENTION_COMPLETED_STATE.clear()


def test_pet_attention_running_is_displayed_without_unread_state(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes, "_pet_pending_approval", lambda sid: (None, 0))
    monkeypatch.setattr(pet_routes, "_pet_pending_clarify", lambda sid: (None, 0))
    monkeypatch.setattr(pet_routes, "_pet_latest_visible_assistant_process_text", lambda session: "")

    item = pet_routes._pet_attention_session({
        "session_id": "sid-running",
        "display_title": "Run",
        "is_streaming": True,
        "started_at": 1234.5,
    })

    assert item["status"] == "running"
    assert item["started_at"] == 1234.5
    assert item["process_text"] == ""


def test_pet_attention_ready_fallback_does_not_override_viewed_count(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes, "_pet_pending_approval", lambda sid: (None, 0))
    monkeypatch.setattr(pet_routes, "_pet_pending_clarify", lambda sid: (None, 0))

    pet_routes._PET_ATTENTION_COMPLETED_STATE.clear()
    try:
        pet_routes._PET_ATTENTION_COMPLETED_STATE["sid-ready"] = {
            "ready_at": time.time(),
            "message_count": 5,
        }
        row = {"session_id": "sid-ready", "display_title": "Ready", "message_count": 5, "last_message_at": 10}
        result = pet_routes._pet_attention_session(
            row,
            {"viewed_counts": {"sid-ready": 5}, "completion_unread": {}},
        )
        assert result["status"] == "idle"
    finally:
        pet_routes._PET_ATTENTION_COMPLETED_STATE.clear()


def test_pet_attention_ready_fallback_expires(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes, "_pet_pending_approval", lambda sid: (None, 0))
    monkeypatch.setattr(pet_routes, "_pet_pending_clarify", lambda sid: (None, 0))
    row = {"session_id": "sid-expired", "display_title": "Ready", "message_count": 5, "last_message_at": 10}

    pet_routes._PET_ATTENTION_COMPLETED_STATE.clear()
    try:
        stale_ts = time.time() - pet_routes._PET_COMPLETION_FALLBACK_SECONDS - 1
        pet_routes._PET_ATTENTION_COMPLETED_STATE["sid-expired"] = {"ready_at": stale_ts, "message_count": 5}
        item = pet_routes._pet_attention_session(
            row,
            {"viewed_counts": {"sid-expired": 5}, "completion_unread": {}},
        )
        assert item["status"] == "idle"
        assert "sid-expired" not in pet_routes._PET_ATTENTION_COMPLETED_STATE
    finally:
        pet_routes._PET_ATTENTION_COMPLETED_STATE.clear()


def test_pet_bundled_skins_and_default_order():
    import api.pet_routes as pet_routes

    courier_manifest = json.loads((ROOT / "static" / "pets" / "courier" / "pet.json").read_text(encoding="utf-8"))
    keeper_manifest = json.loads((ROOT / "static" / "pets" / "keeper" / "pet.json").read_text(encoding="utf-8"))
    shiba_manifest = json.loads((ROOT / "static" / "pets" / "shiba" / "pet.json").read_text(encoding="utf-8"))
    skins = pet_routes._available_pet_skins()

    assert courier_manifest["id"] == "courier"
    assert courier_manifest["displayName"] == "Courier Bot"
    assert keeper_manifest["id"] == "keeper"
    assert keeper_manifest["displayName"] == "May"
    assert shiba_manifest["id"] == "shiba"
    assert shiba_manifest["displayName"] == "shiba"
    assert (ROOT / "static" / "pets" / "courier" / "spritesheet.webp").stat().st_size > 100_000
    assert (ROOT / "static" / "pets" / "keeper" / "spritesheet.webp").stat().st_size > 100_000
    assert (ROOT / "static" / "pets" / "shiba" / "spritesheet.webp").stat().st_size > 100_000
    assert skins[0]["id"] == "keeper"
    assert skins[0]["layout"]["columns"] == 8
    assert skins[0]["layout"]["rows"] == 9
    assert skins[0]["layout"]["frameWidth"] == 192
    assert skins[0]["layout"]["frameHeight"] == 208
    assert skins[0]["layout"]["states"][0] == {"name": "idle", "row": 0, "frames": 6}
    assert {skin["id"] for skin in skins} >= {"courier", "keeper", "shiba"}
    assert pet_routes.DEFAULT_PET_SKIN_ID == "keeper"


def test_pet_routes_are_owned_by_pet_module_and_thin_dispatched():
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    pet_routes = (ROOT / "api" / "pet_routes.py").read_text(encoding="utf-8")

    assert "from api import pet_routes" in routes
    assert "pet_routes.handle_get(handler, parsed)" in routes
    assert "pet_routes.handle_post(handler, parsed, body)" in routes
    assert "def _handle_pet_attention" not in routes
    assert "def _available_pet_skins" not in routes
    assert "def _handle_pet_open_session" not in routes
    assert "def _handle_pet_attention" in pet_routes
    assert "def _available_pet_skins" in pet_routes
    assert "def _handle_pet_navigation" in pet_routes
    assert "def _handle_pet_open_session" in pet_routes
    assert "def _handle_pet_status" in pet_routes
    assert "def _handle_pet_launch" in pet_routes
    assert "def _handle_pet_close" in pet_routes
    assert "DEFAULT_PET_SKIN_ID = \"keeper\"" in pet_routes
    assert "process_text = \"正在思考\"" not in pet_routes
    assert "请审批此会话" not in pet_routes
    assert "需要批准" not in pet_routes
    assert "请处理这个会话" not in pet_routes
    assert "需要选择" not in pet_routes
    assert "message.get(\"reasoning\")" not in pet_routes


def test_pet_navigation_command_is_queued(monkeypatch):
    import api.pet_routes as pet_routes

    class Handler:
        headers = {"Host": "127.0.0.1:8787"}

    monkeypatch.setattr(pet_routes, "get_session", lambda sid, metadata_only=False: object())
    pet_routes._PET_NAVIGATION_COMMANDS.clear()

    command = pet_routes._queue_pet_session_navigation(
        Handler(),
        {"session_id": "sid-1", "draft": "继续", "autosend": True},
    )

    assert command["session_id"] == "sid-1"
    assert command["draft"] == "继续"
    assert command["autosend"] is True
    assert command["url"] == "http://127.0.0.1:8787/session/sid-1?draft=%E7%BB%A7%E7%BB%AD"
    assert pet_routes._PET_NAVIGATION_COMMANDS == [command]


def test_pet_navigation_commands_are_fifo(monkeypatch):
    import api.pet_routes as pet_routes

    class Handler:
        headers = {"Host": "127.0.0.1:8787"}

    monkeypatch.setattr(pet_routes, "get_session", lambda sid, metadata_only=False: object())
    pet_routes._PET_NAVIGATION_COMMANDS.clear()

    first = pet_routes._queue_pet_session_navigation(Handler(), {"session_id": "sid-1"})
    second = pet_routes._queue_pet_session_navigation(Handler(), {"session_id": "sid-2"})

    with pet_routes._PET_NAVIGATION_LOCK:
        assert pet_routes._next_pet_navigation_command_locked("")["id"] == first["id"]
        assert pet_routes._next_pet_navigation_command_locked(first["id"])["id"] == second["id"]
        assert pet_routes._next_pet_navigation_command_locked(second["id"]) == {}


def test_pet_open_session_reuses_existing_webui_url(monkeypatch):
    import api.pet_routes as pet_routes

    class Handler:
        headers = {"Host": "127.0.0.1:8787"}

    reused = []
    monkeypatch.setattr(pet_routes, "get_session", lambda sid, metadata_only=False: object())
    monkeypatch.setattr(pet_routes.sys, "platform", "darwin")
    monkeypatch.setattr(pet_routes, "_reuse_existing_pet_browser_tab", lambda url: reused.append(url) or True)
    # Reuse only happens when the bridge is NOT live; simulate a cold start.
    monkeypatch.setattr(pet_routes, "_pet_bridge_recently_polled", lambda: False)
    pet_routes._PET_NAVIGATION_COMMANDS.clear()

    command = pet_routes._queue_and_focus_pet_session_navigation(
        Handler(),
        {"session_id": "sid-1", "draft": "继续", "autosend": True},
    )

    assert command["session_id"] == "sid-1"
    assert command["focused"] is True
    assert command["reused"] is True
    assert reused == ["http://127.0.0.1:8787/session/sid-1?draft=%E7%BB%A7%E7%BB%AD"]
    assert pet_routes._PET_NAVIGATION_COMMANDS == [command]


def test_queue_and_focus_skips_reuse_when_bridge_is_live(monkeypatch):
    """When a live WebUI tab is polling the bridge, _queue_and_focus must NOT
    call _reuse_existing_pet_browser_tab.  The bridge will handle in-page
    loadSession() — no hard URL change, no full page reload."""
    import api.pet_routes as pet_routes

    class Handler:
        headers = {"Host": "127.0.0.1:8787"}

    reused = []
    monkeypatch.setattr(pet_routes, "get_session", lambda sid, metadata_only=False: object())
    monkeypatch.setattr(pet_routes.sys, "platform", "darwin")
    monkeypatch.setattr(pet_routes, "_reuse_existing_pet_browser_tab", lambda url: reused.append(url) or True)
    # Bridge is live — reuse must be skipped.
    monkeypatch.setattr(pet_routes, "_pet_bridge_recently_polled", lambda: True)
    pet_routes._PET_NAVIGATION_COMMANDS.clear()

    command = pet_routes._queue_and_focus_pet_session_navigation(
        Handler(),
        {"session_id": "sid-live"},
    )

    # _reuse_existing_pet_browser_tab must not have been called.
    assert reused == []
    assert command["reused"] is False
    assert command["focused"] is False
    assert command["session_id"] == "sid-live"


def test_pet_open_session_reused_skips_ack_and_fallback(monkeypatch):
    import api.pet_routes as pet_routes

    class WFile:
        def __init__(self):
            self.body = b""

        def write(self, value):
            self.body += value

    class Handler:
        client_address = ("127.0.0.1", 12345)
        headers = {"Host": "127.0.0.1:8787"}

        def __init__(self):
            self.status = None
            self.headers_sent = []
            self.wfile = WFile()

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.headers_sent.append((key, value))

        def end_headers(self):
            pass

    command = {
        "id": "nav-1",
        "session_id": "sid-1",
        "url": "http://127.0.0.1:8787/session/sid-1",
        "reused": True,
        "focused": True,
    }
    opened = []
    waited = []
    monkeypatch.setattr(pet_routes, "_queue_and_focus_pet_session_navigation", lambda handler, body: dict(command))
    monkeypatch.setattr(pet_routes, "_wait_for_pet_navigation_ack", lambda command_id: waited.append(command_id) or True)
    monkeypatch.setattr(pet_routes, "_fallback_open_pet_browser_url", lambda url: opened.append(url) or True)

    handler = Handler()
    pet_routes._handle_pet_open_session(handler, {"session_id": "sid-1", "draft": "hello"})
    payload = json.loads(handler.wfile.body.decode("utf-8"))

    assert handler.status == 200
    assert payload["reused"] is True
    # A reused tab is already navigated + frontmost, so the handler must not
    # block on the bridge ack (the old ~1.6s spinner tail) or double-open a
    # fallback browser window.
    assert waited == []
    assert opened == []
    assert payload["consumed"] is False
    assert payload["opened"] is False
    assert payload["focused"] is True


def test_pet_open_session_cold_start_skips_ack_wait_when_no_live_bridge(monkeypatch):
    import api.pet_routes as pet_routes

    class WFile:
        def __init__(self):
            self.body = b""

        def write(self, value):
            self.body += value

    class Handler:
        client_address = ("127.0.0.1", 12345)
        headers = {"Host": "127.0.0.1:8787"}

        def __init__(self):
            self.status = None
            self.wfile = WFile()

        def send_response(self, status):
            self.status = status

        def send_header(self, _key, _value):
            pass

        def end_headers(self):
            pass

    command = {
        "id": "nav-cold",
        "session_id": "sid-cold",
        "url": "http://127.0.0.1:8787/session/sid-cold",
        "reused": False,
        "focused": False,
    }
    waited = []
    opened = []
    monkeypatch.setattr(pet_routes, "_queue_and_focus_pet_session_navigation", lambda handler, body: dict(command))
    # No WebUI tab has polled the bridge recently (closed tab / browser).
    monkeypatch.setattr(pet_routes, "_pet_bridge_recently_polled", lambda: False)
    monkeypatch.setattr(pet_routes, "_wait_for_pet_navigation_ack", lambda command_id: waited.append(command_id) or True)
    monkeypatch.setattr(pet_routes, "_fallback_open_pet_browser_url", lambda url: opened.append(url) or True)

    handler = Handler()
    pet_routes._handle_pet_open_session(handler, {"session_id": "sid-cold"})
    payload = json.loads(handler.wfile.body.decode("utf-8"))

    assert handler.status == 200
    # With no live bridge, do not burn the ack timeout — open a fresh tab now.
    assert waited == []
    assert opened == ["http://127.0.0.1:8787/session/sid-cold"]
    assert payload["consumed"] is False
    assert payload["opened"] is True


def test_pet_open_session_falls_back_to_reuse_when_bridge_ack_times_out(monkeypatch):
    """When the bridge is live but ack times out (e.g. poll interval missed),
    the handler must fall back to _reuse_existing_pet_browser_tab so the user
    sees navigation immediately instead of a silent failure."""
    import api.pet_routes as pet_routes

    class WFile:
        def __init__(self):
            self.body = b""

        def write(self, value):
            self.body += value

    class Handler:
        client_address = ("127.0.0.1", 12345)
        headers = {"Host": "127.0.0.1:8787"}

        def __init__(self):
            self.status = None
            self.wfile = WFile()

        def send_response(self, status):
            self.status = status

        def send_header(self, _key, _value):
            pass

        def end_headers(self):
            pass

    command = {
        "id": "nav-timeout",
        "session_id": "sid-timeout",
        "url": "http://127.0.0.1:8787/session/sid-timeout",
        "reused": False,
        "focused": False,
    }
    reused_late = []
    opened = []
    monkeypatch.setattr(pet_routes.sys, "platform", "darwin")
    monkeypatch.setattr(pet_routes, "_queue_and_focus_pet_session_navigation", lambda handler, body: dict(command))
    monkeypatch.setattr(pet_routes, "_pet_bridge_recently_polled", lambda: True)
    # Bridge ack times out (returns False).
    monkeypatch.setattr(pet_routes, "_wait_for_pet_navigation_ack", lambda command_id: False)
    # Fallback reuse is called with the session URL.
    monkeypatch.setattr(
        pet_routes,
        "_reuse_existing_pet_browser_tab",
        lambda url: reused_late.append(url) or True,
    )
    monkeypatch.setattr(pet_routes, "_fallback_open_pet_browser_url", lambda url: opened.append(url) or True)

    handler = Handler()
    pet_routes._handle_pet_open_session(handler, {"session_id": "sid-timeout"})
    payload = json.loads(handler.wfile.body.decode("utf-8"))

    assert handler.status == 200
    assert payload["consumed"] is False
    # Hard URL reuse is the fallback — tab was navigated and brought to front.
    assert reused_late == ["http://127.0.0.1:8787/session/sid-timeout"]
    assert payload["reused"] is True
    assert payload["focused"] is True
    # No extra fallback open (reuse already handled it).
    assert opened == []
    assert payload["opened"] is False


def test_pet_open_session_plain_click_uses_bridge_focus_and_ack(monkeypatch):
    import api.pet_routes as pet_routes

    class WFile:
        def __init__(self):
            self.body = b""

        def write(self, value):
            self.body += value

    class Handler:
        client_address = ("127.0.0.1", 12345)
        headers = {"Host": "127.0.0.1:8787"}

        def __init__(self):
            self.status = None
            self.wfile = WFile()

        def send_response(self, status):
            self.status = status

        def send_header(self, _key, _value):
            pass

        def end_headers(self):
            pass

    command = {
        "id": "plain-nav",
        "session_id": "sid-fast",
        "url": "http://127.0.0.1:8787/session/sid-fast",
        "draft": "",
        "autosend": False,
    }
    focused = []
    waited = []
    monkeypatch.setattr(
        pet_routes,
        "_queue_pet_session_navigation",
        lambda handler, body: (_ for _ in ()).throw(AssertionError("plain click must use bridge-aware focus")),
    )
    monkeypatch.setattr(
        pet_routes,
        "_queue_and_focus_pet_session_navigation",
        lambda handler, body: focused.append(body) or {**command, "reused": False, "focused": True},
    )
    monkeypatch.setattr(pet_routes, "_pet_bridge_recently_polled", lambda: True)
    monkeypatch.setattr(
        pet_routes,
        "_wait_for_pet_navigation_ack",
        lambda command_id: waited.append(command_id) or True,
    )
    monkeypatch.setattr(
        pet_routes,
        "_fallback_open_pet_browser_url",
        lambda url: (_ for _ in ()).throw(AssertionError("acked bridge navigation must not open a fallback URL")),
    )

    handler = Handler()
    pet_routes._handle_pet_open_session(handler, {"session_id": "sid-fast"})
    payload = json.loads(handler.wfile.body.decode("utf-8"))

    assert handler.status == 200
    assert payload["opened"] is False
    assert payload["consumed"] is True
    assert payload["reused"] is False
    assert payload["focused"] is True
    assert focused == [{"session_id": "sid-fast"}]
    assert waited == ["plain-nav"]


def test_pet_open_session_skips_fallback_when_bridge_acks(monkeypatch):
    import api.pet_routes as pet_routes

    class WFile:
        def __init__(self):
            self.body = b""

        def write(self, value):
            self.body += value

    class Handler:
        client_address = ("127.0.0.1", 12345)
        headers = {"Host": "127.0.0.1:8787"}

        def __init__(self):
            self.status = None
            self.wfile = WFile()

        def send_response(self, status):
            self.status = status

        def send_header(self, _key, _value):
            pass

        def end_headers(self):
            pass

    command = {
        "id": "nav-hidden-ack",
        "session_id": "sid-visible",
        "url": "http://127.0.0.1:8787/session/sid-visible",
        "reused": False,
        "focused": False,
    }
    focused = []
    title_focused = []
    monkeypatch.setattr(pet_routes.sys, "platform", "darwin")
    monkeypatch.setattr(pet_routes, "_queue_and_focus_pet_session_navigation", lambda handler, body: dict(command))
    monkeypatch.setattr(pet_routes, "_pet_bridge_recently_polled", lambda: True)
    monkeypatch.setattr(pet_routes, "_wait_for_pet_navigation_ack", lambda command_id: True)
    monkeypatch.setattr(pet_routes, "_focus_existing_pet_browser_tab", lambda url: focused.append(url) or True)
    monkeypatch.setattr(pet_routes, "_focus_existing_pet_browser_window_by_title", lambda: title_focused.append(True) or True)
    monkeypatch.setattr(
        pet_routes,
        "_fallback_open_pet_browser_url",
        lambda url: (_ for _ in ()).throw(AssertionError("bridge ack should prevent fallback browser open")),
    )

    handler = Handler()
    pet_routes._handle_pet_open_session(handler, {"session_id": "sid-visible", "draft": "hello"})
    payload = json.loads(handler.wfile.body.decode("utf-8"))

    assert handler.status == 200
    assert payload["consumed"] is True
    assert payload["opened"] is False
    assert payload["focused"] is True
    if pet_routes.sys.platform == "darwin":
        # The tab-switching focus runs first (it surfaces the correct session
        # tab); the title-based window raise is only a fallback.
        assert focused == ["http://127.0.0.1:8787/session/sid-visible"]
        assert title_focused == []


def test_pet_open_session_foregrounds_app_when_appleScript_focus_fails(monkeypatch):
    """When bridge acks but both AppleScript focus helpers fail, _foreground_pet_browser_app
    must be called as a fallback.  It does not require Automation permission, so it
    succeeds even when the AppleScript paths return False.
    """
    import api.pet_routes as pet_routes

    class WFile:
        def __init__(self):
            self.body = b""

        def write(self, value):
            self.body += value

    class Handler:
        client_address = ("127.0.0.1", 12345)
        headers = {"Host": "127.0.0.1:8787"}

        def __init__(self):
            self.status = None
            self.wfile = WFile()

        def send_response(self, status):
            self.status = status

        def send_header(self, _key, _value):
            pass

        def end_headers(self):
            pass

    command = {
        "id": "nav-noapple",
        "session_id": "sid-noapple",
        "url": "http://127.0.0.1:8787/session/sid-noapple",
        "reused": False,
        "focused": False,
    }
    foreground_calls = []
    monkeypatch.setattr(pet_routes.sys, "platform", "darwin")
    monkeypatch.setattr(pet_routes, "_queue_and_focus_pet_session_navigation", lambda handler, body: dict(command))
    monkeypatch.setattr(pet_routes, "_pet_bridge_recently_polled", lambda: True)
    monkeypatch.setattr(pet_routes, "_wait_for_pet_navigation_ack", lambda command_id: True)
    # Both AppleScript helpers fail (e.g. Automation permission not granted).
    monkeypatch.setattr(pet_routes, "_focus_existing_pet_browser_window_by_title", lambda: False)
    monkeypatch.setattr(pet_routes, "_focus_existing_pet_browser_tab", lambda url: False)
    # open(1)-based fallback should be tried and succeeds.
    monkeypatch.setattr(pet_routes, "_foreground_pet_browser_app", lambda: foreground_calls.append(True) or True)
    # _fallback_open_pet_browser_url must NOT be called (consumed=True skips it).
    monkeypatch.setattr(
        pet_routes,
        "_fallback_open_pet_browser_url",
        lambda url: (_ for _ in ()).throw(AssertionError("bridge-acked navigation must not open a fallback URL")),
    )

    handler = Handler()
    pet_routes._handle_pet_open_session(handler, {"session_id": "sid-noapple"})
    payload = json.loads(handler.wfile.body.decode("utf-8"))

    assert handler.status == 200
    assert payload["consumed"] is True
    assert payload["opened"] is False
    # _foreground_pet_browser_app filled in for ack_focused.
    assert payload["focused"] is True
    assert foreground_calls == [True]


def test_pet_browser_automation_block_short_circuits_osascript_probes(monkeypatch):
    """Once an osascript probe reports a permission error, the AppleScript browser
    helpers must short-circuit instead of re-probing every browser on each click.
    This bounds the click->open latency when macOS Automation permission is denied.
    """
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes.sys, "platform", "darwin")
    # Start from a clean slate where Automation is presumed available.
    pet_routes._pet_record_automation_result(True)

    calls = []

    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "execution error: Not authorized to send Apple events to System Events. (-1743)"

    def fake_run(args, **kwargs):
        calls.append(args)
        return FakeResult()

    monkeypatch.setattr(pet_routes.subprocess, "run", fake_run)

    url = "http://127.0.0.1:8787/session/sid-x"
    # First attempt runs osascript (>=1 call) and records the permission block.
    assert pet_routes._reuse_existing_pet_browser_tab(url) is False
    first_count = len(calls)
    assert first_count >= 1
    assert pet_routes._pet_automation_maybe_available() is False

    # Subsequent AppleScript helpers must NOT spawn any more osascript probes.
    assert pet_routes._reuse_existing_pet_browser_tab(url) is False
    assert pet_routes._focus_existing_pet_browser_tab(url) is False
    assert pet_routes._focus_existing_pet_browser_window_by_title() is False
    assert len(calls) == first_count

    # Restore the cache so later tests are unaffected.
    pet_routes._pet_record_automation_result(True)


def test_pet_navigation_ack_skips_consumed_commands(monkeypatch):
    import api.pet_routes as pet_routes

    class Handler:
        headers = {"Host": "127.0.0.1:8787"}

    monkeypatch.setattr(pet_routes, "get_session", lambda sid, metadata_only=False: object())
    pet_routes._PET_NAVIGATION_COMMANDS.clear()

    first = pet_routes._queue_pet_session_navigation(Handler(), {"session_id": "sid-1"})
    second = pet_routes._queue_pet_session_navigation(Handler(), {"session_id": "sid-2"})

    assert pet_routes._ack_pet_navigation_command(first["id"]) is True
    with pet_routes._PET_NAVIGATION_LOCK:
        assert pet_routes._next_pet_navigation_command_locked("")["id"] == second["id"]
        assert pet_routes._next_pet_navigation_command_locked(first["id"])["id"] == second["id"]


def test_pet_fallback_open_only_allows_loopback_urls(monkeypatch):
    import api.pet_routes as pet_routes

    runs = []
    monkeypatch.setattr(pet_routes.sys, "platform", "darwin")
    monkeypatch.setitem(pet_routes._PET_WEBUI_BROWSER_HINT, "app", "Google Chrome")
    monkeypatch.setitem(pet_routes._PET_WEBUI_BROWSER_HINT, "seen_at", time.time())

    class Result:
        returncode = 0
        stdout = "opened\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        runs.append((argv, kwargs))
        return Result()

    monkeypatch.setattr(pet_routes.subprocess, "run", fake_run)
    monkeypatch.setattr(
        pet_routes.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fallback must use default browser open")),
    )

    assert pet_routes._fallback_open_pet_browser_url("https://evil.example/session/sid-1") is False
    assert runs == []
    assert pet_routes._fallback_open_pet_browser_url("http://127.0.0.1:8787/session/sid-1") is True
    assert len(runs) == 1
    assert runs[0][0][0:1] == ["open"]
    assert runs[0][0][-1] == "http://127.0.0.1:8787/session/sid-1"


def test_pet_fallback_open_uses_default_browser_without_detected_webui(monkeypatch):
    # Cold start: no WebUI tab has been seen recently, so there is no browser
    # hint. The fallback must still open the session in the system default
    # browser via `open <url>`; otherwise a bubble click opens nothing.
    import api.pet_routes as pet_routes

    runs = []
    monkeypatch.setattr(pet_routes.sys, "platform", "darwin")
    monkeypatch.setitem(pet_routes._PET_WEBUI_BROWSER_HINT, "app", "")
    monkeypatch.setitem(pet_routes._PET_WEBUI_BROWSER_HINT, "seen_at", 0.0)

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        runs.append(argv)
        return Result()

    monkeypatch.setattr(pet_routes.subprocess, "run", fake_run)

    assert pet_routes._fallback_open_pet_browser_url("http://127.0.0.1:8787/session/sid-1") is True
    assert len(runs) == 1
    assert runs[0][0:1] == ["open"]
    assert runs[0][-1] == "http://127.0.0.1:8787/session/sid-1"


def test_pet_open_url_rejects_host_and_scheme_injection():
    import api.pet_routes as pet_routes

    class Handler:
        headers = {"Host": "evil.example", "X-Forwarded-Proto": "javascript"}

    url = pet_routes._pet_open_url(Handler(), "sid-1", draft="hello", autosend=True)

    assert url == "http://127.0.0.1:8787/session/sid-1?draft=hello"
    assert "autosend" not in url
    try:
        pet_routes._pet_open_url(Handler(), "../sid")
    except ValueError as exc:
        assert str(exc) == "invalid session_id"
    else:
        raise AssertionError("path-like session ids must be rejected")


def test_pet_skin_scan_rejects_manifest_path_traversal(monkeypatch, tmp_path):
    import api.pet_routes as pet_routes

    pets_root = tmp_path / "pets"
    bad_skin = pets_root / "bad"
    bad_skin.mkdir(parents=True)
    (bad_skin / "pet.json").write_text(
        json.dumps({"id": "bad", "displayName": "bad", "spritesheetPath": "../secret.webp"}),
        encoding="utf-8",
    )
    (pets_root / "secret.webp").write_bytes(b"not a skin")
    mismatched_id = pets_root / "mismatch"
    mismatched_id.mkdir()
    (mismatched_id / "pet.json").write_text(
        json.dumps({"id": "other", "displayName": "other", "spritesheetPath": "spritesheet.webp"}),
        encoding="utf-8",
    )
    (mismatched_id / "spritesheet.webp").write_bytes(b"skin")

    monkeypatch.setattr(pet_routes, "_pet_static_path", lambda *parts: tmp_path.joinpath(*parts))

    assert pet_routes._available_pet_skins() == []






def test_pet_skin_scan_accepts_explicit_layout(monkeypatch, tmp_path):
    import api.pet_routes as pet_routes

    pets_root = tmp_path / "pets"
    skin_dir = pets_root / "custom"
    skin_dir.mkdir(parents=True)
    (skin_dir / "spritesheet.webp").write_bytes(b"skin")
    layout = pet_routes._default_pet_skin_layout()
    layout["frameWidth"] = 256
    layout["frameHeight"] = 256
    (skin_dir / "pet.json").write_text(
        json.dumps(
            {
                "id": "custom",
                "displayName": "Custom",
                "spritesheetPath": "spritesheet.webp",
                "layout": layout,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(pet_routes, "_pet_static_path", lambda *parts: tmp_path.joinpath(*parts))

    skins = pet_routes._available_pet_skins()
    assert len(skins) == 1
    assert skins[0]["id"] == "custom"
    assert skins[0]["layout"]["frameWidth"] == 256
    assert skins[0]["layout"]["frameHeight"] == 256
    assert skins[0]["layout"]["states"][0] == {"name": "idle", "row": 0, "frames": 6}


def test_pet_skin_scan_rejects_invalid_layout(monkeypatch, tmp_path):
    import api.pet_routes as pet_routes

    pets_root = tmp_path / "pets"
    bad_skin = pets_root / "bad-layout"
    bad_skin.mkdir(parents=True)
    (bad_skin / "spritesheet.webp").write_bytes(b"skin")
    (bad_skin / "pet.json").write_text(
        json.dumps(
            {
                "id": "bad-layout",
                "displayName": "Bad Layout",
                "spritesheetPath": "spritesheet.webp",
                "layout": {
                    "columns": 8,
                    "rows": 9,
                    "frameWidth": 192,
                    "frameHeight": 208,
                    "states": [{"name": "idle", "row": 99, "frames": 6}],
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(pet_routes, "_pet_static_path", lambda *parts: tmp_path.joinpath(*parts))

    assert pet_routes._available_pet_skins() == []


def test_pet_routes_remain_behind_global_auth_gate():
    from api.auth import PUBLIC_PATHS

    assert "/pet" not in PUBLIC_PATHS
    assert "/api/pet/attention" not in PUBLIC_PATHS
    assert "/api/pet/skins" not in PUBLIC_PATHS
    assert "/api/pet/navigation" not in PUBLIC_PATHS
    assert "/api/pet/status" not in PUBLIC_PATHS
    assert "/api/pet/install" not in PUBLIC_PATHS
    assert "/api/pet/launch" not in PUBLIC_PATHS
    assert "/api/pet/close" not in PUBLIC_PATHS
    assert "/api/pet/open_session" not in PUBLIC_PATHS


def test_pet_launch_is_loopback_only():
    import api.pet_routes as pet_routes

    class LocalHandler:
        client_address = ("127.0.0.1", 12345)

    class RemoteHandler:
        client_address = ("192.168.10.99", 12345)

    assert pet_routes._pet_client_is_loopback(LocalHandler()) is True
    assert pet_routes._pet_client_is_loopback(RemoteHandler()) is False


def test_pet_navigation_poll_is_loopback_only(monkeypatch):
    import api.pet_routes as pet_routes

    class RemoteHandler:
        client_address = ("192.168.10.99", 12345)
        headers = {}
        calls = []

    monkeypatch.setattr(pet_routes, "_PET_NAVIGATION_LAST_POLL_AT", 12.0)
    monkeypatch.setattr(
        pet_routes,
        "bad",
        lambda handler, message, status=400: handler.calls.append((message, status)) or True,
    )

    handler = RemoteHandler()
    result = pet_routes._handle_pet_navigation(handler, urlparse("/api/pet/navigation"))

    assert result is True
    assert handler.calls == [("desktop pet navigation is only available from this machine", 403)]
    assert pet_routes._PET_NAVIGATION_LAST_POLL_AT == 12.0


def test_pet_open_session_is_loopback_only(monkeypatch):
    import api.pet_routes as pet_routes

    class RemoteHandler:
        client_address = ("192.168.10.99", 12345)
        calls = []

    opened = []
    monkeypatch.setattr(pet_routes, "_queue_and_focus_pet_session_navigation", lambda handler, body: opened.append(body))
    monkeypatch.setattr(
        pet_routes,
        "bad",
        lambda handler, message, status=400: handler.calls.append((message, status)) or True,
    )

    handler = RemoteHandler()
    result = pet_routes._handle_pet_open_session(handler, {"session_id": "sid-1"})

    assert result is True
    assert opened == []
    assert handler.calls == [("desktop pet session navigation is only available from this machine", 403)]


def test_pet_launch_candidates_prefer_existing_shells(monkeypatch, tmp_path):
    import api.pet_routes as pet_routes

    root = tmp_path / "repo"
    binary = root / "desktop-pet" / "src-tauri" / "target" / "debug" / (
        "hermes-desktop-pet.exe" if pet_routes.os.name == "nt" else "hermes-desktop-pet"
    )
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    monkeypatch.setattr(pet_routes, "_repo_root", lambda: root)

    candidates = pet_routes._desktop_pet_launch_candidates()

    assert candidates
    assert candidates[0]["kind"] == "debug-binary"
    assert candidates[0]["argv"] == [str(binary)]


def test_pet_prepare_skips_build_when_shell_exists(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes, "_desktop_pet_launch_candidates", lambda: [{"kind": "debug-binary"}])

    result = pet_routes._prepare_desktop_pet_shell()

    assert result["ok"] is True
    assert result["installed"] is True
    assert result["method"] == "debug-binary"


def test_pet_launch_is_single_instance(monkeypatch):
    import api.pet_routes as pet_routes

    def fail_candidates():
        raise AssertionError("existing desktop pet should be reused before launch candidates are inspected")

    monkeypatch.setattr(pet_routes, "_desktop_pet_processes", lambda: [{"pid": 456, "command": "/repo/hermes-desktop-pet"}])
    monkeypatch.setattr(pet_routes, "_desktop_pet_launch_candidates", fail_candidates)

    result = pet_routes._launch_desktop_pet_process()

    assert result["ok"] is True
    assert result["already_running"] is True
    assert result["method"] == "existing"


def test_pet_launch_reuses_existing_process_for_matching_base(monkeypatch):
    import api.pet_routes as pet_routes

    def fail_candidates():
        raise AssertionError("matching desktop pet should be reused before launch candidates are inspected")

    monkeypatch.setattr(
        pet_routes,
        "_desktop_pet_processes",
        lambda: [{"pid": 456, "command": "/repo/hermes-desktop-pet HERMES_DESKTOP_PET_WEBUI_BASE=http://127.0.0.1:8788"}],
    )
    monkeypatch.setattr(pet_routes, "_desktop_pet_launch_candidates", fail_candidates)

    result = pet_routes._launch_desktop_pet_process("http://127.0.0.1:8788")

    assert result["ok"] is True
    assert result["already_running"] is True
    assert result["method"] == "existing"


def test_pet_launch_restarts_existing_process_for_wrong_base(monkeypatch, tmp_path):
    import api.pet_routes as pet_routes

    calls = {"processes": 0}
    terminated = []
    popen_calls = []

    def fake_processes():
        calls["processes"] += 1
        if calls["processes"] == 1:
            return [{"pid": 456, "command": "/repo/hermes-desktop-pet HERMES_DESKTOP_PET_WEBUI_BASE=http://127.0.0.1:8787"}]
        return []

    def fake_terminate(processes):
        terminated.extend(processes)
        return {"ok": True, "closed": len(processes), "error": ""}

    class Process:
        pid = 789

    def fake_popen(argv, **kwargs):
        popen_calls.append((argv, kwargs))
        return Process()

    monkeypatch.setattr(pet_routes, "_desktop_pet_processes", fake_processes)
    monkeypatch.setattr(pet_routes, "_terminate_desktop_pet_processes", fake_terminate)
    monkeypatch.setattr(
        pet_routes,
        "_desktop_pet_launch_candidates",
        lambda: [{"kind": "debug-binary", "argv": ["/repo/hermes-desktop-pet"], "cwd": tmp_path}],
    )
    monkeypatch.setattr(pet_routes.subprocess, "Popen", fake_popen)

    result = pet_routes._launch_desktop_pet_process("http://127.0.0.1:8788/pet")

    assert result["ok"] is True
    assert result["already_running"] is False
    assert result["pid"] == 789
    assert terminated == [{"pid": 456, "command": "/repo/hermes-desktop-pet HERMES_DESKTOP_PET_WEBUI_BASE=http://127.0.0.1:8787"}]
    assert popen_calls[0][1]["env"]["HERMES_DESKTOP_PET_WEBUI_BASE"] == "http://127.0.0.1:8788"


def test_pet_launch_refuses_to_replace_unregistered_existing_process(monkeypatch):
    import api.pet_routes as pet_routes

    terminated = []
    monkeypatch.setattr(pet_routes, "_desktop_pet_processes", lambda: [{"pid": 456, "command": "/repo/hermes-desktop-pet"}])
    monkeypatch.setattr(pet_routes, "_terminate_desktop_pet_processes", lambda processes: terminated.extend(processes))

    result = pet_routes._launch_desktop_pet_process("http://127.0.0.1:8788")

    assert result["ok"] is False
    assert result["method"] == "existing-unknown"
    assert terminated == []


def test_pet_process_base_url_uses_registry_when_process_env_is_missing(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(
        pet_routes,
        "_read_desktop_pet_registry",
        lambda: {"pid": 456, "base_url": "http://127.0.0.1:8788", "registered_at": 123.0},
    )
    monkeypatch.setattr(pet_routes, "_pid_is_running", lambda pid: pid == 456)

    base_url = pet_routes._desktop_pet_process_base_url({"pid": 456, "command": "/repo/hermes-desktop-pet"})

    assert base_url == "http://127.0.0.1:8788"


def test_pet_register_records_current_desktop_pet_base(monkeypatch):
    import api.pet_routes as pet_routes

    writes = []

    class Handler:
        client_address = ("127.0.0.1", 12345)
        headers = {"Host": "127.0.0.1:8788"}
        responses = []

    monkeypatch.setattr(pet_routes, "_write_desktop_pet_registry", lambda payload: writes.append(payload))
    monkeypatch.setattr(pet_routes.time, "time", lambda: 123.0)
    monkeypatch.setattr(pet_routes, "j", lambda handler, payload, status=200: handler.responses.append((payload, status)) or True)

    result = pet_routes._handle_pet_register(Handler(), {"pid": 789, "base_url": "http://127.0.0.1:8788/pet"})

    assert result is True
    assert writes == [{"pid": 789, "base_url": "http://127.0.0.1:8788", "registered_at": 123.0, "source": "pet"}]


def test_pet_close_noops_when_not_running(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes, "_desktop_pet_processes", lambda: [])

    result = pet_routes._close_desktop_pet_processes()

    assert result == {"ok": True, "closed": 0, "running": False}


def test_pet_process_detection_ignores_shell_commands_and_unowned_pet_binaries(monkeypatch):
    import api.pet_routes as pet_routes

    class Result:
        returncode = 0
        stdout = "123 zsh -lc find . -name 'Hermes Desktop Pet.app'\n456 /tmp/hermes-desktop-pet\n"

    monkeypatch.setattr(pet_routes.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(pet_routes.os, "getpid", lambda: 999)
    monkeypatch.setattr(pet_routes.os, "name", "posix")
    monkeypatch.setattr(pet_routes, "_desktop_pet_known_process_paths", lambda: {"/repo/desktop-pet/src-tauri/target/release/hermes-desktop-pet"})

    processes = pet_routes._desktop_pet_processes()

    assert processes == []


def test_pet_process_detection_accepts_owned_binary(monkeypatch):
    import api.pet_routes as pet_routes

    class Result:
        returncode = 0
        stdout = "456 /repo/desktop-pet/src-tauri/target/release/hermes-desktop-pet\n"

    monkeypatch.setattr(pet_routes.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(pet_routes.os, "getpid", lambda: 999)
    monkeypatch.setattr(pet_routes.os, "name", "posix")
    monkeypatch.setattr(pet_routes, "_desktop_pet_known_process_paths", lambda: {"/repo/desktop-pet/src-tauri/target/release/hermes-desktop-pet"})

    processes = pet_routes._desktop_pet_processes()

    assert processes == [{"pid": 456, "command": "/repo/desktop-pet/src-tauri/target/release/hermes-desktop-pet"}]


def test_pet_process_detection_accepts_app_bundle_executable(monkeypatch):
    import api.pet_routes as pet_routes

    class Result:
        returncode = 0
        stdout = "456 /Applications/Hermes Desktop Pet.app/Contents/MacOS/hermes-desktop-pet\n"

    monkeypatch.setattr(pet_routes.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(pet_routes.os, "getpid", lambda: 999)
    monkeypatch.setattr(pet_routes.os, "name", "posix")
    monkeypatch.setattr(
        pet_routes,
        "_desktop_pet_known_process_paths",
        lambda: {"/Applications/Hermes Desktop Pet.app/Contents/MacOS/hermes-desktop-pet"},
    )

    processes = pet_routes._desktop_pet_processes()

    assert processes == [{"pid": 456, "command": "/Applications/Hermes Desktop Pet.app/Contents/MacOS/hermes-desktop-pet"}]
