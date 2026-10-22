"""Issue #8452 — WebUI agent turns never bind HERMES_SESSION_PROFILE.

ROOT CAUSE (verified line-by-line against upstream/master):
  ``tools/terminal_tool.py::_resolve_container_task_id`` derives the persistent
  Docker container key from ``_current_session_profile()`` →
  ``gateway.session_context.get_session_env("HERMES_SESSION_PROFILE")`` — the
  ``_SESSION_PROFILE`` ContextVar first, then the process env. WebUI set
  neither: ``_build_agent_thread_env`` never emitted the var, no ContextVar was
  bound per turn, and the non-streaming ``_handle_chat_sync`` only stamped
  HERMES_SESSION_KEY. A named-profile (e.g. ``work``) WebUI session therefore
  derived the shared ``"default"`` container key and the user saw a second,
  empty default-keyed sandbox instead of their profile's container.

Maintainer-directed fix shape (nesquena-hermes, 2026-10-21):
  bind the resolved session profile into the per-turn identity lifecycle
  (``_set_turn_session_identity`` / ``_reset_turn_session_identity``, gaining a
  ``gateway.session_context._SESSION_PROFILE`` ContextVar token with an
  optional-import compat path), emit ``HERMES_SESSION_PROFILE`` from
  ``_build_agent_thread_env``, keep a process-env save/restore under
  ``_ENV_LOCK`` as the fallback, and mirror the same bind+restore on the
  non-streaming api/routes.py path. #8467 (env-only, streaming-only) was
  rejected: the thread-env dict does not reach the Agent's ContextVar-first
  consumers, the process env fallback is unsafe under concurrent profiles, and
  the non-streaming path was uncovered.

These tests use REAL modules (no mocks of the code under test) wherever the
agent package is importable, so each one is RED before the fix and GREEN
after — never a tautology. Where the agent package is NOT importable
(hermes-webui CI installs no hermes-agent), ``_session_context`` injects a
faithful fake of ``gateway.session_context`` into ``sys.modules`` instead:
production imports that module lazily per call, so the REAL bind/reset code
still drives the ContextVars and every assertion below still exercises the
shipped path.
"""
from __future__ import annotations

import contextvars
import importlib
import os
import queue
import threading
import time
from pathlib import Path

import pytest


def _streaming():
    return importlib.import_module("api.streaming")


def _install_fake_session_context(monkeypatch):
    """Install a faithful stand-in for the Agent's ``gateway.session_context``.

    hermes-webui CI installs no hermes-agent, so the real module is not
    importable there (the conftest's agent-dir discovery finds nothing and
    ``import gateway.session_context`` raises ``ModuleNotFoundError``).
    Production binds that module LAZILY per call (``from
    gateway.session_context import _SESSION_KEY`` inside
    ``_set_turn_session_identity``), so injecting this module into
    ``sys.modules`` makes the real bind/reset code drive the ContextVars
    below — the same lazy-import fake pattern ``_StreamingScene`` uses for
    ``hermes_cli`` / ``hermes_state``.

    The surface mirrors the real module for the symbols this file and
    production touch: an ``_UNSET`` sentinel, one ContextVar per session var
    (all defaulting to ``_UNSET`` = "never bound here", so
    ``get_session_env`` falls back to the process env), and
    ``get_session_env``'s ContextVar-then-env precedence. Extend the table
    if production starts importing another var.
    """
    import sys
    import types
    from contextvars import ContextVar

    _UNSET = object()
    _VAR_BY_ENV = {
        "HERMES_SESSION_KEY": ContextVar("HERMES_SESSION_KEY", default=_UNSET),
        "HERMES_UI_SESSION_ID": ContextVar("HERMES_UI_SESSION_ID", default=_UNSET),
        "HERMES_SESSION_PROFILE": ContextVar("HERMES_SESSION_PROFILE", default=_UNSET),
    }
    _ATTR_BY_ENV = {
        "HERMES_SESSION_KEY": "_SESSION_KEY",
        "HERMES_UI_SESSION_ID": "_SESSION_UI_SESSION_ID",
        "HERMES_SESSION_PROFILE": "_SESSION_PROFILE",
    }

    def get_session_env(name, default=""):
        var = _VAR_BY_ENV.get(name)
        if var is not None and (value := var.get()) is not _UNSET:
            return value
        return os.getenv(name, default)

    mod = types.ModuleType("gateway.session_context")
    mod._UNSET = _UNSET
    for env_name, var in _VAR_BY_ENV.items():
        setattr(mod, _ATTR_BY_ENV[env_name], var)
    mod.get_session_env = get_session_env

    gateway_pkg = types.ModuleType("gateway")
    gateway_pkg.session_context = mod
    monkeypatch.setitem(sys.modules, "gateway", gateway_pkg)
    monkeypatch.setitem(sys.modules, "gateway.session_context", mod)
    return mod


def _session_context(monkeypatch):
    """The Agent's session_context module — the REAL one when hermes-agent is
    installed, the faithful fake above where it is not (CI)."""
    try:
        return importlib.import_module("gateway.session_context")
    except ModuleNotFoundError:
        return _install_fake_session_context(monkeypatch)


# ---------------------------------------------------------------------------
# _build_agent_thread_env — the thread env handed to the in-process Agent
# ---------------------------------------------------------------------------

def test_build_agent_thread_env_carries_the_resolved_profile():
    """The thread env the Agent runs with must name the turn's profile."""
    env = _streaming()._build_agent_thread_env(
        {"TERMINAL_ENV": "docker"},
        "/active/workspace",
        "active-session",
        "/active/profile/home",
        "work",
    )
    assert env["HERMES_SESSION_PROFILE"] == "work"
    # Per-run values still win over the profile defaults.
    assert env["TERMINAL_CWD"] == "/active/workspace"
    assert env["HERMES_SESSION_KEY"] == "active-session"


def test_build_agent_thread_env_omits_profile_when_unresolved():
    """No resolved profile => key omitted, so the legacy env fallback survives.

    An explicit "" would MASK the process env in ``get_session_env``
    (ContextVar-wins-even-when-empty semantics), breaking CLI/cron consumers.
    """
    env = _streaming()._build_agent_thread_env(
        {}, "/active/workspace", "active-session", "/active/profile/home",
    )
    assert "HERMES_SESSION_PROFILE" not in env


# ---------------------------------------------------------------------------
# _set/_reset_turn_session_identity — the ContextVar token (root fix)
# ---------------------------------------------------------------------------

