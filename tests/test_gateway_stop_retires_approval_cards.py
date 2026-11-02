"""Stop / server settle retires a default-stream Gateway run's approval cards.

On the DEFAULT Gateway path (``/v1/chat/completions``, Runs API off) a parked
approval reaches WebUI as an ``approval.request`` frame on the still-open
completion stream; ``_run_gateway_chat_streaming`` mirrors it into the approval
queue and ``/api/approval/respond`` relays a click to
``POST /v1/runs/{run_id}/approval``.

``cancel_stream`` (the user's Stop and the #7293 restart/shutdown settle in
``run_drain.settle_webui_owned_runs``) saves the interrupted turn and returns
while the stream worker is still blocked reading the Gateway socket. Before
this fix the run's card stayed actionable until that worker reached its own
teardown: an Allow click in that window was relayed, answered 200, and the
Agent ran the command after WebUI had recorded the run as stopped.

Contract pinned here:

* stop-then-click: the click gets 409 ``gateway_run_unavailable`` and the
  Gateway sees ZERO approval POSTs; the mirror is gone as soon as cancel
  returns and the session is saved interrupted with its partial;
* click-then-stop: a relay whose POST is already in flight is NOT waited for
  by the stop (no approval lock on the cancel path); it posts exactly once,
  and the stop still leaves no actionable card behind;
* racing clicks and stops: a click that starts after the stop returned never
  reaches the Gateway (the residual window is a click that validated its card
  in the instant before the stop retired it, i.e. effectively before Stop);
* a card the worker parses while the stop is landing is never published;
* local (non-Gateway) approvals are untouched by a stop.

Everything runs the REAL worker, the REAL ``cancel_stream`` /
``settle_webui_owned_runs`` and the REAL ``/api/approval/respond`` handler;
only the Gateway wire (the completion stream and the approval POST) is faked.
Threading seams use ``threading.Event``; their timeouts are failure guards,
never synchronization delays. The only sleeps are the race test's start
offsets, which spread the orderings and never decide a verdict.
"""

from __future__ import annotations

import io
import json
import queue
import threading
import time
import urllib.request
from collections import OrderedDict
from typing import Callable
from unittest.mock import MagicMock

import pytest

import api.config as config
import api.gateway_chat as gateway_chat
import api.models as models
import api.route_approvals as approvals
import api.routes as routes
import api.run_drain as run_drain
import api.streaming as streaming
from api.models import new_session

PROMPT = "Clean up the temp directory."
PARTIAL = "partial before approval"
RUN_ID = "run-stop-retires-cards"
BASE_URL = "http://gw.stop-retires.test"
WAIT = 10.0  # failure guard for every wait, never a synchronization delay

CANONICAL = {
    # A generation-fenced Gateway: exact prompt id + execution generation.
    "event": "approval.request",
    "run_id": RUN_ID,
    "request_id": "prompt-stop-retires",
    "execution_generation": 3,
    "tool": "terminal",
    "command": "rm -rf /tmp/stop-retires",
    "description": "delete temp dir",
    "choices": ["once", "session", "always", "deny"],
}
LEGACY = {
    # A legacy Gateway: older approval_id name, no execution generation.
    "event": "approval.request",
    "run_id": RUN_ID,
    "approval_id": "appr-stop-retires-legacy",
    "tool": "terminal",
    "command": "rm -rf /tmp/stop-retires",
    "description": "delete temp dir",
}
GATEWAYS = pytest.mark.parametrize("payload", [CANONICAL, LEGACY], ids=["fenced", "legacy"])


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture
def gateway_env(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", BASE_URL)
    monkeypatch.setattr(gateway_chat, "_gateway_endpoint_for_profile", lambda profile: (BASE_URL, ""))
    monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *a, **k: None)
    monkeypatch.setattr(gateway_chat, "gateway_approval_unavailable_reason", lambda *a, **k: None)
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {
        "status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": [],
    })
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    return session_dir


