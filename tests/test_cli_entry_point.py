"""Smoke tests for the packaged ``hermes-webui`` CLI entry point (#6739).

The console script is declared in ``pyproject.toml`` under
``[project.scripts]`` and must keep resolving to ``bootstrap.main`` — the
bootstrap entry point runs launcher-python discovery and dep-ensure before
launching the server, matching the effective behavior of
``python server.py`` / ``python -m server``. These tests
guard the wiring without booting a real server:

1. The console script is declared in the packaging metadata.
2. The declared target (``bootstrap:main``) actually resolves to a callable.
3. When the package is installed in the test environment (e.g. CI editable
   install), the real ``importlib.metadata`` entry point resolves end-to-end.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import os
import tomllib

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED_SCRIPT = "hermes-webui"
EXPECTED_TARGET = "bootstrap:main"


def _pyproject_scripts() -> dict:
    """Parse the [project.scripts] table from pyproject.toml."""
    with open(os.path.join(REPO_ROOT, "pyproject.toml"), "rb") as fh:
        pyproject = tomllib.load(fh)
    return dict(pyproject.get("project", {}).get("scripts", {}))


def test_console_script_declared_in_pyproject():
    """The packaged CLI entry point is declared and maps to bootstrap.main."""
    scripts = _pyproject_scripts()
    assert scripts.get(EXPECTED_SCRIPT) == EXPECTED_TARGET


def test_console_script_target_resolves_to_callable():
    """The declared module:attr target imports and is callable."""
    module_name, _, attr = EXPECTED_TARGET.partition(":")
    module = importlib.import_module(module_name)
    target = getattr(module, attr)
    assert callable(target)


def _installed_entry_point():
    """Return the installed hermes-webui console-script EntryPoint, or None."""
    selected = importlib.metadata.entry_points().select(
        group="console_scripts", name=EXPECTED_SCRIPT
    )
    return selected[0] if selected else None


def test_installed_entry_point_wiring():
    """When installed, the console script must resolve to bootstrap.main."""
    ep = _installed_entry_point()
    if ep is None:
        pytest.skip("hermes-webui not installed in this test environment")
    assert ep.value == EXPECTED_TARGET
    assert callable(ep.load())
