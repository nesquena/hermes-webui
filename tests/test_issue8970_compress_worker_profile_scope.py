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
    # The helper skips this test without an Agent (CI installs none); call it before
    # importing Agent modules (Opus r2).
    agents = _real_agent_runtime(monkeypatch)
    import hermes_constants
    from agent import secret_scope
    from api import config, profiles

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


# ── #8981 review B1: the server's OWN named profile reads its deployment env ──


WORK_DEPLOY_TEMPLATED = """\
model:
  provider: custom:lab
  default: lab-model
platform_toolsets:
  cli: [terminal, file]
custom_providers:
  - name: lab
    base_url: http://work-lab.invalid/v1
    api_key: ${LAB_DEPLOY_KEY}
    model: lab-model
"""


def _deploy_env(monkeypatch, value="deploy-injected-key"):
    """``LAB_DEPLOY_KEY`` set only in the launcher/deployment env (container or
    service env, ``api.paths.STARTUP_ENV``), never in any profile ``.env``."""
    from api import paths

    monkeypatch.setenv("LAB_DEPLOY_KEY", value)
    monkeypatch.setattr(
        paths, "STARTUP_ENV", types.MappingProxyType({**paths.STARTUP_ENV, "LAB_DEPLOY_KEY": value})
    )


def _serve_as_process_profile(monkeypatch, home):
    """Pin *home* as the profile this process serves as its own, with the call
    ``_set_hermes_home()`` makes at startup and on a process-wide switch
    (``_pin_process_profile_home`` -> the Agent's ``pin_process_hermes_home``).
    ``None`` leaves the Agent unpinned. Both pins are restored afterwards."""
    import hermes_constants
    from api import profiles

    monkeypatch.setattr(profiles, "_PROCESS_PROFILE_HOME", profiles._PROCESS_PROFILE_HOME)
    monkeypatch.setattr(
        hermes_constants,
        "_PINNED_PROCESS_HERMES_HOME",
        getattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None),
        raising=False,
    )
    if home is None:
        hermes_constants.pin_process_hermes_home(None)
    else:
        profiles._pin_process_profile_home(home)


def _private_record(profile_name):
    """The first ``custom_providers`` record of the worker's private config read."""
    from api import config, profiles

    seen = {}

    def run():
        with profiles.profile_scope_for_detached_worker(profile_name, "manual compression"), (
            config.thread_private_config()
        ) as private:
            seen["record"] = dict(private["custom_providers"][0])

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(30)
    return seen["record"]


@pytest.mark.parametrize("mode", ["isolated", "sticky"])
def test_the_servers_own_named_profile_reads_its_deployment_env(
    homes, runtime, make_session, monkeypatch, mode
):
    """B1: a named profile that is the process's OWN profile ((a) isolated mode,
    (b) the sticky process-wide active profile) expands ``${VAR}`` from the
    deployment env as well, as the Agent's ``get_secret`` falls through to the
    process env for an unrouted task (and ``.663``'s worker did). Needs the
    Agent's own-home rule, so it skips without the Agent."""
    pytest.importorskip("hermes_constants")
    from api import profiles

    (homes.work / "config.yaml").write_text(WORK_DEPLOY_TEMPLATED, encoding="utf-8")
    _deploy_env(monkeypatch)
    if mode == "isolated":
        monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)
        monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(homes.work))
    else:
        monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(homes.root))
        monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", {})
    monkeypatch.setattr(profiles, "_active_profile", "work")
    monkeypatch.setenv("HERMES_HOME", str(homes.work))
    _serve_as_process_profile(monkeypatch, homes.work)

    sid = make_session("work", model="lab-model", model_provider="custom:lab")
    job, _ = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    assert runtime.agents[0]["base_url"] == "http://work-lab.invalid/v1"
    assert runtime.agents[0]["api_key"] == "deploy-injected-key"