@pytest.fixture(autouse=True)
def _clean_registries():
    run_drain._reset_for_tests()
    with config.STREAMS_LOCK:
        old_streams = dict(config.STREAMS)
        config.STREAMS.clear()
    with config.ACTIVE_RUNS_LOCK:
        old_runs = dict(config.ACTIVE_RUNS)
        config.ACTIVE_RUNS.clear()
    old_reasons = dict(streaming._SERVER_SETTLE_REASONS)
    streaming._SERVER_SETTLE_REASONS.clear()
    with approvals._lock:
        approvals._pending.clear()
        approvals._gateway_queues.clear()
    try:
        yield
    finally:
        run_drain._reset_for_tests()
        streaming._SERVER_SETTLE_REASONS.clear()
        streaming._SERVER_SETTLE_REASONS.update(old_reasons)
        with approvals._lock:
            approvals._pending.clear()
            approvals._gateway_queues.clear()
        with config.STREAMS_LOCK:
            config.STREAMS.clear()
            config.STREAMS.update(old_streams)
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
            config.ACTIVE_RUNS.update(old_runs)


# ---------------------------------------------------------------------------
# The fake Gateway wire
# ---------------------------------------------------------------------------


class _ParkedCompletionStream:
    """A ``/v1/chat/completions`` SSE body that parks after its approval frame.

    Like a real Gateway holding the connection open while the prompt waits,
    the read blocks after the approval frame until ``release`` is set: that
    is the window in which the stream worker cannot reach its teardown.
    """

    def __init__(self, payload, gateway):
        self._gateway = gateway
        self._lines = [
            ("data: " + json.dumps({"choices": [{"delta": {"content": PARTIAL}}]})).encode(),
            b"",
            b"event: approval.request",
            ("data: " + json.dumps(payload)).encode(),
        ]

    def __iter__(self):
        for line in self._lines:
            yield line
        # The worker has handled the approval frame and is blocked on the next read.
        self._gateway.parked.set()
        assert self._gateway.release.wait(WAIT), "the fake Gateway stream was never released"
        yield b""
        yield b"data: [DONE]"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class _FakeGateway:
    """Records every approval POST WebUI relays; the hooks shape its timing."""

    def __init__(self, monkeypatch, payload):
        self.payload = payload
        self.parked = threading.Event()
        self.release = threading.Event()
        self.posts: list[dict] = []
        self.completed_posts = 0
        self.log: list[str] = []
        self.log_lock = threading.Lock()
        self.on_post: Callable[[], object] | None = None  # runs inside the POST, before it is answered

        def fake_urlopen(req, *args, **kwargs):
            assert req.full_url == f"{BASE_URL}/v1/chat/completions", req.full_url
            return _ParkedCompletionStream(self.payload, self)

        def fake_request_json(_client, req, **_kwargs):
            assert req.method == "POST", (req.method, req.full_url)
            assert req.full_url.endswith(f"/v1/runs/{RUN_ID}/approval"), req.full_url
            self.note("post_begin")
            self.posts.append(json.loads(req.data))
            if self.on_post is not None:
                self.on_post()
            self.completed_posts += 1
            self.note("post_end")
            return {"ok": True}

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr("api.runner_client.HttpRunnerClient._request_json", fake_request_json)

    def note(self, what):
        with self.log_lock:
            self.log.append(what)


class _DefaultStreamTurn:
    """A real default-stream turn: route-side registration, then the real worker."""

    def __init__(self, gateway, stream_id):
        s = new_session()
        s.active_stream_id = stream_id
        s.pending_user_message = PROMPT
        s.pending_attachments = []
        s.pending_started_at = time.time()
        s.pending_user_source = "webui"
        s.save()
        self.session_id = s.session_id
        self.stream_id = stream_id
        self.gateway = gateway
        self.events: queue.Queue = queue.Queue()
        # What /api/chat/start publishes before it starts the worker.
        with config.STREAMS_LOCK:
            config.STREAMS[stream_id] = self.events
        config.register_stream_owner(stream_id, self.session_id)
        config.register_session_writeback_owner(self.session_id, stream_id)
        gateway_chat._mark_gateway_run_starting(stream_id)
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._work, name=f"gw-default-{stream_id}", daemon=True)
        self.card: dict | None = None

    def _work(self):
        try:
            gateway_chat._run_gateway_chat_streaming(
                self.session_id, PROMPT, "test-model", "/tmp", self.stream_id, [],
            )
        except BaseException as exc:  # surfaced by finish()
            self.error = exc

    def start(self) -> dict:
        self.thread.start()
        deadline = time.monotonic() + WAIT
        while self.card is None:
            event = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            assert event[0] != "apperror", event
            if event[0] == "approval":
                self.card = event[1]
        assert self.gateway.parked.wait(WAIT), "the Gateway stream never parked on the approval"
        assert self.card["run_id"] == RUN_ID
        assert self.card.get("_gateway_mirror_token"), self.card
        assert approvals.gateway_pending_mirror(self.session_id, run_id=RUN_ID) is not None
        return self.card

    def finish(self):
        self.gateway.release.set()
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "the Gateway stream worker did not finish"
        assert self.error is None, repr(self.error)

    def click(self, choice="once", card=None):
        card = card or self.card
        handler = MagicMock()
        handler.wfile = io.BytesIO()
        routes._handle_approval_respond(handler, {
            "session_id": self.session_id,
            "choice": choice,
            "approval_id": card["approval_id"],
            "run_id": card["run_id"],
            "mirror_token": card["_gateway_mirror_token"],
        })
        return handler.send_response.call_args[0][0], json.loads(handler.wfile.getvalue())

    def disk(self) -> dict:
        return json.loads((models.SESSION_DIR / f"{self.session_id}.json").read_text())


