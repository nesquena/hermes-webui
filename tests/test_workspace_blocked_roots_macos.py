"""
Regression tests for the macOS symlink leg of the workspace blocked-roots check.

On macOS, ``/etc``, ``/var``, and ``/tmp`` are symlinks to ``/private/etc``,
``/private/var``, and ``/private/tmp``.  ``Path('/etc').resolve()`` returns
``/private/etc`` — so a literal-only blocked-roots set would miss the
resolved candidate and let the user register ``/etc`` as a workspace.

Conversely, ``/private/var/folders/<hash>/T/`` is the per-user tmp tree
(this is where pytest's ``tmp_path_factory`` writes), and must remain a
valid workspace candidate even though it lives nominally under ``/var``.

The ``TestEtcAlwaysBlocked`` and ``TestVarSystemBlockedButUserTmpAllowed``
classes contain checks that depend on the macOS layout (where ``/etc``
resolves to ``/private/etc``); those tests are skipped on Linux where
``/etc.resolve() == /etc`` and the ``/private/*`` aliases simply don't exist.
The cross-platform invariants (``/etc`` literal blocked, ``/tmp`` allowed,
non-symlink roots blocked) run on every platform.
"""

import sys
from pathlib import Path

import pytest

from api.workspace import (
    _USER_TMP_PREFIXES,
    _is_blocked_system_path,
    _workspace_blocked_roots,
)


_IS_MACOS = sys.platform == "darwin"
_macos_only = pytest.mark.skipif(not _IS_MACOS, reason="macOS-specific symlink layout")


# ── Blocked-roots set includes both literal and resolved forms ──────────────


class TestBlockedRootsCanonicalisation:
    def test_etc_literal_in_blocked_roots(self):
        assert Path("/etc") in _workspace_blocked_roots()

    def test_etc_resolved_in_blocked_roots(self):
        """``/etc.resolve()`` is ``/private/etc`` on macOS; same path on Linux.
        Either way the resolved form must appear in the set so a candidate
        that crossed a symlink during ``.resolve()`` still matches."""
        resolved = Path("/etc").resolve()
        assert resolved in _workspace_blocked_roots()

    def test_var_literal_and_resolved_in_blocked_roots(self):
        assert Path("/var") in _workspace_blocked_roots()
        assert Path("/var").resolve() in _workspace_blocked_roots()


# ── /etc is rejected on both Linux and macOS ────────────────────────────────


class TestEtcAlwaysBlocked:
    def test_etc_resolved_form_blocked(self):
        """The path-after-resolve form (``/private/etc`` on macOS, ``/etc`` on
        Linux) must be blocked."""
        assert _is_blocked_system_path(Path("/etc").resolve())

    def test_etc_subpath_blocked(self):
        assert _is_blocked_system_path(Path("/etc/hostname").resolve())

    @_macos_only
    def test_private_etc_explicit_blocked(self):
        """Even if the user writes ``/private/etc`` directly (knowing the
        macOS layout), it must still be blocked.  Skipped on Linux —
        ``/private/etc`` doesn't exist there and ``Path('/etc').resolve()``
        is ``/etc`` so the resolved-form aliasing is a no-op."""
        assert _is_blocked_system_path(Path("/private/etc"))


# ── /var is selectively blocked (system parts) but tmp carve-outs work ──────


class TestVarSystemBlockedButUserTmpAllowed:
    def test_var_log_blocked(self):
        """``/private/var/log`` would have been a macOS-only security gap
        before this fix — it resolved through the ``/var`` symlink and
        didn't match the literal blocked root."""
        assert _is_blocked_system_path(Path("/var/log").resolve())

    @_macos_only
    def test_private_var_log_blocked(self):
        """Skipped on Linux: ``/private/var/log`` isn't an alias for
        ``/var/log`` there; ``Path('/var').resolve() == /var`` so no
        ``/private/var`` ever lands in the blocked set."""
        assert _is_blocked_system_path(Path("/private/var/log"))

    def test_var_folders_user_tmp_allowed(self):
        """macOS per-user tmp under /var/folders/<hash>/T/ — pytest's
        tmp_path_factory writes here. Must remain registerable."""
        # This path may not actually exist; the carve-out is path-shape based.
        candidate = Path("/var/folders/abc/T/some-test-dir")
        assert not _is_blocked_system_path(candidate)

    def test_private_var_folders_user_tmp_allowed(self):
        candidate = Path("/private/var/folders/abc/T/some-test-dir")
        assert not _is_blocked_system_path(candidate)

    def test_var_tmp_user_writable_allowed(self):
        """``/var/tmp`` is system-wide user-writable tmp on Linux/macOS.
        Carved out so users can register tmp dirs there."""
        assert not _is_blocked_system_path(Path("/var/tmp/my-workspace"))
        assert not _is_blocked_system_path(Path("/private/var/tmp/my-workspace"))


# ── Carve-out invariants ───────────────────────────────────────────────────


class TestUserTmpPrefixes:
    def test_var_folders_in_carveouts(self):
        assert Path("/var/folders") in _USER_TMP_PREFIXES
        assert Path("/private/var/folders") in _USER_TMP_PREFIXES

    def test_var_tmp_in_carveouts(self):
        assert Path("/var/tmp") in _USER_TMP_PREFIXES
        assert Path("/private/var/tmp") in _USER_TMP_PREFIXES

    def test_carveouts_only_loosen_var_subtree(self):
        """Carve-outs must not let /etc or other strict roots through."""
        for tmp in _USER_TMP_PREFIXES:
            # tmp paths are under /var or /private/var, never under /etc, /usr, /bin, etc.
            assert str(tmp).startswith("/var/") or str(tmp).startswith("/private/var/")


# ── Other roots: literal == resolved on both platforms ─────────────────────


class TestNonSymlinkRootsUnchanged:
    @pytest.mark.parametrize(
        "root",
        [
            "/usr",
            "/bin",
            "/sbin",
            "/proc",
            "/sys",
            "/dev",
            "/lib",
            "/opt/homebrew",
        ],
    )
    def test_root_blocked(self, root):
        # Whether or not the root exists, the literal form must be blocked.
        assert _is_blocked_system_path(Path(root))
        # And the resolved form (almost always equal to literal for these).
        assert _is_blocked_system_path(Path(root).resolve())

    @pytest.mark.parametrize(
        "subpath",
        [
            "/usr/local/bin/something",
            "/proc/self/maps",
            "/sys/class/net",
            "/dev/null",
        ],
    )
    def test_subpath_blocked(self, subpath):
        # Use Path() not .resolve() — we want to assert the shape-based block,
        # not test whether the path actually exists on the test runner.
        assert _is_blocked_system_path(Path(subpath))


# ── New macOS-specific blocked roots: /System and /Library ──────────────────


class TestMacOSSystemAndLibraryBlocked:
    """macOS has /System and /Library as top-level OS directories that must be
    blocked even on Linux (where they don't exist) since the path shapes are
    meaningful on macOS and should always be rejected.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/Library",
            "/Library/Application Support",
            "/Library/Preferences",
            "/System",
            "/System/Library",
            "/System/Library/CoreServices",
        ],
    )
    def test_macos_os_roots_blocked(self, path):
        """Paths under /Library and /System must be blocked regardless of platform."""
        from api.workspace import _is_blocked_workspace_path

        assert _is_blocked_workspace_path(Path(path))
