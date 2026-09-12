"""Direct regression tests for the gateway steer relay helper (#7440).

Covers ``api.gateway_chat.gateway_steer_run`` and its interaction with the
gateway run-id lifecycle (``wait_for_gateway_run_id`` / publication /
retirement). All HTTP is faked at ``urllib.request.urlopen``; the gateway
base URL and API key are pinned to deterministic fake values. No real
gateway, config, credentials, sockets, or sleeps-for-synchronization are
used: cross-thread sequencing observes the real lifecycle waiter count under
the real condition with a bounded deadline. HTTP is faked at the
``urllib.request.build_opener`` seam (the redirect-refusing opener
``gateway_steer_run`` opens through); the redirect row uses a real loopback
HTTP server so the no-follow behavior itself is exercised.
"""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _gateway_run_state_isolation():
    """Snapshot/restore the gateway run-id lifecycle maps around each test.

    Test-specific stream ids keep rows disjoint; this fixture guarantees no
    lifecycle/id state leaks even when an assertion aborts mid-test. Any
    still-blocked waiter is woken by the final ``notify_all`` so a failing
    test cannot strand a thread inside ``wait_for_gateway_run_id``.
    """
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


class _FakeSteerResponse:
    """Context-managed success response that records read/close.

    Models the validated response surface ``gateway_steer_run`` inspects
    after opening: ``geturl()`` (final URL after any redirect handling) and
    ``status``/``code`` (terminal HTTP status).
    """

    def __init__(self, req, *, status: int = 200, final_url: str | None = None):
        self._req = req
        self.status = status
        self.code = status
        self._final_url = final_url
        self.read_called = False
        self.closed = False

    def geturl(self):
        return self._final_url if self._final_url is not None else self._req.full_url

    def read(self, _limit=None):
        self.read_called = True
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        return None


@pytest.fixture()
def steer_relay(monkeypatch):
    """Pin gateway config to fakes and record every opener open attempt.

    ``gateway_steer_run`` opens through ``urllib.request.build_opener``
    (redirect-refusing opener, same as ``stop_gateway_run``), so the fake is
    installed at the ``build_opener`` seam. Returns a namespace with
    ``calls`` (``{"req", "timeout"}`` dicts) and ``responses`` (the fake
    response objects handed out, in order).
    """
    from api import gateway_chat

    monkeypatch.setattr(
        gateway_chat, "_gateway_base_url", lambda *args, **kwargs: "http://gateway.test"
    )
    monkeypatch.setattr(gateway_chat, "_gateway_api_key", lambda *args, **kwargs: "test-key")

    calls: list[dict] = []
    responses: list[_FakeSteerResponse] = []

    def fake_open(req, *, timeout=None):
        resp = _FakeSteerResponse(req)
        calls.append({"req": req, "timeout": timeout})
        responses.append(resp)
        return resp

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers, **kwargs: SimpleNamespace(open=fake_open),
    )
    return SimpleNamespace(calls=calls, responses=responses)


def _waiter_count(stream_id: str) -> int:
    from api import gateway_chat

    with gateway_chat._STREAM_RUN_STARTING_CONDITION:
        state = gateway_chat._STREAM_RUN_LIFECYCLE.get(stream_id) or {}
        return int(state.get("waiters") or 0)


def _wait_for_waiter_count(stream_id: str, expected: int, timeout: float = 5.0) -> None:
    """Poll the real lifecycle waiter count under the real condition.

    This is deadline-bounded observation, not a fixed sleep used as the sole
    synchronization: the waiter increments ``waiters`` while holding
    ``_STREAM_RUN_STARTING_CONDITION``, so each read is authoritative.
    """
    from api import gateway_chat

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with gateway_chat._STREAM_RUN_STARTING_CONDITION:
            state = gateway_chat._STREAM_RUN_LIFECYCLE.get(stream_id) or {}
            if int(state.get("waiters") or 0) >= expected:
                return
        time.sleep(0.005)
    raise AssertionError(f"waiter count for {stream_id!r} never reached {expected}")


def _header(req, name: str):
    lowered = name.lower()
    for key, value in req.headers.items():
        if key.lower() == lowered:
            return value
    return None


