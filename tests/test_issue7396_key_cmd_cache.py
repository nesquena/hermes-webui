import hashlib
import queue
import sys
import types
from unittest import mock

from api import config


_MISSING = object()


class CommandTokenSource:
    """Minimal stand-in for hermes-agent's callable key_cmd credential."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return "minted-token"


def test_callable_api_key_signature_is_stable_secret_free_and_lazy():
    """#7396: cache identity must not resolve or stringify dynamic credentials."""
    import api.streaming as streaming

    source = CommandTokenSource()

    first = streaming._agent_cache_api_key_sig(source, None)
    second = streaming._agent_cache_api_key_sig(source, None)

    assert first == second == "dynamic-credential"
    assert source.calls == 0
    assert streaming._agent_cache_api_key_sig("token-a", None) == hashlib.sha256(
        b"token-a"
    ).hexdigest()[:16]
    assert streaming._agent_cache_api_key_sig("token-b", object()) == "credential-pool"


def test_key_cmd_runtime_reaches_agent_construction_without_resolving_token():
    """A non-ephemeral chat worker must construct AIAgent with callable api_key."""
    import api.streaming as streaming

    captured = {}
    source = CommandTokenSource()

    class FakeSession:
        session_id = "issue7396_key_cmd"
        title = "key_cmd regression"
        workspace = "/tmp"
        model = "gpt-4o"
        model_provider = None
        profile = None
        personality = None
        messages = []
        context_messages = []
        input_tokens = 0
        output_tokens = 0
        estimated_cost = None
        cache_read_tokens = 0
        cache_write_tokens = 0
        tool_calls = []
        gateway_routing = None
        gateway_routing_history = []
        active_stream_id = None
        pending_user_message = None
        pending_attachments = []
        pending_started_at = None
        context_length = 0
        threshold_tokens = 0
        last_prompt_tokens = 0
        llm_title_generated = True

        def save(self, *args, **kwargs):
            return None

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
        ):
            captured["api_key"] = api_key
            captured["session_id"] = session_id
            captured["api_key_calls_at_init"] = getattr(api_key, "calls", None)
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
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

    session = FakeSession()
    stream_id = "stream_issue7396_key_cmd"
    session.active_stream_id = stream_id
    event_queue = queue.Queue()

    runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    runtime_module.resolve_runtime_provider = mock.Mock(
        return_value={
            "provider": "my-gateway",
            "base_url": "https://gateway.example.invalid/v1",
            "api_key": source,
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
        }
    )
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.runtime_provider = runtime_module
    hermes_state = types.ModuleType("hermes_state")
    hermes_state.SessionDB = mock.Mock(return_value=None)
    injected = {
        "hermes_cli": hermes_cli,
        "hermes_cli.runtime_provider": runtime_module,
        "hermes_state": hermes_state,
    }
    saved_modules = {name: sys.modules.get(name, _MISSING) for name in injected}
    sys.modules.update(injected)

    try:
        with mock.patch.object(streaming, "get_session", return_value=session), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=CapturingAgent), \
             mock.patch.object(
                 streaming,
                 "resolve_model_provider",
                 return_value=("gpt-4o", "my-gateway", None),
             ), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            streaming.STREAMS[stream_id] = event_queue
            streaming._run_agent_streaming(
                session_id=session.session_id,
                msg_text="hello",
                model="gpt-4o",
                workspace="/tmp",
                stream_id=stream_id,
            )
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        with config.SESSION_AGENT_CACHE_LOCK:
            config.SESSION_AGENT_CACHE.pop(session.session_id, None)
        for name, previous in saved_modules.items():
            if previous is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    assert captured == {
        "api_key": source,
        "session_id": session.session_id,
        "api_key_calls_at_init": 0,
    }
    # The current agent runtime may resolve the key later for request-time
    # capability checks; this regression only covers pre-construction cache identity.
    assert any(event == "done" for event, _payload in list(event_queue.queue))
