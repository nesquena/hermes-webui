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

    class Env:
        active = False          # is a turn running for SID
        started = []            # texts handed to start_session_turn, in order
        status = 200            # status start_session_turn returns
        lock = threading.Lock()

    env = Env()
    env.started = []

    import api.background_process as bp
    import api.routes as routes

    monkeypatch.setattr(bp, "_session_has_active_turn", lambda sid: env.active)

    def fake_start(session_id, message, **kw):
        with env.lock:
            assert kw.get("source") == "queued_followup"
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
        assert _wait(lambda: fqenv.started == expected), fqenv.started
        assert _wait(lambda: len(fq.list_entries(SID)) == 3 - len(expected))
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


def test_failed_start_is_reported_and_retryable(fqenv):
    fqenv.status = 500
    fq.enqueue(SID, "bad")
    fq.drain_followup_queue_for_session(SID)
    assert _wait(lambda: fq.list_entries(SID)[0]["status"] == "failed")
    assert fq.list_entries(SID)[0]["error"] == "boom"
    fqenv.status = 200
    assert fq.retry(SID, fq.list_entries(SID)[0]["id"])
    assert fq.drain_followup_queue_for_session(SID) is True
    assert _wait(lambda: fqenv.started == ["bad"])


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
