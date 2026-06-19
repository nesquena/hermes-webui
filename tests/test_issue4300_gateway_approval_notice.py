"""Regression coverage for #4300 legacy gateway approval unsupported notice."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATEWAY_CHAT = (REPO / "api" / "gateway_chat.py").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_gateway_chat_has_approval_notice_emitted_attribute_check():
    """Verify _approval_notice_emitted attribute is checked before emitting."""
    assert "if not hasattr(s, \"_approval_notice_emitted\"):" in GATEWAY_CHAT
    assert "s._approval_notice_emitted = False" in GATEWAY_CHAT


def test_gateway_chat_emits_approval_gateway_unsupported_event():
    """Verify put_gateway_event is called with approval_gateway_unsupported type on non-terminal channel."""
    assert "put_gateway_event(\"warning\"" in GATEWAY_CHAT
    assert "\"type\": \"approval_gateway_unsupported\"" in GATEWAY_CHAT


def test_gateway_chat_once_per_session_guard_pattern():
    """Verify the once-per-session guard: capability check + hasattr + flag check + flag set."""
    assert "if not gateway_supports_approval(base_url, api_key):" in GATEWAY_CHAT
    assert "if not hasattr(s, \"_approval_notice_emitted\"):" in GATEWAY_CHAT
    assert "if not s._approval_notice_emitted:" in GATEWAY_CHAT
    assert "s._approval_notice_emitted = True" in GATEWAY_CHAT
    # Verify order: capability gate before session guard before flag set
    cap_pos = GATEWAY_CHAT.find("if not gateway_supports_approval(base_url, api_key):")
    hasattr_pos = GATEWAY_CHAT.find("if not hasattr(s, \"_approval_notice_emitted\"):")
    flag_check_pos = GATEWAY_CHAT.find("if not s._approval_notice_emitted:")
    flag_set_pos = GATEWAY_CHAT.find("s._approval_notice_emitted = True")
    assert cap_pos < hasattr_pos < flag_check_pos < flag_set_pos


def test_gateway_chat_event_payload_contains_type_and_message():
    """Verify the event payload has type and message fields."""
    assert "\"type\": \"approval_gateway_unsupported\"" in GATEWAY_CHAT
    assert "\"message\": \"Approvals require a newer gateway. Upgrade the connected Hermes gateway to enable this.\"" in GATEWAY_CHAT


def test_messages_js_handles_approval_gateway_unsupported_event():
    """Verify client-side warning handler recognizes the event type."""
    assert "d.type==='approval_gateway_unsupported'" in MESSAGES_JS


def test_messages_js_references_i18n_key_for_approval_gateway_unsupported():
    """Verify the i18n key is referenced in messages.js."""
    # The key should be used in the message handling logic
    assert "approval_gateway_unsupported" in MESSAGES_JS


def test_i18n_js_has_approval_gateway_unsupported_key():
    """Verify the i18n key exists in at least the English locale (first occurrence)."""
    assert "approval_gateway_unsupported: 'Approvals require a newer gateway" in I18N_JS
    lines = I18N_JS.split("\n")
    en_end = next(i for i, l in enumerate(lines) if l.strip().startswith("zh:"))
    found_in_english = any(
        "approval_gateway_unsupported:" in l
        for l in lines[:en_end]
    )
    assert found_in_english, "approval_gateway_unsupported key not found in English i18n locale"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