def _install_fake_opener(monkeypatch, open_fn):
    """Install ``open_fn`` at the ``build_opener`` seam ``gateway_steer_run``
    opens through (redirect-refusing opener, same as ``stop_gateway_run``)."""
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers, **kwargs: SimpleNamespace(open=open_fn),
    )


def _start_steer_thread(results: dict, key: str, stream_id: str, text: str) -> threading.Thread:
    from api import gateway_chat

    def run():
        try:
            results[key] = gateway_chat.gateway_steer_run(stream_id, text)
        except Exception as exc:  # pragma: no cover - surfaced via results
            results[key] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _stop_lifecycle(stream_id: str) -> None:
    """Wake any waiter and mark the owner done; safe to call repeatedly."""
    from api import gateway_chat

    gateway_chat._finish_gateway_run_starting(stream_id)
    gateway_chat._clear_gateway_run_starting(stream_id)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def test_gateway_steer_success_posts_text(steer_relay):
    from api import gateway_chat

    stream_a = "stream-steer-success-a"
    stream_b = "stream-steer-success-b"
    gateway_chat._STREAM_RUN_IDS[stream_a] = "run-A"
    gateway_chat._STREAM_RUN_IDS[stream_b] = "run-B"

    result = gateway_chat.gateway_steer_run(stream_a, "use Python — please")

    assert result == (True, None)
    assert len(steer_relay.calls) == 1
    call = steer_relay.calls[0]
    req = call["req"]
    assert req.full_url == "http://gateway.test/v1/runs/run-A/steer"
    assert req.get_method() == "POST"
    assert json.loads(req.data.decode("utf-8")) == {"text": "use Python — please"}
    assert _header(req, "Content-Type") == "application/json"
    assert _header(req, "Authorization") == "Bearer test-key"
    assert call["timeout"] == 15
    resp = steer_relay.responses[0]
    assert resp.read_called is True
    assert resp.closed is True


def test_gateway_steer_success_blank_api_key_keeps_current_header(steer_relay, monkeypatch):
    """Pin the existing blank-key behavior: the Authorization header is still
    sent as ``Bearer `` (with empty token). Not a new auth contract."""
    from api import gateway_chat

    monkeypatch.setattr(gateway_chat, "_gateway_api_key", lambda *args, **kwargs: "")
    stream_id = "stream-steer-blank-key"
    gateway_chat._STREAM_RUN_IDS[stream_id] = "run-blank"

    result = gateway_chat.gateway_steer_run(stream_id, "hi")

    assert result == (True, None)
    assert len(steer_relay.calls) == 1
    assert _header(steer_relay.calls[0]["req"], "Authorization") == "Bearer "


def test_gateway_steer_quotes_run_id(steer_relay):
    """The run-id path segment must be URL-quoted exactly like stop does."""
    from api import gateway_chat

    stream_id = "stream-steer-quoting"
    gateway_chat._STREAM_RUN_IDS[stream_id] = "run/a b?c#d"

    result = gateway_chat.gateway_steer_run(stream_id, "hi")

    assert result == (True, None)
    assert len(steer_relay.calls) == 1
    req = steer_relay.calls[0]["req"]
    assert req.full_url == "http://gateway.test/v1/runs/run%2Fa%20b%3Fc%23d/steer"


# ---------------------------------------------------------------------------
# No usable run id
# ---------------------------------------------------------------------------


def test_gateway_steer_no_run_id(steer_relay):
    from api import gateway_chat

    # No lifecycle entry and no mapped id at all.
    assert gateway_chat.gateway_steer_run("stream-steer-no-id", "hi") == (
        False,
        "gateway_steer_no_run_id",
    )
    # Empty stream id, without creating any pending state for it.
    assert gateway_chat.gateway_steer_run("", "hi") == (False, "gateway_steer_no_run_id")
    assert steer_relay.calls == []


# ---------------------------------------------------------------------------
# HTTP status mapping
# ---------------------------------------------------------------------------


