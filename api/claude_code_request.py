"""HTTP request policy for the Claude Code bridge."""

from __future__ import annotations

from typing import Any
from urllib.parse import ParseResult, urlparse


_BRIDGE_PATH_PREFIX = "/api/claude-code/"
_TERMINAL_STREAM_PATH = "/api/claude-code/terminal/output"


class ClaudeCodeRequestMixin:
    """Add sensitive bridge response headers before base header finalisation."""

    def end_headers(self) -> None:
        if getattr(self, "_is_claude_bridge_request", False):
            buffered = getattr(self, "_headers_buffer", ())
            header_names = {
                line.split(b":", 1)[0].strip().lower()
                for line in buffered
                if b":" in line
            }
            if b"cache-control" not in header_names:
                self.send_header("Cache-Control", "no-store")
            if b"referrer-policy" not in header_names:
                self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()


def prepare_claude_code_request(handler: Any) -> ParseResult:
    """Parse a request and reset bridge metadata for this keep-alive turn."""
    parsed = urlparse(handler.path)
    is_bridge = parsed.path.startswith(_BRIDGE_PATH_PREFIX)
    handler._is_claude_bridge_request = is_bridge
    handler._access_log_path = (
        parsed.path if parsed.path == _TERMINAL_STREAM_PATH else handler.path
    )
    if is_bridge:
        handler._referrer_policy = "no-referrer"
    elif hasattr(handler, "_referrer_policy"):
        del handler._referrer_policy
    return parsed


def request_log_path(handler: Any) -> str:
    """Return the request path with sensitive bridge stream queries removed."""
    return getattr(handler, "_access_log_path", None) or getattr(handler, "path", None) or "-"


def log_request_error(handler: Any, traceback_text: str) -> None:
    """Log a request failure without exposing a bridge stream query."""
    handler._safe_webui_print(
        f"[webui] ERROR {handler.command} {request_log_path(handler)}\n{traceback_text}"
    )
