"""Read-only Hermes control-plane bridge for the WebUI cockpit.

This module intentionally exposes only server-side, browser-safe payloads from
Hermes Agent's existing control-plane dashboard helpers. It does not write to
Supabase, send Telegram messages, change cron, or edit Obsidian authority.
"""
from __future__ import annotations

import concurrent.futures
from pathlib import Path
from typing import Any, Callable, Dict, List

from api.helpers import j

_FORBIDDEN_BROWSER_KEYS = {
    "target" + "_ref",
    "provider" + "_message_ref",
    "error" + "_summary",
    "provider" + "_error_body",
    "raw" + "_payload",
    "private" + "_target_payload",
}


def _load_agent_control_plane_helpers():
    # api.config appends HERMES_WEBUI_AGENT_DIR to sys.path at import time.
    import api.config  # noqa: F401
    from hermes_cli import web_server as agent_web_server

    return agent_web_server


def _strip_forbidden_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_forbidden_keys(item)
            for key, item in value.items()
            if key not in _FORBIDDEN_BROWSER_KEYS
        }
    if isinstance(value, list):
        return [_strip_forbidden_keys(item) for item in value]
    return value


def _disabled_payload(reason: str, message: str) -> Dict[str, Any]:
    return {
        "enabled": False,
        "status": "disabled",
        "mode": "read_only",
        "reason": reason,
        "message": message,
        "boundaries": {
            "writes_enabled": False,
            "telegram_send_enabled": False,
            "obsidian_authority_edit_enabled": False,
            "cron_change_enabled": False,
        },
    }


def _call_with_timeout(fn: Callable[[], Dict[str, Any]], timeout_seconds: float = 8.0) -> Dict[str, Any]:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return _disabled_payload(
            "control_plane_bridge_timeout",
            "Control-plane bridge timed out; read-only page remains available with writes disabled.",
        )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def control_plane_payload() -> Dict[str, Any]:
    def _load_payload() -> Dict[str, Any]:
        agent_web_server = _load_agent_control_plane_helpers()
        return agent_web_server._get_control_plane_morning_brief_payload()
    try:
        payload = _call_with_timeout(_load_payload)
        return _strip_forbidden_keys(payload)
    except BaseException:
        return _disabled_payload(
            "control_plane_bridge_failed",
            "Control-plane bridge failed; check server logs or Hermes Agent dashboard helpers.",
        )


def control_plane_telegram_preview() -> Dict[str, Any]:
    def _load_payload() -> Dict[str, Any]:
        agent_web_server = _load_agent_control_plane_helpers()
        canary = agent_web_server._get_control_plane_morning_brief_payload()
        return agent_web_server._build_morning_brief_telegram_preview(canary)
    try:
        payload = _call_with_timeout(_load_payload)
        return _strip_forbidden_keys(payload)
    except BaseException:
        return _disabled_payload(
            "control_plane_bridge_failed",
            "Telegram preview bridge failed; check server logs or Hermes Agent dashboard helpers.",
        )


def control_plane_safety_preview() -> Dict[str, Any]:
    def _load_payload() -> Dict[str, Any]:
        agent_web_server = _load_agent_control_plane_helpers()
        return agent_web_server._get_control_plane_safety_preview_payload()
    try:
        payload = _call_with_timeout(_load_payload)
        return _strip_forbidden_keys(payload)
    except BaseException:
        return _disabled_payload(
            "control_plane_bridge_failed",
            "Safety preview bridge failed; check server logs or Hermes Agent dashboard helpers.",
        )


def _recent_control_plane_artifacts(limit: int = 6) -> List[Dict[str, Any]]:
    artifact_dir = Path.home() / ".hermes" / "webui-workspace" / "artifacts"
    try:
        files = sorted(
            [p for p in artifact_dir.glob("*.md") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        return [
            {
                "name": p.name,
                "path_label": f"artifacts/{p.name}",
                "updated_unix": int(p.stat().st_mtime),
            }
            for p in files
        ]
    except Exception:
        return []


def control_plane_overview() -> Dict[str, Any]:
    """Return static/live read-only cockpit domains for the first WebUI expansion.

    This is intentionally an overview/reader contract: it exposes what to look at
    and what is held, but it does not execute routines, send notifications,
    mutate Supabase, change cron, or edit Obsidian.
    """
    return {
        "mode": "read_only",
        "status": "ok",
        "cards": [
            {
                "key": "routine_cron_health",
                "title": "Routine / Cron health",
                "status": "reader_only",
                "summary": "Shows routine posture only; cron run/change controls remain held.",
                "lines": ["Morning Routine schedule: monitor only", "Cron mutation: OFF"],
            },
            {
                "key": "notebooklm_prewarm",
                "title": "NotebookLM pre-warm",
                "status": "scheduled_probe",
                "summary": "No-agent deterministic health probe lane; browser/session auth changes are held.",
                "lines": ["Daily pre-warm lane: 06:50 KST", "LLM pre-warm job: replaced/paused"],
            },
            {
                "key": "supabase_control_plane_canary",
                "title": "Supabase control-plane canary",
                "status": "dry_run_reader",
                "summary": "Control-plane canary read model only; production schema/data writes remain held.",
                "lines": ["Production schema mutation: OFF", "Report projection: read-only preview"],
            },
            {
                "key": "approval_gates_hold_list",
                "title": "Approval gates / HOLD list",
                "status": "hold",
                "summary": "Durable changes require explicit approval before action controls are added.",
                "lines": ["Telegram send: OFF", "Obsidian authority promotion: OFF", "Cron changes: OFF"],
            },
            {
                "key": "recent_artifacts",
                "title": "Recent artifacts / verification reports",
                "status": "reader_only",
                "summary": "Local non-Obsidian verification artifacts for operator review.",
                "artifacts": _recent_control_plane_artifacts(),
            },
            {
                "key": "webui_health",
                "title": "WebUI health",
                "status": "live_probe",
                "summary": "Frontend reads the existing /health endpoint; no service restart control is exposed.",
                "lines": ["Health check: GET /health", "Restart control: OFF"],
            },
        ],
        "boundaries": {
            "writes_enabled": False,
            "telegram_send_enabled": False,
            "obsidian_authority_edit_enabled": False,
            "cron_change_enabled": False,
            "service_restart_enabled": False,
        },
    }


def handle_control_plane_get(handler, parsed) -> bool:
    if parsed.path == "/api/control-plane/overview":
        return j(handler, _strip_forbidden_keys(control_plane_overview()))
    if parsed.path == "/api/control-plane/morning-brief-canary":
        return j(handler, control_plane_payload())
    if parsed.path == "/api/control-plane/morning-brief-canary/telegram-preview":
        return j(handler, control_plane_telegram_preview())
    if parsed.path == "/api/control-plane/morning-brief-canary/safety-preview":
        return j(handler, control_plane_safety_preview())
    return False