def _expected_http_reason(code: int) -> str:
    if code in {404, 405, 410}:
        # Endpoint gone/unsupported: compatibility policy queues for next turn.
        return "gateway_steer_queued"
    if code == 409:
        return "gateway_steer_not_accepting"
    return f"gateway_steer_http_{code}"


_HTTP_STATUS_CASES = [302, *range(400, 600)]


@pytest.mark.parametrize(
    "code",
    _HTTP_STATUS_CASES,
    ids=[f"http_{code}" for code in _HTTP_STATUS_CASES],
)
def test_gateway_steer_http_reason_mapping(steer_relay, monkeypatch, code):
    from api import gateway_chat

    stream_id = f"stream-steer-http-{code}"
    gateway_chat._STREAM_RUN_IDS[stream_id] = "run-http"
    error_streams: list[io.BytesIO] = []
    attempts: list[str] = []

    def fake_urlopen(req, *, timeout=None):
        attempts.append(req.full_url)
        fp = io.BytesIO(b'{"error": "nope"}')
        error_streams.append(fp)
        raise urllib.error.HTTPError(req.full_url, code, f"HTTP {code}", {}, fp)

    _install_fake_opener(monkeypatch, fake_urlopen)
    try:
        result = gateway_chat.gateway_steer_run(stream_id, "steer me")
        assert result == (False, _expected_http_reason(code))
        assert len(attempts) == 1  # exactly one delivery attempt, no retries
    finally:
        for fp in error_streams:
            fp.close()  # test-owned error streams are closed by the test


# ---------------------------------------------------------------------------
# Exception mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_factory",
    [
        pytest.param(lambda: urllib.error.URLError("connection refused"), id="url_error"),
        pytest.param(lambda: TimeoutError("timed out"), id="timeout_error"),
        pytest.param(lambda: OSError("socket broken"), id="os_error"),
        pytest.param(lambda: RuntimeError("unexpected"), id="runtime_error"),
    ],
)
def test_gateway_steer_exception_reason_mapping(steer_relay, monkeypatch, exc_factory):
    from api import gateway_chat

    stream_id = "stream-steer-exc"
    gateway_chat._STREAM_RUN_IDS[stream_id] = "run-exc"

    def fake_urlopen(req, *, timeout=None):
        raise exc_factory()

    _install_fake_opener(monkeypatch, fake_urlopen)
    result = gateway_chat.gateway_steer_run(stream_id, "steer me")
    assert result == (False, "gateway_steer_error")


def test_gateway_steer_response_read_failure_is_error(steer_relay, monkeypatch):
    from api import gateway_chat

    stream_id = "stream-steer-read-fail"
    gateway_chat._STREAM_RUN_IDS[stream_id] = "run-read"

    class _ReadFailResponse:
        def read(self, _limit=None):
            raise OSError("read failed")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    _install_fake_opener(monkeypatch, lambda req, *, timeout=None: _ReadFailResponse())
    result = gateway_chat.gateway_steer_run(stream_id, "steer me")
    assert result == (False, "gateway_steer_error")


def test_gateway_steer_base_url_resolution_failure_is_error(steer_relay, monkeypatch):
    from api import gateway_chat

    def boom(*args, **kwargs):
        raise RuntimeError("no gateway config")

    monkeypatch.setattr(gateway_chat, "_gateway_base_url", boom)
    stream_id = "stream-steer-base-url-fail"
    gateway_chat._STREAM_RUN_IDS[stream_id] = "run-base"

    result = gateway_chat.gateway_steer_run(stream_id, "steer me")
    assert result == (False, "gateway_steer_error")
    assert steer_relay.calls == []  # resolution failed before any HTTP attempt


# ---------------------------------------------------------------------------
# Codex r2 transport findings: config authority, redirect refusal,
# accepted-response validation
# ---------------------------------------------------------------------------


