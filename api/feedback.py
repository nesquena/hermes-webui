"""Persist lightweight thumbs feedback from the WebUI action bar.

Backend-only for now: there is no Svelte/vanilla client caller wired yet.
Writes append-only JSONL under the active profile's webui_state dir.

Fail closed on bad input; never raise into the request handler for I/O errors
beyond logging + a 500 response at the route layer.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FEEDBACK_LOCK = threading.Lock()
_RATE_LOCK = threading.Lock()
_RATE_HITS: dict[str, list[float]] = {}

ALLOWED_RATINGS = frozenset({"up", "down"})
ALLOWED_REASONS = frozenset({"inaccurate", "not_helpful", "too_long", "harmful"})
_MAX_SESSION_ID_LEN = 128
_MAX_MESSAGE_ID_LEN = 256
_MAX_MODEL_LEN = 256
_MAX_MODE_LEN = 64
_MAX_PROFILE_LEN = 64
_MAX_FEEDBACK_FILE_BYTES = 5 * 1024 * 1024  # 5 MiB hard cap
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_RATE_LIMIT_MAX = 30  # per session_id per window


class FeedbackValidationError(ValueError):
    """Raised when a feedback payload fails validation (fail closed)."""


def _profile_state_dir() -> Path:
    """Resolve webui_state for the active profile (per-call, not module import)."""
    from api.workspace import _profile_state_dir as _ws_profile_state_dir

    return _ws_profile_state_dir()


def feedback_path() -> Path:
    return _profile_state_dir() / "feedback.jsonl"


def _active_profile_name() -> str:
    try:
        from api.profiles import get_active_profile_name

        name = get_active_profile_name()
        if isinstance(name, str) and name.strip():
            return name.strip()[:_MAX_PROFILE_LEN]
    except Exception:
        logger.debug("feedback: could not resolve active profile", exc_info=True)
    return "default"


def _nonempty_str(value: Any, *, field: str, max_len: int) -> str:
    if value is None:
        raise FeedbackValidationError(f"{field} is required")
    if not isinstance(value, str):
        raise FeedbackValidationError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise FeedbackValidationError(f"{field} is required")
    if len(text) > max_len:
        raise FeedbackValidationError(f"{field} is too long")
    return text


def _optional_str(value: Any, *, field: str, max_len: int) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise FeedbackValidationError(f"{field} must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > max_len:
        raise FeedbackValidationError(f"{field} is too long")
    return text


def _assert_session_exists(session_id: str) -> None:
    from api.models import get_session, is_safe_session_id

    if not is_safe_session_id(session_id):
        raise FeedbackValidationError("session_id is invalid")
    try:
        get_session(session_id, metadata_only=True)
    except KeyError:
        raise FeedbackValidationError("session not found") from None
    except Exception as exc:
        # Fail closed: unknown session store errors are not "allowed".
        raise FeedbackValidationError("session not found") from exc


def _assert_message_target(
    session_id: str,
    message_id: str | None,
    message_index: int | None,
) -> None:
    from api.models import get_session

    session = get_session(session_id, metadata_only=False)
    messages = list(getattr(session, "messages", None) or [])
    if message_index is not None:
        if message_index >= len(messages):
            raise FeedbackValidationError("index out of range")
    if message_id is not None:
        found = False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            identity = msg.get("id") or msg.get("message_id")
            if identity is not None and str(identity) == message_id:
                found = True
                break
        if not found:
            raise FeedbackValidationError("message_id not found in session")


def normalize_feedback_payload(body: Any, *, validate_session: bool = True) -> dict:
    """Validate and normalize a POST /api/feedback body. Fail closed."""
    if not isinstance(body, dict):
        raise FeedbackValidationError("body must be a JSON object")

    session_id = _nonempty_str(body.get("session_id"), field="session_id", max_len=_MAX_SESSION_ID_LEN)
    if validate_session:
        _assert_session_exists(session_id)

    rating_raw = body.get("rating")
    if not isinstance(rating_raw, str):
        raise FeedbackValidationError("rating must be 'up' or 'down'")
    rating = rating_raw.strip().lower()
    if rating not in ALLOWED_RATINGS:
        raise FeedbackValidationError("rating must be 'up' or 'down'")

    message_id = _optional_str(body.get("message_id"), field="message_id", max_len=_MAX_MESSAGE_ID_LEN)
    message_index = body.get("index", body.get("message_index"))
    if message_index is not None:
        if isinstance(message_index, bool) or not isinstance(message_index, int):
            raise FeedbackValidationError("index must be an integer")
        if message_index < 0:
            raise FeedbackValidationError("index must be >= 0")

    if message_id is None and message_index is None:
        raise FeedbackValidationError("message_id or index is required")
    if validate_session:
        _assert_message_target(session_id, message_id, message_index)

    reason = _optional_str(body.get("reason"), field="reason", max_len=64)
    if reason is not None:
        reason = reason.lower().replace(" ", "_").replace("-", "_")
        if reason not in ALLOWED_REASONS:
            raise FeedbackValidationError(
                "reason must be one of: inaccurate, not_helpful, too_long, harmful"
            )
    if rating == "up" and reason is not None:
        # Upvotes don't carry reason chips; ignore silently rather than 400.
        reason = None

    model = _optional_str(body.get("model"), field="model", max_len=_MAX_MODEL_LEN)
    mode = _optional_str(body.get("mode"), field="mode", max_len=_MAX_MODE_LEN)
    active_profile = _active_profile_name()
    profile_override = _optional_str(body.get("profile"), field="profile", max_len=_MAX_PROFILE_LEN)
    if profile_override is not None and profile_override != active_profile:
        raise FeedbackValidationError("profile does not match active profile")
    profile = active_profile

    record = {
        "ts": time.time(),
        "session_id": session_id,
        "rating": rating,
        "profile": profile,
    }
    if message_id is not None:
        record["message_id"] = message_id
    if message_index is not None:
        record["index"] = message_index
    if reason is not None:
        record["reason"] = reason
    if model is not None:
        record["model"] = model
    if mode is not None:
        record["mode"] = mode
    return record


def _prune_rate_limit_keys(cutoff: float) -> None:
    for key in list(_RATE_HITS):
        timestamps = [ts for ts in _RATE_HITS.get(key, []) if ts >= cutoff]
        if timestamps:
            _RATE_HITS[key] = timestamps
        else:
            del _RATE_HITS[key]


def feedback_rate_limited(session_id: str, *, now: float | None = None) -> bool:
    """Return True when this session has exceeded the feedback write budget."""
    now = time.time() if now is None else now
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    key = str(session_id or "").strip() or "_"
    with _RATE_LOCK:
        _prune_rate_limit_keys(cutoff)
        timestamps = [ts for ts in _RATE_HITS.get(key, []) if ts >= cutoff]
        if len(timestamps) >= _RATE_LIMIT_MAX:
            _RATE_HITS[key] = timestamps
            return True
        _RATE_HITS[key] = timestamps
    return False


def feedback_record_rate_hit(session_id: str, *, now: float | None = None) -> None:
    """Record a successful feedback write against the session rate budget."""
    now = time.time() if now is None else now
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    key = str(session_id or "").strip() or "_"
    with _RATE_LOCK:
        _prune_rate_limit_keys(cutoff)
        timestamps = [ts for ts in _RATE_HITS.get(key, []) if ts >= cutoff]
        timestamps.append(now)
        _RATE_HITS[key] = timestamps


def append_feedback(record: dict) -> Path:
    """Append one normalized feedback record to the profile feedback.jsonl."""
    path = feedback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _FEEDBACK_LOCK:
        if path.exists() and path.stat().st_size + len(line.encode("utf-8")) > _MAX_FEEDBACK_FILE_BYTES:
            raise FeedbackValidationError("feedback store is full")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    return path
