"""Gateway Runs API clarify relay: clarify.request / clarify.settled and
/api/clarify/respond forwarding to POST /v1/runs/{run_id}/clarify."""
from __future__ import annotations

import io
import json
import threading
import urllib.error
from unittest.mock import patch

import pytest

import api.clarify as clarify
from api import gateway_chat
from api.runner_client import HttpRunnerClient, RunnerClientError


SID = "sess-gw-clarify"
RUN = "run-clar-1"


@pytest.fixture(autouse=True)
def _clean_clarify_state():
    def reset():
        with clarify._lock:
            for key in [k for k in clarify._gateway_queues if k.startswith("sess-gw-clarify")]:
                clarify._gateway_queues.pop(key, None)
                clarify._pending.pop(key, None)
            for key in [k for k in clarify._clarify_sse_subscribers if k.startswith("sess-gw-clarify")]:
                clarify._clarify_sse_subscribers.pop(key, None)
    reset()
    yield
    reset()


class _Sse:
    def __init__(self, frames):
        self._lines = []
        for frame in frames:
            if "event" in frame and frame.get("_sse_event"):
                self._lines.append(f"event: {frame.pop('_sse_event')}\n".encode())
            self._lines.append(f"data: {json.dumps(frame)}\n".encode())
            self._lines.append(b"\n")

    def __iter__(self):
        return iter(self._lines)


def _request(prompt_id="clar-a", generation=3, **extra):
    return {
        "event": "clarify.request",
        "kind": "clarify",
        "prompt_id": prompt_id,
        "execution_generation": generation,
        "question": extra.pop("question", "Which one?"),
        "choices": extra.pop("choices", ["red", "blue"]),
        "multi_select": extra.pop("multi_select", False),
        **extra,
    }


def _relay(frames, events, session_id=SID, run_id=RUN):
    return gateway_chat._relay_gateway_run_events(
        _Sse(frames), session_id, "stream-clar", run_id, "http://gw:8642", "k",
        put_gateway_event=lambda ev, data: events.append((ev, data)),
        cancel_event=threading.Event(),
    )


def _subscribe(session_id=SID):
    return clarify.sse_subscribe(session_id)


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ── relay ────────────────────────────────────────────────────────────────


def test_clarify_request_relayed_into_pending_and_sse():
    q = _subscribe()
    events = []
    _relay([_request(timeout_s=90)], events)

    assert [e for e, _ in events] == ["clarify"]
    data = events[0][1]
    assert data["question"] == "Which one?"
    assert data["choices_offered"] == ["red", "blue"]
    assert data["multi_select"] is False
    assert data["timeout_seconds"] == 90
    assert data["pending_count"] == 1
    assert data["clarify_id"] == clarify.gateway_run_clarify_id(RUN, "clar-a", 3)

    pending = clarify.get_pending(SID)
    assert pending["_gateway_run_id"] == RUN
    assert pending["_gateway_prompt_id"] == "clar-a"
    assert pending["_gateway_execution_generation"] == 3
    pushed = _drain(q)
    assert pushed and pushed[-1]["pending"]["clarify_id"] == data["clarify_id"]


def test_hermes_prefixed_and_sse_event_named_clarify_frames_are_relayed():
    events = []
    frame = _request(prompt_id="clar-b")
    frame.pop("event")
    frame["type"] = "hermes.clarify.request"
    _relay([frame], events)
    assert [e for e, _ in events] == ["clarify"]


def test_clarify_without_timeout_has_no_countdown():
    events = []
    _relay([_request()], events)
    assert events[0][1]["timeout_seconds"] == 0
    assert events[0][1]["expires_at"] == 0


def test_malformed_clarify_request_is_dropped():
    events = []
    _relay([_request(generation="3"), {**_request(), "prompt_id": ""}], events)
    assert events == []
    assert clarify.get_pending(SID) is None


def test_replayed_clarify_request_is_not_duplicated():
    events = []
    _relay([_request(), _request()], events)
    assert clarify.pending_count(SID) == 1
    assert events[-1][1]["pending_count"] == 1


