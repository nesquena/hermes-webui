"""Regression tests for issue #4700.

Discovery of a pip-style hermes-agent checkout must accept ``cron/jobs.py``
with core markers even when ``run_agent.py`` is missing.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _isolate_discovery_inputs(config, monkeypatch, tmp_path: Path) -> None:
    """Force `_discover_agent_dir()` to only use test-controlled candidates."""
    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setattr(config, "_DEFAULT_HERMES_HOME", tmp_path / "default-hermes-home")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "webui-repo")


def _make_pip_style_agent_root(root: Path) -> Path:
    """Create a pip-style root with cron/jobs.py and a core marker."""
    marker = root / "hermes"
    cron_jobs = root / "cron" / "jobs.py"
    marker.mkdir(parents=True)
    cron_jobs.parent.mkdir(parents=True)
    cron_jobs.write_text("", encoding="utf-8")
    return root


def _make_cron_only_root(root: Path) -> Path:
    """Create a cron-only lookalike that must stay rejected."""
    cron_jobs = root / "cron" / "jobs.py"
    cron_jobs.parent.mkdir(parents=True)
    cron_jobs.write_text("", encoding="utf-8")
    return root


def _make_legacy_agent_root(root: Path) -> Path:
    """Create the legacy launcher root with run_agent.py."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "run_agent.py").write_text("", encoding="utf-8")
    return root


def _drop_cron_modules() -> None:
    for name in list(sys.modules):
        if name == "cron" or name.startswith("cron."):
            sys.modules.pop(name, None)


def _reset_agent_cron_import_path_state(routes) -> None:
    routes._AGENT_CRON_IMPORT_PATH_READY = None


def test_discover_agent_dir_accepts_pip_style_root_without_run_agent(monkeypatch, tmp_path):
    """The primary regression row for #4700: a pip-style root now resolves."""
    import api.config as config

    pip_root = _make_pip_style_agent_root(tmp_path / "pip-style-agent")
    _isolate_discovery_inputs(config, monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", str(pip_root))

    assert config._discover_agent_dir() == pip_root


def test_discover_agent_dir_rejects_cron_only_directory_without_agent_markers(
    monkeypatch, tmp_path
):
    """A cron-only directory without a core marker must remain rejected."""
    import api.config as config

    cron_only = _make_cron_only_root(tmp_path / "cron-only")
    _isolate_discovery_inputs(config, monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", str(cron_only))

    assert config._discover_agent_dir() is None


def test_legacy_run_agent_layout_still_wins(monkeypatch, tmp_path):
    """Legacy `run_agent.py` discovery is unchanged and still takes precedence."""
    import api.config as config

    legacy = _make_legacy_agent_root(tmp_path / "legacy-agent")
    _make_pip_style_agent_root(tmp_path / "fallback-pip-agent")
    _isolate_discovery_inputs(config, monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", str(legacy))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    (tmp_path / "hermes-home" / "hermes-agent").mkdir(parents=True, exist_ok=True)
    _make_pip_style_agent_root(tmp_path / "hermes-home" / "hermes-agent")

    assert config._discover_agent_dir() == legacy


def test_routes_shadow_helper_can_recover_once_agent_dir_resolves(monkeypatch, tmp_path):
    """Once `_AGENT_DIR` resolves, `_ensure_agent_cron_import_path()` drops shadow
    modules and rewires imports to the agent cron package."""
    import api.config as config
    import api.routes as routes

    agent_dir = _make_pip_style_agent_root(tmp_path / "hermes-agent")
    shadow_site_packages = tmp_path / "shadow-site-packages"
    shadow_cron = shadow_site_packages / "cron"
    (shadow_cron / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
    (shadow_cron / "__init__.py").write_text("SHADOW = True", encoding="utf-8")
    (agent_dir / "cron").mkdir(parents=True, exist_ok=True)
    (agent_dir / "cron" / "__init__.py").write_text("", encoding="utf-8")
    (agent_dir / "cron" / "jobs.py").write_text(
        "def list_jobs(*_args, **_kwargs):\n"
        "    return [{\"id\": \"agent-cron\"}]\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(routes, "_AGENT_CRON_IMPORT_PATH_READY", None)
    monkeypatch.setattr(sys, "path", [str(shadow_site_packages)])
    _reset_agent_cron_import_path_state(routes)

    try:
        _drop_cron_modules()
        shadowed = importlib.import_module("cron")
        assert Path(shadowed.__file__).resolve() == (shadow_cron / "__init__.py").resolve()

        routes._ensure_agent_cron_import_path()
        assert "cron" not in sys.modules

        cron_jobs = importlib.import_module("cron.jobs")
        assert Path(cron_jobs.__file__).resolve() == (
            agent_dir / "cron" / "jobs.py"
        ).resolve()
        assert cron_jobs.list_jobs() == [{"id": "agent-cron"}]
    finally:
        _drop_cron_modules()
        _reset_agent_cron_import_path_state(routes)