def test_gateway_steer_base_url_uses_config_file_authority(monkeypatch):
    """The steer POST must resolve its base URL from the SAME config
    authority the run was created with: get_config() honors the
    ``webui_gateway_base_url`` config key, not just the env var."""
    from api import config, gateway_chat

    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {"webui_gateway_base_url": "http://config-authority.test"},
    )
    stream_id = "stream-steer-config-authority"
    gateway_chat._STREAM_RUN_IDS[stream_id] = "run-cfg"

    calls: list[dict] = []

    def fake_open(req, *, timeout=None):
        calls.append({"req": req, "timeout": timeout})
        return _FakeSteerResponse(req)

    _install_fake_opener(monkeypatch, fake_open)
    try:
        assert gateway_chat.gateway_steer_run(stream_id, "steer me") == (True, None)
        assert len(calls) == 1
        assert calls[0]["req"].full_url == (
            "http://config-authority.test/v1/runs/run-cfg/steer"
        )
    finally:
        gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)


def test_gateway_steer_redirect_not_followed(monkeypatch):
    """A redirecting steer endpoint must NOT be followed: urllib's default
    opener would follow the 302 to a 200 landing page and misreport the
    undelivered POST as accepted. The real no-redirect opener surfaces the
    3xx as an HTTPError, mapped to the http_<code> family."""
    import http.server
    import socketserver

    from api import gateway_chat

    class _RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path.endswith("/steer"):
                self.send_response(302)
                self.send_header("Location", "/landing")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(
            gateway_chat, "_gateway_base_url", lambda *a, **k: f"http://127.0.0.1:{server.server_address[1]}"
        )
        stream_id = "stream-steer-redirect"
        gateway_chat._STREAM_RUN_IDS[stream_id] = "run-redir"
        assert gateway_chat.gateway_steer_run(stream_id, "steer me") == (
            False,
            "gateway_steer_http_302",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        gateway_chat._STREAM_RUN_IDS.pop("stream-steer-redirect", None)


def test_gateway_steer_rejects_followed_redirect_landing(monkeypatch):
    """If a response somehow arrives from a different final URL (a followed
    redirect landing on a 200 page), delivery is unproven and must NOT be
    reported as accepted."""
    from api import gateway_chat

    stream_id = "stream-steer-followed-landing"
    gateway_chat._STREAM_RUN_IDS[stream_id] = "run-landing"

    def fake_open(req, *, timeout=None):
        return _FakeSteerResponse(req, status=200, final_url="http://gateway.test/login")

    _install_fake_opener(monkeypatch, fake_open)
    try:
        assert gateway_chat.gateway_steer_run(stream_id, "steer me") == (
            False,
            "gateway_steer_error",
        )
    finally:
        gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)


def test_gateway_steer_rejects_unraised_non_2xx_terminal(monkeypatch):
    """A non-2xx terminal response the opener did not raise for (exotic
    handler) is still a delivery failure, never an accepted steer."""
    from api import gateway_chat

    stream_id = "stream-steer-unraised-500"
    gateway_chat._STREAM_RUN_IDS[stream_id] = "run-unraised"

    def fake_open(req, *, timeout=None):
        return _FakeSteerResponse(req, status=500)

    _install_fake_opener(monkeypatch, fake_open)
    try:
        assert gateway_chat.gateway_steer_run(stream_id, "steer me") == (
            False,
            "gateway_steer_error",
        )
    finally:
        gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)


# ---------------------------------------------------------------------------
# Startup-window run-id lifecycle
# ---------------------------------------------------------------------------


def test_gateway_steer_waits_for_delayed_run_id(steer_relay):
    """Pending startup: steer must wait (bounded) for publication, then POST
    exactly once to the published id — never fire an HTTP request first."""
    from api import gateway_chat

    stream_id = "stream-steer-delayed-publish"
    results: dict = {}
    gateway_chat._mark_gateway_run_starting(stream_id)
    thread = _start_steer_thread(results, "steer", stream_id, "hold on")
    try:
        _wait_for_waiter_count(stream_id, 1)
        assert steer_relay.calls == []  # no HTTP before a run id exists
        gateway_chat._publish_gateway_run_id(stream_id, "run-A")
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert results["steer"] == (True, None)
        assert len(steer_relay.calls) == 1
        req = steer_relay.calls[0]["req"]
        assert req.full_url == "http://gateway.test/v1/runs/run-A/steer"
        assert _waiter_count(stream_id) == 0  # waiter released its reference
    finally:
        _stop_lifecycle(stream_id)
        thread.join(timeout=5)