def test_set_turn_session_identity_binds_the_profile_contextvar(monkeypatch):
    """The REAL _SESSION_PROFILE ContextVar is bound for the turn's context."""
    sc = _session_context(monkeypatch)
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    assert sc.get_session_env("HERMES_SESSION_PROFILE", "") == ""

    tokens = _streaming()._set_turn_session_identity(
        "sid-8452", workspace="/ws", profile_name="work",
    )
    try:
        assert "session_profile" in tokens, (
            "_set_turn_session_identity must bind gateway.session_context."
            "_SESSION_PROFILE (the ContextVar terminal_tool reads FIRST)"
        )
        assert sc.get_session_env("HERMES_SESSION_PROFILE", "") == "work"
        assert sc._SESSION_PROFILE.get() == "work"
    finally:
        _streaming()._reset_turn_session_identity(tokens)
    # Reset-token semantics: back to _UNSET, so the env fallback resumes.
    assert sc._SESSION_PROFILE.get() is sc._UNSET
    assert sc.get_session_env("HERMES_SESSION_PROFILE", "") == ""


def test_reset_turn_session_identity_restores_the_previous_profile(monkeypatch):
    """No bleed: a nested turn restores the OUTER binding, not _UNSET."""
    sc = _session_context(monkeypatch)
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    outer = sc._SESSION_PROFILE.set("outer-profile")
    try:
        tokens = _streaming()._set_turn_session_identity("sid-8452", profile_name="work")
        assert sc.get_session_env("HERMES_SESSION_PROFILE", "") == "work"
        _streaming()._reset_turn_session_identity(tokens)
        assert sc.get_session_env("HERMES_SESSION_PROFILE", "") == "outer-profile", (
            "a completed named-profile turn must not clobber the surrounding "
            "binding — that is exactly the bleed #8452 reports"
        )
    finally:
        sc._SESSION_PROFILE.reset(outer)


