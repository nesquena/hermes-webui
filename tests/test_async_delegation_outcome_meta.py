"""Server-owned outcome metadata for async-delegation wakeup rows (#7361).

The async ``delegate_task`` envelope embeds caller-/subagent-controlled goal,
context and result text with no delimiter, so a forged status frame cannot be
told apart from the formatter's by reading the body. The delivery path derives
the outcome from the completion event instead and hands it to
``start_session_turn``; the accepted turn records it on its pending state
(``Session.pending_wakeup_meta``, bound to ``pending_started_at``) and persists
it before the delivery is acknowledged. Every path that materializes the turn —
settlement, eager checkpoint, adoption, cancel and restart recovery — stamps it
as the row's ``_wakeup_meta``, so neither unrelated deliveries nor a restart
can lose a failure chip.
"""

import json

import pytest

import api.models as models
from api import background_process as bp
from api import process_event_utils as peu
from api import routes, streaming
from api.models import Session, _append_recovered_pending_turn


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


def _single(**fields):
    evt = {"type": "async_delegation", "delegation_id": "deleg_1"}
    evt.update(fields)
    return evt


def _batch(results, **fields):
    evt = {"type": "async_delegation", "delegation_id": "deleg_b", "is_batch": True, "results": results}
    evt.update(fields)
    return evt


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({"status": "completed", "summary": "ok"}, "completed"),
        ({"status": "success"}, "completed"),
        ({}, "completed"),  # the formatter's own default
        ({"status": "completed", "truncated": True}, "partial"),
        ({"status": "completed", "exit_reason": "max_iterations"}, "partial"),
        ({"status": "failed", "error": "boom"}, "error"),
        ({"status": "interrupted"}, "error"),
        ({"status": "timeout"}, "error"),
        ({"status": "unknown"}, "error"),
    ],
)
def test_single_outcome_mirrors_the_formatter_status_line(fields, status):
    assert peu.async_delegation_outcome_meta(_single(**fields)) == {
        "type": "async_delegation",
        "task_id": "deleg_1",
        "status": status,
    }


@pytest.mark.parametrize(
    ("results", "status", "ok"),
    [
        ([{"status": "completed"}, {"status": "success"}], "completed", 2),
        ([{"status": "failed"}, {"status": "error"}], "error", 0),
        ([{"status": "completed"}, {"status": "failed"}], "partial", 1),
        ([{"status": "completed"}, {"status": "completed", "truncated": True}], "partial", 1),
        ([{"status": "failed"}, {"status": "completed", "truncated": True}], "partial", 0),
    ],
)
def test_batch_outcome_aggregates_the_unit_markers(results, status, ok):
    meta = peu.async_delegation_outcome_meta(_batch(results))
    assert meta == {
        "type": "async_delegation",
        "task_id": "deleg_b",
        "status": status,
        "task_count": len(results),
        "ok_count": ok,
    }


def test_batch_crash_without_results_is_error():
    meta = peu.async_delegation_outcome_meta(_batch([], error="pool died", goals=["a", "b"]))
    assert meta["status"] == "error"
    assert meta["task_count"] == 2
    assert meta["ok_count"] is None


def test_batch_without_results_or_error_is_unknown():
    assert peu.async_delegation_outcome_meta(_batch([]))["status"] == "complete"


@pytest.mark.parametrize(
    "evt",
    [
        None,
        {"type": "completion", "session_id": "bg-1"},
        _single(task_failure_notice=True, results=[{"status": "failed"}]),
        _batch([{"status": "completed"}, "not-a-dict"]),
    ],
    ids=["none", "other-type", "task-failure-notice", "malformed-results"],
)
def test_unrenderable_events_have_no_outcome(evt):
    assert peu.async_delegation_outcome_meta(evt) is None


FAILED_META = {"type": "async_delegation", "task_id": "deleg_1", "status": "error"}
SINGLE_BODY = (
    "[ASYNC DELEGATION COMPLETE — deleg_1]\n"
    "A background subagent you dispatched earlier has finished.\n"
    "\n"
    "Original goal: audit\n"
    "\n"
    "Role: leaf   Model: ?\n"
    "Status: failed   API calls: 3\n"
    "--- RESULT ---\n"
    "boom"
)


def test_stamp_copies_the_turn_owned_outcome():
    row = {"role": "user", "content": SINGLE_BODY}
    peu.stamp_message_source(row, "process_wakeup", wakeup_meta=FAILED_META)
    assert row["_wakeup_meta"] == FAILED_META

    # A stamped copy, not the caller's own dict.
    row["_wakeup_meta"]["status"] = "completed"
    assert FAILED_META["status"] == "error"


