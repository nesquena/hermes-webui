#!/usr/bin/env python3
"""Cancel-time gateway history reconciliation (#7977 rework 2).

Drives REAL routes and REAL streaming functions with a fake /v1/runs gateway
that captures request bodies and applies the installed gateway's rules
(gateway/platforms/api_server_runs.py ~712-714 semantics):
  - a captured /v1/runs body WITH conversation_history uses exactly those rows
    (the gateway marks caller-supplied rows already-saved, _db_flush_collect);
  - a body WITHOUT conversation_history makes the gateway load its own stored
    transcript (which includes the user prompt + persisted incomplete snapshot
    it adopted when a run was cancelled).

Every scenario asserts on the CAPTURED REQUEST BODY's conversation_history,
not on WebUI state.

Scenarios:
  1. fork turn 1 -> history == parent prefix (master parity; fork question
     travels as `input`).
  2. [ROUND-2 KILLER] fork turn 1 cancelled with a streamed partial -> fork
     turn 2 -> history == parent prefix + [F-q1, partial]. RED on master:
     the interrupted turn is missing from the captured history.
  3. normal user Stop mid-run on a plain session -> next turn's history
     includes the reconciled user row + partial. RED on master (timing:
     nothing reconciles the model-facing context on Stop).
  4. truncate after an interrupted run -> history is the SHORTENED context
     (user edit wins).
  5. prefill configured + interrupted history -> captured history contains
     the context rows AND the prefill rows (nothing dropped).
  6. double-settle (same turn settled twice) -> assistant partial appended
     once. RED on master: the settle path never appends the partial at all.

Red/green evidence: on the un-fixed master state scenarios 2, 3, 5 and 6
fail (5 composes with the interrupted history) while 1 and 4 pass; with the
reconciliation in place all six pass.

Runs under pytest (conftest provisions isolated HERMES_HOME /
HERMES_WEBUI_STATE_DIR) and standalone:

  HERMES_HOME=/tmp/hermes-webui-agent-home \
  HERMES_WEBUI_STATE_DIR=/tmp/hermes-webui-agent-state \
  python3 tests/test_gateway_cancel_reconciliation.py
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch
from urllib.parse import urlparse

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Isolated trial state before importing api modules (AGENTS.md safety rule).
os.environ.setdefault("HERMES_HOME", "/tmp/hermes-webui-agent-home")
os.environ.setdefault("HERMES_WEBUI_STATE_DIR", "/tmp/hermes-webui-agent-state")
for _trial_dir in (
    os.environ["HERMES_WEBUI_STATE_DIR"],
    os.path.join(os.environ["HERMES_WEBUI_STATE_DIR"], "sessions"),
    os.environ["HERMES_HOME"],
):
    pathlib.Path(_trial_dir).mkdir(parents=True, exist_ok=True)

import api.config as cfg  # noqa: E402
import api.routes as routes  # noqa: E402
from api.models import Session, get_session  # noqa: E402

GATEWAY_STORE: dict[str, list] = {}  # session_id -> gateway's own stored transcript
CAPTURED_BODIES: list[dict] = []  # every /v1/runs admission body, in order


class _JsonResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, _limit=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class _SseResponse:
    """SSE stream for a run: completed by default, or cancelled when armed."""

    cancelled_runs: set[str] = set()

    def __init__(self, run_id: str):
        self._run_id = run_id

    def __iter__(self):
        if self._run_id in _SseResponse.cancelled_runs:
            # Gateway-side cancel: persist the incomplete snapshot into the
            # gateway's own store (adopt/persist path) before reporting. The
            # worker resets STREAM_PARTIAL_TEXT at startup, so the partial it
            # reconciles is whatever THIS stream relayed before the terminal
            # event — stream a delta first, exactly like a real interrupted run.
            body = _last_admitted_body()
            sid = str(body.get("session_id") or "")
            GATEWAY_STORE.setdefault(sid, []).extend(_gateway_adopted_rows(body))
            _SseResponse.cancelled_runs.discard(self._run_id)
            yield b'data: {"event":"message.delta","delta":"gateway cut"}\n\n'
            yield b'data: {"event":"run.cancelled"}\n\n'
        else:
            yield b'data: {"event":"run.completed","output":"turn answer","usage":{"input_tokens":1,"output_tokens":1}}\n\n'
        yield b"data: [DONE]\n\n"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _last_admitted_body() -> dict:
    return CAPTURED_BODIES[-1] if CAPTURED_BODIES else {}


def _gateway_adopted_rows(body: dict) -> list:
    """Rows the gateway persists for an interrupted run: prompt + partial."""
    prompt = body.get("input")
    if isinstance(prompt, list):
        prompt = prompt[-1].get("content") if prompt else ""
    return [
        {"role": "user", "content": str(prompt or "")},
        {"role": "assistant", "content": "gateway-persisted partial"},
    ]


def _anthropic_native_converter_seam(body: dict) -> dict:
    """Webui-side seam for the installed Agent's Anthropic converter.

    anthropic_message_convert.py (~742) folds a /v1/runs body into provider
    messages: the run's `instructions` and every `system` row of
    conversation_history collapse into the provider system prompt keeping
    only the LAST system row; user/assistant rows keep their order in
    `messages` and the run's `input` is appended as the final user message
    (the prompt travels in `input`, never duplicated into the history —
    docs/advanced-chat-setup.md, api_server_runs.py ~712-714 admission
    semantics mirrored by the fake gateway above).
    """
    system_rows = []
    instructions = str(body.get("instructions") or "").strip()
    if instructions:
        system_rows.append(instructions)
    messages = []
    for row in body.get("conversation_history") or []:
        if row.get("role") == "system":
            system_rows.append(str(row.get("content") or ""))
        else:
            messages.append({"role": row.get("role"), "content": row.get("content")})
    prompt = body.get("input")
    if isinstance(prompt, list):
        prompt = prompt[-1].get("content") if prompt else ""
    if prompt:
        messages.append({"role": "user", "content": prompt})
    return {"system": system_rows[-1] if system_rows else "", "messages": messages}


def fake_urlopen(req, *, timeout=None):
    url = req.full_url
    if url.endswith("/v1/runs"):
        body = json.loads(req.data.decode("utf-8"))
        CAPTURED_BODIES.append(body)
        return _JsonResponse({"run_id": f"run-{len(CAPTURED_BODIES)}"})
    if "/events" in url:
        # .../v1/runs/<run_id>/events — the run id sits BEFORE /events, not at
        # the tail (the old rsplit parsed the literal "events" and the
        # cancelled_runs arming could never match).
        run_id = url.split("/v1/runs/", 1)[1].split("/events", 1)[0]
        return _SseResponse(run_id)
    raise AssertionError(f"unexpected gateway request: {url}")


class _FakeQueue:
    def __init__(self):
        self.events = []

    def put_nowait(self, item):
        self.events.append(item)

    def note_last_event_id(self, event_id):
        return None


class _FakeHandler:
    """Minimal handler for routes.handle_post (pattern from tests/test_465)."""

    def __init__(self, body: dict):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(json.dumps(body).encode("utf-8"))
        self.headers = {"Content-Length": str(len(self.rfile.getvalue()))}
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def drive_route(path: str, body: dict):
    """Drive the real routes.handle_post; return (status, payload)."""
    handler = _FakeHandler(body)
    cap = {}
    with patch.object(routes, "_check_csrf", lambda _h: True), \
            patch.object(routes, "read_body", lambda _h: body), \
            patch.object(routes, "j", lambda h, obj, *a, **k: cap.update(ok=obj, status=k.get("status", 200))), \
            patch.object(routes, "bad", lambda h, msg, code=400: cap.update(bad=(msg, code))):
        routes.handle_post(handler, urlparse(path))
    return cap.get("status"), cap.get("ok"), cap.get("bad")


def make_session(title, context, *, messages=None, **kwargs):
    s = Session(title=title, workspace="/tmp/hermes-webui-agent-home", messages=messages if messages is not None else list(context), context_messages=list(context), **kwargs)
    s.save()
    return s


def set_mid_turn_stream_state(session, stream_id, *, partial, prompt):
    """Register a live gateway stream the way the gateway worker + route layer do."""
    q = _FakeQueue()
    with cfg.STREAMS_LOCK:
        cfg.STREAMS[stream_id] = q
        cfg.CANCEL_FLAGS[stream_id] = threading.Event()
        cfg.STREAM_PARTIAL_TEXT[stream_id] = partial
        cfg.STREAM_REASONING_TEXT[stream_id] = ""
        cfg.STREAM_LIVE_TOOL_CALLS[stream_id] = []
    cfg.register_active_run(stream_id, session_id=session.session_id, backend="gateway", phase="gateway-request")
    cfg.register_stream_owner(stream_id, session.session_id)
    session.active_stream_id = stream_id
    session.pending_user_message = prompt
    started = time.time()
    session.pending_started_at = started
    session.pending_attachments = []
    session.pending_user_source = "webui"
    session.gateway_run = {"run_id": f"run-{stream_id}", "stream_id": stream_id}
    session.save()
    return started


def teardown_stream_state(stream_id):
    with cfg.STREAMS_LOCK:
        cfg.STREAMS.pop(stream_id, None)
        cfg.CANCEL_FLAGS.pop(stream_id, None)
        cfg.STREAM_PARTIAL_TEXT.pop(stream_id, None)
        cfg.STREAM_REASONING_TEXT.pop(stream_id, None)
        cfg.STREAM_LIVE_TOOL_CALLS.pop(stream_id, None)
        cfg.STREAM_LAST_EVENT_ID.pop(stream_id, None)
    cfg.unregister_active_run(stream_id)


def admit_run(session_id, *, msg_text, prefill_messages=None, context_messages=None, stream_id=None):
    """Drive the real _run_gateway_runs_api_streaming; return the captured body."""
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_runs_api_streaming

    del CAPTURED_BODIES[:]
    stream_id = stream_id or f"stream-admit-{session_id}"
    with cfg.STREAMS_LOCK:
        cfg.STREAM_PARTIAL_TEXT[stream_id] = ""
        cfg.STREAM_REASONING_TEXT[stream_id] = ""
    session = get_session(session_id) if context_messages is None else None
    try:
        with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            final_text, usage = _run_gateway_runs_api_streaming(
                session_id=session_id,
                msg_text=msg_text,
                model="test-model",
                workspace="/tmp",
                stream_id=stream_id,
                base_url="http://gw:8642",
                api_key="secret",
                prefill_messages=list(prefill_messages or []),
                body_extras={},
                put_gateway_event=lambda *_a, **_k: None,
                cancel_event=threading.Event(),
                session=session,
            )
        assert final_text == "turn answer", final_text
        return CAPTURED_BODIES[0]
    finally:
        with cfg.STREAMS_LOCK:
            cfg.STREAM_PARTIAL_TEXT.pop(stream_id, None)
            cfg.STREAM_REASONING_TEXT.pop(stream_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)


def history_rows(body):
    return [
        (row.get("role"), row.get("content"))
        for row in (body.get("conversation_history") or [])
    ]


PARENT_CONTEXT = [
    {"role": "user", "content": "P-u1"},
    {"role": "assistant", "content": "P-a1"},
]


class ReconciliationHarness(unittest.TestCase):
    def setUp(self):
        del CAPTURED_BODIES[:]
        GATEWAY_STORE.clear()
        _SseResponse.cancelled_runs.clear()
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.clear()

    def tearDown(self):
        with cfg.STREAMS_LOCK:
            for key in ("STREAMS", "CANCEL_FLAGS", "STREAM_PARTIAL_TEXT", "STREAM_REASONING_TEXT", "STREAM_LIVE_TOOL_CALLS", "STREAM_LAST_EVENT_ID"):
                getattr(cfg, key).clear()
        cfg.ACTIVE_RUNS.clear()

    # ── Scenario 1: fork turn 1 (master parity) ─────────────────────────────

    def test_1_fork_first_turn_sends_parent_prefix(self):
        parent = make_session("parent", PARENT_CONTEXT)
        status, ok, _bad = drive_route("/api/session/branch", {"session_id": parent.session_id})
        self.assertEqual(status, 200, ok)
        fork_id = ok["session_id"]

        body = admit_run(fork_id, msg_text="F-q1")

        self.assertEqual(
            history_rows(body),
            [("user", "P-u1"), ("assistant", "P-a1")],
            f"fork turn 1 must send the copied parent prefix, got {history_rows(body)}",
        )
        self.assertEqual(body.get("input"), "F-q1")

    # ── Scenario 2: [ROUND-2 KILLER] fork turn 1 cancelled -> turn 2 ─────────

    def test_2_fork_cancelled_turn_flows_into_turn2_history(self):
        from api.streaming import cancel_stream

        parent = make_session("parent", PARENT_CONTEXT)
        _status, ok, _bad = drive_route("/api/session/branch", {"session_id": parent.session_id})
        fork_id = ok["session_id"]

        stream_id = "stream-fork-cancel"
        fork = get_session(fork_id)
        set_mid_turn_stream_state(fork, stream_id, partial="F-partial…", prompt="F-q1")
        # Gateway adopted/persisted the interrupted turn in its own store.
        GATEWAY_STORE[fork_id] = [
            {"role": "user", "content": "F-q1"},
            {"role": "assistant", "content": "F-partial…"},
        ]

        self.assertTrue(cancel_stream(stream_id))

        body = admit_run(fork_id, msg_text="F-q2")

        self.assertEqual(
            history_rows(body),
            [
                ("user", "P-u1"),
                ("assistant", "P-a1"),
                ("user", "F-q1"),
                ("assistant", "F-partial…"),
            ],
            "ROUND-2 KILLER CASE: fork turn 2 history must include the "
            f"reconciled interrupted turn, got {history_rows(body)}",
        )
        # The reconciled snapshot must not lose the parent prefix.
        self.assertEqual(body.get("input"), "F-q2")
        teardown_stream_state(stream_id)

    # ── Scenario 3: normal user Stop on a plain session ──────────────────────

    def test_3_user_stop_reconciles_rows_into_next_history(self):
        from api.streaming import cancel_stream

        session = make_session("plain", [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
        ])
        stream_id = "stream-stop"
        set_mid_turn_stream_state(session, stream_id, partial="partial answer text", prompt="q1")

        self.assertTrue(cancel_stream(stream_id))

        body = admit_run(session.session_id, msg_text="q2")

        self.assertEqual(
            history_rows(body),
            [
                ("user", "u0"),
                ("assistant", "a0"),
                ("user", "q1"),
                ("assistant", "partial answer text"),
            ],
            "user Stop must reconcile the interrupted turn into the next "
            f"captured history, got {history_rows(body)}",
        )
        teardown_stream_state(stream_id)

    # ── Scenario 4: truncate after an interrupted run (user edit wins) ───────

    def test_4_truncate_after_interrupt_sends_shortened_history(self):
        from api.streaming import cancel_stream

        session = make_session("trunc", [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
        ])
        stream_id = "stream-trunc"
        set_mid_turn_stream_state(session, stream_id, partial="to be truncated", prompt="q1")
        self.assertTrue(cancel_stream(stream_id))
        teardown_stream_state(stream_id)

        status, ok, bad = drive_route(
            "/api/session/truncate",
            {"session_id": session.session_id, "keep_count": 2},
        )
        self.assertIsNone(bad, bad)
        self.assertEqual(status, 200, ok)

        body = admit_run(session.session_id, msg_text="fresh question")

        self.assertEqual(
            history_rows(body),
            [("user", "u0"), ("assistant", "a0")],
            "user edit (truncate) must win: reconciled rows stay deleted, "
            f"got {history_rows(body)}",
        )

    # ── Scenario 5: prefill composes with reconciled history ─────────────────

    def test_5_prefill_composes_with_reconciled_history(self):
        from api.streaming import cancel_stream

        session = make_session("prefill", [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
        ])
        stream_id = "stream-prefill"
        set_mid_turn_stream_state(session, stream_id, partial="half done", prompt="q1")
        self.assertTrue(cancel_stream(stream_id))
        teardown_stream_state(stream_id)

        prefill = [
            {"role": "system", "content": "prefill system"},
            {"role": "user", "content": "prefill question"},
            {"role": "assistant", "content": "prefill reply"},
        ]
        body = admit_run(session.session_id, msg_text="q2", prefill_messages=prefill)

        self.assertEqual(
            history_rows(body),
            [
                ("user", "u0"),
                ("assistant", "a0"),
                ("user", "q1"),
                ("assistant", "half done"),
                ("user", "prefill question"),
                ("assistant", "prefill reply"),
            ],
            "prefill must COMPOSE with the reconciled context rows, got "
            f"{history_rows(body)}",
        )
        self.assertEqual(body.get("instructions"), "prefill system")

    # ── Scenario 6: double-settle appends the partial once ───────────────────

    def test_6_double_settle_appends_partial_once(self):
        from api.gateway_chat import _settle_gateway_cancelled_turn

        session = make_session("double", [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
        ])
        stream_id = "stream-double"
        started = set_mid_turn_stream_state(session, stream_id, partial="settled partial", prompt="q1")

        _settle_gateway_cancelled_turn(session.session_id, stream_id)

        # Simulate the same turn being settled again (worker replay / refetch):
        # restore the exact mid-turn identity the settle path guards on.
        reloaded = get_session(session.session_id)
        reloaded.active_stream_id = stream_id
        reloaded.pending_user_message = "q1"
        reloaded.pending_started_at = started
        reloaded.pending_attachments = []
        reloaded.pending_user_source = "webui"
        reloaded.save()
        _settle_gateway_cancelled_turn(session.session_id, stream_id)

        ctx = get_session(session.session_id).context_messages
        partial_rows = [
            row for row in ctx
            if row.get("role") == "assistant" and row.get("content") == "settled partial"
        ]
        self.assertEqual(
            len(partial_rows), 1,
            f"double settle must append the partial exactly once, got {len(partial_rows)}: {ctx}",
        )
        user_rows = [
            row for row in ctx
            if row.get("role") == "user" and row.get("content") == "q1"
        ]
        self.assertEqual(
            len(user_rows), 1,
            f"double settle must not duplicate the user row, got {len(user_rows)}: {ctx}",
        )
        teardown_stream_state(stream_id)

    # ── Round-3 re-gate (2026-10-05 22:07 UTC): CORE items 1-3 ───────────────

    def test_7_empty_context_seeded_from_transcript_before_interrupt(self):
        """Item 1: Stop on an imported-CLI-shaped session keeps the whole history.

        context_messages is EMPTY while session.messages holds the prior
        conversation. The reconcile must seed the canonical transcript
        projection BEFORE appending the interrupted turn; otherwise the next
        turn's outbound history is a two-row world that replaces the gateway's
        stored transcript and the earlier conversation drops out of model
        context.
        """
        from api.streaming import cancel_stream

        session = make_session(
            "imported", [],
            messages=[
                {"role": "user", "content": "u0"},
                {"role": "assistant", "content": "a0"},
            ],
        )
        # Imported CLI shape: empty model-facing context, full transcript.
        session.context_messages = []
        session.save()

        stream_id = "stream-import"
        set_mid_turn_stream_state(session, stream_id, partial="imported partial", prompt="q1")
        self.assertTrue(cancel_stream(stream_id))
        teardown_stream_state(stream_id)

        body = admit_run(session.session_id, msg_text="q2")

        self.assertEqual(
            history_rows(body),
            [
                ("user", "u0"),
                ("assistant", "a0"),
                ("user", "q1"),
                ("assistant", "imported partial"),
            ],
            "empty context must be seeded from the transcript projection before "
            f"the interrupted turn is appended, got {history_rows(body)}",
        )
        self.assertEqual(body.get("input"), "q2")

    def test_8_settle_attaches_partial_to_its_own_turn_not_older_prompt(self):
        """Item 2: the settle must reconcile while the pending identity is live.

        A queued prompt B runs (eager checkpoint in the transcript) while an
        older user row (turn A) is still unanswered in the context. Persisting
        the cancellation BEFORE the reconcile cleared the pending identity:
        the pending-turn materializer's token dedup then returned early
        without mirroring B into the context, and the partial-append guard —
        seeing only "any trailing user row" — attached B's partial under A.
        """
        from api.gateway_chat import _settle_gateway_cancelled_turn
        from api.routes import _checkpoint_user_message_for_eager_session_save

        session = make_session("queued", [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "A-old"},
        ])
        stream_id = "stream-queued"
        started = set_mid_turn_stream_state(session, stream_id, partial="B partial", prompt="qB")
        # Eager save mode: the turn's user row is checkpointed into the
        # transcript (with the active-turn token) at chat-start.
        _checkpoint_user_message_for_eager_session_save(
            session, "qB", [], started,
        )
        session.save()

        _settle_gateway_cancelled_turn(session.session_id, stream_id)

        ctx = get_session(session.session_id).context_messages
        self.assertEqual(
            history_rows({"conversation_history": [
                {"role": row.get("role"), "content": row.get("content")}
                for row in ctx
            ]}),
            [
                ("user", "u0"),
                ("assistant", "a0"),
                ("user", "A-old"),
                ("user", "qB"),
                ("assistant", "B partial"),
            ],
            "the partial must attach to B's own row; older rows untouched, "
            f"got {ctx}",
        )
        teardown_stream_state(stream_id)

    def _prepare_regenerate_start(self, session, msg_text):
        """Run the production regenerate-start steps and persist the result.

        Mirrors _start_regeneration_stream_locked: plan, apply (truncate at
        the retained row), then _prepare_chat_start_session_for_stream, which
        restamps the retained context row with the NEW stream's active-turn
        token, then saves.
        """
        from api.routes import (
            _RETAINED_CONTEXT_USER_UNSET,
            _prepare_chat_start_session_for_stream,
        )
        from api.session_ops import apply_regeneration_plan, plan_regeneration

        fresh = get_session(session.session_id)
        plan = plan_regeneration(fresh)
        self.assertIsNotNone(plan)
        applied, retained_context_user = apply_regeneration_plan(
            fresh, plan, return_context_user=True,
        )
        self.assertTrue(applied)
        retained_user = fresh.messages[-1]
        stream_id = f"stream-regen-{session.session_id[-6:]}"
        _prepare_chat_start_session_for_stream(
            fresh,
            msg=plan.turn.message_text,
            attachments=[],
            workspace="/tmp",
            model="test-model",
            model_provider="",
            stream_id=stream_id,
            source="webui",
            retained_user=retained_user,
            retained_context_user=(
                retained_context_user
                if retained_context_user is not None
                else _RETAINED_CONTEXT_USER_UNSET
            ),
            defer_save=True,
        )
        # _start_regeneration_stream_locked saves right after prepare; the
        # worker's get_session must see the restamped row.
        fresh.save()
        return stream_id

    def test_9_regenerate_after_stop_sends_prompt_exactly_once(self):
        """Item 3a: Stop then regenerate - the gateway sees the prompt once.

        Pinned to the post-Stop regenerate-admission state the maintainer's
        repro produced: the retained interrupted-turn user row (restamped at
        regenerate start with the NEW stream's token) followed by the
        reconciled partial. Sending that row in conversation_history as well
        as `input` made the gateway see the prompt twice.
        """
        from api.process_event_utils import build_active_turn_token

        session = make_session("regen", [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
        ])
        stop_stream = "stream-stop-regen"
        started = set_mid_turn_stream_state(
            session, stop_stream, partial="cut off mid", prompt="q1",
        )
        teardown_stream_state(stop_stream)

        # Regenerate start: the plan truncates at the retained row and
        # _prepare_chat_start_session_for_stream restamps it with the NEW
        # stream's token; the reconciled partial trails it.
        regen_stream = "stream-regen-once"
        token = build_active_turn_token(regen_stream, started)
        session.context_messages = [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {
                "role": "user", "content": "q1", "_recovered": True,
                "_active_turn_token": token, "timestamp": int(started),
            },
            {
                "role": "assistant", "content": "cut off mid", "_partial": True,
                "_gateway_cancelled_stream": stop_stream,
            },
        ]
        session.active_stream_id = regen_stream
        session.pending_user_message = "q1"
        session.pending_started_at = started
        session.pending_attachments = []
        session.pending_user_source = "webui"
        session.save()

        body = admit_run(session.session_id, msg_text="q1", stream_id=regen_stream)

        seen = history_rows(body).count(("user", "q1"))
        if body.get("input") == "q1":
            seen += 1
        self.assertEqual(
            seen, 1,
            f"regenerate must deliver the prompt exactly once, got {seen}: "
            f"input={body.get('input')!r} history={history_rows(body)}",
        )
        self.assertEqual(body.get("input"), "q1")
        teardown_stream_state(regen_stream)

    def _gateway_worker_env(self):
        return patch.dict(os.environ, {
            "HERMES_WEBUI_CHAT_BACKEND": "gateway",
            "HERMES_WEBUI_GATEWAY_USE_RUNS_API": "1",
        })

    def _drive_full_worker(self, session_id, msg_text, stream_id):
        """Drive the real _run_gateway_chat_streaming end to end."""
        import api.gateway_chat as gateway_chat
        from api.config import create_stream_channel

        with cfg.STREAMS_LOCK:
            cfg.STREAMS[stream_id] = create_stream_channel()
        with patch.object(gateway_chat, "gateway_supports_approval", lambda *a, **k: True), \
                patch.object(gateway_chat.urllib.request, "urlopen", side_effect=fake_urlopen):
            gateway_chat._run_gateway_chat_streaming(
                session_id, msg_text, "test-model", "/tmp", stream_id, [],
            )

    def test_10_regenerate_writeback_saves_single_user_row(self):
        """Item 3b: post-regeneration success writeback keeps ONE prompt row."""
        from api.streaming import cancel_stream

        session = make_session("regen-writeback", [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
        ])
        stop_stream = "stream-stop-wb"
        set_mid_turn_stream_state(session, stop_stream, partial="cut off mid", prompt="q1")
        self.assertTrue(cancel_stream(stop_stream))
        teardown_stream_state(stop_stream)

        stream_id = self._prepare_regenerate_start(session, "q1")
        with self._gateway_worker_env():
            self._drive_full_worker(session.session_id, "q1", stream_id)

        saved = get_session(session.session_id)
        ctx = saved.context_messages
        user_rows = [
            row for row in ctx
            if isinstance(row, dict) and row.get("role") == "user" and row.get("content") == "q1"
        ]
        self.assertEqual(
            len(user_rows), 1,
            f"writeback must save exactly one q1 user row, got {len(user_rows)}: {ctx}",
        )
        assistant_rows = [
            row for row in ctx
            if isinstance(row, dict) and row.get("role") == "assistant"
            and row.get("content") == "turn answer"
        ]
        self.assertEqual(
            len(assistant_rows), 1,
            f"writeback must save the regenerated answer once, got {ctx}",
        )
        self.assertIsNone(saved.gateway_run)
        self.assertIsNone(saved.active_stream_id)
        teardown_stream_state(stream_id)

    def test_11_run_cancelled_event_settles_reconciles_and_matches_store(self):
        """Item 6 headline: the worker's run.cancelled path reconciles context.

        The gateway cancels the run mid-stream; the worker must settle the
        cancelled turn (prompt + streamed partial into context), clear the run
        record, and the next turn's history must carry the interrupted turn
        matching the gateway's own persisted store.
        """
        session = make_session("worker-cancel", [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
        ])
        stream_id = "stream-worker-cancel"
        set_mid_turn_stream_state(session, stream_id, partial="gateway cut", prompt="q1")

        # Arm a gateway-side cancel for the admitted run id (run-1).
        _SseResponse.cancelled_runs.add("run-1")
        with self._gateway_worker_env():
            self._drive_full_worker(session.session_id, "q1", stream_id)

        saved = get_session(session.session_id)
        self.assertIsNone(saved.gateway_run, "cancel settle must clear the run record")
        # The gateway adopted/persisted the interrupted turn in its own store.
        self.assertEqual(
            GATEWAY_STORE.get(session.session_id),
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "gateway-persisted partial"},
            ],
            f"gateway store must hold the adopted interrupted turn, got {GATEWAY_STORE}",
        )
        # WebUI's mirror: prompt + streamed partial under it.
        ctx = saved.context_messages
        self.assertEqual(
            history_rows({"conversation_history": [
                {"role": row.get("role"), "content": row.get("content")}
                for row in ctx
            ]}),
            [
                ("user", "u0"),
                ("assistant", "a0"),
                ("user", "q1"),
                ("assistant", "gateway cut"),
            ],
            f"run.cancelled must reconcile prompt + partial into context, got {ctx}",
        )
        teardown_stream_state(stream_id)

        # Next turn: the outbound history carries the interrupted turn.
        body = admit_run(session.session_id, msg_text="q2")
        self.assertEqual(
            history_rows(body),
            [
                ("user", "u0"),
                ("assistant", "a0"),
                ("user", "q1"),
                ("assistant", "gateway cut"),
            ],
            f"next turn must keep the story after a gateway-side cancel, got {history_rows(body)}",
        )

    # ── Round-4 re-gate (2026-10-06 14:38 UTC): CORE items 1-2 + SILENT item 3

    def test_12_eager_stop_history_pairs_partial_with_its_own_question(self):
        """Item 1: the recovered-question filter must not orphan the partial.

        Eager-save Stop with an older unanswered A-old in context: the stored
        context is correct ([A-old, qB, B partial]) but the history builder's
        recovered-row rule drops qB (a _recovered row preceded by a user
        row), so the Gateway receives [A-old, B partial] — the partial paired
        with the wrong question. The reconcile must clear the owning row's
        recovered flag right before appending the partial under it, so the
        partial rides with its own question.
        """
        from api.gateway_chat import _settle_gateway_cancelled_turn
        from api.routes import _checkpoint_user_message_for_eager_session_save

        session = make_session("queued-history", [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "A-old"},
        ])
        stream_id = "stream-queued-hist"
        started = set_mid_turn_stream_state(session, stream_id, partial="B partial", prompt="qB")
        _checkpoint_user_message_for_eager_session_save(
            session, "qB", [], started,
        )
        session.save()

        _settle_gateway_cancelled_turn(session.session_id, stream_id)
        teardown_stream_state(stream_id)

        body = admit_run(session.session_id, msg_text="q2")

        self.assertEqual(
            history_rows(body),
            [
                ("user", "u0"),
                ("assistant", "a0"),
                ("user", "A-old"),
                ("user", "qB"),
                ("assistant", "B partial"),
            ],
            "the partial must ride with its OWN question: qB must not be "
            f"dropped from the outbound history, got {history_rows(body)}",
        )
        self.assertEqual(body.get("input"), "q2")

    def test_13_truncated_regenerate_sends_sentinel_not_deleted_rows(self):
        """Item 2a: Stop -> truncate -> regenerate must not resurrect turns.

        Excluding the retained active-turn user row empties the outbound
        history; dropping the conversation_history key (or sending []) makes
        the Gateway admission handler fall back to its stored transcript and
        reload the deleted rows. The builder must send a non-empty sentinel
        instead, with the prompt only in `input`.
        """
        from api.streaming import cancel_stream

        session = make_session("trunc-regen", [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
        ])
        stop_stream = "stream-stop-trunc"
        set_mid_turn_stream_state(session, stop_stream, partial="cut off mid", prompt="q1")
        self.assertTrue(cancel_stream(stop_stream))
        teardown_stream_state(stop_stream)

        status, ok, bad = drive_route(
            "/api/session/truncate",
            {"session_id": session.session_id, "keep_count": 2},
        )
        self.assertIsNone(bad, bad)
        self.assertEqual(status, 200, ok)

        regen_stream = self._prepare_regenerate_start(session, "u0")
        fresh = get_session(session.session_id)
        self.assertEqual(
            [(row.get("role"), row.get("content")) for row in fresh.context_messages],
            [("user", "u0")],
            f"pinned admission state: truncate + regenerate leaves the "
            f"retained row alone, got {fresh.context_messages}",
        )

        body = admit_run(session.session_id, msg_text="u0", stream_id=regen_stream)

        self.assertEqual(
            body.get("conversation_history"),
            [{"role": "user", "content": "No earlier conversation turns."}],
            "an empty-by-exclusion history must send the sentinel as a USER "
            f"row, got {body.get('conversation_history')}",
        )
        self.assertEqual(body.get("input"), "u0")
        # The deleted interrupted turn must not ride anywhere in the body.
        flat = json.dumps(body)
        self.assertNotIn("q1", flat, f"deleted prompt resurrected into the body: {flat}")
        self.assertNotIn("cut off mid", flat, f"deleted partial resurrected into the body: {flat}")
        teardown_stream_state(regen_stream)

    def _drive_full_worker_with_gateway_fallback(self, session_id, msg_text, stream_id):
        """Drive the real worker with the gateway's admission fallback applied.

        gateway/platforms/api_server_runs.py ~712-714: a body WITHOUT a
        non-empty conversation_history makes the gateway load its OWN stored
        transcript as the run's context. The fake mirrors that: when the
        fallback fires, the completed run's output is stamped with the rows
        it reloaded, so resurrection is observable in the answer WebUI saves.
        """
        import api.gateway_chat as gateway_chat
        from api.config import create_stream_channel

        captured = []

        def capturing_urlopen(req, *, timeout=None):
            url = req.full_url
            if url.endswith("/v1/runs"):
                body = json.loads(req.data.decode("utf-8"))
                captured.append(body)
                return _JsonResponse({"run_id": f"run-{len(CAPTURED_BODIES) + len(captured)}"})
            return fake_urlopen(req, timeout=timeout)

        class _FallbackSseResponse(_SseResponse):
            def __iter__(self):
                if self._run_id in _SseResponse.cancelled_runs:
                    yield from super().__iter__()
                    return
                body = captured[-1] if captured else {}
                if not body.get("conversation_history"):
                    # Gateway fallback: load its own stored transcript.
                    stored = GATEWAY_STORE.get(
                        str(body.get("session_id") or ""), []
                    )
                    reloaded = "|".join(
                        str(row.get("content") or "") for row in stored
                    )
                    output = f"reloaded[{reloaded}]"
                else:
                    output = "turn answer"
                yield (
                    b'data: {"event":"run.completed","output":'
                    + json.dumps(output).encode("utf-8")
                    + b',"usage":{"input_tokens":1,"output_tokens":1}}\n\n'
                )
                yield b"data: [DONE]\n\n"

        def fallback_urlopen(req, *, timeout=None):
            url = req.full_url
            if "/events" in url:
                run_id = url.split("/v1/runs/", 1)[1].split("/events", 1)[0]
                return _FallbackSseResponse(run_id)
            return capturing_urlopen(req, timeout=timeout)

        with cfg.STREAMS_LOCK:
            cfg.STREAMS[stream_id] = create_stream_channel()
        with patch.object(gateway_chat, "gateway_supports_approval", lambda *a, **k: True), \
                patch.object(gateway_chat.urllib.request, "urlopen", side_effect=fallback_urlopen):
            gateway_chat._run_gateway_chat_streaming(
                session_id, msg_text, "test-model", "/tmp", stream_id, [],
            )
        if captured:
            del CAPTURED_BODIES[:]
            CAPTURED_BODIES.extend(captured)

    def test_14_truncated_regenerate_writeback_saves_one_user_row(self):
        """Item 2b: after the sentinel run the writeback saves ONE user row.

        Without the sentinel the gateway reloads its stored transcript (the
        deleted interrupted turn included); the answer comes back composed
        over resurrected rows. With the sentinel the run is clean and the
        writeback saves exactly one user row + one fresh answer, with no
        deleted rows and no sentinel row in the saved model context.
        """
        from api.streaming import cancel_stream

        session = make_session("trunc-wb", [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
        ])
        stop_stream = "stream-stop-wb2"
        set_mid_turn_stream_state(session, stop_stream, partial="cut off mid", prompt="q1")
        self.assertTrue(cancel_stream(stop_stream))
        teardown_stream_state(stop_stream)

        # The gateway adopted the interrupted turn into its own transcript.
        GATEWAY_STORE[session.session_id] = [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "cut off mid"},
        ]

        status, ok, bad = drive_route(
            "/api/session/truncate",
            {"session_id": session.session_id, "keep_count": 2},
        )
        self.assertIsNone(bad, bad)
        self.assertEqual(status, 200, ok)

        stream_id = self._prepare_regenerate_start(session, "u0")
        with self._gateway_worker_env():
            self._drive_full_worker_with_gateway_fallback(
                session.session_id, "u0", stream_id,
            )

        saved = get_session(session.session_id)
        ctx = saved.context_messages
        user_rows = [
            row for row in ctx
            if isinstance(row, dict) and row.get("role") == "user"
        ]
        self.assertEqual(
            [(row.get("content")) for row in user_rows],
            ["u0"],
            f"writeback must save exactly one user row (the retained "
            f"prompt), got {ctx}",
        )
        assistant_rows = [
            row.get("content") for row in ctx
            if isinstance(row, dict) and row.get("role") == "assistant"
        ]
        self.assertEqual(
            assistant_rows,
            ["turn answer"],
            f"the answer must be composed over the SENTINEL (no gateway "
            f"transcript fallback), got {assistant_rows}",
        )
        flat = json.dumps(ctx)
        self.assertNotIn("q1", flat, f"deleted prompt resurrected into saved context: {flat}")
        self.assertNotIn("cut off mid", flat, f"deleted partial resurrected: {flat}")
        self.assertNotIn(
            "No earlier conversation turns.", flat,
            f"the sentinel is transport-only and must never be saved: {flat}",
        )
        teardown_stream_state(stream_id)

    def test_15_two_interrupts_same_second_stay_distinct_turns(self):
        """Item 3: same-prompt interrupts within one second are two turns.

        The mirror dedup matched content + int-second timestamp, so the
        second interrupt of prompt P inside the same wall second was treated
        as already mirrored: its prompt AND partial never reached the
        context, and the next admission carried only one turn. Recovered
        rows must carry their stream-bound `_active_turn_token` and dedups
        must require token match — text+second is not ownership.
        """
        from api.process_event_utils import build_active_turn_token
        from api.streaming import cancel_stream

        session = make_session("twice", [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
        ])
        fixed_started = 1790000000.5  # both interrupts share one wall second

        stream_1 = "stream-twice-1"
        set_mid_turn_stream_state(session, stream_1, partial="P partial one", prompt="P")
        session.pending_started_at = fixed_started
        session.save()
        self.assertTrue(cancel_stream(stream_1))
        teardown_stream_state(stream_1)

        stream_2 = "stream-twice-2"
        # Re-resolve the session the way the next turn's route does: the
        # first cancel's writeback ran on the object get_session resolved,
        # and stale object handles must never be re-armed (same pattern as
        # the double-settle replay above).
        session = get_session(session.session_id)
        set_mid_turn_stream_state(session, stream_2, partial="P partial two", prompt="P")
        session.pending_started_at = fixed_started
        session.save()
        self.assertTrue(cancel_stream(stream_2))
        teardown_stream_state(stream_2)

        saved = get_session(session.session_id)

        ctx = saved.context_messages
        expected_ctx_shapes = [
            ("user", "u0"),
            ("assistant", "a0"),
            ("user", "P"),
            ("assistant", "P partial one"),
            ("user", "P"),
            ("assistant", "P partial two"),
        ]
        self.assertEqual(
            [(row.get("role"), row.get("content")) for row in ctx],
            expected_ctx_shapes,
            "two interrupts of the same prompt inside one second must stay "
            f"two distinct turns in the model context, got {ctx}",
        )
        p_tokens = [
            row.get("_active_turn_token") for row in ctx
            if row.get("role") == "user" and row.get("content") == "P"
        ]
        self.assertEqual(
            p_tokens,
            [
                build_active_turn_token(stream_1, fixed_started),
                build_active_turn_token(stream_2, fixed_started),
            ],
            f"each interrupt's row must carry its own stream-bound token, got {p_tokens}",
        )
        # Transcript side: the second interrupt must not collapse into the
        # first turn's row either.
        msg_p_rows = [
            row for row in saved.messages
            if isinstance(row, dict) and row.get("role") == "user" and row.get("content") == "P"
        ]
        self.assertEqual(
            len(msg_p_rows), 2,
            f"the transcript must hold BOTH interrupts' user rows, got {saved.messages}",
        )

        body = admit_run(session.session_id, msg_text="q2")
        self.assertEqual(
            history_rows(body),
            expected_ctx_shapes,
            f"next admission must carry both interrupted turns, got {history_rows(body)}",
        )

    def test_16_cleared_session_sends_sentinel_not_stored_transcript(self):
        """#8166 Greptile (2026-10-08): /clear must survive the admission gap.

        /api/session/clear wipes messages + context_messages and stamps the
        0.0 truncate-to-empty watermark — but the Gateway keeps its own
        stored transcript. The next NORMAL (non-regen) turn reaches the
        history builder with an empty context (deferred mode commits the new
        prompt only after the run), so no active-row exclusion fires and the
        conversation_history key was OMITTED — the admission handler then
        fell back to its stored transcript and every pre-clear turn
        resurrected into model context. ANY deliberately-empty webui
        history must carry the sentinel. The fake's store holds
        pre-clear marker turns: if the fallback consults them they show up
        in the composed answer and the saved context and the assertions
        fail.
        """
        session = make_session("cleared", [], messages=[])
        session.truncation_watermark = 0.0
        session.save()

        GATEWAY_STORE[session.session_id] = [
            {"role": "user", "content": "pre-clear-q"},
            {"role": "assistant", "content": "pre-clear-a"},
        ]

        body = admit_run(session.session_id, msg_text="fresh question")

        self.assertEqual(
            body.get("conversation_history"),
            [{"role": "user", "content": "No earlier conversation turns."}],
            "a cleared session's normal turn must send the sentinel as a "
            f"USER row, got {body.get('conversation_history')}",
        )
        self.assertEqual(body.get("input"), "fresh question")
        flat = json.dumps(body)
        self.assertNotIn("pre-clear", flat, f"pre-clear marker resurrected into the body: {flat}")

        # Full worker with the gateway's admission fallback applied: the
        # composed answer proves the stored transcript was NOT consulted.
        # Arm the stream the chat-start route does (active_stream_id + pending
        # fields; deferred mode touches no context rows) so the success
        # writeback is current for this stream.
        from api.routes import _prepare_chat_start_session_for_stream

        stream_id = "stream-cleared-wb"
        fresh = get_session(session.session_id)
        _prepare_chat_start_session_for_stream(
            fresh,
            msg="fresh question",
            attachments=[],
            workspace="/tmp",
            model="test-model",
            model_provider="",
            stream_id=stream_id,
            source="webui",
            defer_save=True,
        )
        fresh.save()
        with self._gateway_worker_env():
            self._drive_full_worker_with_gateway_fallback(
                session.session_id, "fresh question", stream_id,
            )
        self.assertEqual(
            CAPTURED_BODIES[0].get("conversation_history"),
            [{"role": "user", "content": "No earlier conversation turns."}],
            f"the worker's admission must carry the sentinel too, got "
            f"{CAPTURED_BODIES[0].get('conversation_history')}",
        )
        saved = get_session(session.session_id)
        ctx = saved.context_messages
        assistant_rows = [
            row.get("content") for row in ctx
            if isinstance(row, dict) and row.get("role") == "assistant"
        ]
        self.assertEqual(
            assistant_rows,
            ["turn answer"],
            f"the answer must be composed over the SENTINEL, not the "
            f"gateway's pre-clear transcript, got {assistant_rows}",
        )
        flat = json.dumps(ctx)
        self.assertNotIn("pre-clear-q", flat, f"pre-clear prompt resurrected into saved context: {flat}")
        self.assertNotIn("pre-clear-a", flat, f"pre-clear answer resurrected into saved context: {flat}")
        self.assertNotIn(
            "No earlier conversation turns.", flat,
            f"the sentinel is transport-only and must never be saved: {flat}",
        )
        teardown_stream_state(stream_id)

        # Control: a FORKED session carries COPIED context rows, so its
        # history rides the normal path — no sentinel, no fallback.
        parent = make_session("cleared-parent", [
            {"role": "user", "content": "copied-u"},
            {"role": "assistant", "content": "copied-a"},
        ])
        status, ok, _bad = drive_route("/api/session/branch", {"session_id": parent.session_id})
        self.assertEqual(status, 200, ok)
        fork_id = ok["session_id"]
        GATEWAY_STORE[fork_id] = [
            {"role": "user", "content": "store-poison-u"},
            {"role": "assistant", "content": "store-poison-a"},
        ]
        fork_body = admit_run(fork_id, msg_text="fork question")
        self.assertEqual(
            history_rows(fork_body),
            [("user", "copied-u"), ("assistant", "copied-a")],
            "copied fork rows must ride as history, got "
            f"{history_rows(fork_body)}",
        )
        self.assertEqual(fork_body.get("input"), "fork question")
        self.assertNotIn(
            "No earlier conversation turns.", json.dumps(fork_body),
            f"a session with copied rows must not get the sentinel: {json.dumps(fork_body)}",
        )

    def test_16b_cleared_session_sends_sentinel_in_eager_save_mode(self):
        """Re-gate item 1 (2026-10-13): eager mode must not hide the /clear.

        webui.session_save_mode=eager checkpoints the NEW prompt into
        session.messages at chat start
        (routes._checkpoint_user_message_for_eager_session_save) and its
        watermark advance is guarded by `if getattr(s,
        "truncation_watermark", None):` — falsy on 0.0, so a cleared
        session keeps the 0.0 watermark while messages=[prompt]. The old
        AND clause (`not messages and watermark is not None`) skipped that
        shape, the conversation_history key was OMITTED, and the gateway
        store fallback resurrected every pre-clear turn. `watermark == 0.0`
        must fire in BOTH save modes.
        """
        session = make_session("cleared-eager", [], messages=[])
        session.truncation_watermark = 0.0
        session.save()

        GATEWAY_STORE[session.session_id] = [
            {"role": "user", "content": "pre-clear-q"},
            {"role": "assistant", "content": "pre-clear-a"},
        ]

        # The eager chat-start checkpoint: the prompt lands in messages
        # while the 0.0 watermark stays stamped (only a live watermark
        # advances).
        from api.routes import _prepare_chat_start_session_for_stream

        stream_id = "stream-cleared-eager-wb"
        fresh = get_session(session.session_id)
        with patch.object(routes, "get_webui_session_save_mode", lambda: "eager"):
            _prepare_chat_start_session_for_stream(
                fresh,
                msg="fresh question",
                attachments=[],
                workspace="/tmp",
                model="test-model",
                model_provider="",
                stream_id=stream_id,
                source="webui",
                defer_save=True,
            )
        fresh.save()
        eager = get_session(session.session_id)
        self.assertEqual(
            [(row.get("role"), row.get("content")) for row in eager.messages],
            [("user", "fresh question")],
            f"pinned eager shape: the prompt must be checkpointed into "
            f"messages, got {eager.messages}",
        )
        self.assertEqual(
            eager.truncation_watermark, 0.0,
            f"the eager checkpoint must not advance the 0.0 watermark, got "
            f"{eager.truncation_watermark}",
        )
        self.assertEqual(
            eager.context_messages or [], [],
            f"eager mode touches no context rows, got {eager.context_messages}",
        )

        body = admit_run(session.session_id, msg_text="fresh question")

        self.assertEqual(
            body.get("conversation_history"),
            [{"role": "user", "content": "No earlier conversation turns."}],
            "an eager-mode cleared session (prompt checkpointed into "
            "messages, 0.0 watermark) must still send the sentinel as a "
            f"USER row, got {body.get('conversation_history')}",
        )
        self.assertEqual(body.get("input"), "fresh question")
        flat = json.dumps(body)
        self.assertNotIn("pre-clear", flat, f"pre-clear marker resurrected into the body: {flat}")

        # Full worker with the gateway's admission fallback applied: the
        # composed answer proves the stored transcript was NOT consulted
        # under the eager shape too.
        with self._gateway_worker_env():
            self._drive_full_worker_with_gateway_fallback(
                session.session_id, "fresh question", stream_id,
            )
        self.assertEqual(
            CAPTURED_BODIES[0].get("conversation_history"),
            [{"role": "user", "content": "No earlier conversation turns."}],
            f"the worker's admission must carry the sentinel too, got "
            f"{CAPTURED_BODIES[0].get('conversation_history')}",
        )
        saved = get_session(session.session_id)
        ctx = saved.context_messages
        assistant_rows = [
            row.get("content") for row in ctx
            if isinstance(row, dict) and row.get("role") == "assistant"
        ]
        self.assertEqual(
            assistant_rows,
            ["turn answer"],
            f"the answer must be composed over the SENTINEL, not the "
            f"gateway's pre-clear transcript, got {assistant_rows}",
        )
        flat = json.dumps(ctx)
        self.assertNotIn("pre-clear-q", flat, f"pre-clear prompt resurrected into saved context: {flat}")
        self.assertNotIn("pre-clear-a", flat, f"pre-clear answer resurrected into saved context: {flat}")
        self.assertNotIn(
            "No earlier conversation turns.", flat,
            f"the sentinel is transport-only and must never be saved: {flat}",
        )
        teardown_stream_state(stream_id)

    # ── Round-5 re-gate (2026-10-12 09:01 UTC, 29913f2e5): empty-context
    # ── shapes in the gateway-side settle lane ───────────────────────────────

    def test_17_empty_context_gateway_cancel_keeps_question_with_partial(self):
        """Item 1: the gateway-side settle must reconcile BEFORE the display insert.

        context_messages is EMPTY while session.messages holds the prior
        conversation with an older unanswered prompt (A-old). The settle used
        to insert the display partial into session.messages BEFORE
        _reconcile_gateway_cancelled_context ran, so with an empty context
        the reconcile seeded itself from the transcript projection INCLUDING
        that partial: the recovered qB then sat BEFORE an assistant tail,
        the partial-append guard saw no owning-prompt tail (so the seeded
        `_recovered` flag was never cleared), and the admission builder's
        answered-turn rule dropped qB — the partial stayed behind and
        appeared to answer A-old. Master sends NO history for this shape
        (the gateway uses its own transcript), so the regression was
        strictly worse than master.
        """
        from api.gateway_chat import _settle_gateway_cancelled_turn

        session = make_session("empty-ctx-cancel", [], messages=[
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "A-old"},
        ])
        # Imported/legacy shape: empty model-facing context, full transcript.
        session.context_messages = []
        session.save()

        stream_id = "stream-empty-cancel"
        set_mid_turn_stream_state(session, stream_id, partial="B partial", prompt="qB")

        # Gateway-side cancel: the worker settles the cancelled turn itself
        # (status lane — no browser Stop through cancel_stream()).
        _settle_gateway_cancelled_turn(session.session_id, stream_id)

        body = admit_run(session.session_id, msg_text="q2")

        self.assertEqual(
            history_rows(body),
            [
                ("user", "u0"),
                ("assistant", "a0"),
                ("user", "A-old"),
                ("user", "qB"),
                ("assistant", "B partial"),
            ],
            "an empty-context gateway-side cancel must keep qB next to its "
            f"own partial, got {history_rows(body)}",
        )
        self.assertEqual(body.get("input"), "q2")
        teardown_stream_state(stream_id)

    def test_18_projected_rows_keep_active_turn_token(self):
        """Item 2: the empty-context seed must not strip `_active_turn_token`.

        _project_transcript_context_rows dropped each user row's
        `_active_turn_token`, so the FIRST empty-context interrupt seeded a
        token-less prompt row into the model context. The SECOND interrupt of
        the same prompt inside the same wall second then hit the token-less
        fallback in the mirror dedup (content + int-second match) and was
        treated as the first turn: only one prompt/partial pair was mirrored
        and the next admission carried one turn where master carries both.
        """
        from api.gateway_chat import _settle_gateway_cancelled_turn
        from api.process_event_utils import build_active_turn_token

        session = make_session("empty-twice", [], messages=[
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
        ])
        session.context_messages = []
        session.save()
        fixed_started = 1790000000.5  # both interrupts share one wall second

        stream_1 = "stream-empty-twice-1"
        set_mid_turn_stream_state(session, stream_1, partial="P partial one", prompt="P")
        session.pending_started_at = fixed_started
        session.save()
        _settle_gateway_cancelled_turn(session.session_id, stream_1)

        stream_2 = "stream-empty-twice-2"
        # Re-resolve the session the way the next turn's route does; stale
        # handles must never be re-armed (same pattern as test_15).
        session = get_session(session.session_id)
        set_mid_turn_stream_state(session, stream_2, partial="P partial two", prompt="P")
        session.pending_started_at = fixed_started
        session.save()
        _settle_gateway_cancelled_turn(session.session_id, stream_2)

        ctx = get_session(session.session_id).context_messages
        expected_ctx_shapes = [
            ("user", "u0"),
            ("assistant", "a0"),
            ("user", "P"),
            ("assistant", "P partial one"),
            ("user", "P"),
            ("assistant", "P partial two"),
        ]
        self.assertEqual(
            [(row.get("role"), row.get("content")) for row in ctx],
            expected_ctx_shapes,
            "two gateway-side interrupts of the same prompt inside one "
            f"second must stay two distinct turns, got {ctx}",
        )
        p_tokens = [
            row.get("_active_turn_token") for row in ctx
            if row.get("role") == "user" and row.get("content") == "P"
        ]
        self.assertEqual(
            p_tokens,
            [
                build_active_turn_token(stream_1, fixed_started),
                build_active_turn_token(stream_2, fixed_started),
            ],
            f"projected seed rows must keep each stream-bound token, got {p_tokens}",
        )

        body = admit_run(session.session_id, msg_text="q2")
        self.assertEqual(
            history_rows(body),
            expected_ctx_shapes,
            f"next admission must carry both interrupted turns, got {history_rows(body)}",
        )
        teardown_stream_state(stream_1)
        teardown_stream_state(stream_2)

    # ── Round-6 re-gate (2026-10-12 14:04 UTC): sentinel scope — only a
    # ── deliberately emptied history carries the sentinel ───────────────────

    def test_19_imported_session_empty_context_omits_key_gateway_loads_store(self):
        """Round-6 must-fix: imported CLI/legacy sessions keep the fallback.

        import_cli_session creates sessions with messages populated but
        context_messages EMPTY (api/models.py import_cli_session). Master
        omits conversation_history, so the Gateway admission handler loads
        the SessionDB transcript and the first WebUI turn sees the imported
        rows. The cleared-sessions sentinel (73955fec) fired for EVERY empty
        outbound history — the `not conversation_history` clause is always
        true inside the elif — so imported sessions ran blind behind a
        system-only sentinel. This shape (messages populated, context empty,
        no watermark) must OMIT the key so the gateway store fallback fires.
        """
        session = make_session(
            "imported",
            [],
            messages=[
                {"role": "user", "content": "remember mango"},
                {"role": "assistant", "content": "mango acknowledged"},
            ],
        )
        # The gateway's own store holds the imported transcript: what its
        # admission handler loads when the body omits the key.
        GATEWAY_STORE[session.session_id] = [
            {"role": "user", "content": "remember mango"},
            {"role": "assistant", "content": "mango acknowledged"},
        ]

        body = admit_run(session.session_id, msg_text="what fruit?")

        self.assertNotIn(
            "conversation_history",
            body,
            "an imported session (messages populated, context empty) must "
            f"omit the key so the gateway loads its store, got "
            f"{body.get('conversation_history')}",
        )
        self.assertEqual(body.get("input"), "what fruit?")
        self.assertNotIn(
            "No earlier conversation turns.",
            json.dumps(body),
            f"an imported session must not get the sentinel: {json.dumps(body)}",
        )

        # Full worker with the gateway's admission fallback applied: the
        # composed answer proves the gateway loaded its stored mango rows —
        # the first WebUI turn must not run blind.
        from api.routes import _prepare_chat_start_session_for_stream

        stream_id = "stream-imported-wb"
        fresh = get_session(session.session_id)
        _prepare_chat_start_session_for_stream(
            fresh,
            msg="what fruit?",
            attachments=[],
            workspace="/tmp",
            model="test-model",
            model_provider="",
            stream_id=stream_id,
            source="webui",
            defer_save=True,
        )
        fresh.save()
        with self._gateway_worker_env():
            self._drive_full_worker_with_gateway_fallback(
                session.session_id, "what fruit?", stream_id,
            )
        self.assertNotIn(
            "conversation_history",
            CAPTURED_BODIES[0],
            f"the worker's admission must omit the key too, got "
            f"{CAPTURED_BODIES[0].get('conversation_history')}",
        )
        saved = get_session(session.session_id)
        ctx = saved.context_messages
        assistant_rows = [
            row.get("content") for row in ctx
            if isinstance(row, dict) and row.get("role") == "assistant"
        ]
        self.assertEqual(
            assistant_rows[-1],
            "reloaded[remember mango|mango acknowledged]",
            f"the LAST answer must be composed over the gateway's stored "
            f"mango rows (fallback fired), got {assistant_rows}",
        )
        self.assertIn(
            "mango acknowledged",
            [str(row.get("content") or "") for row in ctx],
            f"the imported mango rows must reach the model context via the "
            f"gateway store fallback, got {ctx}",
        )
        flat = json.dumps(ctx)
        self.assertNotIn(
            "No earlier conversation turns.", flat,
            f"the sentinel is transport-only and must never be saved: {flat}",
        )
        teardown_stream_state(stream_id)

    def test_20_brand_new_session_omits_key_and_keeps_system_prompt(self):
        """Round-6 must-fix: a brand-new session's first turn keeps master.

        A NEW Gateway chat's first turn reaches the builder with messages
        empty, context empty and NO truncation watermark (deferred mode
        commits the prompt only after the run). The always-true
        `not conversation_history` clause sent the system-only sentinel; the
        agent seeded from it and prepended its own system row, and the
        Anthropic-native converter keeps the LAST system row — so the cached
        prompt + WebUI instructions were dropped on the wire. This shape
        must OMIT the key (master parity) and carry no sentinel row at all,
        so the agent's own system row is the only system row.
        """
        session = make_session("brand-new", [], messages=[])

        body = admit_run(session.session_id, msg_text="first question")

        self.assertNotIn(
            "conversation_history",
            body,
            "a brand-new session (messages empty, context empty, no "
            f"watermark) must omit the key, got "
            f"{body.get('conversation_history')}",
        )
        self.assertEqual(body.get("input"), "first question")
        self.assertNotIn(
            "No earlier conversation turns.",
            json.dumps(body),
            "no sentinel row may precede the agent's own system row: "
            f"{json.dumps(body)}",
        )

    # ── Round-7 re-gate (2026-10-13 16:02 UTC): sentinel role ───────────────

    def test_21_sentinel_is_user_role_and_system_prompt_survives(self):
        """Re-gate item 2: the sentinel must not eat the Anthropic system prompt.

        The installed Agent's Anthropic converter folds a run's system rows
        into the provider system prompt keeping only the LAST one
        (anthropic_message_convert.py ~742 — modeled by
        _anthropic_native_converter_seam). A `system` sentinel therefore
        replaced the ENTIRE system prompt — agent instructions + WebUI
        context — with "No earlier conversation turns." on every
        deliberate-empty lane (clear, retry-to-empty, undo-to-empty,
        truncate-to-empty, first-turn regeneration). As a USER row the
        converter leaves it in `messages` and the agent's system prompt
        survives.

        Also pins the observed admission shape the run-launch path accepts:
        the sentinel is the ONLY conversation_history row, the prompt rides
        `input` alone (never duplicated into the history), so the composed
        provider tail is the sentinel user turn followed by the input user
        turn — two consecutive user rows the Anthropic Messages API
        combines into one turn rather than rejecting.
        """
        agent_instructions = "You are Hermes, the agent's cached system prompt."
        session = make_session("cleared-sysprompt", [], messages=[])
        session.truncation_watermark = 0.0
        session.save()

        GATEWAY_STORE[session.session_id] = [
            {"role": "user", "content": "pre-clear-q"},
            {"role": "assistant", "content": "pre-clear-a"},
        ]

        body = admit_run(
            session.session_id, msg_text="fresh question",
            prefill_messages=[{"role": "system", "content": agent_instructions}],
        )

        # The admitted shape: sentinel-only history, prompt only in `input`.
        self.assertEqual(
            history_rows(body),
            [("user", "No earlier conversation turns.")],
            "the sentinel must be the ONLY history row, sent as a USER row, "
            f"got {history_rows(body)}",
        )
        self.assertEqual(body.get("input"), "fresh question")

        composed = _anthropic_native_converter_seam(body)
        self.assertEqual(
            composed["system"],
            agent_instructions,
            "the converter must keep the AGENT's system prompt, got "
            f"{composed['system']!r}",
        )
        self.assertEqual(
            composed["messages"],
            [
                {"role": "user", "content": "No earlier conversation turns."},
                {"role": "user", "content": "fresh question"},
            ],
            "the sentinel must appear exactly once, as a USER row, ahead of "
            f"the input prompt turn, got {composed['messages']}",
        )


def test_projection_skips_recovered_display_only_rows():
    """RM fold: a journal-recovered answer marked ``_recovered_display_only``
    must not be projected into the seeded model context on a Gateway cancel.

    The empty-context seed accepted these rows and dropped the marker, so the
    next admission carried an answer the model never produced in this context.
    """
    from api.gateway_chat import _project_transcript_context_rows

    rows = _project_transcript_context_rows([
        {"role": "user", "content": "u0", "timestamp": 1790000000.0},
        {"role": "assistant", "content": "unproven recovered answer",
         "_recovered": True, "_recovered_display_only": True},
        {"role": "assistant", "content": "a0", "timestamp": 1790000001.0},
    ])
    contents = [r.get("content") for r in rows]
    assert "unproven recovered answer" not in contents
    assert contents == ["u0", "a0"]
    assert all("_recovered_display_only" not in r for r in rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)
