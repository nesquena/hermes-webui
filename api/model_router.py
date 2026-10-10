"""api.model_router -- Model scheduler integration for Hermes Web UI.

Thin wrapper around the `model-scheduler` library
(https://github.com/Odd-C/model-scheduler). The scheduler is an optional,
off-by-default advisor: it recommends a model per message; the WebUI still
sends through the normal Hermes path. When the library is not installed,
every public function degrades to a clear "not installed" response.
"""

from __future__ import annotations

import logging
from pathlib import Path

from api.config import STATE_DIR

logger = logging.getLogger(__name__)

# model-policy.json / quota state live in the WebUI state dir, next to the
# other user-editable WebUI state files.
_MS_STATE_DIR: Path = STATE_DIR

_model_scheduler = None
_model_scheduler_missing = False


def _load_lib():
    """Import model_scheduler once; cache the module or the miss flag."""
    global _model_scheduler, _model_scheduler_missing
    if _model_scheduler is not None:
        return _model_scheduler
    if _model_scheduler_missing:
        return None
    try:
        import model_scheduler as _ms
    except Exception:  # pragma: no cover - depends on user environment
        _model_scheduler_missing = True
        logger.warning("model-scheduler library not installed; scheduler disabled")
        return None
    _model_scheduler = _ms
    try:
        _model_scheduler.configure_state_dir(_MS_STATE_DIR)
    except Exception:  # pragma: no cover - defensive
        logger.debug("model_scheduler.configure_state_dir failed", exc_info=True)
    return _model_scheduler


def _require_lib():
    """Return the model_scheduler module or raise a friendly RuntimeError."""
    ms = _load_lib()
    if ms is None:
        raise RuntimeError(
            "model-scheduler library not installed. Install it with: pip install model-scheduler"
        )
    return ms


def _master_enabled() -> bool:
    """Return the WebUI settings master switch state."""
    try:
        from api.config import load_settings
        return bool(load_settings().get("model_scheduler_enabled", False))
    except Exception:  # pragma: no cover - defensive
        return False


def _degraded(reason: str) -> dict:
    """Standard recommendation payload for disabled / missing / failed paths."""
    return {
        "model": "",
        "provider": "",
        "reason": reason,
        "tier": "",
        "cost": "paid",
        "difficulty": 0,
        "urgent": False,
        "peak": False,
        "key": "",
    }


def _policy_error(reason: str) -> dict:
    """Standard policy payload for missing / failed paths."""
    return {
        "enabled": False,
        "schedule": [],
        "models": [],
        "quota_window_hours": 0,
        "error": reason,
    }


def get_status() -> dict:
    """Return scheduler on/off + schedule + quota window summary.

    The master switch is the WebUI settings key `model_scheduler_enabled`;
    it is the only authority for `enabled`. The policy file's `enabled`
    field no longer participates in the gate.
    """
    ms = _load_lib()
    if ms is None:
        return {"enabled": False, "schedule": [], "error": "model-scheduler not installed"}
    try:
        p = ms.get_policy()
    except Exception:
        logger.exception("model-scheduler get_policy failed")
        return {"enabled": False, "schedule": [], "error": "policy load failed"}
    return {
        "enabled": _master_enabled(),
        "schedule": p.get("schedule") or [],
        "quota_window_hours": ms.QUOTA_WINDOW_SECONDS // 3600,
    }


def get_policy() -> dict:
    """Return full policy: schedule, models, quota window."""
    ms = _load_lib()
    if ms is None:
        return _policy_error("model-scheduler not installed")
    try:
        p = ms.get_policy()
    except Exception:
        logger.exception("model-scheduler get_policy failed")
        return _policy_error("policy load failed")
    return {
        "enabled": _master_enabled(),
        "schedule": p.get("schedule") or [],
        "models": ms.list_models(),
        "quota_window_hours": ms.QUOTA_WINDOW_SECONDS // 3600,
    }


def update_policy(updates: dict) -> dict:
    """Merge updates into model-policy.json (JSON override).

    Raises RuntimeError when the library is not installed (route layer maps
    it to a clear 503); returns the merged policy dict on success.
    """
    ms = _require_lib()
    return ms.update_policy(updates)


def recommend(text: str, message_count: int = 0, session_id: str | None = None) -> dict:
    """Recommend a model for a session/message. Never raises.

    Enforces the settings master switch on the backend: when it is off, no
    recommendation is produced even if model-policy.json has "enabled": true.

    model-scheduler v0.2.1 supports the `session_id` keyword directly, so it
    is passed through without signature probing. None/empty session_id means
    the library does not add the field to the recommendation result.
    """
    if not _master_enabled():
        return _degraded("model scheduler disabled")
    ms = _load_lib()
    if ms is None:
        return _degraded("model-scheduler not installed")
    try:
        messages = max(0, int(message_count or 0))
    except (TypeError, ValueError):
        messages = 0
    session_id = str(session_id or "") or None
    try:
        return ms.recommend_for_session(text, message_count=messages, session_id=session_id)
    except Exception:
        logger.exception("model-scheduler recommend failed")
        return _degraded("recommendation unavailable")


def record_failure(model: str, provider: str | None = None) -> None:
    """Record an upstream failure so the model enters cooldown."""
    ms = _load_lib()
    if ms is None:
        return
    try:
        ms.record_failure(str(model or ""), str(provider or "") or None)
    except Exception:
        logger.debug("model-scheduler record_failure failed", exc_info=True)


def to_upstream_model_key(model: str, provider: str) -> str:
    """Convert scheduler {model, provider} to the WebUI model-selector value.

    Thin forwarding to model-scheduler v0.2.1 `format_selector_key`
    (`provider/model`). When the library is not installed, falls back to the
    same inline `provider/model` conversion so the helper never raises.
    """
    ms = _load_lib()
    if ms is not None:
        return ms.format_selector_key(model, provider)
    model = str(model or "").strip()
    provider = str(provider or "").strip()
    if not model:
        return ""
    if provider:
        return f"{provider}/{model}"
    return model


def parse_upstream_model_key(value: str) -> tuple[str, str | None]:
    """Inverse of to_upstream_model_key: `provider/model` -> (model, provider).

    Thin forwarding to model-scheduler v0.2.1 `parse_selector_key`. When the
    library is not installed, falls back to the same inline partition("/")
    logic so the helper never raises.
    """
    ms = _load_lib()
    if ms is not None:
        return ms.parse_selector_key(value)
    value = str(value or "").strip()
    if not value:
        return "", None
    if "/" in value:
        provider, _, model = value.partition("/")
        return model.strip(), provider.strip() or None
    return value, None