def _assert_saved_interrupted(turn, *, server: bool):
    saved = turn.disk()
    assert saved["active_stream_id"] is None
    assert saved["pending_user_message"] is None
    markers = [m for m in saved["messages"] if m.get("role") == "assistant" and m.get("_error")]
    assert markers, saved["messages"]
    if server:
        assert any(m.get("cancel_initiator") == "server" for m in markers), markers
    else:
        assert not any(m.get("cancel_initiator") == "server" for m in markers), markers
    assert any(PARTIAL in str(m.get("content") or "") for m in saved["messages"]), saved["messages"]


def _assert_click_refused(turn, gateway):
    status, answer = turn.click()
    assert status == 409, (status, answer)
    assert answer["ok"] is False and answer["code"] == "gateway_run_unavailable", answer
    assert gateway.posts == [], "a stopped run's card must never reach the Gateway"


def _wait_until(predicate, what):
    deadline = time.monotonic() + WAIT
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.001)


# ---------------------------------------------------------------------------
# (a) user Stop, then click
# ---------------------------------------------------------------------------


@GATEWAYS
def test_user_stop_retires_card_before_returning(gateway_env, monkeypatch, payload):
    gateway = _FakeGateway(monkeypatch, payload)
    turn = _DefaultStreamTurn(gateway, "stream-stop-then-click")
    turn.start()
    try:
        assert streaming.cancel_stream(turn.stream_id) is True
        # The worker is still parked on the Gateway socket; the stop alone retired the card.
        assert turn.thread.is_alive()
        assert approvals.gateway_pending_mirror(turn.session_id) is None
        assert approvals.gateway_pending_mirrors(turn.session_id) == []
        _assert_saved_interrupted(turn, server=False)
        _assert_click_refused(turn, gateway)
    finally:
        turn.finish()
    _assert_click_refused(turn, gateway)
    _assert_saved_interrupted(turn, server=False)


# ---------------------------------------------------------------------------
# (b) server settle (shutdown / restart), then click
# ---------------------------------------------------------------------------


@GATEWAYS
@pytest.mark.parametrize("reason", [run_drain.SHUTDOWN_SETTLE_REASON, run_drain.RESTART_SETTLE_REASON],
                         ids=["shutdown", "restart"])
def test_server_settle_retires_card_before_returning(gateway_env, monkeypatch, payload, reason):
    gateway = _FakeGateway(monkeypatch, payload)
    turn = _DefaultStreamTurn(gateway, "stream-settle-then-click")
    turn.start()
    try:
        assert any(r["stream_id"] == turn.stream_id for r in run_drain.webui_owned_active_runs())
        run_drain.begin_shutdown_drain()
        result = run_drain.settle_webui_owned_runs(reason)
        assert turn.stream_id in result["settled"] and not result["failed"], result
        assert turn.thread.is_alive()
        assert approvals.gateway_pending_mirror(turn.session_id) is None
        _assert_saved_interrupted(turn, server=True)
        _assert_click_refused(turn, gateway)
    finally:
        turn.finish()
    _assert_click_refused(turn, gateway)
    _assert_saved_interrupted(turn, server=True)


# ---------------------------------------------------------------------------
# (c) click in flight, then Stop: the stop does not wait for the relay
# ---------------------------------------------------------------------------


