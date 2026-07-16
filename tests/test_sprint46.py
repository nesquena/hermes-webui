"""
Sprint 46 Tests: manual session compression with optional focus topic.
"""

import contextlib
import io
import json
import os
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


def _make_session(messages=None, tool_calls=None):
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
        tool_calls=tool_calls or [],
    )
    s.save(touch_updated_at=False)
    return s.session_id


def test_session_compress_requires_session_id(cleanup_test_sessions):
    handler = _FakeHandler()
    _handle_session_compress(handler, {})
    assert handler.status == 400
    assert handler.payload()["error"] == "Missing required field(s): session_id"


def test_session_compress_stale_runtime_returns_typed_409_before_mutation(
    monkeypatch, cleanup_test_sessions
):
    import api.routes as routes

    sid = _make_session()
    cleanup_test_sessions.append(sid)
    loaded_before = Session.load(sid)
    assert loaded_before is not None
    before = loaded_before.compact()
    monkeypatch.setattr(
        routes,
        "ensure_agent_runtime_current",
        lambda: (_ for _ in ()).throw(
            routes.AgentRuntimeChangedError("restart required")
        ),
    )

    handler = _FakeHandler()
    _handle_session_compress(handler, {"session_id": sid})

    assert handler.status == 409
    assert handler.payload() == {
        "error": "restart required",
        "type": "agent_runtime_stale",
        "retryable": True,
    }
    loaded_after = Session.load(sid)
    assert loaded_after is not None
    assert loaded_after.compact() == before


def test_session_compress_start_stale_runtime_returns_409_before_job_creation(
    monkeypatch, cleanup_test_sessions
):
    import api.routes as routes

    sid = _make_session()
    cleanup_test_sessions.append(sid)
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        routes._MANUAL_COMPRESSION_JOBS.pop(sid, None)

    monkeypatch.setattr(
        routes,
        "ensure_agent_runtime_current",
        lambda: (_ for _ in ()).throw(
            routes.AgentRuntimeChangedError("restart required")
        ),
    )

    handler = _FakeHandler()
    routes._handle_session_compress_start(handler, {"session_id": sid})

    assert handler.status == 409
    assert json.loads(handler.wfile.getvalue().decode("utf-8")) == {
        "error": "restart required",
        "type": "agent_runtime_stale",
        "retryable": True,
    }
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        assert sid not in routes._MANUAL_COMPRESSION_JOBS


def test_session_compress_start_reuses_running_job_when_runtime_is_stale(
    monkeypatch, cleanup_test_sessions
):
    import api.routes as routes

    sid = _make_session()
    cleanup_test_sessions.append(sid)
    existing = {
        "session_id": sid,
        "focus_topic": "already running",
        "status": "running",
        "started_at": time.time(),
        "updated_at": time.time(),
    }
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        routes._MANUAL_COMPRESSION_JOBS[sid] = existing
    monkeypatch.setattr(
        routes,
        "ensure_agent_runtime_current",
        lambda: (_ for _ in ()).throw(
            routes.AgentRuntimeChangedError("restart required")
        ),
    )

    handler = _FakeHandler()
    routes._handle_session_compress_start(handler, {"session_id": sid})

    assert handler.status == 200
    payload = handler.payload()
    assert payload["status"] == "running"
    assert payload["session_id"] == sid
    assert payload["focus_topic"] == "already running"
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        assert routes._MANUAL_COMPRESSION_JOBS[sid] is existing
        routes._MANUAL_COMPRESSION_JOBS.pop(sid, None)


def test_session_compress_start_rechecks_job_after_runtime_barrier(
    monkeypatch, cleanup_test_sessions
):
    import api.routes as routes

    sid = _make_session()
    cleanup_test_sessions.append(sid)
    admitted = {
        "session_id": sid,
        "focus_topic": "admitted concurrently",
        "status": "running",
        "started_at": time.time(),
        "updated_at": time.time(),
    }

    def barrier_then_admit():
        with routes._MANUAL_COMPRESSION_JOBS_LOCK:
            routes._MANUAL_COMPRESSION_JOBS[sid] = admitted

    def reject_duplicate_worker(**_kwargs):
        raise AssertionError("duplicate worker admitted")

    monkeypatch.setattr(routes, "ensure_agent_runtime_current", barrier_then_admit)
    monkeypatch.setattr(routes.threading, "Thread", reject_duplicate_worker)

    handler = _FakeHandler()
    routes._handle_session_compress_start(handler, {"session_id": sid})

    assert handler.status == 200
    assert handler.payload()["status"] == "running"
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        routes._MANUAL_COMPRESSION_JOBS.pop(sid, None)


