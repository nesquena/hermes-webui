"""Regression tests for #5937 multi-profile terminal backend isolation.

WebUI must invalidate the agent process-global "default" terminal env when the
selected profile's backend identity changes (e.g. SSH → local), without
evicting on same-backend consecutive turns.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import api.terminal_backend_isolation as isolation


REPO_ROOT = Path(__file__).parent.parent
STREAMING_PY = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")


def setup_function(_fn=None):
    isolation.reset_terminal_backend_identity_for_tests()


def test_identity_defaults_missing_backend_to_local():
    assert isolation.terminal_backend_identity({})[0] == "local"
    assert isolation.terminal_backend_identity({"TERMINAL_ENV": "  SSH "})[0] == "ssh"


def test_identity_includes_ssh_target_not_cwd():
    a = isolation.terminal_backend_identity(
        {
            "TERMINAL_ENV": "ssh",
            "TERMINAL_SSH_HOST": "box.example",
            "TERMINAL_SSH_USER": "deploy",
            "TERMINAL_SSH_PORT": "22",
            "TERMINAL_CWD": "/a",
        }
    )
    b = isolation.terminal_backend_identity(
        {
            "TERMINAL_ENV": "ssh",
            "TERMINAL_SSH_HOST": "box.example",
            "TERMINAL_SSH_USER": "deploy",
            "TERMINAL_SSH_PORT": "22",
            "TERMINAL_CWD": "/b",
        }
    )
    assert a == b
    different_host = isolation.terminal_backend_identity(
        {
            "TERMINAL_ENV": "ssh",
            "TERMINAL_SSH_HOST": "other.example",
            "TERMINAL_SSH_USER": "deploy",
            "TERMINAL_SSH_PORT": "22",
        }
    )
    assert a != different_host


def test_first_turn_records_identity_without_cleanup():
    cleanup = MagicMock()
    assert isolation.maybe_invalidate_default_terminal_env(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box"},
        cleanup_vm=cleanup,
    ) is False
    cleanup.assert_not_called()


def test_same_backend_consecutive_turns_do_not_cleanup():
    cleanup = MagicMock()
    env = {
        "TERMINAL_ENV": "local",
    }
    assert isolation.maybe_invalidate_default_terminal_env(env, cleanup_vm=cleanup) is False
    assert isolation.maybe_invalidate_default_terminal_env(env, cleanup_vm=cleanup) is False
    cleanup.assert_not_called()


def test_ssh_then_local_invalidates_default_slot():
    cleanup = MagicMock()
    ssh = {
        "TERMINAL_ENV": "ssh",
        "TERMINAL_SSH_HOST": "box.example",
        "TERMINAL_SSH_USER": "deploy",
        "TERMINAL_SSH_PORT": "22",
    }
    local = {"TERMINAL_ENV": "local"}
    assert isolation.maybe_invalidate_default_terminal_env(ssh, cleanup_vm=cleanup) is False
    assert isolation.maybe_invalidate_default_terminal_env(local, cleanup_vm=cleanup) is True
    cleanup.assert_called_once_with("default")


def test_local_then_ssh_invalidates_default_slot():
    cleanup = MagicMock()
    assert isolation.maybe_invalidate_default_terminal_env(
        {"TERMINAL_ENV": "local"}, cleanup_vm=cleanup
    ) is False
    assert isolation.maybe_invalidate_default_terminal_env(
        {
            "TERMINAL_ENV": "ssh",
            "TERMINAL_SSH_HOST": "box.example",
            "TERMINAL_SSH_USER": "deploy",
        },
        cleanup_vm=cleanup,
    ) is True
    cleanup.assert_called_once_with("default")


def test_ssh_host_change_invalidates():
    cleanup = MagicMock()
    a = {
        "TERMINAL_ENV": "ssh",
        "TERMINAL_SSH_HOST": "a.example",
        "TERMINAL_SSH_USER": "u",
    }
    b = {
        "TERMINAL_ENV": "ssh",
        "TERMINAL_SSH_HOST": "b.example",
        "TERMINAL_SSH_USER": "u",
    }
    isolation.maybe_invalidate_default_terminal_env(a, cleanup_vm=cleanup)
    assert isolation.maybe_invalidate_default_terminal_env(b, cleanup_vm=cleanup) is True
    cleanup.assert_called_once_with("default")


def test_cleanup_exception_is_swallowed_and_returns_false():
    cleanup = MagicMock(side_effect=RuntimeError("boom"))
    isolation.maybe_invalidate_default_terminal_env(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "a"},
        cleanup_vm=cleanup,
    )
    assert (
        isolation.maybe_invalidate_default_terminal_env(
            {"TERMINAL_ENV": "local"},
            cleanup_vm=cleanup,
        )
        is False
    )


def test_streaming_acquires_full_turn_lease_on_effective_env():
    """Source guard: the turn path leases the identity for the whole turn.

    Identity must come from the effective post-merge environment (process env
    overlaid with the profile runtime env), the lease must be acquired BEFORE
    _ENV_LOCK (waiters must not block other turns' env restore), and it must
    be released in the turn's finally after the env restore.
    """
    assert "acquire_terminal_backend_turn_lease" in STREAMING_PY
    # Effective env = os.environ overlaid with the profile runtime env.
    merged_idx = STREAMING_PY.find("_effective_turn_env = dict(os.environ)")
    assert merged_idx >= 0
    overlay_idx = STREAMING_PY.find(
        "_effective_turn_env.update(_safe_profile_runtime_env)", merged_idx
    )
    assert overlay_idx > merged_idx
    acquire_idx = STREAMING_PY.find(
        "acquire_terminal_backend_turn_lease(", overlay_idx
    )
    assert acquire_idx > overlay_idx
    # Lease acquisition happens before the env-mutation critical section.
    update_idx = STREAMING_PY.find("os.environ.update(_safe_profile_runtime_env)")
    assert update_idx >= 0
    assert acquire_idx < update_idx
    # Released in the finally, after the profile env restore loop.
    restore_idx = STREAMING_PY.find("for _key, _old_value in old_profile_env.items()")
    release_idx = STREAMING_PY.find("_terminal_backend_lease.release()")
    assert restore_idx >= 0
    assert release_idx > restore_idx
    # Point-in-time invalidation is no longer called from the turn path.
    assert "maybe_invalidate_default_terminal_env(_safe_profile_runtime_env)" not in STREAMING_PY


def test_docker_transition_force_removes_container():
    """Gate #5988 CORE: leaving a Docker identity must force-remove so a stale
    persistent container can't be reattached by labels under a new image."""
    cleanup = MagicMock()
    docker_a = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img:a"}
    local = {"TERMINAL_ENV": "local"}
    assert isolation.maybe_invalidate_default_terminal_env(docker_a, cleanup_vm=cleanup) is False
    assert isolation.maybe_invalidate_default_terminal_env(local, cleanup_vm=cleanup) is True
    cleanup.assert_called_once_with("default", force_remove=True)


def test_docker_image_change_invalidates_with_force_remove():
    cleanup = MagicMock()
    docker_a = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img:a"}
    docker_b = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img:b"}
    isolation.maybe_invalidate_default_terminal_env(docker_a, cleanup_vm=cleanup)
    assert isolation.maybe_invalidate_default_terminal_env(docker_b, cleanup_vm=cleanup) is True
    cleanup.assert_called_once_with("default", force_remove=True)


def test_non_docker_transition_does_not_force_remove():
    cleanup = MagicMock()
    ssh = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box"}
    docker = {"TERMINAL_ENV": "docker"}
    isolation.maybe_invalidate_default_terminal_env(ssh, cleanup_vm=cleanup)
    assert isolation.maybe_invalidate_default_terminal_env(docker, cleanup_vm=cleanup) is True
    cleanup.assert_called_once_with("default")


def test_identity_includes_ssh_key_and_modal_mode():
    """Gate #5988: two genuinely-different backends must not hash equal."""
    key_a = isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box", "TERMINAL_SSH_KEY": "/k/a"}
    )
    key_b = isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box", "TERMINAL_SSH_KEY": "/k/b"}
    )
    assert key_a != key_b
    modal_direct = isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "modal", "TERMINAL_MODAL_MODE": "direct"}
    )
    modal_managed = isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "modal", "TERMINAL_MODAL_MODE": "managed"}
    )
    assert modal_direct != modal_managed


