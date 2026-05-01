"""Capy Spaces storage and recovery primitives.

This module is intentionally isolated from chat/streaming internals so the
Spaces foundation can survive Hermes WebUI and Hermes Agent updates. The first
slice is storage + safe recovery only; generated widget rendering and agent
execution arrive later behind stricter permissions.
"""
from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import api.config as config

SCHEMA_VERSION = 1
_SPACE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_WIDGET_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_EVENT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,79}$")
_TRUTHY = {"1", "true", "yes", "on", "enabled"}
_OMITTED_PAYLOAD_KEYS = {
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "cookie",
    "data",
    "html",
    "password",
    "renderer",
    "script",
    "secret",
    "source",
    "token",
}
_SECRET_LIKE_VALUE_RE = re.compile(
    r"\b(api[_-]?key|apikey|authorization|bearer|cookie|password|secret|token)\b",
    re.IGNORECASE,
)
_EXECUTABLE_VALUE_MARKERS = ("<script", "</script", "javascript:", "onerror", "onload")
_TRUSTED_SYSTEM_WIDGETS = {
    "chat": {"id": "system-chat", "title": "Chat"},
    "workspaces": {"id": "system-workspaces", "title": "Spaces"},
    "tasks": {"id": "system-tasks", "title": "Tasks"},
    "memory": {"id": "system-memory", "title": "Memory"},
    "settings": {"id": "system-settings", "title": "Settings"},
}
_SPACE_DEMO_RUNS = [
    {"demo": "demo_weather_widget", "template": "weather", "title": "Weather widget"},
    {"demo": "demo_daily_dashboard", "template": "dashboard", "title": "Daily dashboard"},
    {"demo": "demo_notes_app", "template": "notes", "title": "Notes app"},
    {"demo": "demo_camera_dashboard", "template": "camera", "title": "Camera dashboard"},
    {"demo": "demo_local_agent_control_dashboard", "template": "service", "title": "Local service dashboard"},
    {"demo": "demo_browser_cocontrol_google_or_test_site", "template": "browser", "title": "Browser co-control"},
    {"demo": "demo_research_harness_pdf_export", "template": "research", "title": "Research harness"},
    {"demo": "demo_kanban_board", "template": "kanban", "title": "Kanban board"},
    {"demo": "demo_stock_chart", "template": "stock", "title": "Stock chart"},
    {"demo": "demo_snake_iterative_repair", "template": "game", "title": "Snake repair loop"},
    {"demo": "demo_step_sequencer_piano_roll", "template": "music", "title": "Step sequencer"},
    {"demo": "demo_big_bang_onboarding", "template": "big-bang", "title": "Big Bang onboarding"},
    {"demo": "demo_time_travel_restore", "template": "weather", "title": "Time travel restore"},
    {"demo": "demo_safe_admin_recovery", "template": "weather", "title": "Admin recovery"},
]
_SPACE_DEMO_RUN_BY_NAME = {item["demo"]: item for item in _SPACE_DEMO_RUNS}


def spaces_enabled() -> bool:
    """Return whether Capy Spaces is enabled for normal API use."""
    return str(os.getenv("HERMES_WEBUI_SPACES_ENABLED", "")).strip().lower() in _TRUTHY


def spaces_root() -> Path:
    return Path(config.STATE_DIR).expanduser().resolve() / "capy-spaces"


def manifests_dir() -> Path:
    return spaces_root() / "spaces"


def events_dir() -> Path:
    return spaces_root() / "events"


def _ensure_dirs() -> None:
    manifests_dir().mkdir(parents=True, exist_ok=True)
    events_dir().mkdir(parents=True, exist_ok=True)


def _slugify(value: str) -> str:
    value = (value or "space").strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-_")
    return value[:64] or "space"


def validate_space_id(space_id: str) -> str:
    sid = str(space_id or "").strip()
    if not _SPACE_ID_RE.fullmatch(sid):
        raise ValueError("Invalid space_id")
    return sid


def validate_widget_id(widget_id: str) -> str:
    wid = str(widget_id or "").strip()
    if not _WIDGET_ID_RE.fullmatch(wid):
        raise ValueError("Invalid widget_id")
    return wid


def validate_event_name(event_name: str) -> str:
    name = str(event_name or "agent.prompt").strip() or "agent.prompt"
    if not _EVENT_NAME_RE.fullmatch(name):
        raise ValueError("Invalid event_name")
    return name


def _space_dir(space_id: str) -> Path:
    sid = validate_space_id(space_id)
    root = manifests_dir().resolve()
    path = (root / sid).resolve()
    path.relative_to(root)
    return path


def _manifest_path(space_id: str) -> Path:
    return _space_dir(space_id) / "space.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    tmp = path.with_suffix(f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _record_event(
    space_id: str,
    event_type: str,
    details: dict[str, Any] | None = None,
    *,
    event_id: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> str:
    _ensure_dirs()
    safe_event_id = event_id if _event_id_is_safe(event_id) else uuid.uuid4().hex
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": safe_event_id,
        "event_type": event_type,
        "space_id": space_id,
        "created_at": time.time(),
        "details": details or {},
    }
    if isinstance(snapshot, dict):
        event["snapshot"] = json.loads(json.dumps(snapshot, ensure_ascii=False, default=str))
    _atomic_write_json(events_dir() / f"{safe_event_id}.json", event)
    return safe_event_id


