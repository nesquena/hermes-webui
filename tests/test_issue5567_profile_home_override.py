"""Regression test for #5567 — cross-profile HERMES_HOME race at the config reader.

Root cause: `profile_env_for_background_worker` mirrors the profile's HERMES_HOME
into the process-global `os.environ`, and the worker body runs outside the setup
lock. A concurrent cross-profile worker can clobber `os.environ["HERMES_HOME"]`
mid-body, so the agent config reader (`hermes_cli.config.get_config_path` /
`load_config`, which read `get_hermes_home()`) resolves the WRONG profile's
config — intermittent turn-init failures referencing another profile's provider.

Fix (#5567): when hermes-agent >= v0.18.0 exposes the context-local home
override (`hermes_constants.set_hermes_home_override`), the worker scope installs
it so `get_hermes_home()` resolves THIS task's profile home from task-local state,
immune to the process-global clobber — without serializing workers.

Per #2321's acceptance criteria, this exercises the REAL
`hermes_cli.config.load_config()` against a non-default profile with an
intentional mid-body `os.environ` clobber and NO mocking of the production reader.

Degrades gracefully on agents without the override (skips with a clear reason).
"""
import os
import json
import inspect
import textwrap
import contextlib
import queue
import threading
import sys
import types
from pathlib import Path

import pytest

# The production reader — imported unmocked, exactly as #2321 requires. Skip the
# whole module if the agent isn't importable in this environment.
config_mod = pytest.importorskip("hermes_cli.config")
hermes_constants = pytest.importorskip("hermes_constants")

HAS_OVERRIDE = hasattr(hermes_constants, "set_hermes_home_override") and hasattr(
    hermes_constants, "get_hermes_home"
)

from api import profiles as profiles_api  # noqa: E402


def _seed_profile_home(base: Path, name: str, provider: str, model: str) -> Path:
    home = base / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        textwrap.dedent(
            f"""\
            model:
              default: {model}
            provider: {provider}
            """
        ),
        encoding="utf-8",
    )
    return home


@pytest.mark.skipif(
    not HAS_OVERRIDE,
    reason="hermes-agent < v0.18.0: no set_hermes_home_override; WebUI degrades to the os.environ mirror",
)
def test_load_config_resolves_worker_profile_despite_env_clobber(tmp_path, monkeypatch):
    """The crux (#2321 criterion): inside profile_env_for_background_worker(A),
    a concurrent clobber of os.environ['HERMES_HOME']=B must NOT make the real
    load_config() read B — the context-local override pins A."""
    home_a = _seed_profile_home(tmp_path, "alpha", provider="anthropic", model="claude-x")
    home_b = _seed_profile_home(tmp_path, "beta", provider="ollama", model="llama-y")

    # The CM's INPUT (which profile home to scope to) — this is not the reader
    # under test; the reader is the real hermes_cli.config below.
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda name: home_a)

    # Establish a benign starting env, then simulate the race: while the worker
    # body for profile A runs, a sibling profile-B worker clobbers the global.
    monkeypatch.setenv("HERMES_HOME", str(home_a))

    # Clear any cached config so load_config actually hits the resolver.
    for fn in ("reload_config", "_reset_config_cache", "clear_config_cache"):
        if hasattr(config_mod, fn):
            try:
                getattr(config_mod, fn)()
            except Exception:
                pass

    with profiles_api.profile_env_for_background_worker("alpha", "test worker"):
        # The clobber: another profile's worker overwrites the process global.
        os.environ["HERMES_HOME"] = str(home_b)
        # get_config_path must resolve profile A via the context-local override,
        # NOT profile B from the clobbered os.environ.
        resolved = config_mod.get_config_path()
        assert resolved == home_a / "config.yaml", (
            f"config path must resolve profile A ({home_a}) via the context-local "
            f"override despite os.environ clobbered to B ({home_b}); got {resolved}"
        )
        # And the real load_config() must read A's model, not B's.
        cfg = config_mod.load_config()
        model_default = (cfg.get("model") or {}).get("default")
        assert model_default == "claude-x", (
            f"load_config must read profile A's model 'claude-x' despite the "
            f"HERMES_HOME clobber to B; got {model_default!r} (B is 'llama-y')"
        )


@pytest.mark.skipif(
    not HAS_OVERRIDE,
    reason="requires the v0.18.0 override to assert the override is cleared on exit",
)
def test_override_is_cleared_after_worker_exits(tmp_path, monkeypatch):
    """The context-local override must not leak past the worker scope."""
    home_a = _seed_profile_home(tmp_path, "alpha", provider="anthropic", model="claude-x")
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda name: home_a)

    assert hermes_constants.get_hermes_home_override() is None
    with profiles_api.profile_env_for_background_worker("alpha", "test worker"):
        assert hermes_constants.get_hermes_home_override() == str(home_a)
    # Cleared on exit — no leak into subsequent tasks on this context.
    assert hermes_constants.get_hermes_home_override() is None


def test_graceful_degradation_resolver_is_optional():
    """On an agent WITHOUT the override, the resolver returns None and the CM
    falls back to the pre-existing os.environ mirror — never raises. We assert
    the resolver is import-safe and boolean-clean regardless of agent version."""
    mod = profiles_api._resolve_hermes_home_override()
    if HAS_OVERRIDE:
        assert mod is not None and hasattr(mod, "set_hermes_home_override")
    else:
        assert mod is None  # older agent: graceful no-op, os.environ mirror stays


