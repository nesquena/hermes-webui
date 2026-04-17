"""
Tests for GitHub issue #609 — Docker workspace path trust and env-var priority.

Two independent bugs were fixed:

  1. HERMES_WEBUI_DEFAULT_WORKSPACE env var was silently overridden by
     settings.json at server startup.  The env var must always win.

  2. resolve_trusted_workspace() rejected paths that are children of
     DEFAULT_WORKSPACE (e.g. /data/workspace/project) when the default is a
     Docker volume mount outside the user's home directory.  Any path under
     the boot-time default should be trusted automatically.
"""
from pathlib import Path

import pytest

from api.workspace import resolve_trusted_workspace


# ── Fix 2: trust paths under DEFAULT_WORKSPACE ───────────────────────────────

def test_subdir_of_boot_default_is_trusted(monkeypatch, tmp_path):
    """A subdirectory of BOOT_DEFAULT_WORKSPACE must be trusted without being in
    the saved workspace list and without being under the user's home directory.

    This is the core Docker case: DEFAULT_WORKSPACE=/data/workspace, and the
    user tries to open /data/workspace/myproject — should NOT raise ValueError.
    """
    import api.workspace as ws_mod

    boot_default = tmp_path / "data" / "workspace"
    boot_default.mkdir(parents=True)
    sub = boot_default / "myproject"
    sub.mkdir()

    monkeypatch.setattr(ws_mod, "_BOOT_DEFAULT_WORKSPACE", str(boot_default))

    # Should not raise — sub is under the boot default
    result = resolve_trusted_workspace(str(sub))
    assert result == sub.resolve()


def test_boot_default_itself_is_trusted(monkeypatch, tmp_path):
    """The DEFAULT_WORKSPACE path itself must also be trusted (not only subdirs)."""
    import api.workspace as ws_mod

    boot_default = tmp_path / "data" / "workspace"
    boot_default.mkdir(parents=True)

    monkeypatch.setattr(ws_mod, "_BOOT_DEFAULT_WORKSPACE", str(boot_default))

    result = resolve_trusted_workspace(str(boot_default))
    assert result == boot_default.resolve()


def test_path_outside_boot_default_and_home_is_rejected(monkeypatch, tmp_path):
    """A path that is not under home, not in the saved list, and not under
    DEFAULT_WORKSPACE must still be rejected."""
    import api.workspace as ws_mod

    boot_default = tmp_path / "data" / "workspace"
    boot_default.mkdir(parents=True)
    outside = tmp_path / "other_mount" / "secret"
    outside.mkdir(parents=True)

    monkeypatch.setattr(ws_mod, "_BOOT_DEFAULT_WORKSPACE", str(boot_default))

    with pytest.raises(ValueError, match="outside the user home"):
        resolve_trusted_workspace(str(outside))


def test_none_path_returns_boot_default(monkeypatch, tmp_path):
    """resolve_trusted_workspace(None) always returns the boot default unchanged."""
    import api.workspace as ws_mod

    boot_default = tmp_path / "data" / "workspace"
    boot_default.mkdir(parents=True)

    monkeypatch.setattr(ws_mod, "_BOOT_DEFAULT_WORKSPACE", str(boot_default))

    result = resolve_trusted_workspace(None)
    assert result == boot_default.resolve()