def _write_manifest(space: dict[str, Any], event_type: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    now = time.time()
    space.setdefault("created_at", now)
    space["updated_at"] = now
    event_id = uuid.uuid4().hex
    revisions = list(space.get("revision_events") or [])
    revisions.append(event_id)
    space["revision_events"] = revisions
    space["revision_event_id"] = event_id
    _record_event(space["space_id"], event_type, details, event_id=event_id, snapshot=space)
    _atomic_write_json(_manifest_path(space["space_id"]), space)
    return dict(space)


def _summary(space: dict[str, Any]) -> dict[str, Any]:
    widgets = space.get("widgets") or []
    return {
        "schema_version": space.get("schema_version", SCHEMA_VERSION),
        "space_id": space.get("space_id"),
        "name": space.get("name") or space.get("space_id"),
        "description": space.get("description", ""),
        "created_at": space.get("created_at"),
        "updated_at": space.get("updated_at"),
        "revision_event_id": space.get("revision_event_id"),
        "widget_count": len(widgets) if isinstance(widgets, list) else 0,
    }


def _clamped_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _truthy_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUTHY


def _normalize_widget_layout(layout: Any) -> dict[str, Any]:
    raw = layout if isinstance(layout, dict) else {}
    return {
        "x": _clamped_int(raw.get("x"), 0, 0, 10_000),
        "y": _clamped_int(raw.get("y"), 0, 0, 10_000),
        "w": _clamped_int(raw.get("w"), 6, 1, 24),
        "h": _clamped_int(raw.get("h"), 4, 1, 24),
        "minimized": _truthy_bool(raw.get("minimized")),
    }


def _normalize_widget(widget: dict[str, Any]) -> dict[str, Any]:
    wid = validate_widget_id(widget.get("id"))
    clean_widget = dict(widget)
    clean_widget["id"] = wid
    clean_widget["kind"] = str(clean_widget.get("kind") or "custom")
    clean_widget["title"] = str(clean_widget.get("title") or clean_widget.get("name") or wid)
    clean_widget["layout"] = _normalize_widget_layout(clean_widget.get("layout"))
    return clean_widget


def _widget_summary(widget: dict[str, Any]) -> dict[str, Any]:
    clean_widget = _normalize_widget(widget)
    summary = {
        "id": clean_widget["id"],
        "kind": clean_widget["kind"],
        "title": clean_widget["title"],
        "layout": clean_widget["layout"],
    }
    system = widget.get("system") if isinstance(widget.get("system"), dict) else {}
    panel = str(system.get("panel") or "").strip()
    if clean_widget["kind"] == "system" and panel in _TRUSTED_SYSTEM_WIDGETS:
        summary["system_panel"] = panel
    return summary


def _widget_recovery_summary(widget: dict[str, Any]) -> dict[str, Any]:
    clean_widget = _normalize_widget(widget)
    recovery = widget.get("recovery") if isinstance(widget.get("recovery"), dict) else {}
    return {
        "id": clean_widget["id"],
        "kind": clean_widget["kind"],
        "title": clean_widget["title"],
        "disabled": bool(recovery.get("disabled")),
        "disabled_reason": _context_value(recovery.get("disabled_reason"), 300),
    }


def _context_value(value: Any, limit: int = 500) -> str:
    """Return a single-line value safe for compact agent context."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _payload_key_is_safe(key: str) -> bool:
    lowered = str(key or "").strip().lower()
    if not lowered:
        return False
    return not any(part in lowered for part in _OMITTED_PAYLOAD_KEYS)


def _payload_text_summary(value: Any, limit: int = 500) -> str:
    text = _context_value(value, limit)
    lowered = text.lower()
    if text and (
        _SECRET_LIKE_VALUE_RE.search(text)
        or any(marker in lowered for marker in _EXECUTABLE_VALUE_MARKERS)
    ):
        return "[REDACTED]"
    return text


def _payload_summary(value: Any, depth: int = 0) -> Any:
    """Return a bounded, metadata-safe widget event payload summary.

    Widget events are the bridge toward agent-triggered UI actions, but this
    first slice must not persist or echo generated renderer/html/script bodies
    or obvious secret-bearing fields. Full payload delivery can be added later
    behind explicit capability and sandbox checks.
    """
    if depth > 2:
        return "[omitted]"
    if isinstance(value, dict):
        summary: dict[str, Any] = {}
        for key, child in list(value.items())[:50]:
            safe_key = _context_value(key, 80)
            if not _payload_key_is_safe(safe_key):
                continue
            summary[safe_key] = _payload_summary(child, depth + 1)
        return summary
    if isinstance(value, list):
        return [_payload_summary(child, depth + 1) for child in value[:20]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _payload_text_summary(value, 500)
    return _payload_text_summary(type(value).__name__, 80)


def _event_id_is_safe(event_id: Any) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{32}", str(event_id or "")))


def _event_summary(event: dict[str, Any], sid: str) -> dict[str, Any] | None:
    event_id = str(event.get("event_id") or "")
    if not _event_id_is_safe(event_id) or event.get("space_id") != sid:
        return None
    details = _payload_summary(event.get("details") or {})
    if not isinstance(details, dict):
        details = {}
    return {
        "schema_version": event.get("schema_version", SCHEMA_VERSION),
        "event_id": event_id,
        "event_type": _context_value(event.get("event_type"), 120),
        "space_id": sid,
        "created_at": event.get("created_at"),
        "details": details,
    }


def build_agent_context(space_id: str | None) -> str:
    """Build compact active-space context for Hermes agent prompts.

    This intentionally exposes metadata only. Widget renderer/html/script/data
    bodies can contain generated code or sensitive payloads and must stay out of
    chat/system prompts unless a later sandboxed viewer explicitly asks for them.
    """
    if not space_id or not spaces_enabled():
        return ""

    sid = validate_space_id(space_id)
    space = read_space(sid)
    lines = [
        "## Active Capy Space",
        f"id: {sid}",
        f"name: {_context_value(space.get('name') or sid)}",
    ]
    description = _context_value(space.get("description"), 700)
    if description:
        lines.append(f"description: {description}")
    template = _context_value(space.get("template"), 120)
    if template:
        lines.append(f"template: {template}")
    instructions = _context_value(space.get("agent_instructions") or space.get("instructions"), 1500)
    if instructions:
        lines.append("instructions:")
        lines.append(f"  {instructions}")
    lines.append("widgets (id|title|kind):")
    widgets = space.get("widgets") or []
    summaries: list[dict[str, Any]] = []
    if isinstance(widgets, list):
        for widget in widgets:
            if isinstance(widget, dict):
                try:
                    summaries.append(_widget_summary(widget))
                except ValueError:
                    continue
    if summaries:
        for widget in summaries[:25]:
            lines.append(
                "- "
                f"{_context_value(widget['id'], 80)}|"
                f"{_context_value(widget['title'], 160)}|"
                f"{_context_value(widget['kind'], 80)}"
            )
        if len(summaries) > 25:
            lines.append(f"- … {len(summaries) - 25} more widget(s) omitted")
    else:
        lines.append("- none")
    revision = _context_value(space.get("revision_event_id"), 120)
    if revision:
        lines.append(f"revision_event_id: {revision}")
    lines.append(
        "Use Capy space APIs/tools for mutations. Prefer list/read before patching existing widgets; "
        "do not infer or expose generated widget bodies from this compact context."
    )
    return "\n".join(lines)


def _unique_space_id(base: str) -> str:
    sid = validate_space_id(_slugify(base))
    candidate = sid
    idx = 2
    while _manifest_path(candidate).exists():
        suffix = f"-{idx}"
        candidate = f"{sid[:64 - len(suffix)]}{suffix}"
        idx += 1
    return candidate


def list_spaces() -> list[dict[str, Any]]:
    if not spaces_enabled():
        return []
    _ensure_dirs()
    spaces: list[dict[str, Any]] = []
    for manifest in manifests_dir().glob("*/space.json"):
        try:
            spaces.append(_summary(json.loads(manifest.read_text(encoding="utf-8"))))
        except Exception:
            continue
    spaces.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return spaces


def create_space(payload: dict[str, Any]) -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    _ensure_dirs()
    name = str(payload.get("name") or "Untitled Space").strip() or "Untitled Space"
    requested_id = payload.get("space_id")
    space_id = validate_space_id(requested_id) if requested_id else _unique_space_id(name)
    if _manifest_path(space_id).exists():
        raise FileExistsError("Space already exists")
    now = time.time()
    space = {
        "schema_version": SCHEMA_VERSION,
        "space_id": space_id,
        "name": name,
        "description": str(payload.get("description") or ""),
        "agent_instructions": str(payload.get("agent_instructions") or payload.get("instructions") or ""),
        "template": str(payload.get("template") or "blank"),
        "created_at": now,
        "updated_at": now,
        "layout": payload.get("layout") if isinstance(payload.get("layout"), dict) else {},
        "widgets": payload.get("widgets") if isinstance(payload.get("widgets"), list) else [],
        "capabilities": payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {},
        "recovery": {"safe_mode_available": True},
        "revision_events": [],
        "revision_event_id": None,
    }
    return _write_manifest(space, "space.created", {"name": name})


def _safe_session_title_for_space(title: Any) -> str:
    text = _context_value(title, 80)
    if not text or text.lower() == "untitled":
        return "Chat Context Space"
    if re.search(r"api[_-]?key|authorization|bearer|cookie|password|secret|token", text, re.IGNORECASE):
        return "Chat Context Space"
    text = re.sub(r"[<>]", "", text).strip() or "Chat Context"
    return text if text.lower().endswith("space") else f"{text} Space"


def create_space_from_session_metadata(session: Any) -> dict[str, Any]:
    """Create a metadata-only Space linked to a trusted chat session.

    The current chat's message bodies are intentionally not copied into the
    Space manifest or API response. This creates a safe starter surface and the
    route activates it separately on the session.
    """
    title = getattr(session, "title", "")
    name = _safe_session_title_for_space(title)
    return create_space(
        {
            "name": name,
            "description": "Trusted starter created from the current chat. Message bodies stay in chat and are not copied into Space metadata.",
            "template": "chat-context",
            "layout": {"columns": 24},
            "widgets": [
                {
                    "id": "chat-context",
                    "kind": "status",
                    "title": "Linked chat context",
                    "layout": {"x": 0, "y": 0, "w": 8, "h": 4},
                }
            ],
            "capabilities": {"trusted_session_context": True},
        }
    )


def read_space(space_id: str) -> dict[str, Any]:
    path = _manifest_path(space_id)
    if not path.exists():
        raise FileNotFoundError("Space not found")
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("widgets", [])
    data.setdefault("layout", {})
    data.setdefault("revision_events", [])
    return data


def read_space_detail(space_id: str) -> dict[str, Any]:
    """Return safe metadata for detail/list APIs without widget bodies."""
    space = read_space(space_id)
    detail = {
        "schema_version": space.get("schema_version", SCHEMA_VERSION),
        "space_id": space.get("space_id"),
        "name": space.get("name") or space.get("space_id"),
        "description": space.get("description", ""),
        "agent_instructions": space.get("agent_instructions", ""),
        "template": space.get("template", "blank"),
        "created_at": space.get("created_at"),
        "updated_at": space.get("updated_at"),
        "layout": space.get("layout") if isinstance(space.get("layout"), dict) else {},
        "revision_event_id": space.get("revision_event_id"),
        "revision_events": [event_id for event_id in (space.get("revision_events") or []) if _event_id_is_safe(event_id)],
        "recovery": {"safe_mode_available": True},
        "widgets": [],
    }
    widgets = space.get("widgets") or []
    if isinstance(widgets, list):
        detail["widgets"] = [_widget_summary(widget) for widget in widgets if isinstance(widget, dict)]
    return detail


def current_space_for_session(session: Any) -> dict[str, Any]:
    """Return the metadata-only active Space envelope for a WebUI session."""
    if not spaces_enabled():
        return {"enabled": False, "active_space_id": None, "space": None}
    active_space_id = str(getattr(session, "active_space_id", "") or "").strip()
    if not active_space_id:
        return {"enabled": True, "active_space_id": None, "space": None}
    sid = validate_space_id(active_space_id)
    return {
        "enabled": True,
        "active_space_id": sid,
        "space": read_space_detail(sid),
    }


def list_space_demo_runs() -> list[dict[str, Any]]:
    """Return the metadata-only scripted video-demo parity smoke catalog."""
    if not spaces_enabled():
        return []
    return [
        {
            "demo": item["demo"],
            "template": item["template"],
            "title": item["title"],
            "mode": "metadata-only-smoke",
        }
        for item in _SPACE_DEMO_RUNS
    ]


def _space_demo_run_summary(demo: str, template: str, space_id: str, *, action: str) -> dict[str, Any]:
    widgets = list_widgets(space_id)
    revisions = list_revision_events(space_id)
    return {
        "ok": True,
        "demo": demo,
        "template": template,
        "mode": "metadata-only-smoke",
        "action": action,
        "space": read_space_detail(space_id),
        "widgets": widgets,
        "widget_count": len(widgets),
        "revision_event_count": len(revisions),
        "rollback_point": bool(revisions),
    }


def space_demo_run(name: str) -> dict[str, Any]:
    """Run one safe metadata-only smoke for a Space Agent video demo fixture.

    This is intentionally not a renderer executor. It launches the matching
    declarative Capy template, proves there is a persistent widget set and a
    revision anchor, and uses existing recovery/restore primitives for the two
    parity demos that specifically exercise those paths.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    demo = str(name or "").strip()
    spec = _SPACE_DEMO_RUN_BY_NAME.get(demo)
    if spec is None:
        raise ValueError("Unsupported demo")

    template = spec["template"]
    space_id = validate_space_id(_slugify(demo))
    installed = install_template(template, space_id=space_id)
    action = "installed"

    if demo == "demo_time_travel_restore":
        before_patch = str(read_space(space_id).get("revision_event_id") or "")
        widgets = installed.get("installed_widgets") or []
        if widgets and before_patch:
            first = widgets[0]
            patch_widget(space_id, first["id"], {"title": f"{first['title']} smoke patch"})
            restore_revision(space_id, before_patch)
            action = "restored"
    elif demo == "demo_safe_admin_recovery":
        widgets = installed.get("installed_widgets") or []
        if widgets:
            disable_widget_for_recovery(space_id, widgets[0]["id"], reason="demo smoke recovery")
            action = "recovery-disabled"

    return _space_demo_run_summary(demo, template, space_id, action=action)


def space_demo_run_all() -> dict[str, Any]:
    """Run every metadata-only Space Agent video parity smoke fixture."""
    results = [space_demo_run(item["demo"]) for item in _SPACE_DEMO_RUNS]
    passed = sum(1 for item in results if item.get("ok") is True)
    total = len(results)
    return {
        "ok": passed == total,
        "action": "space.demo.run_all",
        "mode": "metadata-only-smoke",
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "results": results,
    }


def _space_tool_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded metadata-only payload accepted by the tool adapter."""
    allowed = {"space_id", "name", "description", "agent_instructions", "instructions", "template"}
    clean = {key: payload[key] for key in allowed if key in payload}
    if isinstance(payload.get("layout"), dict):
        clean["layout"] = _payload_summary(payload["layout"])
    if isinstance(payload.get("capabilities"), dict):
        clean["capabilities"] = _payload_summary(payload["capabilities"])
    return clean


def _space_tool_current_id(payload: dict[str, Any]) -> str:
    """Return the optional current-space id from a tool payload."""
    raw = payload.get("space_id") or payload.get("active_space_id") or payload.get("current_space_id") or ""
    return str(raw or "").strip()


def run_space_tool(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dispatch a safe, Hermes-tool-shaped Capy Spaces action.

    This adapter gives future Hermes Agent tools and API callers a single small
    allowlist while preserving the current safety model: list/get responses are
    metadata-only, create ignores supplied widget/generated bodies, and widget
    mutation delegates to the existing metadata-only patch/event primitives.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    name = str(action or "").strip().lower()
    data = payload if isinstance(payload, dict) else {}

    if name in {"space.list", "space.spaces", "space.spaces.list"}:
        return {"ok": True, "action": name, "spaces": list_spaces()}
    if name in {"space.demo.list", "space.demo.runs"}:
        return {"ok": True, "action": name, "demos": list_space_demo_runs()}
    if name in {"space.demo.run", "space_demo_run"}:
        demo_name = data.get("demo") or data.get("name") or data.get("demo_name") or ""
        return {"action": name, **space_demo_run(demo_name)}
    if name in {"space.demo.run_all", "space.demo.run-all", "space_demo_run_all"}:
        return space_demo_run_all()
    if name in {"space.current", "space.current.get"}:
        current_id = _space_tool_current_id(data)
        if not current_id:
            return {"ok": True, "action": name, "active_space_id": None, "space": None}
        space_id = validate_space_id(current_id)
        return {"ok": True, "action": name, "active_space_id": space_id, "space": read_space_detail(space_id)}
    if name in {"space.current.context", "space.context", "space.current.prompt_context"}:
        current_id = _space_tool_current_id(data)
        if not current_id:
            return {"ok": True, "action": name, "active_space_id": None, "context": ""}
        space_id = validate_space_id(current_id)
        return {"ok": True, "action": name, "active_space_id": space_id, "context": build_agent_context(space_id)}
    if name in {"space.current.widgets", "space.current.widget.list"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        return {"ok": True, "action": name, "active_space_id": space_id, "widgets": list_widgets(space_id)}
    if name == "space.create":
        created = create_space(_space_tool_create_payload(data))
        space = read_space_detail(created["space_id"])
        space["widget_count"] = len(space.get("widgets") or [])
        return {"ok": True, "action": name, "space": space}
    if name == "space.get":
        space_id = validate_space_id(data.get("space_id"))
        return {"ok": True, "action": name, "space": read_space_detail(space_id)}
    if name == "widget.list":
        space_id = validate_space_id(data.get("space_id"))
        return {"ok": True, "action": name, "widgets": list_widgets(space_id)}
    if name == "widget.patch":
        space_id = validate_space_id(data.get("space_id"))
        widget_id = validate_widget_id(data.get("widget_id"))
        result = patch_widget(space_id, widget_id, data.get("patch") if isinstance(data.get("patch"), dict) else {})
        return {"ok": True, "action": name, **result}
    if name == "widget.event":
        space_id = validate_space_id(data.get("space_id"))
        widget_id = validate_widget_id(data.get("widget_id"))
        result = queue_widget_event(
            space_id,
            widget_id,
            data.get("event_name") or "agent.prompt",
            data.get("payload") if isinstance(data.get("payload"), dict) else {},
            prompt=data.get("prompt") or "",
            session_id=data.get("session_id") or "",
        )
        return {"ok": True, "action": name, **result}
    if name in {"space.camera.add_stream", "camera.add_stream"}:
        space_id = validate_space_id(data.get("space_id"))
        result = add_camera_stream(space_id, data)
        return {"ok": True, "action": name, **result}
    raise ValueError("Unsupported Capy Spaces tool action")


def list_revision_events(space_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return newest-first revision event metadata for a space.

    This is deliberately a safe history index, not a rollback executor yet: it
    exposes event type, id, timestamp, and sanitized details only. Generated
    widget bodies, renderer/html/script/data payloads, and secret-looking keys
    are omitted before returning data to recovery/detail UIs.
    """
    if not spaces_enabled():
        return []
    sid = validate_space_id(space_id)
    space = read_space(sid)
    max_events = _clamped_int(limit, 20, 1, 100)
    revision_ids = [str(event_id) for event_id in (space.get("revision_events") or []) if _event_id_is_safe(event_id)]
    summaries: list[dict[str, Any]] = []
    for event_id in reversed(revision_ids):
        if len(summaries) >= max_events:
            break
        event_path = events_dir() / f"{event_id}.json"
        try:
            event = json.loads(event_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        summary = _event_summary(event, sid)
        if summary is not None:
            summaries.append(summary)
    return summaries


def restore_revision(space_id: str, event_id: str) -> dict[str, Any]:
    """Restore a space manifest from a stored revision snapshot.

    Revision event files may contain full internal snapshots so rollback can
    preserve generated/source widget artifacts. Public responses stay
    metadata-only through read_space_detail(), and list_revision_events() ignores
    snapshots entirely.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    sid = validate_space_id(space_id)
    safe_event_id = str(event_id or "")
    if not _event_id_is_safe(safe_event_id):
        raise ValueError("Invalid event_id")
    current = read_space(sid)
    event_path = events_dir() / f"{safe_event_id}.json"
    if not event_path.exists():
        raise FileNotFoundError("Revision event not found")
    event = json.loads(event_path.read_text(encoding="utf-8"))
    if not isinstance(event, dict) or event.get("space_id") != sid:
        raise ValueError("Revision event does not belong to this space")
    snapshot = event.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("Revision snapshot is unavailable")
    restored = dict(snapshot)
    restored["space_id"] = sid
    restored.setdefault("schema_version", SCHEMA_VERSION)
    restored.setdefault("created_at", current.get("created_at") or time.time())
    if not isinstance(restored.get("widgets"), list):
        restored["widgets"] = []
    normalized_widgets: list[dict[str, Any]] = []
    for widget in restored.get("widgets") or []:
        if isinstance(widget, dict):
            normalized_widgets.append(_normalize_widget(widget))
    restored["widgets"] = normalized_widgets
    if not isinstance(restored.get("layout"), dict):
        restored["layout"] = {}
    if not isinstance(restored.get("capabilities"), dict):
        restored["capabilities"] = {}
    restored["revision_events"] = [str(rev) for rev in (restored.get("revision_events") or []) if _event_id_is_safe(rev)]
    saved = _write_manifest(restored, "space.restored", {"restored_event_id": safe_event_id})
    return {
        "ok": True,
        "space": read_space_detail(sid),
        "restored_event_id": safe_event_id,
        "revision_event_id": saved["revision_event_id"],
    }


def update_space(space_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    space = read_space(space_id)
    allowed = {"name", "description", "agent_instructions", "layout", "widgets", "capabilities", "template"}
    for key, value in (updates or {}).items():
        if key in allowed:
            if key == "widgets" and not isinstance(value, list):
                raise ValueError("widgets must be a list")
            if key in {"layout", "capabilities"} and not isinstance(value, dict):
                raise ValueError(f"{key} must be an object")
            if key == "agent_instructions":
                value = str(value or "")
            space[key] = value
    return _write_manifest(space, "space.updated", {"fields": sorted(set(updates or {}) & allowed)})


def delete_space(space_id: str) -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    sid = validate_space_id(space_id)
    path = _space_dir(sid)
    if not path.exists():
        raise FileNotFoundError("Space not found")
    event_id = _record_event(sid, "space.deleted")
    shutil.rmtree(path)
    return {"deleted": True, "space_id": sid, "revision_event_id": event_id}


def _load_yaml_mapping(text: str, label: str) -> dict[str, Any]:
    try:
        import yaml as _yaml
    except ImportError as exc:  # pragma: no cover - dependency is expected in WebUI envs
        raise RuntimeError("YAML support is unavailable") from exc
    try:
        loaded = _yaml.safe_load(str(text or ""))
    except Exception as exc:
        raise ValueError(f"Invalid {label}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} must be a mapping")
    return loaded


def _safe_zip_entry_name(name: str) -> str:
    normalized = str(name or "").replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Unsafe ZIP member path")
    return "/".join(parts)


def _space_agent_files_from_package(package: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
    if not isinstance(package, dict):
        raise ValueError("package must be an object")
    if package.get("archive_b64"):
        try:
            raw = base64.b64decode(str(package.get("archive_b64") or ""), validate=True)
        except Exception as exc:
            raise ValueError("Invalid archive_b64") from exc
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("Space Agent archive is too large")
        space_yaml = ""
        widgets: dict[str, str] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    name = _safe_zip_entry_name(info.filename)
                    lowered = name.lower()
                    if info.file_size > 512 * 1024:
                        raise ValueError("Space Agent YAML file is too large")
                    if lowered.endswith("space.yaml") or lowered.endswith("space.yml"):
                        space_yaml = archive.read(info).decode("utf-8")
                    elif "/widgets/" in f"/{lowered}" and (lowered.endswith(".yaml") or lowered.endswith(".yml")):
                        widgets[name] = archive.read(info).decode("utf-8")
        except zipfile.BadZipFile as exc:
            raise ValueError("Invalid Space Agent ZIP archive") from exc
        if not space_yaml:
            raise ValueError("Space Agent archive is missing space.yaml")
        return "space-agent-zip", space_yaml, widgets

    space_yaml = str(package.get("space_yaml") or "")
    raw_widgets = package.get("widgets_yaml") if isinstance(package.get("widgets_yaml"), dict) else package.get("widgets")
    widgets = {str(path): str(text or "") for path, text in (raw_widgets or {}).items()} if isinstance(raw_widgets, dict) else {}
    if not space_yaml:
        raise ValueError("Missing space_yaml")
    return "space-agent-yaml", space_yaml, widgets


def _widget_id_from_path(path: str) -> str:
    tail = _safe_zip_entry_name(path).rsplit("/", 1)[-1]
    stem = tail.rsplit(".", 1)[0] if "." in tail else tail
    return _slugify(stem)


def _unsafe_import_field_count(widget: dict[str, Any]) -> int:
    return sum(1 for key in widget if not _payload_key_is_safe(str(key)))


def _space_agent_widget_from_yaml(path: str, text: str) -> dict[str, Any]:
    raw = _load_yaml_mapping(text, f"widget YAML {path}")
    wid = validate_widget_id(raw.get("id") or raw.get("widget_id") or _widget_id_from_path(path))
    kind = str(raw.get("kind") or raw.get("type") or raw.get("component") or "custom")
    title = str(raw.get("title") or raw.get("name") or wid)
    widget: dict[str, Any] = {
        "id": wid,
        "kind": kind,
        "title": title,
        "layout": _normalize_widget_layout(raw.get("layout") if isinstance(raw.get("layout"), dict) else raw),
        "imported_from": {"format": "space-agent-yaml"},
    }
    omitted_count = _unsafe_import_field_count(raw)
    if omitted_count:
        unsafe_payload = {str(key): raw.get(key) for key in raw if not _payload_key_is_safe(str(key))}
        digest = hashlib.sha256(json.dumps(unsafe_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        widget["recovery"] = {
            "disabled": True,
            "disabled_reason": "imported generated source disabled pending sandbox review",
        }
        widget["untrusted_artifact"] = {
            "status": "quarantined",
            "sha256": digest,
            "omitted_field_count": omitted_count,
        }
    return widget


def import_space_agent_package(package: dict[str, Any], *, space_id: str | None = None) -> dict[str, Any]:
    """Import a Space Agent space.yaml/widgets YAML or ZIP package safely.

    This compatibility slice intentionally imports only metadata and quarantine
    markers. Generated renderer/html/script/data/source bodies and secret-looking
    fields are not copied into normal widget config or returned by list/detail
    responses; imported widgets start disabled for recovery/sandbox review.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    source_label, space_yaml, widget_files = _space_agent_files_from_package(package)
    space_doc = _load_yaml_mapping(space_yaml, "space.yaml")
    name = str(space_doc.get("name") or space_doc.get("title") or space_doc.get("id") or "Imported Space Agent Space")
    description = str(space_doc.get("description") or "Imported from Space Agent YAML package.")
    instructions = str(space_doc.get("agent_instructions") or space_doc.get("instructions") or space_doc.get("prompt") or "")
    base_id = space_doc.get("space_id") or space_doc.get("id") or name
    target_id = validate_space_id(space_id) if space_id else _unique_space_id(_slugify(str(base_id)))
    if _manifest_path(target_id).exists():
        raise FileExistsError("Space already exists")
    created = create_space(
        {
            "space_id": target_id,
            "name": name,
            "description": description,
            "agent_instructions": instructions,
            "template": "space-agent-import",
            "layout": space_doc.get("layout") if isinstance(space_doc.get("layout"), dict) else {},
            "capabilities": {"generated_rendering": "disabled", "import_review": "required"},
        }
    )
    imported_widgets: list[dict[str, Any]] = []
    for path, text in sorted(widget_files.items()):
        widget = _space_agent_widget_from_yaml(path, text)
        result = upsert_widget(created["space_id"], widget)
        imported_widgets.append(_widget_summary(result["widget"]))
    saved = read_space(created["space_id"])
    _write_manifest(
        saved,
        "space.imported.space_agent",
        {"format": source_label, "widget_count": len(imported_widgets), "status": "metadata-only"},
    )
    return {
        "imported": True,
        "source": source_label,
        "space": read_space_detail(created["space_id"]),
        "imported_widgets": imported_widgets,
    }


def _dump_yaml_mapping(payload: dict[str, Any]) -> str:
    try:
        import yaml as _yaml
    except ImportError as exc:  # pragma: no cover - dependency is expected in WebUI envs
        raise RuntimeError("YAML support is unavailable") from exc
    return _yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _space_agent_widget_export_doc(widget: dict[str, Any]) -> dict[str, Any]:
    clean = _normalize_widget(widget)
    doc: dict[str, Any] = {
        "id": clean["id"],
        "title": clean["title"],
        "type": clean["kind"],
        "layout": clean["layout"],
    }
    exportable_keys = {
        "actions",
        "attachments",
        "browser_surface",
        "cards",
        "checklist",
        "columns",
        "demos",
        "editing",
        "event_bridge",
        "interactions",
        "links",
        "market_data",
        "permissions",
        "safety",
        "series",
        "steps",
        "weather",
    }
    for key in sorted(exportable_keys):
        if key in widget and _payload_key_is_safe(key):
            doc[key] = _payload_summary(widget.get(key))
    recovery = widget.get("recovery") if isinstance(widget.get("recovery"), dict) else {}
    if recovery.get("disabled"):
        doc["recovery"] = {
            "disabled": True,
            "disabled_reason": _context_value(recovery.get("disabled_reason"), 300),
        }
    return doc


def _space_agent_yaml_export(space: dict[str, Any]) -> tuple[str, dict[str, str]]:
    space_doc = {
        "id": space.get("space_id"),
        "name": space.get("name") or space.get("space_id"),
        "description": space.get("description") or "",
        "instructions": space.get("agent_instructions") or "",
        "template": None if space.get("template") == "blank" else space.get("template"),
    }
    widgets: dict[str, str] = {}
    for widget in space.get("widgets") or []:
        if not isinstance(widget, dict):
            continue
        doc = _space_agent_widget_export_doc(widget)
        widgets[f"widgets/{doc['id']}.yaml"] = _dump_yaml_mapping(doc)
    return _dump_yaml_mapping(space_doc), widgets


def _space_agent_zip_b64(space_yaml: str, widgets: dict[str, str]) -> str:
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("space.yaml", space_yaml)
        for path, text in sorted(widgets.items()):
            archive.writestr(_safe_zip_entry_name(path), text)
    return base64.b64encode(bundle.getvalue()).decode("ascii")


def export_space_agent_package(space_id: str, *, format: str = "yaml") -> dict[str, Any]:
    """Export a Capy Space as safe Space Agent-compatible metadata.

    Exports deliberately omit generated renderer/html/script/data/source bodies
    and secret-looking fields. ZIP output contains only sanitized YAML files.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    sid = validate_space_id(space_id)
    space = read_space(sid)
    space_yaml, widgets = _space_agent_yaml_export(space)
    normalized_format = str(format or "yaml").strip().lower()
    if normalized_format in {"zip", "space-agent-zip"}:
        return {
            "source": "capy-space",
            "format": "space-agent-zip",
            "space_id": sid,
            "archive_b64": _space_agent_zip_b64(space_yaml, widgets),
            "widget_count": len(widgets),
        }
    if normalized_format not in {"yaml", "space-agent-yaml"}:
        raise ValueError("Unsupported export format")
    return {
        "source": "capy-space",
        "format": "space-agent-yaml",
        "space_id": sid,
        "space_yaml": space_yaml,
        "widgets": widgets,
        "widget_count": len(widgets),
    }


def _widget_index(space: dict[str, Any], widget_id: str) -> int:
    wid = validate_widget_id(widget_id)
    widgets = space.get("widgets") or []
    if not isinstance(widgets, list):
        raise ValueError("widgets must be a list")
    for idx, widget in enumerate(widgets):
        if isinstance(widget, dict) and widget.get("id") == wid:
            return idx
    raise FileNotFoundError("Widget not found")


def list_widgets(space_id: str) -> list[dict[str, Any]]:
    if not spaces_enabled():
        return []
    space = read_space(space_id)
    widgets = space.get("widgets") or []
    if not isinstance(widgets, list):
        raise ValueError("widgets must be a list")
    summaries: list[dict[str, Any]] = []
    for widget in widgets:
        if isinstance(widget, dict):
            summaries.append(_widget_summary(widget))
    return summaries


def read_widget(space_id: str, widget_id: str) -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    space = read_space(space_id)
    idx = _widget_index(space, widget_id)
    return dict(space["widgets"][idx])


def read_widget_detail(space_id: str, widget_id: str) -> dict[str, Any]:
    """Return safe widget metadata for public detail routes.

    Stored widgets may contain generated renderer/html/script/data bodies or
    secret-looking payloads for later sandboxed review. Public detail routes must
    expose the same metadata-only shape as list/detail APIs until an explicit
    sandboxed viewer exists.
    """
    return _widget_summary(read_widget(space_id, widget_id))


def upsert_widget(space_id: str, widget: dict[str, Any]) -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    if not isinstance(widget, dict):
        raise ValueError("widget must be an object")
    clean_widget = _normalize_widget(widget)
    wid = clean_widget["id"]

    space = read_space(space_id)
    widgets = space.get("widgets") or []
    if not isinstance(widgets, list):
        raise ValueError("widgets must be a list")
    replaced = False
    for idx, existing in enumerate(widgets):
        if isinstance(existing, dict) and existing.get("id") == wid:
            widgets[idx] = clean_widget
            replaced = True
            break
    if not replaced:
        widgets.append(clean_widget)
    space["widgets"] = widgets
    event_type = "widget.updated" if replaced else "widget.created"
    saved = _write_manifest(space, event_type, {"widget_id": wid})
    return {
        "space_id": saved["space_id"],
        "widget": clean_widget,
        "revision_event_id": saved["revision_event_id"],
    }


def _stream_title(value: Any) -> str:
    text = _context_value(value, 120)
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or _payload_text_summary(text, 120) == "[REDACTED]":
        return "Camera stream"
    return text


def _camera_stream_url_metadata(raw_url: Any) -> dict[str, Any]:
    url = str(raw_url or "").strip()
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("Camera stream URL must be http(s) with a host")
    if parsed.username or parsed.password:
        raise ValueError("Camera stream URL must not embed credentials")

    host = parsed.hostname.strip("[]").lower()
    host_class = "public"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            host_class = "private"
    except ValueError:
        if host in {"localhost"} or host.endswith((".local", ".lan", ".internal")) or "." not in host:
            host_class = "private"

    normalized = parsed._replace(fragment="").geturl()
    return {
        "scheme": scheme,
        "host_class": host_class,
        "mixed_content": scheme == "http",
        "url_digest": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24],
    }


def add_camera_stream(space_id: str, stream: dict[str, Any]) -> dict[str, Any]:
    """Append an approved camera-stream reference as metadata only.

    Raw camera URLs can contain private hosts, credentials, and connection
    details. This foundation slice validates the URL and stores only a digest and
    coarse policy metadata so recovery/detail surfaces remain safe until a later
    approved stream-secret/ref store exists.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    if not isinstance(stream, dict):
        raise ValueError("stream must be an object")

    sid = validate_space_id(space_id)
    url_meta = _camera_stream_url_metadata(stream.get("url"))
    approval_id = _payload_text_summary(stream.get("approval_id"), 120)
    approved = _truthy_bool(stream.get("approved")) or bool(approval_id and approval_id != "[REDACTED]")
    if not approved:
        raise PermissionError("Camera stream URLs require explicit approval")

    space = read_space(sid)
    idx = _widget_index(space, "camera-grid")
    widgets = list(space.get("widgets") or [])
    grid = dict(widgets[idx])
    existing = grid.get("streams") if isinstance(grid.get("streams"), list) else []
    stream_id = validate_widget_id(f"stream-{url_meta['url_digest'][:12]}")
    safe_stream = {
        "id": stream_id,
        "title": _stream_title(stream.get("title")),
        "scheme": url_meta["scheme"],
        "host_class": url_meta["host_class"],
        "mixed_content": url_meta["mixed_content"],
        "approved": True,
        "status": "approved-metadata-only",
        "url_digest": url_meta["url_digest"],
    }
    if approval_id and approval_id != "[REDACTED]":
        safe_stream["approval_id"] = approval_id

    streams = [item for item in existing if not (isinstance(item, dict) and item.get("id") == stream_id)]
    streams.append(safe_stream)
    grid["streams"] = streams
    grid["status"] = "approved-stream-metadata-ready"
    widgets[idx] = _normalize_widget(grid)
    space["widgets"] = widgets
    saved = _write_manifest(space, "camera.stream.added", {"widget_id": "camera-grid", "stream_id": stream_id})
    return {
        "space_id": saved["space_id"],
        "stream": safe_stream,
        "widget": _widget_summary(widgets[idx]),
        "revision_event_id": saved["revision_event_id"],
    }


def upsert_system_widget(space_id: str, panel: str, layout: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add/update an allowlisted trusted WebUI system widget as safe metadata."""
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    safe_panel = str(panel or "").strip()
    spec = _TRUSTED_SYSTEM_WIDGETS.get(safe_panel)
    if spec is None:
        raise ValueError("Unknown system panel")
    result = upsert_widget(
        space_id,
        {
            "id": spec["id"],
            "kind": "system",
            "title": spec["title"],
            "layout": layout or {"x": 0, "y": 0, "w": 12, "h": 6},
            "system": {"panel": safe_panel, "trusted": True},
        },
    )
    return {
        "space_id": result["space_id"],
        "widget": _widget_summary(read_widget(result["space_id"], spec["id"])),
        "revision_event_id": result["revision_event_id"],
    }


def patch_widget(space_id: str, widget_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Patch safe widget metadata without rewriting generated/source bodies.

    This is the Capy-native equivalent of a small Space Agent widget patch: it
    updates allowlisted declarative metadata while preserving any stored
    renderer/html/script/data/source artifacts for later sandboxed review. Public
    responses remain metadata-only.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    wid = validate_widget_id(widget_id)
    space = read_space(space_id)
    idx = _widget_index(space, wid)
    widgets = list(space.get("widgets") or [])
    widget = dict(widgets[idx])

    allowed = {
        "title",
        "name",
        "kind",
        "layout",
        "description",
        "metadata",
        "permissions",
        "recovery",
        "event_bridge",
        "prompt",
        "status",
        "weather",
        "chart",
        "table",
        "notes",
        "browser",
        "kanban",
        "markdown",
    }
    changed_fields: list[str] = []
    for key, value in (patch or {}).items():
        safe_key = str(key or "")
        if safe_key not in allowed or not _payload_key_is_safe(safe_key):
            continue
        if safe_key == "layout":
            widget["layout"] = _normalize_widget_layout(value)
        elif safe_key in {"metadata", "permissions", "recovery", "event_bridge", "prompt", "status", "weather", "chart", "table", "notes", "browser", "kanban", "markdown"}:
            if isinstance(value, dict):
                widget[safe_key] = _payload_summary(value)
            else:
                widget[safe_key] = _payload_summary(value)
        else:
            widget[safe_key] = _context_value(value, 500)
        changed_fields.append(safe_key)

    widget["id"] = wid
    widget = _normalize_widget(widget)
    widgets[idx] = widget
    space["widgets"] = widgets
    saved = _write_manifest(space, "widget.patched", {"widget_id": wid, "fields": sorted(set(changed_fields))})
    return {
        "space_id": saved["space_id"],
        "widget": _widget_summary(widget),
        "revision_event_id": saved["revision_event_id"],
    }



def delete_widget(space_id: str, widget_id: str) -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    wid = validate_widget_id(widget_id)
    space = read_space(space_id)
    idx = _widget_index(space, wid)
    widgets = list(space.get("widgets") or [])
    widgets.pop(idx)
    space["widgets"] = widgets
    saved = _write_manifest(space, "widget.deleted", {"widget_id": wid})
    return {
        "deleted": True,
        "space_id": saved["space_id"],
        "widget_id": wid,
        "revision_event_id": saved["revision_event_id"],
    }


def _weather_demo_widget() -> dict[str, Any]:
    """Return the safe declarative weather demo widget seed.

    This is intentionally metadata/declarative state only. It does not include
    renderer/html/script bodies; later refresh tooling can fill live weather data
    through typed APIs without exposing generated code through list/detail views.
    """
    return {
        "id": "weather-current",
        "kind": "weather",
        "title": "Weather in Prague",
        "layout": {"x": 0, "y": 0, "w": 8, "h": 5, "minimized": False},
        "weather": {
            "location": "Prague",
            "country": "CZ",
            "units": "metric",
            "status": "ready-for-agent-refresh",
        },
        "permissions": {"network": "agent-mediated"},
    }


def _research_harness_widgets() -> list[dict[str, Any]]:
    """Return safe declarative research harness widget seeds.

    This starter maps the Space Agent demo's research workflow into metadata-only
    Capy widgets: a prompt/event entry point plus plan, citations, notes, and
    summary widgets that later agent runs can update through typed space APIs.
    """
    return [
        {
            "id": "research-query",
            "kind": "prompt",
            "title": "Research query",
            "layout": {"x": 0, "y": 0, "w": 8, "h": 4, "minimized": False},
            "event_bridge": {"event_name": "agent.prompt", "status": "ready-for-user-confirmation"},
            "prompt": {
                "placeholder": "Research a topic and update the harness widgets",
                "suggested_event": "agent.prompt",
            },
        },
        {
            "id": "research-plan",
            "kind": "status",
            "title": "Plan",
            "layout": {"x": 8, "y": 0, "w": 8, "h": 4, "minimized": False},
            "status": {"phase": "ready", "message": "Waiting for a confirmed research prompt."},
        },
        {
            "id": "research-sources",
            "kind": "table",
            "title": "Sources",
            "layout": {"x": 16, "y": 0, "w": 8, "h": 6, "minimized": False},
            "columns": ["title", "url", "notes"],
            "permissions": {"network": "agent-mediated"},
        },
        {
            "id": "research-notes",
            "kind": "markdown",
            "title": "Research notes",
            "layout": {"x": 0, "y": 4, "w": 12, "h": 8, "minimized": False},
            "content_status": "agent-managed-empty",
        },
        {
            "id": "research-summary",
            "kind": "markdown",
            "title": "Summary report",
            "layout": {"x": 12, "y": 6, "w": 12, "h": 8, "minimized": False},
            "content_status": "agent-managed-empty",
            "export": {"pdf": "planned"},
        },
    ]


def _dashboard_demo_widgets() -> list[dict[str, Any]]:
    """Return safe declarative daily dashboard widget seeds.

    This starter covers the demo-parity path for prebuilt prices, news, and
    daily dashboard surfaces without introducing live network fetches or
    generated renderer bodies. Agents can later refresh these widgets through
    typed space APIs while preserving safe list/detail responses.
    """
    return [
        {
            "id": "dashboard-prices",
            "kind": "chart",
            "title": "Market prices",
            "layout": {"x": 0, "y": 0, "w": 8, "h": 5, "minimized": False},
            "series": ["NVDA", "AAPL", "GOOGL"],
            "refresh": {"mode": "agent-mediated", "status": "ready-for-agent-refresh"},
            "permissions": {"network": "agent-mediated"},
        },
        {
            "id": "dashboard-news",
            "kind": "news",
            "title": "News brief",
            "layout": {"x": 8, "y": 0, "w": 8, "h": 5, "minimized": False},
            "topics": ["markets", "ai", "local ops"],
            "refresh": {"mode": "agent-mediated", "status": "ready-for-agent-refresh"},
            "permissions": {"network": "agent-mediated"},
        },
        {
            "id": "dashboard-agenda",
            "kind": "checklist",
            "title": "Daily agenda",
            "layout": {"x": 16, "y": 0, "w": 8, "h": 5, "minimized": False},
            "items_status": "agent-managed-empty",
        },
        {
            "id": "dashboard-brief",
            "kind": "markdown",
            "title": "Daily brief",
            "layout": {"x": 0, "y": 5, "w": 16, "h": 7, "minimized": False},
            "content_status": "agent-managed-empty",
            "export": {"markdown": "planned"},
        },
    ]


def _camera_dashboard_widgets() -> list[dict[str, Any]]:
    """Return safe declarative camera dashboard widget seeds.

    This starter maps the Space Agent camera/video dashboard demo into
    metadata-only widgets. It intentionally starts with no configured streams;
    private camera URLs and live network access must be supplied later through
    explicit approval and agent-mediated typed APIs.
    """
    return [
        {
            "id": "camera-grid",
            "kind": "camera-grid",
            "title": "Camera grid",
            "layout": {"x": 0, "y": 0, "w": 16, "h": 10, "minimized": False},
            "streams": [],
            "stream_policy": {
                "network": "explicit-approval",
                "private_urls": "approval-required",
                "mixed_content": "blocked-by-default",
            },
            "status": "awaiting-approved-streams",
        },
        {
            "id": "camera-permissions",
            "kind": "status",
            "title": "Stream permissions",
            "layout": {"x": 16, "y": 0, "w": 8, "h": 5, "minimized": False},
            "permissions": {
                "network": "explicit-approval",
                "camera_urls": "agent-mediated",
            },
            "review": "No stream URLs are stored by default; add sources only after explicit approval.",
        },
        {
            "id": "camera-incidents",
            "kind": "table",
            "title": "Incident notes",
            "layout": {"x": 16, "y": 5, "w": 8, "h": 5, "minimized": False},
            "columns": ["time", "camera", "note", "status"],
            "rows": [],
            "entry_mode": "metadata-only",
        },
    ]


def _kanban_board_widgets() -> list[dict[str, Any]]:
    """Return safe declarative Kanban board widget seeds.

    This starter maps the demo's colorful Trello-style board into metadata-only
    columns and cards. Drag/drop and inline editing are declared as planned
    interactions, not executable renderer code.
    """
    column_interaction = {"drag_drop": "planned", "edit_cards": "metadata-only"}
    return [
        {
            "id": "kanban-backlog",
            "kind": "kanban-column",
            "title": "Backlog",
            "layout": {"x": 0, "y": 0, "w": 8, "h": 8, "minimized": False},
            "color": "#7dd3fc",
            "cards": [{"id": "card-plan", "title": "Plan the first task", "status": "todo"}],
            "interaction": column_interaction,
        },
        {
            "id": "kanban-doing",
            "kind": "kanban-column",
            "title": "Doing",
            "layout": {"x": 8, "y": 0, "w": 8, "h": 8, "minimized": False},
            "color": "#fbbf24",
            "cards": [],
            "interaction": column_interaction,
        },
        {
            "id": "kanban-done",
            "kind": "kanban-column",
            "title": "Done",
            "layout": {"x": 16, "y": 0, "w": 8, "h": 8, "minimized": False},
            "color": "#86efac",
            "cards": [],
            "interaction": column_interaction,
        },
        {
            "id": "kanban-notes",
            "kind": "markdown",
            "title": "Board notes",
            "layout": {"x": 0, "y": 8, "w": 24, "h": 4, "minimized": False},
            "content_status": "agent-managed-empty",
        },
    ]


def _notes_app_widgets() -> list[dict[str, Any]]:
    """Return safe declarative notes app widget seeds.

    This starter maps the Space Agent notes demo into metadata-only widgets:
    folders, editor, preview, and attachments. Rich editing and attachment
    handling are declared as planned capabilities, not executable renderer code.
    """
    return [
        {
            "id": "notes-folders",
            "kind": "folder-list",
            "title": "Folders",
            "layout": {"x": 0, "y": 0, "w": 5, "h": 10, "minimized": False},
            "folders": [{"id": "folder-inbox", "title": "Inbox"}],
            "interaction": {"rename": "planned", "create_folder": "metadata-only"},
        },
        {
            "id": "notes-editor",
            "kind": "rich-text-editor",
            "title": "Editor",
            "layout": {"x": 5, "y": 0, "w": 11, "h": 10, "minimized": False},
            "editing": {"wysiwyg": "planned", "markdown_mode": "planned", "copy_paste": "metadata-only"},
            "content_status": "agent-managed-empty",
        },
        {
            "id": "notes-preview",
            "kind": "markdown",
            "title": "Markdown preview",
            "layout": {"x": 16, "y": 0, "w": 8, "h": 10, "minimized": False},
            "content_status": "agent-managed-empty",
        },
        {
            "id": "notes-attachments",
            "kind": "attachment-list",
            "title": "Attachments",
            "layout": {"x": 0, "y": 10, "w": 24, "h": 4, "minimized": False},
            "attachments": {"images": "planned", "files": "planned", "storage": "agent-mediated"},
            "permissions": {"filesystem": "agent-mediated"},
        },
    ]


def _stock_chart_widgets() -> list[dict[str, Any]]:
    """Return safe declarative stock chart widget seeds.

    This starter maps the Space Agent demo's stock graph into metadata-only
    chart/watchlist widgets. Live market fetches are agent-mediated later, not
    embedded as browser-executable renderer code or secret-bearing API config.
    """
    return [
        {
            "id": "stock-chart",
            "kind": "chart",
            "title": "NVDA / AAPL / GOOGL",
            "layout": {"x": 0, "y": 0, "w": 16, "h": 8, "minimized": False},
            "series": ["NVDA", "AAPL", "GOOGL"],
            "market_data": {
                "provider": "agent-mediated",
                "status": "ready-for-agent-refresh",
                "range": "1mo",
            },
            "permissions": {"network": "agent-mediated"},
        },
        {
            "id": "stock-watchlist",
            "kind": "table",
            "title": "Watchlist",
            "layout": {"x": 16, "y": 0, "w": 8, "h": 8, "minimized": False},
            "columns": ["symbol", "last", "change", "notes"],
            "symbols": ["NVDA", "AAPL", "GOOGL"],
            "refresh": {"mode": "agent-mediated", "status": "ready-for-agent-refresh"},
            "permissions": {"network": "agent-mediated"},
        },
        {
            "id": "stock-notes",
            "kind": "markdown",
            "title": "Market notes",
            "layout": {"x": 0, "y": 8, "w": 24, "h": 4, "minimized": False},
            "content_status": "agent-managed-empty",
        },
    ]


def _browser_surface_widgets() -> list[dict[str, Any]]:
    """Return safe declarative browser-surface widget seeds.

    This starter captures the Space Agent browser panel parity path as metadata
    only. It declares an inspectable/co-controllable browser surface and planned
    control primitives without embedding executable page, renderer, or credential
    material in list/detail responses.
    """
    return [
        {
            "id": "browser-panel",
            "kind": "browser-surface",
            "title": "Shared browser panel",
            "layout": {"x": 0, "y": 0, "w": 16, "h": 10, "minimized": False},
            "browser_surface": {
                "target": "about:blank",
                "control": "user-and-agent",
                "inspection": "metadata-only",
                "bridge": "planned-cdp",
            },
            "permissions": {"network": "explicit-approval", "browser_control": "agent-mediated"},
        },
        {
            "id": "browser-controls",
            "kind": "browser-controls",
            "title": "Agent controls",
            "layout": {"x": 16, "y": 0, "w": 8, "h": 5, "minimized": False},
            "actions": ["open_url", "snapshot", "click_ref", "type_ref"],
            "permissions": {"network": "explicit-approval", "browser_control": "agent-mediated"},
        },
        {
            "id": "browser-notes",
            "kind": "markdown",
            "title": "Browser notes",
            "layout": {"x": 16, "y": 5, "w": 8, "h": 5, "minimized": False},
            "content_status": "agent-managed-empty",
        },
    ]


def _local_service_dashboard_widgets() -> list[dict[str, Any]]:
    """Return safe declarative local-service dashboard widget seeds.

    This starter maps the Space Agent local-agent/service dashboard demo into
    metadata-only widgets: an API connector, a shared browser panel, health
    checks, and a settings review table. Local URLs, auth headers, API keys, and
    provider secrets must be configured outside widget metadata and used only
    through explicit approval / typed agent mediation.
    """
    return [
        {
            "id": "service-api-chat",
            "kind": "api-connector",
            "title": "Service API chat",
            "layout": {"x": 0, "y": 0, "w": 10, "h": 6, "minimized": False},
            "connector": {
                "target": "local-service",
                "auth": "configured-outside-widget",
                "mode": "agent-mediated",
            },
            "actions": ["send_message", "inspect_status", "summarize_response"],
            "permissions": {"network": "explicit-approval", "secrets": "never-store-in-widget"},
        },
        {
            "id": "service-browser-panel",
            "kind": "browser-surface",
            "title": "Service browser panel",
            "layout": {"x": 10, "y": 0, "w": 14, "h": 8, "minimized": False},
            "browser_surface": {
                "target": "about:blank",
                "control": "user-and-agent",
                "inspection": "metadata-only",
                "bridge": "planned-cdp",
            },
            "permissions": {"network": "explicit-approval", "browser_control": "agent-mediated"},
        },
        {
            "id": "service-health",
            "kind": "status",
            "title": "Health checks",
            "layout": {"x": 0, "y": 6, "w": 10, "h": 4, "minimized": False},
            "checks": ["/health", "api/status", "browser-root"],
            "refresh": {"mode": "agent-mediated", "status": "awaiting-approved-service"},
            "permissions": {"network": "explicit-approval"},
        },
        {
            "id": "service-settings-review",
            "kind": "table",
            "title": "Settings review",
            "layout": {"x": 10, "y": 8, "w": 14, "h": 4, "minimized": False},
            "columns": ["setting", "status", "notes"],
            "rows": [
                {"setting": "endpoint", "status": "not-configured", "notes": "add after explicit approval"},
                {"setting": "auth", "status": "external", "notes": "never store secrets in widgets"},
            ],
            "entry_mode": "metadata-only",
        },
    ]


def _model_provider_setup_widgets() -> list[dict[str, Any]]:
    """Return safe declarative model/provider setup widget seeds."""
    return [
        {
            "id": "model-provider-status",
            "kind": "status",
            "title": "Provider status",
            "layout": {"x": 0, "y": 0, "w": 10, "h": 5, "minimized": False},
            "provider_setup": {
                "mode": "configured-outside-widget",
                "secret_storage": "never-store-in-widget",
                "targets": ["Hermes profiles", "LM Studio", "OpenAI-compatible providers"],
            },
            "checks": ["profile-selected", "provider-config-present", "runtime-reachable"],
            "permissions": {"configuration": "trusted-shell", "network": "explicit-approval"},
        },
        {
            "id": "model-local-runtime",
            "kind": "local-runtime",
            "title": "Local runtime",
            "layout": {"x": 10, "y": 0, "w": 14, "h": 5, "minimized": False},
            "local_runtime": {
                "engine": "LM Studio",
                "status": "external-service-review",
                "model_loading": "agent-mediated-with-approval",
            },
            "runtime_checks": ["server-status", "loaded-models", "context-window"],
            "permissions": {"local_process": "review-only", "model_loading": "approval-required"},
        },
        {
            "id": "model-settings-review",
            "kind": "table",
            "title": "Settings review",
            "layout": {"x": 0, "y": 5, "w": 14, "h": 5, "minimized": False},
            "columns": ["setting", "status", "notes"],
            "rows": [
                {"setting": "profile", "status": "external", "notes": "review in trusted settings"},
                {"setting": "provider", "status": "external", "notes": "do not copy auth material into widgets"},
                {"setting": "model", "status": "agent-mediated", "notes": "load only after approval"},
            ],
            "entry_mode": "metadata-only",
        },
        {
            "id": "model-next-steps",
            "kind": "checklist",
            "title": "Next steps",
            "layout": {"x": 14, "y": 5, "w": 10, "h": 5, "minimized": False},
            "items": [
                {"id": "open-settings", "title": "Open trusted provider settings", "status": "ready"},
                {"id": "review-runtime", "title": "Check LM Studio runtime", "status": "suggested"},
                {"id": "test-chat", "title": "Run an approved test prompt", "status": "planned"},
            ],
            "interaction": {"setup": "trusted-shell", "runtime_actions": "agent-mediated"},
        },
    ]


def _game_sandbox_widgets() -> list[dict[str, Any]]:
    """Return safe declarative canvas-game widget seeds.

    This starter maps the Space Agent snake/game demo into metadata-only Capy
    widgets. Executable game renderer code stays disabled until a sandboxed
    viewer, keyboard focus isolation, cleanup hooks, and rollback tests exist.
    """
    return [
        {
            "id": "game-canvas",
            "kind": "canvas-game",
            "title": "Snake game sandbox",
            "layout": {"x": 0, "y": 0, "w": 16, "h": 10, "minimized": False},
            "game": "snake",
            "input_policy": {
                "keyboard_focus": "explicit-click",
                "global_keys": "blocked",
                "cleanup": "planned",
            },
            "rendering": {"mode": "metadata-only", "sandbox": "planned"},
            "permissions": {"generated_rendering": "disabled", "keyboard": "explicit-focus"},
        },
        {
            "id": "game-controls",
            "kind": "status",
            "title": "Game controls",
            "layout": {"x": 16, "y": 0, "w": 8, "h": 5, "minimized": False},
            "actions": ["start", "pause", "reset", "report-bug"],
            "interaction": {"controls": "planned-metadata", "bug_reports": "agent-mediated"},
            "permissions": {"generated_rendering": "disabled"},
        },
        {
            "id": "game-repair-notes",
            "kind": "markdown",
            "title": "Repair notes",
            "layout": {"x": 16, "y": 5, "w": 8, "h": 5, "minimized": False},
            "content_status": "agent-managed-empty",
            "repair_loop": {"iterative_patch": "planned", "rollback": "revision-history"},
        },
    ]


def _music_sequencer_widgets() -> list[dict[str, Any]]:
    """Return safe declarative music/sequencer widget seeds.

    This starter maps the Space Agent step-sequencer/piano-roll demo into
    metadata-only Capy widgets. WebAudio, keyboard capture, generated renderer
    code, and cleanup hooks remain planned until explicit sandbox tests exist.
    """
    return [
        {
            "id": "music-sequencer-grid",
            "kind": "step-sequencer",
            "title": "Step sequencer",
            "layout": {"x": 0, "y": 0, "w": 14, "h": 8, "minimized": False},
            "pattern_status": "metadata-only-empty",
            "audio_policy": {
                "permission": "explicit-user-gesture",
                "webaudio": "disabled-until-approved",
                "cleanup": "planned-on-rerender",
            },
            "permissions": {"audio": "explicit-approval", "generated_rendering": "disabled"},
        },
        {
            "id": "music-synth-controls",
            "kind": "audio-controls",
            "title": "Synth controls",
            "layout": {"x": 14, "y": 0, "w": 10, "h": 4, "minimized": False},
            "controls_status": "metadata-only-defaults",
            "audio_policy": {"permission": "explicit-user-gesture", "webaudio": "disabled-until-approved"},
            "permissions": {"audio": "explicit-approval", "generated_rendering": "disabled"},
        },
        {
            "id": "music-piano-roll",
            "kind": "piano-roll",
            "title": "Piano roll",
            "layout": {"x": 0, "y": 8, "w": 18, "h": 6, "minimized": False},
            "interaction": {"keyboard": "explicit-focus", "editing": "planned-metadata"},
            "audio_policy": {"permission": "explicit-user-gesture", "cleanup": "planned-on-rerender"},
            "permissions": {"audio": "explicit-approval", "keyboard": "explicit-focus"},
        },
        {
            "id": "music-notes",
            "kind": "markdown",
            "title": "Music notes",
            "layout": {"x": 18, "y": 8, "w": 6, "h": 6, "minimized": False},
            "content_status": "agent-managed-empty",
            "repair_loop": {"resize_cleanup": "planned", "rollback": "revision-history"},
        },
    ]


def _big_bang_onboarding_widgets() -> list[dict[str, Any]]:
    """Return safe declarative Big Bang onboarding widget seeds.

    The first-run space should demonstrate what Capy Spaces can do without
    enabling generated renderer execution. It links the existing demo templates,
    documents safety defaults, and gives Capy/user next-step metadata to drive
    future agent-mediated setup.
    """
    return [
        {
            "id": "bigbang-welcome",
            "kind": "markdown",
            "title": "Welcome to Capy Spaces",
            "layout": {"x": 0, "y": 0, "w": 12, "h": 5, "minimized": False},
            "content_status": "curated-metadata",
            "summary": "First-run tour for persistent, recoverable, metadata-only spaces.",
        },
        {
            "id": "bigbang-demo-launcher",
            "kind": "checklist",
            "title": "Demo launchers",
            "layout": {"x": 12, "y": 0, "w": 12, "h": 5, "minimized": False},
            "demo_templates": [
                "weather",
                "research",
                "dashboard",
                "camera",
                "kanban",
                "notes",
                "browser",
                "stock",
                "game",
                "music",
            ],
            "items": [
                {"id": "try-weather", "title": "Install the Weather Demo", "status": "suggested"},
                {"id": "try-research", "title": "Open the Research Harness", "status": "suggested"},
                {"id": "try-dashboard", "title": "Install the Daily Dashboard", "status": "suggested"},
                {"id": "try-camera", "title": "Review Camera Dashboard safety planning", "status": "suggested"},
                {"id": "try-browser", "title": "Preview Browser Surface planning", "status": "suggested"},
                {"id": "try-music", "title": "Review Music Sequencer sandbox planning", "status": "suggested"},
            ],
            "interaction": {"install_templates": "agent-mediated", "preview": "metadata-only"},
        },
        {
            "id": "bigbang-safety",
            "kind": "status",
            "title": "Safety guardrails",
            "layout": {"x": 0, "y": 5, "w": 12, "h": 4, "minimized": False},
            "safety": {
                "generated_code": "disabled-by-default",
                "recovery": "available",
                "rollback": "revision-history-planned",
            },
            "permissions": {"generated_rendering": "disabled", "network": "agent-mediated"},
        },
        {
            "id": "bigbang-next-steps",
            "kind": "checklist",
            "title": "Next steps",
            "layout": {"x": 12, "y": 5, "w": 12, "h": 4, "minimized": False},
            "items": [
                {"id": "activate-chat", "title": "Use this space in chat", "status": "ready"},
                {"id": "ask-capy", "title": "Ask Capy to customize widgets", "status": "ready"},
                {"id": "review-recovery", "title": "Review recovery and revision history", "status": "planned"},
            ],
        },
    ]


def install_template(template: str, *, space_id: str | None = None) -> dict[str, Any]:
    """Install a safe Capy Spaces demo template.

    Templates are early demo-parity seeds. They create/update persistent spaces
    and widgets using the same validated storage primitives as normal mutations,
    while returning only metadata-safe detail/list payloads.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    template_name = str(template or "").strip().lower()
    if template_name not in {"weather", "weather-demo", "research", "research-harness", "dashboard", "daily-dashboard", "camera", "camera-dashboard", "kanban", "kanban-board", "notes", "notes-app", "browser", "browser-surface", "stock", "stock-chart", "stocks", "service", "service-dashboard", "local-service", "local-service-dashboard", "agent-zero", "agent-zero-dashboard", "model", "model-setup", "model-provider", "model-provider-setup", "provider-setup", "game", "game-sandbox", "snake", "snake-game", "music", "music-sequencer", "sequencer", "step-sequencer", "synth", "piano-roll", "big-bang", "bigbang", "onboarding", "big-bang-onboarding"}:
        raise ValueError("Unsupported template")

    if template_name in {"weather", "weather-demo"}:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("weather-demo")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Weather Demo",
                    "description": "Persistent Prague weather widget starter for the Space Agent demo parity path.",
                    "agent_instructions": "Keep the weather widget declarative. Use typed Capy space APIs for updates and preserve revision history.",
                    "template": "weather-demo",
                }
            )
        widgets = [_weather_demo_widget()]
        response_template = "weather"
    elif template_name in {"research", "research-harness"}:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("research-harness")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Research Harness",
                    "description": "Metadata-only starter for the Space Agent research workflow: prompt, plan, citations, notes, and summary.",
                    "agent_instructions": "Use widget-to-agent events for confirmed prompts. Update research widgets through typed Capy space APIs, cite sources, and preserve revision history.",
                    "template": "research-harness",
                }
            )
        widgets = _research_harness_widgets()
        response_template = "research"
    elif template_name in {"dashboard", "daily-dashboard"}:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("daily-dashboard")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Daily Dashboard",
                    "description": "Metadata-only starter for prices, news, agenda, and daily briefing widgets.",
                    "agent_instructions": "Keep dashboard widgets declarative. Refresh data through typed Capy space APIs, cite sources, and preserve revision history.",
                    "template": "daily-dashboard",
                }
            )
        widgets = _dashboard_demo_widgets()
        response_template = "dashboard"
    elif template_name in {"camera", "camera-dashboard"}:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("camera-dashboard")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Camera Dashboard",
                    "description": "Metadata-only starter for reviewing approved camera streams, permissions, and incident notes.",
                    "agent_instructions": "Keep camera widgets declarative. Do not store or fetch stream URLs without explicit approval; use typed Capy space APIs and preserve revision history.",
                    "template": "camera-dashboard",
                }
            )
        widgets = _camera_dashboard_widgets()
        response_template = "camera"
    elif template_name in {"kanban", "kanban-board"}:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("kanban-board")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Kanban Board",
                    "description": "Metadata-only starter for a Trello-style board with persistent columns and cards.",
                    "agent_instructions": "Keep board updates declarative. Use typed Capy space APIs for cards/columns and preserve revision history.",
                    "template": "kanban-board",
                }
            )
        widgets = _kanban_board_widgets()
        response_template = "kanban"
    elif template_name in {"browser", "browser-surface"}:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("browser-surface")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Browser Surface",
                    "description": "Metadata-only starter for an inspectable browser panel with planned user and agent co-control.",
                    "agent_instructions": "Keep browser surfaces declarative. Require explicit approval for navigation/control and preserve revision history.",
                    "template": "browser-surface",
                }
            )
        widgets = _browser_surface_widgets()
        response_template = "browser"
    elif template_name in {"stock", "stock-chart", "stocks"}:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("stock-chart")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Stock Chart",
                    "description": "Metadata-only starter for market chart, watchlist, and notes widgets.",
                    "agent_instructions": "Keep stock widgets declarative. Refresh market data through agent-mediated typed Capy space APIs, cite sources, and preserve revision history.",
                    "template": "stock-chart",
                }
            )
        widgets = _stock_chart_widgets()
        response_template = "stock"
    elif template_name in {"service", "service-dashboard", "local-service", "local-service-dashboard", "agent-zero", "agent-zero-dashboard"}:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("local-service-dashboard")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Local Service Dashboard",
                    "description": "Metadata-only starter for local service API chat, browser review, health checks, and settings review.",
                    "agent_instructions": "Keep service widgets declarative. Configure auth outside widget manifests, require explicit network approval, and preserve revision history.",
                    "template": "local-service-dashboard",
                }
            )
        widgets = _local_service_dashboard_widgets()
        response_template = "service"
    elif template_name in {"model", "model-setup", "model-provider", "model-provider-setup", "provider-setup"}:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("model-provider-setup")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Model Provider Setup",
                    "description": "Metadata-only starter for provider selection, local runtime review, settings checks, and setup next steps.",
                    "agent_instructions": "Keep provider setup declarative. Configure auth material outside widget manifests, require explicit approval for runtime actions, and preserve revision history.",
                    "template": "model-provider-setup",
                }
            )
        widgets = _model_provider_setup_widgets()
        response_template = "model-setup"
    elif template_name in {"game", "game-sandbox", "snake", "snake-game"}:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("game-sandbox")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Game Sandbox",
                    "description": "Metadata-only starter for the snake/canvas game demo with explicit keyboard focus and recovery planning.",
                    "agent_instructions": "Keep game widgets declarative until sandboxed rendering is approved. Require explicit keyboard focus, preserve revision history, and use bug-report events for iterative repair.",
                    "template": "game-sandbox",
                }
            )
        widgets = _game_sandbox_widgets()
        response_template = "game"
    elif template_name in {"music", "music-sequencer", "sequencer", "step-sequencer", "synth", "piano-roll"}:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("music-sequencer")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Music Sequencer",
                    "description": "Metadata-only starter for WebAudio sequencer, synth controls, piano roll, and repair notes.",
                    "agent_instructions": "Keep music widgets declarative until sandboxed WebAudio is approved. Require explicit audio permission and keyboard focus, preserve revision history, and clean up on rerender.",
                    "template": "music-sequencer",
                }
            )
        widgets = _music_sequencer_widgets()
        response_template = "music"
    elif template_name in {"big-bang", "bigbang", "onboarding", "big-bang-onboarding"}:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("big-bang-onboarding")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Big Bang Onboarding",
                    "description": "Metadata-only first-run tour for Capy Spaces demos, safety guardrails, and next steps.",
                    "agent_instructions": "Use this onboarding space to explain Capy Spaces, install demo templates on request, keep generated code disabled by default, and preserve revision history.",
                    "template": "big-bang-onboarding",
                }
            )
        widgets = _big_bang_onboarding_widgets()
        response_template = "big-bang"
    else:
        target_id = validate_space_id(space_id) if space_id else _unique_space_id("notes-app")
        if _manifest_path(target_id).exists():
            space = read_space(target_id)
        else:
            space = create_space(
                {
                    "space_id": target_id,
                    "name": "Notes App",
                    "description": "Metadata-only starter for folders, rich-text editing, markdown preview, and attachments.",
                    "agent_instructions": "Keep notes widgets declarative. Use typed Capy space APIs for folders, note bodies, and attachments; preserve revision history.",
                    "template": "notes-app",
                }
            )
        widgets = _notes_app_widgets()
        response_template = "notes"

    for widget in widgets:
        upsert_widget(space["space_id"], widget)
    return {
        "template": response_template,
        "space": read_space_detail(space["space_id"]),
        "installed_widgets": list_widgets(space["space_id"]),
    }


