"""Capy Spaces storage and recovery primitives.

This module is intentionally isolated from chat/streaming internals so the
Spaces foundation can survive Hermes WebUI and Hermes Agent updates. The first
slice is storage + safe recovery only; generated widget rendering and agent
execution arrive later behind stricter permissions.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

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


def _record_event(space_id: str, event_type: str, details: dict[str, Any] | None = None) -> str:
    _ensure_dirs()
    event_id = uuid.uuid4().hex
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "space_id": space_id,
        "created_at": time.time(),
        "details": details or {},
    }
    _atomic_write_json(events_dir() / f"{event_id}.json", event)
    return event_id


def _write_manifest(space: dict[str, Any], event_type: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    now = time.time()
    space.setdefault("created_at", now)
    space["updated_at"] = now
    event_id = _record_event(space["space_id"], event_type, details)
    revisions = list(space.get("revision_events") or [])
    revisions.append(event_id)
    space["revision_events"] = revisions
    space["revision_event_id"] = event_id
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


def _widget_summary(widget: dict[str, Any]) -> dict[str, Any]:
    wid = validate_widget_id(widget.get("id"))
    title = str(widget.get("title") or widget.get("name") or wid)
    return {
        "id": wid,
        "kind": str(widget.get("kind") or "custom"),
        "title": title,
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
        return _context_value(value, 500)
    return _context_value(type(value).__name__, 80)


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


def upsert_widget(space_id: str, widget: dict[str, Any]) -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    if not isinstance(widget, dict):
        raise ValueError("widget must be an object")
    wid = validate_widget_id(widget.get("id"))
    clean_widget = dict(widget)
    clean_widget["id"] = wid
    clean_widget["kind"] = str(clean_widget.get("kind") or "custom")
    clean_widget["title"] = str(clean_widget.get("title") or clean_widget.get("name") or wid)

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
    prompt_preview = _context_value(prompt, 1000)
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
    return {
        "enabled": True,
        "schema_version": SCHEMA_VERSION,
        "generated_widgets_rendered": False,
        "spaces": list_spaces(),
    }