def test_interleaved_concurrent_profile_turns_do_not_bleed(monkeypatch):
    """THE invariant. Two concurrent turns on different profiles.

    Each worker thread binds its own profile AND writes the process-env
    fallback while the other turn is mid-run (the exact #8467 env-only race:
    A publishes, B replaces, A would resolve through B). The ContextVar must
    win per-thread, and a copy_context() child (concurrent tool batches) must
    inherit the PARENT turn's profile.
    """
    sc = _session_context(monkeypatch)
    streaming = _streaming()
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)

    barrier = threading.Barrier(2)
    observed: dict[str, str] = {}
    errors: list[BaseException] = []

    def _turn(profile: str, other: str) -> None:
        try:
            tokens = streaming._set_turn_session_identity(
                f"sid-{profile}", profile_name=profile,
            )
            try:
                # The process-env fallback write this turn's _ENV_LOCK block
                # performs — visible to the other thread mid-run.
                os.environ["HERMES_SESSION_PROFILE"] = profile
                barrier.wait(timeout=10)
                # The other turn's env write has landed by now.
                os.environ["HERMES_SESSION_PROFILE"] = other
                barrier.wait(timeout=10)
                observed[profile] = sc.get_session_env("HERMES_SESSION_PROFILE", "")
                child: dict[str, str] = {}

                def _child() -> None:
                    child["profile"] = sc.get_session_env("HERMES_SESSION_PROFILE", "")

                contextvars.copy_context().run(_child)
                observed[f"{profile}-child"] = child["profile"]
            finally:
                streaming._reset_turn_session_identity(tokens)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=_turn, args=("work", "play")),
        threading.Thread(target=_turn, args=("play", "work")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors, f"concurrent turn raised: {errors!r}"
    assert observed["work"] == "work", (
        f"turn 'work' resolved profile {observed['work']!r} — a concurrent "
        "turn's process-env write bled through the fallback"
    )
    assert observed["play"] == "play"
    assert observed["work-child"] == "work"
    assert observed["play-child"] == "play"


def test_profile_bind_degrades_when_agent_lacks_the_contextvar(monkeypatch):
    """Optional-import compat: an Agent without _SESSION_PROFILE must not break.

    Older hermes-agent builds have no ``gateway.session_context.
    _SESSION_PROFILE``; the bind must skip (and the reset must no-op) while
    every sibling binding still lands, and the process-env fallback below
    still carries the profile.
    """
    sc = _session_context(monkeypatch)
    streaming = _streaming()
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    # Capture the real ContextVar first: the delattr below removes the module
    # attribute, which is exactly what an older Agent's import would miss.
    profile_ctxvar = sc._SESSION_PROFILE
    monkeypatch.delattr(sc, "_SESSION_PROFILE", raising=False)

    tokens = streaming._set_turn_session_identity(
        "sid-8452-compat", profile_name="work",
    )
    try:
        assert "session_profile" not in tokens
        # Siblings unaffected — asserted on the ContextVar itself: the process
        # env legitimately carries its own HERMES_SESSION_KEY in some
        # environments (get_session_env's documented fallback).
        assert sc._SESSION_KEY.get() == "sid-8452-compat"
        assert profile_ctxvar.get() is sc._UNSET
    finally:
        streaming._reset_turn_session_identity(tokens)
    assert sc._SESSION_KEY.get() is sc._UNSET


# ---------------------------------------------------------------------------
# End-to-end derivation through the REAL agent consumer
# ---------------------------------------------------------------------------

def test_named_profile_derives_the_profile_scoped_container_key(monkeypatch):
    """With the profile bound, the REAL _resolve_container_task_id keys
    ``profile:<name>``; unbound it collapses to the shared ``default`` key —
    the second empty sandbox from the issue."""
    terminal_tool = pytest.importorskip(
        "tools.terminal_tool",
        reason="agent tools.terminal_tool not importable in this environment",
    )
    streaming = _streaming()
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    scope = terminal_tool._SessionScope(env_type="docker", persistent=True)
    assert scope.docker_profile_scoped is True
    monkeypatch.setattr(terminal_tool, "_session_scope", lambda: scope)
    monkeypatch.setattr(terminal_tool, "_current_session_key", lambda: "sid-8452")

    tokens = streaming._set_turn_session_identity("sid-8452", profile_name="work")
    try:
        key = terminal_tool._resolve_container_task_id("task-8452")
        assert key == "profile:work", (
            f"named-profile turn must derive its own container key, got {key!r}"
        )
    finally:
        streaming._reset_turn_session_identity(tokens)
    # The bug contrast: nothing bound => the shared default key.
    assert terminal_tool._resolve_container_task_id("task-8452") == "default"


# ---------------------------------------------------------------------------
# Streaming worker — bind during the turn, restore on EVERY exit path
# ---------------------------------------------------------------------------

class _StreamingScene:
    """Drive the REAL _run_agent_streaming worker with a sentinel Agent.

    Adapted from tests/test_issue2965_streaming_sessiondb_profile_home.py:
    only the external Agent/provider is replaced; the worker's own profile
    resolution, identity lifecycle, env mutation and teardown all run for real.
    """

    def __init__(self, monkeypatch, tmp_path, *, profile, active_profile_name=None, on_run=None, tag="", real_profile_env=False):
        from typing import Callable, Optional

        from api import config as cfg
        from api import oauth
        from api import profiles
        from api import streaming

        self.streaming = streaming
        self.cfg = cfg
        self.profile = profile
        self.observed: dict[str, object] = {}
        self.on_run: Optional[Callable[[], None]] = on_run

        profile_home = tmp_path / "hermes-home" / "profiles" / (profile or "default")
        profile_home.mkdir(parents=True, exist_ok=True)
        self.profile_home = profile_home
        _tag = f"-{tag}" if tag else ""
        self.session_id = f"8452-{profile or 'noprofile'}{_tag}-session"
        self.stream_id = f"8452-{profile or 'noprofile'}{_tag}-stream"

        scene = self
        _profile_arg = profile

        class FakeSession:
            session_id = scene.session_id
            title = "Issue 8452"
            workspace = str(tmp_path)
            model = "test-model"
            model_provider = None
            profile = _profile_arg
            personality = None
            messages = []
            context_messages = []
            tool_calls = []
            input_tokens = 0
            output_tokens = 0
            estimated_cost = None
            context_length = 0
            threshold_tokens = 0
            last_prompt_tokens = 0
            active_stream_id = scene.stream_id
            pending_user_message = None
            pending_attachments = []
            pending_started_at = None
            llm_title_generated = True

            def save(self, *args, **kwargs):
                return None

            def compact(self):
                return {
                    "session_id": self.session_id,
                    "title": self.title,
                    "workspace": self.workspace,
                    "model": self.model,
                    "created_at": 0,
                    "updated_at": 0,
                    "pinned": False,
                    "archived": False,
                    "project_id": None,
                    "profile": self.profile,
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "estimated_cost": self.estimated_cost,
                    "personality": self.personality,
                }

        class FakeSessionDB:
            def __init__(self, db_path=None):
                self.db_path = db_path

            def close(self):
                return None

        class SentinelAgent:
            def __init__(self, **kwargs):
                self.session_db = kwargs.get("session_db")
                self._session_db = self.session_db
                self.session_prompt_tokens = 0
                self.session_completion_tokens = 0
                self.session_estimated_cost_usd = None
                self.context_compressor = None
                self._last_error = None
                self.ephemeral_system_prompt = None

            def run_conversation(self, **kwargs):
                import gateway.session_context as sc_mod

                scene.observed["ctx_profile"] = (
                    "UNSET"
                    if sc_mod._SESSION_PROFILE.get() is sc_mod._UNSET
                    else sc_mod._SESSION_PROFILE.get()
                )
                scene.observed["resolved_profile"] = sc_mod.get_session_env(
                    "HERMES_SESSION_PROFILE", "<unset>"
                )
                scene.observed["env_profile"] = os.environ.get("HERMES_SESSION_PROFILE")
                scene.observed["thread_env_profile"] = getattr(
                    cfg._thread_ctx, "env", {}
                ).get("HERMES_SESSION_PROFILE")
                if scene.on_run is not None:
                    scene.on_run()
                history = list(kwargs.get("conversation_history") or [])
                return {
                    "messages": history
                    + [
                        {"role": "user", "content": kwargs.get("persist_user_message", "")},
                        {"role": "assistant", "content": "ok"},
                    ]
                }

            def interrupt(self, _message):
                return None

        self.agent_cls = SentinelAgent
        # NOTE (#8452 review round 2): the constructor's on_run hook MUST stay
        # alive here. An earlier `self.on_run = None` in this slot overwrote
        # the constructor argument, so the failure/cancellation tests below
        # silently exercised the SUCCESS path (their triggers never fired).
        # Callback entry is now asserted by those tests directly.

        import sys
        import types

        fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
        fake_runtime_module.resolve_runtime_provider = (
            lambda requested=None, **_kw: {
                "provider": requested or "test-provider",
                "api_key": "synthetic-key",
                "base_url": None,
            }
        )
        fake_hermes_cli = types.ModuleType("hermes_cli")
        fake_hermes_cli.runtime_provider = fake_runtime_module
        fake_hermes_state = types.ModuleType("hermes_state")
        fake_hermes_state.SessionDB = FakeSessionDB

        self.session = FakeSession()
        monkeypatch.setattr(streaming, "get_session", lambda _sid: self.session)
        monkeypatch.setattr(streaming, "_get_ai_agent", lambda: SentinelAgent)
        monkeypatch.setattr(
            streaming,
            "resolve_model_provider",
            lambda _model, **_kw: ("test-model", "test-provider", None),
        )
        monkeypatch.setattr(streaming, "_maybe_schedule_title_refresh", lambda *a, **k: None)
        monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _p: profile_home)
        if not real_profile_env:
            monkeypatch.setattr(profiles, "get_profile_runtime_env", lambda _home: {})
        # real_profile_env=True leaves the REAL api.profiles.get_profile_runtime_env
        # in place so it reads a real .env file the test writes into this
        # scene's profile home — the round-3 controls must exercise the
        # loader → worker → cleanup chain for real, not stubbed to {}.
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: active_profile_name)
        monkeypatch.setattr(
            oauth,
            "resolve_runtime_provider_with_anthropic_env_lock",
            lambda _resolver, requested=None, **_kw: {
                "provider": requested or "test-provider",
                "api_key": "synthetic-key",
                "base_url": None,
            },
        )
        monkeypatch.setattr("api.config.get_config", lambda: {})
        monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda _cfg: [])
        monkeypatch.setattr("api.config.load_settings", lambda: {})
        monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
        monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
        monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

        for registry in (
            "STREAMS", "CANCEL_FLAGS", "AGENT_INSTANCES", "STREAM_PARTIAL_TEXT",
            "STREAM_REASONING_TEXT", "STREAM_LIVE_TOOL_CALLS",
        ):
            getattr(streaming, registry).clear()
        with cfg.SESSION_AGENT_CACHE_LOCK:
            cfg.SESSION_AGENT_CACHE.clear()
        streaming.STREAMS[self.stream_id] = queue.Queue()

    def run(self):
        old_home = os.environ.get("HERMES_HOME")
        try:
            self.streaming._run_agent_streaming(
                session_id=self.session_id,
                msg_text="hello from 8452",
                model="test-model",
                model_provider="test-provider",
                workspace=self.session.workspace,
                stream_id=self.stream_id,
            )
        finally:
            if old_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_home


def _assert_identity_restored(sc, monkeypatch):
    """After ANY exit, the turn leaked nothing into this context or the env."""
    assert sc._SESSION_PROFILE.get() is sc._UNSET, (
        "per-turn _SESSION_PROFILE must be reset-token restored after the turn"
    )
    assert os.environ.get("HERMES_SESSION_PROFILE") is None, (
        "process-env HERMES_SESSION_PROFILE fallback must be restored after the turn"
    )


def _drain_stream_events(q):
    """Collect every event type the worker published, without blocking.

    The worker pops its stream registry in the outer finally, so the queue
    object must be captured BEFORE the run and drained afterwards.
    """
    seen: list[object] = []
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        seen.append(item[0] if isinstance(item, tuple) and item else item)
    return seen


