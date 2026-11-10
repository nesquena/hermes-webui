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


# ── renamed root alias (Codex r1) ────────────────────────────────────────────


def test_renamed_root_alias_keeps_the_root_env_for_templated_config(
    homes, make_session, monkeypatch
):
    """A renamed root profile still gets master's env scope inside the TLS scope.

    ``profile_scope_for_detached_worker`` binds no env for a root profile, but
    master's ``profile_env_for_background_worker(session)`` applied the root
    ``.env`` (and its secret scope) for a renamed root alias. Without it a
    ``${VAR}`` defined only in the root ``.env`` stays unexpanded, and the
    #8690 ambiguity verdict falsely refuses the job. This test needs the real
    Agent's env expander and secret scope, so only the Agent's runtime
    credential resolver and the AIAgent class are stubbed here.
    """
    from api import profiles

    runtime_provider = pytest.importorskip("hermes_cli.runtime_provider")
    pytest.importorskip("agent.secret_scope")

    agents = []

    class RecordingAgent:
        def __init__(self, **kwargs):
            self.context_compressor = _FakeCompressor()
            agents.append(kwargs)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = RecordingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda requested=None, **_kw: {
            "api_key": "synthetic-key",
            "provider": requested,
            "base_url": "https://api.openai.com/v1",
        },
    )

    (homes.root / "config.yaml").write_text(
        "model:\n"
        "  provider: openai\n"
        "  default: probe-model\n"
        "platform_toolsets:\n"
        "  cli: []\n"
        "custom_providers:\n"
        "  - name: ${GATE_EXTRA_PROVIDER}\n"
        "    base_url: https://lab.invalid/v1\n"
        "    api_key: synthetic-lab-key\n",
        encoding="utf-8",
    )
    (homes.root / ".env").write_text("GATE_EXTRA_PROVIDER=lab\n", encoding="utf-8")
    monkeypatch.delenv("GATE_EXTRA_PROVIDER", raising=False)
    monkeypatch.setattr(profiles, "_active_profile", "kinni")
    monkeypatch.setattr(profiles, "_root_profile_name_cache", {"default", "kinni"})
    monkeypatch.setattr(profiles, "_root_profile_name_cache_loaded", True)

    sid = make_session("kinni", model_provider="openai")
    job, _ = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    assert agents and agents[0]["provider"] == "openai"


# ── ${VAR} expansion in the private read (Codex r2) ──────────────────────────


def _real_agent_runtime(monkeypatch):
    """The real Agent modules, with only its credential resolver and AIAgent stubbed."""
    runtime_provider = pytest.importorskip("hermes_cli.runtime_provider")
    secret_scope = pytest.importorskip("agent.secret_scope")
    if not hasattr(secret_scope, "serves_routed_profile"):
        pytest.skip("installed Agent predates serves_routed_profile")

    agents = []

    class RecordingAgent:
        def __init__(self, **kwargs):
            self.context_compressor = _FakeCompressor()
            agents.append(kwargs)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = RecordingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda requested=None, **_kw: {
            "api_key": f"runtime-key-{requested}",
            "provider": requested,
            "base_url": "https://runtime.invalid/v1",
        },
    )
    return agents


