"""Tests for run-scoped routed-model lifecycle capture."""

from concurrent.futures import ThreadPoolExecutor
import sys
from threading import Barrier
from types import ModuleType, SimpleNamespace

import pytest

import api.routed_model_observability as observability
from api.routed_model_observability import (
    _install_post_api_request_observer,
    _observe_post_api_request,
    begin_routed_model_capture,
    provider_display_name,
    reset_routed_model_capture,
    snapshot_routed_model_capture,
)


@pytest.fixture(autouse=True)
def _isolate_observer_install(monkeypatch):
    monkeypatch.setattr(observability, "_install_post_api_request_observer", lambda: None)


def test_capture_uses_last_matching_safe_response_and_reset_clears_it():
    token = begin_routed_model_capture(
        session_id="session-a",
        stream_id="stream-a",
        task_id="session-a",
        requested_model="auto",
        requested_provider="TokenTable",
    )
    try:
        _observe_post_api_request(
            platform="WebUI",
            session_id="session-a",
            task_id="session-a",
            response_model="gpt-5.6",
            provider="custom",
        )
        _observe_post_api_request(
            platform="webui",
            session_id="session-a",
            task_id="session-a",
            response_model="gpt-5.6-sol",
            provider="custom",
        )

        assert snapshot_routed_model_capture() == {
            "requested_model": "auto",
            "requested_provider": "TokenTable",
            "used_model": "gpt-5.6-sol",
            "used_provider": "TokenTable",
            "source": "openai-compatible-sse",
        }
    finally:
        reset_routed_model_capture(token)

    assert snapshot_routed_model_capture() is None


@pytest.mark.parametrize(
    ("platform", "session_id", "task_id"),
    [
        ("telegram", "session-a", "session-a"),
        ("webui", "session-b", "session-a"),
        ("webui", "session-a", "task-b"),
    ],
)
def test_capture_ignores_nonmatching_lifecycle_events(platform, session_id, task_id):
    token = begin_routed_model_capture(
        session_id="session-a",
        stream_id="stream-a",
        task_id="session-a",
        requested_model="auto",
        requested_provider="TokenTable",
    )
    try:
        _observe_post_api_request(
            platform=platform,
            session_id=session_id,
            task_id=task_id,
            response_model="wrong-model",
            provider="custom",
        )
        assert snapshot_routed_model_capture() is None
    finally:
        reset_routed_model_capture(token)


@pytest.mark.parametrize(
    "response_model",
    [
        None,
        "",
        "   ",
        {},
        "x" * 241,
        "gpt-5.6\nspoof",
        "gpt-5.6\x00spoof",
        "gpt-5.6\u202espoof",
    ],
)
def test_capture_rejects_unsafe_response_model(response_model):
    token = begin_routed_model_capture(
        session_id="session-a",
        stream_id="stream-a",
        task_id="session-a",
        requested_model="auto",
        requested_provider="TokenTable",
    )
    try:
        _observe_post_api_request(
            platform="webui",
            session_id="session-a",
            task_id="session-a",
            response_model=response_model,
            provider="custom",
        )
        assert snapshot_routed_model_capture() is None
    finally:
        reset_routed_model_capture(token)


def test_contextvar_isolates_concurrent_capture_runs():
    barrier = Barrier(2)

    def capture(session_id, stream_id, model):
        token = begin_routed_model_capture(
            session_id=session_id,
            stream_id=stream_id,
            task_id=session_id,
            requested_model="auto",
            requested_provider="TokenTable",
        )
        try:
            barrier.wait(timeout=5)
            _observe_post_api_request(
                platform="webui",
                session_id=session_id,
                task_id=session_id,
                response_model=model,
                provider="custom",
            )
            return snapshot_routed_model_capture()
        finally:
            reset_routed_model_capture(token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: capture(*args),
                [
                    ("session-a", "stream-a", "model-a"),
                    ("session-b", "stream-b", "model-b"),
                ],
            )
        )

    assert [result["used_model"] for result in results] == ["model-a", "model-b"]


def test_provider_display_name_resolves_named_custom_provider():
    config = SimpleNamespace(custom_providers=[{"name": "TokenTable"}])

    assert provider_display_name("custom:tokentable", "custom", config) == "TokenTable"
    assert provider_display_name(None, "openai-codex", config) == "openai-codex"


def test_installer_registers_callback_only_once(monkeypatch):
    manager = SimpleNamespace(_hooks={})
    plugins = ModuleType("hermes_cli.plugins")
    plugins.discover_plugins = lambda: None
    plugins.get_plugin_manager = lambda: manager
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

    _install_post_api_request_observer()
    _install_post_api_request_observer()

    assert manager._hooks["post_api_request"] == [_observe_post_api_request]


def test_installer_registers_each_manager_and_restores_after_force_reload(monkeypatch):
    manager_a = SimpleNamespace(_hooks={})
    manager_b = SimpleNamespace(_hooks={})
    current = {"manager": manager_a}
    plugins = ModuleType("hermes_cli.plugins")
    plugins.discover_plugins = lambda: None
    plugins.get_plugin_manager = lambda: current["manager"]
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

    _install_post_api_request_observer()
    current["manager"] = manager_b
    _install_post_api_request_observer()

    assert manager_a._hooks["post_api_request"] == [_observe_post_api_request]
    assert manager_b._hooks["post_api_request"] == [_observe_post_api_request]

    manager_b._hooks.clear()
    _install_post_api_request_observer()
    _install_post_api_request_observer()

    assert manager_b._hooks["post_api_request"] == [_observe_post_api_request]


def test_capture_begin_and_reset_fail_open_when_plugin_discovery_fails(monkeypatch):
    plugins = ModuleType("hermes_cli.plugins")

    def fail_discovery():
        raise RuntimeError("plugin discovery unavailable")

    plugins.discover_plugins = fail_discovery
    plugins.get_plugin_manager = lambda: None
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    monkeypatch.setattr(
        observability,
        "_install_post_api_request_observer",
        _install_post_api_request_observer,
    )

    token = begin_routed_model_capture(
        session_id="session-a",
        stream_id="stream-a",
        task_id="session-a",
        requested_model="auto",
        requested_provider="TokenTable",
    )
    reset_routed_model_capture(token)

    assert snapshot_routed_model_capture() is None


def test_capture_begin_and_reset_fail_open_when_plugin_manager_is_unavailable(
    monkeypatch,
):
    plugins = ModuleType("hermes_cli.plugins")
    plugins.discover_plugins = lambda: None
    plugins.get_plugin_manager = lambda: None
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    monkeypatch.setattr(
        observability,
        "_install_post_api_request_observer",
        _install_post_api_request_observer,
    )

    token = begin_routed_model_capture(
        session_id="session-a",
        stream_id="stream-a",
        task_id="session-a",
        requested_model="auto",
        requested_provider="TokenTable",
    )
    reset_routed_model_capture(token)

    assert snapshot_routed_model_capture() is None
