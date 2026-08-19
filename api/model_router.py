"""api.model_router -- Model scheduler integration for Hermes Web UI.

Thin wrapper around the `model-scheduler` library (https://github.com/Odd-C/model-scheduler).

What this provides:
  - Session-level model recommendation (difficulty + urgency -> best model),
    driven by a user-editable profile table (`model-policy.json` in the
    WebUI state dir).
  - Free-quota tracking (5h sliding window) and failure cooldown, so
    routing automatically avoids models that are exhausted or rate-limited.
  - Peak-hour awareness (Asia/Shanghai 9:00-12:00 / 14:00-18:00 by default,
    configurable per model).

Design note: this is the "advisor" integration. The scheduler recommends a
model before a message is sent; the WebUI still sends the request through
the normal Hermes path. A standalone OpenAI-compatible proxy (`model-scheduler
serve`) is available in the library for multi-client scenarios, but is out of
scope here.

The scheduler is OFF by default; enable it in the WebUI model panel or by
setting `"enabled": true` in model-policy.json.
"""

from __future__ import annotations

import logging
from pathlib import Path

from api.config import STATE_DIR

logger = logging.getLogger(__name__)

# State dir for model-scheduler (profile JSON + quota JSON live here).
# We use the WebUI state dir so `model-policy.json` sits next to the other
# user-editable WebUI state files.
_MS_STATE_DIR: Path = STATE_DIR

# The scheduler library is an OPTIONAL dependency (requirements.txt comment).
# Import lazily so the WebUI keeps working when model-scheduler is not
# installed; every public function degrades to a clear "not installed" state.
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


def _configure() -> None:
    """Point model-scheduler at the WebUI state dir (idempotent)."""
    ms = _model_scheduler
    if ms is None:
        return
    try:
        ms.configure_state_dir(_MS_STATE_DIR)
    except Exception:  # pragma: no cover - defensive
        logger.debug("model_scheduler.configure_state_dir failed", exc_info=True)


def get_status() -> dict:
    """Return scheduler on/off + schedule + quota window summary.

    The master switch lives in WebUI settings (`model_scheduler_enabled`),
    NOT in model-policy.json. The policy file's `enabled` field only takes
    effect when the settings switch is also on — so the feature stays off
    by default for every user until they opt in from Settings.
    """
    ms = _load_lib()
    if ms is None:
        return {"enabled": False, "schedule": [], "error": "model-scheduler not installed"}
    try:
        from api.config import load_settings
        master_on = bool(load_settings().get("model_scheduler_enabled", False))
    except Exception:  # pragma: no cover - defensive
        master_on = False
    try:
        p = ms.get_policy()
    except Exception:
        logger.exception("model-scheduler get_policy failed")
        return {"enabled": False, "schedule": [], "error": "policy load failed"}
    return {
        "enabled": bool(master_on and p.get("enabled", False)),
        "schedule": p.get("schedule") or [],
        "quota_window_hours": ms.QUOTA_WINDOW_SECONDS // 3600,
    }


def get_policy() -> dict:
    """Return full policy: enabled, schedule, models, quota window."""
    ms = _require_lib()
    return {
        "enabled": bool(ms.get_policy().get("enabled", False)),
        "schedule": ms.get_policy().get("schedule") or [],
        "models": ms.list_models(),
        "quota_window_hours": ms.QUOTA_WINDOW_SECONDS // 3600,
    }


def update_policy(updates: dict) -> dict:
    """Merge updates into model-policy.json (JSON override)."""
    ms = _require_lib()
    return ms.update_policy(updates)


def recommend(text: str, message_count: int = 0) -> dict:
    """Recommend a model for a session/message. Never raises."""
    ms = _load_lib()
    if ms is None:
        return {
            "model": "",
            "provider": "",
            "reason": "model-scheduler not installed",
            "tier": "",
            "cost": "paid",
            "difficulty": 0,
            "urgent": False,
            "peak": False,
            "key": "",
        }
    text = str(text or "")
    try:
        messages = max(0, int(message_count or 0))
    except (TypeError, ValueError):
        messages = 0
    try:
        return ms.recommend_for_session(text, message_count=messages)
    except Exception:
        logger.exception("model-scheduler recommend failed")
        return {
            "model": "",
            "provider": "",
            "reason": "recommendation unavailable",
            "tier": "",
            "cost": "paid",
            "difficulty": 0,
            "urgent": False,
            "peak": False,
            "key": "",
        }


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

    The WebUI's model selector uses `provider/model` values (e.g.
    `openai/gpt-5.4-mini`), while the scheduler uses `id@provider`. This
    helper produces the value that `$('modelSelect')` expects so a
    recommendation can be applied by setting the select value.
    """
    model = str(model or "").strip()
    provider = str(provider or "").strip()
    if not model:
        return ""
    if provider:
        return f"{provider}/{model}"
    return model


def parse_upstream_model_key(value: str) -> tuple[str, str | None]:
    """Inverse of to_upstream_model_key: `provider/model` -> (model, provider)."""
    value = str(value or "").strip()
    if not value:
        return "", None
    if "/" in value:
        provider, _, model = value.partition("/")
        return model.strip(), provider.strip() or None
    return value, None
