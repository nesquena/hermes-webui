"""Reserved ``[SILENT]`` must never become a WebUI conversation turn.

Cron agents use the exact final response ``[SILENT]`` as a delivery-suppression
sentinel. If an external wake relay accidentally POSTs that sentinel to
``/api/chat/start`` and 8701 restarts while the turn is pending, session repair
materializes it as ``{"role": "user", "_recovered": True}``. That creates a
visible user turn and can repeat on every restart.

Both server-side entry points therefore treat the exact normalized sentinel as
a successful no-op *before* session lookup or pending-state mutation.
"""
from __future__ import annotations

import io
import json

from api import routes


class _JSONHandler:
    headers = {}

    def __init__(self):
        self.status = None
        self.wfile = io.BytesIO()
        self.headers_sent = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers_sent[key] = value

    def end_headers(self):
        pass


def _payload(handler):
    raw = handler.wfile.getvalue().decode("utf-8")
    return json.loads(raw) if raw else {}


def test_http_chat_start_suppresses_silent_before_session_lookup(monkeypatch):
    looked_up = []

    def _unexpected_lookup(*args, **kwargs):
        looked_up.append((args, kwargs))
        raise AssertionError("[SILENT] must be suppressed before session lookup")

    monkeypatch.setattr(routes, "_get_or_materialize_session", _unexpected_lookup)
    handler = _JSONHandler()

    routes._handle_chat_start(
        handler,
        {"session_id": "does-not-need-to-exist", "message": "  [SILENT]\n"},
    )

    assert handler.status == 200
    assert _payload(handler) == {
        "status": "suppressed",
        "reason": "silent_control_message",
    }
    assert looked_up == []


def test_server_side_start_suppresses_silent_before_session_lookup(monkeypatch):
    looked_up = []

    def _unexpected_lookup(*args, **kwargs):
        looked_up.append((args, kwargs))
        raise AssertionError("[SILENT] must be suppressed before session lookup")

    monkeypatch.setattr(routes, "get_session", _unexpected_lookup)

    result = routes.start_session_turn(
        "does-not-need-to-exist",
        "\t[SILENT] ",
        source="process_wakeup",
    )

    assert result == {
        "status": "suppressed",
        "reason": "silent_control_message",
        "_status": 200,
    }
    assert looked_up == []


def test_silent_suppression_is_exact_and_case_sensitive():
    assert routes._is_silent_control_message("[SILENT]") is True
    assert routes._is_silent_control_message("  [SILENT]\n") is True
    assert routes._is_silent_control_message("[silent]") is False
    assert routes._is_silent_control_message("prefix [SILENT]") is False
    assert routes._is_silent_control_message("") is False
    assert routes._is_silent_control_message(None) is False
