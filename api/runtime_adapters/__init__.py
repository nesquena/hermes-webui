"""Runtime adapter package for WebUI runtime backend selection.

Exposes a factory that builds the configured adapter based on
``HERMES_WEBUI_RUNTIME_ADAPTER`` and the associated env vars.
"""
from __future__ import annotations

import os
from typing import Any

from api.runtime_adapter import (
    RuntimeAdapter,
    runtime_adapter_mode,
    runtime_adapter_enabled,
    runtime_adapter_agent_runs_enabled,
    _RUNTIME_ADAPTER_DIRECT,
    _RUNTIME_ADAPTER_JOURNAL,
    _RUNTIME_ADAPTER_AGENT_RUNS,
)

_AGENT_RUNS_BASE_URL_ENV = "HERMES_WEBUI_AGENT_RUNS_BASE_URL"
_AGENT_RUNS_API_KEY_ENV = "HERMES_WEBUI_AGENT_RUNS_API_KEY"

_adapter_instance = None


def _reset_adapter_instance_for_test():
    global _adapter_instance
    _adapter_instance = None


def get_runtime_adapter(environ: dict[str, str] | None = None) -> RuntimeAdapter | None:
    """Return the configured runtime adapter, or None for legacy-direct."""
    global _adapter_instance
    mode = runtime_adapter_mode(environ)
    if mode == _RUNTIME_ADAPTER_DIRECT:
        return None
    if _adapter_instance is not None:
        return _adapter_instance
    if mode == _RUNTIME_ADAPTER_AGENT_RUNS:
        from api.runtime_adapters.agent_runs import AgentRunsAdapter

        _adapter_instance = AgentRunsAdapter.from_env(environ=environ)
        return _adapter_instance
    if mode == _RUNTIME_ADAPTER_JOURNAL:
        return None
    return None
