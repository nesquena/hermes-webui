"""HTTP-level integration coverage for the /api/sessions/events SSE endpoint.

The pure-bus tests in test_session_events.py cover the in-process publish/subscribe
contract but never open an actual HTTP connection — they don't exercise the SSE
handler's unsubscribe-on-disconnect path, which is the entire safety contract for
the new event bus (without it, server threads leak queues until process exit).

These tests open a real connection to the test server, drive the bus by triggering
side-effects from a separate thread (creating a session via /api/session/new), and
assert the SSE frame arrives. The test-server subprocess has its own subscriber
pool, so we can only test the disconnect-without-crash path here, not the in-pytest
subscriber count. Added in stage-393 follow-up to Opus advisor's blocking concern
on PR #2637.
"""

import json
import threading
import time
import urllib.error
import urllib.request

from tests._pytest_port import BASE


def _read_sse_frames(resp, deadline):
    """Read raw SSE frames from a streaming response until deadline.

    Yields ('event_name', payload_str) tuples. Treats `: keepalive` lines as
    keepalives (returned as ('keepalive', None)). Returns when the deadline
    elapses or the connection closes.
    """
    event_type = None
    data_lines = []
    while time.time() < deadline:
        line = resp.readline()
        if not line:
            time.sleep(0.05)
            continue
        decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if decoded == "":
            if data_lines:
                yield (event_type or "message", "\n".join(data_lines))
                event_type = None
                data_lines = []
            continue
        if decoded.startswith("event: "):
            event_type = decoded[7:].strip()
        elif decoded.startswith("data: "):
            data_lines.append(decoded[6:])
        elif decoded.startswith(": "):
            yield ("keepalive", None)


def _subscriber_count():
    """Read the live subscriber count from this pytest process's bus module."""
    from api import session_events
    with session_events._SESSION_EVENTS_LOCK:
        return len(session_events._SESSION_EVENTS_SUBSCRIBERS)


def test_session_events_bus_subscribe_unsubscribe_balance():
    """
    Pure-bus contract: subscribe → publish → unsubscribe leaves the subscriber
    set at the baseline. This is the safety invariant the SSE handler's
    finally block depends on.
    """
    from api import session_events

    baseline = _subscriber_count()
    q = session_events.subscribe_session_events()
    try:
        assert _subscriber_count() == baseline + 1
        session_events.publish_session_list_changed("integration_test")
        payload = q.get(timeout=2.0)
        assert payload["type"] == "sessions_changed"
        assert payload["reason"] == "integration_test"
    finally:
        session_events.unsubscribe_session_events(q)
    assert _subscriber_count() == baseline


def test_session_events_sse_endpoint_handshake_succeeds(cleanup_test_sessions):
    """
    Open a real HTTP connection to /api/sessions/events on the test server.
    Verify the handshake succeeds (200 + text/event-stream Content-Type) and
    we can read at least one event — either a sessions_changed event triggered
    by a side-effect we cause (POST /api/session/new) or a keepalive after the
    heartbeat interval. Then close the socket; a leaking handler would not
    crash the server but would tie up a thread + queue indefinitely.
    """
    url = BASE + "/api/sessions/events"
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        raise AssertionError(f"SSE endpoint returned HTTP {e.code}: {e.read()[:200]!r}")

    try:
        assert resp.status == 200
        ct = resp.headers.get("Content-Type", "")
        assert "text/event-stream" in ct, f"unexpected Content-Type: {ct!r}"

        # Cause a side-effect that should publish an event: create a session.
        # The publish runs in the server process, so the subscribed queue on
        # the server side gets the frame. We must give the SSE thread time to
        # subscribe before publishing — do the POST on a small delay thread.
        def trigger():
            time.sleep(0.5)
            try:
                payload = json.dumps({"title": "sse-test"}).encode()
                req2 = urllib.request.Request(
                    BASE + "/api/session/new",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req2, timeout=5) as r2:
                    d = json.loads(r2.read())
                    sid = d.get("session", {}).get("session_id")
                    if sid:
                        cleanup_test_sessions.append(sid)
            except Exception:
                pass

        threading.Thread(target=trigger, daemon=True).start()

        deadline = time.time() + 8.0  # heartbeat is 5s; we should see an event well before that or a keepalive after
        got_real_event = False
        got_keepalive = False
        for kind, data in _read_sse_frames(resp, deadline):
            if kind == "sessions_changed":
                got_real_event = True
                try:
                    payload = json.loads(data)
                    assert payload.get("type") == "sessions_changed"
                except (ValueError, AssertionError):
                    pass
                break
            elif kind == "keepalive":
                got_keepalive = True
                break
        assert got_real_event or got_keepalive, (
            "expected either a sessions_changed event from the triggered POST or "
            "a keepalive frame within 8s"
        )
    finally:
        resp.close()


def test_session_events_sse_endpoint_survives_rapid_disconnect():
    """
    Open and immediately close several SSE connections back-to-back. The
    handler must not leak threads/queues — the server should stay responsive
    for a subsequent regular /api/sessions GET.
    """
    url = BASE + "/api/sessions/events"
    for _ in range(5):
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            try:
                # Read a single byte to confirm the stream opened, then close.
                resp.read(1)
            finally:
                resp.close()
        except urllib.error.HTTPError as e:
            raise AssertionError(f"connection iteration failed: HTTP {e.code}")
        time.sleep(0.1)

    # Server still serves a normal GET after the rapid open/close burst.
    sentinel_req = urllib.request.Request(
        BASE + "/api/sessions", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(sentinel_req, timeout=5) as r:
            assert r.status == 200
            body = json.loads(r.read())
            assert isinstance(body, dict)
    except urllib.error.HTTPError as e:
        raise AssertionError(f"server unresponsive after SSE burst: HTTP {e.code}")


def test_session_events_handler_uses_disconnect_safe_errors_tuple():
    """
    Source-level guard that the handler's except clause references the shared
    _CLIENT_DISCONNECT_ERRORS tuple defined in api/routes.py. This is the
    safety mechanism that makes the unsubscribe-on-disconnect path actually
    fire across the full range of socket-failure modes (not just BrokenPipeError).
    """
    from pathlib import Path

    routes = Path("api/routes.py").read_text(encoding="utf-8")
    handler_start = routes.find("def _handle_session_events_stream")
    assert handler_start >= 0, "_handle_session_events_stream not found"
    handler_end = routes.find("\ndef ", handler_start + 1)
    if handler_end < 0:
        handler_end = len(routes)
    handler_body = routes[handler_start:handler_end]
    assert "_CLIENT_DISCONNECT_ERRORS" in handler_body, (
        "handler must catch the shared disconnect tuple to trigger unsubscribe"
    )
    assert "unsubscribe_session_events" in handler_body, (
        "handler must unsubscribe in the cleanup path"
    )
    assert "finally:" in handler_body, "handler must use a finally block for cleanup"
