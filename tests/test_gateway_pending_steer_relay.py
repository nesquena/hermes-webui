"""Regression tests for gateway terminal pending_steer translation (#7440 gate).

The installed Agent preserves unconsumed mid-run guidance in the terminal
``run.completed`` payload's ``pending_steer`` field. The Runs-API translator
(``_run_gateway_runs_api_streaming``) must convert a non-empty
``pending_steer`` into the EXISTING ``pending_steer_leftover`` SSE event
(session id + text) before terminal completion, so the frontend's existing
listener queues it for the next turn — the same convention as the in-process
path's end-of-turn drain (api/streaming.py).

The exact-once reconnect property is proven against the REAL run-journal
replay reader (``read_run_events(after_seq=...)`` — the exact call
``routes._replay_run_journal`` makes for a reconnecting SSE client): the
leftover event replays exactly once per cursor window and never again once
the client cursor advances past it.

All gateway HTTP is faked at ``urllib.request.urlopen`` (POST /v1/runs returns
a run id; the events stream is canned SSE). The journal is REAL
(``RunJournalWriter`` + ``read_run_events`` against a temporary session dir);
no real gateway, sockets, or user state are used.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).parent.parent
MESSAGES_JS = REPO / "static" / "messages.js"


@pytest.fixture(autouse=True)
def _gateway_run_state_isolation():
    """Snapshot/restore gateway run-id lifecycle state around each test."""
    from api import gateway_chat

    with gateway_chat._STREAM_RUN_STARTING_CONDITION:
        prior_ids = dict(gateway_chat._STREAM_RUN_IDS)
        prior_lifecycle = {
            key: dict(value) for key, value in gateway_chat._STREAM_RUN_LIFECYCLE.items()
        }
    try:
        yield
    finally:
        with gateway_chat._STREAM_RUN_STARTING_CONDITION:
            gateway_chat._STREAM_RUN_IDS.clear()
            gateway_chat._STREAM_RUN_IDS.update(prior_ids)
            gateway_chat._STREAM_RUN_LIFECYCLE.clear()
            gateway_chat._STREAM_RUN_LIFECYCLE.update(prior_lifecycle)
            gateway_chat._STREAM_RUN_STARTING_CONDITION.notify_all()


class _JsonResponse:
    """Context-managed JSON response for POST /v1/runs."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, _limit=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class _SseResponse:
    """Context-managed SSE events stream (iterable raw lines)."""

    def __init__(self, lines=()):
        self._lines = [line if isinstance(line, bytes) else line.encode("utf-8") for line in lines]

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _run_translator(tmp_path, session_id, stream_id, sse_lines, cancel_event=None):
    """Drive the REAL runs-API bridge with a REAL journaling put_gateway_event.

    The ``put_gateway_event`` closure mirrors the production one from
    ``_run_gateway_chat_streaming``: every event is appended to the real run
    journal (assigning the event_id/seq the SSE layer re-emits on reconnect)
    and recorded in ``emitted``.
    """
    from api import gateway_chat
    from api.run_journal import RunJournalWriter

    journal = RunJournalWriter(session_id, stream_id, session_dir=tmp_path)
    emitted: list[tuple[str, dict, dict]] = []

    def put_gateway_event(event, data):
        entry = journal.append_sse_event(event, data)
        emitted.append((event, data, entry))

    def fake_urlopen(req, *, timeout=None):
        if req.get_method() == "POST" and req.full_url.endswith("/v1/runs"):
            return _JsonResponse({"run_id": f"run-{stream_id}"})
        if req.get_method() == "GET" and req.full_url.endswith("/events"):
            return _SseResponse(sse_lines)
        raise AssertionError(f"unexpected gateway request: {req.get_method()} {req.full_url}")

    monkeypatch_target = urllib.request
    original_urlopen = monkeypatch_target.urlopen
    monkeypatch_target.urlopen = fake_urlopen
    try:
        final_text, usage = gateway_chat._run_gateway_runs_api_streaming(
            session_id=session_id,
            msg_text="hello",
            model="test-model",
            workspace="/tmp/nowhere",
            stream_id=stream_id,
            base_url="http://gateway.test",
            api_key="test-key",
            prefill_messages=[],
            body_extras={},
            put_gateway_event=put_gateway_event,
            cancel_event=cancel_event or threading.Event(),
            session=SimpleNamespace(context_messages=[]),
        )
    finally:
        monkeypatch_target.urlopen = original_urlopen
    return SimpleNamespace(final_text=final_text, usage=usage, emitted=emitted)


