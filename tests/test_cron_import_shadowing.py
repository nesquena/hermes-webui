"""Regression coverage for top-level cron packages shadowing agent cron.jobs."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _drop_cron_modules() -> None:
    for name in list(sys.modules):
        if name == "cron" or name.startswith("cron."):
            sys.modules.pop(name, None)


def _reset_cron_import_path_ready(routes) -> None:
    routes._AGENT_CRON_IMPORT_PATH_READY = None


def test_agent_cron_import_path_prefers_agent_cron_over_plugin_shadow(monkeypatch, tmp_path):
    import api.config as config
    import api.routes as routes

    agent_dir = tmp_path / "hermes-agent"
    site_packages = tmp_path / "site-packages"
    agent_cron = agent_dir / "cron"
    shadow_cron = site_packages / "cron"
    agent_cron.mkdir(parents=True)
    shadow_cron.mkdir(parents=True)
    (agent_cron / "__init__.py").write_text("", encoding="utf-8")
    (agent_cron / "jobs.py").write_text(
        "def list_jobs(include_disabled=False):\n"
        "    return [{'id': 'agent-cron', 'include_disabled': include_disabled}]\n",
        encoding="utf-8",
    )
    (shadow_cron / "__init__.py").write_text("SHADOW_CRON = True\n", encoding="utf-8")

    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(routes, "_AGENT_CRON_IMPORT_PATH_READY", None)
    monkeypatch.syspath_prepend(str(agent_dir))
    monkeypatch.syspath_prepend(str(site_packages))
    _drop_cron_modules()
    try:
        shadowed_cron = importlib.import_module("cron")
        assert Path(shadowed_cron.__file__).resolve() == shadow_cron / "__init__.py"

        routes._ensure_agent_cron_import_path()
        cron_jobs = importlib.import_module("cron.jobs")

        assert Path(cron_jobs.__file__).resolve() == agent_cron / "jobs.py"
        assert cron_jobs.list_jobs(include_disabled=True) == [
            {"id": "agent-cron", "include_disabled": True}
        ]

        sys_path_after_first_call = list(sys.path)
        routes._ensure_agent_cron_import_path()
        assert sys.path == sys_path_after_first_call
    finally:
        _drop_cron_modules()
        _reset_cron_import_path_ready(routes)
