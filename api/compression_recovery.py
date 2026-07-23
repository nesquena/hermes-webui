"""Compression-exhausted recovery metadata and intent helpers."""

from __future__ import annotations

import re
import time
from typing import Any


COMPRESSION_RECOVERY_TERMINAL_STATE = "compression_exhausted"
COMPRESSION_RECOVERY_ACTION_START_FOCUSED = "start_focused_continuation"


_GENERIC_CONTINUATION_INTENTS = frozenset(
    {
        "continue",
        "continue please",
        "go on",
        "keep going",
        "resume",
        "proceed",
        "carry on",
        "继续",
        "继续吧",
        "接着",
        "接着做",
        "继续做",
        "继续执行",
    }
)


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _normalize_intent_text(text: str) -> str:
    raw = str(text or "").strip().lower()
    if not raw:
        return ""
    # Keep CJK letters while dropping punctuation/emoji/noise around short intents.
    return re.sub(r"[\W_]+", " ", raw, flags=re.UNICODE).strip()


def is_generic_continuation_intent(text: str) -> bool:
    """Return True only for short, content-free continuation requests."""

    normalized = _normalize_intent_text(text)
    if normalized in _GENERIC_CONTINUATION_INTENTS:
        return True
    # A single repeated word such as "continue continue" is still generic, but
    # longer prompts like "continue by summarizing file X" are substantive.
    parts = normalized.split()
    return bool(parts) and len(parts) <= 2 and all(part in _GENERIC_CONTINUATION_INTENTS for part in parts)


def build_compression_recovery_payload(session, *, message: str = "", details: str = "") -> dict:
    """Build the durable UI/route payload for a compression-exhausted turn."""

    source_sid = str(getattr(session, "session_id", "") or "")
    last_prompt_tokens = _positive_int(getattr(session, "last_prompt_tokens", None))
    threshold_tokens = _positive_int(getattr(session, "threshold_tokens", None))
    context_length = _positive_int(getattr(session, "context_length", None))
    payload = {
        "terminal_state": COMPRESSION_RECOVERY_TERMINAL_STATE,
        "recommended_action": COMPRESSION_RECOVERY_ACTION_START_FOCUSED,
        "source_session_id": source_sid,
        "created_at": time.time(),
        "title": "Context compression exhausted",
        "summary": (
            "This run could not safely shrink the conversation enough to continue in place. "
            "Start a focused continuation, then describe the next narrow task."
        ),
        "action_label": "Start focused continuation",
        "message": str(message or "").strip(),
        "details": str(details or "").strip()[:1200],
        "last_prompt_tokens": last_prompt_tokens,
        "threshold_tokens": threshold_tokens,
        "context_length": context_length,
    }
    return payload


def stamp_compression_exhausted_recovery(session, *, message: str = "", details: str = "") -> dict:
    """Persist recovery metadata on a session and return the message payload."""

    payload = build_compression_recovery_payload(session, message=message, details=details)
    session.recommended_recovery_action = payload["recommended_action"]
    session.compression_recovery = payload
    return payload


def compression_recovery_payload_for_session(session) -> dict | None:
    payload = getattr(session, "compression_recovery", None)
    if not isinstance(payload, dict):
        return None
    if payload.get("terminal_state") != COMPRESSION_RECOVERY_TERMINAL_STATE:
        return None
    action = str(payload.get("recommended_action") or getattr(session, "recommended_recovery_action", "") or "")
    if action != COMPRESSION_RECOVERY_ACTION_START_FOCUSED:
        return None
    return payload


def clear_compression_recovery(session) -> None:
    session.recommended_recovery_action = None
    session.compression_recovery = {}
