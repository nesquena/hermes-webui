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
    errors = {}
    barrier = threading.Barrier(2, timeout=10)

    def turn_a():
        token = terminal_scope.install_profile_terminal_scope(home_a)
        try:
            barrier.wait()  # both turns "start" together
            barrier.wait()  # B's export is now live
            results["a_backend"] = terminal_scope.terminal_env("TERMINAL_ENV", "local")
            barrier.wait()  # A has recorded its result; B may now clean up
        except Exception as exc:  # a failed turn must never wedge its sibling
            errors["a"] = repr(exc)
        finally:
            terminal_scope.reset_terminal_scope(token)

    def turn_b():
        # B's turn-start env export (runs outside the env lock, like the
        # streaming path: agent runs unlocked). The export STAYS in the
        # environ until A has recorded its read — removing it earlier would
        # let the test pass on a cleaned environ even with no scope bound.
        try:
            barrier.wait()
            os.environ["TERMINAL_ENV"] = "docker"  # sibling B's export (docker)
            barrier.wait()
            barrier.wait()
        except Exception as exc:
            errors["b"] = repr(exc)
        finally:
            os.environ.pop("TERMINAL_ENV", None)

    t_a = threading.Thread(target=turn_a)
    t_b = threading.Thread(target=turn_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert not errors, f"turn thread(s) failed: {errors}"
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


@pytest.mark.skipif(not HAS_SCOPE, reason="requires scope machinery")
def test_launch_env_only_policy_survives_scope_binding(tmp_path, monkeypatch):
    """Review of #7861, case 1: a deployment launched with an env-only
    terminal backend (TERMINAL_ENV=ssh from systemd / op run / a launcher
    bridge — no config.yaml / .env entry) must KEEP that policy once the
    turn binds a scope, while a routed secondary profile with no files of
    its own must resolve its own default instead of borrowing the launch
    env. The overlay is the FROZEN launch snapshot — a later live environ
    write must not leak into the owning turn (never read os.environ at
    turn entry)."""
    import api.profiles as api_profiles
    import api.streaming as streaming

    launch_home = tmp_path / "launch"
    launch_home.mkdir()
    (launch_home / "config.yaml").write_text(
        "model:\n  default: test-model\n", encoding="utf-8"
    )  # NO terminal: section — the policy exists only in the launch env
    secondary_home = tmp_path / "secondary"
    secondary_home.mkdir()  # empty profile: no config.yaml at all

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.delenv("TERMINAL_SSH_HOST", raising=False)

    _saved_snapshot = dict(streaming._LAUNCH_TERMINAL_ENV_SNAPSHOT)
    _saved_owner = streaming._LAUNCH_TERMINAL_ENV_OWNER
    streaming._LAUNCH_TERMINAL_ENV_SNAPSHOT.clear()  # deterministic first capture
    try:
        # Startup order: init_profile_state() pins the process owner BEFORE
        # the import-time freeze, so the capture records alpha as the owner.
        monkeypatch.setattr(api_profiles, "_PROCESS_PROFILE_HOME", str(launch_home))

        # The launch process's env-only policy, present before any turn ran.
        monkeypatch.setenv("TERMINAL_ENV", "ssh")
        monkeypatch.setenv("TERMINAL_SSH_HOST", "bridge-host")
        streaming._capture_launch_terminal_env()

        # A sibling turn mutates the shared environ AFTER the freeze: the
        # owning turn must not see this (frozen snapshot, not live reads).
        os.environ["TERMINAL_ENV"] = "docker"

        # The launch home IS the process-owning home.
        scope_mod, token, installed = streaming._set_streaming_terminal_scope(
            str(launch_home)
        )
        try:
            assert installed, "helper must install for the owning home"
            assert (
                terminal_scope.terminal_env("TERMINAL_ENV", "local") == "ssh"
            ), "env-only launch policy lost when the scope bound (file-built default won)"
            assert (
                terminal_scope.terminal_env("TERMINAL_SSH_HOST", "") == "bridge-host"
            ), "launch overlay var missing from the owning-home scope"
            assert (
                terminal_scope.terminal_env("TERMINAL_ENV", "local") != "docker"
            ), "post-freeze sibling environ write leaked into the owning turn"
        finally:
            streaming._reset_streaming_terminal_scope(scope_mod, token, installed)

        # Routed secondary: empty profile resolves its own default and does
        # NOT borrow the launch env overlay.
        scope_mod2, token2, installed2 = streaming._set_streaming_terminal_scope(
            str(secondary_home)
        )
        try:
            assert installed2
            assert (
                terminal_scope.terminal_env("TERMINAL_ENV", "local") == "local"
            ), "routed secondary borrowed the launch profile's env-only policy"
        finally:
            streaming._reset_streaming_terminal_scope(scope_mod2, token2, installed2)
    finally:
        os.environ.pop("TERMINAL_ENV", None)
        streaming._LAUNCH_TERMINAL_ENV_SNAPSHOT.clear()
        streaming._LAUNCH_TERMINAL_ENV_SNAPSHOT.update(_saved_snapshot)
        streaming._LAUNCH_TERMINAL_ENV_OWNER = _saved_owner


@pytest.mark.skipif(not HAS_SCOPE, reason="requires scope machinery")
def test_process_wide_switch_does_not_inherit_launch_policy(tmp_path, monkeypatch):
    """Review of #7861, round 3 ([CORE]): the frozen launch TERMINAL_*
    snapshot is a loan to the home that owned the process when it was
    captured. After switch_profile(..., process_wide=True) re-pins the
    process owner to a new profile home, that new profile must resolve
    NEITHER the old deployment's env-only backend nor its host — its turns
    build policy from its own files (empty profile -> defaults), while a
    switch BACK to the captured owner must restore the full launch policy
    (the snapshot is owner-anchored, never destroyed)."""
    import api.profiles as api_profiles
    import api.streaming as streaming

    launch_home = tmp_path / "alpha"
    launch_home.mkdir()
    (launch_home / "config.yaml").write_text(
        "model:\n  default: test-model\n", encoding="utf-8"
    )  # NO terminal: section — the env-only ssh policy exists only at launch
    beta_home = tmp_path / "beta"
    beta_home.mkdir()  # no terminal settings of its own

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.delenv("TERMINAL_SSH_HOST", raising=False)

    _saved_snapshot = dict(streaming._LAUNCH_TERMINAL_ENV_SNAPSHOT)
    _saved_owner = streaming._LAUNCH_TERMINAL_ENV_OWNER
    streaming._LAUNCH_TERMINAL_ENV_SNAPSHOT.clear()  # deterministic first capture
    try:
        # The deployment launched with an env-only ssh policy, owned by
        # alpha: the owner pin is set at startup, BEFORE the import-time
        # freeze, so the capture records alpha as the snapshot's owner.
        monkeypatch.setattr(api_profiles, "_PROCESS_PROFILE_HOME", str(launch_home))
        monkeypatch.setenv("TERMINAL_ENV", "ssh")
        monkeypatch.setenv("TERMINAL_SSH_HOST", "prod.example")
        streaming._capture_launch_terminal_env()

        # switch_profile(..., process_wide=True) re-pins the owner to beta.
        monkeypatch.setattr(api_profiles, "_PROCESS_PROFILE_HOME", str(beta_home))

        # A beta turn binds its scope: it must NOT inherit the launch policy.
        scope_mod, token, installed = streaming._set_streaming_terminal_scope(
            str(beta_home)
        )
        try:
            assert installed, "helper must install for the new process owner"
            assert (
                terminal_scope.terminal_env("TERMINAL_ENV", "local") != "ssh"
            ), "new profile inherited the old deployment's env-only backend"
            assert (
                terminal_scope.terminal_env("TERMINAL_SSH_HOST", "") == ""
            ), "new profile inherited the old deployment's ssh host"
        finally:
            streaming._reset_streaming_terminal_scope(scope_mod, token, installed)

        # Switching back: the captured owner gets its launch policy again.
        monkeypatch.setattr(api_profiles, "_PROCESS_PROFILE_HOME", str(launch_home))
        scope_mod2, token2, installed2 = streaming._set_streaming_terminal_scope(
            str(launch_home)
        )
        try:
            assert installed2
            assert (
                terminal_scope.terminal_env("TERMINAL_ENV", "local") == "ssh"
            ), "return to the captured owner lost the launch env policy"
            assert (
                terminal_scope.terminal_env("TERMINAL_SSH_HOST", "") == "prod.example"
            ), "return to the captured owner lost the launch ssh host"
        finally:
            streaming._reset_streaming_terminal_scope(scope_mod2, token2, installed2)
    finally:
        os.environ.pop("TERMINAL_ENV", None)
        os.environ.pop("TERMINAL_SSH_HOST", None)
        streaming._LAUNCH_TERMINAL_ENV_SNAPSHOT.clear()
        streaming._LAUNCH_TERMINAL_ENV_SNAPSHOT.update(_saved_snapshot)
        streaming._LAUNCH_TERMINAL_ENV_OWNER = _saved_owner


@pytest.mark.skipif(not HAS_SCOPE, reason="requires scope machinery")
def test_scoped_turn_runs_in_session_workspace(tmp_path, monkeypatch):
    """Review of #7861 round 2, case 1: with the profile scope bound, a
    terminal command must run in the session WORKSPACE. The file-built scope
    resolves the profile's cwd (or home); without the turn overlay every
    WebUI command would run in the wrong directory. Exercised end-to-end
    through the agent's own cwd resolver (resolve_agent_cwd), which is what
    terminal_tool uses for command cwd."""
    import api.streaming as streaming

    # Profile home with terminal.cwd pinned to a DIFFERENT directory.
    home = tmp_path / "alpha"
    home.mkdir()
    pinned_cwd = tmp_path / "pinned-by-config"
    pinned_cwd.mkdir()
    workspace = tmp_path / "session-workspace"
    workspace.mkdir()
    (home / "config.yaml").write_text(
        textwrap.dedent(
            f"""\
            model:
              default: test-model
            terminal:
              backend: local
              cwd: {pinned_cwd}
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("TERMINAL_CWD", raising=False)

    # Turn overlay: runtime env (empty here) + workspace as TERMINAL_CWD —
    # exactly what the streaming call site passes.
    overlay = {"TERMINAL_CWD": str(workspace)}
    scope_mod, token, installed = streaming._set_streaming_terminal_scope(
        str(home), turn_overlay=overlay
    )
    try:
        assert installed
        from agent.runtime_cwd import resolve_agent_cwd
        assert str(resolve_agent_cwd()) == str(workspace), (
            "scoped turn resolved the profile-pinned cwd "
            f"({pinned_cwd}) instead of the session workspace ({workspace})"
        )
    finally:
        streaming._reset_streaming_terminal_scope(scope_mod, token, installed)

    # Negative control: WITHOUT the overlay the file-built scope wins and the
    # turn would run in the profile-pinned cwd — the bug her review found.
    scope_mod2, token2, installed2 = streaming._set_streaming_terminal_scope(str(home))
    try:
        assert installed2
        from agent.runtime_cwd import resolve_agent_cwd as rac2
        assert str(rac2()) == str(pinned_cwd)
    finally:
        streaming._reset_streaming_terminal_scope(scope_mod2, token2, installed2)


@pytest.mark.skipif(not HAS_SCOPE, reason="requires scope machinery")
def test_env_override_beats_yaml_in_scope(tmp_path, monkeypatch):
    """Review of #7861 round 2, case 2: WebUI applies the profile .env AFTER
    config.yaml, so .env wins in the pre-PR env mirror. The Agent's scope
    builder applies config.yaml last (YAML wins). The turn overlay must
    restore WebUI's precedence: a .env TERMINAL_ENV=docker override keeps
    working under the bound scope despite config.yaml backend: local."""
    import api.streaming as streaming

    home = tmp_path / "alpha"
    home.mkdir()
    (home / "config.yaml").write_text(
        textwrap.dedent(
            """\
            model:
              default: test-model
            terminal:
              backend: local
            """
        ),
        encoding="utf-8",
    )
    (home / ".env").write_text("TERMINAL_ENV=docker\n", encoding="utf-8")

    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    # What the streaming call site passes: the profile runtime env as WebUI
    # resolved it (.env applied after config.yaml -> docker) plus workspace.
    overlay = {"TERMINAL_ENV": "docker", "TERMINAL_CWD": str(tmp_path)}
    scope_mod, token, installed = streaming._set_streaming_terminal_scope(
        str(home), turn_overlay=overlay
    )
    try:
        assert installed
        assert terminal_scope.terminal_env("TERMINAL_ENV", "local") == "docker", (
            "profile .env terminal override lost under the bound scope — "
            "config.yaml precedence inverted WebUI's resolution order"
        )
    finally:
        streaming._reset_streaming_terminal_scope(scope_mod, token, installed)

    # Negative control: without the overlay, the Agent's file-built scope
    # applies config.yaml last and resolves local — the precedence inversion.
    scope_mod2, token2, installed2 = streaming._set_streaming_terminal_scope(str(home))
    try:
        assert installed2
        assert terminal_scope.terminal_env("TERMINAL_ENV", "local") == "local"
    finally:
        streaming._reset_streaming_terminal_scope(scope_mod2, token2, installed2)


@pytest.mark.skipif(not HAS_SCOPE, reason="requires scope machinery")
def test_turn_overlay_terminal_only_and_refusal_kept(tmp_path, monkeypatch):
    """Overlay hygiene: only TERMINAL_* keys from the turn overlay are
    applied (non-terminal thread-env keys must not enter the terminal
    policy), and an unreadable profile policy keeps the fail-closed refusal
    scope — the overlay never widens a refusal into a working policy."""
    import api.streaming as streaming

    home = tmp_path / "alpha"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: test-model\nterminal:\n  backend: local\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    overlay = {
        "TERMINAL_ENV": "ssh",
        "TERMINAL_SSH_HOST": "bridge-host",
        "HERMES_SESSION_KEY": "must-not-enter-the-scope",  # non-terminal key
    }
    scope_mod, token, installed = streaming._set_streaming_terminal_scope(
        str(home), turn_overlay=overlay
    )
    try:
        assert installed
        scope = terminal_scope.get_terminal_scope()
        assert isinstance(scope, dict)
        assert scope.get("TERMINAL_ENV") == "ssh"
        assert "HERMES_SESSION_KEY" not in scope, (
            "non-terminal key leaked into the terminal policy scope"
        )
        # The refusal-scope guard: terminal_env() on a POLICY scope answers
        # normally (proves this is a policy, not a refusal).
        assert terminal_scope.terminal_env("TERMINAL_SSH_HOST", "") == "bridge-host"
    finally:
        streaming._reset_streaming_terminal_scope(scope_mod, token, installed)

    # Unreadable profile policy -> refusal scope, overlay or not. The
    # TerminalPolicyUnavailable path is triggered directly (a directory in
    # place of config.yaml makes .exists() True and the open() fail), which
    # is deterministic regardless of filesystem permissions/running-as-root.
    bad_home = tmp_path / "unreadable"
    bad_home.mkdir()
    (bad_home / "config.yaml").mkdir()  # a DIRECTORY: present but unreadable
    scope_mod3, token3, installed3 = streaming._set_streaming_terminal_scope(
        str(bad_home), turn_overlay={"TERMINAL_ENV": "ssh"}
    )
    try:
        assert installed3
        from tools.terminal_scope import TerminalPolicyRefusal
        scope3 = terminal_scope.get_terminal_scope()
        assert isinstance(scope3, TerminalPolicyRefusal), (
            f"unreadable profile policy must keep the fail-closed refusal scope, got {type(scope3).__name__}"
        )
        # The refusal must refuse: terminal_env() raises rather than answering.
        raised = False
        try:
            terminal_scope.terminal_env("TERMINAL_ENV", "local")
        except Exception:
            raised = True
        assert raised, "refusal scope answered instead of refusing"
    finally:
        streaming._reset_streaming_terminal_scope(scope_mod3, token3, installed3)


@pytest.mark.skipif(not HAS_SCOPE, reason="requires scope machinery")
def test_scope_reaches_context_copying_worker(tmp_path, monkeypatch):
    """Review of #7861, case 2: the streaming parent installs the scope and
    the Agent's tool workers run under ``contextvars.copy_context()`` (the
    agent's context-propagating worker seam — agent/deadline.py,
    agent/memory_provider.py). A reader executed in a fresh thread under the
    COPIED context must see the parent turn's policy, not the shared
    os.environ. The prior concurrency test proved sibling isolation but
    installed the scope inside the reading thread itself — it did not prove
    parent-to-worker propagation."""
    import api.streaming as streaming
    import contextvars

    home_a = _seed_profile_home(tmp_path, "alpha", backend="docker")

    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    scope_mod, token, installed = streaming._set_streaming_terminal_scope(str(home_a))
    try:
        assert installed
        # Sibling export live in the shared environ while the worker runs.
        os.environ["TERMINAL_ENV"] = "local"

        ctx = contextvars.copy_context()
        results = {}

        def worker_reader():
            # Inside the Agent worker: no scope install of its own; policy
            # must arrive via the copied context.
            results["backend"] = terminal_scope.terminal_env("TERMINAL_ENV", "local")
            results["scope_present"] = terminal_scope.get_terminal_scope() is not None

        worker = threading.Thread(target=lambda: ctx.run(worker_reader))
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "worker thread hung"

        assert results.get("scope_present") is True, (
            "worker context saw no bound scope — parent install did not propagate"
        )
        assert results.get("backend") == "docker", (
            "worker resolved the sibling environ export instead of the "
            f"parent turn's policy: {results.get('backend')!r}"
        )
    finally:
        os.environ.pop("TERMINAL_ENV", None)
        streaming._reset_streaming_terminal_scope(scope_mod, token, installed)
