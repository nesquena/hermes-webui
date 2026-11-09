"""#8970: the manual-compression worker reads the SESSION profile's WebUI config.

``POST /api/session/compress/start`` runs ``_run_manual_compression_job`` on a
new daemon thread. That thread has no per-request profile thread-local, so on
master every TLS-resolved WebUI read on it (the view-only guard's state.db,
the #8690 provider-ambiguity verdict, the named ``custom:<slug>`` record, the
helper agent's toolsets) resolves the process-default profile, and the
model/provider resolvers read the shared ``cfg`` alias, i.e. whichever profile
last refilled the process-wide config cache.

These tests use two real profile homes (root ``default`` and named ``work``)
and drive the real ``_run_manual_compression_job`` on a fresh thread. Only the
LLM side is stubbed: the ``AIAgent`` class (its compressor) and the Agent's
runtime credential resolver.
"""

import copy
import json
import sys
import threading
import time
import types

import pytest


ROOT_CONFIG = """\
model:
  provider: openrouter
  default: probe-model
platform_toolsets:
  cli: [web]
custom_providers:
  - name: lab
    base_url: http://default-lab.invalid/v1
    api_key: root-lab-key
    model: lab-model
  - name: anthropic
    base_url: http://root-anthropic-proxy.invalid/v1
    api_key: root-anthropic-key
"""

WORK_CONFIG = """\
model:
  provider: deepseek
  default: probe-model
platform_toolsets:
  cli: [terminal, file]
custom_providers:
  - name: lab
    base_url: http://work-lab.invalid/v1
    api_key: work-lab-key
    model: lab-model
"""

_MESSAGES = [
    {"role": "user", "content": "one"},
    {"role": "assistant", "content": "two"},
    {"role": "user", "content": "three"},
    {"role": "assistant", "content": "four"},
]


class _FakeCompressor:
    def compress(self, messages, current_tokens=None, focus_topic=None):
        return [messages[0], messages[-1]]


@pytest.fixture
def homes(monkeypatch, tmp_path):
    """A root profile and a named ``work`` profile with diverging config.yaml."""
    from api import config, profiles

    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    (base / "config.yaml").write_text(ROOT_CONFIG, encoding="utf-8")
    (work_home / "config.yaml").write_text(WORK_CONFIG, encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    # conftest pins HERMES_CONFIG_PATH at the suite's state dir; profile config
    # paths only resolve once that override is out of the way.
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)

    with config._cfg_lock:
        saved = (
            copy.deepcopy(dict(config._cfg_cache)),
            config._cfg_path,
            config._cfg_mtime,
            config._cfg_fingerprint,
        )
    yield types.SimpleNamespace(base=base, root=base, work=work_home)
    with config._cfg_lock:
        config._cfg_cache.clear()
        config._cfg_cache.update(saved[0])
        config._cfg_path, config._cfg_mtime, config._cfg_fingerprint = saved[1:]
        config.cfg = config._cfg_cache


@pytest.fixture
def runtime(monkeypatch):
    """Stub only the LLM side: the AIAgent class and the runtime credential resolver."""
    from api import config

    rec = types.SimpleNamespace(agents=[], refresh_threads=[], compressor=_FakeCompressor)

    class RecordingAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.context_compressor = rec.compressor()
            rec.agents.append(kwargs)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = RecordingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    def resolve_runtime_provider(requested=None, **_kwargs):
        return {
            "api_key": f"runtime-key-{requested}",
            "provider": requested,
            "base_url": "https://runtime.invalid/v1",
        }

    fake_runtime_provider = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_provider.resolve_runtime_provider = resolve_runtime_provider
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__path__ = []
    fake_hermes_cli.runtime_provider = fake_runtime_provider
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_provider)

    # Observe (never alter) every refill of the process-wide config cache.
    real_refresh = config._refresh_config_cache

    def observed_refresh(*args, **kwargs):
        rec.refresh_threads.append(threading.current_thread().name)
        return real_refresh(*args, **kwargs)

    monkeypatch.setattr(config, "_refresh_config_cache", observed_refresh)
    return rec


