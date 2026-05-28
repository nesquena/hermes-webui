"""Behavioural tests for the side-effecting wrappers in ``api.todo_state``.

These tests exercise ``emit_todo_state`` and ``attach_todo_state``
directly against a captured ``put`` callable / payload dict, without
spinning up a full HTTP server. They cover:

* the public contract — when does an event/attachment fire, what shape
  does it have, and what is its source-of-truth for each field;
* the failure-isolation contract — emission must never raise, even
  when ``put`` itself raises or the input is garbage;
* the wire-format contract — every key the frontend (``static/messages.js``
  for live, ``static/ui.js`` for cold-load) reads must be present.

End-to-end scenarios that combine emit + attach with realistic
session histories live in ``tests/test_todo_state_scenarios.py``.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple

import pytest

from api.todo_state import (
    EVENT_NAME,
    PAYLOAD_KEY,
    VERSION,
    attach_todo_state,
    emit_todo_state,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _todo_payload(todos: List[Dict[str, str]]) -> str:
    """JSON string mirroring what ``tools.todo_tool.todo_tool`` returns."""
    pending = sum(1 for t in todos if t["status"] == "pending")
    in_progress = sum(1 for t in todos if t["status"] == "in_progress")
    completed = sum(1 for t in todos if t["status"] == "completed")
    cancelled = sum(1 for t in todos if t["status"] == "cancelled")
    return json.dumps({
        "todos": todos,
        "summary": {
            "total": len(todos),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "cancelled": cancelled,
        },
    }, ensure_ascii=False)


class _Recorder:
    """Captures ``(event, data)`` calls to a ``put``-shaped callable."""

    def __init__(self) -> None:
        self.events: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, event: str, data: Dict[str, Any]) -> None:
        self.events.append((event, data))


# ---------------------------------------------------------------------------
# emit_todo_state — happy path
# ---------------------------------------------------------------------------


class TestEmitTodoStateHappyPath:
    """Coverage for the ``name=='todo'`` + valid payload case."""

    def test_emits_when_name_is_todo(self):
        rec = _Recorder()
        result = emit_todo_state(
            rec,
            name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "task", "status": "pending"},
            ]),
            session_id="sess-A",
            stream_id="stream-1",
        )
        assert result is True
        assert len(rec.events) == 1
        ev_name, ev_data = rec.events[0]
        assert ev_name == EVENT_NAME == "todo_state"
        assert isinstance(ev_data, dict)

    def test_payload_carries_full_wire_contract(self):
        """Pin every key ``static/messages.js`` reads off the event."""
        rec = _Recorder()
        emit_todo_state(
            rec,
            name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "x", "status": "in_progress"},
                {"id": "2", "content": "y", "status": "pending"},
            ]),
            session_id="sess-A",
            stream_id="stream-1",
        )
        _, ev = rec.events[0]
        # ─── identity / addressing ───────────────────────────────────
        assert ev["session_id"] == "sess-A"
        assert ev["stream_id"] == "stream-1"
        assert ev["source"] == "tool"
        # ─── ordering ───────────────────────────────────────────────
        # ts is a server-clock float; the frontend uses it to drop
        # strictly-older snapshots. Assert it's wall-clock-ish, not
        # epoch zero or a sentinel.
        assert isinstance(ev["ts"], float)
        assert ev["ts"] > 1_700_000_000  # well past 2023-11
        # ─── snapshot ───────────────────────────────────────────────
        assert isinstance(ev["todos"], list)
        assert len(ev["todos"]) == 2
        assert ev["todos"][0]["status"] == "in_progress"
        assert ev["summary"]["total"] == 2
        assert ev["summary"]["in_progress"] == 1
        assert ev["version"] == VERSION

    def test_default_source_is_tool(self):
        rec = _Recorder()
        emit_todo_state(
            rec,
            name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
            session_id="s", stream_id="r",
        )
        assert rec.events[0][1]["source"] == "tool"

    def test_custom_source_respected(self):
        # Future callers — e.g. a compression-refresh refresh — pass an
        # explicit ``source`` so the frontend can distinguish fresh
        # writes from re-hydrate snapshots.
        rec = _Recorder()
        emit_todo_state(
            rec,
            name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
            session_id="s", stream_id="r",
            source="compression-refresh",
        )
        assert rec.events[0][1]["source"] == "compression-refresh"

    def test_accepts_pre_parsed_dict_result(self):
        # Modern callbacks may deserialise earlier in the pipeline; the
        # helper must accept a dict, not just a JSON string.
        rec = _Recorder()
        ok = emit_todo_state(
            rec,
            name="todo",
            function_result={
                "todos": [{"id": "1", "content": "x", "status": "pending"}],
                "summary": {"total": 1, "pending": 1},
            },
            session_id="s", stream_id="r",
        )
        assert ok is True
        assert rec.events[0][1]["todos"][0]["id"] == "1"

    def test_ts_is_close_to_wall_clock(self):
        before = time.time()
        rec = _Recorder()
        emit_todo_state(
            rec,
            name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
            session_id="s", stream_id="r",
        )
        after = time.time()
        ts = rec.events[0][1]["ts"]
        # Allow a generous ±2 s window for slow CI.  The point is to
        # catch a regression that hard-codes 0 or a stale value.
        assert before - 2 <= ts <= after + 2


# ---------------------------------------------------------------------------
# emit_todo_state — guard / no-emit cases
# ---------------------------------------------------------------------------


class TestEmitTodoStateGuards:
    """All the cases where the helper must NOT emit."""

    @pytest.mark.parametrize("name", [
        "read_file", "write_file", "patch", "terminal", "todo_tool",
        "TODO", "todos", "", None,
    ])
    def test_skips_non_todo_names(self, name):
        rec = _Recorder()
        result = emit_todo_state(
            rec,
            name=name,
            function_result=_todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
            session_id="s", stream_id="r",
        )
        assert result is False
        assert rec.events == []

    @pytest.mark.parametrize("bad", [
        None,
        "",
        "not json",
        "{",
        "{}",
        '{"foo": "bar"}',
        '{"todos": "not a list"}',
        '{"todos": null}',
        '"just a string"',
        "42",
    ])
    def test_skips_when_payload_unparseable_or_wrong_shape(self, bad):
        rec = _Recorder()
        result = emit_todo_state(
            rec,
            name="todo",
            function_result=bad,
            session_id="s", stream_id="r",
        )
        assert result is False
        assert rec.events == []

    def test_returns_false_when_put_raises(self):
        # A broken queue must not bring down the tool delivery path.
        # The helper swallows + returns False so the caller's own
        # try/except is no longer load-bearing.
        def boom(_event, _data):
            raise RuntimeError("queue is dead")
        result = emit_todo_state(
            boom,
            name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
            session_id="s", stream_id="r",
        )
        assert result is False  # swallowed, never raised


# ---------------------------------------------------------------------------
# emit_todo_state — addressing / metadata
# ---------------------------------------------------------------------------


class TestEmitTodoStateAddressing:
    def test_none_session_and_stream_still_emits(self):
        # Defensive: if a caller forgets to pass IDs we still emit so
        # the panel can update; the frontend treats missing
        # ``session_id`` as "no filter" and accepts the event.
        rec = _Recorder()
        emit_todo_state(
            rec,
            name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
            session_id=None, stream_id=None,
        )
        ev = rec.events[0][1]
        assert ev["session_id"] is None
        assert ev["stream_id"] is None

    def test_event_name_and_payload_keys_match_module_constants(self):
        # Frontend pins on string literals; the module exposes them as
        # constants so a rename is one place. This test makes sure the
        # constants and the actual emission stay in sync.
        rec = _Recorder()
        emit_todo_state(
            rec,
            name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
            session_id="s", stream_id="r",
        )
        ev_name, ev_data = rec.events[0]
        assert ev_name == EVENT_NAME
        # PAYLOAD_KEY is for cold-load on the session GET, not for the
        # SSE event — but the wire field on cold-load is the same name.
        assert PAYLOAD_KEY == EVENT_NAME == "todo_state"
        # Top-level event dict must not collide with snapshot fields.
        for k in ("session_id", "stream_id", "source", "ts",
                  "todos", "summary", "version"):
            assert k in ev_data, f"missing {k} in emit payload"


# ---------------------------------------------------------------------------
# attach_todo_state — direct unit tests
# ---------------------------------------------------------------------------


class TestAttachTodoStateHappyPath:
    def test_attaches_to_empty_payload(self):
        payload: Dict[str, Any] = {}
        msgs = [{"role": "tool", "content": _todo_payload([
            {"id": "1", "content": "x", "status": "pending"},
        ])}]
        result = attach_todo_state(payload, msgs)
        assert result is True
        assert PAYLOAD_KEY in payload
        snap = payload[PAYLOAD_KEY]
        assert snap["version"] == VERSION
        assert snap["todos"][0]["id"] == "1"

    def test_attaches_to_existing_payload_without_clobbering(self):
        # Mirrors how ``api/routes.py`` calls the helper: ``raw`` already
        # contains session metadata when we attach todo_state.
        payload: Dict[str, Any] = {
            "session_id": "s",
            "title": "do something",
            "messages": [],
            "tool_calls": [],
        }
        msgs = [{"role": "tool", "content": _todo_payload([
            {"id": "1", "content": "x", "status": "pending"},
        ])}]
        attach_todo_state(payload, msgs)
        # Original keys must survive the mutation.
        assert payload["session_id"] == "s"
        assert payload["title"] == "do something"
        assert PAYLOAD_KEY in payload

    def test_returns_false_and_leaves_payload_alone_for_no_messages(self):
        payload: Dict[str, Any] = {"session_id": "s"}
        assert attach_todo_state(payload, []) is False
        assert attach_todo_state(payload, None) is False
        assert "todo_state" not in payload

    def test_returns_false_for_messages_without_todo(self):
        payload: Dict[str, Any] = {}
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "tool", "content": '{"output": "ok"}'},
        ]
        assert attach_todo_state(payload, msgs) is False
        assert "todo_state" not in payload

    def test_returns_false_when_messages_iteration_raises(self):
        # If the caller hands us a broken iterable (e.g. a generator
        # that raises on next()), the helper must swallow and not pollute
        # the response.
        def broken():
            yield {"role": "user", "content": "hi"}
            raise RuntimeError("storage corrupted")
        payload: Dict[str, Any] = {"session_id": "s"}
        result = attach_todo_state(payload, broken())
        assert result is False
        assert "todo_state" not in payload


# ---------------------------------------------------------------------------
# attach_todo_state — multi-write history
# ---------------------------------------------------------------------------


class TestAttachTodoStateMultiWrite:
    def test_picks_latest_write_in_history(self):
        old = _todo_payload([
            {"id": "1", "content": "old plan", "status": "completed"},
        ])
        new = _todo_payload([
            {"id": "1", "content": "new plan", "status": "in_progress"},
            {"id": "2", "content": "next step", "status": "pending"},
        ])
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "tool", "content": old},
            {"role": "assistant", "content": "thinking"},
            {"role": "tool", "content": new},
            {"role": "assistant", "content": "done"},
        ]
        payload: Dict[str, Any] = {}
        attach_todo_state(payload, msgs)
        snap = payload["todo_state"]
        assert len(snap["todos"]) == 2
        assert snap["todos"][0]["content"] == "new plan"
        assert snap["todos"][0]["status"] == "in_progress"

    def test_falls_back_to_prior_valid_when_latest_truncated(self):
        # Realistic transport corruption: the most recent tool message was
        # truncated mid-write.  We must surface the prior valid snapshot
        # rather than wiping the panel.  Mirrors the agent's hydrate
        # fallback logic.
        good = _todo_payload([
            {"id": "1", "content": "valid", "status": "in_progress"},
        ])
        msgs = [
            {"role": "tool", "content": good},
            {"role": "tool", "content": '{"todos": ['},
        ]
        payload: Dict[str, Any] = {}
        result = attach_todo_state(payload, msgs)
        assert result is True
        assert payload["todo_state"]["todos"][0]["content"] == "valid"