def test_streaming_named_profile_turn_binds_and_restores_on_success(monkeypatch, tmp_path):
    sc = _session_context(monkeypatch)
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    scene = _StreamingScene(monkeypatch, tmp_path, profile="work")
    scene.run()
    assert scene.observed["ctx_profile"] == "work", (
        "the streaming worker must bind the resolved profile into the turn's "
        f"identity context; observed {scene.observed.get('ctx_profile')!r}"
    )
    assert scene.observed["resolved_profile"] == "work"
    assert scene.observed["env_profile"] == "work"
    assert scene.observed["thread_env_profile"] == "work"
    _assert_identity_restored(sc, monkeypatch)


def test_streaming_named_profile_turn_restores_on_failure(monkeypatch, tmp_path):
    sc = _session_context(monkeypatch)
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)

    fired: list[str] = []

    def _boom():
        fired.append("boom")
        raise RuntimeError("sentinel agent failure")

    scene = _StreamingScene(monkeypatch, tmp_path, profile="work", on_run=_boom)
    events_q = scene.streaming.STREAMS[scene.stream_id]
    # The worker's outer handler catches the agent error and emits apperror;
    # what matters for #8452 is that its finally still restores the identity.
    scene.run()
    # Review round-2 finding 2: the trigger must ACTUALLY fire. With the old
    # `self.on_run = None` overwrite this list stayed empty and the test
    # exercised the success path instead.
    assert fired == ["boom"], (
        "the failure trigger never ran — the scene discarded its on_run hook, "
        "so this test was a false-green success-path run"
    )
    assert scene.observed["ctx_profile"] == "work"
    assert "apperror" in _drain_stream_events(events_q), (
        "a failed agent turn must surface an apperror event, not silently "
        "succeed — the callback entry alone does not prove the exit path"
    )
    _assert_identity_restored(sc, monkeypatch)


def test_streaming_named_profile_turn_restores_on_cancellation(monkeypatch, tmp_path):
    sc = _session_context(monkeypatch)
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)

    fired: list[str] = []

    def _cancel_mid_turn():
        # The Stop button: flip the worker's own cancel event mid-run.
        fired.append("cancel")
        scene.streaming.CANCEL_FLAGS[scene.stream_id].set()

    scene = _StreamingScene(
        monkeypatch, tmp_path, profile="work", on_run=_cancel_mid_turn,
    )
    events_q = scene.streaming.STREAMS[scene.stream_id]
    scene.run()
    assert fired == ["cancel"], (
        "the cancellation trigger never ran — the scene discarded its on_run "
        "hook, so this test was a false-green success-path run"
    )
    assert scene.observed["ctx_profile"] == "work", (
        "profile must be bound for the whole turn, including a cancelled one"
    )
    assert "cancel" in _drain_stream_events(events_q), (
        "a cancelled turn must publish its cancel event — callback entry "
        "alone does not prove the cancellation exit path"
    )
    _assert_identity_restored(sc, monkeypatch)


def test_streaming_next_turn_does_not_inherit_the_previous_profile(monkeypatch, tmp_path):
    """Maintainer criterion (3): a named turn must not bleed into the next."""
    sc = _session_context(monkeypatch)
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    first = _StreamingScene(monkeypatch, tmp_path, profile="work")
    first.run()
    assert first.observed["ctx_profile"] == "work"
    _assert_identity_restored(sc, monkeypatch)

    second = _StreamingScene(
        monkeypatch, tmp_path, profile=None, active_profile_name=None,
    )
    second.run()
    assert second.observed["ctx_profile"] == "UNSET", (
        "the profile-less turn after a named-profile turn must see no bound "
        f"profile; observed {second.observed.get('ctx_profile')!r}"
    )
    assert second.observed["resolved_profile"] == "<unset>"
    assert second.observed["env_profile"] is None
    assert second.observed["thread_env_profile"] is None
    _assert_identity_restored(sc, monkeypatch)


# ---------------------------------------------------------------------------
# Non-streaming path — api/routes.py::_handle_chat_sync mirrors bind+restore
# ---------------------------------------------------------------------------

def _drive_sync_chat(monkeypatch, tmp_path, agent_cls, *, profile, message="Plan the release"):
    import sys
    from types import SimpleNamespace

    import api.config as config
    import api.models as models
    import api.routes as routes

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "get_session", models.get_session)
    monkeypatch.setattr(routes, "title_from", models.title_from)
    monkeypatch.setattr(config, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value, **_kw: tmp_path)
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])

    session = models.Session(
        session_id="f8452sync",
        workspace=str(tmp_path),
        messages=[],
        context_messages=[],
        model="test-model",
        model_provider="test-provider",
        profile=profile,
    )
    session.save(touch_updated_at=False)
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=agent_cls))

    class Handler:
        def __init__(self):
            self.headers = {}
            self.status = None
            self.wfile = SimpleNamespace(write=lambda _body: None)

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            return None

        def end_headers(self):
            return None

    handler = Handler()
    routes._handle_chat_sync(
        handler, {"session_id": session.session_id, "message": message, "workspace": str(tmp_path)}
    )
    return handler


def _sync_sentinel(observed, on_run=None):
    class SyncAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **kwargs):
            import gateway.session_context as sc_mod

            observed["ctx_profile"] = (
                "UNSET"
                if sc_mod._SESSION_PROFILE.get() is sc_mod._UNSET
                else sc_mod._SESSION_PROFILE.get()
            )
            observed["resolved_profile"] = sc_mod.get_session_env(
                "HERMES_SESSION_PROFILE", "<unset>"
            )
            observed["env_profile"] = os.environ.get("HERMES_SESSION_PROFILE")
            if on_run is not None:
                on_run()
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "ok"},
                ],
                "final_response": "ok",
                "completed": True,
            }

    return SyncAgent


def test_sync_chat_named_profile_binds_and_restores(monkeypatch, tmp_path):
    sc = _session_context(monkeypatch)
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    observed: dict[str, object] = {}
    handler = _drive_sync_chat(
        monkeypatch, tmp_path, _sync_sentinel(observed), profile="work",
    )
    assert handler.status == 200
    assert observed["ctx_profile"] == "work", (
        "the non-streaming path must bind the session's profile into the turn "
        f"identity; observed {observed.get('ctx_profile')!r}"
    )
    assert observed["resolved_profile"] == "work"
    assert observed["env_profile"] == "work"
    _assert_identity_restored(sc, monkeypatch)


def test_sync_chat_named_profile_restores_on_failure(monkeypatch, tmp_path):
    sc = _session_context(monkeypatch)
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    observed: dict[str, object] = {}
    fired: list[str] = []

    def _boom():
        fired.append("boom")
        raise RuntimeError("sentinel sync failure")

    agent_cls = _sync_sentinel(observed, on_run=_boom)
    with pytest.raises(RuntimeError, match="sentinel sync failure"):
        _drive_sync_chat(monkeypatch, tmp_path, agent_cls, profile="work")
    assert fired == ["boom"], (
        "the failure trigger must actually fire before the raise propagates"
    )
    assert observed["ctx_profile"] == "work"
    _assert_identity_restored(sc, monkeypatch)