@pytest.fixture
def make_session(tmp_path, cleanup_test_sessions):
    from api.config import LOCK, SESSION_DIR, SESSIONS
    from api.models import Session

    created = []

    def make(profile, model="probe-model", model_provider=None):
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        s = Session(
            session_id=f"c8970_{time.time_ns()}",
            title="Untitled",
            workspace=str(tmp_path / "ws"),
            model=model,
            model_provider=model_provider,
            profile=profile,
            messages=copy.deepcopy(_MESSAGES),
        )
        s.profile = profile
        s.save(touch_updated_at=False)
        created.append(s)
        cleanup_test_sessions.append(s.session_id)
        _load_as_start_request(s.session_id, profile)
        return s.session_id

    yield make
    for s in created:
        with LOCK:
            SESSIONS.pop(s.session_id, None)
        for path in (s.path, s.path.with_suffix(".json.bak")):
            try:
                path.unlink()
            except OSError:
                pass


def _on_request_thread(profile_name, fn):
    """Run ``fn`` on a fresh thread carrying ``profile_name``'s request TLS."""
    from api import profiles

    def run():
        if profile_name:
            profiles.set_request_profile(profile_name)
        try:
            fn()
        finally:
            profiles.clear_request_profile()

    t = threading.Thread(target=run, name=f"request-{profile_name}")
    t.start()
    t.join(10)
    assert not t.is_alive()


def _load_as_start_request(sid, profile_name):
    """``/api/session/compress/start`` loads the session on its request thread
    before it spawns the worker, so the worker finds it in the session LRU."""
    from api.models import get_session

    _on_request_thread(profile_name, lambda: get_session(sid))


def _fill_shared_cache_as(profile_name):
    """Refill the shared config cache the way a request on ``profile_name`` does."""
    from api import config

    _on_request_thread(profile_name, config.get_config)


def _shared_cache_state():
    from api import config

    with config._cfg_lock:
        return (
            config._cfg_path,
            config.cfg is config._cfg_cache,
            json.dumps(config._cfg_cache, sort_keys=True, default=str),
        )


def _run_job_on_fresh_thread(sid):
    worker = _start_job_on_fresh_thread(sid)
    return _finish_job(sid, worker)


def _start_job_on_fresh_thread(sid):
    from api import routes

    now = time.time()
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        routes._MANUAL_COMPRESSION_JOBS[sid] = {
            "session_id": sid,
            "focus_topic": None,
            "status": "running",
            "started_at": now,
            "updated_at": now,
        }
    worker = threading.Thread(
        target=routes._run_manual_compression_job,
        args=(sid, {"session_id": sid}),
        name=f"manual-compress-{sid[:8]}",
        daemon=True,
    )
    worker.start()
    return worker


def _finish_job(sid, worker):
    from api import routes

    worker.join(30)
    assert not worker.is_alive()
    with routes._MANUAL_COMPRESSION_JOBS_LOCK:
        return dict(routes._MANUAL_COMPRESSION_JOBS.pop(sid)), worker.name


# ── (a) false refusal ─────────────────────────────────────────────────────────


def test_work_session_is_not_refused_for_a_root_only_provider_ambiguity(homes, runtime, make_session):
    """Only ROOT has a custom row named 'anthropic'; the work session must compress."""
    sid = make_session("work", model_provider="anthropic")
    _fill_shared_cache_as("default")

    job, _ = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    assert len(runtime.agents) == 1
    assert runtime.agents[0]["provider"] == "anthropic"
    assert "root-anthropic" not in json.dumps(runtime.agents[0], default=str)


# ── (b) named custom:<slug> record ─────────────────────────────────────────────


@pytest.mark.parametrize("last_refresher", ["default", "work"])
def test_custom_slug_uses_the_work_profile_record(homes, runtime, make_session, last_refresher):
    sid = make_session("work", model="lab-model", model_provider="custom:lab")
    _fill_shared_cache_as(last_refresher)

    job, _ = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    kwargs = runtime.agents[0]
    assert kwargs["base_url"] == "http://work-lab.invalid/v1"
    assert kwargs["api_key"] == "work-lab-key"
    dumped = json.dumps(kwargs, default=str)
    assert "default-lab" not in dumped and "root-lab-key" not in dumped


# ── (c) model/provider resolution + shared cache untouched ────────────────────


