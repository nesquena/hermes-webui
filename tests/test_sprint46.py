"""
Sprint 46 Tests: manual session compression with optional focus topic.
"""

import contextlib
import io
import json
import sys
import threading
import time
import types

from api.models import Session
from api.config import SESSION_DIR
from api.routes import _handle_session_compress, get_session
from tests._pytest_port import BASE


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


def _install_fake_compression_runtime(monkeypatch, agent_cls):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
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
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_provider)
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


def _make_session(messages=None):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    messages = messages or [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]
    s = Session(
        session_id=f"compress_test_{time.time_ns()}",
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

    _install_fake_compression_runtime(monkeypatch, _FakeAgent)

    handler = _FakeHandler()
    _handle_session_compress(handler, {"session_id": sid, "focus_topic": "database schema"})

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
    assert payload["session"]["compression_anchor_summary"] is not None
    assert payload["session"]["compression_anchor_visible_idx"] == 1
    assert isinstance(payload["session"]["compression_anchor_message_key"], dict)
    assert payload["session"]["compression_anchor_message_key"].get("role") == "assistant"
    loaded = get_session(sid)
    assert loaded.compression_anchor_summary == payload["session"]["compression_anchor_summary"]
    assert loaded.compression_anchor_visible_idx == payload["session"]["compression_anchor_visible_idx"]
    assert loaded.compression_anchor_message_key == payload["session"]["compression_anchor_message_key"]
    assert _FakeAgent.last_instance is not None
    assert _FakeAgent.last_instance.context_compressor.calls[0]["focus_topic"] == "database schema"


def test_session_compress_start_is_async_and_reuses_running_job(monkeypatch, cleanup_test_sessions):
    import api.routes as routes

    assert hasattr(routes, "_handle_session_compress_start")
    assert hasattr(routes, "_handle_session_compress_status")

    class BlockingCompressor:
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def compress(self, messages, current_tokens=None, focus_topic=None):
            self.calls.append({"messages": list(messages), "focus_topic": focus_topic})
            self.entered.set()
            assert self.release.wait(timeout=5), "test timed out waiting to release compression"
            return [messages[0], messages[-1]]

    class BlockingAgent:
        instances = []

        def __init__(self, **kwargs):
            self.context_compressor = BlockingCompressor()
            self.instances.append(self)

    created = cleanup_test_sessions
    sid = _make_session()
    created.append(sid)
    _install_fake_compression_runtime(monkeypatch, BlockingAgent)
    try:
        first = _FakeHandler()
        routes._handle_session_compress_start(first, {"session_id": sid, "focus_topic": "slow"})
        assert first.status == 200
        first_payload = first.payload()
        assert first_payload["ok"] is True
        assert first_payload["status"] == "running"
        assert first_payload["session_id"] == sid
        assert first_payload["focus_topic"] == "slow"
        assert BlockingCompressor.entered.wait(timeout=2)

        second = _FakeHandler()
        routes._handle_session_compress_start(second, {"session_id": sid, "focus_topic": "slow"})
        assert second.status == 200
        second_payload = second.payload()
        assert second_payload["status"] == "running"
        assert len(BlockingAgent.instances) == 1

        running = _FakeHandler()
        routes._handle_session_compress_status(running, sid)
        assert running.status == 200
        assert running.payload()["status"] == "running"
    finally:
        BlockingCompressor.release.set()

    deadline = time.time() + 5
    done_payload = None
    while time.time() < deadline:
        done = _FakeHandler()
        routes._handle_session_compress_status(done, sid)
        payload = done.payload()
        if payload["status"] == "done":
            done_payload = payload
            break
        time.sleep(0.02)
    assert done_payload is not None
    assert done_payload["summary"]["headline"] == "Compressed: 4 → 2 messages"
    assert done_payload["session"]["messages"] == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "four"},
    ]


def test_session_compress_status_reports_worker_error_without_raw_paths(monkeypatch, cleanup_test_sessions):
    import api.routes as routes

    assert hasattr(routes, "_handle_session_compress_start")
    assert hasattr(routes, "_handle_session_compress_status")

    class FailingCompressor:
        entered = threading.Event()

        def compress(self, messages, current_tokens=None, focus_topic=None):
            self.entered.set()
            raise RuntimeError("provider log at /Users/alice/.hermes/secrets/token.txt failed")

    class FailingAgent:
        def __init__(self, **kwargs):
            self.context_compressor = FailingCompressor()

    created = cleanup_test_sessions
    sid = _make_session()
    created.append(sid)
    _install_fake_compression_runtime(monkeypatch, FailingAgent)

    start = _FakeHandler()
    routes._handle_session_compress_start(start, {"session_id": sid})
    assert start.status == 200
    assert FailingCompressor.entered.wait(timeout=2)

    deadline = time.time() + 5
    error_payload = None
    while time.time() < deadline:
        status = _FakeHandler()
        routes._handle_session_compress_status(status, sid)
        payload = status.payload()
        if payload["status"] == "error":
            error_payload = payload
            break
        time.sleep(0.02)
    assert error_payload is not None
    assert error_payload["ok"] is False
    assert error_payload["error_status"] == 400
    assert "<path>" in error_payload["error"]
    assert "/Users/alice" not in error_payload["error"]