def test_profile_env_for_background_worker_uses_legacy_skill_module_patching(monkeypatch, tmp_path):
    """When override support is unavailable for this run, fallback still patches skill modules."""
    profile_home = tmp_path / "legacy-profile-home"
    profile_home.mkdir(parents=True, exist_ok=True)

    fake_skill_module = types.ModuleType("tools.skills_tool")
    fake_skill_module.HERMES_HOME = "default-home"
    fake_skill_module.SKILLS_DIR = "default-home/skills"
    fake_skill_manager_module = types.ModuleType("tools.skill_manager_tool")
    fake_skill_manager_module.HERMES_HOME = "default-home"
    fake_skill_manager_module.SKILLS_DIR = "default-home/skills"
    monkeypatch.setitem(sys.modules, "tools.skills_tool", fake_skill_module)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", fake_skill_manager_module)

    monkeypatch.setenv("HERMES_HOME", "default-home")
    monkeypatch.delenv("HERMES_TEST_PROFILE_ENV", raising=False)

    monkeypatch.setattr(profiles_api, "_hermes_home_override_available", False)
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda profile: profile_home)
    monkeypatch.setattr(
        profiles_api,
        "get_profile_runtime_env",
        lambda home: {"HERMES_TEST_PROFILE_ENV": "legacy-runtime"},
    )
    monkeypatch.setattr(
        profiles_api,
        "filter_runtime_env_for_gateway_parity",
        lambda env: env,
    )

    with profiles_api.profile_env_for_background_worker("legacy", "legacy worker"):
        assert os.environ.get("HERMES_HOME") == str(profile_home)
        assert os.environ.get("HERMES_TEST_PROFILE_ENV") == "legacy-runtime"
        assert fake_skill_module.HERMES_HOME == profile_home
        assert fake_skill_module.SKILLS_DIR == profile_home / "skills"

    assert fake_skill_module.HERMES_HOME == "default-home"
    assert fake_skill_module.SKILLS_DIR == "default-home/skills"
    assert os.environ.get("HERMES_HOME") == "default-home"
    assert os.environ.get("HERMES_TEST_PROFILE_ENV") is None


def test_run_agent_streaming_installs_and_resets_profile_home_override(tmp_path, monkeypatch):
    """Streaming must install the worker home override and clear it in teardown."""

    import api.streaming as _streaming

    _session_id = "streaming-override-session"
    _stream_id = "streaming-override-stream"
    _workspace = tmp_path / "workspace"
    _workspace.mkdir()
    _home = tmp_path / "alpha"
    _home.mkdir()

    class _Session:
        def __init__(self):
            self.session_id = _session_id
            self.workspace = str(_workspace)
            self.profile = "alpha"
            self.model = "gpt-4"
            self.model_provider = "hermes"
            self.messages = []
            self.context_messages = []
            self.path = str(_workspace / "session.json")
            self.active_stream_id = _stream_id
            self.pending_user_message = None
            self.pending_started_at = None
            self.pending_user_source = None
            self.pending_attachments = []

        def save(self, *args, **kwargs):
            return None

    _events = {}

    class _FakeMeter:
        def begin_session(self, *args, **kwargs):
            _events["begin_session"] = (_events.get("begin_session", 0) + 1)

        def end_session(self, *args, **kwargs):
            _events["end_session"] = (_events.get("end_session", 0) + 1)

        def get_interval(self):
            return 11.0

        def set_pending_started_at(self, stream_id, pending_started_at):
            _events.setdefault("set_pending_started_at", []).append(
                (stream_id, pending_started_at)
            )

        def get_ttft_ms(self, stream_id):
            _events.setdefault("get_ttft_ms", []).append(stream_id)
            return None

        def get_stats(self):
            return {}

        def record_token(self, *args, **kwargs):
            pass

        def record_reasoning(self, *args, **kwargs):
            pass

    q = queue.Queue()
    _streaming.STREAMS[_stream_id] = q

    def _set_thread_env(**kwargs):
        _events["set_thread_env"] = True

    def _set_override(profile_home: str):
        _events["set_override_home"] = profile_home
        return ("sentinel-module", None, True)

    def _reset_override(mod, reset_token, override_installed):
        _events["reset_override"] = (mod, reset_token, override_installed)

    def _patch_skill_home_modules(*_):
        _events["patch_skill_home_modules"] = _events.get("patch_skill_home_modules", 0) + 1
        raise AssertionError("patch_skill_home_modules must be skipped when context-local override installs")

    class _SentinelAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run_conversation(self, *args, **kwargs):
            _events["run_conversation"] = True
            raise RuntimeError("streaming test sentinel")

    def _get_ai_agent():
        return _SentinelAgent

    def _discover_mcp_tools():
        _events["discover_mcp_tools"] = _events.get("discover_mcp_tools", 0) + 1

    monkeypatch.setattr(_streaming, "register_active_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(_streaming, "update_active_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(_streaming, "get_session", lambda sid: _Session())
    monkeypatch.setattr(_streaming, "_get_session_agent_lock", lambda sid: contextlib.nullcontext())
    monkeypatch.setattr(_streaming, "_set_streaming_hermes_home_override", _set_override)
    monkeypatch.setattr(_streaming, "_reset_streaming_hermes_home_override", _reset_override)
    monkeypatch.setattr(_streaming, "_set_thread_env", _set_thread_env)
    monkeypatch.setattr(_streaming, "_prewarm_skill_tool_modules", lambda: None)
    monkeypatch.setattr(_streaming, "_install_streaming_cronjob_profile_wrapper", lambda: None)
    monkeypatch.setattr(_streaming, "_clear_thread_env", lambda: None)
    monkeypatch.setattr(_streaming, "_get_ai_agent", _get_ai_agent)
    monkeypatch.setattr(_streaming, "_materialize_pending_user_turn_before_error", lambda *a, **k: None)
    monkeypatch.setattr(_streaming, "_snapshot_and_append_partial_on_error", lambda *a, **k: None)
    monkeypatch.setattr(_streaming, "append_turn_journal_event_for_stream", lambda *a, **k: None)
    monkeypatch.setattr(_streaming, "meter", lambda: _FakeMeter())
    monkeypatch.setattr(_streaming, "RunJournalWriter", lambda *a, **k: None)
    monkeypatch.setattr(_streaming, "_build_agent_thread_env", lambda *a, **k: {})
    monkeypatch.setattr(
        _streaming,
        "resolve_model_provider",
        lambda model_with_provider_context, explicitly_picked=None: (model_with_provider_context, None, None),
    )
    monkeypatch.setattr(_streaming, "_runtime_preferred_base_url", lambda rt, provider, configured_base_url: configured_base_url)
    import api.profiles as profiles_api

    monkeypatch.setattr(profiles_api, "_skill_modules_support_profile_home", lambda profile_home: True)
    import api.config as _config_mod
    monkeypatch.setattr(_config_mod, "_resolve_cli_toolsets", lambda cfg: [])
    monkeypatch.setattr(_config_mod, "get_config_for_profile_home", lambda profile_home: {})
    _fake_mcp_module = types.ModuleType("tools.mcp_tool")
    _fake_mcp_module.discover_mcp_tools = _discover_mcp_tools
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", _fake_mcp_module)

    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda name: _home)
    monkeypatch.setattr(profiles_api, "get_profile_runtime_env", lambda home: {})
    monkeypatch.setattr(profiles_api, "filter_runtime_env_for_gateway_parity", lambda env: {})
    monkeypatch.setattr(profiles_api, "patch_skill_home_modules", _patch_skill_home_modules)
    monkeypatch.setattr(_streaming, "_apply_profile_home_context_to_streaming_model",
                        lambda model, provider_context, profile_home, has_profile: (model, provider_context, False))

    _streaming._run_agent_streaming(
        session_id=_session_id,
        msg_text="hi",
        model="gpt-4",
        workspace=str(_workspace),
        stream_id=_stream_id,
    )

    assert _events.get("set_override_home") == str(_home)
    assert _events.get("reset_override") == ("sentinel-module", None, True)
    assert _events.get("run_conversation") is True
    assert _events.get("discover_mcp_tools", 0) == 1
    assert _events.get("set_thread_env") is True
    assert _events.get("patch_skill_home_modules", 0) == 0
    assert _stream_id not in _streaming.STREAMS


