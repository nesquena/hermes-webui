import queue
import sys
import types
from unittest import mock


def test_stream_completion_overwrites_session_usage_with_latest_turn(cleanup_test_sessions):
    """#1857: completed turns must not add prompt tokens to stale session totals."""
    import api.streaming as streaming

    saved_snapshots = []

    class FakeSession:
        def __init__(self):
            self.session_id = "issue1857_usage_overwrite"
            self.title = "Existing title"
            self.workspace = "/tmp"
            self.model = "gpt-5.4"
            self.model_provider = None
            self.profile = None
            self.personality = None
            self.messages = [
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": "old answer"},
            ]
            self.context_messages = list(self.messages)
            self.input_tokens = 9000
            self.output_tokens = 800
            self.estimated_cost = 12.34
            self.tool_calls = []
            self.gateway_routing = None
            self.gateway_routing_history = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.llm_title_generated = True

        def save(self, *args, **kwargs):
            saved_snapshots.append(
                {
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "estimated_cost": self.estimated_cost,
                    "kwargs": kwargs,
                }
            )

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

    class UsageAgent:
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
            self.session_id = session_id
            self.context_compressor = None
            self.session_prompt_tokens = 123
            self.session_completion_tokens = 45
            self.session_estimated_cost_usd = 0.067
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            return {
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "new answer"},
                ]
            }

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    fake_stream_id = "stream_issue1857_usage_overwrite"
    fake_queue = queue.Queue()
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = mock.Mock(
        return_value={
            "provider": "openai",
            "base_url": None,
            "api_key": "sk-test",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
        }
    )
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=None)

    with mock.patch.object(streaming, "get_session", return_value=fake_session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=UsageAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-5.4", "openai", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
         mock.patch.dict(
             sys.modules,
             {
                 "hermes_cli": fake_hermes_cli,
                 "hermes_cli.runtime_provider": fake_runtime_module,
                 "hermes_state": fake_hermes_state,
             },
         ):
        streaming.STREAMS[fake_stream_id] = fake_queue
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text="new turn",
            model="gpt-5.4",
            workspace="/tmp",
            stream_id=fake_stream_id,
        )

    assert fake_session.input_tokens == 123
    assert fake_session.output_tokens == 45
    assert fake_session.estimated_cost == 0.067
    assert any(
        event == "done"
        and payload["usage"]["input_tokens"] == 123
        and payload["usage"]["output_tokens"] == 45
        and payload["usage"]["estimated_cost"] == 0.067
        for event, payload in list(fake_queue.queue)
    )
    assert saved_snapshots[-1]["input_tokens"] == 123
    assert saved_snapshots[-1]["output_tokens"] == 45
    assert saved_snapshots[-1]["estimated_cost"] == 0.067