@GATEWAYS
def test_stop_does_not_wait_for_an_in_flight_relay(gateway_env, monkeypatch, payload):
    gateway = _FakeGateway(monkeypatch, payload)
    turn = _DefaultStreamTurn(gateway, "stream-click-then-stop")
    turn.start()
    post_entered = threading.Event()
    post_release = threading.Event()

    def held_post():
        post_entered.set()
        assert post_release.wait(WAIT), "the relay POST was never released"

    gateway.on_post = held_post
    clicked: dict = {}
    stopped: dict = {}
    stop_returned = threading.Event()

    def click():
        clicked["result"] = turn.click()

    def stop():
        stopped["cancelled"] = streaming.cancel_stream(turn.stream_id)
        # What the stop left behind at the instant it reported the run settled.
        stopped["mirrors"] = approvals.gateway_pending_mirrors(turn.session_id)
        stopped["completed_posts"] = gateway.completed_posts
        stop_returned.set()

    click_thread = threading.Thread(target=click, daemon=True)
    stop_thread = threading.Thread(target=stop, daemon=True)
    try:
        click_thread.start()
        assert post_entered.wait(WAIT), "the click never reached the Gateway"
        stop_thread.start()
        # The relay POST is still blocked: the stop must return without it.
        assert stop_returned.wait(WAIT), "the stop waited for an in-flight approval relay"
        assert not post_release.is_set() and click_thread.is_alive()
        assert stopped["cancelled"] is True
        assert stopped["completed_posts"] == 0
        assert stopped["mirrors"] == [], "the stop returned with the run's card still actionable"
        assert approvals.gateway_pending_mirror(turn.session_id) is None
        _assert_saved_interrupted(turn, server=False)
        post_release.set()
        click_thread.join(WAIT)
        stop_thread.join(WAIT)
        assert not click_thread.is_alive() and not stop_thread.is_alive()
    finally:
        post_release.set()
        turn.finish()

    # The click that validated its card before the stop relays exactly once.
    status, answer = clicked["result"]
    assert status == 200 and answer["ok"] is True and answer["relayed"] is True, (status, answer)
    assert len(gateway.posts) == 1 and gateway.completed_posts == 1
    assert approvals.gateway_pending_mirror(turn.session_id) is None
    _assert_saved_interrupted(turn, server=False)
    # A second click on the same (retired) card stays local.
    status, answer = turn.click()
    assert status == 409 and answer["code"] == "gateway_run_unavailable", (status, answer)
    assert len(gateway.posts) == 1


# ---------------------------------------------------------------------------
# (d) click and Stop race: no POST from a click that started after the stop
# ---------------------------------------------------------------------------

# (click_delay, stop_delay) after the shared barrier, spanning both sides of
# the stop's return: a delayed stop lets the click relay first, a delayed click
# lands during or after the stop. ``None`` = the click starts only once the
# stop has returned, so every run has a deterministic stop-then-click attempt.
_RACE_DELAYS = (
    (0.0, 0.005), (0.0, 0.002), (0.0, 0.0005),
    (0.0, 0.0), (0.0, 0.0),
    (0.0005, 0.0), (0.001, 0.0), (0.002, 0.0), (0.005, 0.0), (0.013, 0.0), (0.034, 0.0),
    (None, 0.0),
)


