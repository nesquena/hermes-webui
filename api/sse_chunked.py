"""Opt-in chunked transfer-encoding for SSE responses.

The stdlib HTTP server emits SSE bodies with no framing at all (no
Content-Length, no Transfer-Encoding). Tornado-based reverse proxies —
notably jupyter-server-proxy, which fronts the WebUI in JupyterHub/REANA
deployments — read such responses with read-until-close semantics and
buffer the ENTIRE body until the upstream connection dies, so the browser
receives no events while a stream is live: chat appears frozen, then the
client's watchdog kills it ("Connection interrupted"). Explicit chunked
framing lets every event flush through each hop immediately.

This is opt-in via the ``HERMES_WEBUI_SSE_CHUNKED`` environment variable so
the default wire format is unchanged: directly-served deployments keep the
historical unframed stream, and only deployments behind a buffering proxy
need to set the flag. It sits alongside the existing ``X-Accel-Buffering: no``
proxy-compatibility hint on these same handlers.

Usage: in an SSE handler, replace ``handler.end_headers()`` with
``end_sse_headers(handler)``. When the flag is set, all subsequent
``handler.wfile.write()`` calls are framed transparently.
"""

import os

_TRUTHY = {"1", "true", "yes", "on"}


def chunked_sse_enabled() -> bool:
    """True when ``HERMES_WEBUI_SSE_CHUNKED`` opts into chunked SSE framing."""
    return os.getenv("HERMES_WEBUI_SSE_CHUNKED", "").strip().lower() in _TRUTHY


class _ChunkedSSEWriter:
    """Wrap ``wfile`` so each write becomes an HTTP/1.1 chunk."""

    def __init__(self, raw):
        self._raw = raw

    def write(self, data):
        if not data:
            return 0
        payload = bytes(data)
        self._raw.write(b"%X\r\n" % len(payload) + payload + b"\r\n")
        return len(payload)

    def flush(self):
        self._raw.flush()

    def __getattr__(self, name):
        return getattr(self._raw, name)


def end_sse_headers(handler):
    """Finish SSE response headers, optionally enabling chunked framing.

    When ``HERMES_WEBUI_SSE_CHUNKED`` is set, send ``Transfer-Encoding: chunked``
    and wrap ``wfile`` so each write is framed as one HTTP/1.1 chunk; otherwise
    behave exactly like ``handler.end_headers()`` so the default wire format is
    preserved. Chunked + ``Connection: close`` is legal and unambiguous on the
    HTTP/1.1 responses these handlers emit.
    """
    if chunked_sse_enabled():
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        handler.wfile = _ChunkedSSEWriter(handler.wfile)
    else:
        handler.end_headers()