def test_session_compress_start_retries_after_terminal_error(monkeypatch, cleanup_test_sessions):
    import api.routes as routes

    class BlockingCompressor:
        entered = threading.Event()
        release = threading.Event()

        def compress(self, messages, current_tokens=None, focus_topic=None):
            self.entered.set()
            assert self.release.wait(timeout=5), "test timed out waiting to release compression"
            return [messages[0], messages[-1]]

    class BlockingAgent:
        def __init__(self, **kwargs):
            self.context_compressor = BlockingCompressor()

    created = cleanup_test_sessions
    sid = _make_session()
    created.append(sid)
    _install_fake_compression_runtime(monkeypatch, BlockingAgent)

    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        routes._MANUAL_COMPRESSION_JOBS[sid] = {
            "session_id": sid,
            "focus_topic": None,
            "status": "error",
            "error": "previous failure",
            "error_status": 400,
            "started_at": time.time(),
            "updated_at": time.time(),
        }

    try:
        retry = _FakeHandler()
        routes._handle_session_compress_start(retry, {"session_id": sid})
        assert retry.status == 200
        retry_payload = retry.payload()
        assert retry_payload["status"] == "running"
        assert retry_payload["ok"] is True
        assert BlockingCompressor.entered.wait(timeout=2)
    finally:
        BlockingCompressor.release.set()


def test_session_compress_async_reports_stale_session_guard(monkeypatch, cleanup_test_sessions):
    import api.routes as routes

    created = cleanup_test_sessions
    sid = _make_session()
    created.append(sid)

    class MutatingCompressor:
        entered = threading.Event()

        def compress(self, messages, current_tokens=None, focus_topic=None):
            live = get_session(sid)
            live.messages.append({"role": "user", "content": "concurrent edit"})
            self.entered.set()
            return [messages[0], messages[-1]]

    class MutatingAgent:
        def __init__(self, **kwargs):
            self.context_compressor = MutatingCompressor()

    _install_fake_compression_runtime(monkeypatch, MutatingAgent)

    start = _FakeHandler()
    routes._handle_session_compress_start(start, {"session_id": sid})
    assert start.status == 200
    assert MutatingCompressor.entered.wait(timeout=2)

    deadline = time.time() + 5
    error_payload = None
    while time.time() < deadline:
        status = _FakeHandler()
        routes._handle_session_compress_status(status, sid)
        payload = status.payload()
        if payload["status"] == "error":
            error_payload = payload
            break
        time.sleep(0.02)
    assert error_payload is not None
    assert error_payload["ok"] is False
    assert error_payload["error_status"] == 409
    assert "modified during compression" in error_payload["error"]
    assert get_session(sid).messages[-1]["content"] == "concurrent edit"


def test_session_compress_async_reports_stream_state_guard(monkeypatch, cleanup_test_sessions):
    import api.routes as routes

    created = cleanup_test_sessions
    sid = _make_session()
    created.append(sid)

    class StreamMutatingCompressor:
        entered = threading.Event()

        def compress(self, messages, current_tokens=None, focus_topic=None):
            live = get_session(sid)
            live.active_stream_id = "stream-concurrent"
            self.entered.set()
            return [messages[0], messages[-1]]

    class StreamMutatingAgent:
        def __init__(self, **kwargs):
            self.context_compressor = StreamMutatingCompressor()

    _install_fake_compression_runtime(monkeypatch, StreamMutatingAgent)

    start = _FakeHandler()
    routes._handle_session_compress_start(start, {"session_id": sid})
    assert start.status == 200
    assert StreamMutatingCompressor.entered.wait(timeout=2)

    deadline = time.time() + 5
    error_payload = None
    while time.time() < deadline:
        status = _FakeHandler()
        routes._handle_session_compress_status(status, sid)
        payload = status.payload()
        if payload["status"] == "error":
            error_payload = payload
            break
        time.sleep(0.02)
    assert error_payload is not None
    assert error_payload["ok"] is False
    assert error_payload["error_status"] == 409
    assert "stream state changed" in error_payload["error"]
    assert get_session(sid).active_stream_id == "stream-concurrent"


def test_static_commands_js_registers_compress_alias(cleanup_test_sessions):
    from pathlib import Path

    with open(Path(__file__).resolve().parents[1] / "static" / "commands.js", encoding="utf-8") as f:
        src = f.read()
    assert "name:'compress'" in src
    assert "name:'compact'" in src
    assert "/api/session/compress/start" in src
    assert "/api/session/compress/status" in src
    assert "await api('/api/session/compress'," not in src
    assert "beforeCount:visibleCount" in src
    assert "cmdCompress" in src
    assert "cmdCompact" in src


def test_static_commands_js_prefers_persisted_reference_message(cleanup_test_sessions):
    from pathlib import Path

    with open(Path(__file__).resolve().parents[1] / "static" / "commands.js", encoding="utf-8") as f:
        src = f.read()

    assert "const messageRef=referenceMsg?msgContent(referenceMsg)||String(referenceMsg.content||''):'';" in src
    assert "const referenceText=messageRef || summaryRef;" in src


def test_static_session_load_resumes_manual_compression_polling(cleanup_test_sessions):
    from pathlib import Path

    with open(Path(__file__).resolve().parents[1] / "static" / "sessions.js", encoding="utf-8") as f:
        src = f.read()

    assert "resumeManualCompressionForSession" in src