def _race_click_and_stop(gateway, stream_id, click_delay, stop_delay) -> str:
    """One click/stop race on a fresh turn; returns the ordering it observed."""
    gateway.parked.clear()
    gateway.release.clear()
    gateway.posts.clear()
    gateway.completed_posts = 0
    gateway.on_post = None
    with gateway.log_lock:
        gateway.log.clear()
    turn = _DefaultStreamTurn(gateway, stream_id)
    turn.start()
    go = threading.Barrier(2)
    stop_returned = threading.Event()
    clicked: dict = {}

    def click():
        go.wait(WAIT)
        if click_delay is None:
            assert stop_returned.wait(WAIT)
        elif click_delay:
            time.sleep(click_delay)
        gateway.note("click_enter")
        clicked["result"] = turn.click()

    def stop():
        go.wait(WAIT)
        if stop_delay:
            time.sleep(stop_delay)
        assert streaming.cancel_stream(turn.stream_id) is True
        gateway.note("stop_returned")
        stop_returned.set()

    threads = [threading.Thread(target=click, daemon=True), threading.Thread(target=stop, daemon=True)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT)
            assert not thread.is_alive()
    finally:
        turn.finish()

    with gateway.log_lock:
        log = list(gateway.log)
    status, answer = clicked["result"]
    # Noting before the handler / after cancel returns can only classify a
    # late click as early (weakening, never falsely failing, the check).
    click_after_stop = log.index("click_enter") > log.index("stop_returned")
    if click_after_stop:
        assert "post_begin" not in log, f"a click that started after the stop reached the Gateway: {log}"
        assert (status, answer["code"]) == (409, "gateway_run_unavailable"), (status, answer)
    elif status == 200:
        assert answer["relayed"] is True and len(gateway.posts) == 1, (answer, log)
    else:
        assert (status, answer["code"]) == (409, "gateway_run_unavailable"), (status, answer)
        assert gateway.posts == [], log
    assert len(gateway.posts) <= 1, log
    assert approvals.gateway_pending_mirror(turn.session_id) is None
    _assert_saved_interrupted(turn, server=False)
    # Whatever the ordering, the retired card is now refused locally.
    posts_before = len(gateway.posts)
    again, again_answer = turn.click()
    assert (again, again_answer["code"]) == (409, "gateway_run_unavailable"), (again, again_answer)
    assert len(gateway.posts) == posts_before
    if click_after_stop:
        return "stop-then-click"
    return "click-then-stop" if status == 200 else "click-during-stop"


@GATEWAYS
def test_click_started_after_stop_never_reaches_gateway(gateway_env, monkeypatch, payload):
    gateway = _FakeGateway(monkeypatch, payload)
    outcomes = [
        _race_click_and_stop(gateway, f"stream-race-{attempt}", click_delay, stop_delay)
        for attempt, (click_delay, stop_delay) in enumerate(_RACE_DELAYS)
    ]
    assert "stop-then-click" in outcomes
    print("race outcomes:", {o: outcomes.count(o) for o in sorted(set(outcomes))})


# ---------------------------------------------------------------------------
# (f) a card parsed while the stop lands is never published
# ---------------------------------------------------------------------------


@GATEWAYS
def test_card_parsed_while_stop_lands_is_not_published(gateway_env, monkeypatch, payload):
    gateway = _FakeGateway(monkeypatch, payload)
    turn = _DefaultStreamTurn(gateway, "stream-late-card")
    real_translate = gateway_chat._gateway_runs_approval_event
    stopped = threading.Event()

    def translate_then_stop(raw):
        # The worker passed its per-line cancel check and is translating the
        # approval frame when the user's Stop lands (on this same thread, so
        # the interleaving is exact).
        translated = real_translate(raw)
        assert streaming.cancel_stream(turn.stream_id) is True
        stopped.set()
        return translated

    monkeypatch.setattr(gateway_chat, "_gateway_runs_approval_event", translate_then_stop)
    turn.thread.start()
    try:
        assert stopped.wait(WAIT) and gateway.parked.wait(WAIT)
        assert approvals.gateway_pending_mirror(turn.session_id) is None, (
            "a card published after the stop stays actionable for a stopped run"
        )
        card = {
            "approval_id": payload.get("request_id") or payload.get("approval_id"),
            "run_id": RUN_ID,
            "_gateway_mirror_token": "any",
        }
        status, answer = turn.click(card=card)
        assert status == 409 and answer["code"] == "gateway_run_unavailable", (status, answer)
        # Without the exact mirror token too: still nothing reaches the Gateway.
        for body in ({"approval_id": card["approval_id"]}, {"approval_id": card["approval_id"], "run_id": RUN_ID}):
            handler = MagicMock()
            handler.wfile = io.BytesIO()
            routes._handle_approval_respond(handler, {"session_id": turn.session_id, "choice": "once", **body})
        assert gateway.posts == []
        _assert_saved_interrupted(turn, server=False)
    finally:
        turn.finish()