def test_gateway_steer_pending_timeout(steer_relay, monkeypatch):
    """A still-pending lifecycle resolves to no-id within the (shortened)
    wait budget, makes no HTTP call, releases its waiter, and leaves the
    pending phase owned by the worker."""
    from api import gateway_chat

    monkeypatch.setattr(gateway_chat, "GATEWAY_RUN_ID_WAIT_TIMEOUT", 0.05)
    stream_id = "stream-steer-pending-timeout"
    gateway_chat._mark_gateway_run_starting(stream_id)
    try:
        started = time.monotonic()
        result = gateway_chat.gateway_steer_run(stream_id, "hello?")
        elapsed = time.monotonic() - started
        assert result == (False, "gateway_steer_no_run_id")
        assert elapsed < 1.0  # generous bound around the 0.05s budget
        assert steer_relay.calls == []
        assert _waiter_count(stream_id) == 0
        with gateway_chat._STREAM_RUN_STARTING_CONDITION:
            phase = gateway_chat._STREAM_RUN_LIFECYCLE[stream_id]["phase"]
        assert phase == "pending"  # owner has not finished; state not retired
    finally:
        _stop_lifecycle(stream_id)


@pytest.mark.parametrize(
    "mode",
    ["failed", "fallback", "ready_empty_id", "fallback_legacy_map_id"],
)
def test_gateway_steer_terminal_lifecycle_without_id(steer_relay, mode):
    """Terminal/no-id lifecycle states are explicit outcomes with no HTTP
    attempt. Codex r2 (legacy fallback reason): a known ``fallback`` phase is
    the legacy chat-completions transport — live relay is impossible for the
    whole turn, so it degrades to the queue convention instead of no-run-id.
    The fallback-with-legacy-id row pins phase-before-id precedence: even if
    a legacy approval event populates an id, a known ``fallback`` phase must
    not relay."""
    from api import gateway_chat

    expected = "gateway_steer_queued" if "fallback" in mode else "gateway_steer_no_run_id"
    stream_id = f"stream-steer-terminal-{mode}"
    gateway_chat._mark_gateway_run_starting(stream_id)
    if mode == "failed":
        gateway_chat._finish_gateway_run_starting(stream_id)
    elif mode == "fallback":
        gateway_chat._finish_gateway_run_starting(stream_id, result="fallback")
    elif mode == "ready_empty_id":
        gateway_chat._publish_gateway_run_id(stream_id, "")
    elif mode == "fallback_legacy_map_id":
        gateway_chat._finish_gateway_run_starting(stream_id, result="fallback")
        gateway_chat._STREAM_RUN_IDS[stream_id] = "run-legacy"

    try:
        started = time.monotonic()
        result = gateway_chat.gateway_steer_run(stream_id, "too late")
        elapsed = time.monotonic() - started
        assert result == (False, expected)
        assert elapsed < 1.0  # failed/fallback must not wait out the 5s budget
        assert steer_relay.calls == []
        assert _waiter_count(stream_id) == 0
    finally:
        _stop_lifecycle(stream_id)


@pytest.mark.parametrize("terminal", ["failed", "fallback"])
def test_gateway_steer_waiter_retires_owner_done_state(steer_relay, terminal):
    """Owner finishes while a steer waiter exists: the waiter still gets the
    terminal answer, and once that final waiter exits both lifecycle and id
    entries disappear."""
    from api import gateway_chat

    stream_id = f"stream-steer-retire-{terminal}"
    results: dict = {}
    gateway_chat._mark_gateway_run_starting(stream_id)
    thread = _start_steer_thread(results, "steer", stream_id, "abort this")
    try:
        _wait_for_waiter_count(stream_id, 1)
        if terminal == "fallback":
            gateway_chat._finish_gateway_run_starting(stream_id, result="fallback")
        else:
            gateway_chat._finish_gateway_run_starting(stream_id)
        gateway_chat._clear_gateway_run_starting(stream_id)  # owner done; waiter alive
        thread.join(timeout=5)
        assert not thread.is_alive()
        expected = "gateway_steer_queued" if terminal == "fallback" else "gateway_steer_no_run_id"
        assert results["steer"] == (False, expected)
        assert steer_relay.calls == []
        # The exiting waiter was the last reference: state retired.
        assert stream_id not in gateway_chat._STREAM_RUN_LIFECYCLE
        assert stream_id not in gateway_chat._STREAM_RUN_IDS
    finally:
        _stop_lifecycle(stream_id)
        thread.join(timeout=5)