def test_session_compress_worker_preserves_stale_runtime_taxonomy(monkeypatch):
    import api.routes as routes

    sid = "stale-worker-session"
    started_at = time.time()
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        routes._MANUAL_COMPRESSION_JOBS[sid] = {
            "session_id": sid,
            "status": "running",
            "started_at": started_at,
            "updated_at": started_at,
        }

    monkeypatch.setattr(
        routes,
        "_handle_session_compress",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            routes.AgentRuntimeChangedError("restart required")
        ),
    )

    routes._run_manual_compression_job(sid, {"session_id": sid})

    handler = _FakeHandler()
    routes._handle_session_compress_status(handler, sid)
    payload = handler.payload()
    assert handler.status == 200
    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["session_id"] == sid
    assert payload["error"] == "restart required"
    assert payload["error_status"] == 409
    assert payload["type"] == "agent_runtime_stale"
    assert payload["retryable"] is True
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        routes._MANUAL_COMPRESSION_JOBS.pop(sid, None)


def test_session_compress_roundtrip(monkeypatch, cleanup_test_sessions):
    created = cleanup_test_sessions
    original_messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]
    compressed_messages = [original_messages[0], original_messages[-1]]
    settled_tool_calls = [
        {
            "id": "call_1",
            "name": "terminal",
            "assistant_msg_idx": 1,
            "done": True,
            "result": "schema.sql",
        }
    ]
    sid = _make_session(original_messages, tool_calls=settled_tool_calls)
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
    assert payload["session"]["messages"] == original_messages
    assert payload["session"]["tool_calls"] == settled_tool_calls
    assert payload["session"]["message_count"] == len(original_messages)
    assert payload["session"]["compression_anchor_summary"] is not None
    assert payload["session"]["compression_anchor_visible_idx"] == 3
    assert isinstance(payload["session"]["compression_anchor_message_key"], dict)
    assert payload["session"]["compression_anchor_message_key"].get("role") == "assistant"
    assert payload["session"]["compression_anchor_message_key"].get("text") == "four"
    loaded = get_session(sid)
    persisted = Session.load(sid)
    assert loaded.messages == original_messages
    assert [m.get("role") for m in loaded.context_messages] == [m.get("role") for m in compressed_messages]
    assert [m.get("content") for m in loaded.context_messages] == [m.get("content") for m in compressed_messages]
    assert loaded.tool_calls == settled_tool_calls
    assert persisted.messages == original_messages
    assert [m.get("role") for m in persisted.context_messages] == [m.get("role") for m in compressed_messages]
    assert [m.get("content") for m in persisted.context_messages] == [m.get("content") for m in compressed_messages]
    assert persisted.tool_calls == settled_tool_calls
    assert loaded.compression_anchor_summary == payload["session"]["compression_anchor_summary"]
    assert loaded.compression_anchor_visible_idx == payload["session"]["compression_anchor_visible_idx"]
    assert loaded.compression_anchor_message_key == payload["session"]["compression_anchor_message_key"]
    assert loaded.compression_anchor_mode == "manual"
    assert loaded.truncation_watermark is not None
    assert loaded.truncation_boundary == loaded.truncation_watermark
    assert loaded.last_prompt_tokens is not None
    assert persisted.compression_anchor_mode == "manual"
    assert persisted.truncation_watermark == loaded.truncation_watermark
    assert persisted.truncation_boundary == loaded.truncation_boundary
    assert not (SESSION_DIR / f"{sid}.json.bak").exists()
    assert persisted.compression_anchor_visible_idx == 3
    assert persisted.compression_anchor_message_key == payload["session"]["compression_anchor_message_key"]
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
    settled_tool_calls = [
        {
            "id": "call_async_1",
            "name": "read_file",
            "assistant_msg_idx": 1,
            "done": True,
            "result": "config.yaml",
        }
    ]
    sid = _make_session(tool_calls=settled_tool_calls)
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
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]
    assert done_payload["session"]["tool_calls"] == settled_tool_calls
    persisted = Session.load(sid)
    assert persisted.tool_calls == settled_tool_calls
    # /compress now stamps missing message timestamps, so compare role/content
    # rather than full-dict equality (mirrors test_session_compress_roundtrip).
    assert [m.get("role") for m in persisted.context_messages] == ["user", "assistant"]
    assert [m.get("content") for m in persisted.context_messages] == ["one", "four"]


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