def test_sync_chat_path_wires_the_profile_binding_structurally():
    """Belt-and-suspenders: the sync route mirrors the streaming bind+restore.

    The behavioural tests above drive the real route; this pins the wiring so a
    future refactor cannot silently drop one side of the lifecycle.
    """
    import ast

    src = Path("api/routes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_handle_chat_sync"),
        None,
    )
    assert fn is not None, "_handle_chat_sync not found in api/routes.py"
    body = ast.get_source_segment(src, fn) or ""
    assert "_set_turn_session_identity(" in body, (
        "the non-streaming path must bind the per-turn session identity "
        "(#8452 maintainer requirement 1: every execution path)"
    )
    assert "profile_name=" in body, (
        "the non-streaming path must bind the RESOLVED session profile, "
        "not just the session key (#8467 rejection reason c)"
    )
    assert "_reset_turn_session_identity(" in body, (
        "the non-streaming path must restore the identity on every exit"
    )
    assert "_publish_turn_session_profile(" in body, (
        "the non-streaming path must keep the process-env fallback for the "
        "profile (#8467 rejection reason b) — now through the ownership-safe "
        "mirror, since a bare save/restore cannot own the shared slot under "
        "overlapping turns (review round-2 finding 1)"
    )
    assert "_release_turn_session_profile(" in body, (
        "the non-streaming path must release its profile publication on EVERY "
        "exit (success, failure) so no completed turn's profile survives"
    )


# ---------------------------------------------------------------------------
# Overlapping turns through the REAL entrypoints — the process-env mirror
# must never outlive its owners (nesquena-hermes review round 2, 2026-10-23)
# ---------------------------------------------------------------------------

def _wait_for_env(value, timeout=15.0):
    """Block until the process env carries *value* (a publisher landed)."""
    deadline = time.monotonic() + timeout
    while os.environ.get("HERMES_SESSION_PROFILE") != value:
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"HERMES_SESSION_PROFILE never became {value!r} "
                f"(still {os.environ.get('HERMES_SESSION_PROFILE')!r})"
            )
        time.sleep(0.005)


def _setup_overlap_sync_scene(monkeypatch, tmp_path):
    """Shared wiring for two concurrent REAL _handle_chat_sync invocations."""
    from types import SimpleNamespace

    import api.config as config
    import api.models as models
    import api.routes as routes

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "get_session", models.get_session)
    monkeypatch.setattr(routes, "title_from", models.title_from)
    monkeypatch.setattr(config, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value, **_kw: tmp_path)
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])

    class Handler:
        def __init__(self):
            self.headers = {}
            self.status = None
            self.wfile = SimpleNamespace(write=lambda _body: None)

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            return None

        def end_headers(self):
            return None

    return routes, Handler


def _install_overlap_agent(monkeypatch, observed_by_sid, hooks):
    """One agent class for both threads, dispatching per ``task_id``.

    A single class is REQUIRED, not a convenience: both invocations resolve
    their agent through ``from run_agent import AIAgent`` at call time, so
    per-thread classes would clobber each other's ``sys.modules`` entry.
    """
    import sys
    from types import SimpleNamespace

    class OverlapAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **kwargs):
            import gateway.session_context as sc_mod

            sid = kwargs.get("task_id")
            observed = observed_by_sid[sid]
            observed["ctx_profile"] = (
                "UNSET"
                if sc_mod._SESSION_PROFILE.get() is sc_mod._UNSET
                else sc_mod._SESSION_PROFILE.get()
            )
            observed["resolved_profile"] = sc_mod.get_session_env(
                "HERMES_SESSION_PROFILE", "<unset>"
            )
            observed["env_profile"] = os.environ.get("HERMES_SESSION_PROFILE")
            hook = hooks.get(sid)
            if hook is not None:
                hook()
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "ok"},
                ],
                "final_response": "ok",
                "completed": True,
            }

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=OverlapAgent))


@pytest.mark.parametrize("ambient", [None, "inherited-ambient"])
@pytest.mark.parametrize("holder_profile", ["work", "play"])
def test_sync_chat_overlapping_turns_never_leak_a_completed_turns_profile(
    monkeypatch, tmp_path, ambient, holder_profile
):
    """Review round-2 finding 1, through the REAL _handle_chat_sync entrypoint.

    Two concurrent turns on different profiles: the holder publishes its
    profile and starts its Agent under CHAT_LOCK; the waiter publishes its
    own profile and then blocks on the lock. Whatever the completion order,
    once BOTH turns have finished the process-env fallback must be back to
    the ambient value — a completed turn's profile must never survive there.
    The old save/restore pair failed exactly here: the waiter restored the
    holder's just-published value after the holder had already restored the
    ambient one.
    """
    sc = _session_context(monkeypatch)
    if ambient is None:
        monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    else:
        monkeypatch.setenv("HERMES_SESSION_PROFILE", ambient)

    waiter_profile = "play" if holder_profile == "work" else "work"
    # Unique session ids per parametrization: api.models.get_session serves a
    # process-wide LRU session cache keyed by sid, so reusing ids across
    # parametrized runs would hand this run the PREVIOUS run's cached session
    # (and its profile) — a harness artifact, not the behaviour under test.
    _tag = f"{holder_profile}-{'absent' if ambient is None else 'inherited'}"
    holder_sid, waiter_sid = f"ov-holder-{_tag}", f"ov-waiter-{_tag}"
    routes, Handler = _setup_overlap_sync_scene(monkeypatch, tmp_path)

    import api.models as models

    for sid, profile in ((holder_sid, holder_profile), (waiter_sid, waiter_profile)):
        session = models.Session(
            session_id=sid,
            workspace=str(tmp_path),
            messages=[],
            context_messages=[],
            model="test-model",
            model_provider="test-provider",
            profile=profile,
        )
        session.save(touch_updated_at=False)

    observed_by_sid = {holder_sid: {}, waiter_sid: {}}
    statuses: dict[str, int | None] = {}
    holder_in_agent = threading.Event()
    waiter_in_agent = threading.Event()
    holder_release = threading.Event()
    waiter_release = threading.Event()

    def _holder_hook():
        holder_in_agent.set()
        assert holder_release.wait(15), "holder turn was never released"

    def _waiter_hook():
        waiter_in_agent.set()
        assert waiter_release.wait(15), "waiter turn was never released"

    _install_overlap_agent(
        monkeypatch, observed_by_sid, {holder_sid: _holder_hook, waiter_sid: _waiter_hook}
    )

    def _run(sid):
        handler = Handler()
        try:
            routes._handle_chat_sync(
                handler,
                {"session_id": sid, "message": "overlap probe", "workspace": str(tmp_path)},
            )
        finally:
            statuses[sid] = handler.status

    holder_thread = threading.Thread(target=_run, args=(holder_sid,))
    waiter_thread = threading.Thread(target=_run, args=(waiter_sid,))
    holder_thread.start()
    assert holder_in_agent.wait(15), "holder turn never reached its Agent"
    # The holder published first, so the env names the holder's profile.
    assert observed_by_sid[holder_sid]["env_profile"] == holder_profile
    waiter_thread.start()
    # The waiter publishes its profile BEFORE blocking on CHAT_LOCK; wait for
    # that publication to be observable so the overlap is deterministic.
    _wait_for_env(waiter_profile)

    holder_release.set()
    holder_thread.join(timeout=30)
    assert not holder_thread.is_alive(), "holder turn did not finish"
    assert waiter_in_agent.wait(15), (
        "waiter turn never entered its Agent after the holder released CHAT_LOCK"
    )
    waiter_release.set()
    waiter_thread.join(timeout=30)
    assert not waiter_thread.is_alive(), "waiter turn did not finish"

    assert statuses[holder_sid] == 200 and statuses[waiter_sid] == 200, (
        f"both overlap turns must succeed; got {statuses!r}"
    )
    # THE invariant first: every turn finished => the fallback is the ambient
    # value again — absent stays absent, inherited stays inherited, and no
    # completed turn's profile survives.
    assert os.environ.get("HERMES_SESSION_PROFILE") == ambient, (
        f"process env kept {os.environ.get('HERMES_SESSION_PROFILE')!r} after "
        f"both turns finished; ambient was {ambient!r} — a completed turn's "
        "profile leaked into the process-env fallback"
    )
    # Mid-flight correctness: each turn resolved ITS OWN profile
    # (context-local), and while the waiter was the only active publisher the
    # mirror named the waiter — never the completed holder's profile.
    assert observed_by_sid[holder_sid]["ctx_profile"] == holder_profile
    assert observed_by_sid[waiter_sid]["ctx_profile"] == waiter_profile
    assert observed_by_sid[holder_sid]["env_profile"] == holder_profile
    assert observed_by_sid[waiter_sid]["env_profile"] == waiter_profile
    # Each turn bound and reset its identity in its own handler thread; the
    # main thread's context was never touched.
    assert sc._SESSION_PROFILE.get() is sc._UNSET


