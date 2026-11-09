"""Manual compression asks the chat's own profile whether it is gateway-backed (#8860 fold).

``/compress/start`` checks the gateway backend on the request thread, where the
profile cookie selects the chat's profile. The job then runs on a worker thread
that carries the profile env but not the per-request profile, so the second
``/compress`` guard inside ``_handle_session_compress`` read the process-default
profile's config. With a gateway default profile and a local chat profile the
start call answered 200 and the job then failed with the gateway refusal.

These tests use two real profile homes with different ``webui_chat_backend``
values and drive the real start route, worker and handler.
"""
import threading

import pytest

from api import config, oauth, profiles, routes
from tests.test_sprint46 import _FakeAgent, _FakeHandler, _make_session

_REFUSAL = "/compress is unavailable on gateway-backed sessions"


@pytest.fixture
def profile_pair(tmp_path, monkeypatch, cleanup_test_sessions):
    """Root profile plus a named ``work`` profile; returns a backend setter."""
    root = tmp_path / "profiles-root"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(root))
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    cache = {}
    monkeypatch.setattr(config, "_cfg_cache", cache)
    monkeypatch.setattr(config, "cfg", cache)
    monkeypatch.setattr(config, "_cfg_path", None)
    monkeypatch.setattr(config, "_cfg_mtime", 0.0)
    monkeypatch.setattr(routes, "ensure_agent_runtime_current", lambda: None)
    monkeypatch.setattr(routes, "require_ai_agent_class", lambda: _FakeAgent)
    monkeypatch.setattr(
        config,
        "resolve_model_provider",
        lambda model: ("test-model", "openai", "https://example.invalid/v1"),
    )
    monkeypatch.setattr(
        oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda *a, **kw: {
            "provider": "openai",
            "api_key": "fake-key",
            "base_url": "https://example.invalid/v1",
        },
    )
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])

    def set_backends(default_backend, work_backend):
        (root / "config.yaml").write_text(f"webui_chat_backend: {default_backend}\n")
        (work / "config.yaml").write_text(f"webui_chat_backend: {work_backend}\n")

    yield set_backends
    profiles.clear_request_profile()


def _work_session():
    sid = _make_session()
    session = routes.get_session(sid)
    session.profile = "work"
    session.save(touch_updated_at=False)
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        routes._MANUAL_COMPRESSION_JOBS.pop(sid, None)
    return sid, session


def _as_work_request(fn, *args):
    """Run a route handler the way a request with the ``work`` profile cookie does."""
    profiles.set_request_profile("work")
    try:
        return fn(*args)
    finally:
        profiles.clear_request_profile()


def _job(sid):
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        return dict(routes._MANUAL_COMPRESSION_JOBS.get(sid) or {})


def test_local_chat_profile_compresses_when_default_profile_is_gateway(profile_pair, monkeypatch):
    profile_pair("gateway", "webui")
    sid, session = _work_session()
    workers = []
    real_thread = threading.Thread

    def record_thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(routes.threading, "Thread", record_thread)
    handler = _FakeHandler()
    _as_work_request(routes._handle_session_compress_start, handler, {"session_id": sid})
    assert handler.status == 200, handler.payload()
    assert workers, "the start route did not launch a compression worker"
    for worker in workers:
        worker.join(15)
        assert not worker.is_alive()

    job = _job(sid)
    assert job.get("status") == "done", job
    assert job.get("error_status") is None, job
    assert len(routes.get_session(sid).context_messages or []) > 0


def test_gateway_chat_profile_is_refused_at_start_when_default_profile_is_local(profile_pair):
    profile_pair("webui", "gateway")
    sid, _session = _work_session()
    handler = _FakeHandler()
    _as_work_request(routes._handle_session_compress_start, handler, {"session_id": sid})
    assert handler.status == 409
    payload = handler.payload()
    assert payload["code"] == "gateway_mode_unsupported"
    assert payload["error"] == _REFUSAL
    assert _job(sid) == {}, "a refused start must not create a compression job"


def test_sync_compress_route_still_refuses_a_gateway_backed_chat(profile_pair):
    profile_pair("webui", "gateway")
    sid, _session = _work_session()
    handler = _FakeHandler()
    _as_work_request(routes._handle_session_compress, handler, {"session_id": sid})
    assert handler.status == 409
    assert handler.payload()["code"] == "gateway_mode_unsupported"


def test_worker_guard_still_refuses_when_the_chat_profile_is_gateway(profile_pair):
    """The worker's own guard asks the chat's profile, not the default (which is local here)."""
    profile_pair("webui", "gateway")
    sid, _session = _work_session()
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        routes._MANUAL_COMPRESSION_JOBS[sid] = {
            "session_id": sid,
            "focus_topic": None,
            "status": "running",
            "started_at": 0.0,
            "updated_at": 0.0,
        }
    routes._run_manual_compression_job(sid, {"session_id": sid})
    job = _job(sid)
    assert job.get("status") == "error", job
    assert job.get("error_status") == 409, job
    assert job.get("error") == _REFUSAL, job


def test_external_config_override_is_honoured_like_the_start_route(profile_pair, tmp_path, monkeypatch):
    """``HERMES_CONFIG_PATH`` is what ``/compress/start`` reads for every profile; the worker must agree."""
    profile_pair("gateway", "gateway")
    override = tmp_path / "external-config.yaml"
    override.write_text("webui_chat_backend: webui\n")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(override))
    sid, _session = _work_session()
    workers = []
    real_thread = threading.Thread

    def record_thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(routes.threading, "Thread", record_thread)
    handler = _FakeHandler()
    _as_work_request(routes._handle_session_compress_start, handler, {"session_id": sid})
    assert handler.status == 200, handler.payload()
    for worker in workers:
        worker.join(15)
        assert not worker.is_alive()
    job = _job(sid)
    assert job.get("status") == "done", job