@GATEWAYS
def test_card_cannot_be_published_after_stop_returned(gateway_env, monkeypatch, payload):
    # The worker passed its pre-publish cancel check, then Stop ran to
    # completion (its settle found nothing to retire) before the worker inserted
    # the card. The insertion itself must see the cancel flag under the
    # registry lock, so no card for the stopped run ever becomes clickable,
    # not even for the moment before the worker's post-publish recheck.
    # (Codex release gate #8634, reproduced natively with the installed Agent.)
    gateway = _FakeGateway(monkeypatch, payload)
    turn = _DefaultStreamTurn(gateway, "stream-publish-after-stop")
    before_publish = threading.Event()
    publish_allowed = threading.Event()
    after_publish = threading.Event()
    worker_allowed = threading.Event()
    real_submit = approvals.submit_gateway_pending_mirror

    def delayed_submit(*args, **kwargs):
        before_publish.set()
        assert publish_allowed.wait(WAIT)
        answer = real_submit(*args, **kwargs)
        after_publish.set()
        # Hold the worker before its own post-publish recheck.
        assert worker_allowed.wait(WAIT)
        return answer

    monkeypatch.setattr(approvals, "submit_gateway_pending_mirror", delayed_submit)
    turn.thread.start()
    try:
        assert before_publish.wait(WAIT)
        assert streaming.cancel_stream(turn.stream_id) is True
        assert approvals.gateway_pending_mirror(turn.session_id) is None
        publish_allowed.set()
        assert after_publish.wait(WAIT)
        assert approvals.gateway_pending_mirror(turn.session_id) is None, (
            "the worker published a card after Stop had returned"
        )
        card = {
            "approval_id": payload.get("request_id") or payload.get("approval_id"),
            "run_id": RUN_ID,
            "_gateway_mirror_token": "any",
        }
        status, answer = turn.click(card=card)
        assert status == 409 and answer["code"] == "gateway_run_unavailable", (status, answer)
        assert gateway.posts == []
    finally:
        publish_allowed.set()
        worker_allowed.set()
        turn.finish()


# ---------------------------------------------------------------------------
# (e) local (non-Gateway) approvals are unaffected
# ---------------------------------------------------------------------------


class _LocalProducer:
    """The shape of an in-process approval parked in ``_gateway_queues``."""

    def __init__(self, data):
        self.data = data
        self.event = threading.Event()
        self.result = None
        self.reason = None


def test_local_stream_stop_leaves_local_approvals_alone(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    retirements = []
    monkeypatch.setattr(
        gateway_chat, "settle_gateway_stream_approvals",
        lambda *a, **k: retirements.append((a, k)), raising=False,
    )
    posts = []
    monkeypatch.setattr(
        "api.runner_client.HttpRunnerClient._request_json",
        lambda _client, req, **_kw: posts.append(req.full_url) or {"ok": True},
    )

    s = new_session()
    stream_id = "stream-local-agent"
    s.active_stream_id = stream_id
    s.pending_user_message = PROMPT
    s.pending_started_at = time.time()
    s.save()
    sid = s.session_id
    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = queue.Queue()
    config.register_stream_owner(stream_id, sid)
    config.register_active_run(stream_id, session_id=sid, started_at=time.time(), backend="legacy")
    # A queued local approval card plus a parked in-process producer with its mirror.
    approvals.submit_pending(sid, {"command": "ls /", "pattern_key": "ls", "approval_id": "local-card"})
    producer = _LocalProducer({"command": "rm -rf /tmp/local", "request_id": "local-producer"})
    with approvals._lock:
        approvals._gateway_queues[sid] = [producer]
    head, total = approvals.submit_gateway_pending_mirror(sid, dict(producer.data))
    assert total == 2

    assert streaming.cancel_stream(stream_id) is True
    assert retirements == [], "a local stream's stop must not run the Gateway card retirement"
    assert not producer.event.is_set(), "a stop must not settle a local producer"
    with approvals._lock:
        pending_ids = [e.get("approval_id") for e in approvals._pending.get(sid) or []]
    assert "local-card" in pending_ids and len(pending_ids) == 2, pending_ids

    handler = MagicMock()
    handler.wfile = io.BytesIO()
    routes._handle_approval_respond(handler, {"session_id": sid, "choice": "once", "approval_id": "local-card"})
    assert handler.send_response.call_args[0][0] == 200
    assert json.loads(handler.wfile.getvalue())["ok"] is True
    assert posts == []
    with config.STREAMS_LOCK:
        config.STREAMS.pop(stream_id, None)
    config.unregister_active_run(stream_id)
    config.unregister_stream_owner(stream_id)
