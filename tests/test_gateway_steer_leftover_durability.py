"""Regression tests for the server-durable terminal-steer leftover (#7440 gate).

The gate review reproduced two loss boundaries in the live-only leftover
translation:

1. Switching to an existing session closes the owning stream's only SSE
   consumer: the terminal ``run.completed.pending_steer`` event fired into a
   stream nobody was watching, and returning to the owning session did not
   replay it — the accepted guidance was silently lost.
2. A received leftover could be erased on settled-session reload (client
   ``_queued_at`` stamp preceding the settled assistant timestamp) and queue
   application was not idempotent by stable identity.

The fix makes recovery SERVER-DURABLE and OWNER-SCOPED: the relay persists
the leftover into the owning session's sidecar (keyed by the gateway run id)
at terminal translation time, ``GET /api/session`` re-offers an unconsumed
slot on every load, and the slot is retired ONLY by an explicit ack — the
transactional ``/api/chat/start`` clear (matched by run id) when the turn
that ships the text starts, or the user's explicit dismissal. The frontend
queue is idempotent by run id (never by text or a time window), and leftover
entries are exempt from the restore freshness filter because they are
post-assistant user intent by construction (frontend contract locked in
tests/test_gateway_pending_steer_relay.py + the browser gate).

These tests pin the SERVER half: slot persistence, sidecar round-trip,
newest-run-wins supersession, ack matching (stale acks never clear a newer
slot), the transactional chat-start clear, and the translator wiring (slot
write + run-id stamp on the SSE payload). The leftover text field passes
through the session payload unredacted exactly like its sibling
``pending_user_message`` (user-authored guidance echoed only to the owning
session's own GET); the redaction boundary covers transcript-bearing fields.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).parent.parent
SESSIONS_JS = REPO / "static" / "sessions.js"
UI_JS = REPO / "static" / "ui.js"


@pytest.fixture()
def session_store(monkeypatch, tmp_path):
    """Isolate the Session sidecar store + in-memory LRU."""
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    try:
        yield models
    finally:
        models.SESSIONS.clear()


def _make_session(models, sid: str):
    s = models.Session(session_id=sid, messages=[], workspace="/tmp/w")
    s.save()
    models.SESSIONS[sid] = s
    return s


# ---------------------------------------------------------------------------
# Slot persistence + sidecar round-trip
# ---------------------------------------------------------------------------


def test_persist_writes_durable_slot_to_owning_session(session_store):
    from api import gateway_chat

    models = session_store
    _make_session(models, "sess-durable")
    assert gateway_chat._persist_gateway_steer_leftover(
        "sess-durable", "run-abc", "use the safer path"
    ) is True

    reloaded = models.Session.load("sess-durable")
    assert reloaded.pending_steer_leftover_text == "use the safer path"
    assert reloaded.pending_steer_leftover_run_id == "run-abc"
    assert reloaded.pending_steer_leftover_at is not None
    # Durability means the SIDE CAR carries it, not just the in-memory object.
    sidecar = json.loads(
        (models.SESSION_DIR / "sess-durable.json").read_text(encoding="utf-8")
    )
    assert sidecar["pending_steer_leftover_text"] == "use the safer path"
    assert sidecar["pending_steer_leftover_run_id"] == "run-abc"


def test_persist_is_owner_scoped_and_missing_session_is_false(session_store):
    from api import gateway_chat

    models = session_store
    _make_session(models, "sess-owner")
    assert gateway_chat._persist_gateway_steer_leftover(
        "sess-unknown", "run-x", "orphan guidance"
    ) is False
    # The owning session was not touched by the unknown session's write.
    reloaded = models.Session.load("sess-owner")
    assert reloaded.pending_steer_leftover_run_id == ""


def test_slot_survives_save_load_roundtrip(session_store):
    models = session_store
    s = _make_session(models, "sess-roundtrip")
    s.pending_steer_leftover_text = "remember the constraint"
    s.pending_steer_leftover_run_id = "run-42"
    s.pending_steer_leftover_at = 1234.5
    s.save()
    reloaded = models.Session.load("sess-roundtrip")
    assert reloaded.pending_steer_leftover_text == "remember the constraint"
    assert reloaded.pending_steer_leftover_run_id == "run-42"
    assert reloaded.pending_steer_leftover_at == 1234.5


def test_newest_run_supersedes_older_slot(session_store):
    from api import gateway_chat

    models = session_store
    _make_session(models, "sess-supersede")
    gateway_chat._persist_gateway_steer_leftover("sess-supersede", "run-1", "first guidance")
    gateway_chat._persist_gateway_steer_leftover("sess-supersede", "run-2", "second guidance")
    reloaded = models.Session.load("sess-supersede")
    # A session runs one gateway run at a time: the newest terminal leftover
    # owns the single durable slot.
    assert reloaded.pending_steer_leftover_run_id == "run-2"
    assert reloaded.pending_steer_leftover_text == "second guidance"


# ---------------------------------------------------------------------------
# Ack semantics: transactional consume + explicit dismissal
# ---------------------------------------------------------------------------


def test_ack_matching_run_clears_slot_and_persists(session_store):
    from api import routes

    models = session_store
    _make_session(models, "sess-ack")
    from api import gateway_chat

    gateway_chat._persist_gateway_steer_leftover("sess-ack", "run-ok", "ship this")

    assert routes._ack_steer_leftover("sess-ack", "run-ok", action="dismissed") is True
    reloaded = models.Session.load("sess-ack")
    assert reloaded.pending_steer_leftover_text == ""
    assert reloaded.pending_steer_leftover_run_id == ""
    assert reloaded.pending_steer_leftover_at is None
    # Second ack has nothing to clear.
    assert routes._ack_steer_leftover("sess-ack", "run-ok", action="dismissed") is False


def test_ack_stale_run_id_never_clears_newer_slot(session_store):
    from api import routes

    models = session_store
    _make_session(models, "sess-stale")
    from api import gateway_chat

    gateway_chat._persist_gateway_steer_leftover("sess-stale", "run-1", "old")
    gateway_chat._persist_gateway_steer_leftover("sess-stale", "run-2", "new")
    # A stale ack (e.g. from a long-lived tab holding run-1) must not erase
    # the newer leftover that now owns the slot.
    assert routes._ack_steer_leftover("sess-stale", "run-1", action="consumed") is False
    reloaded = models.Session.load("sess-stale")
    assert reloaded.pending_steer_leftover_run_id == "run-2"
    assert reloaded.pending_steer_leftover_text == "new"


def test_ack_unknown_session_is_false(session_store):
    from api import routes

    assert routes._ack_steer_leftover("sess-ghost", "run-1", action="dismissed") is False


def test_chat_start_prepare_clears_slot_transactionally(session_store):
    """The turn that ships the leftover retires the slot at the same mutation
    point that claims the turn (consume-and-ack as ONE atomic state change):
    a crash either leaves the slot intact (re-offered on reload, never
    silently lost) or leaves a normally-pending user turn."""
    from api import routes

    models = session_store
    s = _make_session(models, "sess-txn")
    s.pending_steer_leftover_text = "queued guidance"
    s.pending_steer_leftover_run_id = "run-txn"
    s.pending_steer_leftover_at = 111.0
    s.save()

    routes._prepare_chat_start_session_for_stream(
        s,
        msg="queued guidance",
        attachments=[],
        workspace="/tmp/w",
        model="m",
        model_provider=None,
        stream_id="stream-1",
        steer_leftover_ack="run-txn",
    )
    assert s.pending_steer_leftover_text == ""
    assert s.pending_steer_leftover_run_id == ""
    # The turn state itself was claimed normally.
    assert s.pending_user_message == "queued guidance"

    # A turn WITHOUT a matching ack must NOT clear the slot (the client queue
    # may still hold the leftover behind another in-flight turn).
    s2 = _make_session(models, "sess-txn2")
    s2.pending_steer_leftover_text = "still pending"
    s2.pending_steer_leftover_run_id = "run-keep"
    s2.pending_steer_leftover_at = 222.0
    routes._prepare_chat_start_session_for_stream(
        s2,
        msg="an unrelated user message",
        attachments=[],
        workspace="/tmp/w",
        model="m",
        model_provider=None,
        stream_id="stream-2",
    )
    assert s2.pending_steer_leftover_text == "still pending"
    assert s2.pending_steer_leftover_run_id == "run-keep"

    # And a MISMATCHED ack must not clear it either (stale ack).
    routes._prepare_chat_start_session_for_stream(
        s2,
        msg="another message",
        attachments=[],
        workspace="/tmp/w",
        model="m",
        model_provider=None,
        stream_id="stream-3",
        steer_leftover_ack="run-old",
    )
    assert s2.pending_steer_leftover_run_id == "run-keep"


# ---------------------------------------------------------------------------
# Translator wiring: terminal translation persists the slot + stamps run id
# ---------------------------------------------------------------------------


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
    def __init__(self, lines=()):
        self._lines = [line if isinstance(line, bytes) else line.encode("utf-8") for line in lines]

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _run_translator(session_store, tmp_path, session_id, stream_id, sse_lines):
    from api import gateway_chat
    from api.run_journal import RunJournalWriter

    journal = RunJournalWriter(session_id, stream_id, session_dir=tmp_path)
    emitted = []

    def put_gateway_event(event, data):
        entry = journal.append_sse_event(event, data)
        emitted.append((event, data, entry))

    def fake_urlopen(req, *, timeout=None):
        if req.get_method() == "POST" and req.full_url.endswith("/v1/runs"):
            return _JsonResponse({"run_id": f"run-{stream_id}"})
        if req.get_method() == "GET" and req.full_url.endswith("/events"):
            return _SseResponse(sse_lines)
        raise AssertionError(f"unexpected gateway request: {req.get_method()} {req.full_url}")

    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
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
            cancel_event=threading.Event(),
            session=SimpleNamespace(context_messages=[]),
        )
    finally:
        urllib.request.urlopen = original_urlopen
    return SimpleNamespace(final_text=final_text, usage=usage, emitted=emitted)


def _completed_lines(payload: dict) -> list[bytes]:
    return [
        b"event: run.completed\n",
        f"data: {json.dumps(payload)}\n".encode("utf-8"),
        b"\n",
    ]


def test_translator_persists_slot_and_stamps_run_id(session_store, tmp_path):
    models = session_store
    _make_session(models, "sess-translate")
    result = _run_translator(
        session_store,
        tmp_path,
        "sess-translate",
        "stream-translate",
        _completed_lines({"output": "done", "pending_steer": "finalize with tests"}),
    )
    assert result.final_text == "done"

    # The SSE payload carries the stable run id for client-side dedupe/ack.
    leftovers = [
        (event, data) for event, data, _ in result.emitted if event == "pending_steer_leftover"
    ]
    assert leftovers == [
        (
            "pending_steer_leftover",
            {
                "session_id": "sess-translate",
                "run_id": "run-stream-translate",
                "text": "finalize with tests",
            },
        )
    ]

    # AND the durable slot was persisted on the owning session — recovery no
    # longer depends on a live SSE consumer being attached at completion.
    reloaded = models.Session.load("sess-translate")
    assert reloaded.pending_steer_leftover_text == "finalize with tests"
    assert reloaded.pending_steer_leftover_run_id == "run-stream-translate"


def test_translator_without_pending_steer_leaves_no_slot(session_store, tmp_path):
    models = session_store
    _make_session(models, "sess-noleftover")
    result = _run_translator(
        session_store,
        tmp_path,
        "sess-noleftover",
        "stream-noleftover",
        _completed_lines({"output": "done"}),
    )
    assert result.final_text == "done"
    assert not any(event == "pending_steer_leftover" for event, _, _ in result.emitted)
    reloaded = models.Session.load("sess-noleftover")
    assert reloaded.pending_steer_leftover_run_id == ""


# ---------------------------------------------------------------------------
# Frontend contract locks (fast static/VM-level)
# ---------------------------------------------------------------------------


def test_restore_filter_exempts_leftover_entries():
    """Blocker 2's erase half: a leftover entry (tagged with its stable run
    id) must NEVER be discarded for preceding the settled assistant
    timestamp — it is post-assistant user intent by construction."""
    src = SESSIONS_JS.read_text(encoding="utf-8")
    needle = "_entries.filter(e=>"
    start = src.index(needle)
    end = src.index(";", start)
    filter_expr = src[start:end]
    assert "_leftover_id" in filter_expr, (
        "the queue-restore freshness filter must exempt _leftover_id entries: "
        f"got {filter_expr!r}"
    )


def test_load_restores_server_durable_slot():
    """Blocker 1's recovery half: loadSession must re-offer an unconsumed
    server slot (GET /api/session payload fields) through the run-id-deduped
    queue — recovery must not depend on a live SSE consumer."""
    src = SESSIONS_JS.read_text(encoding="utf-8")
    assert "pending_steer_leftover_text" in src, (
        "sessions.js must read the durable leftover slot from the session payload"
    )
    assert "pending_steer_leftover_run_id" in src
    assert "_trackLeftoverPrefill" in src, (
        "recovered prefills must be tracked for transactional send-ack / "
        "explicit-dismissal wiring"
    )


def test_queue_idempotent_by_stable_identity():
    """Blocker 2's duplication half: queueSessionMessage must dedupe by the
    leftover's run id (stable identity), never by text or a time window, and
    must tag the entry so the drain can ack the server transactionally."""
    src = UI_JS.read_text(encoding="utf-8")
    assert "leftoverId" in src
    assert "_leftover_id" in src
    drain_pos = src.index("send({leftoverAck:")
    assert "next._leftover_id" in src[drain_pos - 400 : drain_pos + 200], (
        "the queue drain must pass the drained entry's _leftover_id into send()"
    )