def test_clarify_settled_removes_prompt_and_notifies():
    events = []
    _relay([_request()], events)
    q = _subscribe()
    _relay([{"event": "clarify.settled", "prompt_id": "clar-a", "execution_generation": 3}], events)
    assert clarify.get_pending(SID) is None
    assert _drain(q)[-1] == {"pending": None, "pending_count": 0}


def test_sequential_clarify_prompts_queue_and_settle_in_order():
    events = []
    _relay([_request("c1", question="Q1"), _request("c2", question="Q2")], events)
    assert clarify.pending_count(SID) == 2
    assert clarify.get_pending(SID)["question"] == "Q1"
    _relay([{"event": "clarify.settled", "prompt_id": "c1", "execution_generation": 3}], events)
    assert clarify.pending_count(SID) == 1
    assert clarify.get_pending(SID)["question"] == "Q2"


def test_terminal_run_end_clears_run_clarify_but_not_local_clarify():
    local = clarify.submit_pending(SID, {"question": "local?", "choices_offered": []})
    events = []
    _relay([_request(), {"event": "run.completed", "output": "done"}], events)
    remaining = clarify.get_pending(SID)
    assert clarify.pending_count(SID) == 1
    assert remaining["clarify_id"] == local.clarify_id
    assert not local.event.is_set()


@pytest.mark.parametrize("terminal", ["run.failed", "run.cancelled"])
def test_failed_or_cancelled_run_clears_clarify(terminal):
    events = []
    frames = [_request(), {"event": terminal, "error": "boom"}]
    if terminal == "run.failed":
        with pytest.raises(RuntimeError):
            _relay(frames, events)
    else:
        _relay(frames, events)
    assert clarify.get_pending(SID) is None


def test_stream_teardown_settle_clears_run_clarify():
    events = []
    _relay([_request()], events)
    gateway_chat._STREAM_RUN_IDS["stream-teardown"] = RUN
    try:
        gateway_chat.settle_gateway_stream_approvals(SID, "stream-teardown", reason="teardown")
    finally:
        gateway_chat._STREAM_RUN_IDS.pop("stream-teardown", None)
    assert clarify.get_pending(SID) is None


def test_clarify_event_is_recorded_in_run_journal(tmp_path):
    from api.run_journal import RunJournalWriter, read_run_events

    writer = RunJournalWriter(SID, "stream-journal", session_dir=tmp_path)
    gateway_chat._relay_gateway_run_events(
        _Sse([{**_request(), "seq": 7}]), SID, "stream-journal", RUN, "http://gw", "k",
        put_gateway_event=lambda ev, data: writer.append_sse_event(ev, data),
        cancel_event=threading.Event(),
    )
    rows = read_run_events(SID, "stream-journal", session_dir=tmp_path)["events"]
    clar = [r for r in rows if r.get("event") == "clarify"]
    assert clar and clar[0]["payload"]["gateway_seq"] == 7
    assert clar[0]["payload"]["question"] == "Which one?"


# ── runner client ────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class _Opener:
    def __init__(self, fn):
        self.fn = fn

    def open(self, req, timeout=0):
        return self.fn(req)


def test_runner_client_posts_canonical_clarify_body(monkeypatch):
    calls = []

    def fake(req):
        calls.append((req.get_method(), req.full_url, json.loads(req.data)))
        return _FakeResp({"run_id": "r/1", "status": "resolved", "prompt_id": "c1"})

    monkeypatch.setattr(HttpRunnerClient, "_opener", lambda self: _Opener(fake))
    result = HttpRunnerClient(base_url="http://gw").respond_clarify("r/1", "c1", "", execution_generation=4)
    assert result["status"] == "resolved"
    method, url, body = calls[0]
    assert (method, url) == ("POST", "http://gw/v1/runs/r%2F1/clarify")
    assert set(body) == {"request_id", "execution_generation", "answer"}
    assert body == {"request_id": "c1", "execution_generation": 4, "answer": ""}
    assert type(body["execution_generation"]) is int


# ── /api/clarify/respond ─────────────────────────────────────────────────


class _Handler:
    def __init__(self):
        self.status = None
        self.body = None


def _respond(body):
    from api import routes

    captured = {}

    def fake_j(handler, payload, status=200, **_kw):
        captured["status"] = status
        captured["payload"] = payload

    with patch.object(routes, "j", fake_j), patch.object(routes, "bad", lambda h, m, status=400: fake_j(h, {"error": m}, status)):
        routes._handle_clarify_respond(_Handler(), body)
    return captured