def test_stamp_ignores_outcome_for_non_wakeup_sources():
    typed = {"role": "user", "content": SINGLE_BODY}
    peu.stamp_message_source(typed, "webui", wakeup_meta=FAILED_META)
    assert "_wakeup_meta" not in typed


def test_stamp_never_overwrites_an_existing_meta():
    existing = {"type": "async_delegation", "task_id": "deleg_1", "status": "completed"}
    row = {"role": "user", "content": SINGLE_BODY, "_wakeup_meta": dict(existing)}
    peu.stamp_message_source(row, "process_wakeup", wakeup_meta=FAILED_META)
    assert row["_wakeup_meta"] == existing


def test_body_alone_never_yields_an_async_delegation_outcome():
    """No process-global body lookup remains: an unowned row stays unstamped."""
    row = {"role": "user", "content": SINGLE_BODY}
    peu.stamp_message_source(row, "process_wakeup")
    assert "_wakeup_meta" not in row


def _pending_wakeup_session(sid="sess-a", started_at=1788025846.8926563, meta=FAILED_META):
    s = Session(
        session_id=sid,
        pending_user_message=SINGLE_BODY,
        pending_user_source="process_wakeup",
        pending_started_at=started_at,
    )
    s.pending_wakeup_meta = peu.pending_wakeup_meta_record(meta, started_at)
    return s


def test_pending_outcome_is_owned_by_the_turn_it_was_bound_to():
    s = _pending_wakeup_session()
    assert peu.pending_turn_wakeup_meta(s) == FAILED_META

    # A later turn (new pending_started_at) never inherits it.
    s.pending_started_at += 1
    assert peu.pending_turn_wakeup_meta(s) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: setattr(s, "pending_user_source", "webui"),
        lambda s: setattr(s, "pending_user_message", None),
        lambda s: setattr(s, "pending_started_at", None),
        lambda s: setattr(s, "pending_wakeup_meta", {"meta": FAILED_META}),
        lambda s: setattr(s, "pending_wakeup_meta", "garbage"),
    ],
    ids=["typed-turn", "settled", "no-start", "unbound-record", "malformed-record"],
)
def test_pending_outcome_requires_a_live_owning_wakeup_turn(mutate):
    s = _pending_wakeup_session()
    mutate(s)
    assert peu.pending_turn_wakeup_meta(s) is None


def _persisted(sid):
    return json.loads((models.SESSION_DIR / f"{sid}.json").read_text(encoding="utf-8"))


def _accept_wakeup_turn(sid, meta):
    """Run the real chat-start pending-state write the accepted wakeup takes."""
    s = Session(session_id=sid, title="t", messages=[{"role": "assistant", "content": "earlier"}])
    models.SESSIONS[sid] = s
    routes._prepare_chat_start_session_for_stream(
        s,
        msg=SINGLE_BODY,
        attachments=[],
        workspace="",
        model="m",
        model_provider=None,
        stream_id=f"stream-{sid}",
        source="process_wakeup",
        wakeup_meta=meta,
    )
    return s


def test_accepted_turn_persists_its_outcome_before_acknowledgement(monkeypatch):
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    s = _accept_wakeup_turn("sess-ack", FAILED_META)

    # Deferred mode keeps the transcript empty, but the pending turn's own
    # durable state already carries the outcome.
    disk = _persisted("sess-ack")
    assert disk["pending_user_source"] == "process_wakeup"
    assert disk["pending_wakeup_meta"] == {
        "pending_started_at": s.pending_started_at,
        "meta": FAILED_META,
    }


def test_outcome_survives_many_other_deliveries_and_a_restart(monkeypatch):
    """Eviction + restart: the failed chip comes from the accepted turn's own
    state, not a bounded process-global cache, so 300 later deliveries and a
    process restart before settlement still recover ``error``."""
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    _accept_wakeup_turn("sess-victim", FAILED_META)
    ok = {"type": "async_delegation", "task_id": "deleg_x", "status": "completed"}
    for i in range(300):
        _accept_wakeup_turn(f"sess-other-{i}", ok)

    models.SESSIONS.clear()  # restart: nothing survives but the sidecar
    reloaded = Session.load("sess-victim")
    assert peu.pending_turn_wakeup_meta(reloaded) == FAILED_META

    recovered = _append_recovered_pending_turn(reloaded)
    assert recovered["_source"] == "process_wakeup"
    assert recovered["_wakeup_meta"] == FAILED_META