@pytest.mark.parametrize(
    "process_home, session_profile",
    [("root", "work"), ("work", "other"), ("unpinned", "work")],
)
def test_a_routed_named_profile_still_ignores_the_deployment_env(
    homes, runtime, make_session, monkeypatch, process_home, session_profile
):
    """B1's fall-through is for the process's own home only. A routed named
    profile (root serves the process, or another named profile does) keeps the
    closed rule and never sees a deployment-env secret, with the Agent's
    functions present. An unpinned Agent follows ``HERMES_HOME``, which the
    worker's env scope mirrors to the session's home, so it is not trusted."""
    pytest.importorskip("hermes_constants")
    from api import profiles

    other = homes.base / "profiles" / "other"
    other.mkdir(parents=True)
    for home in (homes.work, other):
        (home / "config.yaml").write_text(WORK_DEPLOY_TEMPLATED, encoding="utf-8")
    _deploy_env(monkeypatch)
    pinned = {"root": homes.root, "work": homes.work, "unpinned": None}[process_home]
    if pinned is not None and pinned != homes.root:
        monkeypatch.setattr(profiles, "_active_profile", pinned.name)
    monkeypatch.setenv("HERMES_HOME", str(pinned or homes.work))
    _serve_as_process_profile(monkeypatch, pinned)

    assert _private_record(session_profile)["api_key"] == "${LAB_DEPLOY_KEY}"
    sid = make_session(session_profile, model="lab-model", model_provider="custom:lab")
    job, _ = _run_job_on_fresh_thread(sid)

    assert job["status"] == "error"
    assert "deploy-injected-key" not in json.dumps(runtime.agents)


def test_the_own_profile_read_keeps_its_env_first_and_drops_another_launch_homes_env(
    homes, monkeypatch
):
    """B1's deployment env is the launch-time snapshot under the profile's own
    ``.env`` (which wins), without the values a shell launcher sourced from
    ANOTHER launch home's ``.env`` (the #8435 provenance rule): after a
    process-wide switch from the launch profile, the Agent's process env no
    longer holds those either."""
    pytest.importorskip("hermes_constants")
    from api import paths, profiles

    (homes.work / "config.yaml").write_text(
        "custom_providers:\n"
        "  - name: lab\n"
        "    base_url: http://lab.invalid/v1\n"
        "    api_key: ${LAB_DEPLOY_KEY}\n"
        "    model: ${LAB_MODEL}\n"
        "    key_env: ${LAUNCH_HOME_KEY}\n",
        encoding="utf-8",
    )
    (homes.work / ".env").write_text("LAB_MODEL=work-dotenv-model\n", encoding="utf-8")
    startup = {
        **paths.STARTUP_ENV,
        "LAB_DEPLOY_KEY": "deploy-injected-key",
        "LAB_MODEL": "launcher-model",
        "LAUNCH_HOME_KEY": "launch-home-secret",
    }
    monkeypatch.setattr(paths, "STARTUP_ENV", types.MappingProxyType(startup))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(homes.root))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", {"LAUNCH_HOME_KEY": "launch-home-secret"})
    monkeypatch.setattr(profiles, "_active_profile", "work")
    _serve_as_process_profile(monkeypatch, homes.work)

    record = _private_record("work")

    assert record["api_key"] == "deploy-injected-key"
    assert record["model"] == "work-dotenv-model"
    assert record["key_env"] == "${LAUNCH_HOME_KEY}"