def test_run_agent_streaming_falls_back_to_skill_module_patch_for_static_modules(
    tmp_path,
    monkeypatch,
):
    """Streaming should use snapshot/patch/restore when skill modules are static."""

    import api.streaming as _streaming

    _session_id = "streaming-override-static-session"
    _stream_id = "streaming-override-static-stream"
    _workspace = tmp_path / "workspace"
    _workspace.mkdir()
    _home = tmp_path / "alpha"
    _home.mkdir()

    class _Session:
        def __init__(self):
            self.session_id = _session_id
            self.workspace = str(_workspace)
            self.profile = "alpha"
            self.model = "gpt-4"
            self.model_provider = "hermes"
            self.messages = []
            self.context_messages = []
            self.path = str(_workspace / "session.json")
            self.active_stream_id = _stream_id
            self.pending_user_message = None
            self.pending_started_at = None
            self.pending_user_source = None
            self.pending_attachments = []

        def save(self, *args, **kwargs):
            return None

    _events = {}

    class _FakeMeter:
        def begin_session(self, *args, **kwargs):
            _events["begin_session"] = (_events.get("begin_session", 0) + 1)

        def end_session(self, *args, **kwargs):
            _events["end_session"] = (_events.get("end_session", 0) + 1)

        def get_interval(self):
            return 11.0

        def set_pending_started_at(self, stream_id, pending_started_at):
            _events.setdefault("set_pending_started_at", []).append(
                (stream_id, pending_started_at)
            )

        def get_ttft_ms(self, stream_id):
            _events.setdefault("get_ttft_ms", []).append(stream_id)
            return None

        def get_stats(self):
            return {}

    q = queue.Queue()
    _streaming.STREAMS[_stream_id] = q

    def _set_thread_env(**kwargs):
        _events["set_thread_env"] = True

    def _set_override(profile_home: str):
        _events["set_override_home"] = profile_home
        return ("sentinel-module", None, True)

    def _reset_override(mod, reset_token, override_installed):
        _events["reset_override"] = (mod, reset_token, override_installed)

    def _caller_info():
        _frame = inspect.currentframe()
        _wrapper = _frame.f_back if _frame is not None else None
        _caller = _wrapper.f_back if _wrapper is not None else None
        try:
            if _caller is None:
                return "<unknown>", "<unknown>", -1
            return (
                Path(_caller.f_code.co_filename).name,
                _caller.f_code.co_name,
                _caller.f_lineno,
            )
        finally:
            del _wrapper
            del _caller
            del _frame

    def _snapshot_skill_home_modules():
        _events.setdefault("snapshot_skill_home_modules", []).append(
            {"source": _caller_info(), "args": ()}
        )
        payload = {"snapshot": True}
        return payload

    def _patch_skill_home_modules(*args):
        _events.setdefault("patch_skill_home_modules", []).append(
            {"source": _caller_info(), "args": tuple(args)}
        )

    def _restore_skill_home_modules(snapshot):
        _events.setdefault("restore_skill_home_modules", []).append(
            {"source": _caller_info(), "snapshot": snapshot, "args": (snapshot,)}
        )

    class _SentinelAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run_conversation(self, *args, **kwargs):
            _events["run_conversation"] = True
            raise RuntimeError("streaming test sentinel")

    def _get_ai_agent():
        return _SentinelAgent

    def _discover_mcp_tools():
        _events["discover_mcp_tools"] = _events.get("discover_mcp_tools", 0) + 1

    monkeypatch.setattr(_streaming, "register_active_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(_streaming, "update_active_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(_streaming, "get_session", lambda sid: _Session())
    monkeypatch.setattr(_streaming, "_get_session_agent_lock", lambda sid: contextlib.nullcontext())
    monkeypatch.setattr(_streaming, "_set_streaming_hermes_home_override", _set_override)
    monkeypatch.setattr(_streaming, "_reset_streaming_hermes_home_override", _reset_override)
    monkeypatch.setattr(_streaming, "_set_thread_env", _set_thread_env)
    monkeypatch.setattr(_streaming, "_prewarm_skill_tool_modules", lambda: None)
    monkeypatch.setattr(_streaming, "_install_streaming_cronjob_profile_wrapper", lambda: None)
    monkeypatch.setattr(_streaming, "_clear_thread_env", lambda: None)
    monkeypatch.setattr(_streaming, "_get_ai_agent", _get_ai_agent)
    monkeypatch.setattr(_streaming, "_materialize_pending_user_turn_before_error", lambda *a, **k: None)
    monkeypatch.setattr(_streaming, "_snapshot_and_append_partial_on_error", lambda *a, **k: None)
    monkeypatch.setattr(_streaming, "append_turn_journal_event_for_stream", lambda *a, **k: None)
    monkeypatch.setattr(_streaming, "meter", lambda: _FakeMeter())
    monkeypatch.setattr(_streaming, "RunJournalWriter", lambda *a, **k: None)
    monkeypatch.setattr(_streaming, "_build_agent_thread_env", lambda *a, **k: {})
    monkeypatch.setattr(
        _streaming,
        "resolve_model_provider",
        lambda model_with_provider_context, explicitly_picked=None: (model_with_provider_context, None, None),
    )
    monkeypatch.setattr(_streaming, "_runtime_preferred_base_url", lambda rt, provider, configured_base_url: configured_base_url)
    import api.profiles as profiles_api

    monkeypatch.setattr(profiles_api, "snapshot_skill_home_modules", _snapshot_skill_home_modules)
    monkeypatch.setattr(profiles_api, "patch_skill_home_modules", _patch_skill_home_modules)
    monkeypatch.setattr(profiles_api, "restore_skill_home_modules", _restore_skill_home_modules)

    fake_skills_tool = types.ModuleType('tools.skills_tool')
    fake_skills_tool.HERMES_HOME = 'default-home'
    fake_skills_tool.SKILLS_DIR = 'default-home/skills'
    fake_skill_manager_tool = types.ModuleType('tools.skill_manager_tool')
    fake_skill_manager_tool.HERMES_HOME = 'default-home'
    fake_skill_manager_tool.SKILLS_DIR = 'default-home/skills'
    monkeypatch.setitem(sys.modules, 'tools.skills_tool', fake_skills_tool)
    monkeypatch.setitem(sys.modules, 'tools.skill_manager_tool', fake_skill_manager_tool)

    _fake_mcp_module = types.ModuleType("tools.mcp_tool")
    _fake_mcp_module.discover_mcp_tools = _discover_mcp_tools
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", _fake_mcp_module)

    import api.config as _config_mod
    monkeypatch.setattr(_config_mod, "_resolve_cli_toolsets", lambda cfg: [])
    monkeypatch.setattr(_config_mod, "get_config_for_profile_home", lambda profile_home: {})

    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda name: _home)
    monkeypatch.setattr(profiles_api, "get_profile_runtime_env", lambda home: {})
    monkeypatch.setattr(profiles_api, "filter_runtime_env_for_gateway_parity", lambda env: {})
    monkeypatch.setattr(
        _streaming,
        "_apply_profile_home_context_to_streaming_model",
        lambda model, provider_context, profile_home, has_profile: (model, provider_context, False),
    )

    _streaming._run_agent_streaming(
        session_id=_session_id,
        msg_text="hi",
        model="gpt-4",
        workspace=str(_workspace),
        stream_id=_stream_id,
    )

    assert _events.get("set_override_home") == str(_home)
    assert _events.get("reset_override") == ("sentinel-module", None, True)
    assert _events.get("run_conversation") is True
    # Two call paths can reach the legacy skill-module helpers now:
    # 1) upstream runtime-refresh model-resolution scope, and
    # 2) the PR-owned streaming fallback branch at _run_agent_streaming.
    # Assert that the streaming-owned fallback still executes once and fully
    # restores the snapshot it captured.
    _streaming_patch_calls = [
        entry["source"]
        for entry in _events.get("patch_skill_home_modules", [])
        if entry["source"][0] == "streaming.py"
    ]
    _streaming_snapshot_calls = [
        entry["source"]
        for entry in _events.get("snapshot_skill_home_modules", [])
        if entry["source"][0] == "streaming.py"
    ]
    _streaming_restore_calls = [
        entry["snapshot"]
        for entry in _events.get("restore_skill_home_modules", [])
        if entry["source"][0] == "streaming.py"
    ]
    assert len(_streaming_patch_calls) == 1
    assert len(_streaming_snapshot_calls) == 1
    assert _streaming_restore_calls == [{"snapshot": True}]
    assert _stream_id not in _streaming.STREAMS


