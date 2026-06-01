"""End-to-end real-world scenario coverage for the ``todo_state`` pipeline.

Each test in this file maps to a concrete user-facing scenario that
showed up in the original review. Together they exercise:

  * **Live SSE path** — ``api.todo_state.emit_todo_state`` driven from
    the legacy and modern tool callbacks.
  * **Cold-load path** — ``api.todo_state.attach_todo_state`` invoked
    from both the WebUI session GET and the CLI session fallback.
  * **Round-trips** — emit a snapshot, replay the same payload as a
    settled tool message, and confirm cold-load returns the same
    todos.

The wire-format pin (`SSE event keys`, `session GET payload key`) is
duplicated here on purpose so a regression that breaks the frontend
contract surfaces in *both* the emission unit tests and the scenario
tests.

Cross-cutting expectations
--------------------------
Backend payload correctness:

* ``emit_todo_state`` events always carry
  ``{session_id, stream_id, source, ts, todos, summary, version}``.
* ``attach_todo_state`` injects exactly one key into the response —
  ``todo_state`` — whose value is ``{todos, summary, version}``
  (no SSE-only fields like ``ts`` leak into cold-load).
* Snapshots are full — re-applying the same one is a no-op.
* Identical input → identical output (deterministic).
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple

from api.todo_state import (
    EVENT_NAME,
    PAYLOAD_KEY,
    VERSION,
    attach_todo_state,
    emit_todo_state,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _todo_payload(todos: List[Dict[str, str]]) -> str:
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


def _user_msg(text: str) -> Dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant_msg(text: str) -> Dict[str, Any]:
    return {"role": "assistant", "content": text}


def _tool_msg(content: str) -> Dict[str, Any]:
    return {"role": "tool", "content": content}


def _multimodal_tool_msg() -> Dict[str, Any]:
    """Realistic Anthropic/OpenAI multimodal content shape — list of parts."""
    return {
        "role": "tool",
        "content": [
            {"type": "text", "text": "saw a cat"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,iVBOR..."}},
        ],
    }


class _Recorder:
    """Captures ``put(event, data)`` calls."""

    def __init__(self) -> None:
        self.events: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, event: str, data: Dict[str, Any]) -> None:
        self.events.append((event, data))

    def todo_events(self) -> List[Dict[str, Any]]:
        return [d for (e, d) in self.events if e == EVENT_NAME]


# ---------------------------------------------------------------------------
# Scenario 1 — Live mid-turn updates
# ---------------------------------------------------------------------------


class TestScenarioLiveMidTurn:
    """Agent walks the todos panel from pending → in_progress → done."""

    def test_three_writes_produce_three_distinct_snapshots(self):
        rec = _Recorder()
        # Initial plan.
        emit_todo_state(rec, name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "design", "status": "in_progress"},
                {"id": "2", "content": "implement", "status": "pending"},
                {"id": "3", "content": "test", "status": "pending"},
            ]),
            session_id="s", stream_id="r")
        # Mid-turn step 1.
        emit_todo_state(rec, name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "design", "status": "completed"},
                {"id": "2", "content": "implement", "status": "in_progress"},
                {"id": "3", "content": "test", "status": "pending"},
            ]),
            session_id="s", stream_id="r")
        # Mid-turn step 2.
        emit_todo_state(rec, name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "design", "status": "completed"},
                {"id": "2", "content": "implement", "status": "completed"},
                {"id": "3", "content": "test", "status": "in_progress"},
            ]),
            session_id="s", stream_id="r")

        evs = rec.todo_events()
        assert len(evs) == 3
        statuses = [
            tuple(t["status"] for t in ev["todos"])
            for ev in evs
        ]
        assert statuses == [
            ("in_progress", "pending",     "pending"),
            ("completed",   "in_progress", "pending"),
            ("completed",   "completed",   "in_progress"),
        ]

    def test_ts_is_monotonic_within_a_stream(self):
        rec = _Recorder()
        for _ in range(5):
            emit_todo_state(rec, name="todo",
                function_result=_todo_payload([
                    {"id": "1", "content": "x", "status": "pending"},
                ]),
                session_id="s", stream_id="r")
            time.sleep(0.001)  # ensure clock advances on fast machines
        ts_values = [ev["ts"] for ev in rec.todo_events()]
        # Every consecutive ts must be >= prior. Frontend uses this
        # ordering to drop strictly-older snapshots on replay.
        for i in range(1, len(ts_values)):
            assert ts_values[i] >= ts_values[i - 1]

    def test_full_snapshot_idempotent_on_double_emit(self):
        # Frontend treats incoming snapshots as full replacements, never
        # diffs.  Re-applying the exact same snapshot must paint the
        # exact same DOM — captured here by checking payload equality.
        rec = _Recorder()
        payload = _todo_payload([
            {"id": "1", "content": "x", "status": "in_progress"},
        ])
        emit_todo_state(rec, name="todo", function_result=payload,
            session_id="s", stream_id="r")
        emit_todo_state(rec, name="todo", function_result=payload,
            session_id="s", stream_id="r")
        a, b = rec.todo_events()
        for k in ("todos", "summary", "version", "session_id",
                  "stream_id", "source"):
            assert a[k] == b[k], f"field {k} drifted across identical emits"


# ---------------------------------------------------------------------------
# Scenario 2 — Cold reopen
# ---------------------------------------------------------------------------


class TestScenarioColdReopen:
    """Browser opens a settled session — the panel must paint immediately."""

    def test_session_with_todo_history_attaches_snapshot(self):
        msgs = [
            _user_msg("plan and execute"),
            _assistant_msg("here's the plan"),
            _tool_msg(_todo_payload([
                {"id": "1", "content": "step 1", "status": "completed"},
                {"id": "2", "content": "step 2", "status": "in_progress"},
            ])),
            _assistant_msg("working on step 2"),
        ]
        payload: Dict[str, Any] = {"session_id": "s", "messages": msgs}
        ok = attach_todo_state(payload, msgs)
        assert ok is True
        snap = payload[PAYLOAD_KEY]
        assert snap["version"] == VERSION
        assert len(snap["todos"]) == 2
        # Cold-load payload must NOT carry SSE-only fields.
        for k in ("session_id", "stream_id", "source", "ts"):
            assert k not in snap

    def test_session_without_todo_attaches_nothing(self):
        msgs = [
            _user_msg("just a chat"),
            _assistant_msg("hi"),
            _tool_msg('{"result":"ok"}'),
        ]
        payload: Dict[str, Any] = {"session_id": "s"}
        ok = attach_todo_state(payload, msgs)
        assert ok is False
        assert PAYLOAD_KEY not in payload

    def test_attach_picks_latest_when_multiple_writes_exist(self):
        # Same session, todo was rewritten 4 times across many turns.
        # Cold-load must reflect the FINAL state, not any intermediate.
        history: List[Dict[str, Any]] = []
        for i in range(4):
            history.append(_assistant_msg(f"turn {i}"))
            history.append(_tool_msg(_todo_payload([
                {"id": "1", "content": "x",
                 "status": "in_progress" if i < 3 else "completed"},
            ])))
        payload: Dict[str, Any] = {}
        attach_todo_state(payload, history)
        assert payload[PAYLOAD_KEY]["todos"][0]["status"] == "completed"


# ---------------------------------------------------------------------------
# Scenario 3 — SSE replay safety
# ---------------------------------------------------------------------------


class TestScenarioSSEReplay:
    """Frontend reconnects mid-turn; journal replays old events."""

    def test_replayed_snapshot_matches_original_payload(self):
        # The run journal stores SSE events verbatim. Replaying them
        # through ``put`` (effectively re-running ``emit_todo_state``
        # with the same args) must yield the same wire payload — minus
        # the freshly-stamped ``ts``, which is intentionally re-clocked.
        rec_live = _Recorder()
        result_json = _todo_payload([
            {"id": "1", "content": "x", "status": "in_progress"},
        ])
        emit_todo_state(rec_live, name="todo",
            function_result=result_json,
            session_id="s", stream_id="r")

        rec_replay = _Recorder()
        emit_todo_state(rec_replay, name="todo",
            function_result=result_json,
            session_id="s", stream_id="r")

        live = rec_live.events[0][1]
        replay = rec_replay.events[0][1]
        # All snapshot fields are byte-identical.
        for k in ("todos", "summary", "version",
                  "session_id", "stream_id", "source"):
            assert live[k] == replay[k]
        # Both ts are valid wall-clock floats.
        assert live["ts"] > 0 and replay["ts"] > 0


# ---------------------------------------------------------------------------
# Scenario 4 — Cross-session and stream addressing
# ---------------------------------------------------------------------------


class TestScenarioAddressing:
    """Backend tags each event so the frontend can filter cross-session."""

    def test_two_sessions_get_two_addresses(self):
        rec = _Recorder()
        emit_todo_state(rec, name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "a", "status": "pending"},
            ]),
            session_id="sess-A", stream_id="stream-A1")
        emit_todo_state(rec, name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "b", "status": "pending"},
            ]),
            session_id="sess-B", stream_id="stream-B1")
        evs = rec.todo_events()
        assert evs[0]["session_id"] == "sess-A"
        assert evs[1]["session_id"] == "sess-B"
        assert evs[0]["stream_id"] != evs[1]["stream_id"]

    def test_two_clients_on_same_stream_see_same_address(self):
        # Two browser tabs both attach to the same active stream id.
        # Each emission produces one event tagged with that stream;
        # both clients dispatch off the same payload.
        rec = _Recorder()
        for _ in range(2):  # two emissions during one shared stream
            emit_todo_state(rec, name="todo",
                function_result=_todo_payload([
                    {"id": "1", "content": "x", "status": "pending"},
                ]),
                session_id="s", stream_id="shared-stream")
        evs = rec.todo_events()
        assert all(ev["stream_id"] == "shared-stream" for ev in evs)
        assert all(ev["session_id"] == "s" for ev in evs)


# ---------------------------------------------------------------------------
# Scenario 5 — Multimodal / vision-capable tool results
# ---------------------------------------------------------------------------


class TestScenarioMultimodal:
    """Vision tools return list-shaped content; todo never does."""

    def test_multimodal_tool_messages_dont_disturb_attach(self):
        # A todo write sits between two image-bearing tool results. The
        # multimodal entries are skipped (content is a list, not str)
        # and the cold-load returns the todo snapshot intact.
        msgs = [
            _multimodal_tool_msg(),
            _tool_msg(_todo_payload([
                {"id": "1", "content": "review image", "status": "pending"},
            ])),
            _multimodal_tool_msg(),
        ]
        payload: Dict[str, Any] = {}
        ok = attach_todo_state(payload, msgs)
        assert ok is True
        assert payload[PAYLOAD_KEY]["todos"][0]["content"] == "review image"

    def test_multimodal_tool_after_todo_does_not_clobber(self):
        # The most recent message is multimodal (image only). The prior
        # todo write must still surface — otherwise opening a session
        # whose last tool was an image read would erase the panel.
        msgs = [
            _tool_msg(_todo_payload([
                {"id": "1", "content": "earlier", "status": "in_progress"},
            ])),
            _multimodal_tool_msg(),
        ]
        payload: Dict[str, Any] = {}
        attach_todo_state(payload, msgs)
        assert payload[PAYLOAD_KEY]["todos"][0]["status"] == "in_progress"

    def test_emit_skips_image_bearing_tool(self):
        # If a tool callback ever fires with name="todo" but a
        # multimodal result (which would be a bug elsewhere), the emit
        # helper falls through cleanly — list isn't a JSON string, isn't
        # a dict with a todos list, so parse returns None.
        rec = _Recorder()
        result = emit_todo_state(rec, name="todo",
            function_result=[{"type": "text", "text": "nope"}],
            session_id="s", stream_id="r")
        assert result is False
        assert rec.todo_events() == []


# ---------------------------------------------------------------------------
# Scenario 6 — CLI session fallback
# ---------------------------------------------------------------------------


class TestScenarioCLISession:
    """A CLI-only session has no WebUI sidecar — routes.py uses a
    different code path. The cold-load helper must work identically."""

    def test_cli_session_messages_attach_correctly(self):
        # Mirrors the shape ``get_cli_session_messages`` returns: a flat
        # list of dicts with role/content/timestamp.
        msgs = [
            {"role": "user", "content": "hello", "timestamp": 1.0},
            {"role": "assistant", "content": "hi", "timestamp": 2.0},
            {"role": "tool", "content": _todo_payload([
                {"id": "cli-1", "content": "do it", "status": "pending"},
            ]), "timestamp": 3.0},
        ]
        sess: Dict[str, Any] = {
            "session_id": "cli-sid",
            "is_cli_session": True,
            "messages": msgs,
        }
        ok = attach_todo_state(sess, msgs)
        assert ok is True
        assert sess[PAYLOAD_KEY]["todos"][0]["id"] == "cli-1"
        # Other CLI-session fields must survive.
        assert sess["is_cli_session"] is True
        assert sess["session_id"] == "cli-sid"

    def test_cli_session_without_todo_unchanged(self):
        msgs = [
            {"role": "user", "content": "hi", "timestamp": 1.0},
            {"role": "assistant", "content": "hello", "timestamp": 2.0},
        ]
        sess: Dict[str, Any] = {"session_id": "cli", "messages": msgs}
        ok = attach_todo_state(sess, msgs)
        assert ok is False
        assert PAYLOAD_KEY not in sess


# ---------------------------------------------------------------------------
# Scenario 7 — Round-trip: emit → store as message → attach
# ---------------------------------------------------------------------------


class TestScenarioEmitAttachRoundTrip:
    """The same underlying tool-result string drives both paths.

    When the agent emits live, it later writes the same JSON string
    into ``messages[]`` as the tool result of that turn. Cold-load
    must therefore return a snapshot with the same todos/summary as
    the live event — same ``version`` and identical contents minus
    SSE-only addressing fields.
    """

    def test_live_emit_and_cold_load_agree_on_snapshot_body(self):
        result_json = _todo_payload([
            {"id": "1", "content": "design", "status": "completed"},
            {"id": "2", "content": "implement", "status": "in_progress"},
            {"id": "3", "content": "test", "status": "pending"},
        ])

        # Live SSE emission
        rec = _Recorder()
        emit_todo_state(rec, name="todo", function_result=result_json,
            session_id="s", stream_id="r")
        live = rec.todo_events()[0]

        # Cold-load from the same JSON tucked into messages[]
        payload: Dict[str, Any] = {}
        attach_todo_state(payload, [_tool_msg(result_json)])
        cold = payload[PAYLOAD_KEY]

        # Body must agree exactly.
        assert live["todos"] == cold["todos"]
        assert live["summary"] == cold["summary"]
        assert live["version"] == cold["version"]

        # Cold-load is leaner — no SSE addressing.
        assert set(cold.keys()) == {"todos", "summary", "version"}
        assert {"session_id", "stream_id", "source", "ts"}.issubset(
            set(live.keys())
        )

    def test_session_with_only_emits_then_replay_yields_same_state(self):
        # Simulate: agent emits N times. Each emission's full snapshot
        # gets stored as a tool message in conversation history. After
        # the turn ends, the user reloads the session — cold-load must
        # surface the exact final emit's snapshot.
        history: List[Dict[str, Any]] = [_user_msg("plan")]
        rec = _Recorder()
        for status in ("pending", "in_progress", "completed"):
            jp = _todo_payload([
                {"id": "1", "content": "x", "status": status},
            ])
            emit_todo_state(rec, name="todo", function_result=jp,
                session_id="s", stream_id="r")
            history.append(_tool_msg(jp))

        cold_payload: Dict[str, Any] = {}
        attach_todo_state(cold_payload, history)
        last_live = rec.todo_events()[-1]

        # Last live emit and final cold-load agree on the last status.
        assert last_live["todos"][0]["status"] == "completed"
        assert cold_payload[PAYLOAD_KEY]["todos"][0]["status"] == "completed"


# ---------------------------------------------------------------------------
# Scenario 8 — Failure isolation
# ---------------------------------------------------------------------------


class TestScenarioFailureIsolation:
    """A bad payload must never break the SSE stream or session GET."""

    def test_emit_does_not_raise_on_garbage_result(self):
        rec = _Recorder()
        for bad in (None, b"\x00\x01", object(), [{"role": "tool"}]):
            try:
                emit_todo_state(rec, name="todo", function_result=bad,
                    session_id="s", stream_id="r")
            except Exception as exc:  # pragma: no cover — defensive
                raise AssertionError(
                    f"emit_todo_state raised on {bad!r}: {exc}"
                ) from exc
        # No event should have leaked through; all silently dropped.
        assert rec.todo_events() == []

    def test_emit_swallows_put_callback_exceptions(self):
        seen: List[bool] = []

        def angry_put(_event, _data):
            seen.append(True)
            raise ValueError("queue is dead")

        ok = emit_todo_state(angry_put, name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
            session_id="s", stream_id="r")
        assert ok is False
        # ``put`` was attempted exactly once; the helper did not retry,
        # so a broken queue can't snowball into N failed retries.
        assert seen == [True]

    def test_attach_does_not_raise_on_corrupt_message_in_history(self):
        # A single corrupt entry must not poison the whole derive.
        msgs: List[Any] = [
            _tool_msg(_todo_payload([
                {"id": "1", "content": "valid", "status": "pending"},
            ])),
            123,  # bogus
            {"role": "tool", "content": object()},  # not str/list
        ]
        payload: Dict[str, Any] = {}
        ok = attach_todo_state(payload, msgs)
        assert ok is True
        assert payload[PAYLOAD_KEY]["todos"][0]["content"] == "valid"


# ---------------------------------------------------------------------------
# Scenario 9 — Long histories
# ---------------------------------------------------------------------------


class TestScenarioLongHistories:
    """Cold-load must stay fast and correct on multi-thousand-msg sessions."""

    def test_recent_todo_in_long_history(self):
        # Realistic: a session has 10k messages; a todo write happens
        # in the last 50.  Reverse iteration short-circuits.
        msgs: List[Dict[str, Any]] = []
        for i in range(9_950):
            msgs.append(_assistant_msg(f"turn {i}"))
        msgs.append(_tool_msg(_todo_payload([
            {"id": "late", "content": "ship", "status": "in_progress"},
        ])))
        for i in range(49):
            msgs.append(_assistant_msg(f"after {i}"))

        payload: Dict[str, Any] = {}
        t0 = time.time()
        ok = attach_todo_state(payload, msgs)
        t1 = time.time()
        assert ok is True
        assert payload[PAYLOAD_KEY]["todos"][0]["id"] == "late"
        # Soft perf canary: typical run is sub-millisecond. 200 ms is a
        # generous ceiling for slow CI; if we ever exceed this, the
        # algorithm has regressed (e.g. someone removed the substring
        # fast-path or accidentally introduced N² behavior).
        assert (t1 - t0) < 0.2, f"derive_todo_state took {t1-t0:.3f}s"

    def test_no_todo_in_long_history_returns_false_quickly(self):
        msgs: List[Dict[str, Any]] = []
        for i in range(5_000):
            msgs.append(_assistant_msg(f"t{i}"))
            msgs.append(_tool_msg('{"output":"ok"}'))
        payload: Dict[str, Any] = {}
        t0 = time.time()
        ok = attach_todo_state(payload, msgs)
        t1 = time.time()
        assert ok is False
        # 5k tool messages, all bypass the JSON parse via the substring
        # gate. Should still be cheap.
        assert (t1 - t0) < 0.5, f"derive_todo_state took {t1-t0:.3f}s"


# ---------------------------------------------------------------------------
# Scenario 10 — Frontend wire-format pin
# ---------------------------------------------------------------------------


class TestScenarioWireFormatPin:
    """Pin every key the frontend reads.

    Hardcoded against ``static/messages.js`` (live SSE listener) and
    ``static/ui.js`` (cold-load hydrate). If either side changes, this
    test must change too — by design.
    """

    LIVE_KEYS = {
        # Identity / addressing.
        "session_id", "stream_id", "source",
        # Ordering.
        "ts",
        # Snapshot.
        "todos", "summary", "version",
    }

    COLD_KEYS = {"todos", "summary", "version"}

    def test_live_event_carries_every_frontend_field(self):
        rec = _Recorder()
        emit_todo_state(rec, name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "x", "status": "pending"},
            ]),
            session_id="s", stream_id="r")
        ev_data = rec.events[0][1]
        assert self.LIVE_KEYS.issubset(set(ev_data.keys())), (
            f"missing live wire keys: {self.LIVE_KEYS - set(ev_data.keys())}"
        )

    def test_cold_payload_carries_only_snapshot_fields(self):
        payload: Dict[str, Any] = {}
        attach_todo_state(payload, [_tool_msg(_todo_payload([
            {"id": "1", "content": "x", "status": "pending"},
        ]))])
        cold = payload[PAYLOAD_KEY]
        assert set(cold.keys()) == self.COLD_KEYS

    def test_event_name_matches_frontend_listener(self):
        # ``static/messages.js``: source.addEventListener('todo_state', ...)
        assert EVENT_NAME == "todo_state"

    def test_payload_key_matches_frontend_cold_load(self):
        # ``static/ui.js``: const cold = session && session.todo_state
        assert PAYLOAD_KEY == "todo_state"

    def test_todo_item_shape_matches_frontend_renderer(self):
        # ``static/panels.js`` renders id / content / status. Pin those
        # so a backend rename (e.g. ``status`` → ``state``) breaks here
        # and not in production.
        rec = _Recorder()
        emit_todo_state(rec, name="todo",
            function_result=_todo_payload([
                {"id": "1", "content": "x", "status": "in_progress"},
            ]),
            session_id="s", stream_id="r")
        item = rec.events[0][1]["todos"][0]
        assert "id" in item
        assert "content" in item
        assert "status" in item
        assert item["status"] in {
            "pending", "in_progress", "completed", "cancelled",
        }


# ---------------------------------------------------------------------------
# Scenario 11 — Mid-stream state evolution + final hydrate
# ---------------------------------------------------------------------------


class TestScenarioFullTurnLifecycle:
    """Simulate one full agent turn end-to-end:
    user → 3 emits during streaming → turn settles → reload → cold-load.
    """

    def test_full_turn_lifecycle(self):
        rec = _Recorder()
        history: List[Dict[str, Any]] = [_user_msg("design and ship")]

        emit_payloads = []
        for plan in [
            [
                {"id": "d", "content": "design",   "status": "in_progress"},
                {"id": "i", "content": "impl",     "status": "pending"},
                {"id": "t", "content": "test",     "status": "pending"},
            ],
            [
                {"id": "d", "content": "design",   "status": "completed"},
                {"id": "i", "content": "impl",     "status": "in_progress"},
                {"id": "t", "content": "test",     "status": "pending"},
            ],
            [
                {"id": "d", "content": "design",   "status": "completed"},
                {"id": "i", "content": "impl",     "status": "completed"},
                {"id": "t", "content": "test",     "status": "completed"},
            ],
        ]:
            jp = _todo_payload(plan)
            emit_payloads.append(jp)
            emit_todo_state(rec, name="todo", function_result=jp,
                session_id="s", stream_id="r")
            history.append(_tool_msg(jp))

        # Final assistant message.
        history.append(_assistant_msg("shipped"))

        # During the live turn, the panel saw 3 distinct snapshots in order.
        evs = rec.todo_events()
        assert len(evs) == 3
        statuses = [
            tuple(t["status"] for t in ev["todos"])
            for ev in evs
        ]
        assert statuses[0][0] == "in_progress"
        assert statuses[2] == ("completed", "completed", "completed")

        # Cold-load reflects ONLY the latest write — exactly the final
        # emit's snapshot body.
        payload: Dict[str, Any] = {}
        attach_todo_state(payload, history)
        cold = payload[PAYLOAD_KEY]
        assert cold["todos"] == evs[-1]["todos"]
        assert cold["summary"] == evs[-1]["summary"]
        assert cold["version"] == VERSION


# ---------------------------------------------------------------------------
# Scenario 12 — Determinism
# ---------------------------------------------------------------------------


class TestScenarioDeterminism:
    """Same input → same output (modulo the freshly-stamped ``ts``)."""

    def test_emit_is_deterministic_modulo_ts(self):
        result_json = _todo_payload([
            {"id": "1", "content": "x", "status": "pending"},
        ])
        a = _Recorder()
        b = _Recorder()
        emit_todo_state(a, name="todo", function_result=result_json,
            session_id="s", stream_id="r")
        emit_todo_state(b, name="todo", function_result=result_json,
            session_id="s", stream_id="r")
        ev_a = {k: v for k, v in a.events[0][1].items() if k != "ts"}
        ev_b = {k: v for k, v in b.events[0][1].items() if k != "ts"}
        assert ev_a == ev_b

    def test_attach_is_fully_deterministic(self):
        msgs = [_tool_msg(_todo_payload([
            {"id": "1", "content": "x", "status": "in_progress"},
        ]))]
        a: Dict[str, Any] = {}
        b: Dict[str, Any] = {}
        attach_todo_state(a, msgs)
        attach_todo_state(b, msgs)
        # No ``ts`` on cold-load — full byte equality.
        assert a == b