def test_identity_normalizes_agent_defaults():
    """Absent keys and explicitly-configured agent defaults hash equal."""
    assert isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box"}
    ) == isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box", "TERMINAL_SSH_PORT": "22"}
    )
    assert isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "docker"}
    ) == isolation.terminal_backend_identity(
        {
            "TERMINAL_ENV": "docker",
            "TERMINAL_DOCKER_IMAGE": "nikolaik/python-nodejs:python3.11-nodejs20",
        }
    )
    assert isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "modal"}
    ) == isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "modal", "TERMINAL_MODAL_MODE": "AUTO"}
    )


def test_identity_excludes_inactive_backend_settings():
    """A stray SSH/docker var in env must not perturb another backend's identity."""
    assert isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "local", "TERMINAL_SSH_HOST": "leftover.example"}
    ) == isolation.terminal_backend_identity({"TERMINAL_ENV": "local"})
    assert isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box", "TERMINAL_DOCKER_IMAGE": "img:x"}
    ) == isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box"}
    )


def test_cleanup_failure_is_retried_by_next_turn():
    """Gate #5988: a transient failure must not permanently skip invalidation."""
    failing = MagicMock(side_effect=RuntimeError("boom"))
    ok = MagicMock()
    ssh = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box"}
    local = {"TERMINAL_ENV": "local"}
    assert isolation.maybe_invalidate_default_terminal_env(ssh, cleanup_vm=ok) is False
    # Transition attempt fails: not committed.
    assert isolation.maybe_invalidate_default_terminal_env(local, cleanup_vm=failing) is False
    # Next turn on the same new identity retries and succeeds.
    assert isolation.maybe_invalidate_default_terminal_env(local, cleanup_vm=ok) is True
    ok.assert_called_once_with("default")


