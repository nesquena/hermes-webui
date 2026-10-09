"""
Test for Issue #6935: Ensure agent.run_conversation() does not trip a TypeError
when persist_user_timestamp is passed to agent implementations that do not accept it.
"""
from api.streaming import _supports_kwarg


class LegacyAgentStub:
    """Agent stub mimicking older hermes-agent whose run_conversation does NOT accept **kwargs or persist_user_timestamp."""
    def run_conversation(self, user_message, system_message=None, conversation_history=None, task_id=None, persist_user_message=None):
        return {"final_response": "ok", "messages": []}


class ModernAgentWithKwargsStub:
    """Agent stub whose run_conversation accepts **kwargs."""
    def run_conversation(self, **kwargs):
        return {"final_response": "ok", "messages": []}


class ModernAgentWithExplicitParamStub:
    """Agent stub whose run_conversation explicitly accepts persist_user_timestamp."""
    def run_conversation(self, user_message, persist_user_timestamp=None):
        return {"final_response": "ok", "messages": []}


def test_supports_kwarg_detection():
    legacy_agent = LegacyAgentStub()
    modern_kwargs_agent = ModernAgentWithKwargsStub()
    modern_explicit_agent = ModernAgentWithExplicitParamStub()

    assert _supports_kwarg(legacy_agent.run_conversation, "persist_user_timestamp") is False
    assert _supports_kwarg(modern_kwargs_agent.run_conversation, "persist_user_timestamp") is True
    assert _supports_kwarg(modern_explicit_agent.run_conversation, "persist_user_timestamp") is True


def test_legacy_agent_run_conversation_kwargs_filtering():
    legacy_agent = LegacyAgentStub()
    kwargs = {
        "user_message": "Hello",
        "system_message": "System prompt",
        "conversation_history": [],
        "task_id": "test-session-123",
        "persist_user_message": "Hello",
        "persist_user_timestamp": 1700000000.0,
    }

    if not _supports_kwarg(legacy_agent.run_conversation, "persist_user_timestamp"):
        kwargs.pop("persist_user_timestamp", None)

    # Calling run_conversation with filtered kwargs must not raise TypeError
    result = legacy_agent.run_conversation(**kwargs)
    assert result["final_response"] == "ok"


if __name__ == "__main__":
    test_supports_kwarg_detection()
    test_legacy_agent_run_conversation_kwargs_filtering()
    print("All tests passed successfully!")