@pytest.mark.parametrize("agent_shape", ["no_hermes_constants", "no_own_home_functions"])
def test_without_the_agents_own_home_rule_the_closed_rule_holds(
    homes, runtime, make_session, monkeypatch, agent_shape
):
    """Without the Agent (CI) or without its own-home functions, even the process's
    own named profile (isolated mode, the launch home) keeps the closed rule:
    a deployment-env-only secret stays the literal ``${VAR}``. Runs with no Agent."""
    from api import profiles

    # Both shapes make WebUI cache "no Hermes-home override"; restore it for later tests.
    monkeypatch.setattr(profiles, "_hermes_home_override_available", profiles._hermes_home_override_available)
    if agent_shape == "no_hermes_constants":
        monkeypatch.setitem(sys.modules, "hermes_constants", None)
    else:
        monkeypatch.setitem(sys.modules, "hermes_constants", types.ModuleType("hermes_constants"))
    (homes.work / "config.yaml").write_text(WORK_DEPLOY_TEMPLATED, encoding="utf-8")
    _deploy_env(monkeypatch)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(homes.work))
    monkeypatch.setattr(profiles, "_PROCESS_PROFILE_HOME", str(homes.work))
    monkeypatch.setattr(profiles, "_active_profile", "work")

    assert _private_record("work")["api_key"] == "${LAB_DEPLOY_KEY}"
    sid = make_session("work", model="lab-model", model_provider="custom:lab")
    job, _ = _run_job_on_fresh_thread(sid)
    assert job["status"] == "error"
    assert "deploy-injected-key" not in json.dumps(runtime.agents)


# ── #8981 review O1: root sessions keep master's scope exactly ──────────────


@pytest.mark.parametrize("profile", ["default", "kinni"])
def test_root_sessions_keep_masters_scope_under_a_named_process_profile(
    homes, runtime, make_session, monkeypatch, profile
):
    """O1: a root (or renamed-root alias) session compressed while a named profile
    is process-active keeps ``.663``'s scope: no request-profile binding, so the
    worker neither refills the shared cache nor mixes two profiles (provider from
    the stale shared alias, toolsets from a root refill). Which profile it uses is
    the pre-existing residual; this pins only that it is ONE profile."""
    from api import config, profiles

    monkeypatch.setattr(profiles, "_root_profile_name_cache", {"default", "kinni"})
    monkeypatch.setattr(profiles, "_root_profile_name_cache_loaded", True)
    monkeypatch.setattr(profiles, "_active_profile", "work")
    sid = make_session(profile, model_provider=None)
    _fill_shared_cache_as("work")
    before = _shared_cache_state()

    job, worker_name = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    assert worker_name not in runtime.refresh_threads
    assert _shared_cache_state() == before
    kwargs = runtime.agents[0]
    one_profile = {
        ("openrouter", json.dumps(config._resolve_cli_toolsets({"platform_toolsets": {"cli": ["web"]}}))),
        ("deepseek", json.dumps(config._resolve_cli_toolsets({"platform_toolsets": {"cli": ["terminal", "file"]}}))),
    }
    assert (kwargs["provider"], json.dumps(kwargs["enabled_toolsets"])) in one_profile



# ── #8981 review: Agent-free pins (CI installs no Agent) ──────────────────────
#
# Stand-ins for the Agent modules WebUI reads here, planted in ``sys.modules``. With
# an Agent installed a stand-in keeps the real module's other names, so the rest of
# the job keeps working; in CI it holds only the names below.


def _stand_in_module(monkeypatch, name, **overrides):
    """Plant a stand-in for Agent module *name*: the real module's names when it is
    importable, else none, plus *overrides*. Restored by ``monkeypatch``."""
    import importlib

    real = sys.modules.get(name)
    if real is None:
        try:
            real = importlib.import_module(name)
        except Exception:
            real = None
    stand_in = types.ModuleType(name)
    if real is not None:
        stand_in.__dict__.update({k: v for k, v in vars(real).items() if not k.startswith("__")})
    stand_in.__dict__.update(overrides)
    monkeypatch.setitem(sys.modules, name, stand_in)
    return stand_in


