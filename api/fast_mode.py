"""Fast-mode readiness helpers for the Issue 39 prototype path.

This module deliberately exposes only sanitized capability/readiness metadata. It
must not read provider credential files, environment secrets, transcripts, or
workspace contents. The first slices use this as a truthful health surface before
any user-facing launcher prints "ready".
"""
from __future__ import annotations

import os
from typing import Any

_FAST_MODE_VERSION = 1
_ALLOWED_MODES = {
    "disabled",
    "synthetic_fixture",
    "host_smoke",
    "real_model_prototype",
}
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in _TRUE_VALUES


def _safe_mode(raw: str | None, *, enabled: bool) -> str:
    value = str(raw or "").strip().lower()
    if value in _ALLOWED_MODES:
        return value
    return "host_smoke" if enabled else "disabled"


def health_payload() -> dict[str, Any]:
    """Return a sanitized fast-mode readiness payload.

    The shape is intentionally conservative: booleans/enums only, no local
    paths, no raw config, no model responses, no token material, and no provider
    credential details. Later implementation slices can fill in effective
    provider/model labels after they have a safe resolver surface.
    """
    enabled = _env_flag("HERMES_WEBUI_FAST_MODE")
    mode = _safe_mode(os.getenv("HERMES_WEBUI_FAST_MODE_KIND"), enabled=enabled)
    return {
        "ok": True,
        "fast_mode": {
            "version": _FAST_MODE_VERSION,
            "enabled": enabled,
            "mode": mode,
        },
        "foreground": {
            "path": "webui_chat_stream",
            "real_profile_required_for_acceptance": True,
            "strict_no_tools_enforced": False,
            "strict_no_tools_enforcement": "not_implemented",
        },
        "background": {
            "durable_task_store": True,
            "parent_transcript_return": False,
            "parent_transcript_return_mode": "not_implemented",
            "legacy_polling_endpoint": "/api/background/status",
        },
        "delivery": {
            "normal_host_webui_required_for_acceptance": True,
            "container_local_urls_are_acceptance_blocker": True,
        },
        "acceptance": {
            "synthetic_provider_counts_as_acceptance": False,
            "voice_in_v1_scope": False,
        },
    }
