"""Hermes agent/gateway heartbeat payload helpers (#716, #1879).

The WebUI process is not always paired with a long-running Hermes gateway. Some
setups use WebUI only, while self-hosted messaging deployments run a separate
Hermes gateway daemon that records runtime metadata in the Hermes Agent home.
This module turns those existing safe runtime signals into a small UI-facing
heartbeat without shelling out or adding psutil as a hard dependency.

Cross-container note (#1879): ``gateway.status.get_running_pid()`` uses
``fcntl.flock`` and ``os.kill(pid, 0)``, both of which require the caller to
share a PID namespace with the gateway process. In multi-container deployments
where the WebUI runs separately from ``hermes-agent`` and only a Hermes data
volume is shared, those checks always return ``None`` and the dashboard
incorrectly shows "Gateway not running". To stay accurate without forcing a
``pid: "service:hermes-agent"`` compose workaround, we accept a recent
``updated_at`` timestamp on ``gateway_state.json`` (combined with
``gateway_state == "running"``) as an equivalent live-process signal — the
gateway already writes that file on every tick.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from typing import Any


# Two cron ticks (~60s each). Chosen to avoid false negatives during brief
# gateway restarts while still surfacing a true outage within a couple of
# minutes. Override is intentionally not exposed: keep the check deterministic
# and identical across deployments so support diagnostics are reproducible.
GATEWAY_FRESHNESS_THRESHOLD_S: float = 120.0


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runtime_status_is_fresh(
    runtime_status: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    threshold_s: float = GATEWAY_FRESHNESS_THRESHOLD_S,
) -> bool:
    """Return ``True`` when ``gateway_state.json`` looks freshly written.

    "Fresh" means the gateway self-reported ``running`` and the ``updated_at``
    ISO-8601 timestamp is no older than ``threshold_s`` seconds. This is the
    cross-container liveness signal used when ``get_running_pid()`` returns
    ``None`` purely because of PID-namespace isolation (#1879).

    Any unparseable input is treated as "not fresh" — a stale or missing
    timestamp must never report alive.
    """
    if not isinstance(runtime_status, dict):
        return False
    if runtime_status.get("gateway_state") != "running":
        return False

    raw_updated_at = runtime_status.get("updated_at")
    if not isinstance(raw_updated_at, str) or not raw_updated_at:
        return False

    # ``datetime.fromisoformat`` accepts the exact format gateway/status.py
    # writes (``datetime.now(timezone.utc).isoformat()``). We deliberately
    # don't pull in dateutil — keeping this stdlib-only matches the rest of
    # this module.
    try:
        updated_at = datetime.fromisoformat(raw_updated_at)
    except (TypeError, ValueError):
        return False

    if updated_at.tzinfo is None:
        # A naive timestamp could mean anything across containers / hosts.
        # Refuse to interpret it rather than assume UTC.
        return False

    reference = now if now is not None else datetime.now(timezone.utc)
    age_s = (reference - updated_at).total_seconds()
    if age_s < 0:
        # Clock skew between containers can produce small negatives. A future
        # timestamp is still a "fresh" signal — the gateway clearly wrote it
        # very recently — so accept it. A wildly-future timestamp (> threshold
        # in the future) is rejected to avoid trusting a broken clock.
        return -age_s <= threshold_s
    return age_s <= threshold_s


def _gateway_status_module():
    """Load gateway.status lazily so tests and WebUI-only installs stay isolated."""
    return importlib.import_module("gateway.status")


def _runtime_detail_subset(runtime_status: dict[str, Any] | None) -> dict[str, Any]:
    """Return only non-sensitive runtime fields for the browser.

    gateway.status records argv/PID metadata so the CLI can validate process
    identity. The WebUI alert only needs health semantics, never raw command
    lines, paths, environment, or tokens.
    """
    if not isinstance(runtime_status, dict):
        return {}

    details: dict[str, Any] = {}
    gateway_state = runtime_status.get("gateway_state")
    if isinstance(gateway_state, str) and gateway_state:
        details["gateway_state"] = gateway_state

    updated_at = runtime_status.get("updated_at")
    if isinstance(updated_at, str) and updated_at:
        details["updated_at"] = updated_at

    try:
        details["active_agents"] = max(0, int(runtime_status.get("active_agents") or 0))
    except (TypeError, ValueError):
        pass

    platforms = runtime_status.get("platforms")
    if isinstance(platforms, dict):
        details["platform_count"] = len(platforms)
        states: dict[str, int] = {}
        for payload in platforms.values():
            if not isinstance(payload, dict):
                continue
            state = payload.get("state")
            if isinstance(state, str) and state:
                states[state] = states.get(state, 0) + 1
        if states:
            details["platform_states"] = states

    return details


def build_agent_health_payload() -> dict[str, Any]:
    """Return `{alive, checked_at, details}` for the Hermes gateway/agent.

    `alive` is intentionally tri-state:
      * True: a gateway runtime signal says the process is alive.
      * False: gateway metadata exists, but no live gateway process owns it.
      * None: no gateway metadata/status is available, so this WebUI setup is
        probably not configured with a separate gateway process.
    """
    checked_at = _checked_at()
    try:
        gateway_status = _gateway_status_module()
    except Exception as exc:
        return {
            "alive": None,
            "checked_at": checked_at,
            "details": {
                "state": "unknown",
                "reason": "gateway_status_unavailable",
                "error": type(exc).__name__,
            },
        }

    runtime_status = None
    try:
        runtime_status = gateway_status.read_runtime_status()
    except Exception:
        runtime_status = None

    try:
        running_pid = gateway_status.get_running_pid(cleanup_stale=False)
    except TypeError:
        # Older agent versions may not expose cleanup_stale. Keep compatibility.
        running_pid = gateway_status.get_running_pid()
    except Exception:
        running_pid = None

    safe_details = _runtime_detail_subset(runtime_status)
    if running_pid is not None:
        return {
            "alive": True,
            "checked_at": checked_at,
            "details": {
                "state": "alive",
                **safe_details,
            },
        }

    # Cross-container fallback (#1879): when ``get_running_pid()`` cannot see
    # the gateway because we're in a different PID namespace, a recent
    # ``updated_at`` on ``gateway_state.json`` is a reliable equivalent signal
    # since the gateway writes it on every tick. We only trust this fallback
    # when the gateway also self-reports ``gateway_state == "running"`` so
    # crash-without-cleanup scenarios still surface as "down".
    if _runtime_status_is_fresh(runtime_status):
        return {
            "alive": True,
            "checked_at": checked_at,
            "details": {
                "state": "alive",
                "reason": "cross_container_freshness",
                **safe_details,
            },
        }

    if isinstance(runtime_status, dict):
        return {
            "alive": False,
            "checked_at": checked_at,
            "details": {
                "state": "down",
                "reason": "gateway_not_running",
                **safe_details,
            },
        }

    return {
        "alive": None,
        "checked_at": checked_at,
        "details": {
            "state": "unknown",
            "reason": "gateway_not_configured",
        },
    }
