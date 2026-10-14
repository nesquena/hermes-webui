"""GET /api/session must window synthesized foreign (CLI/TUI/Desktop) sessions.

Sessions without a WebUI sidecar are served by the ``KeyError`` synthesis
branch of ``_handle_session_get``. That branch used to return the whole
stitched transcript on every request: it ignored ``msg_limit`` / ``msg_before``
and even ``messages=0``. A long Desktop conversation (tens of thousands of
stitched rows) therefore produced a multi-megabyte payload on every metadata
poll and every paginated open, which native clients reject outright (#6491).

These pin the paginated contract for the synthesis branch:

* ``messages=0`` returns metadata only while keeping the full ``message_count``;
* ``msg_limit`` returns a bounded tail window with truncation metadata;
* ``msg_before`` pages backwards in the same coordinate space;
* hidden tool payloads inside a limited window are bounded;
* todo state is still derived from the full transcript;
* a request without ``msg_limit`` keeps the explicit full-transcript shape.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import api.routes as routes
from api.models import Session


SID = "20260101_000000_abc123"


def _row():
    return {
        "session_id": SID,
        "title": "Desktop transcript",
        "workspace": "/home/user/project",
        "model": "test-model",
        "message_count": 0,
        "created_at": 1.0,
        "updated_at": 2.0,
        "last_message_at": 2.0,
        "pinned": False,
        "archived": False,
        "project_id": None,
        "profile": None,
        "source_tag": "desktop",
        "raw_source": "desktop",
        "session_source": "desktop",
        "source_label": "Desktop",
        "is_cli_session": True,
        "read_only": False,
    }


def _todo_call(call_id, todos):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": "todo", "arguments": json.dumps({"todos": todos})},
        }],
    }


def _todo_result(call_id, todos):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps({"todos": todos}),
    }


def _transcript(turns=40, big_tool_chars=0):
    msgs = []
    todos = [{"id": "1", "content": "early task", "status": "in_progress"}]
    msgs.append(_todo_call("todo-early", todos))
    msgs.append(_todo_result("todo-early", todos))
    for i in range(turns):
        msgs.append({"role": "user", "content": f"question {i}"})
        if big_tool_chars:
            msgs.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"call-{i}",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            })
            msgs.append({
                "role": "tool",
                "tool_call_id": f"call-{i}",
                "content": "x" * big_tool_chars,
            })
        msgs.append({"role": "assistant", "content": f"answer {i}"})
    return msgs


def _synth(messages):
    return Session(
        session_id=SID,
        title="Desktop transcript",
        workspace="/home/user/project",
        model="test-model",
        messages=messages,
        is_cli_session=True,
        source_tag="desktop",
        raw_source="desktop",
        session_source="desktop",
        source_label="Desktop",
    )


def _load(monkeypatch, query, messages):
    cap = {}

    def fake_j(_handler, data, status=200, **_kwargs):
        cap["data"] = data
        cap["status"] = status
        return True

    def fake_bad(_handler, msg, status=400, **_kwargs):
        cap["error"] = msg
        cap["status"] = status
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "bad", fake_bad)
    parsed = urlparse(f"/api/session?session_id={SID}&{query}")
    with patch("api.routes.get_session", side_effect=KeyError(SID)), \
         patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._lookup_cli_session_metadata", return_value=_row()), \
         patch(
             "api.routes._claim_or_synthesize_cli_session",
             return_value=(_synth(messages), "materialized"),
         ):
        assert routes.handle_get(MagicMock(), parsed) is True
    assert cap.get("error") is None, cap.get("error")
    assert cap["status"] == 200
    return cap["data"]["session"]


def test_metadata_only_load_omits_foreign_messages(monkeypatch):
    msgs = _transcript(turns=40)
    sess = _load(monkeypatch, "messages=0&resolve_model=0", msgs)

    assert sess["messages"] == []
    assert sess["message_count"] == len(msgs)
    assert sess["_messages_truncated"] is False
    assert sess["_messages_offset"] == 0


def test_msg_limit_windows_foreign_tail(monkeypatch):
    msgs = _transcript(turns=40)
    sess = _load(monkeypatch, "messages=1&resolve_model=0&msg_limit=10", msgs)

    visible = [m for m in sess["messages"] if m.get("role") in ("user", "assistant")]
    assert len(visible) == 10
    assert sess["messages"][-1]["content"] == "answer 39"
    assert sess["message_count"] == len(msgs)
    assert sess["_messages_truncated"] is True
    offset = sess["_messages_offset"]
    assert offset == len(msgs) - len(sess["messages"])
    assert sess["_msg_limit_max"] == routes._MAX_MSG_LIMIT


def test_msg_before_pages_foreign_history(monkeypatch):
    msgs = _transcript(turns=40)
    tail = _load(monkeypatch, "messages=1&resolve_model=0&msg_limit=10", msgs)
    before = tail["_messages_offset"]

    page = _load(
        monkeypatch,
        f"messages=1&resolve_model=0&msg_limit=10&msg_before={before}",
        msgs,
    )

    assert page["messages"]
    assert page["_messages_offset"] + len(page["messages"]) == before
    assert page["messages"][-1] == msgs[before - 1]
    assert page["_messages_truncated"] is True


def test_limited_window_bounds_hidden_tool_payloads(monkeypatch):
    msgs = _transcript(turns=20, big_tool_chars=200_000)
    sess = _load(monkeypatch, "messages=1&resolve_model=0&msg_limit=5", msgs)

    tool_rows = [m for m in sess["messages"] if m.get("role") == "tool"]
    assert tool_rows
    assert all(m.get("_content_truncated") for m in tool_rows)
    assert len(json.dumps(sess)) < 200_000


def test_todo_state_uses_full_foreign_transcript(monkeypatch):
    msgs = _transcript(turns=40)
    sess = _load(monkeypatch, "messages=1&resolve_model=0&msg_limit=5", msgs)

    # The only todo write sits far outside the tail window.
    assert all(m.get("tool_call_id") != "todo-early" for m in sess["messages"])
    assert sess.get("todo_state")


def test_unlimited_foreign_load_keeps_full_transcript(monkeypatch):
    msgs = _transcript(turns=40)
    sess = _load(monkeypatch, "messages=1&resolve_model=0", msgs)

    assert len(sess["messages"]) == len(msgs)
    assert sess["_messages_truncated"] is False
    assert sess["_messages_offset"] == 0