@pytest.fixture
def stand_in_hermes_constants(monkeypatch):
    """A stand-in ``hermes_constants`` with the three functions B1 reads
    (``get_routing_process_hermes_home``, ``hermes_home_key``,
    ``process_hermes_home_is_pinned``). ``serve(home, pinned=True)`` names *home*
    as the process's routing home; unpinned, the Agent's routing home follows
    ``HERMES_HOME``, which the worker mirrors to the session's home."""
    from pathlib import Path

    from api import profiles

    # WebUI caches whether hermes_constants has the Hermes-home override; restore it.
    monkeypatch.setattr(profiles, "_hermes_home_override_available", profiles._hermes_home_override_available)
    monkeypatch.setattr(profiles, "_PROCESS_PROFILE_HOME", profiles._PROCESS_PROFILE_HOME)
    state = types.SimpleNamespace(home=None, pinned=False)
    _stand_in_module(
        monkeypatch,
        "hermes_constants",
        get_routing_process_hermes_home=lambda: Path(state.home),
        hermes_home_key=lambda home: str(Path(home).expanduser().resolve()),
        process_hermes_home_is_pinned=lambda: state.pinned,
    )

    def serve(home, *, pinned=True):
        state.home, state.pinned = home, pinned
        if pinned:
            profiles._PROCESS_PROFILE_HOME = str(home)

    return serve