@pytest.mark.parametrize("last_refresher", ["default", "work"])
def test_model_provider_follows_the_work_config_and_leaves_the_shared_cache_alone(
    homes, runtime, make_session, last_refresher
):
    sid = make_session("work", model_provider=None)
    _fill_shared_cache_as(last_refresher)
    before = _shared_cache_state()
    expected_path = (homes.root if last_refresher == "default" else homes.work) / "config.yaml"
    assert before[0] == expected_path

    job, worker_name = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    assert runtime.agents[0]["model"] == "probe-model"
    # The provider comes from the work config, and the worker neither refilled
    # the process-wide cache nor left it changed.
    assert (
        runtime.agents[0]["provider"],
        worker_name in runtime.refresh_threads,
        _shared_cache_state() == before,
    ) == ("deepseek", False, True)


# ── (d) helper-agent toolsets ─────────────────────────────────────────────────


def test_helper_agent_toolsets_come_from_the_work_profile(homes, runtime, make_session):
    from api import config

    work_toolsets = config._resolve_cli_toolsets(config.get_config_for_profile_home(homes.work))
    root_toolsets = config._resolve_cli_toolsets({"platform_toolsets": {"cli": ["web"]}})
    assert work_toolsets != root_toolsets
    sid = make_session("work", model_provider="deepseek")
    _fill_shared_cache_as("default")

    job, _ = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    assert runtime.agents[0]["enabled_toolsets"] == work_toolsets


# ── read 1: view-only guard state.db ──────────────────────────────────────────


def test_view_only_guard_reads_the_work_profile_state_db(homes, runtime, make_session, monkeypatch):
    from api import routes

    seen = []
    real_guard = routes._session_is_subagent_view_only

    def recording_guard(sid):
        seen.append(routes._active_state_db_path())
        return real_guard(sid)

    monkeypatch.setattr(routes, "_session_is_subagent_view_only", recording_guard)
    sid = make_session("work", model_provider="deepseek")

    job, _ = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    assert [str(p) for p in seen] == [str(homes.work / "state.db")]


# ── scope restoration ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("body_raises", [False, True])
def test_worker_scope_binds_the_session_profile_and_restores_on_exit(
    homes, runtime, make_session, monkeypatch, body_raises
):
    import os

    from api import config, profiles, routes

    sid = make_session("work", model_provider="deepseek")
    _fill_shared_cache_as("default")
    before = _shared_cache_state()
    home_before = os.environ.get("HERMES_HOME")
    inside = {}
    after = {}
    real_handler = routes._handle_session_compress
    real_job = routes._run_manual_compression_job

    def observing_handler(handler, body):
        inside["profile"] = profiles.get_active_profile_name()
        inside["config_path"] = config._get_config_path()
        inside["provider"] = config.get_config()["model"]["provider"]
        inside["snapshot_provider"] = config.get_config_snapshot()["model"]["provider"]
        if body_raises:
            raise RuntimeError("boom inside the scope")
        return real_handler(handler, body)

    def run_then_observe(sid_, body_):
        real_job(sid_, body_)
        # Captured before any read below can refill the shared cache.
        after["cache_unchanged"] = _shared_cache_state() == before
        after["worker_refilled_cache"] = threading.current_thread().name in runtime.refresh_threads
        after["tls"] = getattr(profiles._tls, "profile", None)
        after["profile"] = profiles.get_active_profile_name()
        after["thread_env"] = dict(getattr(config._thread_ctx, "env", {}) or {})
        after["block"] = bool(getattr(config._thread_ctx, "block_process_env_fallback", False))
        after["private_config"] = getattr(config._thread_ctx, "private_config", None)
        after["provider"] = config.get_config()["model"]["provider"]

    monkeypatch.setattr(routes, "_handle_session_compress", observing_handler)
    monkeypatch.setattr(routes, "_run_manual_compression_job", run_then_observe)
    job, _ = _run_job_on_fresh_thread(sid)

    assert inside == {
        "profile": "work",
        "config_path": homes.work / "config.yaml",
        "provider": "deepseek",
        "snapshot_provider": "deepseek",
    }
    assert job["status"] == ("error" if body_raises else "done")
    assert after == {
        "cache_unchanged": True,
        "worker_refilled_cache": False,
        "tls": None,
        "profile": "default",
        "thread_env": {},
        "block": False,
        "private_config": None,
        "provider": "openrouter",
    }
    assert os.environ.get("HERMES_HOME") == home_before


