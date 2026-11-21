"""Regression: the session content search must not run on the request thread.

``GET /api/sessions/search?content=1`` resolved every sidebar session inline in
the request thread: the response time was the sum of every transcript read, and
the client issued a new one on every debounced keystroke. On an install with a
few hundred sessions that is seconds of CPU and hundreds of MB of transient
memory per scan, and two overlapping scans were enough to push the host into
swap while the WebUI stopped accepting connections.

The change moves the scan into a background worker (capped by a process-global
semaphore), publishes matches over ``GET /api/sessions/search/stream`` as they
are found, and makes the legacy endpoint answer with ``partial: true`` after a
wait budget instead of blocking.

These tests fail on the pre-change code: there is no ``partial`` key, the legacy
handler only answers after the whole scan, and the stream route does not exist.
"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


NEEDLE = "needle"


def _make_sessions(count):
    """*count* sidebar metadata rows, every third one containing the needle."""
    return [
        {"session_id": f"s{i}", "title": f"Session {i}", "profile": "default"}
        for i in range(count)
    ]


def _make_session(index):
    content = f"body of session {index}"
    if index % 3 == 0:
        content = f"this transcript mentions {NEEDLE} once"
    return SimpleNamespace(
        session_id=f"s{index}",
        messages=[
            {"role": "user", "content": f"hello {index}"},
            {"role": "assistant", "content": content},
        ],
    )


class _ScanProbe:
    """Slow ``get_session_for_scan`` replacement that records its call count."""

    def __init__(self, delay):
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, sid):
        with self._lock:
            self.calls += 1
            index = int(str(sid).lstrip("s"))
        time.sleep(self.delay)
        return _make_session(index)

    @property
    def count(self):
        with self._lock:
            return self.calls


class _Headers(list):
    """Header list that also answers ``.get`` like http.client's message object."""

    def get(self, key, default=None):
        for name, value in self:
            if str(name).lower() == str(key).lower():
                return value
        return default


class _FakeHandler:
    """Minimal request handler: captures the body instead of writing a socket."""

    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.headers = _Headers()
        self.connection = None
        self.prints = []

    def send_response(self, status, message=None):
        self.status = status
        self.headers = _Headers()

    def send_header(self, name, value):
        self.headers.append((name, value))

    def end_headers(self):
        pass

    def _safe_webui_print(self, message):
        self.prints.append(message)

    @property
    def body(self):
        return self.wfile.getvalue().decode("utf-8", "replace")


def _sse_events(body):
    """Split an SSE body into ``(event, payload)`` pairs."""
    events = []
    for frame in body.split("\n\n"):
        event = None
        data = None
        for line in frame.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if event is not None:
            events.append((event, json.loads(data) if data else None))
    return events


@pytest.fixture
def scan_env(monkeypatch):
    """Isolate both search handlers from the real session store."""
    import api.routes as routes

    def _install(count=40, delay=0.05, budget=0.4):
        probe = _ScanProbe(delay)
        sessions = _make_sessions(count)
        monkeypatch.setattr(routes, "all_sessions", lambda: list(sessions))
        monkeypatch.setattr(routes, "get_session_for_scan", probe)
        monkeypatch.setattr(
            "api.profiles.get_active_profile_name", lambda: "default"
        )
        # raising=False keeps the fixture runnable against the pre-change module,
        # where the budget knob does not exist (it scanned inline instead).
        monkeypatch.setattr(routes, "CONTENT_SEARCH_WAIT_SECONDS", budget, raising=False)
        return sessions, probe

    return _install


def _wait_for_slot(timeout=5.0):
    """Block until the process-global scan slot is free again."""
    import api.routes as routes

    got = routes._CONTENT_SEARCH_SLOTS.acquire(timeout=timeout)
    if got:
        routes._CONTENT_SEARCH_SLOTS.release()
    return got