def _surface(**kw):
    events = []
    _relay([_request(**kw)], events)
    return events[-1][1]["clarify_id"]


def _gateway_post(outcome):
    """Patch urlopen behind HttpRunnerClient; return list of captured POSTs."""
    calls = []

    def fake(req):
        calls.append((req.full_url, json.loads(req.data)))
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResp(outcome)

    return calls, patch.object(HttpRunnerClient, "_opener", lambda self: _Opener(fake))


def _http_error(code, err_code):
    return urllib.error.HTTPError(
        "http://gw/v1/runs/x/clarify", code, "err", {},
        io.BytesIO(json.dumps({"error": {"code": err_code, "message": err_code}}).encode()),
    )


@pytest.fixture
def _endpoint():
    with patch.object(gateway_chat, "gateway_run_endpoint", lambda run_id: ("http://gw:8642", "k")):
        yield


@pytest.mark.parametrize(
    "answer, choices, multi",
    [("blue", ["red", "blue"], False), ("red, blue", ["red", "blue"], True), ("free text answer", [], False)],
)
def test_respond_forwards_answer_to_gateway(_endpoint, answer, choices, multi):
    cid = _surface(choices=choices, multi_select=multi)
    calls, ctx = _gateway_post({"run_id": RUN, "status": "resolved", "prompt_id": "clar-a"})
    with ctx:
        out = _respond({"session_id": SID, "clarify_id": cid, "response": answer})
    assert out["status"] == 200 and out["payload"]["ok"] is True
    assert calls == [(
        f"http://gw:8642/v1/runs/{RUN}/clarify",
        {"request_id": "clar-a", "execution_generation": 3, "answer": answer},
    )]
    assert clarify.get_pending(SID) is None


def test_respond_empty_answer_is_gateway_skip(_endpoint):
    cid = _surface()
    calls, ctx = _gateway_post({"run_id": RUN, "status": "resolved", "prompt_id": "clar-a"})
    with ctx:
        out = _respond({"session_id": SID, "clarify_id": cid, "response": ""})
    assert out["status"] == 200
    assert calls[0][1]["answer"] == ""


def test_respond_empty_answer_still_rejected_for_local_clarify():
    entry = clarify.submit_pending(SID, {"question": "local?"})
    out = _respond({"session_id": SID, "clarify_id": entry.clarify_id, "response": ""})
    assert out["status"] == 400
    assert clarify.pending_count(SID) == 1


def test_respond_already_resolved_is_stale_and_retires_card(_endpoint):
    cid = _surface()
    _calls, ctx = _gateway_post({"run_id": RUN, "status": "already_resolved", "prompt_id": "clar-a"})
    with ctx:
        out = _respond({"session_id": SID, "clarify_id": cid, "response": "x"})
    assert out["status"] == 409 and out["payload"]["stale"] is True
    assert clarify.get_pending(SID) is None


@pytest.mark.parametrize("code", ["stale_generation", "approval_not_pending", "not_found", "invalid_params"])
def test_respond_dead_prompt_errors_retire_card(_endpoint, code):
    cid = _surface()
    _calls, ctx = _gateway_post(_http_error(409, code))
    with ctx:
        out = _respond({"session_id": SID, "clarify_id": cid, "response": "x"})
    assert out["status"] == 409
    assert clarify.get_pending(SID) is None


def test_respond_transient_error_keeps_card(_endpoint):
    cid = _surface()
    _calls, ctx = _gateway_post(_http_error(503, "storage_unavailable"))
    with ctx:
        out = _respond({"session_id": SID, "clarify_id": cid, "response": "x"})
    # Not stale: respondClarify only hides the card on 409, any other error keeps card + draft.
    assert out["status"] == 503
    assert out["payload"]["ok"] is False and "stale" not in out["payload"]
    assert out["payload"]["retryable"] is True
    assert clarify.get_pending(SID)["clarify_id"] == cid