def test_gateway_steer_ready_state_usable_once_then_retires(steer_relay):
    """Ready publication followed by owner-done: a steer waiter can still
    borrow the published id once; when the last waiter reference exits, the
    ready state retires. (No assertion that a later cancel revokes the id —
    that race is decided by the upstream response.)"""
    from api import gateway_chat

    stream_id = "stream-steer-retire-ready"
    gateway_chat._mark_gateway_run_starting(stream_id)
    gateway_chat._publish_gateway_run_id(stream_id, "run-ready")
    # Hold one external waiter reference so owner-done cannot retire the
    # ready state before the steer helper borrows the id.
    with gateway_chat._STREAM_RUN_STARTING_CONDITION:
        gateway_chat._STREAM_RUN_LIFECYCLE[stream_id]["waiters"] = 1
    gateway_chat._clear_gateway_run_starting(stream_id)
    try:
        assert stream_id in gateway_chat._STREAM_RUN_LIFECYCLE
        result = gateway_chat.gateway_steer_run(stream_id, "finish up")
        assert result == (True, None)
        assert len(steer_relay.calls) == 1
        req = steer_relay.calls[0]["req"]
        assert req.full_url == "http://gateway.test/v1/runs/run-ready/steer"
        # Final waiter exits: owner-done state retires.
        with gateway_chat._STREAM_RUN_STARTING_CONDITION:
            state = gateway_chat._STREAM_RUN_LIFECYCLE[stream_id]
            state["waiters"] = max(0, int(state.get("waiters") or 0) - 1)
            assert gateway_chat._retire_gateway_run_starting_if_done(stream_id) is True
        assert stream_id not in gateway_chat._STREAM_RUN_LIFECYCLE
        assert stream_id not in gateway_chat._STREAM_RUN_IDS
    finally:
        _stop_lifecycle(stream_id)


def test_gateway_steer_wait_is_stream_scoped(steer_relay):
    """Two pending streams: publishing B must not wake or misroute A's
    delivery; each stream relays exactly its own id/text."""
    from api import gateway_chat

    stream_a = "stream-steer-scope-a"
    stream_b = "stream-steer-scope-b"
    results: dict = {}
    gateway_chat._mark_gateway_run_starting(stream_a)
    gateway_chat._mark_gateway_run_starting(stream_b)
    thread_a = _start_steer_thread(results, "a", stream_a, "text-for-a")
    thread_b = _start_steer_thread(results, "b", stream_b, "text-for-b")
    try:
        _wait_for_waiter_count(stream_a, 1)
        _wait_for_waiter_count(stream_b, 1)

        gateway_chat._publish_gateway_run_id(stream_b, "run-B")
        thread_b.join(timeout=5)
        assert not thread_b.is_alive()
        assert results["b"] == (True, None)
        urls = [call["req"].full_url for call in steer_relay.calls]
        assert urls == ["http://gateway.test/v1/runs/run-B/steer"]
        assert thread_a.is_alive()  # A still waiting; no cross-stream POST

        gateway_chat._publish_gateway_run_id(stream_a, "run-A")
        thread_a.join(timeout=5)
        assert not thread_a.is_alive()
        assert results["a"] == (True, None)
        urls = [call["req"].full_url for call in steer_relay.calls]
        assert urls == [
            "http://gateway.test/v1/runs/run-B/steer",
            "http://gateway.test/v1/runs/run-A/steer",
        ]
        bodies = [json.loads(call["req"].data.decode("utf-8")) for call in steer_relay.calls]
        assert bodies == [{"text": "text-for-b"}, {"text": "text-for-a"}]
        assert _waiter_count(stream_a) == 0
        assert _waiter_count(stream_b) == 0
    finally:
        _stop_lifecycle(stream_a)
        _stop_lifecycle(stream_b)
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)