@pytest.mark.parametrize("ambient", [None, "inherited-ambient"])
@pytest.mark.parametrize("first_out", ["sync", "streaming"])
def test_mixed_sync_streaming_overlap_never_leaks_a_completed_turns_profile(
    monkeypatch, tmp_path, ambient, first_out
):
    """The completion order a CHAT_LOCK-only pair cannot reach.

    Two sync turns serialise on CHAT_LOCK, so the lock holder always finishes
    first — the late publisher can never exit before the first publisher. A
    streaming turn has no CHAT_LOCK, so pairing one of each gives genuine
    concurrency AND a controllable completion order: the streaming turn is
    the late publisher (it publishes on top of the sync turn's profile), and
    releasing it FIRST exercises the late-publisher-exits-first order. The
    invariant is unchanged — once BOTH turns finish, the process-env fallback
    is the ambient value again.
    """
    from types import SimpleNamespace

    import sys

    sc = _session_context(monkeypatch)
    if ambient is None:
        monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    else:
        monkeypatch.setenv("HERMES_SESSION_PROFILE", ambient)

    routes, Handler = _setup_overlap_sync_scene(monkeypatch, tmp_path)

    import api.models as models

    sync_sid = f"ov-mix-sync-{first_out}-{'absent' if ambient is None else 'inherited'}"
    session = models.Session(
        session_id=sync_sid,
        workspace=str(tmp_path),
        messages=[],
        context_messages=[],
        model="test-model",
        model_provider="test-provider",
        profile="work",
    )
    session.save(touch_updated_at=False)

    sync_observed: dict[str, object] = {}
    sync_status: dict[str, int | None] = {}
    sync_in_agent = threading.Event()
    sync_release = threading.Event()

    class MixSyncAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **kwargs):
            import gateway.session_context as sc_mod

            sync_observed["ctx_profile"] = (
                "UNSET"
                if sc_mod._SESSION_PROFILE.get() is sc_mod._UNSET
                else sc_mod._SESSION_PROFILE.get()
            )
            sync_observed["env_profile"] = os.environ.get("HERMES_SESSION_PROFILE")
            sync_in_agent.set()
            assert sync_release.wait(15), "sync turn was never released"
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "ok"},
                ],
                "final_response": "ok",
                "completed": True,
            }

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=MixSyncAgent))

    # The streaming turn: the real worker with the sentinel-Agent scene.
    scene = _StreamingScene(monkeypatch, tmp_path, profile="play")
    # Capture the queue object NOW: the worker pops its stream registry in
    # the outer finally, so STREAMS[stream_id] is gone once the run ends.
    scene_events_q = scene.streaming.STREAMS[scene.stream_id]
    stream_in_agent = threading.Event()
    stream_release = threading.Event()

    def _stream_hook():
        stream_in_agent.set()
        assert stream_release.wait(15), "streaming turn was never released"

    scene.on_run = _stream_hook

    def _run_sync():
        handler = Handler()
        try:
            routes._handle_chat_sync(
                handler,
                {"session_id": sync_sid, "message": "mix overlap", "workspace": str(tmp_path)},
            )
        finally:
            sync_status["value"] = handler.status

    sync_thread = threading.Thread(target=_run_sync)
    stream_thread = threading.Thread(target=scene.run)
    sync_thread.start()
    assert sync_in_agent.wait(15), "sync turn never reached its Agent"
    # The sync turn published first, so the env names ITS profile.
    assert sync_observed["env_profile"] == "work"
    stream_thread.start()
    # The streaming turn publishes on top of the sync turn's profile.
    _wait_for_env("play")
    assert stream_in_agent.wait(15), "streaming turn never reached its Agent"
    assert scene.observed["env_profile"] == "play", (
        "the late publisher's own turn must observe its own profile"
    )

    if first_out == "streaming":
        stream_release.set()
        stream_thread.join(timeout=60)
        assert not stream_thread.is_alive(), "streaming turn did not finish"
        sync_release.set()
        sync_thread.join(timeout=30)
        assert not sync_thread.is_alive(), "sync turn did not finish"
    else:
        sync_release.set()
        sync_thread.join(timeout=30)
        assert not sync_thread.is_alive(), "sync turn did not finish"
        stream_release.set()
        stream_thread.join(timeout=60)
        assert not stream_thread.is_alive(), "streaming turn did not finish"

    assert sync_status["value"] == 200, "the sync overlap turn must succeed"
    assert "apperror" not in _drain_stream_events(scene_events_q), (
        "the streaming overlap turn must not error"
    )
    # THE invariant, in this completion order too: every turn finished => the
    # fallback is the ambient value again.
    assert os.environ.get("HERMES_SESSION_PROFILE") == ambient, (
        f"process env kept {os.environ.get('HERMES_SESSION_PROFILE')!r} after "
        f"both turns finished; ambient was {ambient!r} — a completed turn's "
        "profile leaked into the process-env fallback"
    )
    # Each turn resolved ITS OWN profile through the context-local binding.
    assert sync_observed["ctx_profile"] == "work"
    assert scene.observed["ctx_profile"] == "play"
    assert sc._SESSION_PROFILE.get() is sc._UNSET


