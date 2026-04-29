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
import json
import os
import re
import shutil
import time
import uuid
import zipfile
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
    return {
        "id": clean_widget["id"],
        "kind": clean_widget["kind"],
        "title": clean_widget["title"],
        "layout": clean_widget["layout"],
    }


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
            "demo_templates": ["weather", "research", "kanban", "notes", "browser", "stock"],
            "items": [
                {"id": "try-weather", "title": "Install the Weather Demo", "status": "suggested"},
                {"id": "try-research", "title": "Open the Research Harness", "status": "suggested"},
                {"id": "try-browser", "title": "Preview Browser Surface planning", "status": "suggested"},
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
    if template_name not in {"weather", "weather-demo", "research", "research-harness", "dashboard", "daily-dashboard", "kanban", "kanban-board", "notes", "notes-app", "browser", "browser-surface", "stock", "stock-chart", "stocks", "big-bang", "bigbang", "onboarding", "big-bang-onboarding"}:
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
