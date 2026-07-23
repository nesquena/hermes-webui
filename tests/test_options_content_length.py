"""Regression test — the CORS preflight (OPTIONS) 200 must be framed.

`Handler.do_OPTIONS` answered every preflight with `send_response(200)` +
CORS headers + `end_headers()` but no `Content-Length`. The class runs
`protocol_version = "HTTP/1.1"` (keep-alive on), and its own docstring states
"every response must declare framing". An unframed 200 is "read body until the
connection closes" (RFC 7230 §3.3.3), so a keep-alive client issuing a preflight
(browser CORS for an allowlisted cross-origin front-end, curl, a proxy, a
monitor) blocks reading a body that never arrives until the 30s connection
timeout — and can't reuse the connection.

This pins that the preflight now carries `Content-Length: 0`, sent before
`end_headers()` (so it lands among the emitted headers), mirroring the framing
every other bodyless response in the server already sets. The CORS-header policy
itself (which Origins are echoed) is unchanged and covered separately.
"""
class _FakeHeaders(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class _OptionsHandler:
    """Captures the send_response / send_header / end_headers sequence."""

    def __init__(self, headers=None):
        self.headers = _FakeHeaders(headers or {})
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.order = []

    def send_response(self, status):
        self.status = status
        self.order.append(("status", status, None))

    def send_header(self, key, value):
        self.order.append(("hdr", key.lower(), value))

    def end_headers(self):
        self.order.append(("end", None, None))


def _run_options(headers=None):
    import server

    handler = _OptionsHandler(headers)
    server.Handler.do_OPTIONS(handler)
    return handler


def test_options_sets_content_length_zero():
    handler = _run_options()
    assert handler.status == 200
    header_map = {k: v for kind, k, v in handler.order if kind == "hdr"}
    assert header_map.get("content-length") == "0", (
        "OPTIONS preflight 200 must be framed with Content-Length: 0"
    )


def test_options_content_length_precedes_end_headers():
    """Framing must land before end_headers() to be emitted at all."""
    handler = _run_options()
    end_idx = next(i for i, e in enumerate(handler.order) if e[0] == "end")
    header_names = [k for kind, k, _ in handler.order[:end_idx] if kind == "hdr"]
    assert "content-length" in header_names


def test_options_framed_even_for_allowlisted_origin(monkeypatch):
    """The framing is unconditional — a preflight that DOES emit CORS headers
    (allowlisted origin) must still be framed, not just the no-origin case."""
    import api.routes as routes

    monkeypatch.setattr(
        routes, "_check_same_origin_browser_request", lambda handler: True
    )
    handler = _run_options({"Origin": "https://app.example"})
    header_map = {k: v for kind, k, v in handler.order if kind == "hdr"}
    assert header_map.get("content-length") == "0"