def test_profile_env_for_background_worker_uses_static_modules_fallback_when_dynamic_checks_fail(
    tmp_path,
    monkeypatch,
):
    """Background worker falls back to module patching when dynamic checks fail."""

    import api.profiles as _profiles_api

    profile_home = tmp_path / "legacy-home"
    profile_home.mkdir(parents=True, exist_ok=True)

    events = {}
    fake_skill_module = types.ModuleType("tools.skills_tool")
    fake_skill_module.HERMES_HOME = "default-home"
    fake_skill_module.SKILLS_DIR = "default-home/skills"
    fake_skill_module._SKILLS_DIR_AT_IMPORT = "default-home/skills"

    fake_skill_manager_module = types.ModuleType("tools.skill_manager_tool")
    fake_skill_manager_module.HERMES_HOME = "default-home"
    fake_skill_manager_module.SKILLS_DIR = "default-home/skills"
    fake_skill_manager_module._SKILLS_DIR_AT_IMPORT = "default-home/skills"

    monkeypatch.setitem(sys.modules, "tools.skills_tool", fake_skill_module)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", fake_skill_manager_module)

    fake_constants = types.SimpleNamespace()

    def _set_override(profile_home: str):
        events["set_override_home"] = str(profile_home)
        return None

    def _reset_override(token):
        events["reset_token"] = token

    fake_constants.set_hermes_home_override = _set_override
    fake_constants.reset_hermes_home_override = _reset_override
    monkeypatch.setattr(_profiles_api, "_resolve_hermes_home_override", lambda: fake_constants)
    monkeypatch.setattr(_profiles_api, "_hermes_home_override_available", True)

    def _snapshot_skill_home_modules():
        events["snapshot"] = True
        return {"snapshot": True}

    def _patch_skill_home_modules(*_):
        events["patch"] = events.get("patch", 0) + 1
        fake_skill_module.HERMES_HOME = profile_home
        fake_skill_module.SKILLS_DIR = profile_home / "skills"
        fake_skill_manager_module.HERMES_HOME = profile_home
        fake_skill_manager_module.SKILLS_DIR = profile_home / "skills"

    def _restore_skill_home_modules(snapshot):
        events["restore"] = snapshot
        fake_skill_module.HERMES_HOME = "default-home"
        fake_skill_module.SKILLS_DIR = "default-home/skills"
        fake_skill_manager_module.HERMES_HOME = "default-home"
        fake_skill_manager_module.SKILLS_DIR = "default-home/skills"

    monkeypatch.setattr(_profiles_api, "snapshot_skill_home_modules", _snapshot_skill_home_modules)
    monkeypatch.setattr(_profiles_api, "patch_skill_home_modules", _patch_skill_home_modules)
    monkeypatch.setattr(_profiles_api, "restore_skill_home_modules", _restore_skill_home_modules)
    monkeypatch.setattr(_profiles_api, "get_hermes_home_for_profile", lambda profile: profile_home)
    monkeypatch.setattr(_profiles_api, "get_profile_runtime_env", lambda home: {})
    monkeypatch.setattr(_profiles_api, "filter_runtime_env_for_gateway_parity", lambda env: env)

    with _profiles_api.profile_env_for_background_worker("legacy", "legacy worker"):
        assert fake_skill_module.HERMES_HOME == profile_home
        assert fake_skill_module.SKILLS_DIR == profile_home / "skills"
        assert fake_skill_manager_module.HERMES_HOME == profile_home
        assert fake_skill_manager_module.SKILLS_DIR == profile_home / "skills"

    assert events.get("snapshot") is True
    assert events.get("patch") == 1
    assert events.get("restore") == {"snapshot": True}
    assert events.get("reset_token") is None
    assert fake_skill_module.HERMES_HOME == "default-home"
    assert fake_skill_manager_module.SKILLS_DIR == "default-home/skills"