class _SearchHarness(BaseHTTPRequestHandler):
    """Real HTTP surface for two routes, no auth/CSP/TLS in the way."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - http.server API
        import api.routes as routes

        routes.handle_get(self, urlparse(self.path))

    def log_message(self, format, *args):  # noqa: A002 - http.server API
        pass


@pytest.fixture
def search_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SearchHarness)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _http_get(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read() or b"{}"), response.status


def test_content_search_answers_while_scan_is_still_running(scan_env, search_server):
    # 40 sessions x 50 ms: a full inline scan takes ~2 s.
    sessions, probe = scan_env(count=40, delay=0.05, budget=0.4)

    result = {}

    def run_search():
        started = time.monotonic()
        try:
            payload, status = _http_get(
                search_server + f"/api/sessions/search?q={NEEDLE}&content=1&depth=5"
            )
            result["payload"] = payload
            result["status"] = status
        except Exception as exc:  # pragma: no cover - surfaced below
            result["error"] = exc
        result["elapsed"] = time.monotonic() - started

    worker = threading.Thread(target=run_search, daemon=True)
    worker.start()

    # Wait until the scan has demonstrably started, then serve an unrelated
    # cheap request from the same server while it is still scanning.
    deadline = time.monotonic() + 5.0
    while probe.count < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert probe.count >= 2, "the content scan never started"

    cheap_started = time.monotonic()
    cheap_payload, cheap_status = _http_get(search_server + "/api/sessions/search?q=")
    cheap_elapsed = time.monotonic() - cheap_started

    assert cheap_status == 200
    assert "sessions" in cheap_payload
    assert cheap_elapsed < 0.3, (
        f"an empty-query search took {cheap_elapsed:.2f}s while a content scan "
        "was running; the request thread is not free"
    )
    assert "elapsed" not in result, (
        "the content search had already answered before the unrelated request "
        "ran, so the concurrency window was not exercised"
    )

    worker.join(timeout=10)
    assert "error" not in result, result.get("error")

    payload = result["payload"]
    assert "partial" in payload, (
        "the legacy endpoint must report an incomplete answer instead of "
        "blocking for the whole scan"
    )
    assert payload["partial"] is True
    assert payload["scanned"] < payload["total"], (
        f"scanned={payload['scanned']} total={payload['total']}: the response "
        "waited for the whole scan"
    )
    assert result["elapsed"] < 1.5, (
        f"search answered after {result['elapsed']:.2f}s; the budget was 0.4s "
        "and the full scan takes ~2s"
    )
    assert payload["count"] == len(payload["sessions"])
    assert len(sessions) == payload["total"]

    assert _wait_for_slot(), "the cancelled scan never released its slot"


def test_budget_timeout_returns_partial_and_stops_the_scan(scan_env):
    import api.routes as routes

    sessions, probe = scan_env(count=40, delay=0.04, budget=0.3)
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    original_j = routes.j
    routes.j = fake_j
    try:
        started = time.monotonic()
        routes._handle_sessions_search(
            _FakeHandler(), urlparse("/api/sessions/search?q=needle&content=1&depth=5")
        )
        elapsed = time.monotonic() - started
    finally:
        routes.j = original_j

    payload = captured["payload"]
    calls_at_response = probe.count
    # budget + one session in flight + the cancel grace, with slack
    assert elapsed < 1.0, f"search blocked for {elapsed:.2f}s on a 0.3s budget"
    assert payload["partial"] is True
    assert payload["scanned"] < payload["total"], (
        f"scanned={payload['scanned']} of {payload['total']}: the budget did "
        "not interrupt the scan"
    )
    assert payload["count"] == len(payload["sessions"])
    assert payload["count"] <= payload["scanned"]

    # The scan must not keep resolving sessions after the response was built.
    time.sleep(0.3)
    calls_mid = probe.count
    assert calls_mid - calls_at_response <= 1, (
        f"{calls_mid - calls_at_response} more sessions were resolved after the "
        "response; cancellation is not observed at the session boundary"
    )
    time.sleep(0.4)
    assert probe.count == calls_mid, "the scan was still running after the response"
    assert _wait_for_slot(), "the cancelled scan never released its slot"


def test_stream_publishes_matches_before_the_scan_finishes(scan_env):
    import api.routes as routes

    sessions, probe = scan_env(count=30, delay=0.04, budget=30.0)
    handler = _FakeHandler()
    parsed = urlparse("/api/sessions/search/stream?q=needle&content=1&depth=5")
    finished = {}

    def run_stream():
        finished["result"] = routes._handle_sessions_search_stream(handler, parsed)

    worker = threading.Thread(target=run_stream, daemon=True)
    worker.start()

    # The first matching session is index 0; the scan needs ~1.2 s in total, so
    # the first results frame has to be visible while it is still resolving.
    deadline = time.monotonic() + 5.0
    while "event: results" not in handler.body and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "event: results" in handler.body, (
        "the stream did not publish any match before the scan completed"
    )
    assert probe.count < len(sessions), (
        "the scan had already finished when the first batch was published"
    )

    worker.join(timeout=15)
    assert finished.get("result") is True

    events = _sse_events(handler.body)
    names = [name for name, _ in events]
    assert names[0] == "results"
    assert names[-1] == "done"
    assert "results" in names

    published = []
    done_payload = None
    for name, data in events:
        if name == "results":
            published.extend(data["sessions"])
        else:
            done_payload = data
    assert done_payload is not None, "the stream ended without a done event"
    sent_ids = [row["session_id"] for row in published]
    assert len(sent_ids) == len(set(sent_ids)), "a session was published twice"
    expected = [s["session_id"] for s in sessions if int(s["session_id"][1:]) % 3 == 0]
    assert sent_ids == expected, "the stream dropped or reordered matches"
    assert all(row["match_type"] == "content" for row in published)
    assert done_payload["count"] == len(expected)
    assert done_payload["scanned"] == len(sessions)
    assert done_payload["partial"] is False
    assert done_payload["cancelled"] is False
    assert handler.status == 200
    assert ("Content-Type", "text/event-stream; charset=utf-8") in handler.headers
    assert _wait_for_slot(), "the finished scan never released its slot"


def test_stream_empty_query_closes_as_a_stream(scan_env):
    import api.routes as routes

    scan_env(count=3, delay=0.0, budget=0.4)
    handler = _FakeHandler()
    assert routes._handle_sessions_search_stream(
        handler, urlparse("/api/sessions/search/stream?q=&content=1")
    ) is True

    events = _sse_events(handler.body)
    assert [name for name, _ in events] == ["done"], (
        "an empty query must answer on the stream, not with a JSON body that "
        "the EventSource client can only read as an error"
    )
    assert events[0][1]["count"] == 0
    assert ("Content-Type", "text/event-stream; charset=utf-8") in handler.headers


def test_concurrent_searches_share_one_scan_bound(scan_env, monkeypatch):
    """Three overlapping searches must not run three transcripts scans at once."""
    import api.routes as routes

    sessions, _probe = scan_env(count=10, delay=0.03, budget=30.0)
    lock = threading.Lock()
    active = {"now": 0, "peak": 0}

    def tracking_scan(sid):
        with lock:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        time.sleep(0.03)
        with lock:
            active["now"] -= 1
        return _make_session(int(str(sid).lstrip("s")))

    monkeypatch.setattr(routes, "get_session_for_scan", tracking_scan)

    original_j = routes.j
    routes.j = lambda handler, payload, status=200, extra_headers=None: None
    try:
        threads = [
            threading.Thread(
                target=routes._handle_sessions_search,
                args=(_FakeHandler(), urlparse("/api/sessions/search?q=needle&content=1")),
            )
            for _ in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "a content search request never returned"
    finally:
        routes.j = original_j

    # The semaphore is the only thing standing between a debounced search box
    # and the swap exhaustion this change fixes.
    assert active["peak"] <= getattr(routes, "CONTENT_SEARCH_SCAN_SLOTS", 1), (
        f"{active['peak']} scans ran at once; the scan-slot bound did not hold"
    )
    assert active["peak"] >= 1
    assert _wait_for_slot(), "the concurrent scans never released the slot"


def test_scan_thread_start_failure_answers_503_on_both_routes(scan_env):
    """A thread that cannot start must answer, not wedge or 500 either route."""
    import api.routes as routes
    from unittest.mock import patch

    scan_env(count=3, delay=0.0, budget=0.4)

    def explode(self):
        raise RuntimeError("can't start new thread")

    with patch.object(routes.threading.Thread, "start", explode):
        legacy_handler = _FakeHandler()
        routes._handle_sessions_search(
            legacy_handler, urlparse("/api/sessions/search?q=needle&content=1")
        )
        stream_handler = _FakeHandler()
        routes._handle_sessions_search_stream(
            stream_handler, urlparse("/api/sessions/search/stream?q=needle&content=1")
        )

    assert legacy_handler.status == 503, (
        "the legacy route 500s when the scan thread cannot start"
    )
    assert stream_handler.status == 503, (
        "the stream route 500s when the scan thread cannot start"
    )