def _journal_rows(tmp_path, session_id, stream_id):
    from api.run_journal import read_run_events

    journal = read_run_events(session_id, stream_id, session_dir=tmp_path)
    return [row for row in (journal.get("events") or []) if isinstance(row, dict)]


def _completed_lines(payload: dict) -> list[bytes]:
    return [
        b"event: run.completed\n",
        f"data: {json.dumps(payload)}\n".encode("utf-8"),
        b"\n",
    ]


# ---------------------------------------------------------------------------
# Terminal pending_steer translation
# ---------------------------------------------------------------------------


def test_run_completed_pending_steer_emits_leftover_event(tmp_path):
    session_id = "sess-psteer-basic"
    stream_id = "stream-psteer-basic"
    result = _run_translator(
        tmp_path,
        session_id,
        stream_id,
        _completed_lines({"output": "done", "pending_steer": "use the safer path"}),
    )

    assert result.final_text == "done"
    leftovers = [
        (event, data)
        for event, data, _entry in result.emitted
        if event == "pending_steer_leftover"
    ]
    assert leftovers == [
        ("pending_steer_leftover", {"session_id": session_id, "text": "use the safer path"})
    ]
    # Journal rows carry the envelope the SSE replay layer re-emits.
    rows = _journal_rows(tmp_path, session_id, stream_id)
    leftover_rows = [row for row in rows if row.get("event") == "pending_steer_leftover"]
    assert len(leftover_rows) == 1
    row = leftover_rows[0]
    assert row.get("payload") == {"session_id": session_id, "text": "use the safer path"}
    assert row.get("event_id")
    assert int(row.get("seq") or 0) > 0
    # Emitted BEFORE terminal completion: it is the last event the translator
    # journals (the caller's terminal done/stream_end events come after).
    assert row.get("seq") == max(int(r.get("seq") or 0) for r in rows)


@pytest.mark.parametrize(
    "pending_value",
    [None, "", "   \t "],
    ids=["missing", "empty", "whitespace"],
)
def test_run_completed_without_pending_steer_emits_no_leftover(tmp_path, pending_value):
    payload = {"output": "done"}
    if pending_value is not None:
        payload["pending_steer"] = pending_value
    result = _run_translator(
        tmp_path,
        "sess-psteer-none",
        "stream-psteer-none",
        _completed_lines(payload),
    )

    assert result.final_text == "done"
    assert not [e for e in result.emitted if e[0] == "pending_steer_leftover"]
    rows = _journal_rows(tmp_path, "sess-psteer-none", "stream-psteer-none")
    assert not [row for row in rows if row.get("event") == "pending_steer_leftover"]


def test_run_completed_error_completion_emits_no_leftover(tmp_path):
    """Error completions raise before leftover translation (local-path parity:
    the in-process drain only runs on successful turn completion)."""
    session_id = "sess-psteer-error"
    stream_id = "stream-psteer-error"
    lines = [
        b"event: run.completed\n",
        b'data: {"event": "run.completed", "error": "model exploded", "pending_steer": "x"}\n',
        b"\n",
    ]
    with pytest.raises(RuntimeError):
        _run_translator(tmp_path, session_id, stream_id, lines)
    rows = _journal_rows(tmp_path, session_id, stream_id)
    assert not [row for row in rows if row.get("event") == "pending_steer_leftover"]


def test_run_completed_pending_steer_suppressed_when_cancelled(tmp_path):
    """A cancelled turn must not queue leftover guidance (parity with the
    production put_gateway_event cancel guard and the in-process path)."""
    cancel_event = threading.Event()
    cancel_event.set()
    result = _run_translator(
        tmp_path,
        "sess-psteer-cancel",
        "stream-psteer-cancel",
        _completed_lines({"output": "done", "pending_steer": "use the safer path"}),
        cancel_event=cancel_event,
    )
    assert result.final_text is None
    rows = _journal_rows(tmp_path, "sess-psteer-cancel", "stream-psteer-cancel")
    assert not [row for row in rows if row.get("event") == "pending_steer_leftover"]


# ---------------------------------------------------------------------------
# Exact-once reconnect replay (real journal reader, real cursor semantics)
# ---------------------------------------------------------------------------