@pytest.fixture
def stand_in_secret_scope(monkeypatch):
    """Stand-ins for the Agent's secret scope (``agent.secret_scope``: the scope
    binding, ``get_secret`` and the process-global rule ``_is_global_env``, which
    names exactly ``state.global_names``) and its config expander
    (``hermes_cli.config._expand_env_vars``: ``${VAR}`` / ``${env:VAR}`` read through
    ``get_secret`` under the bound scope, an unset reference left verbatim)."""
    import contextvars
    import os
    import re

    from api import profiles

    monkeypatch.setattr(profiles, "_secret_scope_available", profiles._secret_scope_available)
    state = types.SimpleNamespace(global_names=set())
    scope = contextvars.ContextVar("stand_in_secret_scope", default=None)

    def is_global_env(name):
        return name in state.global_names

    def get_secret(name, default=None):
        bound = scope.get()
        if bound is None or is_global_env(name):
            return os.environ.get(name, default)
        return bound.get(name, default)

    def expand_env_vars(value):
        if isinstance(value, dict):
            return {k: expand_env_vars(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand_env_vars(v) for v in value]
        if not isinstance(value, str):
            return value

        def one(match):
            name = match.group(1).strip()
            name = name[len("env:"):].strip() if name.startswith("env:") else name
            found = get_secret(name)
            return match.group(0) if found is None else found

        return re.sub(r"\$\{([^}]*)\}", one, value)

    _stand_in_module(
        monkeypatch,
        "agent.secret_scope",
        _is_global_env=is_global_env,
        get_secret=get_secret,
        set_secret_scope=lambda env: scope.set(dict(env)),
        reset_secret_scope=scope.reset,
        current_secret_scope=scope.get,
    )
    _stand_in_module(monkeypatch, "hermes_cli.config", _expand_env_vars=expand_env_vars)
    return state


def _in_private_scope(profile_name, fn):
    """``fn()`` on a fresh worker thread inside the compression worker's two scopes
    for *profile_name* (its detached-worker scope and its private config read)."""
    from api import config, profiles

    out = {}

    def run():
        try:
            with profiles.profile_scope_for_detached_worker(profile_name, "manual compression"), (
                config.thread_private_config()
            ):
                out["value"] = fn()
        except BaseException as exc:  # re-raised on the test thread
            out["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(30)
    assert not worker.is_alive()
    if "error" in out:
        raise out["error"]
    return out["value"]


# ── B1 with a stand-in Agent ──────────────────────────────────────────────────


@pytest.mark.parametrize("case", ["own_home", "routed_from_root", "unpinned"])
def test_b1_own_home_rule_with_a_stand_in_agent(
    homes, runtime, make_session, monkeypatch, stand_in_hermes_constants, case
):
    """B1 with no real Agent: the process's own pinned named profile expands a
    ``${VAR}`` from ``api.paths.STARTUP_ENV``; a routed profile (root is the
    process's home) does not, nor does an unpinned Agent whose routing home merely
    follows the session's mirrored ``HERMES_HOME``."""
    from api import profiles

    (homes.work / "config.yaml").write_text(WORK_DEPLOY_TEMPLATED, encoding="utf-8")
    _deploy_env(monkeypatch)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(homes.root))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", {})
    monkeypatch.setenv("HERMES_HOME", str(homes.work))
    if case == "own_home":
        monkeypatch.setattr(profiles, "_active_profile", "work")
        stand_in_hermes_constants(homes.work)
    elif case == "routed_from_root":
        stand_in_hermes_constants(homes.root)
    else:
        stand_in_hermes_constants(homes.work, pinned=False)

    record = _private_record("work")
    sid = make_session("work", model="lab-model", model_provider="custom:lab")
    job, _ = _run_job_on_fresh_thread(sid)

    if case == "own_home":
        assert record["api_key"] == "deploy-injected-key"
        assert job["status"] == "done", job.get("error")
        assert runtime.agents[0]["api_key"] == "deploy-injected-key"
    else:
        assert record["api_key"] == "${LAB_DEPLOY_KEY}"
        assert job["status"] == "error"
        assert "deploy-injected-key" not in json.dumps(runtime.agents)


def test_the_own_profile_read_keeps_its_env_first_with_a_stand_in_agent(
    homes, monkeypatch, stand_in_hermes_constants
):
    """The provenance pin with no real Agent: the profile's ``.env`` wins over the
    launch snapshot, and a value sourced from another launch home's ``.env`` is
    dropped."""
    from api import paths, profiles

    (homes.work / "config.yaml").write_text(
        "custom_providers:\n"
        "  - name: lab\n"
        "    base_url: http://lab.invalid/v1\n"
        "    api_key: ${LAB_DEPLOY_KEY}\n"
        "    model: ${LAB_MODEL}\n"
        "    key_env: ${LAUNCH_HOME_KEY}\n",
        encoding="utf-8",
    )
    (homes.work / ".env").write_text("LAB_MODEL=work-dotenv-model\n", encoding="utf-8")
    startup = {
        **paths.STARTUP_ENV,
        "LAB_DEPLOY_KEY": "deploy-injected-key",
        "LAB_MODEL": "launcher-model",
        "LAUNCH_HOME_KEY": "launch-home-secret",
    }
    monkeypatch.setattr(paths, "STARTUP_ENV", types.MappingProxyType(startup))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(homes.root))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", {"LAUNCH_HOME_KEY": "launch-home-secret"})
    monkeypatch.setattr(profiles, "_active_profile", "work")
    stand_in_hermes_constants(homes.work)

    record = _private_record("work")

    assert (record["api_key"], record["model"], record["key_env"]) == (
        "deploy-injected-key",
        "work-dotenv-model",
        "${LAUNCH_HOME_KEY}",
    )



# ── the private view in every resolver the job reads (#8981 review) ───────────

ROOT_READERS_CONFIG = """\
model:
  provider: openrouter
  default: root-default-model
providers:
  root-only:
    base_url: http://root-only.invalid/v1
"""

WORK_READERS_CONFIG = """\
model:
  provider: lmstudio
  default: vendor/work-default-model
  base_url: http://work-lm.invalid/v1
providers:
  work-only:
    base_url: http://work-only.invalid/v1
custom_providers:
  - name: lab
    base_url: http://work-lab.invalid/v1
    model: vendor/work-lab-model
"""

