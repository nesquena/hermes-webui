from concurrent.futures import ThreadPoolExecutor
import contextvars
import os
from pathlib import Path
import re
import sys
import threading
import pytest

# Ensure sibling hermes-agent checkout or ~/.hermes/hermes-agent is on sys.path
for _candidate in [
    Path(__file__).resolve().parent.parent.parent / "hermes-agent",
    Path(__file__).resolve().parent.parent / "hermes-agent",
    Path.home() / ".hermes" / "hermes-agent",
]:
    if (_candidate / "gateway" / "session_context.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from api.streaming import (
    _bind_turn_session_identity,
    _build_agent_thread_env,
    _set_turn_session_identity,
    _reset_turn_session_identity,
)


def test_build_agent_thread_env_sets_session_profile():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    match = re.search(
        r"(def _build_agent_thread_env\(.*?\n)(?=\ndef |\nclass )",
        src,
        re.DOTALL,
    )
    assert match, "_build_agent_thread_env not found in api/streaming.py"
    ns: dict = {}
    exec(compile(match.group(1), "<streaming_extract>", "exec"), ns)

    fn = ns["_build_agent_thread_env"]

    # When profile_name is passed
    env_with_profile = fn({}, "/workspace", "sess-123", "/path/home", profile_name="coding")
    assert env_with_profile.get("HERMES_SESSION_PROFILE") == "coding"
    assert env_with_profile.get("HERMES_SESSION_KEY") == "sess-123"
    assert env_with_profile.get("HERMES_HOME") == "/path/home"

    # When profile_name is empty or omitted
    env_no_profile = fn({}, "/workspace", "sess-123", "/path/home")
    assert "HERMES_SESSION_PROFILE" not in env_no_profile

    env_empty_profile = fn({}, "/workspace", "sess-123", "/path/home", profile_name="")
    assert "HERMES_SESSION_PROFILE" not in env_empty_profile


def test_streaming_wires_session_profile_in_thread_and_process_env():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    assert "profile_name=_resolved_profile_name or \"\"" in src
    assert "os.environ['HERMES_SESSION_PROFILE'] = str(_resolved_profile_name)" in src
    assert "if old_session_profile is None: os.environ.pop('HERMES_SESSION_PROFILE', None)" in src
    assert 'tokens["session_profile"] = _SPROF.set(str(prof))' in src
    assert '_SPROF.reset(tok)' in src


