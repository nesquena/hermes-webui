"""Regression test for #3103 / PR #3128.

Commit 598fd4ff added `Connection: close` to the two long-lived SSE handlers
(`_handle_gateway_sse_stream`, `_handle_session_events_stream`). Browsers treat
`Connection: close` on an SSE response as a signal that the EventSource lifecycle
has ended and auto-reconnect, producing a reconnect storm that thrashes the
session list (#3103). These handlers must NOT emit `Connection: close`.

Finite responses (the on-the-fly ZIP download, terminal SSE, etc.) still need the
header for unambiguous HTTP/1.1 message boundaries — this test only pins the two
long-lived session/gateway event streams.
"""

import re
from pathlib import Path

ROUTES = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(encoding="utf-8")


def _handler_body(name: str) -> str:
    """Return the source of a handler function from its `def` to the next top-level `def`."""
    start = ROUTES.index(f"def {name}(")
    # find the next top-level "def " after the function start
    nxt = ROUTES.find("\ndef ", start + 1)
    return ROUTES[start: nxt if nxt != -1 else len(ROUTES)]


def _emits_connection_close(body: str) -> bool:
    """True if the handler actually CALLS send_header to emit Connection: close
    (ignores mentions in comments)."""
    return bool(
        re.search(r"send_header\(\s*['\"]Connection['\"]\s*,\s*['\"]close['\"]", body)
    )


def test_session_events_stream_does_not_emit_connection_close():
    body = _handler_body("_handle_session_events_stream")
    assert "text/event-stream" in body, "sanity: this is the SSE handler"
    assert not _emits_connection_close(body), (
        "_handle_session_events_stream must not emit `Connection: close` — it triggers "
        "browser EventSource reconnect storms on this long-lived stream (#3103)"
    )


def test_gateway_sse_stream_does_not_emit_connection_close():
    body = _handler_body("_handle_gateway_sse_stream")
    assert "text/event-stream" in body, "sanity: this is the SSE handler"
    assert not _emits_connection_close(body), (
        "_handle_gateway_sse_stream must not emit `Connection: close` — it triggers "
        "browser EventSource reconnect storms on this long-lived stream (#3103)"
    )


def test_connection_close_still_present_for_finite_zip_download():
    """The on-the-fly ZIP download (no Content-Length) still needs Connection: close
    for unambiguous HTTP/1.1 framing (#2836). Guard against an over-broad removal."""
    assert ROUTES.count('"Connection", "close"') + ROUTES.count("'Connection', 'close'") >= 1, (
        "at least the finite-response handlers (ZIP download, etc.) must keep "
        "`Connection: close` for HTTP/1.1 message-boundary correctness (#2836)"
    )
