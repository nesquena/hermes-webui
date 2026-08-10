"""Opt-in strict workspace registration (#6424).

Default Add Space remains permissive for external mounts (#953/#991).
When HERMES_WEBUI_STRICT_WORKSPACE_REGISTRATION is enabled, registration
cannot widen the trusted filesystem boundary to arbitrary existing dirs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from api import workspace


def test_strict_registration_rejects_outside_root(tmp_path, monkeypatch):
    outside = tmp_path / "outside-agent-root"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(workspace.STRICT_WORKSPACE_REGISTRATION_ENV, "1")
    monkeypatch.delenv(workspace.ALLOWED_WORKSPACE_ROOTS_ENV, raising=False)
    monkeypatch.setattr(workspace, "_home_path", lambda: home)
    monkeypatch.setattr(workspace, "_BOOT_DEFAULT_WORKSPACE", str(home / "workspace"))

    with pytest.raises(ValueError, match="Strict workspace registration"):
        workspace.validate_workspace_to_add(str(outside))


def test_strict_registration_allows_home_descendant(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = home / "projects" / "demo"
    project.mkdir(parents=True)
    monkeypatch.setenv(workspace.STRICT_WORKSPACE_REGISTRATION_ENV, "1")
    monkeypatch.delenv(workspace.ALLOWED_WORKSPACE_ROOTS_ENV, raising=False)
    monkeypatch.setattr(workspace, "_home_path", lambda: home)
    monkeypatch.setattr(workspace, "_BOOT_DEFAULT_WORKSPACE", str(home / "workspace"))

    assert workspace.validate_workspace_to_add(str(project)) == project.resolve()


def test_strict_registration_allows_configured_extra_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    allowed = tmp_path / "mnt" / "projects"
    allowed.mkdir(parents=True)
    monkeypatch.setenv(workspace.STRICT_WORKSPACE_REGISTRATION_ENV, "1")
    monkeypatch.setenv(workspace.ALLOWED_WORKSPACE_ROOTS_ENV, str(allowed))
    monkeypatch.setattr(workspace, "_home_path", lambda: home)
    monkeypatch.setattr(workspace, "_BOOT_DEFAULT_WORKSPACE", str(home / "workspace"))

    assert workspace.validate_workspace_to_add(str(allowed)) == allowed.resolve()


def test_default_registration_still_allows_external_paths(tmp_path, monkeypatch):
    outside = tmp_path / "external-mount"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv(workspace.STRICT_WORKSPACE_REGISTRATION_ENV, raising=False)
    monkeypatch.setattr(workspace, "_home_path", lambda: home)

    assert workspace.validate_workspace_to_add(str(outside)) == outside.resolve()