def test_respond_gateway_unreachable_is_retryable_not_stale(_endpoint):
    cid = _surface()
    _calls, ctx = _gateway_post(urllib.error.URLError("connection refused"))
    with ctx:
        out = _respond({"session_id": SID, "clarify_id": cid, "response": "x"})
    assert out["status"] == 503 and "stale" not in out["payload"]
    assert clarify.get_pending(SID)["clarify_id"] == cid
    # The retry reaches the Gateway with the same identity once it is back.
    calls, ctx = _gateway_post({"run_id": RUN, "status": "resolved", "prompt_id": "clar-a"})
    with ctx:
        out = _respond({"session_id": SID, "clarify_id": cid, "response": "x"})
    assert out["status"] == 200 and calls[0][1]["request_id"] == "clar-a"


def test_respond_missing_or_expired_gateway_prompt_never_posts(_endpoint):
    calls, ctx = _gateway_post({"status": "resolved"})
    with ctx:
        out = _respond({
            "session_id": SID,
            "clarify_id": clarify.gateway_run_clarify_id(RUN, "gone", 3),
            "response": "x",
        })
    assert out["status"] == 409
    assert calls == []


def test_respond_wrong_session_never_posts(_endpoint):
    cid = _surface()
    calls, ctx = _gateway_post({"status": "resolved"})
    with ctx:
        out = _respond({"session_id": SID + "-other", "clarify_id": cid, "response": "x"})
    assert out["status"] == 409
    assert calls == []
    assert clarify.get_pending(SID)["clarify_id"] == cid


def _gateway_post_with_auth(outcome):
    calls = []

    def fake(req):
        calls.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
        return _FakeResp(outcome)

    return calls, patch.object(HttpRunnerClient, "_opener", lambda self: _Opener(fake))


@pytest.fixture
def _other_default_gateway(monkeypatch):
    """The process/default Gateway differs from the one that parked the prompt,
    and no live stream maps the run (stream torn down / other profile / restart)."""
    monkeypatch.setattr(gateway_chat, "_gateway_base_url", lambda *a, **k: "http://default-gw:8642")
    monkeypatch.setattr(gateway_chat, "_gateway_api_key", lambda *a, **k: "default-secret")


def test_respond_delivers_only_to_owning_gateway(_other_default_gateway):
    """Ownership regression: the answer and credential go to the Gateway the
    prompt was relayed from, never to the process-default Gateway."""
    events = []
    gateway_chat._relay_gateway_run_events(
        _Sse([_request()]), SID, "stream-owner", RUN, "http://owner-gw:9000", "owner-secret",
        put_gateway_event=lambda ev, data: events.append((ev, data)),
        cancel_event=threading.Event(),
    )
    card = events[-1][1]
    # Credentials and Gateway URLs stay server-side.
    assert "owner-secret" not in json.dumps(card) and "owner-gw" not in json.dumps(card)
    assert "owner-secret" not in json.dumps(clarify.get_pending(SID))
    calls, ctx = _gateway_post_with_auth({"status": "resolved"})
    with ctx:
        out = _respond({"session_id": SID, "clarify_id": card["clarify_id"], "response": "blue"})
    assert out["status"] == 200
    assert [(url, auth) for url, auth, _b in calls] == [
        (f"http://owner-gw:9000/v1/runs/{RUN}/clarify", "Bearer owner-secret"),
    ]


def test_respond_fails_closed_when_owner_unknown(_other_default_gateway):
    """A Gateway-run mirror whose owning Gateway was never established is never
    answered through a default/shared Gateway or credential."""
    data = gateway_chat._gateway_runs_clarify_event(_request(), RUN)
    assert data is not None
    clarify.submit_gateway_run_pending(SID, data)
    calls, ctx = _gateway_post_with_auth({"status": "resolved"})
    with ctx:
        out = _respond({"session_id": SID, "clarify_id": data["clarify_id"], "response": "blue"})
    assert out["status"] == 409
    assert calls == []