# reader -> (call, work's answer from the private view, root's answer from the shared alias)
_PRIVATE_VIEW_READERS = {
    "get_effective_default_model": (
        lambda c: c.get_effective_default_model(),
        "vendor/work-default-model",
        "root-default-model",
    ),
    "_get_providers_cfg": (lambda c: sorted(c._get_providers_cfg()), ["work-only"], ["root-only"]),
    "_get_provider_base_url": (lambda c: c._get_provider_base_url("lmstudio"), "http://work-lm.invalid/v1", None),
    "_model_id_declared_in_config": (
        lambda c: (
            c._model_id_declared_in_config("vendor/work-default-model", "lmstudio"),
            c._model_id_declared_in_config("vendor/work-lab-model", "custom:lab"),
        ),
        (True, True),
        (False, False),
    ),
    "model_with_provider_context": (
        lambda c: c.model_with_provider_context("probe-model", "openrouter"),
        "@openrouter:probe-model",
        "probe-model",
    ),
}


@pytest.mark.parametrize("reader", sorted(_PRIVATE_VIEW_READERS))
def test_each_resolver_reads_the_private_view_while_root_holds_the_shared_cache(homes, monkeypatch, reader):
    """Every model/provider reader the job reaches serves the worker's private
    config, never the shared alias another profile last filled. The root request's
    answer from the shared alias is the control that the two differ."""
    from api import config

    for name in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    (homes.root / "config.yaml").write_text(ROOT_READERS_CONFIG, encoding="utf-8")
    (homes.work / "config.yaml").write_text(WORK_READERS_CONFIG, encoding="utf-8")
    call, work_answer, root_answer = _PRIVATE_VIEW_READERS[reader]
    _fill_shared_cache_as("default")
    shared = []
    _on_request_thread("default", lambda: shared.append(call(config)))

    assert (_in_private_scope("work", lambda: call(config)), shared) == (work_answer, [root_answer])


def test_a_work_session_on_another_provider_keeps_it_while_root_filled_the_shared_cache(
    homes, runtime, make_session
):
    """The reviewer's routing case: a ``work`` session on ``openrouter`` (work's
    default is ``deepseek``) while root (default ``openrouter``) last filled the
    shared cache. Read from the shared alias, the session's provider looks like the
    default, the model goes bare, and the private resolver sends it to deepseek."""
    sid = make_session("work", model_provider="openrouter")
    _fill_shared_cache_as("default")

    job, _ = _run_job_on_fresh_thread(sid)

    assert job["status"] == "done", job.get("error")
    assert runtime.agents[0]["provider"] == "openrouter"


def test_editing_the_private_snapshot_changes_neither_the_private_config_nor_the_shared_cache(homes):
    """``get_config_snapshot()`` on the private scope returns a deep copy, as it does
    for the shared cache: a caller that edits it (nested values included) changes
    neither what the thread's later reads serve nor the shared cache."""
    from api import config

    _fill_shared_cache_as("default")
    before = _shared_cache_state()

    def edit_then_read():
        snapshot = config.get_config_snapshot()
        snapshot["model"]["provider"] = "edited-in-the-snapshot"
        snapshot["custom_providers"].append({"name": "edited"})
        served = config.get_config()
        return (
            served["model"]["provider"],
            config._config_view()["model"]["provider"],
            [entry["name"] for entry in served["custom_providers"]],
        )

    assert _in_private_scope("work", edit_then_read) == ("deepseek", "deepseek", ["lab"])
    assert _shared_cache_state() == before


def test_a_nested_private_scope_restores_the_outer_binding(homes):
    """Leaving a nested private scope restores the outer one instead of clearing it:
    the outer dict is served again, not the shared cache."""
    from api import config

    _fill_shared_cache_as("default")

    def nest():
        outer = config._thread_ctx.private_config
        with config.thread_private_config() as inner:
            inner_served = config.get_config() is inner
        return (
            inner_served,
            config._thread_ctx.private_config is outer,
            config.get_config() is outer[1],
            config._config_view() is outer[1],
        )

    assert _in_private_scope("work", nest) == (True, True, True, True)


