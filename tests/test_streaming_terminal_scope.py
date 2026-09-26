"""Regression tests for per-turn terminal-scope binding in streaming turns.

Context: `_run_agent_streaming` applies each turn's profile terminal settings
via the process-global os.environ for the turn's duration (with `_ENV_LOCK`
held only around the write, not the agent run). Two concurrent turns on
different profiles therefore interleave: while turn B's export is live, a
scope-less reader in turn A resolves B's TERMINAL_* from the shared environ —
so a turn whose profile pins a local backend can execute terminal/file tools
on a sibling profile's docker/ssh backend.

The fix binds each turn's complete profile terminal policy via
``tools.terminal_scope.install_profile_terminal_scope`` (context-local,
propagates into the tool-executor pool threads), so scope-aware readers
resolve the turn's OWN policy regardless of sibling environ writes.

These tests exercise the failure shape against the REAL production helpers
(no mocking of the readers): while a profile-A scope is installed, a sibling
"poisons" os.environ with profile B's TERMINAL_* / HERMES_HOME; the
scope-aware terminal_env() and get_hermes_home() must still resolve A.

Degrades to skip on agents lacking the scope machinery.
"""
import os
import textwrap
import threading
from pathlib import Path

import pytest

terminal_scope = pytest.importorskip("tools.terminal_scope")
hermes_constants = pytest.importorskip("hermes_constants")

HAS_SCOPE = hasattr(terminal_scope, "install_profile_terminal_scope") and hasattr(
    terminal_scope, "terminal_env"
)
HAS_HOME_OVERRIDE = hasattr(hermes_constants, "set_hermes_home_override")


def _seed_profile_home(base: Path, name: str, backend: str) -> Path:
    """Profile home with a terminal backend pin in config.yaml."""
    home = base / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        textwrap.dedent(
            f"""\
            model:
              default: test-model
            terminal:
              backend: {backend}
            """
        ),
        encoding="utf-8",
    )
    return home


@pytest.mark.skipif(
    not HAS_SCOPE,
    reason="hermes-agent lacks terminal scope machinery; WebUI degrades to os.environ",
)
def test_terminal_env_resolves_own_profile_despite_environ_poison(tmp_path, monkeypatch):
    """Inside profile A's terminal scope (local backend), a poison drop of
    TERMINAL_ENV=docker into os.environ must NOT change what terminal_env()
    resolves — the concurrent-turn failure shape."""
    home_a = _seed_profile_home(tmp_path, "alpha", backend="local")
    _seed_profile_home(tmp_path, "beta", backend="docker")

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.delenv("TERMINAL_DOCKER_IMAGE", raising=False)

    token = terminal_scope.install_profile_terminal_scope(home_a)
    try:
        assert terminal_scope.terminal_env("TERMINAL_ENV", "local") == "local"
        # The poison: a sibling turn exports its profile's runtime env into
        # the process global for ITS duration.
        os.environ["TERMINAL_ENV"] = "docker"
        os.environ["TERMINAL_DOCKER_IMAGE"] = "hermes-sandbox:latest"
        try:
            assert terminal_scope.terminal_env("TERMINAL_ENV", "local") == "local", (
                "terminal_env() resolved the poisoned os.environ TERMINAL_ENV=docker "
                "instead of the bound profile-A (local) scope"
            )
            # A scope-bound var must come from the scope's own policy — the
            # poisoned value must never win.
            assert (
                terminal_scope.terminal_env("TERMINAL_DOCKER_IMAGE", "")
                != "hermes-sandbox:latest"
            ), "docker image resolved the poisoned os.environ value"
        finally:
            monkeypatch.delenv("TERMINAL_ENV", raising=False)
            monkeypatch.delenv("TERMINAL_DOCKER_IMAGE", raising=False)
    finally:
        terminal_scope.reset_terminal_scope(token)


@pytest.mark.skipif(
    not (HAS_SCOPE and HAS_HOME_OVERRIDE),
    reason="requires both the terminal scope and the home override",
)
def test_hermes_home_resolves_own_profile_despite_environ_poison(tmp_path, monkeypatch):
    """With A's home override + terminal scope installed and os.environ
    poisoned to B, get_hermes_home() must resolve A (the wrong-profile
    resolution symptom of the same race)."""
    home_a = _seed_profile_home(tmp_path, "alpha", backend="local")
    home_b = _seed_profile_home(tmp_path, "beta", backend="docker")

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    home_token = hermes_constants.set_hermes_home_override(str(home_a))
    scope_token = terminal_scope.install_profile_terminal_scope(home_a)
    try:
        os.environ["HERMES_HOME"] = str(home_b)  # sibling poison
        assert str(hermes_constants.get_hermes_home()) == str(home_a), (
            "get_hermes_home() resolved the poisoned os.environ HERMES_HOME "
            f"({home_b}) instead of the context-local override ({home_a})"
        )
    finally:
        os.environ["HERMES_HOME"] = str(home_a)
        terminal_scope.reset_terminal_scope(scope_token)
        hermes_constants.reset_hermes_home_override(home_token)


