"""Regression tests for the #4273 ship-gate follow-up fixes (null-byte handling).

Scope note: an earlier gate round suggested restoring ~user/~root expansion and
blocking /root. Both were REVERTED after the full suite surfaced
tests/test_batch_fixes.py::TestRootWorkspaceUnblocked — /root is DELIBERATELY
not blocked (#510/#521: Hermes commonly runs as root, where /root is the
legitimate home, allowed via the home carve-out). Expanding ~root for a non-root
deployment would then register root's home, so the PR's original behavior
(leave ~user literal) is the correct, safer choice. Only the null-byte
fail-closed hardening is kept here — it has no contradiction.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.workspace as ws  # noqa: E402


def test_as_posix_path_rejects_null_byte():
    assert ws._as_posix_path("/home/u/p/\x00x") is None


def test_safe_resolve_fail_closed_on_null_byte():
    # Must not raise; falls back to the raw path so the block gate can reject it.
    out = ws._safe_resolve(Path("/home/u/\x00x"))
    assert isinstance(out, Path)


def test_validate_workspace_to_add_null_byte_is_clean_validation_error():
    import pytest
    with pytest.raises(ValueError) as ei:
        ws.validate_workspace_to_add("/home/user/project/\x00x")
    # Clean, fail-closed message — not the raw "embedded null byte" crash bubbling
    # uncaught (the route catches ValueError and returns a 400).
    assert "Invalid path" in str(ei.value) or "null" in str(ei.value).lower()


def test_root_remains_unblocked_for_root_deployments():
    # Guard the deliberate #510/#521 decision: /root must stay registrable
    # (Hermes-as-root deployments). This pins that our null-byte work did not
    # accidentally re-block it.
    src = (Path(__file__).resolve().parent.parent / "api" / "workspace.py").read_text(encoding="utf-8")
    assert "'/root'" not in src and "PurePosixPath('/root')" not in src, (
        "/root must not be blocked — breaks Hermes-as-root deployments (#510/#521)"
    )


def test_workspaces_add_route_has_home_carveout_before_block():
    # The /api/workspaces/add preflight must apply the home carve-out before
    # rejecting a blocked-system-root path, so systemd-homed /var/home/<user>
    # workspaces register (matching the validators). Pin the carve-out exists.
    src = (Path(__file__).resolve().parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
    assert "_home_path()" in src and "_is_within(candidate, _home)" in src, (
        "route preflight must apply the home carve-out before _is_blocked_system_path rejection"
    )