# ---------------------------------------------------------------------------
# The mirror helpers themselves — ownership semantics the entrypoints above
# cannot reach (nesting, profile-less publishers, unknown tokens)
# ---------------------------------------------------------------------------

def test_profile_mirror_restores_ambient_after_overlapping_publishers(monkeypatch):
    """The ownership core: overlapping publishers, both completion orders.

    The process env is a single slot overlapping turns all publish into, so
    the mirror tracks a publisher stack: the ambient value is captured when
    the FIRST publisher arrives, every exit re-publishes the most recent
    REMAINING publisher, and only the last exit restores the ambient value.
    """
    streaming = _streaming()
    key = "HERMES_SESSION_PROFILE"

    for ambient in (None, "inherited-ambient"):
        for first_out in ("a", "b"):
            monkeypatch.delenv(key, raising=False)
            if ambient is not None:
                os.environ[key] = ambient
            token_a = streaming._publish_turn_session_profile("work")
            token_b = streaming._publish_turn_session_profile("play")
            # Most recent publisher owns the slot.
            assert os.environ[key] == "play"
            if first_out == "a":
                streaming._release_turn_session_profile(token_a)
                # A completed turn's value must NOT be what remains.
                assert os.environ[key] == "play"
                streaming._release_turn_session_profile(token_b)
            else:
                streaming._release_turn_session_profile(token_b)
                assert os.environ[key] == "work"
                streaming._release_turn_session_profile(token_a)
            assert os.environ.get(key) == ambient, (
                f"ambient={ambient!r} first_out={first_out!r}: the mirror must "
                f"restore the ambient value once every publisher has left, "
                f"got {os.environ.get(key)!r}"
            )


def test_profile_mirror_nested_publishers_restore_the_outer_value(monkeypatch):
    """Same-thread nesting: the inner turn's exit re-publishes the outer's."""
    streaming = _streaming()
    key = "HERMES_SESSION_PROFILE"
    monkeypatch.delenv(key, raising=False)

    outer = streaming._publish_turn_session_profile("outer-profile")
    assert os.environ[key] == "outer-profile"
    inner = streaming._publish_turn_session_profile("inner-profile")
    assert os.environ[key] == "inner-profile"
    streaming._release_turn_session_profile(inner)
    assert os.environ[key] == "outer-profile", (
        "a nested turn's exit must restore the enclosing publisher's value"
    )
    streaming._release_turn_session_profile(outer)
    assert os.environ.get(key) is None


def test_profile_mirror_profile_less_publisher_never_masks_ambient(monkeypatch):
    """A turn with no resolved profile must not mask an inherited value — and
    must not restore a concurrent publisher's value either."""
    streaming = _streaming()
    key = "HERMES_SESSION_PROFILE"
    monkeypatch.setenv(key, "inherited-ambient")

    named = streaming._publish_turn_session_profile("work")
    anonymous = streaming._publish_turn_session_profile(None)
    assert os.environ[key] == "work"
    streaming._release_turn_session_profile(named)
    # The anonymous publisher is now the most recent owner: the slot falls
    # back to ambient rather than keeping the finished turn's profile.
    assert os.environ[key] == "inherited-ambient"
    streaming._release_turn_session_profile(anonymous)
    assert os.environ[key] == "inherited-ambient"


def test_profile_mirror_unknown_token_is_a_no_op(monkeypatch):
    """Releasing a token that was never published must not touch the env."""
    streaming = _streaming()
    key = "HERMES_SESSION_PROFILE"
    monkeypatch.setenv(key, "inherited-ambient")
    streaming._release_turn_session_profile(None)
    streaming._release_turn_session_profile(object())
    assert os.environ[key] == "inherited-ambient"


# ---------------------------------------------------------------------------
# Real .env loader → worker → cleanup: a profile .env's HERMES_SESSION_PROFILE
# is TURN-LOCAL. It must never become the mirror's ambient, and it must never
# outlive the turn (nesquena-hermes review round 3, 2026-10-23).
# ---------------------------------------------------------------------------
#
# The controls above stub ``get_profile_runtime_env`` to ``{}`` (the scene
# default), so none of them exercises the REAL loader — exactly the gap the
# re-gate found. In the streaming worker the generic profile-env apply ran
# BEFORE the mirror publish, so the mirror captured the .env's own value as
# its ambient; generic teardown then restored the real value, and the final
# mirror release wrote the contaminated snapshot back over it. The fix gives
# the mirror exclusive ownership of the key across setup and teardown: the
# generic profile-env path neither applies nor snapshots/restores it, so the
# ambient the mirror captures is the true pre-turn value. These controls
# drive the real loader through the real worker and assert the post-turn
# process env.


@pytest.mark.parametrize("ambient", [None, "inherited-ambient"])
@pytest.mark.parametrize("profile", ["work", None])
def test_streaming_real_dotenv_profile_never_outlives_the_turn(
    monkeypatch, tmp_path, ambient, profile
):
    """A profile .env's HERMES_SESSION_PROFILE must not survive the turn.

    Matrix (the re-gate probe): ambient absent/inherited × named/unresolved
    profile, with HERMES_SESSION_PROFILE=dotenv-profile in the profile home's
    real .env, loaded by the REAL api.profiles.get_profile_runtime_env.
    """
    sc = _session_context(monkeypatch)
    if ambient is None:
        monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    else:
        monkeypatch.setenv("HERMES_SESSION_PROFILE", ambient)

    tag = f"{'absent' if ambient is None else 'inherited'}-{profile or 'unresolved'}"
    scene = _StreamingScene(
        monkeypatch, tmp_path,
        profile=profile,
        active_profile_name=None,
        tag=tag,
        real_profile_env=True,
    )
    # The REAL loader reads this file — nothing on this path is stubbed.
    (scene.profile_home / ".env").write_text(
        "HERMES_SESSION_PROFILE=dotenv-profile\n", encoding="utf-8"
    )

    scene.run()

    # THE invariant: once the turn finishes the process var is exactly the
    # pre-turn ambient — absent stays absent, an inherited value is never
    # replaced by the turn-local dotenv value.
    assert os.environ.get("HERMES_SESSION_PROFILE") == ambient, (
        f"process env kept {os.environ.get('HERMES_SESSION_PROFILE')!r} after "
        f"the turn; ambient was {ambient!r} — the turn-local dotenv value "
        "leaked into the process-env fallback"
    )
    # Mid-turn, the sentinel Agent observed the env the mirror owns.
    if profile:
        assert scene.observed["env_profile"] == "work", (
            "a named turn must publish its resolved profile, not the .env value"
        )
        assert scene.observed["thread_env_profile"] == "work"
        assert scene.observed["ctx_profile"] == "work"
    else:
        # Unresolved: the mirror publishes nothing and the generic path no
        # longer applies the .env value, so the ambient stays visible — the
        # intended unresolved-profile fallback (CLI/cron consumers inherit).
        assert scene.observed["ctx_profile"] == "UNSET"
        assert scene.observed["env_profile"] == ambient, (
            "an unresolved turn must leave the ambient visible, not the .env "
            "value the generic path used to apply"
        )
        # ...while the raw loader still feeds the THREAD env, so in-process
        # consumers see the profile .env's declaration.
        assert scene.observed["thread_env_profile"] == "dotenv-profile"
    assert sc._SESSION_PROFILE.get() is sc._UNSET


