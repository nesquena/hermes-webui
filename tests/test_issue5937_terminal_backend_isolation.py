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


def test_streaming_applies_isolation_after_profile_runtime_env_update():
    """Source guard: the turn path must call isolation after os.environ.update."""
    assert "maybe_invalidate_default_terminal_env" in STREAMING_PY
    assert "terminal_backend_isolation" in STREAMING_PY
    # Ordering: update profile runtime env, then isolation check.
    update_idx = STREAMING_PY.find("os.environ.update(_safe_profile_runtime_env)")
    isolate_idx = STREAMING_PY.find("maybe_invalidate_default_terminal_env(_safe_profile_runtime_env)")
    assert update_idx >= 0
    assert isolate_idx > update_idx
