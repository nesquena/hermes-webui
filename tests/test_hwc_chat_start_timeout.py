"""hwc: slash ids on openrouter, custom, or an empty profile skip the catalog.

send() also waits 120s. A cold catalog can outlast the default client timeout.
An omitted profile argument is not an empty profile and stays on the catalog.
"""

from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _resolve_without_catalog(model: str, profile_provider: str):
    from api import routes

    def _boom(*_args, **_kwargs):
        raise AssertionError("live catalog must not run")

    with patch.object(routes, "get_available_models", _boom):
        return routes._resolve_compatible_session_model_state(
            model,
            None,
            profile_provider=profile_provider,
        )


def test_openrouter_slash_id_skips_catalog():
    effective, provider, changed = _resolve_without_catalog(
        "openai/gpt-6-luna",
        "openrouter",
    )
    assert effective == "openai/gpt-6-luna"
    assert provider == "openrouter"
    assert changed is False


def test_custom_slash_id_skips_catalog():
    effective, provider, changed = _resolve_without_catalog(
        "openai/gpt-6-luna",
        "custom",
    )
    assert effective == "openai/gpt-6-luna"
    assert provider == "custom"
    assert changed is False


def test_empty_profile_slash_id_skips_catalog():
    effective, provider, changed = _resolve_without_catalog(
        "x-ai/grok-4.7",
        "",
    )
    assert effective == "x-ai/grok-4.7"
    assert provider == ""
    assert changed is False


def test_chat_start_post_waits_120s():
    text = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    send_at = text.find("async function send(")
    assert send_at >= 0
    regen_at = text.find("async function startRegeneration(", send_at)
    assert regen_at > send_at
    send_body = text[send_at:regen_at]
    assert "timeoutMs:120000" in send_body