def test_a_named_profile_endpoint_on_a_process_global_variable_still_resolves(
    homes, make_session, monkeypatch
):
    """A ``${API_SERVER_PORT}``-style endpoint expands as the Agent expands it.

    The Agent reads process-global deployment variables from the process env
    even inside a routed profile's secret scope; master's worker read them
    through the shared cache. Leaving the reference verbatim would hand the
    client ``http://127.0.0.1:${API_SERVER_PORT}/v1`` and fail the job.
    """
    agents = _real_agent_runtime(monkeypatch)
    (homes.work / "config.yaml").write_text(
        "model:\n"
        "  provider: custom:lab\n"
        "  default: lab-model\n"
        "platform_toolsets:\n"
        "  cli: []\n"
        "custom_providers:\n"
        "  - name: lab\n"
        "    base_url: http://127.0.0.1:${API_SERVER_PORT}/v1\n"
        "    api_key: work-lab-key\n"
        "    model: lab-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("API_SERVER_PORT", "18080")

    sid = make_session("work", model="lab-model", model_provider="custom:lab")
    job, _ = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    assert agents and agents[0]["base_url"] == "http://127.0.0.1:18080/v1"


def test_the_private_read_never_falls_back_to_a_process_only_secret(homes, monkeypatch):
    """Inside the named profile's scope: the profile ``.env`` wins, process-global
    variables expand, and a process-only secret stays verbatim (no fallback to
    another profile's process env). ``${env:VAR}`` follows the same rule."""
    from api import config, profiles

    _real_agent_runtime(monkeypatch)
    (homes.work / "config.yaml").write_text(
        "custom_providers:\n"
        "  - name: lab\n"
        "    base_url: http://127.0.0.1:${API_SERVER_PORT}/v1\n"
        "    api_key: ${LAB_PROCESS_ONLY_SECRET}\n"
        "    key_env: ${env:LAB_PROCESS_ONLY_SECRET}\n"
        "    model: ${WORK_LAB_MODEL}\n",
        encoding="utf-8",
    )
    (homes.work / ".env").write_text("WORK_LAB_MODEL=work-model\n", encoding="utf-8")
    monkeypatch.setenv("API_SERVER_PORT", "18080")
    monkeypatch.setenv("LAB_PROCESS_ONLY_SECRET", "process-secret-value")
    monkeypatch.setenv("WORK_LAB_MODEL", "process-model")

    seen = {}

    def run():
        with profiles.profile_scope_for_detached_worker("work", "manual compression"), (
            config.thread_private_config()
        ) as private:
            seen["record"] = dict(private["custom_providers"][0])

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(30)

    record = seen["record"]
    assert record["base_url"] == "http://127.0.0.1:18080/v1"
    assert record["model"] == "work-model"
    assert record["api_key"] == "${LAB_PROCESS_ONLY_SECRET}"
    assert record["key_env"] == "${env:LAB_PROCESS_ONLY_SECRET}"
    assert "process-secret-value" not in json.dumps(seen)


@pytest.mark.parametrize("spelling", ["${API_SERVER_PORT}", "${ API_SERVER_PORT }"])
@pytest.mark.parametrize("case", ["server_own_profile", "no_home_override", "older_agent"])
def test_the_closed_expansion_still_reads_the_agents_process_global_variables(
    homes, make_session, monkeypatch, case, spelling
):
    """Codex r3: whatever the Agent's scope (the named profile is the server's own
    profile, the Hermes-home override is unavailable, or the Agent predates
    ``serves_routed_profile``), the private read still reads the Agent's
    process-global variables from the process env, and nothing else from it.
    A name padded with spaces is read the way WebUI's own lookup reads it (stripped),
    as master's shared-cache expansion did (Codex r4)."""
    import hermes_constants
    from agent import secret_scope
    from api import config, profiles

    agents = _real_agent_runtime(monkeypatch)
    if case == "server_own_profile":
        monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", str(homes.work))
    elif case == "no_home_override":
        monkeypatch.setattr(profiles, "_resolve_hermes_home_override", lambda: None)
    else:
        monkeypatch.delattr(secret_scope, "serves_routed_profile")
    (homes.work / "config.yaml").write_text(
        "model:\n"
        "  provider: custom:lab\n"
        "  default: lab-model\n"
        "platform_toolsets:\n"
        "  cli: []\n"
        "custom_providers:\n"
        "  - name: lab\n"
        f"    base_url: http://127.0.0.1:{spelling}/v1\n"
        "    api_key: work-lab-key\n"
        "    model: lab-model\n"
        "    key_env: ${LAB_PROCESS_ONLY_SECRET}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("API_SERVER_PORT", "18080")
    monkeypatch.setenv("LAB_PROCESS_ONLY_SECRET", "process-secret-value")

    sid = make_session("work", model="lab-model", model_provider="custom:lab")
    job, _ = _run_job_on_fresh_thread(sid)
    assert job["status"] == "done", job.get("error")
    assert agents and agents[0]["base_url"] == "http://127.0.0.1:18080/v1"

    seen = {}

    def run():
        with profiles.profile_scope_for_detached_worker("work", "manual compression"), (
            config.thread_private_config()
        ) as private:
            seen["record"] = dict(private["custom_providers"][0])

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(30)
    assert seen["record"]["base_url"] == "http://127.0.0.1:18080/v1"
    assert seen["record"]["key_env"] == "${LAB_PROCESS_ONLY_SECRET}"
    assert "process-secret-value" not in json.dumps(seen)


def test_a_failed_profile_env_resolution_keeps_process_only_secrets_out(homes, monkeypatch):
    """Opus r1: when the worker's profile-env resolution raises, its fallback binds
    no thread env and no process-env block. The private read still expands against
    the profile's own binding, so a secret that lives only in the server process env
    never reaches the named profile's config."""
    from api import config, profiles

    (homes.work / "config.yaml").write_text(
        "custom_providers:\n"
        "  - name: lab\n"
        "    base_url: http://lab.invalid/v1\n"
        "    api_key: ${LAB_KEY}\n"
        "    model: lab-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LAB_KEY", "proc-only-root-secret")

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("profile env unavailable")

    monkeypatch.setattr(profiles, "get_profile_runtime_env", unavailable)
    seen = {}

    def run():
        with profiles.profile_scope_for_detached_worker("work", "manual compression"):
            with config.thread_private_config() as private:
                seen["record"] = dict(private["custom_providers"][0])

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(30)
    assert seen["record"]["api_key"] == "${LAB_KEY}"
    assert "proc-only-root-secret" not in json.dumps(seen)


def test_a_recursive_yaml_alias_does_not_fail_the_job(homes, runtime, make_session):
    """Opus r1: a recursive YAML alias elsewhere in the profile's config.yaml must not
    fail the compression job; the profile's ordinary settings are still served, as
    the #8032 helper serves them."""
    (homes.work / "config.yaml").write_text(WORK_CONFIG + "loop: &L\n  - *L\n", encoding="utf-8")
    sid = make_session("work", model="lab-model", model_provider="custom:lab")
    job, _ = _run_job_on_fresh_thread(sid)
    assert job["status"] == "done", job.get("error")
    assert runtime.agents[0]["base_url"] == "http://work-lab.invalid/v1"
    assert runtime.agents[0]["api_key"] == "work-lab-key"


def test_a_global_variable_removed_mid_read_is_skipped(homes, monkeypatch):
    """Codex r6: another worker can restore its env (removing a key) while the private
    read collects the Agent's process-global variables. ``os.environ.items()`` fetches
    each value after listing the names, so a removed key raised ``KeyError`` and
    failed the compression job. A name that vanishes is now skipped."""
    import collections.abc
    import os as _real_os

    from api import config

    class VanishingEnviron(collections.abc.Mapping):
        """Lists a global that is gone by the time its value is read."""

        _data = {"API_SERVER_PORT": "18080", "WORK_ONLY_NOT_GLOBAL": "x"}

        def __iter__(self):
            return iter(list(self._data) + ["VANISHING_GLOBAL"])

        def __len__(self):
            return len(self._data) + 1

        def __getitem__(self, name):
            return self._data[name]

    class OsProxy:
        environ = VanishingEnviron()

        def __getattr__(self, name):
            return getattr(_real_os, name)

    monkeypatch.setattr(
        config, "_agent_global_env_rule", lambda: (lambda name: name in {"API_SERVER_PORT", "VANISHING_GLOBAL"})
    )
    monkeypatch.setattr(config, "os", OsProxy())

    env = config._private_config_env_binding(homes.work)

    assert env["API_SERVER_PORT"] == "18080"
    assert "VANISHING_GLOBAL" not in env
    assert "WORK_ONLY_NOT_GLOBAL" not in env