def test_profile_env_for_background_worker_serializes_static_module_scope_with_lock(tmp_path, monkeypatch):
    """`_SKILL_HOME_MODULE_PATCH_LOCK` serializes concurrent static module scopes."""

    profile_alpha = tmp_path / "alpha"
    profile_beta = tmp_path / "beta"
    profile_alpha.mkdir()
    profile_beta.mkdir()

    fake_skill_module = types.ModuleType("tools.skills_tool")
    fake_skill_module.HERMES_HOME = "default-home"
    fake_skill_module.SKILLS_DIR = "default-home/skills"
    fake_skill_manager_module = types.ModuleType("tools.skill_manager_tool")
    fake_skill_manager_module.HERMES_HOME = "default-home"
    fake_skill_manager_module.SKILLS_DIR = "default-home/skills"
    monkeypatch.setitem(sys.modules, "tools.skills_tool", fake_skill_module)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", fake_skill_manager_module)

    events = {
        "set": [],
        "reset": [],
    }

    fake_constants = types.SimpleNamespace()

    def _set_override(profile_home: str):
        events["set"].append(str(profile_home))
        return None

    def _reset_override(reset_token):
        events["reset"].append(reset_token)

    fake_constants.set_hermes_home_override = _set_override
    fake_constants.reset_hermes_home_override = _reset_override
    monkeypatch.setattr(profiles_api, "_resolve_hermes_home_override", lambda: fake_constants)
    monkeypatch.setattr(profiles_api, "_hermes_home_override_available", True)
    monkeypatch.setattr(
        profiles_api,
        "get_hermes_home_for_profile",
        lambda profile: profile_alpha if profile == "alpha" else profile_beta,
    )
    monkeypatch.setattr(profiles_api, "get_profile_runtime_env", lambda home: {})
    monkeypatch.setattr(profiles_api, "filter_runtime_env_for_gateway_parity", lambda env: env)

    alpha_entered = threading.Event()
    beta_entered = threading.Event()
    alpha_release = threading.Event()
    worker_errors: list[tuple[str, BaseException]] = []

    def _worker_alpha() -> None:
        try:
            with profiles_api.profile_env_for_background_worker("alpha", "lock holder"):
                assert fake_skill_module.HERMES_HOME == profile_alpha
                assert fake_skill_module.SKILLS_DIR == profile_alpha / "skills"
                assert fake_skill_manager_module.HERMES_HOME == profile_alpha
                assert fake_skill_manager_module.SKILLS_DIR == profile_alpha / "skills"
                alpha_entered.set()
                assert alpha_release.wait(timeout=5)
                raise RuntimeError("alpha worker sentinel")
        except BaseException as exc:
            worker_errors.append(("alpha", exc))

    def _worker_beta() -> None:
        try:
            with profiles_api.profile_env_for_background_worker("beta", "lock waiter"):
                beta_entered.set()
                assert fake_skill_module.HERMES_HOME == profile_beta
                assert fake_skill_module.SKILLS_DIR == profile_beta / "skills"
                assert fake_skill_manager_module.HERMES_HOME == profile_beta
                assert fake_skill_manager_module.SKILLS_DIR == profile_beta / "skills"
        except BaseException as exc:
            worker_errors.append(("beta", exc))

    _thread_alpha = threading.Thread(target=_worker_alpha)
    _thread_beta = threading.Thread(target=_worker_beta)

    _thread_alpha.start()
    assert alpha_entered.wait(timeout=5)

    _thread_beta.start()
    assert not beta_entered.wait(timeout=0.2)
    alpha_release.set()
    assert beta_entered.wait(timeout=5)

    _thread_alpha.join(timeout=5)
    _thread_beta.join(timeout=5)

    assert not _thread_alpha.is_alive()
    assert not _thread_beta.is_alive()

    alpha_error = next((exc for name, exc in worker_errors if name == "alpha"), None)
    beta_error = next((exc for name, exc in worker_errors if name == "beta"), None)
    assert beta_error is None
    assert isinstance(alpha_error, RuntimeError)
    assert str(alpha_error) == "alpha worker sentinel"

    assert fake_skill_module.HERMES_HOME == "default-home"
    assert fake_skill_module.SKILLS_DIR == "default-home/skills"
    assert fake_skill_manager_module.HERMES_HOME == "default-home"
    assert fake_skill_manager_module.SKILLS_DIR == "default-home/skills"

    assert sorted(events["set"]) == [str(profile_alpha), str(profile_beta)]
    assert len(events["reset"]) == 2
    assert all(token is None for token in events["reset"])

    with profiles_api.profile_env_for_background_worker("alpha", "same-thread follow up"):
        assert fake_skill_module.HERMES_HOME == profile_alpha
        assert fake_skill_module.SKILLS_DIR == profile_alpha / "skills"
        assert fake_skill_manager_module.HERMES_HOME == profile_alpha
        assert fake_skill_manager_module.SKILLS_DIR == profile_alpha / "skills"

    assert fake_skill_module.HERMES_HOME == "default-home"
    assert fake_skill_module.SKILLS_DIR == "default-home/skills"
    assert fake_skill_manager_module.HERMES_HOME == "default-home"
    assert fake_skill_manager_module.SKILLS_DIR == "default-home/skills"