def test_restart_recovery_before_error_stamps_the_failed_outcome(monkeypatch):
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    _accept_wakeup_turn("sess-err", FAILED_META)
    models.SESSIONS.clear()
    reloaded = Session.load("sess-err")

    assert streaming._materialize_pending_user_turn_before_error(reloaded) is True
    row = next(m for m in reversed(reloaded.messages) if m.get("role") == "user")
    assert row["_wakeup_meta"] == FAILED_META


def test_settlement_after_restart_stamps_the_failed_outcome(monkeypatch):
    """Adopting the pending turn into the turn identity carries the outcome to
    the row deferred settlement materializes."""
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    _accept_wakeup_turn("sess-settle", FAILED_META)
    models.SESSIONS.clear()
    reloaded = Session.load("sess-settle")

    identity = streaming._active_turn_authority(reloaded, reloaded.active_stream_id, SINGLE_BODY)
    assert identity["wakeup_meta"] == FAILED_META
    row = streaming._materialize_active_turn_user(identity, SINGLE_BODY, "webui")
    assert row["_source"] == "process_wakeup"
    assert row["_wakeup_meta"] == FAILED_META


def test_eager_checkpoint_stamps_the_turn_owned_outcome(monkeypatch):
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "eager")
    s = _accept_wakeup_turn("sess-eager", FAILED_META)
    row = s.messages[-1]
    assert row["_source"] == "process_wakeup"
    assert row["_wakeup_meta"] == FAILED_META
    assert _persisted("sess-eager")["messages"][-1]["_wakeup_meta"] == FAILED_META


def test_settled_turn_drops_its_outcome_and_never_stamps_the_next(monkeypatch):
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    s = _accept_wakeup_turn("sess-next", FAILED_META)
    s.active_stream_id = None
    s.pending_user_message = None
    s.pending_started_at = None
    s.pending_user_source = None
    s.save()
    assert _persisted("sess-next")["pending_wakeup_meta"] is None

    # A later wakeup turn without an event-derived outcome is unstamped.
    routes._prepare_chat_start_session_for_stream(
        s,
        msg=SINGLE_BODY,
        attachments=[],
        workspace="",
        model="m",
        model_provider=None,
        stream_id="stream-next-2",
        source="process_wakeup",
    )
    assert s.pending_wakeup_meta is None
    assert "_wakeup_meta" not in _append_recovered_pending_turn(s)


def test_delivery_hands_the_event_outcome_to_the_accepted_turn(monkeypatch):
    captured = {}

    class _Claim:
        durable = True

    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)
    monkeypatch.setattr(bp, "claim_async_delegation_delivery", lambda *_a, **_k: _Claim())
    monkeypatch.setattr(bp, "format_wakeup_prompt", lambda _evt: SINGLE_BODY)
    monkeypatch.setattr(
        bp, "_start_async_delegation_wakeup_turn", lambda *a, **k: captured.update(k)
    )
    try:
        bp._process_async_delegation_event(
            _single(status="failed", error="boom"),
            session_id="sess-deliver",
            delegation_id="deleg_1",
            process_registry=None,
        )
    finally:
        bp._release_async_delegation_wakeup_admission("sess-deliver")
    assert captured["wakeup_meta"] == FAILED_META


def test_outcome_derivation_failure_never_costs_the_delivery(monkeypatch):
    captured = {}

    class _Claim:
        durable = True

    def _boom(_evt):
        raise ValueError("bad event")

    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)
    monkeypatch.setattr(bp, "claim_async_delegation_delivery", lambda *_a, **_k: _Claim())
    monkeypatch.setattr(bp, "format_wakeup_prompt", lambda _evt: SINGLE_BODY)
    monkeypatch.setattr(bp, "async_delegation_outcome_meta", _boom)
    monkeypatch.setattr(
        bp, "_start_async_delegation_wakeup_turn", lambda *a, **k: captured.update(k)
    )
    try:
        bp._process_async_delegation_event(
            _single(status="failed"),
            session_id="sess-deliver-2",
            delegation_id="deleg_1",
            process_registry=None,
        )
    finally:
        bp._release_async_delegation_wakeup_admission("sess-deliver-2")
    assert "wakeup_meta" in captured and captured["wakeup_meta"] is None