def test_leftover_event_replays_exactly_once_across_reconnect_cursors(tmp_path):
    session_id = "sess-psteer-replay"
    stream_id = "stream-psteer-replay"
    _run_translator(
        tmp_path,
        session_id,
        stream_id,
        _completed_lines({"output": "done", "pending_steer": "use the safer path"}),
    )

    rows = _journal_rows(tmp_path, session_id, stream_id)
    leftover_rows = [row for row in rows if row.get("event") == "pending_steer_leftover"]
    assert len(leftover_rows) == 1
    leftover_seq = int(leftover_rows[0].get("seq") or 0)
    leftover_event_id = leftover_rows[0].get("event_id")

    from api.run_journal import read_run_events

    def replayed(after_seq):
        journal = read_run_events(session_id, stream_id, session_dir=tmp_path, after_seq=after_seq)
        return [
            row
            for row in (journal.get("events") or [])
            if row.get("event") == "pending_steer_leftover"
        ]

    # Reconnect with a cursor BEFORE the leftover: replayed exactly once,
    # with the same durable event_id the SSE layer re-emits.
    before = replayed(leftover_seq - 1)
    assert len(before) == 1
    assert before[0].get("event_id") == leftover_event_id
    # Two consecutive reconnects with the same cursor: still exactly one per
    # replay window (no intra-window duplication).
    assert len(replayed(leftover_seq - 1)) == 1
    # Reconnect with the cursor AT/after the leftover (the browser advanced its
    # Last-Event-ID past it): never replayed again.
    assert replayed(leftover_seq) == []
    assert replayed(leftover_seq + 1) == []
    # Fresh full replay (cursor reset to start): present exactly once overall.
    assert len(replayed(None)) == 1


# ---------------------------------------------------------------------------
# Frontend reconnect contract (static source guards)
# ---------------------------------------------------------------------------


def test_frontend_reconnect_contract_for_leftover_event():
    """The exact-once reconnect property needs BOTH frontend halves: the
    ``pending_steer_leftover`` listener (queue-for-next-turn, same as the
    in-process path) and its inclusion in the run-journal cursor-advance
    list (so a reconnecting client's Last-Event-ID moves past the leftover
    and it is never double-queued)."""
    messages_src = MESSAGES_JS.read_text(encoding="utf-8")
    assert "source.addEventListener('pending_steer_leftover'" in messages_src, (
        "messages.js must keep the pending_steer_leftover SSE listener"
    )
    cursor_loop_pos = messages_src.index("for(const _runJournalEventName of [")
    cursor_loop = messages_src[
        cursor_loop_pos : messages_src.index("]", cursor_loop_pos)
    ]
    assert "'pending_steer_leftover'" in cursor_loop, (
        "pending_steer_leftover must advance the run-journal replay cursor "
        "or a reconnecting client double-queues the leftover guidance"
    )


def _leftover_listener_statement() -> str:
    """Slice the real pending_steer_leftover listener registration out of
    messages.js (implementation is never copied into the test)."""
    src = MESSAGES_JS.read_text(encoding="utf-8")
    start = src.index("source.addEventListener('pending_steer_leftover',e=>{")
    end = src.index("\n    });", start) + len("\n    });")
    return src[start:end]


def _find_node() -> str:
    for command in ("node", "node.exe"):
        path = shutil.which(command)
        if path:
            return path
    pytest.skip("node is not available for the messages.js VM test")


