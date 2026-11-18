"""Server-side follow-up FIFO: messages sent while a turn runs are stored durably and
started in order as the next turns, never interrupting the running one.

Exercises the real api.followup_queue module and the real /api/chat/queue route
handler; only the agent-turn starter and the "is a turn active" probe are faked.
"""
import json
import threading
import time
from pathlib import Path

import pytest

from api import followup_queue as fq

SID = "sess_followup_1"


@pytest.fixture
def fqenv(tmp_path, monkeypatch):
    from api import config as cfg

    monkeypatch.setattr(cfg, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fq, "_START_RETRY_DELAY", 0.01)

    class Sess:
        gateway_run = None
        pending_user_message = None
        messages = []

    class Env:
        active = False          # is a turn running for SID
        started = []            # texts handed to start_session_turn, in order
        status = 200            # status start_session_turn returns
        lock = threading.Lock()
        session = Sess()        # None => the session no longer exists
        calls = 0

    env = Env()
    env.started = []

    import api.background_process as bp
    import api.routes as routes

    monkeypatch.setattr(bp, "_session_has_active_turn", lambda sid: env.active)
    monkeypatch.setattr(fq, "_load_session", lambda sid: env.session)
    fq._DRAIN_REQUESTED.clear()

    def fake_start(session_id, message, **kw):
        with env.lock:
            assert kw.get("source") == "queued_followup"
            env.calls += 1
            if env.status == 200:
                env.started.append(message)
                env.active = True  # the new turn is now running
            return {"_status": env.status, "error": "boom" if env.status >= 400 and env.status != 409 else None}

    monkeypatch.setattr(routes, "start_session_turn", fake_start)
    return env