def reset_template(template: str, *, space_id: str | None = None) -> dict[str, Any]:
    """Reset an installed demo template to its canonical metadata-only state.

    This is intentionally narrower than install_template: reset replaces the
    target Space metadata and widget list with the trusted template definition,
    dropping any generated/unsafe extra widgets from the active manifest while
    preserving normal revision history for rollback/audit.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    template_name = str(template or "").strip().lower()
    if template_name not in {"big-bang", "bigbang", "onboarding", "big-bang-onboarding"}:
        raise ValueError("Unsupported template")

    sid = validate_space_id(space_id) if space_id else "big-bang-onboarding"
    now = time.time()
    if _manifest_path(sid).exists():
        existing = read_space(sid)
    else:
        existing = {"space_id": sid, "created_at": now, "revision_events": []}

    space = dict(existing)
    space.update(
        {
            "schema_version": SCHEMA_VERSION,
            "space_id": sid,
            "name": "Big Bang Onboarding",
            "description": "Metadata-only first-run tour for Capy Spaces demos, safety guardrails, and next steps.",
            "agent_instructions": "Use this onboarding space to explain Capy Spaces, install demo templates on request, keep generated code disabled by default, and preserve revision history.",
            "template": "big-bang-onboarding",
            "layout": {"columns": 24},
            "capabilities": {
                "demo_launchers": True,
                "generated_code": "disabled-by-default",
                "recovery": "available",
            },
            "recovery": {"safe_mode_available": True},
            "widgets": [_normalize_widget(widget) for widget in _big_bang_onboarding_widgets()],
        }
    )
    space.setdefault("created_at", existing.get("created_at") or now)
    space["revision_events"] = [
        str(rev) for rev in (existing.get("revision_events") or []) if _event_id_is_safe(rev)
    ]
    _write_manifest(space, "template.reset", {"template": "big-bang"})
    return {
        "template": "big-bang",
        "reset": True,
        "space": read_space_detail(sid),
        "installed_widgets": list_widgets(sid),
    }


def disable_widget_for_recovery(space_id: str, widget_id: str, *, reason: str = "") -> dict[str, Any]:
    """Mark a widget disabled from safe recovery without deleting its source.

    The normal widget manifest keeps renderer/data for later repair or rollback,
    while recovery/list APIs expose only safe metadata. This gives the recovery
    panel an escape hatch for broken generated widgets without losing the
    evidence needed to fix them.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    wid = validate_widget_id(widget_id)
    space = read_space(space_id)
    idx = _widget_index(space, wid)
    widgets = list(space.get("widgets") or [])
    widget = dict(widgets[idx])
    recovery = widget.get("recovery") if isinstance(widget.get("recovery"), dict) else {}
    recovery = dict(recovery)
    recovery["disabled"] = True
    recovery["disabled_reason"] = _context_value(reason or "disabled from recovery", 300)
    widget["recovery"] = recovery
    widgets[idx] = widget
    space["widgets"] = widgets
    saved = _write_manifest(space, "widget.recovery_disabled", {"widget_id": wid, "reason": recovery["disabled_reason"]})
    return {
        "disabled": True,
        "space_id": saved["space_id"],
        "widget_id": wid,
        "revision_event_id": saved["revision_event_id"],
    }