def test_leftover_listener_queues_for_owner_across_session_switch():
    """Unit-level lock of the listener's owner-scoping semantics (greptile
    P1/P2, #7440): queue the leftover for the OWNING session id even when
    another session is viewed, keep toast/anchor limited to the owner being
    the active view, and never leak the current picker's model state into
    another session's queue entry (setBusy's drain restores the model from
    the queued item).

    Executes the REAL listener source (sliced from static/messages.js) in a
    Node VM against stubbed collaborators. This is the fast contract lock;
    the application's real stream/session-switch/queue-drain wiring for the
    same behavior is proven end-to-end (real server, real Chromium, real
    Gateway fixture) by tests/browser_gateway_steer_leftover.py, which is
    red on a tree without the server-side pending_steer translation."""
    listener_stmt = _leftover_listener_statement()
    # Static guard first: the view-mismatch drop must be gone.
    assert "sid!==activeSid" not in listener_stmt, (
        "the leftover listener must not drop the event when the owning "
        "session is not the active view"
    )

    def _fire_expr(data: dict) -> str:
        return f"__handler({{data: {json.dumps(json.dumps(data))}}});"

    scenario_scripts = {
        "viewed": _fire_expr({"session_id": "sess-a", "text": "use the safer path"}),
        "switched": _fire_expr({"session_id": "sess-a", "text": "use the safer path"}),
        "empty": _fire_expr({"session_id": "sess-a", "text": "   "}),
        "fallback_sid": _fire_expr({"text": "legacy event"}),
    }

    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const ctx = vm.createContext({{
            console, JSON, String,
            activeSid: 'sess-a',
            S: {{session: {{session_id: 'sess-a'}}, activeProfile: 'default'}},
            queueSessionMessage: () => 0, updateQueueBadge: () => {{}},
            showToast: () => {{}}, _applyToAnchor: () => {{}},
            _chatPayloadModelState: () => ({{}}), t: (k) => k,
        }});
        vm.runInContext(
            'this.__calls = {{queue: [], badge: [], toast: [], anchor: []}}; ' +
            'this.source = {{addEventListener: (name, fn) => {{ this.__handler = fn; }}}};',
            ctx);
        vm.runInContext({json.dumps(listener_stmt)}, ctx,
                        {{filename: 'messages.js#pending_steer_leftover'}});
        const armStubs = () => vm.runInContext(
            'this.__calls = {{queue: [], badge: [], toast: [], anchor: []}}; ' +
            'queueSessionMessage = (sid, payload) => {{ __calls.queue.push([sid, payload]); return 1; }}; ' +
            'updateQueueBadge = (sid) => {{ __calls.badge.push(sid); }}; ' +
            'showToast = (msg, ms) => {{ __calls.toast.push([msg, ms]); }}; ' +
            "_applyToAnchor = (kind, d, e) => {{ __calls.anchor.push(kind); }}; " +
            "_chatPayloadModelState = () => ({{model: 'picker-model', model_provider: 'picker-provider'}}); " +
            "t = (k) => 'translated:' + k;",
            ctx);
        const fire = (expr) => {{
            armStubs();
            vm.runInContext(expr, ctx);
            return vm.runInContext('__calls', ctx);
        }};
        const failures = [];
        let c;
        // Scenario 1: owning session is the active view.
        c = fire({json.dumps(scenario_scripts['viewed'])});
        if (c.queue.length !== 1 || c.queue[0][0] !== 'sess-a') failures.push('viewed: not queued for owner ' + JSON.stringify(c.queue));
        if (!c.queue[0] || c.queue[0][1].text !== 'use the safer path') failures.push('viewed: wrong payload ' + JSON.stringify(c.queue[0] && c.queue[0][1]));
        if (c.queue[0][1].model !== 'picker-model' || c.queue[0][1].model_provider !== 'picker-provider') failures.push('viewed: picker model state missing ' + JSON.stringify(c.queue[0][1]));
        if (c.anchor.length !== 1) failures.push('viewed: anchor not applied ' + JSON.stringify(c.anchor));
        if (c.toast.length !== 1) failures.push('viewed: toast missing ' + JSON.stringify(c.toast));
        if (c.badge.length !== 1 || c.badge[0] !== 'sess-a') failures.push('viewed: badge ' + JSON.stringify(c.badge));
        // Scenario 2: user switched to sess-b before completion — queue for the OWNER anyway.
        vm.runInContext("S = {{session: {{session_id: 'sess-b'}}, activeProfile: 'default'}}", ctx);
        c = fire({json.dumps(scenario_scripts['switched'])});
        if (c.queue.length !== 1 || c.queue[0][0] !== 'sess-a') failures.push('switched: not queued for owner ' + JSON.stringify(c.queue));
        if (c.queue[0][1].model !== '' || c.queue[0][1].model_provider !== null) failures.push('switched: picker model leaked into owner queue entry ' + JSON.stringify(c.queue[0][1]));
        if (c.anchor.length !== 0) failures.push('switched: anchor applied to foreign view ' + JSON.stringify(c.anchor));
        if (c.toast.length !== 0) failures.push('switched: toast shown for foreign view ' + JSON.stringify(c.toast));
        if (c.badge.length !== 1 || c.badge[0] !== 'sess-a') failures.push('switched: badge ' + JSON.stringify(c.badge));
        // Scenario 3: empty text is still dropped.
        vm.runInContext("S = {{session: {{session_id: 'sess-a'}}, activeProfile: 'default'}}", ctx);
        c = fire({json.dumps(scenario_scripts['empty'])});
        if (c.queue.length !== 0) failures.push('empty: queued empty text ' + JSON.stringify(c.queue));
        // Scenario 4: missing session_id falls back to the wire-time session.
        c = fire({json.dumps(scenario_scripts['fallback_sid'])});
        if (c.queue.length !== 1 || c.queue[0][0] !== 'sess-a') failures.push('fallback_sid: ' + JSON.stringify(c.queue));
        if (failures.length) {{ console.error('FAIL:\\n' + failures.join('\\n')); process.exit(1); }}
        console.log('leftover listener owner-scoping OK');
        """
    )
    result = subprocess.run([_find_node(), "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"leftover listener VM test failed (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