@pytest.mark.skipif(
    not HAS_OVERRIDE,
    reason="requires v0.18.0 context-local home override support",
)
def test_profile_env_for_background_worker_uses_real_modules_and_serializes_overlap(tmp_path, monkeypatch):
    """Real skill modules should serialize static fallback scopes across profiles."""

    try:
        import tools.skills_tool as skills_tool
        import tools.skill_manager_tool as skill_manager_tool
    except Exception as exc:
        pytest.skip(f"hermes-agent skill modules unavailable for this environment: {exc}")

    required_attrs = ("_SKILLS_DIR_AT_IMPORT", "SKILLS_DIR", "HERMES_HOME")
    for _name, _mod in (
        ("tools.skills_tool", skills_tool),
        ("tools.skill_manager_tool", skill_manager_tool),
    ):
        for _attr in required_attrs:
            if not hasattr(_mod, _attr):
                pytest.skip(f"{_name} missing {_attr}")

    if not hasattr(skills_tool, "skills_list"):
        pytest.skip("tools.skills_tool missing skills_list API")

    alpha_home = tmp_path / "alpha"
    beta_home = tmp_path / "beta"
    alpha_home.mkdir()
    beta_home.mkdir()

    alpha_skill = f"alpha-only-{tmp_path.name}"
    beta_skill = f"beta-only-{tmp_path.name}"

    def _write_skill(home: Path, name: str) -> None:
        skill_dir = home / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            textwrap.dedent(
                f"""\
                ---
                name: {name}
                description: skill for overlap test
                ---
                skill body
                """
            ),
            encoding="utf-8",
        )

    def _parse_skill_names(raw: object) -> set[str]:
        payload = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        data = json.loads(payload) if isinstance(payload, str) else payload
        if isinstance(data, dict):
            data = data.get("skills", [])
        return {
            item.get("name")
            for item in data or []
            if isinstance(item, dict) and item.get("name") is not None
        }

    _write_skill(alpha_home, alpha_skill)
    _write_skill(beta_home, beta_skill)

    baseline_override = hermes_constants.get_hermes_home_override()
    baseline_env_home = os.environ.get("HERMES_HOME")
    baseline_env_has = "HERMES_HOME" in os.environ

    baseline_skill_dir = skills_tool.SKILLS_DIR
    baseline_skill_home = getattr(skills_tool, "HERMES_HOME", None)
    baseline_skill_manager_dir = skill_manager_tool.SKILLS_DIR
    baseline_skill_manager_home = getattr(skill_manager_tool, "HERMES_HOME", None)

    alpha_skills_dir = str(alpha_home / "skills")

    # Start with both modules deliberately patched to alpha.
    skills_tool.HERMES_HOME = str(alpha_home)
    skills_tool.SKILLS_DIR = alpha_skills_dir
    skill_manager_tool.HERMES_HOME = str(alpha_home)
    skill_manager_tool.SKILLS_DIR = alpha_skills_dir

    monkeypatch.setenv("HERMES_HOME", str(alpha_home))
    monkeypatch.setattr(
        profiles_api,
        "get_hermes_home_for_profile",
        lambda profile: alpha_home if profile == "alpha" else beta_home,
    )
    monkeypatch.setattr(profiles_api, "get_profile_runtime_env", lambda home: {})
    monkeypatch.setattr(profiles_api, "filter_runtime_env_for_gateway_parity", lambda env: env)

    alpha_ready = threading.Event()
    alpha_listed = threading.Event()
    beta_started = threading.Event()
    beta_entered = threading.Event()
    release_alpha = threading.Event()

    seen: dict[str, set[str]] = {}
    observed: dict[str, dict[str, object]] = {}
    worker_errors: list[tuple[str, BaseException]] = []

    def _worker_alpha() -> None:
        try:
            with profiles_api.profile_env_for_background_worker("alpha", "real alpha worker"):
                alpha_ready.set()
                names = _parse_skill_names(skills_tool.skills_list())
                seen["alpha"] = names
                assert alpha_skill in names
                assert beta_skill not in names
                alpha_listed.set()
                assert release_alpha.wait(timeout=5)
        except BaseException as exc:  # pragma: no cover - defensive
            worker_errors.append(("alpha", exc))
        finally:
            observed["alpha"] = {
                "override": hermes_constants.get_hermes_home_override(),
            }

    def _worker_beta() -> None:
        try:
            beta_started.set()
            with profiles_api.profile_env_for_background_worker("beta", "real beta worker"):
                beta_entered.set()
                names = _parse_skill_names(skills_tool.skills_list())
                seen["beta"] = names
                assert beta_skill in names
                assert alpha_skill not in names
        except BaseException as exc:  # pragma: no cover - defensive
            worker_errors.append(("beta", exc))
        finally:
            observed["beta"] = {
                "override": hermes_constants.get_hermes_home_override(),
            }

    thread_alpha = threading.Thread(target=_worker_alpha)
    thread_beta = threading.Thread(target=_worker_beta)

    try:
        thread_alpha.start()
        assert alpha_ready.wait(timeout=5), "alpha worker should enter first"

        thread_beta.start()
        assert beta_started.wait(timeout=5), "beta worker should attempt to start"
        assert not beta_entered.wait(0.25), "beta must block on fallback scope lock while alpha holds it"

        assert alpha_listed.wait(timeout=5), "alpha must list skills while beta is waiting"

        release_alpha.set()
        assert beta_entered.wait(timeout=5), "beta should enter after alpha exits"

        thread_alpha.join(timeout=5)
        thread_beta.join(timeout=5)

        assert not thread_alpha.is_alive()
        assert not thread_beta.is_alive()
        assert not worker_errors

        assert seen.get("alpha", set()) == {alpha_skill}
        assert seen.get("beta", set()) == {beta_skill}

        assert observed["alpha"]["override"] == baseline_override
        assert observed["beta"]["override"] == baseline_override

        assert hermes_constants.get_hermes_home_override() == baseline_override
        assert skills_tool.HERMES_HOME == str(alpha_home)
        assert skills_tool.SKILLS_DIR == alpha_skills_dir
        assert skill_manager_tool.HERMES_HOME == str(alpha_home)
        assert skill_manager_tool.SKILLS_DIR == alpha_skills_dir
        assert os.environ.get("HERMES_HOME") == str(alpha_home)
    finally:
        thread_alpha.join(timeout=1)
        thread_beta.join(timeout=1)
        release_alpha.set()

        if baseline_env_has:
            if baseline_env_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = baseline_env_home
        else:
            os.environ.pop("HERMES_HOME", None)

        skills_tool.SKILLS_DIR = baseline_skill_dir
        if baseline_skill_home is not None:
            skills_tool.HERMES_HOME = baseline_skill_home
        else:
            skills_tool.__dict__.pop("HERMES_HOME", None)

        skill_manager_tool.SKILLS_DIR = baseline_skill_manager_dir
        if baseline_skill_manager_home is not None:
            skill_manager_tool.HERMES_HOME = baseline_skill_manager_home
        else:
            skill_manager_tool.__dict__.pop("HERMES_HOME", None)


