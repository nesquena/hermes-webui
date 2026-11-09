"""Manual compression trusts the gateway check made by ``/compress/start`` (#8860 fold).

``/compress/start`` checks the gateway backend on the request thread, where the
profile cookie selects the chat's profile, and only then starts the worker. The
PR's second ``/compress`` guard inside ``_handle_session_compress`` also ran on
that worker thread, which carries the profile env but not the per-request
profile, so it re-read the process-default profile's config: with a gateway
default profile and a local chat profile the start call answered 200 and the job
then failed with the gateway refusal.

The worker now marks its in-memory handler as admitted and the handler skips the
guard for that marker only; the synchronous ``/api/session/compress`` route keeps
the guard exactly. These tests use two real profile homes and drive the real
start route, worker and handler.
"""
import ast
import threading
import types
from pathlib import Path

import pytest

from api import config, oauth, profiles, routes
from tests.test_sprint46 import _FakeAgent, _FakeHandler, _make_session

_REFUSAL = "/compress is unavailable on gateway-backed sessions"
_MARKER = "gateway_admission_checked"
_REPO = Path(__file__).resolve().parent.parent


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


@pytest.fixture
def workers(monkeypatch):
    """Record every thread the start route launches."""
    started = []
    real_thread = threading.Thread

    def record_thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        started.append(worker)
        return worker

    monkeypatch.setattr(routes.threading, "Thread", record_thread)
    return started


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


def _start_and_finish(sid, workers):
    handler = _FakeHandler()
    _as_work_request(routes._handle_session_compress_start, handler, {"session_id": sid})
    assert handler.status == 200, handler.payload()
    assert workers, "the start route did not launch a compression worker"
    for worker in workers:
        worker.join(15)
        assert not worker.is_alive()
    return _job(sid)


def test_local_chat_profile_compresses_when_default_profile_is_gateway(profile_pair, workers):
    profile_pair("gateway", "webui")
    sid, _session = _work_session()
    job = _start_and_finish(sid, workers)
    assert job.get("status") == "done", job
    assert job.get("error_status") is None, job
    assert len(routes.get_session(sid).context_messages or []) > 0


def test_gateway_chat_profile_is_refused_at_start_when_default_profile_is_local(profile_pair, workers):
    profile_pair("webui", "gateway")
    sid, _session = _work_session()
    handler = _FakeHandler()
    _as_work_request(routes._handle_session_compress_start, handler, {"session_id": sid})
    assert handler.status == 409
    payload = handler.payload()
    assert payload["code"] == "gateway_mode_unsupported"
    assert payload["error"] == _REFUSAL
    assert _job(sid) == {}, "a refused start must not create a compression job"
    assert workers == [], "a refused start must not launch a worker"


def test_sync_compress_route_still_refuses_a_gateway_backed_chat(profile_pair):
    profile_pair("webui", "gateway")
    sid, _session = _work_session()
    handler = _FakeHandler()
    _as_work_request(routes._handle_session_compress, handler, {"session_id": sid})
    assert handler.status == 409
    assert handler.payload()["code"] == "gateway_mode_unsupported"


def test_external_config_override_job_completes_when_admitted(profile_pair, tmp_path, monkeypatch, workers):
    """``HERMES_CONFIG_PATH`` selects local mode at admission; the job follows that verdict."""
    profile_pair("gateway", "gateway")
    override = tmp_path / "external-config.yaml"
    override.write_text("webui_chat_backend: webui\n")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(override))
    sid, _session = _work_session()
    job = _start_and_finish(sid, workers)
    assert job.get("status") == "done", job


def _functions_by_line(tree):
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((node.lineno, node.end_lineno, node.name))
    return spans


def _enclosing(spans, lineno):
    inside = [s for s in spans if s[0] <= lineno <= s[1]]
    return min(inside, key=lambda s: s[1] - s[0])[2] if inside else "<module>"


def _source_trees():
    paths = sorted((_REPO / "api").glob("*.py")) + [_REPO / "server.py"]
    for path in paths:
        if path.exists():
            yield path, ast.parse(path.read_text(encoding="utf-8"))


def test_worker_is_reached_only_through_a_passing_compress_start():
    """The worker trusts admission, so its only entry must sit behind the start route's guard."""
    references = []
    for path, tree in _source_trees():
        spans = _functions_by_line(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "_run_manual_compression_job":
                references.append((path.name, _enclosing(spans, node.lineno), node.lineno))
    assert references, "no reference to _run_manual_compression_job found"
    assert {(f, fn) for f, fn, _ in references} == {("routes.py", "_handle_session_compress_start")}, references

    tree = ast.parse((_REPO / "api" / "routes.py").read_text(encoding="utf-8"))
    start = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_handle_session_compress_start"
    )
    guard_lines = [
        n.lineno for n in ast.walk(start)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "refuse_on_gateway_backend"
    ]
    worker_lines = [line for _f, _fn, line in references]
    assert guard_lines and max(guard_lines) < min(worker_lines), (guard_lines, worker_lines)


def test_guard_is_skipped_only_for_the_worker_marker(profile_pair):
    profile_pair("webui", "gateway")

    def run(handler):
        _as_work_request(routes._handle_session_compress, handler, {"session_id": "no-such-session"})
        return handler.status

    assert run(_FakeHandler()) == 409
    truthy = _FakeHandler()
    setattr(truthy, _MARKER, "yes")
    assert run(truthy) == 409, "only the exact True set by the worker skips the guard"
    marked = _FakeHandler()
    setattr(marked, _MARKER, True)
    assert run(marked) != 409

    assert not hasattr(routes._ManualCompressionMemoryHandler(), _MARKER)
    assert not hasattr(types.SimpleNamespace(), _MARKER)
    uses = []
    for path, tree in _source_trees():
        spans = _functions_by_line(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == _MARKER:
                uses.append((path.name, _enclosing(spans, node.lineno), type(node.ctx).__name__))
            elif isinstance(node, ast.Constant) and node.value == _MARKER:
                uses.append((path.name, _enclosing(spans, node.lineno), "Str"))
    assert sorted(uses) == [
        ("routes.py", "_handle_session_compress", "Str"),
        ("routes.py", "_run_manual_compression_job", "Store"),
    ], uses
