import logging

from api.streaming import (
    _normalize_prefill_messages_before_user_turn,
    _prefill_messages_with_webui_context,
    _webui_ephemeral_system_prompt,
)


def test_webui_ephemeral_prompt_includes_browser_surface_context():
    prompt = _webui_ephemeral_system_prompt(
        "Use a concise tone.",
        surface_context={
            "source": "webui",
            "session_id": "session-123",
            "profile": "default",
            "workspace": "/tmp/example-workspace",
        },
    )

    assert "Use a concise tone." in prompt
    assert "WebUI session context" in prompt
    assert "Source: webui" in prompt
    assert "Session ID: session-123" in prompt
    assert "Profile: default" in prompt
    assert "Workspace: /tmp/example-workspace" in prompt
    assert "not the same live transcript as Telegram" in prompt
    assert "Do not copy or dump this browser transcript" in prompt
    assert "Write to external notes or durable memory only" in prompt
    assert "otherwise leave notes unchanged" in prompt
    assert "what note/section changed" in prompt
    assert "explicit captures" in prompt
    assert "durable user preferences" in prompt
    assert "Do not include terse planning fragments" in prompt
    assert "Final visible assistant replies" in prompt
    assert "user-facing English" not in prompt
    assert "in the user's language" in prompt
    assert "Need script" in prompt
    assert "Need inspect email" in prompt
    assert "clear user-facing progress" in prompt


def test_webui_ephemeral_prompt_skips_empty_surface_fields():
    prompt = _webui_ephemeral_system_prompt(
        None,
        surface_context={
            "source": "webui",
            "session_id": "",
            "profile": None,
            "workspace": "   ",
        },
    )

    assert "WebUI session context" in prompt
    assert "Source: webui" in prompt
    assert "Session ID:" not in prompt
    assert "Profile:" not in prompt
    assert "Workspace:" not in prompt


def test_ephemeral_prompt_avoids_platform_info_when_no_config():
    """Without config_data, the delivery context falls back to defaults."""
    prompt = _webui_ephemeral_system_prompt(
        "Be concise.",
        surface_context={"source": "webui"},
    )

    # Core platform headings should still appear (fallback data).
    assert "Connected Platforms:" in prompt
    assert "Delivery options for scheduled tasks:" in prompt
    # But home channels are only present when the config has them.
    assert "Home Channels" not in prompt


def test_prefill_no_longer_adds_session_context_user_message():
    """_prefill_messages_with_webui_context must NOT append a user message.

    Strict chat templates (Mistral, Gemma) require user/assistant alternation.
    Adding a 'user' session context message creates two consecutive user turns.
    """
    prefill = {"messages": [{"role": "system", "content": "recall note"}]}
    result = _prefill_messages_with_webui_context(prefill)
    assert len(result) == 1
    assert result[0]["role"] == "system"
    assert "Connected Platforms" not in result[0].get("content", "")


def test_prefill_boundary_normalizer_removes_terminal_user_tail():
    """Trailing user messages are removed until the first non-user boundary."""

    raw = [
        {"role": "assistant", "content": "recall summary"},
        {"role": "user", "content": "session context"},
    ]

    assert _normalize_prefill_messages_before_user_turn(raw) == [
        {"role": "assistant", "content": "recall summary"},
    ]
    assert _normalize_prefill_messages_before_user_turn([
        {"role": "assistant", "content": "prefill"},
        {"role": "user", "content": "legacy user"},
        {"role": "assistant", "content": "assistant follow-up"},
    ]) == [
        {"role": "assistant", "content": "prefill"},
        {"role": "user", "content": "legacy user"},
        {"role": "assistant", "content": "assistant follow-up"},
    ]
    assert _normalize_prefill_messages_before_user_turn([
        {"role": "assistant", "content": "tail",},
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]) == [
        {"role": "assistant", "content": "tail",},
    ]
    assert _normalize_prefill_messages_before_user_turn([]) == []
    assert _normalize_prefill_messages_before_user_turn([{"role": "user", "content": "only user"}]) == []


def test_prefill_boundary_normalizer_logs_when_user_tail_dropped(caplog):
    """Dropping trailing user messages is logged with the count."""
    caplog.set_level(logging.DEBUG, logger="api.streaming")

    messages = [
        {"role": "assistant", "content": "prefill"},
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]

    assert _normalize_prefill_messages_before_user_turn(messages) == [
        {"role": "assistant", "content": "prefill"},
    ]
    assert any(
        rec.message == "Dropped 2 trailing user message(s) from prefill" for rec in caplog.records
    )


def test_prefill_boundary_normalizer_no_log_when_no_terminal_user(caplog):
    """No-op normalization must not emit the prefill-dropping debug log."""
    caplog.set_level(logging.DEBUG, logger="api.streaming")

    messages = [
        {"role": "assistant", "content": "turn 1"},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": "turn 3"},
    ]

    assert _normalize_prefill_messages_before_user_turn(messages) == messages
    assert len(caplog.records) == 0


def test_prefill_preserves_empty_and_none_messages():
    """Edge cases: empty prefill stays empty, missing key returns empty."""
    assert _prefill_messages_with_webui_context({"messages": []}) == []
    assert _prefill_messages_with_webui_context({}) == []
    assert _prefill_messages_with_webui_context({"messages": None}) == []


def test_delivery_context_includes_home_channels_when_configured():
    """When config_data has platforms with a home_channel, the prompt includes it."""
    config = {
        "platforms": {
            "telegram": {
                "enabled": True,
                "home_channel": {"name": "General"},
            },
        },
    }
    prompt = _webui_ephemeral_system_prompt(
        None,
        surface_context={"source": "webui"},
        config_data=config,
    )

    assert "Connected Platforms:" in prompt
    assert "Home Channels (default destinations):" in prompt
    assert "telegram: General" in prompt
    assert "telegram" in prompt and "Home channel" in prompt