def test_sequential_prompts_answer_each_with_own_identity(_endpoint):
    events = []
    _relay([_request("c1", question="Q1"), _request("c2", generation=3, question="Q2")], events)
    calls, ctx = _gateway_post({"status": "resolved"})
    with ctx:
        first = clarify.get_pending(SID)["clarify_id"]
        assert _respond({"session_id": SID, "clarify_id": first, "response": "a1"})["status"] == 200
        second = clarify.get_pending(SID)["clarify_id"]
        assert second != first
        assert _respond({"session_id": SID, "clarify_id": second, "response": "a2"})["status"] == 200
    assert [c[1]["request_id"] for c in calls] == ["c1", "c2"]
    assert clarify.get_pending(SID) is None


def test_local_clarify_respond_unchanged():
    entry = clarify.submit_pending(SID, {"question": "local?"})
    with patch.object(HttpRunnerClient, "_post", side_effect=AssertionError("no gateway post")):
        out = _respond({"session_id": SID, "clarify_id": entry.clarify_id, "response": "yes"})
    assert out["status"] == 200
    assert entry.event.is_set() and entry.result == "yes"


def test_runner_client_error_carries_code():
    exc = RunnerClientError("x", status=409, code="stale_generation")
    assert exc.code == "stale_generation"


# ── WebUI restart / reattach ─────────────────────────────────────────────


def _reattach(monkeypatch, statuses, events, seen=None):
    """Run the post-restart reattach loop against a scripted status sequence."""
    it = iter(statuses)

    def status(*_a):
        if seen is not None:
            seen.append(clarify.gateway_run_prompt_ids(SID, RUN))
        return next(it)

    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", status)
    monkeypatch.setattr(gateway_chat, "update_active_run", lambda *a, **k: None)
    monkeypatch.setattr(gateway_chat, "GATEWAY_REATTACH_POLL_INTERVAL", 0)
    monkeypatch.setattr("api.route_approvals.settle_gateway_pending_run", lambda *a, **k: None)
    try:
        return gateway_chat._await_gateway_run_result(
            SID, "stream-reattach", RUN, "http://gw", "k",
            put_gateway_event=lambda e, d: events.append((e, d)), cancel_event=threading.Event())
    finally:
        gateway_chat._STREAM_RUN_IDS.pop("stream-reattach", None)
        gateway_chat._STREAM_RUN_LIFECYCLE.pop("stream-reattach", None)


def _parked(prompt_id="clar-a", generation=3):
    control = _request(prompt_id, generation)
    control.pop("event")
    return control


def test_restart_restores_parked_clarify_from_run_status(monkeypatch, _endpoint):
    """The pre-restart clarify row is behind the cursor and the new queue is empty:
    reattach rebuilds the card from pending_controls so the answer can be delivered."""
    assert clarify.get_pending(SID) is None
    events, seen = [], []
    running = {"status": "running", "pending_controls": [_parked()]}
    _reattach(monkeypatch, [running, running, {"status": "completed", "output": "ok"}], events, seen)
    assert seen[1] == {"clar-a"}
    cards = [d for e, d in events if e == "clarify"]
    assert len(cards) == 1  # re-polling the same parked prompt does not duplicate the card
    assert cards[0]["clarify_id"] == clarify.gateway_run_clarify_id(RUN, "clar-a", 3)
    assert cards[0]["question"] == "Which one?"


def test_restart_restored_clarify_is_answerable(_endpoint):
    events = []
    gateway_chat._sync_gateway_run_clarify_from_status(
        SID, RUN, {"status": "running", "pending_controls": [_parked()]}, "http://gw:8642", "k",
        put_gateway_event=lambda e, d: events.append((e, d)))
    cid = events[-1][1]["clarify_id"]
    calls, ctx = _gateway_post({"run_id": RUN, "status": "resolved", "prompt_id": "clar-a"})
    with ctx:
        out = _respond({"session_id": SID, "clarify_id": cid, "response": "blue"})
    assert out["status"] == 200
    assert calls[0][1] == {"request_id": "clar-a", "execution_generation": 3, "answer": "blue"}


def test_restart_does_not_resurrect_settled_clarify(monkeypatch):
    """clarify.settled writes no journal row; only the status says which prompts live."""
    events, seen = [], []
    _relay([_request("clar-old")], [])  # mirrored, then settled while the WebUI was down
    _reattach(monkeypatch, [
        {"status": "running", "pending_controls": [_parked("clar-new")]},
        {"status": "completed", "output": "ok"},
    ], events, seen)
    assert seen[1] == {"clar-new"}
    assert [d["_gateway_prompt_id"] for e, d in events if e == "clarify"] == ["clar-new"]


