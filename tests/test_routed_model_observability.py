"""Tests for run-scoped routed-model lifecycle capture."""

from concurrent.futures import ThreadPoolExecutor
import queue
import sys
from threading import Barrier
from types import ModuleType, SimpleNamespace

import pytest

import api.models as models
import api.routed_model_observability as observability
import api.streaming as streaming
from api.models import Session
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


def _run_streaming_routed_model_integration(
    monkeypatch,
    tmp_path,
    *,
    self_heal,
    ephemeral=False,
):
    suffix = "ephemeral-auth" if ephemeral else ("self-heal" if self_heal else "normal")
    session_id = f"session-routed-model-{suffix}"
    stream_id = f"stream-routed-model-{suffix}"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    session = Session(
        session_id=session_id,
        title="Routing",
        workspace=str(tmp_path),
        model="auto",
        model_provider="custom:tokentable",
        messages=[],
        context_messages=[],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "route this"
    session.save()

    class FakeAgent:
        instances = 0

        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            session_id=None,
            **kwargs,
        ):
            self.model = model
            self.provider = provider
            self.base_url = base_url
            self.session_id = session_id
            self.context_compressor = None
            self.session_prompt_tokens = 11
            self.session_completion_tokens = 7
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None
            self.fail_with_auth = (self_heal or ephemeral) and FakeAgent.instances == 0
            FakeAgent.instances += 1

        def run_conversation(self, **kwargs):
            if self.fail_with_auth:
                raise RuntimeError("401 unauthorized: invalid api key")
            observability._observe_post_api_request(
                platform="webui",
                session_id=self.session_id,
                task_id=kwargs["task_id"],
                response_model="gpt-5.6-sol",
                provider="custom",
            )
            return {
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "routed answer"},
                ]
            }

        def interrupt(self, _message):
            return None

    runtime_provider = ModuleType("hermes_cli.runtime_provider")
    runtime = {
        "provider": "custom",
        "base_url": "https://example.invalid",
        "api_key": "test-only",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }
    runtime_provider.resolve_runtime_provider = lambda **_kwargs: dict(runtime)
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.runtime_provider = runtime_provider
    hermes_state = ModuleType("hermes_state")
    hermes_state.SessionDB = lambda *_args, **_kwargs: object()
    config = {
        "custom_providers": [
            {
                "name": "TokenTable",
                "base_url": "https://example.invalid",
                "api_key": "test-only",
            }
        ]
    }
    journal_events = []

    class FakeRunJournal:
        def __init__(self, *_args, **_kwargs):
            pass

        def append_sse_event(self, event, data):
            journal_events.append((event, data))
            return None

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", runtime_provider)
    monkeypatch.setitem(sys.modules, "hermes_state", hermes_state)
    monkeypatch.setattr(streaming, "get_session", lambda _session_id: session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "RunJournalWriter", FakeRunJournal)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_args, **_kwargs: (
            "auto",
            "custom:tokentable",
            "https://example.invalid",
        ),
    )
    monkeypatch.setattr(
        streaming,
        "resolve_custom_provider_connection",
        lambda *_args, **_kwargs: ("test-only", "https://example.invalid"),
    )
    monkeypatch.setattr(streaming, "get_config", lambda: config)
    monkeypatch.setattr("api.config.get_config", lambda: config)
    monkeypatch.setattr("api.config.get_config_for_profile_home", lambda *_args: config)
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        streaming,
        "_attempt_credential_self_heal",
        (
            (lambda *_args, **_kwargs: dict(runtime))
            if self_heal
            else (lambda *_args, **_kwargs: None)
        ),
    )
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue

    streaming._run_agent_streaming(
        session_id=session_id,
        msg_text="route this",
        model="auto",
        workspace=tmp_path,
        stream_id=stream_id,
        model_provider="custom:tokentable",
        ephemeral=ephemeral,
    )

    expected = {
        "requested_model": "auto",
        "requested_provider": "TokenTable",
        "used_model": "gpt-5.6-sol",
        "used_provider": "TokenTable",
        "source": "openai-compatible-sse",
        "provider_changed": False,
        "model_changed": True,
        "has_failover": False,
    }
    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    if ephemeral:
        assert not [payload for event, payload in events if event == "done"]
        errors = [payload for event, payload in events if event == "apperror"]
        assert errors[-1]["type"] == "auth_mismatch"
        assert snapshot_routed_model_capture() is None
        return
    done = [payload for event, payload in events if event == "done"][-1]
    journal_done = [payload for event, payload in journal_events if event == "done"][-1]

    assert session.messages[-1]["_gatewayRouting"] == expected
    assert session.gateway_routing == expected
    assert session.gateway_routing_history == [expected]
    assert done["usage"]["gateway_routing"] == expected
    assert done["session"]["messages"][-1]["_gatewayRouting"] == expected
    assert journal_done["usage"]["gateway_routing"] == expected
    assert journal_done["session"]["messages"][-1]["_gatewayRouting"] == expected
    reloaded = Session.load(session_id)
    assert reloaded.messages[-1]["_gatewayRouting"] == expected
    assert reloaded.gateway_routing == expected
    assert reloaded.gateway_routing_history == [expected]
    assert reloaded.messages[-1]["_gatewayRouting"]["source"] == "openai-compatible-sse"
    assert all(
        "_gatewayRouting" not in message
        for message in streaming._sanitize_messages_for_api(session.messages)
    )
    assert snapshot_routed_model_capture() is None


def test_streaming_persists_sse_routed_model_in_message_session_and_done(
    monkeypatch,
    tmp_path,
):
    _run_streaming_routed_model_integration(
        monkeypatch,
        tmp_path,
        self_heal=False,
    )


def test_streaming_self_heal_retry_persists_routed_model_through_common_finalization(
    monkeypatch,
    tmp_path,
):
    _run_streaming_routed_model_integration(
        monkeypatch,
        tmp_path,
        self_heal=True,
    )


def test_ephemeral_streaming_auth_exception_uses_outer_error_semantics(
    monkeypatch,
    tmp_path,
):
    _run_streaming_routed_model_integration(
        monkeypatch,
        tmp_path,
        self_heal=False,
        ephemeral=True,
    )