def test_the_private_binding_keeps_the_profiles_own_hermes_home(homes, monkeypatch, stand_in_secret_scope):
    """``HERMES_HOME`` stays the profile's home even when the Agent's rule calls it
    process-global and the process env names another profile (another worker's
    mirror): the binding and a ``${HERMES_HOME}`` in the private read both say work."""
    from api import config, profiles

    stand_in_secret_scope.global_names.update({"HERMES_HOME", "API_SERVER_PORT"})
    monkeypatch.setenv("HERMES_HOME", str(homes.root))
    monkeypatch.setenv("API_SERVER_PORT", "18080")
    (homes.work / "config.yaml").write_text(
        "terminal:\n  cwd: ${HERMES_HOME}/workspace\n  port: ${API_SERVER_PORT}\n", encoding="utf-8"
    )

    env = config._private_config_env_binding(homes.work)
    seen = {}

    def run():
        # The request TLS only, with no env mirror: os.environ still names root.
        profiles.set_request_profile("work")
        try:
            with config.thread_private_config() as private:
                seen["terminal"] = dict(private["terminal"])
        finally:
            profiles.clear_request_profile()

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(30)

    assert (env["HERMES_HOME"], env["API_SERVER_PORT"]) == (str(homes.work), "18080")
    assert seen["terminal"] == {"cwd": f"{homes.work}/workspace", "port": "18080"}


def test_the_private_read_applies_the_config_defaults(homes):
    """The private dict gets the same documented defaults the shared cache gets."""
    from api import config

    def read():
        served = config.get_config()
        return (
            served["agent"]["personalities"].get("helpful"),
            served["experimental"].get("unified_session_db"),
        )

    assert _in_private_scope("work", read) == (config._DEFAULT_AGENT_PERSONALITIES["helpful"], False)



# ── Agent-only pins, CI-visible with stand-ins (#8981 review) ────────────────


def test_renamed_root_alias_keeps_the_root_env_with_a_stand_in_agent(
    homes, runtime, make_session, monkeypatch, stand_in_secret_scope
):
    """The renamed-root env scope with no real Agent (its Agent variant is
    ``test_renamed_root_alias_keeps_the_root_env_for_templated_config``): a session
    on a renamed root alias runs in master's ``profile_env_for_background_worker``
    scope, which binds root's ``.env`` as the Agent secret scope. Without that scope
    the templated record name can't be expanded, so it claims every name and the
    #8690 ambiguity verdict refuses the job."""
    from api import profiles

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
    assert runtime.agents and runtime.agents[0]["provider"] == "openai"


@pytest.mark.parametrize("spelling", ["${API_SERVER_PORT}", "${ API_SERVER_PORT }"])
@pytest.mark.parametrize("case", ["server_own_profile", "routed_profile"])
def test_the_closed_expansion_reads_process_global_variables_with_a_stand_in_agent(
    homes, runtime, make_session, monkeypatch, stand_in_hermes_constants, stand_in_secret_scope, case, spelling
):
    """The process-global expansion rule with no real Agent (its Agent variant is
    ``test_the_closed_expansion_still_reads_the_agents_process_global_variables``):
    the private read takes the variables the Agent's rule calls process-global from
    the process env, and nothing else from it, whether the named profile is the
    process's own (B1, launch snapshot) or a routed one. A padded name is read
    stripped."""
    from api import profiles

    stand_in_secret_scope.global_names.add("API_SERVER_PORT")
    if case == "server_own_profile":
        monkeypatch.setattr(profiles, "_active_profile", "work")
        stand_in_hermes_constants(homes.work)
    else:
        stand_in_hermes_constants(homes.root)
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
    assert runtime.agents[0]["base_url"] == "http://127.0.0.1:18080/v1"

    record = _private_record("work")
    assert (record["base_url"], record["key_env"]) == ("http://127.0.0.1:18080/v1", "${LAB_PROCESS_ONLY_SECRET}")
    assert "process-secret-value" not in json.dumps([record, runtime.agents], default=str)