def test_interleaved_turn_session_profiles_isolate_agent_container_key(monkeypatch):
    """Two concurrent turns carrying different profiles retain their own Agent profile
    and Docker container key, even after one writes the process-global env fallback."""
    pytest.importorskip("gateway.session_context")
    pytest.importorskip("tools.terminal_tool")
    from gateway.session_context import get_session_env
    from tools import terminal_tool

    # Enable profile-scoped Docker containers for this test
    monkeypatch.setattr(
        terminal_tool,
        "_session_scope",
        lambda: type("Scope", (), {"session_isolated": False, "docker_profile_scoped": True})(),
    )

    prev_env = os.environ.get("HERMES_SESSION_PROFILE")

    captured_a: dict[str, str] = {}
    captured_b: dict[str, str] = {}
    barrier = threading.Barrier(2)
    b_wrote_env = threading.Event()

    def turn_a():
        with _bind_turn_session_identity("sess-A", profile="profile-A"):
            # Turn A sets initial process fallback
            os.environ["HERMES_SESSION_PROFILE"] = "profile-A"
            barrier.wait()
            # Wait until Turn B overwrites the process-global env
            b_wrote_env.wait(timeout=5)

            # In Turn A's context, ContextVar must win over dirty process env
            captured_a["env_profile"] = os.environ.get("HERMES_SESSION_PROFILE", "")
            captured_a["session_profile"] = terminal_tool._current_session_profile()
            captured_a["container_key"] = terminal_tool._resolve_container_task_id(None)

    def turn_b():
        barrier.wait()
        with _bind_turn_session_identity("sess-B", profile="profile-B"):
            # Turn B overwrites the shared process-global env slot
            os.environ["HERMES_SESSION_PROFILE"] = "profile-B"
            b_wrote_env.set()

            captured_b["env_profile"] = os.environ.get("HERMES_SESSION_PROFILE", "")
            captured_b["session_profile"] = terminal_tool._current_session_profile()
            captured_b["container_key"] = terminal_tool._resolve_container_task_id(None)

    try:
        t_a = threading.Thread(target=turn_a)
        t_b = threading.Thread(target=turn_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

        assert not t_a.is_alive()
        assert not t_b.is_alive()

        # Both turns ran with process env set to profile-B at the end
        assert captured_a["env_profile"] == "profile-B"
        assert captured_b["env_profile"] == "profile-B"

        # But Turn A retained profile-A ContextVar and profile-A container
        assert captured_a["session_profile"] == "profile-A"
        assert captured_a["container_key"] == "profile:profile-A"

        # And Turn B retained profile-B ContextVar and profile-B container
        assert captured_b["session_profile"] == "profile-B"
        assert captured_b["container_key"] == "profile:profile-B"
    finally:
        if prev_env is None:
            os.environ.pop("HERMES_SESSION_PROFILE", None)
        else:
            os.environ["HERMES_SESSION_PROFILE"] = prev_env


def test_default_profile_binding_and_env_fallback(monkeypatch):
    """Default profile explicitly binds '' in ContextVar so a dirty process env does
    not contaminate default-profile Docker container resolution."""
    pytest.importorskip("gateway.session_context")
    pytest.importorskip("tools.terminal_tool")
    from tools import terminal_tool

    monkeypatch.setattr(
        terminal_tool,
        "_session_scope",
        lambda: type("Scope", (), {"session_isolated": False, "docker_profile_scoped": True})(),
    )
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "dirty-foreign-profile")

    # When no turn is active, get_session_env falls back to os.environ
    assert terminal_tool._current_session_profile() == "dirty-foreign-profile"

    # When default profile turn binds ''
    with _bind_turn_session_identity("sess-default", profile=""):
        # ContextVar is bound to '', so it masks the dirty process env
        assert terminal_tool._current_session_profile() == ""
        # And persistent Docker maps to 'default' container
        assert terminal_tool._resolve_container_task_id(None) == "default"


def test_nested_prior_profile_context_restore(monkeypatch):
    """Nested turns restore the outer turn's profile upon exit via reset tokens."""
    pytest.importorskip("gateway.session_context")
    pytest.importorskip("tools.terminal_tool")
    from tools import terminal_tool

    with _bind_turn_session_identity("sess-outer", profile="outer-prof"):
        assert terminal_tool._current_session_profile() == "outer-prof"

        with _bind_turn_session_identity("sess-inner", profile="inner-prof"):
            assert terminal_tool._current_session_profile() == "inner-prof"

        # Restores outer profile on inner exit
        assert terminal_tool._current_session_profile() == "outer-prof"


def test_exception_cleanup_restores_profile(monkeypatch):
    """Exceptions during a turn do not leak profile ContextVar to subsequent work."""
    pytest.importorskip("gateway.session_context")
    pytest.importorskip("tools.terminal_tool")
    from tools import terminal_tool

    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    initial_profile = terminal_tool._current_session_profile()

    with pytest.raises(RuntimeError):
        with _bind_turn_session_identity("sess-err", profile="err-profile"):
            assert terminal_tool._current_session_profile() == "err-profile"
            raise RuntimeError("simulated streaming failure")

    # Post-exception cleanup must restore initial state
    assert terminal_tool._current_session_profile() == initial_profile


def test_propagated_tool_worker_context(monkeypatch):
    """Tool workers spawned in copied contexts inherit the turn's bound profile."""
    pytest.importorskip("gateway.session_context")
    pytest.importorskip("tools.terminal_tool")
    from tools import terminal_tool

    monkeypatch.setattr(
        terminal_tool,
        "_session_scope",
        lambda: type("Scope", (), {"session_isolated": False, "docker_profile_scoped": True})(),
    )
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "env-clobbered")

    with _bind_turn_session_identity("sess-worker", profile="worker-profile"):
        ctx = contextvars.copy_context()

        def worker_fn():
            return (
                terminal_tool._current_session_profile(),
                terminal_tool._resolve_container_task_id(None),
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ctx.run, worker_fn)
            prof, key = future.result(timeout=5)

        assert prof == "worker-profile"
        assert key == "profile:worker-profile"