@pytest.mark.parametrize("ambient", [None, "inherited-ambient"])
@pytest.mark.parametrize("first_out", ["streaming", "sync"])
@pytest.mark.parametrize("stream_profile", ["play", None])
def test_mixed_overlap_with_real_dotenv_restores_the_ambient(
    monkeypatch, tmp_path, ambient, first_out, stream_profile
):
    """Round-3 overlap control: real loader, real worker, real sync handler.

    The streaming turn publishes FIRST — so IT is the publisher that captures
    the ambient, which is exactly where the contaminated capture lived. The
    sync turn publishes on top of it, and either turn may exit first. A real
    .env HERMES_SESSION_PROFILE sits in the streaming turn's profile home.
    Whatever the exit order, once BOTH turns finish the process var must be
    the pre-turn ambient: the dotenv value must never become ambient, and the
    generic teardown must not overwrite the surviving publisher either.
    """
    from types import SimpleNamespace

    import sys

    sc = _session_context(monkeypatch)
    if ambient is None:
        monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    else:
        monkeypatch.setenv("HERMES_SESSION_PROFILE", ambient)

    routes, Handler = _setup_overlap_sync_scene(monkeypatch, tmp_path)

    import api.models as models

    tag = (
        f"{'absent' if ambient is None else 'inherited'}"
        f"-{first_out}-{stream_profile or 'unresolved'}"
    )
    sync_sid = f"ov-mix3-sync-{tag}"
    session = models.Session(
        session_id=sync_sid,
        workspace=str(tmp_path),
        messages=[],
        context_messages=[],
        model="test-model",
        model_provider="test-provider",
        profile="work",
    )
    session.save(touch_updated_at=False)

    sync_observed: dict[str, object] = {}
    sync_status: dict[str, int | None] = {}
    sync_in_agent = threading.Event()
    sync_release = threading.Event()

    class MixSyncAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **kwargs):
            import gateway.session_context as sc_mod

            sync_observed["ctx_profile"] = (
                "UNSET"
                if sc_mod._SESSION_PROFILE.get() is sc_mod._UNSET
                else sc_mod._SESSION_PROFILE.get()
            )
            sync_observed["env_profile"] = os.environ.get("HERMES_SESSION_PROFILE")
            sync_in_agent.set()
            assert sync_release.wait(15), "sync turn was never released"
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "ok"},
                ],
                "final_response": "ok",
                "completed": True,
            }

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=MixSyncAgent))

    # The streaming turn: the real worker with the real .env loader.
    scene = _StreamingScene(
        monkeypatch, tmp_path,
        profile=stream_profile,
        active_profile_name=None,
        tag=tag,
        real_profile_env=True,
    )
    (scene.profile_home / ".env").write_text(
        "HERMES_SESSION_PROFILE=dotenv-profile\n", encoding="utf-8"
    )
    # Capture the queue object NOW: the worker pops its stream registry in
    # the outer finally, so STREAMS[stream_id] is gone once the run ends.
    scene_events_q = scene.streaming.STREAMS[scene.stream_id]
    stream_in_agent = threading.Event()
    stream_release = threading.Event()

    def _stream_hook():
        stream_in_agent.set()
        assert stream_release.wait(15), "streaming turn was never released"

    scene.on_run = _stream_hook

    def _run_sync():
        handler = Handler()
        try:
            routes._handle_chat_sync(
                handler,
                {
                    "session_id": sync_sid,
                    "message": "mix overlap round3",
                    "workspace": str(tmp_path),
                },
            )
        finally:
            sync_status["value"] = handler.status

    stream_thread = threading.Thread(target=scene.run)
    stream_thread.start()
    # The streaming turn publishes BEFORE its agent runs, so reaching the
    # agent proves its mirror publication — and its ambient capture — has
    # already happened: this turn is the FIRST publisher.
    assert stream_in_agent.wait(15), "streaming turn never reached its Agent"
    if stream_profile:
        assert scene.observed["env_profile"] == stream_profile
    else:
        assert scene.observed["env_profile"] == ambient

    sync_thread = threading.Thread(target=_run_sync)
    sync_thread.start()
    assert sync_in_agent.wait(15), "sync turn never reached its Agent"
    # The sync turn published on top of the streaming turn's slot.
    assert sync_observed["env_profile"] == "work"

    if first_out == "streaming":
        stream_release.set()
        stream_thread.join(timeout=60)
        assert not stream_thread.is_alive(), "streaming turn did not finish"
        # The sync turn is still running: the slot must name the SURVIVING
        # publisher, never the streaming home's dotenv value.
        assert os.environ.get("HERMES_SESSION_PROFILE") == "work", (
            "after the first publisher exits, the slot must name the "
            "surviving turn's profile — not a dotenv value or ambient"
        )
        sync_release.set()
        sync_thread.join(timeout=30)
        assert not sync_thread.is_alive(), "sync turn did not finish"
    else:
        sync_release.set()
        sync_thread.join(timeout=30)
        assert not sync_thread.is_alive(), "sync turn did not finish"
        # The streaming turn is still running. A named publisher keeps its
        # own value; a profile-less one leaves the ambient visible.
        expected = stream_profile if stream_profile else ambient
        assert os.environ.get("HERMES_SESSION_PROFILE") == expected, (
            "after the sync turn exits, the slot must name the surviving "
            f"publisher's value ({expected!r}), not a stale or dotenv value"
        )
        stream_release.set()
        stream_thread.join(timeout=60)
        assert not stream_thread.is_alive(), "streaming turn did not finish"

    assert sync_status["value"] == 200, "the sync overlap turn must succeed"
    assert "apperror" not in _drain_stream_events(scene_events_q), (
        "the streaming overlap turn must not error"
    )
    # THE invariant, in every exit order: both turns finished => the process
    # var is the pre-turn ambient again.
    assert os.environ.get("HERMES_SESSION_PROFILE") == ambient, (
        f"process env kept {os.environ.get('HERMES_SESSION_PROFILE')!r} after "
        f"both turns finished; ambient was {ambient!r} — the .env value leaked "
        "into the process-env fallback"
    )
    assert sync_observed["ctx_profile"] == "work"
    assert sc._SESSION_PROFILE.get() is sc._UNSET
