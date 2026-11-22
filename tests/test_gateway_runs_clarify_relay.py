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
    assert out["status"] == 409
    assert clarify.get_pending(SID)["clarify_id"] == cid


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