def test_status_without_pending_controls_keeps_clarify_queue():
    _relay([_request()], [])
    gateway_chat._sync_gateway_run_clarify_from_status(
        SID, RUN, {"status": "running"}, "http://gw:8642", "k", put_gateway_event=lambda *a: None)
    assert clarify.gateway_run_prompt_ids(SID, RUN) == {"clar-a"}


# ── live streaming watchdog (events endpoint down, run still live) ───────

WD_SID = "sess-watchdog-harness"


@pytest.fixture
def _wd_session():
    clarify.clear_pending(WD_SID)
    yield WD_SID
    clarify.clear_pending(WD_SID)


def _watchdog(urlopen_script, statuses, seen):
    """Drive the live streaming watchdog; ``seen`` records queued prompt ids at each probe."""
    from tests import test_gateway_events_watchdog_7978 as wd

    scripted = iter(statuses)

    def status(*_a):
        seen.append(clarify.gateway_run_prompt_ids(WD_SID, wd.RUN_ID))
        return dict(next(scripted))

    return wd.RUN_ID, wd.run_turn(
        urlopen_script=urlopen_script, extra_patches=[(gateway_chat, "_get_gateway_run_status", status)])


def _events_404(n):
    from tests import test_gateway_events_watchdog_7978 as wd

    return [wd._http_error(404) for _ in range(n)]


def test_watchdog_status_restores_live_clarify_before_reconnect(_wd_session):
    """Events 404 + status running with a parked clarify: the card is up while the
    run and native waiter are still live, and re-polling does not duplicate it."""
    seen = []
    running = {"status": "running", "pending_controls": [_parked()]}
    run, h = _watchdog(_events_404(3), [running, running, {"status": "completed", "output": "ok"}], seen)
    assert seen[1] == {"clar-a"}  # mirrored from the first live status, before reconnecting
    cards = [d for e, d in h["events"] if e == "clarify"]
    assert len(cards) == 1
    assert cards[0]["clarify_id"] == clarify.gateway_run_clarify_id(run, "clar-a", 3)
    assert cards[0]["question"] == "Which one?"
    assert clarify.gateway_run_prompt_ids(WD_SID, run) == set()  # terminal cleanup


def test_watchdog_status_retires_settled_prompt_and_keeps_local_card(_wd_session):
    local = clarify.submit_pending(WD_SID, {"question": "local?", "choices_offered": []})
    seen = []
    run, _h = _watchdog(_events_404(4), [
        {"status": "running", "pending_controls": [_parked()]},
        {"status": "running", "pending_controls": []},
        {"status": "running"},  # no pending_controls: proves nothing, changes nothing
        {"status": "completed", "output": "ok"},
    ], seen)
    assert seen[1:] == [{"clar-a"}, set(), set()]
    assert clarify.pending_count(WD_SID) == 1
    assert clarify.get_pending(WD_SID)["clarify_id"] == local.clarify_id
    assert not local.event.is_set()


def test_watchdog_no_pending_status_leaves_local_card_unchanged(_wd_session):
    local = clarify.submit_pending(WD_SID, {"question": "local?", "choices_offered": []})
    seen = []
    _run, h = _watchdog(_events_404(2), [{"status": "running"}, {"status": "completed", "output": "ok"}], seen)
    assert [e for e, _d in h["events"] if e == "clarify"] == []
    assert clarify.get_pending(WD_SID)["clarify_id"] == local.clarify_id


def test_watchdog_ordinary_event_delivery_surfaces_same_prompt(_wd_session):
    """Control: /events delivers the clarify.request; one card with the same identity."""
    from tests import test_gateway_events_watchdog_7978 as wd

    frames = wd.sse_frame(0, _request()) + wd.sse_frame(1, {"event": "run.completed", "output": "ok"})
    seen = []
    run, h = _watchdog([wd.FakeSseResponse(frames)], [], seen)
    cards = [d for e, d in h["events"] if e == "clarify"]
    assert [c["clarify_id"] for c in cards] == [clarify.gateway_run_clarify_id(run, "clar-a", 3)]
    assert seen == []
