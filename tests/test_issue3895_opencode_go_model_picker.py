"""Regression tests for OpenCode-Go model-picker runtime routing (#3895)."""

import pathlib
import queue
import sys
import types
from unittest import mock

import api.oauth
import api.streaming as streaming


def test_runtime_preferred_base_url_uses_runtime_value_for_pooled_provider():
    assert streaming._runtime_preferred_base_url(
        {"provider": "opencode-go", "base_url": "https://opencode.example.com/api"},
        "opencode-go",
        "https://opencode.example.com/api/v1",
    ) == "https://opencode.example.com/api"


def test_runtime_preferred_base_url_keeps_custom_config_base_url():
    assert streaming._runtime_preferred_base_url(
        {"provider": "custom:opencode-proxy", "base_url": "https://runtime.example.com"},
        "custom:opencode-proxy",
        "https://config.example.com/v1",
    ) == "https://config.example.com/v1"


def test_streaming_passes_target_model_and_prefers_runtime_base_url(monkeypatch):
    captured = {}

    class FakeSession:
        def __init__(self):
            self.session_id = "sess-3895"
            self.title = "OpenCode test"
            self.workspace = "/tmp"
            self.model = "glm-5.1"
            self.messages = []
            self.personality = None
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = None
            self.tool_calls = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None

        def save(self, touch_updated_at=True):
            self._saved = touch_updated_at

        def compact(self):
            return {
                "session_id": self.session_id,
                "title": self.title,
                "workspace": self.workspace,
                "model": self.model,
                "created_at": 0,
                "updated_at": 0,
                "pinned": False,
                "archived": False,
                "project_id": None,
                "profile": None,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "estimated_cost": self.estimated_cost,
                "personality": self.personality,
            }

    class CapturingAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            **kwargs,
        ):
            captured["init_kwargs"] = {
                "model": model,
                "provider": provider,
                "base_url": base_url,
                "api_key": api_key,
                "session_id": session_id,
                "session_db": session_db,
            }
            self.session_id = session_id
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            captured["run_kwargs"] = kwargs
            return {
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "ok"},
                ]
            }

        def interrupt(self, _message):
            captured["interrupted"] = _message

    fake_session = FakeSession()
    fake_stream_id = "stream-3895"
    fake_session.active_stream_id = fake_stream_id
    fake_queue = queue.Queue()
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    resolve_runtime_provider = mock.Mock(
        return_value={
            "provider": "opencode-go",
            "base_url": "https://opencode.example.com/api",
            "api_key": "rt-key",
        }
    )
    fake_runtime_module.resolve_runtime_provider = resolve_runtime_provider
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=object())

    def fake_runtime_lock(resolver, **kwargs):
        return resolver(**kwargs)

    monkeypatch.setattr(
        api.oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        fake_runtime_lock,
    )
    monkeypatch.setattr(streaming, "get_session", lambda _session_id: fake_session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: CapturingAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_args, **_kwargs: (
            "glm-5.1",
            "opencode-go",
            "https://opencode.example.com/api/v1",
        ),
    )
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    streaming.STREAMS[fake_stream_id] = fake_queue
    streaming._run_agent_streaming(
        session_id=fake_session.session_id,
        msg_text="hello from picker",
        model="glm-5.1",
        workspace="/tmp",
        stream_id=fake_stream_id,
    )

    resolve_runtime_provider.assert_called_once_with(
        requested="opencode-go",
        target_model="glm-5.1",
    )
    assert captured["init_kwargs"]["provider"] == "opencode-go"
    assert captured["init_kwargs"]["base_url"] == "https://opencode.example.com/api"
    assert captured["init_kwargs"]["api_key"] == "rt-key"


def test_streaming_reuses_runtime_base_url_helper_in_self_heal_paths():
    source = (pathlib.Path(__file__).parent.parent / "api" / "streaming.py").read_text(
        encoding="utf-8"
    )
    assert source.count("_runtime_preferred_base_url(") == 4

