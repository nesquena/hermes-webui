"""
Sprint 46 Tests: manual session compression with optional focus topic.
"""

import contextlib
import io
import json
import sys
import types

from api.models import Session
from api.config import SESSION_DIR
from api.routes import _handle_session_compress


class _FakeHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class _FakeCompressor:
    def __init__(self):
        self.calls = []

    def compress(self, messages, current_tokens=None, focus_topic=None):
        self.calls.append(
            {
                "messages": list(messages),
                "current_tokens": current_tokens,
                "focus_topic": focus_topic,
            }
        )
        if len(messages) >= 2:
            return [messages[0], messages[-1]]
        return list(messages)


class _FakeAgent:
    last_instance = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.context_compressor = _FakeCompressor()
        _FakeAgent.last_instance = self


def _make_session(messages=None):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    messages = messages or [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]
    s = Session(
        session_id="compress_test_001",
        title="Untitled",
        workspace="/tmp/hermes-webui-test",
        model="openai/gpt-5.4-mini",
        messages=messages,
    )
    s.save(touch_updated_at=False)
    return s.session_id


def test_session_compress_requires_session_id(cleanup_test_sessions):
    handler = _FakeHandler()
    _handle_session_compress(handler, {})
    assert handler.status == 400
    assert handler.payload()["error"] == "Missing required field(s): session_id"


def test_session_compress_roundtrip(monkeypatch, cleanup_test_sessions):
    created = cleanup_test_sessions
    sid = _make_session()
    created.append(sid)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    import api.config as _cfg

    fake_runtime_provider = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_provider.resolve_runtime_provider = lambda requested=None: {
        "api_key": "fake-key",
        "provider": requested or "openai",
        "base_url": "https://api.openai.com/v1",
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__path__ = []
    fake_hermes_cli.runtime_provider = fake_runtime_provider
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(
        sys.modules, "hermes_cli.runtime_provider", fake_runtime_provider
    )
    import hermes_cli.runtime_provider as _rtp

    monkeypatch.setattr(
        _cfg,
        "resolve_model_provider",
        lambda model: ("openai/gpt-5.4-mini", "openai", "https://api.openai.com/v1"),
    )
    monkeypatch.setattr(
        _cfg,
        "_get_session_agent_lock",
        lambda sid: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        _rtp,
        "resolve_runtime_provider",
        lambda requested=None: {
            "api_key": "fake-key",
            "provider": requested or "openai",
            "base_url": "https://api.openai.com/v1",
        },
    )

    handler = _FakeHandler()
    _handle_session_compress(
        handler, {"session_id": sid, "focus_topic": "database schema"}
    )

    assert handler.status == 200
    payload = handler.payload()
    assert payload["ok"] is True
    assert payload["focus_topic"] == "database schema"
    assert payload["summary"]["headline"] == "Compressed: 4 → 2 messages"
    assert payload["session"]["session_id"] == sid
    assert payload["session"]["messages"] == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "four"},
    ]
    assert _FakeAgent.last_instance is not None
    assert (
        _FakeAgent.last_instance.context_compressor.calls[0]["focus_topic"]
        == "database schema"
    )


def test_static_commands_js_registers_compress_alias(cleanup_test_sessions):
    from pathlib import Path

    with open(
        Path(__file__).resolve().parents[1] / "static" / "commands.js", encoding="utf-8"
    ) as f:
        src = f.read()
    assert "name:'compress'" in src
    assert "name:'compact'" in src
    assert "/api/session/compress" in src
    assert "cmdCompress" in src
    assert "cmdCompact" in src


def test_static_commands_js_prefers_persisted_reference_message(cleanup_test_sessions):
    from pathlib import Path

    with open(
        Path(__file__).resolve().parents[1] / "static" / "commands.js", encoding="utf-8"
    ) as f:
        src = f.read()

    assert (
        "const messageRef=referenceMsg?msgContent(referenceMsg)||String(referenceMsg.content||''):'';"
        in src
    )
    assert "const referenceText=messageRef || summaryRef;" in src
