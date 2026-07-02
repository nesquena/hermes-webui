"""Regression tests for wakeup model picker races (#4251)."""

import queue
import sys
import types
from unittest import mock

import api.oauth
import api.streaming as streaming


class FakeSession:
    def __init__(self, *, model, model_provider="anthropic"):
        self.session_id = "sess-4251"
        self.title = "Wakeup picker race"
        self.workspace = "/tmp"
        self.model = model
        self.model_provider = model_provider
        self.messages = []
        self.personality = None
        self.profile = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = None
        self.tool_calls = []
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None

    def save(self, touch_updated_at=True, **kwargs):
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
            "profile": self.profile,
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
        self.init_kwargs = {
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
        return {
            "messages": [
                {"role": "user", "content": kwargs["persist_user_message"]},
                {"role": "assistant", "content": "ok"},
            ]
        }

    def interrupt(self, _message):
        return None


def _install_streaming_harness(monkeypatch, fake_session):
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = mock.Mock(
        return_value={
            "provider": "anthropic",
            "base_url": None,
            "api_key": "rt-key",
        }
    )
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
        lambda *_args, **_kwargs: ("haiku-4-5", "anthropic", None),
    )
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)


def _run_streaming_turn(
    monkeypatch,
    fake_session,
    *,
    stream_id,
    dispatch_model="haiku-4-5",
    dispatch_provider="anthropic",
):
    _install_streaming_harness(monkeypatch, fake_session)
    fake_session.active_stream_id = stream_id
    fake_queue = queue.Queue()
    try:
        streaming.STREAMS[stream_id] = fake_queue
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text="background wakeup turn",
            model=dispatch_model,
            model_provider=dispatch_provider,
            workspace="/tmp",
            stream_id=stream_id,
        )
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)


def test_dispatch_stamp_does_not_clobber_newer_picker_model(monkeypatch):
    fake_session = FakeSession(model="opus-4-8", model_provider="openrouter")

    _run_streaming_turn(
        monkeypatch,
        fake_session,
        stream_id="stream-4251-race",
    )

    assert fake_session.model == "opus-4-8"
    assert fake_session.model_provider == "openrouter"


def test_dispatch_stamp_does_not_clobber_newer_picker_provider_only_choice(monkeypatch):
    fake_session = FakeSession(model="haiku-4-5", model_provider="openrouter")

    _run_streaming_turn(
        monkeypatch,
        fake_session,
        stream_id="stream-4251-provider-race",
    )

    assert fake_session.model == "haiku-4-5"
    assert fake_session.model_provider == "openrouter"


def test_dispatch_stamp_persists_resolved_model_when_no_race(monkeypatch):
    fake_session = FakeSession(model="haiku-4-5", model_provider=None)

    _run_streaming_turn(
        monkeypatch,
        fake_session,
        stream_id="stream-4251-steady",
    )

    assert fake_session.model == "haiku-4-5"
    assert fake_session.model_provider == "anthropic"


def test_dispatch_stamp_persists_when_session_model_was_empty(monkeypatch):
    fake_session = FakeSession(model="", model_provider=None)

    _run_streaming_turn(
        monkeypatch,
        fake_session,
        stream_id="stream-4251-empty",
    )

    assert fake_session.model == "haiku-4-5"
    assert fake_session.model_provider == "anthropic"


def test_profile_repair_skips_persistence_when_newer_picker_choice_already_won(monkeypatch):
    fake_session = FakeSession(model="opus-4-8", model_provider="openrouter")
    fake_session.profile = "worker-profile"
    monkeypatch.setattr(
        streaming,
        "_apply_profile_home_context_to_streaming_model",
        lambda **_kwargs: ("claude-profile-default", "anthropic", True),
    )

    _run_streaming_turn(
        monkeypatch,
        fake_session,
        stream_id="stream-4251-profile-race",
    )

    assert fake_session.model == "opus-4-8"
    assert fake_session.model_provider == "openrouter"


def test_dispatch_stamp_snapshots_provider_under_agent_lock(monkeypatch):
    fake_session = FakeSession(model="haiku-4-5", model_provider="anthropic")

    class PickerUpdateLock:
        def __init__(self):
            self._entered = 0

        def __enter__(self):
            self._entered += 1
            if self._entered == 1:
                fake_session.model_provider = "openrouter"
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _session_id: PickerUpdateLock())

    _run_streaming_turn(
        monkeypatch,
        fake_session,
        stream_id="stream-4251-lock-race",
    )

    assert fake_session.model == "haiku-4-5"
    assert fake_session.model_provider == "openrouter"