def enable_widget_for_recovery(space_id: str, widget_id: str, *, reason: str = "") -> dict[str, Any]:
    """Re-enable a widget from safe recovery without exposing or executing its source."""
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    wid = validate_widget_id(widget_id)
    space = read_space(space_id)
    idx = _widget_index(space, wid)
    widgets = list(space.get("widgets") or [])
    widget = dict(widgets[idx])
    recovery = widget.get("recovery") if isinstance(widget.get("recovery"), dict) else {}
    recovery = dict(recovery)
    recovery["disabled"] = False
    recovery["disabled_reason"] = ""
    widget["recovery"] = recovery
    widgets[idx] = widget
    space["widgets"] = widgets
    detail_reason = _context_value(reason or "enabled from recovery", 300)
    saved = _write_manifest(space, "widget.recovery_enabled", {"widget_id": wid, "reason": detail_reason})
    return {
        "disabled": False,
        "space_id": saved["space_id"],
        "widget_id": wid,
        "revision_event_id": saved["revision_event_id"],
    }


def _widget_event_summary(event: dict[str, Any], sid: str, widget_id: str | None = None) -> dict[str, Any] | None:
    event_id = str(event.get("event_id") or "")
    if not _event_id_is_safe(event_id) or event.get("space_id") != sid:
        return None
    if _context_value(event.get("event_type"), 120) != "widget.event.queued":
        return None
    details = _payload_summary(event.get("details") or {})
    if not isinstance(details, dict):
        return None
    wid = _context_value(details.get("widget_id"), 120)
    if not wid or (widget_id and wid != widget_id):
        return None
    payload_summary = details.get("payload_summary") if isinstance(details.get("payload_summary"), dict) else {}
    return {
        "schema_version": event.get("schema_version", SCHEMA_VERSION),
        "event_id": event_id,
        "space_id": sid,
        "widget_id": wid,
        "event_name": _context_value(details.get("event_name"), 120),
        "status": _context_value(details.get("status") or "queued", 80),
        "prompt_preview": _payload_text_summary(details.get("prompt_preview"), 1000),
        "payload_summary": payload_summary,
        "created_at": event.get("created_at"),
    }


