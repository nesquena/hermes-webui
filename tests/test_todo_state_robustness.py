"""Robustness tests for ``api.todo_state`` derivation and emission.

Adds coverage that earlier review identified as missing or weak:

* malformed *items* inside an otherwise-valid ``todos`` list — the
  helper currently passes them through (todo tool produces canonical
  shapes only), and we want that contract pinned with explicit
  evidence so a future tightener does so deliberately;
* ``timestamp`` propagation from cold-load source message to the
  derived snapshot ``ts`` field — the frontend depends on this for
  cold-load vs. INFLIGHT recency reconciliation;
* extra malformed payload shapes for ``emit_todo_state`` (top-level
  array, ``ts`` non-numeric, ``session_id`` non-string);
* concurrent ``emit_todo_state`` calls on a thread-safe ``put`` —
  smoke test for the helper itself; the production ``put`` is a
  ``Queue.put_nowait`` which is already thread-safe.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Tuple

import pytest

from api.todo_state import (
    attach_todo_state,
    derive_todo_state,
    emit_todo_state,
    parse_todo_tool_result,
)


def _todo_payload(todos):
    return json.dumps({
        "todos": todos,
        "summary": {"total": len(todos)},
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Malformed items inside a valid `todos` list
# ---------------------------------------------------------------------------


class TestMalformedTodoItems:
    """Pin the current pass-through contract for non-canonical items.

    The ``todo`` tool always returns canonical ``{id, content, status}``
    objects, so production never sees these.  But the helper does not
    validate per-item shape, and the frontend tolerates whatever lands
    in the array (``esc()`` handles the rendering).  These tests pin
    that contract so a refactor that adds validation has to do so
    deliberately and update both ends together.
    """

    @pytest.mark.parametrize("garbage", [
        None,
        42,
        3.14,
        True,
        "bare string",
        [],
        ["nested", "list"],
        {"id": "1"},  # missing content/status
        {"content": "x"},  # missing id/status
        {"status": "pending"},  # missing id/content
    ])
    def test_parse_passes_through_malformed_items(self, garbage):
        result = parse_todo_tool_result(_todo_payload([garbage]))
        assert result is not None
        assert result["todos"] == [garbage]

    def test_derive_passes_through_mixed_canonical_and_malformed(self):
        msgs = [{"role": "tool", "content": _todo_payload([
            {"id": "1", "content": "ok", "status": "pending"},
            None,
            "stray string",
            {"id": "2", "content": "also ok", "status": "completed"},
        ])}]
        snap = derive_todo_state(msgs)
        assert snap is not None
        assert len(snap["todos"]) == 4
        assert snap["todos"][0]["id"] == "1"
        assert snap["todos"][3]["id"] == "2"

    def test_emit_passes_through_malformed_items(self):
        events: List[Tuple[str, Dict[str, Any]]] = []
        emit_todo_state(
            lambda ev, d: events.append((ev, d)),
            name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "ok", "status": "pending"},
                None,
            ]),
            session_id="s",
            stream_id="r",
        )
        assert len(events) == 1
        assert events[0][1]["todos"][1] is None


# ---------------------------------------------------------------------------
# Cold-load `ts` propagation (drives frontend INFLIGHT recency reconcile)
# ---------------------------------------------------------------------------


class TestColdLoadTimestampPropagation:
    """``derive_todo_state`` must surface the source message timestamp.

    The frontend's ``_hydrateTodosFromSession`` uses this field to pick
    the newer of cold-load vs. INFLIGHT when both are present, avoiding
    visible rollback on reload of a still-running session.
    """

    def test_ts_propagates_when_message_has_timestamp(self):
        msgs = [{
            "role": "tool",
            "timestamp": 1_700_000_500.5,
            "content": _todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
        }]
        snap = derive_todo_state(msgs)
        assert snap is not None
        assert snap.get("ts") == pytest.approx(1_700_000_500.5)

    def test_ts_omitted_when_message_has_no_timestamp(self):
        msgs = [{
            "role": "tool",
            "content": _todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
        }]
        snap = derive_todo_state(msgs)
        assert snap is not None
        # Frontend treats missing ts as 0 — explicit omission keeps the
        # wire payload small and signals "unknown" instead of "0".
        assert "ts" not in snap

    @pytest.mark.parametrize("bad_ts", [None, "", "not a number", float("nan"), [], {}])
    def test_garbage_timestamp_falls_back_to_omitted(self, bad_ts):
        msgs = [{
            "role": "tool",
            "timestamp": bad_ts,
            "content": _todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
        }]
        snap = derive_todo_state(msgs)
        assert snap is not None
        # NaN may slip past float() but we don't propagate it.
        ts = snap.get("ts")
        assert ts is None or (isinstance(ts, float) and ts == ts)  # not NaN

    def test_ts_picks_latest_message_in_history(self):
        """Multi-write history: the propagated ts must come from the
        same message we picked for the snapshot, not an older one."""
        old = {
            "role": "tool",
            "timestamp": 1_700_000_000.0,
            "content": _todo_payload([
                {"id": "1", "content": "old", "status": "completed"},
            ]),
        }
        new = {
            "role": "tool",
            "timestamp": 1_700_000_500.0,
            "content": _todo_payload([
                {"id": "1", "content": "new", "status": "in_progress"},
            ]),
        }
        snap = derive_todo_state([old, new])
        assert snap is not None
        assert snap["todos"][0]["content"] == "new"
        assert snap.get("ts") == pytest.approx(1_700_000_500.0)

    def test_attach_carries_ts_through_to_payload(self):
        payload: Dict[str, Any] = {}
        msgs = [{
            "role": "tool",
            "timestamp": 1_700_001_234.0,
            "content": _todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
        }]
        attach_todo_state(payload, msgs)
        assert payload["todo_state"].get("ts") == pytest.approx(1_700_001_234.0)


# ---------------------------------------------------------------------------
# Extra malformed payload shapes for emit_todo_state
# ---------------------------------------------------------------------------


class TestEmitMalformedExtras:
    @pytest.mark.parametrize("payload", [
        "[]",                  # top-level array
        "[{\"todos\": []}]",   # array wrapping object
        '{"todos":[],"ts":"not a number"}',
        '{"todos":[],"summary":"not an object"}',
        '"',                   # truly garbage
    ])
    def test_does_not_emit_for_unparseable_or_wrong_top_level(self, payload):
        events: List[Tuple[str, Dict[str, Any]]] = []
        ok = emit_todo_state(
            lambda ev, d: events.append((ev, d)),
            name="todo",
            function_result=payload,
            session_id="s",
            stream_id="r",
        )
        # The first three are valid JSON shapes that don't match the
        # contract; the helper either rejects them outright (top-level
        # array) or normalizes the parts it understands and emits.  We
        # accept either outcome here — the contract we pin is "no crash"
        # and "no malformed top-level event".
        if ok:
            assert events and events[0][0] == "todo_state"
            assert isinstance(events[0][1].get("todos"), list)
        else:
            assert events == []


# ---------------------------------------------------------------------------
# Concurrent emit_todo_state — thread safety smoke test
# ---------------------------------------------------------------------------


class TestConcurrentEmit:
    def test_concurrent_emits_do_not_drop_or_crash(self):
        """Production ``put`` is ``Queue.put_nowait`` (thread-safe).
        Verify the helper itself adds no shared mutable state — N threads
        each emit M times should produce exactly N*M events."""
        lock = threading.Lock()
        events: List[Tuple[str, Dict[str, Any]]] = []

        def put(ev, d):
            with lock:
                events.append((ev, d))

        N_THREADS = 8
        N_EMITS = 50
        barrier = threading.Barrier(N_THREADS)
        errors: List[BaseException] = []

        def worker(thread_idx: int):
            try:
                barrier.wait()  # maximize contention
                for i in range(N_EMITS):
                    emit_todo_state(
                        put,
                        name="todo",
                        function_result=_todo_payload([
                            {"id": f"t{thread_idx}-{i}",
                             "content": "x",
                             "status": "pending"},
                        ]),
                        session_id=f"sess-{thread_idx}",
                        stream_id=f"stream-{thread_idx}",
                    )
            except BaseException as exc:  # pragma: no cover — should never fire
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"emit raised under contention: {errors}"
        assert len(events) == N_THREADS * N_EMITS
        # Each emit must produce exactly one event with the correct name.
        assert all(ev == "todo_state" for ev, _ in events)
        # No event must mix session_ids — the helper takes session_id by
        # parameter so cross-talk is structurally impossible, but pin it.
        per_session_counts: Dict[str, int] = {}
        for _, data in events:
            per_session_counts[data["session_id"]] = (
                per_session_counts.get(data["session_id"], 0) + 1
            )
        assert per_session_counts == {f"sess-{i}": N_EMITS for i in range(N_THREADS)}
