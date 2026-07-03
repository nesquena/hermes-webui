"""Tests for provider-aware reasoning_content history replay stripping.

Historical assistant reasoning_content is preserved by default. It is stripped
only when the user explicitly chooses strip mode, or when auto mode can identify
a local/generic effective backend for the current session/request.
"""
import copy
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

from api.streaming import _sanitize_messages_for_api


def _asst_with_reasoning(content="hello", reasoning="I think about this..."):
    """Create an assistant message with both content and reasoning_content."""
    return {
        "role": "assistant",
        "content": content,
        "reasoning_content": reasoning,
        "_ts": 12345,
    }


def _asst_with_tool_calls_and_reasoning(reasoning="Let me call a tool..."):
    """Create an assistant message with tool_calls AND reasoning_content."""
    return {
        "role": "assistant",
        "content": None,
        "reasoning_content": reasoning,
        "tool_calls": [
            {"type": "function", "id": "call-1", "function": {"name": "search", "arguments": "{}"}}
        ],
    }


def _tool_result(call_id="call-1"):
    return {"role": "tool", "tool_call_id": call_id, "content": "ok"}


def _user(text="hello"):
    return {"role": "user", "content": text}


def _assistant(result):
    return [m for m in result if m["role"] == "assistant"][0]


def _auto_cfg(provider="openai", default="gpt-4o", **model_overrides):
    model = {"provider": provider, "default": default}
    model.update(model_overrides)
    return {"model": model, "webui": {"reasoning_content_replay": "auto"}}


def _assert_reasoning_preserved(result, expected):
    assert _assistant(result).get("reasoning_content") == expected


def _assert_reasoning_stripped(result):
    assert "reasoning_content" not in _assistant(result)


def test_cfg_none_preserves_reasoning_content():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Internal thought")]

    result = _sanitize_messages_for_api(msgs, cfg=None)

    _assert_reasoning_preserved(result, "Internal thought")


def test_empty_cfg_preserves_reasoning_content():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Internal thought")]

    result = _sanitize_messages_for_api(msgs, cfg={})

    _assert_reasoning_preserved(result, "Internal thought")


def test_empty_webui_cfg_preserves_reasoning_content():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Internal thought")]
    cfg = {"webui": {}, "model": {"provider": "ollama", "default": "qwen3:latest"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    _assert_reasoning_preserved(result, "Internal thought")


def test_non_dict_webui_cfg_preserves_reasoning_content():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Internal thought")]
    cfg = {"webui": "not-a-dict", "model": {"provider": "ollama", "default": "qwen3:latest"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    _assert_reasoning_preserved(result, "Internal thought")


def test_unknown_mode_preserves_reasoning_content():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Internal thought")]
    cfg = {"webui": {"reasoning_content_replay": "foobar"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    _assert_reasoning_preserved(result, "Internal thought")


def test_strip_mode_removes_reasoning_content():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Internal thought")]
    cfg = {"webui": {"reasoning_content_replay": "strip"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    assert len(result) == 2
    assistant_msg = _assistant(result)
    assert assistant_msg["content"] == "A1"
    assert "reasoning_content" not in assistant_msg


def test_preserve_mode_keeps_reasoning_content():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Internal thought")]
    cfg = {"webui": {"reasoning_content_replay": "preserve"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    _assert_reasoning_preserved(result, "Internal thought")


def test_auto_with_profile_openai_effective_lmstudio_strips():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "LM Studio stale thought")]
    cfg = _auto_cfg(provider="openai", default="gpt-4o")

    result = _sanitize_messages_for_api(
        msgs,
        cfg=cfg,
        effective_model="qwen3-235b-a22b",
        effective_provider="lmstudio",
    )

    _assert_reasoning_stripped(result)


def test_auto_with_profile_openai_effective_ollama_strips():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Ollama stale thought")]
    cfg = _auto_cfg(provider="openai", default="gpt-4o")

    result = _sanitize_messages_for_api(
        msgs,
        cfg=cfg,
        effective_model="qwen3:latest",
        effective_provider="ollama",
    )

    _assert_reasoning_stripped(result)


def test_auto_with_profile_openai_effective_llamacpp_strips():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "llama.cpp stale thought")]
    cfg = _auto_cfg(provider="openai", default="gpt-4o")

    result = _sanitize_messages_for_api(
        msgs,
        cfg=cfg,
        effective_model="qwen3-235b-a22b",
        effective_provider="llamacpp",
    )

    _assert_reasoning_stripped(result)


def test_auto_with_custom_without_local_base_url_preserves():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Custom cloud thought")]
    cfg = _auto_cfg(provider="openai", default="gpt-4o")

    result = _sanitize_messages_for_api(
        msgs,
        cfg=cfg,
        effective_model="custom-reasoner",
        effective_provider="custom",
    )

    _assert_reasoning_preserved(result, "Custom cloud thought")


def test_auto_with_custom_local_base_url_strips():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Custom local stale thought")]
    cfg = _auto_cfg(provider="openai", default="gpt-4o")

    result = _sanitize_messages_for_api(
        msgs,
        cfg=cfg,
        effective_model="qwen3-235b-a22b",
        effective_provider="custom",
        effective_base_url="http://127.0.0.1:1234/v1",
    )

    _assert_reasoning_stripped(result)


def test_auto_with_anthropic_claude_effective_preserves():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Claude thinking")]
    cfg = _auto_cfg(provider="openai", default="gpt-4o")

    result = _sanitize_messages_for_api(
        msgs,
        cfg=cfg,
        effective_model="claude-sonnet-4.6",
        effective_provider="anthropic",
    )

    _assert_reasoning_preserved(result, "Claude thinking")


def test_auto_with_deepseek_effective_preserves():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "DeepSeek thinking")]
    cfg = _auto_cfg(provider="openai", default="gpt-4o")

    result = _sanitize_messages_for_api(
        msgs,
        cfg=cfg,
        effective_model="deepseek-v4-flash",
        effective_provider="deepseek",
    )

    _assert_reasoning_preserved(result, "DeepSeek thinking")


def test_auto_with_openai_gpt5_effective_preserves():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "GPT-5 reasoning")]
    cfg = _auto_cfg(provider="ollama", default="qwen3:latest")

    result = _sanitize_messages_for_api(
        msgs,
        cfg=cfg,
        effective_model="gpt-5-mini",
        effective_provider="openai",
    )

    _assert_reasoning_preserved(result, "GPT-5 reasoning")


def test_auto_with_openai_o_series_effective_preserves():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "o1 reasoning")]
    cfg = _auto_cfg(provider="ollama", default="qwen3:latest")

    result = _sanitize_messages_for_api(
        msgs,
        cfg=cfg,
        effective_model="o1-preview",
        effective_provider="openai",
    )

    _assert_reasoning_preserved(result, "o1 reasoning")


