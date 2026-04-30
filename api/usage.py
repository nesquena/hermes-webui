"""
Hermes Web UI -- Provider usage limits.

Reads usage data written by the ai-usage Tauri app (which fetches live from
provider APIs) and surfaces it to the frontend for the rail icon tooltip.

Data format (one JSON file per provider, written by ai-usage app):
  {
    "provider": "minimax",
    "hourly_limit": {"used": 123, "limit": 1500},
    "weekly_limit": {"used": 456, "limit": 15000},
    "hourly_reset_ts": 1746057600000,   // ms UTC epoch, optional
    "weekly_reset_ts": 1746057600000     // ms UTC epoch, optional
  }

Storage path: ~/.hermes/data/usage/<provider>.json
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Path discovery ────────────────────────────────────────────────────────────

def _hermes_home() -> Path:
    """Return the active Hermes home directory."""
    try:
        from api.profiles import get_active_hermes_home
        return get_active_hermes_home()
    except ImportError:
        return Path.home() / ".hermes"


def _usage_dir() -> Path:
    return _hermes_home() / "data" / "usage"


# ── Data loading ─────────────────────────────────────────────────────────────

def _load_provider_file(provider: str) -> dict[str, Any] | None:
    """Load a single provider's usage JSON file, or return None if absent."""
    path = _usage_dir() / f"{provider}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read usage file for %s: %s", provider, exc)
        return None


def get_usage_limits() -> list[dict[str, Any]]:
    """Return usage limits for all providers that have a usage file.

    Each dict contains:
      - provider: str
      - limit_5h: int   (hourly_limit.limit)
      - limit_7d: int   (weekly_limit.limit)
      - used_5h: int    (hourly_limit.used)
      - used_7d: int    (weekly_limit.used)
      - reset_5h_ts: int | None
      - reset_7d_ts: int | None
    """
    usage_dir = _usage_dir()
    if not usage_dir.is_dir():
        return []

    results: list[dict[str, Any]] = []

    for path in sorted(usage_dir.iterdir()):
        if path.suffix != ".json":
            continue
        provider = path.stem  # "minimax", "zai", etc.
        data = _load_provider_file(provider)
        if data is None:
            continue

        hourly = data.get("hourly_limit", {})
        weekly = data.get("weekly_limit", {})

        results.append({
            "provider": data.get("provider", provider),
            "limit_5h": int(hourly.get("limit", 0)),
            "limit_7d": int(weekly.get("limit", 0)),
            "used_5h": int(hourly.get("used", 0)),
            "used_7d": int(weekly.get("used", 0)),
            "reset_5h_ts": data.get("hourly_reset_ts"),
            "reset_7d_ts": data.get("weekly_reset_ts"),
        })

    return results


def get_enabled_providers() -> list[str]:
    """Return provider IDs that have an API key configured.

    Uses the same detection logic as api/providers.py so the rail icons
    match which providers the user has actually configured.
    """
    try:
        from api.providers import get_providers
        providers_data = get_providers()
        return [
            p["id"] for p in providers_data.get("providers", [])
            if p.get("has_key", False)
        ]
    except Exception as exc:
        logger.warning("Could not determine enabled providers: %s", exc)
        return []