def test_a_running_work_job_does_not_flip_the_shared_config_for_other_requests(
    homes, runtime, make_session
):
    """Mid-job, a root request thread reading the bare ``cfg`` alias still sees root."""
    from api import config

    entered, release = threading.Event(), threading.Event()

    class PausingCompressor:
        def compress(self, messages, current_tokens=None, focus_topic=None):
            entered.set()
            release.wait(10)
            return [messages[0], messages[-1]]

    runtime.compressor = PausingCompressor
    sid = make_session("work", model_provider="deepseek")
    _fill_shared_cache_as("default")

    worker = _start_job_on_fresh_thread(sid)
    try:
        # The job has resolved its provider, bundle and toolsets and is now in the
        # (stubbed) LLM call; another profile's request reads the shared alias.
        assert entered.wait(10)
        seen_mid_job = []
        _on_request_thread(
            "default",
            lambda: seen_mid_job.append((config._cfg_path, config.cfg["model"]["provider"])),
        )
    finally:
        release.set()
    job, _ = _finish_job(sid, worker)

    assert job["status"] == "done", job.get("error")
    assert runtime.agents[0]["provider"] == "deepseek"
    assert seen_mid_job == [(homes.root / "config.yaml", "openrouter")]


def test_private_config_yields_to_a_nested_scope_for_another_profile(homes):
    """The private config is served only while the thread still names its file."""
    from api import config, profiles

    seen = {}

    def run():
        with profiles.profile_scope_for_detached_worker("work", "test"), config.thread_private_config():
            seen["work"] = config.get_config()["model"]["provider"]
            seen["work_view"] = config._config_view()["model"]["provider"]
            with profiles.profile_scope_for_detached_worker("default", "test"):
                seen["nested_root"] = config.get_config()["model"]["provider"]
            seen["work_again"] = config.get_config_snapshot()["model"]["provider"]
        seen["after"] = getattr(config._thread_ctx, "private_config", None)

    t = threading.Thread(target=run)
    t.start()
    t.join(10)

    assert seen == {
        "work": "deepseek",
        "work_view": "deepseek",
        "nested_root": "openrouter",
        "work_again": "deepseek",
        "after": None,
    }


# ── (e) negative controls: root profile and no profile ─────────────────────────


@pytest.mark.parametrize("profile", ["default", None])
def test_root_and_profileless_sessions_resolve_the_root_config(homes, runtime, make_session, profile):
    from api import config
    from api.models import get_session

    sid = make_session(profile, model_provider=None)
    assert (get_session(sid).profile or None) == profile
    _fill_shared_cache_as("default")
    before = _shared_cache_state()

    job, _ = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    kwargs = runtime.agents[0]
    assert kwargs["provider"] == "openrouter"
    assert kwargs["enabled_toolsets"] == config._resolve_cli_toolsets(
        {"platform_toolsets": {"cli": ["web"]}}
    )
    assert _shared_cache_state() == before


@pytest.mark.parametrize("profile", ["default", None])
def test_root_and_profileless_sessions_keep_the_root_custom_record(homes, runtime, make_session, profile):
    sid = make_session(profile, model="lab-model", model_provider="custom:lab")
    _fill_shared_cache_as("default")

    job, _ = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    assert runtime.agents[0]["base_url"] == "http://default-lab.invalid/v1"
    assert runtime.agents[0]["api_key"] == "root-lab-key"


@pytest.mark.parametrize("profile", ["default", None])
def test_root_and_profileless_sessions_still_honor_the_shared_in_memory_config(
    homes, runtime, make_session, monkeypatch, profile
):
    """Root keeps master's shared-cache semantics, including in-memory overrides."""
    from api import config

    sid = make_session(profile, model_provider=None)
    _fill_shared_cache_as("default")
    override = copy.deepcopy(config._cfg_cache)
    override["model"]["provider"] = "nous"
    monkeypatch.setattr(config, "cfg", override)

    job, _ = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    assert runtime.agents[0]["provider"] == "nous"
