"""Reserved ``[SILENT]`` must never become a goal kickoff turn.

Cron agents use the exact final response ``[SILENT]`` as a delivery-suppression
sentinel. ``/api/goal`` accepts free text via ``args``/``text`` and, when it
looks like a kickoff, persists it as ``pending_user_message`` through
``_start_chat_stream_for_session``. If an external wake relay POSTs the
sentinel and 8701 restarts while the turn is pending, session repair
materializes it as ``{"role": "user", "_recovered": True}`` — the same
phantom-recovered-turn exposure #7018 closed for ``/api/chat/start``.

The goal ingress therefore treats the exact normalized sentinel as a
successful no-op *before* session lookup or goal-state mutation (mirrors
``tests/test_silent_control_suppression.py`` for the goal path).
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


def test_goal_ingress_suppresses_silent_before_session_lookup(monkeypatch):
    looked_up = []

    def _unexpected_lookup(*args, **kwargs):
        looked_up.append((args, kwargs))
        raise AssertionError("[SILENT] must be suppressed before session lookup")

    monkeypatch.setattr(routes, "get_session", _unexpected_lookup)
    handler = _JSONHandler()

    routes._handle_goal_command(
        handler,
        {"session_id": "does-not-need-to-exist", "args": "  [SILENT]\n"},
    )

    assert handler.status == 200
    assert _payload(handler) == {
        "status": "suppressed",
        "reason": "silent_control_message",
    }
    assert looked_up == []


def test_goal_ingress_suppresses_silent_via_text_field(monkeypatch):
    looked_up = []

    def _unexpected_lookup(*args, **kwargs):
        looked_up.append((args, kwargs))
        raise AssertionError("[SILENT] must be suppressed before session lookup")

    monkeypatch.setattr(routes, "get_session", _unexpected_lookup)
    handler = _JSONHandler()

    routes._handle_goal_command(
        handler,
        {"session_id": "does-not-need-to-exist", "text": "\t[SILENT] "},
    )

    assert handler.status == 200
    assert _payload(handler) == {
        "status": "suppressed",
        "reason": "silent_control_message",
    }
    assert looked_up == []


def test_goal_ingress_does_not_suppress_ordinary_kickoff(monkeypatch):
    looked_up = []

    def _record_lookup(*args, **kwargs):
        looked_up.append((args, kwargs))
        raise KeyError("session")

    monkeypatch.setattr(routes, "get_session", _record_lookup)
    handler = _JSONHandler()

    routes._handle_goal_command(
        handler,
        {"session_id": "does-not-need-to-exist", "args": "write a report"},
    )

    # Ordinary kickoff text must NOT be suppressed: the request proceeds to
    # session lookup (which raises here) instead of returning the 200 no-op.
    assert looked_up != []
    assert handler.status != 200


def test_goal_silent_suppression_is_exact_and_case_sensitive():
    assert routes._is_silent_control_message("[SILENT]") is True
    assert routes._is_silent_control_message("  [SILENT]\n") is True
    assert routes._is_silent_control_message("[silent]") is False
    assert routes._is_silent_control_message("prefix [SILENT]") is False
    assert routes._is_silent_control_message("") is False
    assert routes._is_silent_control_message(None) is False