def _wait(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _finish_turn(env):
    """What stream teardown does: session goes idle, then the drain hook runs."""
    env.active = False
    return fq.drain_followup_queue_for_session(SID)


def test_follow_up_is_stored_not_started_while_turn_running(fqenv):
    fqenv.active = True
    entry, err = fq.enqueue(SID, "do B next")
    assert err is None and entry["status"] == "queued"
    assert fq.drain_followup_queue_for_session(SID) is False  # busy: running task untouched
    assert fqenv.started == []
    assert [e["text"] for e in fq.list_entries(SID)] == ["do B next"]


def test_queued_message_runs_automatically_after_completion(fqenv):
    fqenv.active = True
    fq.enqueue(SID, "B")
    assert _finish_turn(fqenv) is True
    assert _wait(lambda: fqenv.started == ["B"])
    assert _wait(lambda: fq.list_entries(SID) == [])  # consumed exactly once


def test_multiple_messages_run_fifo_one_per_turn(fqenv):
    fqenv.active = True
    for t in ("one", "two", "three"):
        fq.enqueue(SID, t)
    for expected in (["one"], ["one", "two"], ["one", "two", "three"]):
        assert _finish_turn(fqenv) is True
        assert _wait(lambda exp=expected: fqenv.started == exp), fqenv.started
        assert _wait(lambda exp=expected: len(fq.list_entries(SID)) == 3 - len(exp))
    assert _finish_turn(fqenv) is False  # nothing left


def test_survives_restart_and_reconnect(fqenv):
    fqenv.active = True
    fq.enqueue(SID, "keep me")
    fq.enqueue(SID, "and me")
    # "Restart": nothing but the on-disk file is shared; a fresh read sees both in order.
    raw = json.loads((fq._path(SID)).read_text())
    assert [i["text"] for i in raw["items"]] == ["keep me", "and me"]
    assert [e["text"] for e in fq.list_entries(SID)] == ["keep me", "and me"]


def test_idempotent_client_id_prevents_duplicates(fqenv):
    fqenv.active = True
    a, _ = fq.enqueue(SID, "same", client_id="c1")
    b, _ = fq.enqueue(SID, "same", client_id="c1")  # api() network retry / re-migration
    assert a["id"] == b["id"] and len(fq.list_entries(SID)) == 1


def test_concurrent_drains_never_double_run(fqenv):
    fqenv.active = False
    fq.enqueue(SID, "only once")
    results = []
    gate = threading.Barrier(8)

    def go():
        gate.wait()
        results.append(fq.drain_followup_queue_for_session(SID))

    # fake_start flips active=True, but the claim itself must serialise the race.
    fqenv.active = False
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert _wait(lambda: fqenv.started == ["only once"])
    time.sleep(0.1)
    assert fqenv.started == ["only once"]  # exactly one start
    assert sum(1 for r in results if r) == 1


def test_enqueue_racing_turn_completion_is_not_lost(fqenv):
    """Message lands after the turn ended (teardown already ran): enqueue itself drains."""
    fqenv.active = False
    fq.enqueue(SID, "late")
    assert fq.drain_followup_queue_for_session(SID) is True
    assert _wait(lambda: fqenv.started == ["late"])


def test_409_keeps_entry_in_place_for_next_teardown(fqenv):
    fqenv.status = 409
    fq.enqueue(SID, "first")
    fq.enqueue(SID, "second")
    assert fq.drain_followup_queue_for_session(SID) is True
    assert _wait(lambda: [e["status"] for e in fq.list_entries(SID)] == ["queued", "queued"])
    assert [e["text"] for e in fq.list_entries(SID)] == ["first", "second"]  # order kept
    fqenv.status = 200
    assert fq.drain_followup_queue_for_session(SID) is True
    assert _wait(lambda: fqenv.started == ["first"])


def test_rejected_start_is_reported_and_retryable(fqenv):
    fqenv.status = 400  # the server rejected it outright: visible failure, user can retry
    fq.enqueue(SID, "bad")
    fq.drain_followup_queue_for_session(SID)
    assert _wait(lambda: fq.list_entries(SID)[0]["status"] == "failed")
    assert fq.list_entries(SID)[0]["error"] == "boom"
    fqenv.status = 200
    assert fq.retry(SID, fq.list_entries(SID)[0]["id"])
    assert fq.drain_followup_queue_for_session(SID) is True
    assert _wait(lambda: fqenv.started == ["bad"])


def test_server_error_is_retried_in_bounded_rounds_not_failed_at_once(fqenv):
    fqenv.status = 500  # e.g. Gateway not ready yet: keep the message, count the round
    fq.enqueue(SID, "later")
    fq.drain_followup_queue_for_session(SID)
    assert _wait(lambda: fq.list_entries(SID)[0]["status"] == "queued")
    fqenv.status = 200
    assert fq.drain_followup_queue_for_session(SID) is True
    assert _wait(lambda: fqenv.started == ["later"])


def test_stale_processing_entry_is_recovered(fqenv):
    fq.enqueue(SID, "orphan")
    claimed = fq.claim_next(SID)  # worker "died" mid-start
    assert claimed and fq.claim_next(SID) is None  # in flight: nobody else may start it
    items = json.loads(fq._path(SID).read_text())
    items["items"][0]["claimed_at"] = time.time() - fq.PROCESSING_STALE_SECS - 5
    fq._path(SID).write_text(json.dumps(items))
    assert fq.drain_followup_queue_for_session(SID) is True
    assert _wait(lambda: fqenv.started == ["orphan"])


def test_remove_clear_cap_and_validation(fqenv):
    fqenv.active = True
    e1, _ = fq.enqueue(SID, "a")
    fq.enqueue(SID, "b")
    assert fq.remove(SID, e1["id"]) and [e["text"] for e in fq.list_entries(SID)] == ["b"]
    assert fq.clear(SID) == 1 and fq.list_entries(SID) == []
    assert fq.enqueue(SID, "   ")[1] == "message is required"
    for i in range(fq.MAX_PENDING):
        assert fq.enqueue(SID, f"m{i}")[1] is None
    assert "full" in fq.enqueue(SID, "overflow")[1]
    for bad_sid in ("../x", "a/b", ""):
        with pytest.raises(ValueError):
            fq.list_entries(bad_sid)


def test_sessions_are_isolated(fqenv):
    fqenv.active = True
    fq.enqueue("sess_a", "for a")
    fq.enqueue("sess_b", "for b")
    assert [e["text"] for e in fq.list_entries("sess_a")] == ["for a"]
    assert [e["text"] for e in fq.list_entries("sess_b")] == ["for b"]


# ── HTTP route handler ───────────────────────────────────────────────────────

class _Sess:
    read_only = False


@pytest.fixture
def route(fqenv, monkeypatch):
    import api.routes as routes

    out = {}
    monkeypatch.setattr(routes, "j", lambda h, payload, status=200: out.update(payload=payload, status=status) or True)
    monkeypatch.setattr(routes, "bad", lambda h, msg, status=400: out.update(payload={"error": msg}, status=status) or True)
    monkeypatch.setattr(routes, "get_session", lambda sid: _Sess() if sid.startswith("sess_") else (_ for _ in ()).throw(KeyError(sid)))
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda sid: False)

    def call(path, body):
        out.clear()
        routes._handle_chat_queue(object(), path, body)
        return out["status"], out["payload"]

    return call