def test_import_failure_is_retried_by_next_turn():
    """Same retry guarantee when cleanup_vm cannot even be resolved."""
    ssh = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box"}
    local = {"TERMINAL_ENV": "local"}
    ok = MagicMock()
    assert isolation.maybe_invalidate_default_terminal_env(ssh, cleanup_vm=ok) is False
    original = isolation._resolve_cleanup_vm
    isolation._resolve_cleanup_vm = lambda: None
    try:
        assert isolation.maybe_invalidate_default_terminal_env(local) is False
    finally:
        isolation._resolve_cleanup_vm = original
    assert isolation.maybe_invalidate_default_terminal_env(local, cleanup_vm=ok) is True
    ok.assert_called_once_with("default")


def test_differing_backend_turn_waits_for_active_lease():
    """Gate #5988: a differing-backend turn must not evict an in-use env."""
    import threading as _threading

    cleanup = MagicMock()
    ssh = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box"}
    local = {"TERMINAL_ENV": "local"}
    first, invalidated = isolation.acquire_terminal_backend_turn_lease(
        ssh, cleanup_vm=cleanup
    )
    assert invalidated is False

    entered = _threading.Event()
    result = {}

    def _differing_turn():
        entered.set()
        lease, did = isolation.acquire_terminal_backend_turn_lease(
            local, cleanup_vm=cleanup, wait_seconds=10
        )
        result["invalidated"] = did
        lease.release()

    thread = _threading.Thread(target=_differing_turn, daemon=True)
    thread.start()
    entered.wait(timeout=5)
    time_waited = thread.join(timeout=0.5)  # noqa: F841 — expect still alive
    assert thread.is_alive(), "differing-backend turn should wait on the lease"
    cleanup.assert_not_called()

    first.release()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["invalidated"] is True
    cleanup.assert_called_once_with("default")


def test_lease_wait_timeout_skips_invalidation_and_retries_later():
    cleanup = MagicMock()
    ssh = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box"}
    local = {"TERMINAL_ENV": "local"}
    first, _ = isolation.acquire_terminal_backend_turn_lease(ssh, cleanup_vm=cleanup)
    lease, invalidated = isolation.acquire_terminal_backend_turn_lease(
        local, cleanup_vm=cleanup, wait_seconds=0.05
    )
    assert invalidated is False
    cleanup.assert_not_called()
    lease.release()
    first.release()
    # Transition was not committed — a later unobstructed turn retries.
    lease2, invalidated2 = isolation.acquire_terminal_backend_turn_lease(
        local, cleanup_vm=cleanup
    )
    assert invalidated2 is True
    cleanup.assert_called_once_with("default")
    lease2.release()


def test_concurrent_same_identity_turns_share_one_invalidation():
    cleanup = MagicMock()
    ssh = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box"}
    local = {"TERMINAL_ENV": "local"}
    seed, _ = isolation.acquire_terminal_backend_turn_lease(ssh, cleanup_vm=cleanup)
    seed.release()
    first, did_first = isolation.acquire_terminal_backend_turn_lease(
        local, cleanup_vm=cleanup
    )
    # Second same-identity turn while the first is still running: piggybacks.
    second, did_second = isolation.acquire_terminal_backend_turn_lease(
        local, cleanup_vm=cleanup
    )
    assert did_first is True
    assert did_second is False
    cleanup.assert_called_once_with("default")
    second.release()
    first.release()