def test_manual_compress_worker_uses_session_profile_env(monkeypatch, tmp_path, cleanup_test_sessions):
    import api.profiles as profiles
    import api.routes as routes

    class EnvAssertingAgent:
        seen_env = None

        def __init__(self, **kwargs):
            from api.config import _thread_ctx

            skill_module = sys.modules.get("tools.skills_tool")
            thread_env = getattr(_thread_ctx, "env", {})
            EnvAssertingAgent.seen_env = {
                "HERMES_HOME": os.environ.get("HERMES_HOME"),
                "HERMES_TEST_PROFILE_ENV": os.environ.get("HERMES_TEST_PROFILE_ENV"),
                "THREAD_HERMES_HOME": thread_env.get("HERMES_HOME"),
                "THREAD_HERMES_TEST_PROFILE_ENV": thread_env.get("HERMES_TEST_PROFILE_ENV"),
                "SKILL_MODULE_HOME": getattr(skill_module, "HERMES_HOME", None),
                "SKILL_MODULE_DIR": getattr(skill_module, "SKILLS_DIR", None),
            }
            self.context_compressor = _FakeCompressor()

    created = cleanup_test_sessions
    sid = _make_session()
    created.append(sid)
    session = get_session(sid)
    session.profile = "work"
    session.model_provider = "profile-provider"
    session.save(touch_updated_at=False)

    profile_home = tmp_path / "work-profile-home"
    fake_skill_module = types.ModuleType("tools.skills_tool")
    setattr(fake_skill_module, "HERMES_HOME", "default-home")
    setattr(fake_skill_module, "SKILLS_DIR", "default-home/skills")
    monkeypatch.setitem(sys.modules, "tools.skills_tool", fake_skill_module)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda profile: profile_home)
    monkeypatch.setattr(
        profiles,
        "get_profile_runtime_env",
        lambda home: {"HERMES_TEST_PROFILE_ENV": "work-runtime"},
    )
    monkeypatch.setenv("HERMES_HOME", "default-home")
    monkeypatch.delenv("HERMES_TEST_PROFILE_ENV", raising=False)
    _install_fake_compression_runtime(monkeypatch, EnvAssertingAgent)

    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        routes._MANUAL_COMPRESSION_JOBS[sid] = {
            "session_id": sid,
            "focus_topic": None,
            "status": "running",
            "started_at": time.time(),
            "updated_at": time.time(),
        }

    routes._run_manual_compression_job(sid, {"session_id": sid})

    assert EnvAssertingAgent.seen_env == {
        "HERMES_HOME": str(profile_home),
        "HERMES_TEST_PROFILE_ENV": "work-runtime",
        "THREAD_HERMES_HOME": str(profile_home),
        "THREAD_HERMES_TEST_PROFILE_ENV": "work-runtime",
        "SKILL_MODULE_HOME": profile_home,
        "SKILL_MODULE_DIR": profile_home / "skills",
    }
    assert str(getattr(fake_skill_module, "HERMES_HOME")) == "default-home"
    assert str(getattr(fake_skill_module, "SKILLS_DIR")) == "default-home/skills"
    assert os.environ.get("HERMES_HOME") == "default-home"
    assert os.environ.get("HERMES_TEST_PROFILE_ENV") is None
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        assert routes._MANUAL_COMPRESSION_JOBS[sid]["status"] == "done"


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