def test_route_accepts_while_busy_returns_queue_and_does_not_start(fqenv, route):
    fqenv.active = True
    status, p = route("/api/chat/queue", {"session_id": SID, "text": "x", "client_id": "k"})
    assert status == 200 and [i["text"] for i in p["items"]] == ["x"] and p["items"][0]["status"] == "queued"
    assert fqenv.started == []
    status, p = route("/api/chat/queue/remove", {"session_id": SID, "id": p["items"][0]["id"]})
    assert status == 200 and p["items"] == []


def test_route_reports_failures(fqenv, route):
    assert route("/api/chat/queue", {"session_id": SID, "text": ""})[0] == 400
    assert route("/api/chat/queue", {"text": "x"})[0] == 400
    assert route("/api/chat/queue", {"session_id": "nope", "text": "x"})[0] == 404
    assert route("/api/chat/queue/remove", {"session_id": SID, "id": "zzz"})[0] == 404
    fqenv.active = True
    for i in range(fq.MAX_PENDING):
        route("/api/chat/queue", {"session_id": SID, "text": f"m{i}"})
    assert route("/api/chat/queue", {"session_id": SID, "text": "over"})[0] == 429


def test_route_enqueue_when_idle_starts_immediately(fqenv, route):
    fqenv.active = False
    route("/api/chat/queue", {"session_id": SID, "text": "now"})
    assert _wait(lambda: fqenv.started == ["now"])


def test_stop_is_independent_of_queue_route():
    """Submitting a follow-up uses /api/chat/queue only; cancel stays /api/chat/cancel."""
    src = Path(__file__).parent.parent.joinpath("api", "routes.py").read_text(encoding="utf-8")
    body = src[src.index("def _handle_chat_queue"):src.index("def _handle_chat_start(")]
    assert "cancel" not in body.lower().replace("cancelled", "")


def test_teardown_hook_drains_followups():
    src = Path(__file__).parent.parent.joinpath("api", "streaming.py").read_text(encoding="utf-8")
    assert "drain_followup_queue_for_session(session_id)" in src
    assert src.index("drain_deferred_wakeups_for_session(session_id)") < src.index("drain_followup_queue_for_session(session_id)")


def test_gateway_backend_teardown_drains_followups():
    """HERMES_WEBUI_CHAT_BACKEND=gateway never runs streaming.py's teardown: the Gateway
    worker's own finally block must drain too (found by the live test on the deployed host)."""
    src = Path(__file__).parent.parent.joinpath("api", "gateway_chat.py").read_text(encoding="utf-8")
    tail = src[src.rindex("unregister_active_run(stream_id)"):]
    assert "drain_followup_queue_for_session(session_id)" in tail
    assert tail.index("clear_session_writeback_owner_if_owned") < tail.index("drain_followup_queue_for_session")


def test_get_queue_self_heals_a_stranded_idle_queue(fqenv, monkeypatch):
    import api.routes as routes

    seen = {}
    monkeypatch.setattr(routes, "j", lambda h, payload, status=200: seen.update(payload=payload) or True)
    monkeypatch.setattr(routes, "bad", lambda h, msg, status=400: seen.update(payload={"error": msg}) or True)
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *a, **k: True)
    fqenv.active = False
    fq.enqueue(SID, "stranded")  # e.g. teardown ran before the entry existed / server restarted
    fq.release(SID, fq.claim_next(SID)["id"])  # leave it queued, not processing
    class P:  # parsed url
        path = "/api/chat/queue"
        query = f"session_id={SID}"
    routes.handle_get(object(), P)
    assert _wait(lambda: fqenv.started == ["stranded"])


# ── review-driven hardening ──────────────────────────────────────────────────

def test_drain_refuses_while_a_gateway_turn_awaits_recovery(fqenv):
    """A disconnected Gateway turn keeps gateway_run/pending_user_message on the record so it can
    be re-attached; a follow-up must not replace it, from ANY drain trigger."""
    fqenv.session.gateway_run = {"stream_id": "old"}
    fq.enqueue(SID, "wait for recovery")
    assert fq.drain_followup_queue_for_session(SID) is False
    assert fqenv.started == [] and fq.list_entries(SID)[0]["status"] == "queued"
    fqenv.session.gateway_run = None  # recovered/settled
    fqenv.session.pending_user_message = "still pending"
    assert fq.drain_followup_queue_for_session(SID) is False
    fqenv.session.pending_user_message = None
    assert fq.drain_followup_queue_for_session(SID) is True
    assert _wait(lambda: fqenv.started == ["wait for recovery"])