def test_auto_with_openai_gpt4o_effective_preserves():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "gpt-4o thought")]
    cfg = _auto_cfg(provider="openai", default="gpt-4o")

    result = _sanitize_messages_for_api(
        msgs,
        cfg=cfg,
        effective_model="gpt-4o",
        effective_provider="openai",
    )

    _assert_reasoning_preserved(result, "gpt-4o thought")


def test_auto_with_unknown_effective_provider_preserves():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Unknown provider thought")]
    cfg = _auto_cfg(provider="openai", default="gpt-4o")

    result = _sanitize_messages_for_api(
        msgs,
        cfg=cfg,
        effective_model="mystery-model",
        effective_provider="unknown-cloud",
    )

    _assert_reasoning_preserved(result, "Unknown provider thought")


def test_auto_without_effective_provider_uses_production_profile_keys_for_local_fallback():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Profile local stale thought")]
    cfg = _auto_cfg(provider="ollama", default="qwen3:latest")

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    _assert_reasoning_stripped(result)


def test_auto_without_effective_provider_preserves_openai_gpt4o_profile():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "OpenAI non-reasoning thought")]
    cfg = _auto_cfg(provider="openai", default="gpt-4o")

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    _assert_reasoning_preserved(result, "OpenAI non-reasoning thought")


def test_auto_profile_model_name_fallback_preserves_anthropic_claude():
    msgs = [_user("Q"), _asst_with_reasoning("A1", "Claude profile thinking")]
    cfg = {
        "model": {"provider": "anthropic", "name": "claude-sonnet-4"},
        "webui": {"reasoning_content_replay": "auto"},
    }

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    _assert_reasoning_preserved(result, "Claude profile thinking")


def test_input_messages_not_mutated():
    original_messages = [_user("Q"), _asst_with_reasoning("A1", "Internal thought")]
    msgs = copy.deepcopy(original_messages)
    cfg = {"webui": {"reasoning_content_replay": "strip"}}

    _sanitize_messages_for_api(msgs, cfg=cfg)

    assert msgs == original_messages


