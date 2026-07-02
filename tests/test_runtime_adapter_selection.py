import importlib
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from api.runtime_adapter import (
    runtime_adapter_mode,
    runtime_adapter_enabled,
    runtime_adapter_agent_runs_enabled,
    runtime_adapter_runner_enabled,
    build_runtime_adapter,
    _RUNTIME_ADAPTER_DIRECT,
    _RUNTIME_ADAPTER_JOURNAL,
    _RUNTIME_ADAPTER_AGENT_RUNS,
)

from api.runtime_adapters import get_runtime_adapter, _reset_adapter_instance_for_test


def test_default_env_selects_legacy_direct(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_RUNTIME_ADAPTER", raising=False)
    assert runtime_adapter_mode() == "legacy-direct"
    assert runtime_adapter_enabled() is False
    assert runtime_adapter_agent_runs_enabled() is False


def test_legacy_direct_explicit(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    assert runtime_adapter_mode() == "legacy-direct"
    assert runtime_adapter_enabled() is False


def test_legacy_journal_selects_legacy_journal(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    assert runtime_adapter_mode() == "legacy-journal"
    assert runtime_adapter_enabled() is True
    assert runtime_adapter_agent_runs_enabled() is False


def test_agent_runs_selects_agent_runs(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
    monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
    assert runtime_adapter_mode() == "agent-runs"
    assert runtime_adapter_agent_runs_enabled() is True


def test_unknown_adapter_value_gives_clean_config_error(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "bogus-mode")
    assert runtime_adapter_mode() == "legacy-direct"
    assert runtime_adapter_enabled() is False


def test_agent_runs_without_base_url_raises(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
    monkeypatch.delenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", raising=False)
    assert runtime_adapter_mode() == "agent-runs"
    assert runtime_adapter_agent_runs_enabled() is True
    _reset_adapter_instance_for_test()
    with pytest.raises(ValueError, match="HERMES_WEBUI_AGENT_RUNS_BASE_URL"):
        get_runtime_adapter()


def test_agent_runs_adapter_singleton(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
    monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
    _reset_adapter_instance_for_test()
    a1 = get_runtime_adapter()
    a2 = get_runtime_adapter()
    assert a1 is a2


def test_legacy_direct_returns_none(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    _reset_adapter_instance_for_test()
    assert get_runtime_adapter() is None


def test_legacy_journal_returns_none_from_factory(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    _reset_adapter_instance_for_test()
    assert get_runtime_adapter() is None


def test_build_runtime_adapter_agent_runs(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
    monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
    _reset_adapter_instance_for_test()
    from api.runtime_adapters.agent_runs import AgentRunsAdapter

    adapter = build_runtime_adapter(
        agent_runs_adapter_factory=lambda: AgentRunsAdapter(base_url="http://127.0.0.1:8642"),
    )
    assert adapter is not None
    assert isinstance(adapter, AgentRunsAdapter)


def test_capabilities_uses_env_mode(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
    assert runtime_adapter_mode() == "agent-runs"
    assert runtime_adapter_enabled() is False
    assert runtime_adapter_agent_runs_enabled() is True


def test_empty_env_uses_default(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_RUNTIME_ADAPTER", raising=False)
    assert runtime_adapter_mode() == "legacy-direct"


def test_whitespace_env_uses_default(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "   ")
    assert runtime_adapter_mode() == "legacy-direct"
