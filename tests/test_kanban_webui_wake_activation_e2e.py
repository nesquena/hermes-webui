import json
import os
import pathlib
import sqlite3
import time
import uuid
import urllib.error
import urllib.request

import pytest


TEST_BASE = f"http://127.0.0.1:{os.environ.get('HERMES_WEBUI_TEST_PORT', '8788')}"
STATE_DIR = pathlib.Path(os.environ["HERMES_WEBUI_TEST_STATE_DIR"])


def _request(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Origin": TEST_BASE}
    request = urllib.request.Request(TEST_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _require_kanban_dependency():
    status, payload = _request("GET", "/api/kanban/boards")
    error = str(payload.get("error") or "") if isinstance(payload, dict) else ""
    if status == 503 and error.startswith("kanban unavailable:"):
        pytest.skip(f"repository Kanban dependency unavailable: {error}")
    assert status == 200, payload


def test_webui_wake_http_routes_and_sidecar_use_session_server_only():
    _require_kanban_dependency()
    get_status, initial = _request("GET", "/api/kanban/webui-wake")
    assert get_status == 200
    assert initial["enabled"] is False

    bad_status, _ = _request("POST", "/api/kanban/webui-wake", {"action": "bogus"})
    assert bad_status == 400
    for action in (None, True, 1, [], {}):
        bad_status, _ = _request("POST", "/api/kanban/webui-wake", {"action": action})
        assert bad_status == 400

    post_status, enabled = _request("POST", "/api/kanban/webui-wake", {"action": "enable"})
    assert post_status == 200
    assert enabled["enabled"] is True
    assert enabled["baseline_complete"] is True

    sidecar = STATE_DIR / "kanban_webui_wake_state.json"
    assert sidecar.exists()
    state = json.loads(sidecar.read_text(encoding="utf-8"))
    assert state["schema_version"] == 1
    assert state["enabled"] is True
    assert state["db_boundaries"]

    disable_status, disabled = _request("POST", "/api/kanban/webui-wake", {"action": "disable"})
    assert disable_status == 200
    assert disabled["enabled"] is False
    assert sidecar.exists()

    db_path = next(iter(state["db_boundaries"]))
    task_id = f"backlog-{uuid.uuid4().hex}"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "completed", "{}", int(time.time())),
        )
        conn.execute(
            """
            INSERT INTO kanban_notify_subs
                (task_id, platform, chat_id, thread_id, notifier_profile,
                 delivery_mode, created_at, last_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, "webui", "chat", "", "default", "notify+wake", int(time.time()), 0),
        )

    second_enable_status, second_enabled = _request(
        "POST", "/api/kanban/webui-wake", {"action": "enable"}
    )
    assert second_enable_status == 200
    assert second_enabled["enabled"] is True
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT last_event_id FROM kanban_notify_subs WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert cursor == second_enabled["db_boundaries"][db_path]
    final_disable_status, final_disabled = _request(
        "POST", "/api/kanban/webui-wake", {"action": "disable"}
    )
    assert final_disable_status == 200
    assert final_disabled["enabled"] is False