def test_profile_env_for_background_worker_resets_override_when_dynamic_check_raises(
    tmp_path,
    monkeypatch,
):
    """A dynamic capability check exception must still clear the override state."""
    import api.profiles as _profiles_api

    profile_home = tmp_path / "legacy-home"
    profile_home.mkdir(parents=True, exist_ok=True)

    events = {}
    fake_skill_module = types.ModuleType("tools.skills_tool")
    fake_skill_module.HERMES_HOME = "default-home"
    fake_skill_module.SKILLS_DIR = "default-home/skills"
    fake_skill_manager_module = types.ModuleType("tools.skill_manager_tool")
    fake_skill_manager_module.HERMES_HOME = "default-home"
    fake_skill_manager_module.SKILLS_DIR = "default-home/skills"

    monkeypatch.setitem(sys.modules, "tools.skills_tool", fake_skill_module)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", fake_skill_manager_module)

    fake_constants = types.SimpleNamespace()

    def _set_override(profile_home: str):
        events["set"] = str(profile_home)
        return "override-token"

    def _reset_override(reset_token):
        events["reset"] = reset_token

    fake_constants.set_hermes_home_override = _set_override
    fake_constants.reset_hermes_home_override = _reset_override
    monkeypatch.setattr(_profiles_api, "_resolve_hermes_home_override", lambda: fake_constants)
    monkeypatch.setattr(_profiles_api, "_hermes_home_override_available", True)

    def _snapshot_skill_home_modules():
        events["snapshot"] = True
        return {"snapshot": True}

    def _patch_skill_home_modules(*_):
        events["patch"] = events.get("patch", 0) + 1
        fake_skill_module.HERMES_HOME = profile_home
        fake_skill_module.SKILLS_DIR = profile_home / "skills"
        fake_skill_manager_module.HERMES_HOME = profile_home
        fake_skill_manager_module.SKILLS_DIR = profile_home / "skills"

    def _restore_skill_home_modules(snapshot):
        events["restore"] = snapshot
        fake_skill_module.HERMES_HOME = "default-home"
        fake_skill_module.SKILLS_DIR = "default-home/skills"
        fake_skill_manager_module.HERMES_HOME = "default-home"
        fake_skill_manager_module.SKILLS_DIR = "default-home/skills"

    def _raise(*_):
        raise RuntimeError("dynamic capability probe failed")

    monkeypatch.setattr(_profiles_api, "snapshot_skill_home_modules", _snapshot_skill_home_modules)
    monkeypatch.setattr(_profiles_api, "patch_skill_home_modules", _patch_skill_home_modules)
    monkeypatch.setattr(_profiles_api, "restore_skill_home_modules", _restore_skill_home_modules)
    monkeypatch.setattr(_profiles_api, "_skill_modules_support_profile_home", _raise)
    monkeypatch.setattr(_profiles_api, "get_hermes_home_for_profile", lambda profile: profile_home)
    monkeypatch.setattr(_profiles_api, "get_profile_runtime_env", lambda home: {})
    monkeypatch.setattr(_profiles_api, "filter_runtime_env_for_gateway_parity", lambda env: env)

    with _profiles_api.profile_env_for_background_worker("legacy", "legacy worker"):
        assert fake_skill_module.HERMES_HOME == profile_home
        assert fake_skill_module.SKILLS_DIR == profile_home / "skills"

    assert events.get("set") == str(profile_home)
    assert events.get("reset") == "override-token"
    assert events.get("snapshot") is True
    assert events.get("patch") == 1
    assert events.get("restore") == {"snapshot": True}
    assert fake_skill_module.HERMES_HOME == "default-home"
    assert fake_skill_module.SKILLS_DIR == "default-home/skills"
    assert fake_skill_manager_module.HERMES_HOME == "default-home"
    assert fake_skill_manager_module.SKILLS_DIR == "default-home/skills"