@pytest.mark.skipif(not HAS_SCOPE, reason="requires scope machinery")
def test_docker_pinned_profile_scope_still_resolves_docker(tmp_path, monkeypatch):
    """Symmetric correctness: a docker-pinned profile's bound scope resolves
    docker even while os.environ holds a sibling local export."""
    _seed_profile_home(tmp_path, "alpha", backend="local")  # sibling A (local)
    home_b = _seed_profile_home(tmp_path, "beta", backend="docker")

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    token = terminal_scope.install_profile_terminal_scope(home_b)
    try:
        os.environ["TERMINAL_ENV"] = "local"  # sibling A's export
        try:
            assert terminal_scope.terminal_env("TERMINAL_ENV", "local") == "docker", (
                "docker-pinned profile must resolve docker from its bound scope, "
                "not the sibling's local export in os.environ"
            )
        finally:
            monkeypatch.delenv("TERMINAL_ENV", raising=False)
    finally:
        terminal_scope.reset_terminal_scope(token)


@pytest.mark.skipif(not HAS_SCOPE, reason="requires scope machinery")
def test_concurrent_turns_keep_their_own_policies(tmp_path, monkeypatch):
    """Barrier-controlled concurrency: thread A holds profile A's scope while
    thread B flips the shared environ to profile B's export; A's reads must
    stay on A. Mirrors two interleaved streaming turns."""
    home_a = _seed_profile_home(tmp_path, "alpha", backend="local")
    _seed_profile_home(tmp_path, "beta", backend="docker")  # sibling B (docker)

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    results = {}
    barrier = threading.Barrier(2)

    def turn_a():
        token = terminal_scope.install_profile_terminal_scope(home_a)
        try:
            barrier.wait()  # both turns "start" together
            barrier.wait()  # B's export is now live
            results["a_backend"] = terminal_scope.terminal_env("TERMINAL_ENV", "local")
            barrier.wait()  # A has recorded its result; B may now clean up
        finally:
            terminal_scope.reset_terminal_scope(token)

    def turn_b():
        # B's turn-start env export (runs outside the env lock, like the
        # streaming path: agent runs unlocked). The export STAYS in the
        # environ until A has recorded its read — removing it earlier would
        # let the test pass on a cleaned environ even with no scope bound.
        barrier.wait()
        os.environ["TERMINAL_ENV"] = "docker"  # sibling B's export (docker)
        try:
            barrier.wait()
            barrier.wait()
        finally:
            os.environ.pop("TERMINAL_ENV", None)

    t_a = threading.Thread(target=turn_a)
    t_b = threading.Thread(target=turn_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert results.get("a_backend") == "local", (
        "concurrent local-profile turn resolved the docker-profile sibling's "
        f"environ export: {results.get('a_backend')!r}"
    )


@pytest.mark.skipif(not HAS_SCOPE, reason="requires scope machinery")
def test_streaming_helpers_bind_and_reset_the_scope(tmp_path, monkeypatch):
    """The streaming binding path itself: _set_streaming_terminal_scope
    installs the profile's policy and _reset_streaming_terminal_scope
    restores the prior state (the reset semantics the streaming finally
    relies on). Guards against the helpers silently becoming no-ops."""
    from api.streaming import (
        _set_streaming_terminal_scope,
        _reset_streaming_terminal_scope,
    )

    home_a = _seed_profile_home(tmp_path, "alpha", backend="docker")
    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    scope_mod, token, installed = _set_streaming_terminal_scope(str(home_a))
    try:
        assert installed and token is not None, "helper must install the scope"
        assert terminal_scope.terminal_env("TERMINAL_ENV", "local") == "docker", (
            "bound scope must resolve the profile's own backend (docker)"
        )
    finally:
        _reset_streaming_terminal_scope(scope_mod, token, installed)

    # After reset the scope is gone: reads fall back to the environ.
    assert terminal_scope.get_terminal_scope() is None

    # Degenerate inputs stay no-ops, never raise.
    assert _set_streaming_terminal_scope("") == (None, None, False)
    _reset_streaming_terminal_scope(None, None, False)
