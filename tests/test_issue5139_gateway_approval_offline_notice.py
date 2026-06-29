"""Regression coverage for #5139 gateway approval offline notice."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from api.config import STREAMS, STREAMS_LOCK, invalidate_gateway_caps
from api.gateway_chat import _run_gateway_chat_streaming

REPO = Path(__file__).resolve().parents[1]
GATEWAY_CHAT = (REPO / "api" / "gateway_chat.py").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def _run_gateway_warning_case(unavailable_reason: str) -> list:
    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)

    stream_id = f"sid-{unavailable_reason}"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    mock_session = MagicMock()
    mock_session.active_stream_id = stream_id
    mock_session.workspace = "/tmp"
    mock_session.model = "test"
    mock_session.model_provider = None
    mock_session.profile = None
    mock_session.context_messages = []
    mock_session.messages = []
    mock_session.pending_user_message = None
    mock_session.pending_attachments = None
    mock_session.pending_started_at = None
    mock_session._approval_notice_emitted = False

    def fake_urlopen(req, *, timeout=None):
        assert req.full_url == "http://127.0.0.1:8642/v1/chat/completions"
        assert req.get_method() == "POST"
        payload = req.data.decode("utf-8")
        assert json.loads(payload)["messages"][-1]["content"] == "hi"
        resp = MagicMock()
        resp.__iter__ = lambda s: iter(
            [
                b'data: {"choices":[{"delta":{"content":"Done"}}]}\n',
                b"\n",
                b"data: [DONE]\n",
            ]
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    try:
        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}):
            with patch("api.gateway_chat.gateway_supports_approval", return_value=False), \
                 patch("api.gateway_chat.gateway_approval_unavailable_reason", return_value=unavailable_reason), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("api.gateway_chat.get_session", return_value=mock_session), \
                 patch("api.gateway_chat._stream_writeback_is_current", return_value=True), \
                 patch("api.gateway_chat.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id="sess-warning",
                    msg_text="hi",
                    model="test-model",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)

    return events


def test_gateway_chat_emits_offline_warning_for_unreachable_probe():
    events = _run_gateway_warning_case("unreachable")
    warnings = [item for item in events if isinstance(item, tuple) and item[0] == "warning"]
    assert warnings
    assert warnings[0][1]["type"] == "approval_gateway_offline"
    assert warnings[0][1]["message"] == "Gateway connection failed. Check that the connected Hermes gateway is running and reachable."
    assert any(isinstance(item, tuple) and item[0] == "done" for item in events)
    assert not any(
        isinstance(item, tuple) and item[0] == "warning" and item[1].get("type") == "approval_gateway_unsupported"
        for item in events
    )


def test_gateway_chat_keeps_unsupported_warning_for_reachable_older_gateway():
    events = _run_gateway_warning_case("unsupported")
    warnings = [item for item in events if isinstance(item, tuple) and item[0] == "warning"]
    assert warnings
    assert warnings[0][1]["type"] == "approval_gateway_unsupported"
    assert warnings[0][1]["message"] == "Approvals require a newer gateway. Upgrade the connected Hermes gateway to enable this."
    assert any(isinstance(item, tuple) and item[0] == "done" for item in events)
    assert not any(
        isinstance(item, tuple) and item[0] == "warning" and item[1].get("type") == "approval_gateway_offline"
        for item in events
    )


def test_gateway_chat_keeps_unsupported_warning_for_404_capabilities_probe():
    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)

    stream_id = "sid-http-404"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    mock_session = MagicMock()
    mock_session.active_stream_id = stream_id
    mock_session.workspace = "/tmp"
    mock_session.model = "test"
    mock_session.model_provider = None
    mock_session.profile = None
    mock_session.context_messages = []
    mock_session.messages = []
    mock_session.pending_user_message = None
    mock_session.pending_attachments = None
    mock_session.pending_started_at = None
    mock_session._approval_notice_emitted = False

    def fake_urlopen(req, *, timeout=None):
        if req.full_url == "http://127.0.0.1:8642/v1/capabilities":
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=io.BytesIO(b""))
        assert req.full_url == "http://127.0.0.1:8642/v1/chat/completions"
        resp = MagicMock()
        resp.__iter__ = lambda s: iter(
            [
                b'data: {"choices":[{"delta":{"content":"Done"}}]}\n',
                b"\n",
                b"data: [DONE]\n",
            ]
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    invalidate_gateway_caps()

    try:
        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("api.gateway_chat.get_session", return_value=mock_session), \
                 patch("api.gateway_chat._stream_writeback_is_current", return_value=True), \
                 patch("api.gateway_chat.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id="sess-404",
                    msg_text="hi",
                    model="test-model",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
        invalidate_gateway_caps()

    warnings = [item for item in events if isinstance(item, tuple) and item[0] == "warning"]
    assert warnings
    assert warnings[0][1]["type"] == "approval_gateway_unsupported"
    assert warnings[0][1]["message"] == "Approvals require a newer gateway. Upgrade the connected Hermes gateway to enable this."
    assert any(isinstance(item, tuple) and item[0] == "done" for item in events)
    assert not any(
        isinstance(item, tuple) and item[0] == "warning" and item[1].get("type") == "approval_gateway_offline"
        for item in events
    )


def test_gateway_chat_keeps_unsupported_warning_for_timeout_capabilities_probe():
    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)

    stream_id = "sid-timeout"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    mock_session = MagicMock()
    mock_session.active_stream_id = stream_id
    mock_session.workspace = "/tmp"
    mock_session.model = "test"
    mock_session.model_provider = None
    mock_session.profile = None
    mock_session.context_messages = []
    mock_session.messages = []
    mock_session.pending_user_message = None
    mock_session.pending_attachments = None
    mock_session.pending_started_at = None
    mock_session._approval_notice_emitted = False

    def fake_urlopen(req, *, timeout=None):
        if req.full_url == "http://127.0.0.1:8642/v1/capabilities":
            raise TimeoutError("timed out")
        assert req.full_url == "http://127.0.0.1:8642/v1/chat/completions"
        resp = MagicMock()
        resp.__iter__ = lambda s: iter(
            [
                b'data: {"choices":[{"delta":{"content":"Done"}}]}\n',
                b"\n",
                b"data: [DONE]\n",
            ]
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    invalidate_gateway_caps()

    try:
        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("api.gateway_chat.get_session", return_value=mock_session), \
                 patch("api.gateway_chat._stream_writeback_is_current", return_value=True), \
                 patch("api.gateway_chat.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id="sess-timeout",
                    msg_text="hi",
                    model="test-model",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
        invalidate_gateway_caps()

    warnings = [item for item in events if isinstance(item, tuple) and item[0] == "warning"]
    assert warnings
    assert warnings[0][1]["type"] == "approval_gateway_unsupported"
    assert warnings[0][1]["message"] == "Approvals require a newer gateway. Upgrade the connected Hermes gateway to enable this."
    assert any(isinstance(item, tuple) and item[0] == "done" for item in events)
    assert not any(
        isinstance(item, tuple) and item[0] == "warning" and item[1].get("type") == "approval_gateway_offline"
        for item in events
    )


def test_messages_js_handles_offline_warning_without_touching_unsupported_branch():
    assert "d.type==='approval_gateway_offline'" in MESSAGES_JS
    assert "Gateway offline" in MESSAGES_JS
    assert "d.type==='approval_gateway_unsupported'" in MESSAGES_JS
    assert "Approvals not supported" in MESSAGES_JS
    assert "setComposerStatus(`${d.message||'Warning'}`);" in MESSAGES_JS


def test_gateway_chat_source_mentions_offline_warning_type():
    assert "approval_type = \"approval_gateway_offline\"" in GATEWAY_CHAT
    assert "approval_type = \"approval_gateway_unsupported\"" in GATEWAY_CHAT
    assert "approval_message = \"Gateway connection failed. Check that the connected Hermes gateway is running and reachable.\"" in GATEWAY_CHAT