def test_multiple_assistant_reasoning_all_stripped():
    msgs = [
        _user("Q1"),
        _asst_with_reasoning("A1", "Thought 1"),
        _user("Q2"),
        _asst_with_reasoning("A2", "Thought 2"),
    ]
    cfg = {"webui": {"reasoning_content_replay": "strip"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    for msg in result:
        if msg["role"] == "assistant":
            assert "reasoning_content" not in msg


def test_tool_call_chain_preserved_with_reasoning_stripped():
    msgs = [
        _user("Q"),
        _asst_with_tool_calls_and_reasoning("Let me search..."),
        _tool_result("call-1"),
        _user("A2"),
    ]
    cfg = {"webui": {"reasoning_content_replay": "strip"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    roles = [m["role"] for m in result]
    assert roles == ["user", "assistant", "tool", "user"]
    asst_msg = _assistant(result)
    assert asst_msg.get("tool_calls") is not None
    assert len(asst_msg["tool_calls"]) == 1
    assert asst_msg["tool_calls"][0]["id"] == "call-1"
    assert "reasoning_content" not in asst_msg
    tool_ids = {m["tool_call_id"] for m in result if m["role"] == "tool"}
    assistant_tool_ids = {tc["id"] for tc in asst_msg["tool_calls"]}
    assert tool_ids == assistant_tool_ids


def test_orphaned_tool_messages_still_dropped_with_reasoning_strip():
    msgs = [
        _user("Q"),
        _asst_with_tool_calls_and_reasoning(),
        {"role": "tool", "tool_call_id": "call-orphan", "content": "orphan"},
        _user("A2"),
    ]
    cfg = {"webui": {"reasoning_content_replay": "strip"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    roles = [m["role"] for m in result]
    assert "tool" not in roles
    assert "assistant" not in roles


def test_oob_terminal_marker_behavior_unchanged_with_reasoning_strip():
    msgs = [
        _user("Q"),
        {
            "role": "assistant",
            "content": "Here is the answer. [[hermes:agent:terminal]]\n\nDone.",
            "reasoning_content": "I should not leak this.",
        },
    ]
    cfg = {"webui": {"reasoning_content_replay": "strip"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    asst_msg = _assistant(result)
    assert asst_msg.get("content") == "Here is the answer. [[hermes:agent:terminal]]\n\nDone."
    assert "reasoning_content" not in asst_msg


def test_empty_messages_list():
    cfg = {"webui": {"reasoning_content_replay": "strip"}}

    assert _sanitize_messages_for_api([], cfg=cfg) == []


def test_assistant_without_reasoning_unchanged():
    msgs = [_user("Q"), {"role": "assistant", "content": "A1"}]
    cfg = {"webui": {"reasoning_content_replay": "strip"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    assert len(result) == 2
    assert _assistant(result)["content"] == "A1"


def test_reasoning_only_assistant_message_not_duplicated():
    msgs = [
        _user("Q"),
        {"role": "assistant", "content": "", "reasoning_content": "Thinking..."},
        _user("A2"),
    ]
    cfg = {"webui": {"reasoning_content_replay": "preserve"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    roles = [m["role"] for m in result]
    assert roles == ["user", "user"]


def test_system_messages_unaffected():
    msgs = [
        {"role": "system", "content": "You are helpful."},
        _user("Q"),
        _asst_with_reasoning("A1", "Internal thought"),
    ]
    cfg = {"webui": {"reasoning_content_replay": "strip"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    assert len(result) == 3
    assert result[0]["role"] == "system"
    assert result[0]["content"] == "You are helpful."


def test_multiple_turns_with_mixed_reasoning():
    msgs = [
        _user("Q1"),
        {"role": "assistant", "content": "A1", "reasoning_content": "Thought 1"},
        _user("Q2"),
        {"role": "assistant", "content": "A2"},
        _user("Q3"),
        {"role": "assistant", "content": "A3", "reasoning_content": "Thought 3"},
    ]
    cfg = {"webui": {"reasoning_content_replay": "strip"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    assert len(result) == 6
    for msg in result:
        if msg["role"] == "assistant":
            assert "reasoning_content" not in msg
        if msg.get("content") and msg["role"] != "system":
            assert msg["content"] is not None


def test_reasoning_content_on_user_message_untouched():
    msgs = [
        _user("Q"),
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2", "reasoning_content": "unexpected field"},
    ]
    cfg = {"webui": {"reasoning_content_replay": "strip"}}

    result = _sanitize_messages_for_api(msgs, cfg=cfg)

    user_msg = [m for m in result if m["role"] == "user" and m.get("reasoning_content")][0]
    assert user_msg.get("reasoning_content") == "unexpected field"