def test_consumed_client_id_cannot_run_twice_after_response_loss(fqenv):
    fqenv.active = False
    fq.enqueue(SID, "once", client_id="c-resp-lost")
    assert fq.drain_followup_queue_for_session(SID) is True
    assert _wait(lambda: fqenv.started == ["once"] and fq.list_entries(SID) == [])
    entry, err = fq.enqueue(SID, "once", client_id="c-resp-lost")  # client's network retry
    assert err is None and entry["status"] == "started"
    assert fq.list_entries(SID) == [] and fqenv.started == ["once"]


def test_consumed_ids_expire_and_are_bounded(fqenv, monkeypatch):
    fqenv.active = True
    fq.enqueue(SID, "x", client_id="old")
    fq.complete(SID, fq.list_entries(SID)[0]["id"])
    monkeypatch.setattr(fq, "DONE_ID_TTL_SECS", 0.0)
    e, _ = fq.enqueue(SID, "x", client_id="old")  # expired id no longer blocks a deliberate resend
    assert e["status"] == "queued"


def test_release_race_redrains_when_the_ending_turn_asked(fqenv):
    """The ending turn's teardown hit a mid-claim entry and asked for a retry: the claimer must
    re-drain after it releases (no sleeping, no lost wake-up)."""
    fq.enqueue(SID, "raced")
    claimed = fq.claim_next(SID)
    assert fq.claim_next(SID) is None and SID in fq._DRAIN_REQUESTED  # teardown's refused drain
    fq.release(SID, claimed["id"], count_round=True)
    fq._redrain_if_requested(SID)
    assert _wait(lambda: fqenv.started == ["raced"])


def test_stuck_409_is_bounded_then_failed_not_looped(fqenv):
    fqenv.status = 409  # stale active-stream id: the session never accepts a start
    fq.enqueue(SID, "never")
    for _ in range(fq.MAX_START_ROUNDS + 2):
        fq.drain_followup_queue_for_session(SID)
        _wait(lambda: fq.list_entries(SID)[0]["status"] != "processing")
    e = fq.list_entries(SID)[0]
    assert e["status"] == "failed" and "busy" in e["error"]
    calls = fqenv.calls
    fq.drain_followup_queue_for_session(SID)
    time.sleep(0.2)
    assert fqenv.calls == calls  # failed entries are not retried automatically


def test_deleted_session_queue_is_dropped(fqenv):
    fq.enqueue(SID, "orphan")
    fqenv.session = None
    assert fq.drain_followup_queue_for_session(SID) is False
    assert fq.list_entries(SID) == []
    fqenv.session = type("S", (), {"gateway_run": None, "pending_user_message": None, "messages": []})()
    fq.enqueue(SID, "again")
    fqenv.status = 404
    fq.drain_followup_queue_for_session(SID)
    assert _wait(lambda: fq.list_entries(SID) == [])


def test_startup_recovery_resumes_orphans_without_rerunning_started_ones(fqenv):
    # A: claimed, process died before the start -> must run.  B: its user turn was already persisted
    # (start_session_turn saves it before complete()) -> must be completed, not run again.
    fq.enqueue(SID, "never started", client_id="a")
    fq.enqueue(SID, "already ran", client_id="b")
    items = json.loads(fq._path(SID).read_text())
    for it in items["items"]:
        it["status"], it["claimed_at"] = "processing", time.time()  # fresh: not "stale"
    fq._path(SID).write_text(json.dumps(items))
    fqenv.session.messages = [{"role": "user", "content": "already ran", "_ts": time.time()}]
    fq.recover_on_startup(delay=0)
    assert _wait(lambda: fqenv.started == ["never started"], 4)
    time.sleep(0.3)
    assert fqenv.started == ["never started"]            # B was not re-run
    assert "already ran" not in [e["text"] for e in fq.list_entries(SID)]


def test_startup_recovery_is_wired_in_server():
    src = Path(__file__).parent.parent.joinpath("server.py").read_text(encoding="utf-8")
    assert "recover_on_startup" in src
    assert src.index("resume_gateway_runs_after_restart()") < src.index("recover_on_startup")


def test_session_delete_discards_queue_only_after_the_delete_is_durable():
    src = Path(__file__).parent.parent.joinpath("api", "routes.py").read_text(encoding="utf-8")
    i = src.index("_discard_followups(sid)")
    # after every refusal/early-return of the delete flow, i.e. next to the "delete is durable" cleanup
    assert src.rindex("share_revoke_failed", 0, i) < i
    assert src.rindex("state_db_cleanup_failed", 0, i) < i
    assert "Runtime cleanup only after the delete is durable" in src[i - 900:i]