def list_widget_events(space_id: str, widget_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Return newest-first metadata-only queued widget events for a space/widget."""
    if not spaces_enabled():
        return []
    sid = validate_space_id(space_id)
    space = read_space(sid)
    wid = validate_widget_id(widget_id) if widget_id else None
    if wid:
        _widget_index(space, wid)
    max_events = _clamped_int(limit, 20, 1, 100)
    summaries: list[dict[str, Any]] = []
    _ensure_dirs()
    for event_path in sorted(events_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if len(summaries) >= max_events:
            break
        try:
            event = json.loads(event_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        summary = _widget_event_summary(event, sid, wid)
        if summary is not None:
            summaries.append(summary)
    summaries.sort(key=lambda event: float(event.get("created_at") or 0), reverse=True)
    return summaries[:max_events]


def queue_widget_event(
    space_id: str,
    widget_id: str,
    event_name: str = "agent.prompt",
    payload: dict[str, Any] | None = None,
    *,
    prompt: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    sid = validate_space_id(space_id)
    wid = validate_widget_id(widget_id)
    name = validate_event_name(event_name)
    if payload is not None and not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    space = read_space(sid)
    _widget_index(space, wid)
    prompt_preview = _payload_text_summary(prompt, 1000)
    payload_summary = _payload_summary(payload or {})
    event_id = _record_event(
        sid,
        "widget.event.queued",
        {
            "widget_id": wid,
            "event_name": name,
            "prompt_preview": prompt_preview,
            "payload_summary": payload_summary,
            "session_id": _context_value(session_id, 120),
            "status": "queued",
        },
    )
    return {
        "queued": True,
        "status": "queued",
        "space_id": sid,
        "widget_id": wid,
        "event_name": name,
        "event_id": event_id,
        "prompt_preview": prompt_preview,
        "payload_summary": payload_summary,
    }


def recovery_snapshot() -> dict[str, Any]:
    """Return safe recovery metadata without rendering/returning widget code."""
    if not spaces_enabled():
        return {"enabled": False, "generated_widgets_rendered": False, "spaces": []}
    _ensure_dirs()
    spaces: list[dict[str, Any]] = []
    for manifest in manifests_dir().glob("*/space.json"):
        try:
            space = json.loads(manifest.read_text(encoding="utf-8"))
            summary = _summary(space)
            widgets = space.get("widgets") if isinstance(space.get("widgets"), list) else []
            summary["widgets"] = [_widget_recovery_summary(widget) for widget in widgets if isinstance(widget, dict)]
            spaces.append(summary)
        except Exception:
            continue
    spaces.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return {
        "enabled": True,
        "schema_version": SCHEMA_VERSION,
        "generated_widgets_rendered": False,
        "spaces": spaces,
    }
