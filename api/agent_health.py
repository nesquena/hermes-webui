"""Hermes agent/gateway heartbeat payload helpers (#716).

The WebUI process is not always paired with a long-running Hermes gateway. Some
setups use WebUI only, while self-hosted messaging deployments run a separate
Hermes gateway daemon that records runtime metadata in the Hermes Agent home.
This module turns those existing safe runtime signals into a small UI-facing
heartbeat without shelling out or adding psutil as a hard dependency.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_GATEWAY_PID_FILE = "gateway.pid"
_GATEWAY_RUNTIME_STATUS_FILE = "gateway_state.json"


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gateway_status_module():
    """Load gateway.status lazily so tests and WebUI-only installs stay isolated."""
    return importlib.import_module("gateway.status")


def _gateway_root_pid_path() -> Path | None:
    """Return the root Hermes gateway PID path.

    Gateway runtime files are root-level singletons.  A profile-scoped WebUI
    process may have HERMES_HOME=<root>/profiles/<name>, but gateway.pid,
    gateway.lock, and gateway_state.json still live under <root>.
    """
    try:
        from hermes_constants import get_default_hermes_root
        return get_default_hermes_root() / _GATEWAY_PID_FILE
    except Exception:
        return None


def _read_runtime_status_path(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _read_gateway_runtime_status(gateway_status: Any, pid_path: Path | None) -> dict[str, Any] | None:
    read_runtime_status = gateway_status.read_runtime_status
    if pid_path is not None:
        try:
            return read_runtime_status(pid_path=pid_path)
        except TypeError:
            try:
                return read_runtime_status(pid_path)
            except TypeError:
                if getattr(gateway_status, "__name__", "") == "gateway.status" or hasattr(
                    gateway_status,
                    "_read_json_file",
                ):
                    runtime_status_file = str(
                        getattr(gateway_status, "_RUNTIME_STATUS_FILE", _GATEWAY_RUNTIME_STATUS_FILE)
                    )
                    runtime_status = _read_runtime_status_path(pid_path.with_name(runtime_status_file))
                    if runtime_status is not None:
                        return runtime_status
    return read_runtime_status()


def _gateway_running_pid(gateway_status: Any, pid_path: Path | None) -> int | None:
    get_running_pid = gateway_status.get_running_pid
    if pid_path is not None:
        try:
            return get_running_pid(pid_path=pid_path, cleanup_stale=False)
        except TypeError:
            try:
                return get_running_pid(pid_path, cleanup_stale=False)
            except TypeError:
                pass
    try:
        return get_running_pid(cleanup_stale=False)
    except TypeError:
        # Older agent versions may not expose cleanup_stale. Keep compatibility.
        return get_running_pid()


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

    gateway_pid_path = _gateway_root_pid_path()

    runtime_status = None
    try:
        runtime_status = _read_gateway_runtime_status(gateway_status, gateway_pid_path)
    except Exception:
        runtime_status = None

    try:
        running_pid = _gateway_running_pid(gateway_status, gateway_pid_path)
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