@pytest.mark.skipif(
    not HAS_OVERRIDE,
    reason="requires v0.18.0 context-local home override support",
)
def test_run_agent_streaming_override_helpers_with_concurrent_skills_list_workers(tmp_path, monkeypatch):
    """Foreground streaming override and background profile scope resolve skills correctly.

    Foreground: call the streaming override helper for alpha and then clobber
    process env to beta.
    Background: run `profile_env_for_background_worker('beta', ...)` and then
    also clobber env to alpha.
    Both threads call real `tools.skills_tool.skills_list()` and should see their
    own profile's skill directory.
    """

    import api.streaming as _streaming
    try:
        import tools.skills_tool as skills_tool
        import tools.skill_manager_tool as skill_manager_tool
    except Exception as exc:  # pragma: no cover - hermes-agent dependency probe
        pytest.skip(f"hermes-agent skill modules unavailable for this environment: {exc}")

    _home_alpha = tmp_path / "alpha"
    _home_beta = tmp_path / "beta"
    _home_alpha.mkdir()
    _home_beta.mkdir()

    alpha_name = f"alpha-skill-{tmp_path.name}"
    beta_name = f"beta-skill-{tmp_path.name}"

    def _write_skill_dir(root: Path, name: str) -> None:
        skill_dir = root / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            textwrap.dedent(
                f"""\
                ---
                name: {name}
                description: {name} description
                ---
                skill body
                """
            ),
            encoding="utf-8",
        )

    _write_skill_dir(_home_alpha, alpha_name)
    _write_skill_dir(_home_beta, beta_name)

    _baseline_override = hermes_constants.get_hermes_home_override()
    _baseline_has_env = "HERMES_HOME" in os.environ
    _baseline_env = os.environ.get("HERMES_HOME")
    _baseline_skill_dir = skills_tool.SKILLS_DIR
    _baseline_skill_home = getattr(skills_tool, "HERMES_HOME", None)
    _baseline_manager_skill_dir = skill_manager_tool.SKILLS_DIR
    _baseline_manager_skill_home = getattr(skill_manager_tool, "HERMES_HOME", None)

    # Force deterministic resolution path independent of previous suite side effects.
    skills_tool.SKILLS_DIR = skills_tool._SKILLS_DIR_AT_IMPORT
    if _baseline_skill_home is not None:
        skills_tool.HERMES_HOME = _baseline_skill_home
    skill_manager_tool.SKILLS_DIR = getattr(
        skill_manager_tool,
        "_SKILLS_DIR_AT_IMPORT",
        _baseline_manager_skill_dir,
    )
    if _baseline_manager_skill_home is not None:
        skill_manager_tool.HERMES_HOME = _baseline_manager_skill_home
    os.environ["HERMES_HOME"] = str(_baseline_skill_home) if _baseline_skill_home else ""

    def _get_profile_home(name: str) -> Path:
        if name == "beta":
            return _home_beta
        if name == "alpha":
            return _home_alpha
        return _home_alpha

    # Keep the helper and module state deterministic for this threaded race test.
    assert _baseline_override is None
    _baseline_skills_tuple = (skills_tool._SKILLS_DIR_AT_IMPORT, _baseline_skill_home)
    _baseline_manager_tuple = (
        getattr(skill_manager_tool, "_SKILLS_DIR_AT_IMPORT", _baseline_manager_skill_dir),
        _baseline_manager_skill_home,
    )
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", _get_profile_home)

    start_barrier = threading.Barrier(2)
    clobber_barrier = threading.Barrier(2)
    _results: dict[str, set[str]] = {}
    _worker_errors: list[tuple[str, BaseException]] = []
    _lock = threading.Lock()
    _post_reset_overrides: dict[str, object | None] = {}
    _post_reset_skill_dirs: dict[str, Path] = {}
    _post_reset_skill_homes: dict[str, object | None] = {}
    _post_reset_manager_skill_dirs: dict[str, Path] = {}
    _post_reset_manager_skill_homes: dict[str, object | None] = {}
    _beta_scope_snapshots: dict[str, dict[str, tuple[object | None, object | None]]] = {}

    def _parse_skills(raw: str) -> set[str]:
        payload = json.loads(raw)
        return {
            item.get("name")
            for item in payload.get("skills", [])
            if isinstance(item, dict) and "name" in item
        }

    def _worker_alpha() -> None:
        mod_ctx, reset_token, override_installed = _streaming._set_streaming_hermes_home_override(
            str(_home_alpha)
        )
        try:
            start_barrier.wait(timeout=5)
            os.environ["HERMES_HOME"] = str(_home_beta)
            clobber_barrier.wait(timeout=5)

            names = _parse_skills(skills_tool.skills_list())
            with _lock:
                _results["alpha"] = names
        except BaseException as exc:
            with _lock:
                _worker_errors.append(("alpha", exc))
        finally:
            _streaming._reset_streaming_hermes_home_override(
                mod_ctx,
                reset_token,
                override_installed,
            )
            with _lock:
                _post_reset_overrides["alpha"] = hermes_constants.get_hermes_home_override()
                _post_reset_skill_dirs["alpha"] = skills_tool.SKILLS_DIR
                _post_reset_skill_homes["alpha"] = getattr(skills_tool, "HERMES_HOME", None)
                _post_reset_manager_skill_dirs["alpha"] = skill_manager_tool.SKILLS_DIR
                _post_reset_manager_skill_homes["alpha"] = getattr(skill_manager_tool, "HERMES_HOME", None)

    def _worker_beta() -> None:
        pre_scope = (
            skills_tool.SKILLS_DIR,
            getattr(skills_tool, "HERMES_HOME", None),
        )
        try:
            with profiles_api.profile_env_for_background_worker(
                "beta",
                "test-streaming-skill-list",
            ):
                start_barrier.wait(timeout=5)
                os.environ["HERMES_HOME"] = str(_home_alpha)
                clobber_barrier.wait(timeout=5)

                names = _parse_skills(skills_tool.skills_list())
                with _lock:
                    _results["beta"] = names
                    _beta_scope_snapshots["beta"] = {
                        "pre": pre_scope,
                        "during": (
                            skills_tool.SKILLS_DIR,
                            getattr(skills_tool, "HERMES_HOME", None),
                        ),
                    }
        except BaseException as exc:
            with _lock:
                _worker_errors.append(("beta", exc))
        finally:
            with _lock:
                _post_reset_overrides["beta"] = hermes_constants.get_hermes_home_override()
                _post_reset_skill_dirs["beta"] = skills_tool.SKILLS_DIR
                _post_reset_skill_homes["beta"] = getattr(skills_tool, "HERMES_HOME", None)
                _post_reset_manager_skill_dirs["beta"] = skill_manager_tool.SKILLS_DIR
                _post_reset_manager_skill_homes["beta"] = getattr(skill_manager_tool, "HERMES_HOME", None)

    _thread_alpha = threading.Thread(target=_worker_alpha)
    _thread_beta = threading.Thread(target=_worker_beta)

    _thread_alpha.start()
    _thread_beta.start()
    _thread_alpha.join(timeout=10)
    _thread_beta.join(timeout=10)

    try:
        assert not _thread_alpha.is_alive()
        assert not _thread_beta.is_alive()
        assert _results.get("alpha") is not None
        assert _results.get("beta") is not None
        if _worker_errors:
            raise _worker_errors[0][1]

        alpha_names = _results.get("alpha", set())
        beta_names = _results.get("beta", set())

        assert alpha_name in alpha_names
        assert beta_name not in alpha_names
        assert beta_name in beta_names
        assert alpha_name not in beta_names

        assert _post_reset_overrides["alpha"] == _baseline_override
        assert _post_reset_overrides["beta"] == _baseline_override

        assert _post_reset_skill_dirs["alpha"] == skills_tool._SKILLS_DIR_AT_IMPORT
        assert _post_reset_skill_dirs["beta"] == skills_tool._SKILLS_DIR_AT_IMPORT
        assert _post_reset_manager_skill_dirs["alpha"] == _baseline_manager_tuple[0]
        assert _post_reset_manager_skill_dirs["beta"] == _baseline_manager_tuple[0]
        assert _post_reset_skill_homes["alpha"] == _baseline_skill_home
        assert _post_reset_skill_homes["beta"] == _baseline_skill_home
        assert _post_reset_manager_skill_homes["alpha"] == _baseline_manager_skill_home
        assert _post_reset_manager_skill_homes["beta"] == _baseline_manager_skill_home

        beta_snapshot = _beta_scope_snapshots.get("beta")
        assert beta_snapshot is not None
        assert beta_snapshot["pre"] == _baseline_skills_tuple
        assert beta_snapshot["during"] == _baseline_skills_tuple
    finally:
        if _baseline_has_env:
            os.environ["HERMES_HOME"] = _baseline_env or ""
        else:
            os.environ.pop("HERMES_HOME", None)
        skills_tool.SKILLS_DIR = _baseline_skill_dir
        if _baseline_skill_home is not None:
            skills_tool.HERMES_HOME = _baseline_skill_home
        skill_manager_tool.SKILLS_DIR = _baseline_manager_skill_dir
        if _baseline_manager_skill_home is not None:
            skill_manager_tool.HERMES_HOME = _baseline_manager_skill_home
