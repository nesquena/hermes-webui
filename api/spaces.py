"""Capy Spaces storage and recovery primitives.

This module is intentionally isolated from chat/streaming internals so the
Spaces foundation can survive Hermes WebUI and Hermes Agent updates. The first
slice is storage + safe recovery only; generated widget rendering and agent
execution arrive later behind stricter permissions.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import ipaddress
import json
import math
import os
import re
import shutil
import threading
import time
import unicodedata
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
_SPACE_AGENT_UNSUPPORTED_API_RE = re.compile(r"\bspace\.(?:current|spaces)\.[a-zA-Z0-9_.:-]+")
_TRUTHY = {"1", "true", "yes", "on", "enabled"}
_OMITTED_PAYLOAD_KEYS = {
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "cookie",
    "credential",
    "credentials",
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
    r"(^|[^a-z0-9])(api[_-]?key|apikey|authorization|bearer|cookie|credential|credentials|password|secret|token)([^a-z0-9]|$)",
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
_SOURCE_WIDGET_DEFAULT_POSITION = {"col": 0, "row": 0}
_SOURCE_WIDGET_DEFAULT_SIZE = {"cols": 6, "rows": 3}
_SOURCE_WIDGET_SIZE_PRESETS = {
    "small": {"cols": 4, "rows": 2},
    "medium": {"cols": 6, "rows": 3},
    "large": {"cols": 8, "rows": 4},
    "tall": {"cols": 4, "rows": 5},
    "full": {"cols": 12, "rows": 4},
}
_SOURCE_GRID_COORD_MIN = -4096
_SOURCE_GRID_COORD_MAX = 4096
_SOURCE_PACKING_VIEWPORT_HEADROOM_COLS = 2
_SOURCE_SPACES_ROOT_PATH = "~/spaces/"
_SOURCE_SPACE_MANIFEST_FILE = "space.yaml"
_SOURCE_SPACE_WIDGETS_DIR = "widgets/"
_SOURCE_SPACE_WIDGET_FILE_EXTENSION = ".yaml"
_SOURCE_SPACE_DATA_DIR = "data/"
_CREATOR_PREVIEW_TTL_SECONDS = 30 * 60
_CREATOR_PREVIEW_CACHE_MAX = 100
_CREATOR_PREVIEW_RECEIPTS: dict[str, dict[str, Any]] = {}
_CREATOR_PREVIEW_RECEIPTS_LOCK = threading.RLock()
_SOURCE_SPACE_ASSETS_DIR = "assets/"
_SOURCE_SPACE_SCRIPTS_DIR = "scripts/"
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
    {"demo": "demo_provider_setup", "template": "model-setup", "title": "Provider setup"},
    {"demo": "demo_big_bang_onboarding", "template": "big-bang", "title": "Big Bang onboarding"},
    {"demo": "demo_time_travel_restore", "template": "weather", "title": "Time travel restore"},
    {"demo": "demo_safe_admin_recovery", "template": "weather", "title": "Admin recovery"},
]
_SPACE_DEMO_RUN_BY_NAME = {item["demo"]: item for item in _SPACE_DEMO_RUNS}
_WIDGET_DETAIL_METADATA_FIELDS = (
    "content_status",
    "status",
    "export",
    "interaction",
    "folders",
    "attachments",
    "event_bridge",
    "refresh",
    "permissions",
    "capabilities",
    "audio_policy",
    "browser_surface",
    "network",
    "weather",
    "market_data",
    "watchlist",
    "chart",
    "table",
    "notes",
    "kanban",
    "prompt",
)


def spaces_enabled() -> bool:
    """Return whether Capy Spaces is enabled for normal API use."""
    return str(os.getenv("HERMES_WEBUI_SPACES_ENABLED", "")).strip().lower() in _TRUTHY


def spaces_root() -> Path:
    return Path(config.STATE_DIR).expanduser().resolve() / "capy-spaces"


def manifests_dir() -> Path:
    return spaces_root() / "spaces"


def events_dir() -> Path:
    return spaces_root() / "events"


def recovery_modules_dir() -> Path:
    return spaces_root() / "recovery-modules"


def _ensure_dirs() -> None:
    manifests_dir().mkdir(parents=True, exist_ok=True)
    events_dir().mkdir(parents=True, exist_ok=True)
    recovery_modules_dir().mkdir(parents=True, exist_ok=True)


def _slugify(value: str) -> str:
    value = (value or "space").strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-_")
    return value[:64] or "space"


def _source_slugify_segment(value: Any, fallback: str = "item") -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip()).lower()
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", normalized)
    slug = re.sub(r"^-+|-+$", "", slug)
    return slug or fallback


def _space_tool_normalize_id_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    default_fallback = "space" if kind == "space" else "widget"
    fallback = _source_slugify_segment(payload.get("fallback"), default_fallback)
    raw_value = (
        payload.get("value")
        or payload.get("spaceId")
        or payload.get("space_id")
        or payload.get("widgetId")
        or payload.get("widget_id")
        or payload.get("id")
        or payload.get("name")
        or payload.get("title")
        or ""
    )
    return {"id": _source_slugify_segment(raw_value, fallback), "normalize": {"mode": "metadata-only"}}


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


def validate_module_id(module_id: str) -> str:
    mid = str(module_id or "").strip()
    if not _WIDGET_ID_RE.fullmatch(mid):
        raise ValueError("Invalid module_id")
    return mid


def validate_data_key(key: str) -> str:
    data_key = str(key or "").strip()
    if not _WIDGET_ID_RE.fullmatch(data_key):
        raise ValueError("Invalid data key")
    return data_key


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


def _recovery_module_path(module_id: str) -> Path:
    mid = validate_module_id(module_id)
    root = recovery_modules_dir().resolve()
    path = (root / f"{mid}.json").resolve()
    path.relative_to(root)
    return path


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


def _recovery_reason_summary(value: Any, limit: int = 300) -> str:
    text = _context_value(value, limit)
    lowered = text.lower()
    unsafe_marker_re = re.compile(
        r"(^|[^a-z0-9])(api[_-]?key|api[_-]?auth|apikey|apiauth|authorization|bearer|cookie|credential|credentials|password|secret|token|renderer|source|html|script|data)([^a-z0-9]|$)",
        re.IGNORECASE,
    )
    if text and (
        unsafe_marker_re.search(text)
        or any(marker in lowered for marker in _EXECUTABLE_VALUE_MARKERS)
    ):
        return "[REDACTED]"
    return text


def _summary(space: dict[str, Any]) -> dict[str, Any]:
    widgets = space.get("widgets") or []
    recovery = space.get("recovery") if isinstance(space.get("recovery"), dict) else {}
    return {
        "schema_version": space.get("schema_version", SCHEMA_VERSION),
        "space_id": space.get("space_id"),
        "name": _public_display_text_summary(space.get("name") or space.get("space_id"), 160),
        "description": _public_display_text_summary(space.get("description", ""), 300),
        "created_at": space.get("created_at"),
        "updated_at": space.get("updated_at"),
        "revision_event_id": space.get("revision_event_id"),
        "widget_count": len(widgets) if isinstance(widgets, list) else 0,
        "disabled": bool(recovery.get("disabled")),
        "disabled_reason": _recovery_reason_summary(recovery.get("disabled_reason"), 300),
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


def _normalize_source_widget_size(size: Any, fallback: dict[str, int] | None = None) -> dict[str, int]:
    """Normalize Space Agent-style widget size input for metadata-only helpers."""
    fallback_size = dict(fallback or _SOURCE_WIDGET_DEFAULT_SIZE)
    if isinstance(size, str):
        normalized = size.strip().lower()
        if normalized in _SOURCE_WIDGET_SIZE_PRESETS:
            return dict(_SOURCE_WIDGET_SIZE_PRESETS[normalized])
        match = re.match(r"^(\d+)\s*x\s*(\d+)$", normalized)
        if match:
            return _normalize_source_widget_size({"cols": match.group(1), "rows": match.group(2)}, fallback_size)
        return fallback_size
    if isinstance(size, (list, tuple)) and len(size) >= 2:
        return _normalize_source_widget_size({"cols": size[0], "rows": size[1]}, fallback_size)
    if isinstance(size, dict):
        return {
            "cols": _clamped_int(size.get("cols", size.get("width", size.get("w"))), fallback_size["cols"], 1, 24),
            "rows": _clamped_int(size.get("rows", size.get("height", size.get("h"))), fallback_size["rows"], 1, 24),
        }
    return fallback_size


def _coerce_source_widget_position(position: Any, fallback: dict[str, int]) -> dict[str, int]:
    if isinstance(position, str):
        match = re.match(r"^(-?\d+)\s*,\s*(-?\d+)$", position.strip())
        if match:
            return _coerce_source_widget_position({"col": match.group(1), "row": match.group(2)}, fallback)
        return dict(fallback)
    if isinstance(position, (list, tuple)) and len(position) >= 2:
        return _coerce_source_widget_position({"col": position[0], "row": position[1]}, fallback)
    if isinstance(position, dict):
        return {
            "col": _clamped_int(
                position.get("col", position.get("x")),
                fallback["col"],
                _SOURCE_GRID_COORD_MIN,
                _SOURCE_GRID_COORD_MAX,
            ),
            "row": _clamped_int(
                position.get("row", position.get("y")),
                fallback["row"],
                _SOURCE_GRID_COORD_MIN,
                _SOURCE_GRID_COORD_MAX,
            ),
        }
    return dict(fallback)


def _normalize_source_widget_position(position: Any, fallback: Any = None) -> dict[str, int]:
    """Normalize Space Agent-style widget position input for metadata-only helpers."""
    fallback_position = _coerce_source_widget_position(fallback, _SOURCE_WIDGET_DEFAULT_POSITION)
    return _coerce_source_widget_position(position, fallback_position)


def _space_tool_position_to_token(payload: dict[str, Any]) -> dict[str, Any]:
    position = _normalize_source_widget_position(payload.get("position", payload.get("value")), payload.get("fallback"))
    return {"token": f"{position['col']},{position['row']}", "position": position}


def _space_tool_parse_widget_position_token(payload: dict[str, Any]) -> dict[str, Any]:
    """Parse a Space Agent widget-position token without echoing unsafe payload fields."""
    position = _normalize_source_widget_position(
        payload.get("value", payload.get("token", payload.get("position", ""))),
        payload.get("fallback"),
    )
    return {"token": f"{position['col']},{position['row']}", "position": position}


def _space_tool_clamp_widget_position(payload: dict[str, Any]) -> dict[str, Any]:
    """Clamp a Space Agent-style position so rendered widget bounds stay inside the source grid."""
    size = _normalize_source_widget_size(payload.get("size"), _SOURCE_WIDGET_DEFAULT_SIZE)
    position = _normalize_source_widget_position(payload.get("position"), payload.get("fallback"))
    max_col = _SOURCE_GRID_COORD_MAX - size["cols"] + 1
    max_row = _SOURCE_GRID_COORD_MAX - size["rows"] + 1
    clamped = {
        "col": min(max_col, max(_SOURCE_GRID_COORD_MIN, position["col"])),
        "row": min(max_row, max(_SOURCE_GRID_COORD_MIN, position["row"])),
    }
    return {"token": f"{clamped['col']},{clamped['row']}", "position": clamped, "size": size}


def _space_tool_get_rendered_widget_size(payload: dict[str, Any]) -> dict[str, Any]:
    fallback = _normalize_source_widget_size(payload.get("fallback"), _SOURCE_WIDGET_DEFAULT_SIZE)
    size = _normalize_source_widget_size(payload.get("size"), fallback)
    if _truthy_bool(payload.get("minimized")):
        size["rows"] = 1
    return {"token": f"{size['cols']}x{size['rows']}", "size": size}


def _space_tool_size_to_token(payload: dict[str, Any]) -> dict[str, Any]:
    fallback = _normalize_source_widget_size(payload.get("fallback"), _SOURCE_WIDGET_DEFAULT_SIZE)
    size = _normalize_source_widget_size(payload.get("size"), fallback)
    return {"token": f"{size['cols']}x{size['rows']}", "size": size}


def _source_create_rect(widget_id: str, position: dict[str, int], size: dict[str, int]) -> dict[str, Any]:
    clamped = _space_tool_clamp_widget_position({"position": position, "size": size})["position"]
    return {
        "bottom": clamped["row"] + size["rows"] - 1,
        "left": clamped["col"],
        "right": clamped["col"] + size["cols"] - 1,
        "top": clamped["row"],
        "widget_id": widget_id,
    }


def _source_rects_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return not (
        left["right"] < right["left"]
        or left["left"] > right["right"]
        or left["bottom"] < right["top"]
        or left["top"] > right["bottom"]
    )


def _source_can_place_rect(position: dict[str, int], size: dict[str, int], occupied_rects: list[dict[str, Any]]) -> bool:
    candidate = _source_create_rect("", position, size)
    return all(not _source_rects_overlap(candidate, occupied) for occupied in occupied_rects)



def _source_column_search_order(start_col: int, radius: int) -> list[int]:
    columns = [start_col]
    for offset in range(1, radius + 1):
        columns.extend([start_col + offset, start_col - offset])
    ordered: list[int] = []
    seen: set[int] = set()
    for column in columns:
        if column in seen:
            continue
        ordered.append(column)
        seen.add(column)
    return ordered



def _source_find_first_available_position(
    size: dict[str, int],
    occupied_rects: list[dict[str, Any]],
    preferred_position: dict[str, int] | None = None,
) -> dict[str, int]:
    normalized_size = _normalize_source_widget_size(size, _SOURCE_WIDGET_DEFAULT_SIZE)
    normalized_position = _space_tool_clamp_widget_position(
        {"position": preferred_position or _SOURCE_WIDGET_DEFAULT_POSITION, "size": normalized_size}
    )["position"]
    min_col = _SOURCE_GRID_COORD_MIN
    max_col = _SOURCE_GRID_COORD_MAX - normalized_size["cols"] + 1
    max_row = _SOURCE_GRID_COORD_MAX - normalized_size["rows"] + 1
    start_col = min(max_col, max(min_col, normalized_position["col"]))
    column_order = _source_column_search_order(start_col, _SOURCE_GRID_COORD_MAX - _SOURCE_GRID_COORD_MIN)

    for row in range(normalized_position["row"], max_row + 1):
        for col in column_order:
            if col < min_col or col > max_col:
                continue
            position = {"col": col, "row": row}
            if _source_can_place_rect(position, normalized_size, occupied_rects):
                return position
    return dict(normalized_position)



def _source_build_packing_entries(widget_ids: Any, widget_sizes: Any) -> list[dict[str, Any]]:
    ids = widget_ids if isinstance(widget_ids, list) else []
    sizes = widget_sizes if isinstance(widget_sizes, dict) else {}
    entries = []
    for index, raw_widget_id in enumerate(ids):
        widget_id = validate_widget_id(raw_widget_id)
        size = _normalize_source_widget_size(sizes.get(widget_id), _SOURCE_WIDGET_DEFAULT_SIZE)
        entries.append({"area": size["cols"] * size["rows"], "index": index, "size": size, "widget_id": widget_id})
    return entries


def _source_sort_packing_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: (-entry["area"], -entry["size"]["cols"], -entry["size"]["rows"], entry["index"]))


def _source_viewport_cols(value: Any, fallback: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(0, parsed)


def _source_packing_width_threshold(entries: list[dict[str, Any]], viewport_cols: Any = 0, *, cap_to_total_width: bool = True) -> int:
    if not entries:
        return 1
    max_widget_width = max(entry["size"]["cols"] for entry in entries)
    total_width = sum(entry["size"]["cols"] for entry in entries)
    viewport = _source_viewport_cols(viewport_cols)
    normalized_viewport = max(1, viewport - _SOURCE_PACKING_VIEWPORT_HEADROOM_COLS) if viewport > 0 else total_width
    if not cap_to_total_width:
        return max(max_widget_width, normalized_viewport)
    return max(max_widget_width, min(total_width, max(max_widget_width, normalized_viewport)))


def _source_scan_cell_occupied(position: dict[str, int], occupied_rects: list[dict[str, Any]]) -> bool:
    return not _source_can_place_rect(position, {"cols": 1, "rows": 1}, occupied_rects)


def _source_find_physically_fitting_entry(
    entries: list[dict[str, Any]],
    position: dict[str, int],
    width_threshold: int,
    occupied_rects: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for entry in entries:
        if position["col"] + entry["size"]["cols"] > width_threshold:
            continue
        if not _source_can_place_rect(position, entry["size"], occupied_rects):
            continue
        return entry
    return None


def _source_build_first_fit_packed_positions(
    entries: list[dict[str, Any]],
    width_threshold: int,
    *,
    occupied_rects: list[dict[str, Any]] | None = None,
    start_row: int = 0,
) -> dict[str, dict[str, int]]:
    occupied = list(occupied_rects or [])
    positions: dict[str, dict[str, int]] = {}
    remaining = _source_sort_packing_entries(entries)
    row = max(0, int(start_row or 0))
    while remaining:
        for col in range(0, max(1, width_threshold)):
            candidate = {"col": col, "row": row}
            if _source_scan_cell_occupied(candidate, occupied):
                continue
            matching = _source_find_physically_fitting_entry(remaining, candidate, width_threshold, occupied)
            if not matching:
                continue
            positions[matching["widget_id"]] = candidate
            occupied.append(_source_create_rect(matching["widget_id"], candidate, matching["size"]))
            remaining.remove(matching)
        row += 1
    return positions


def _source_packed_bounds(positions: dict[str, dict[str, int]], sizes: dict[str, dict[str, int]]) -> dict[str, int]:
    bounds = {"min_col": 0, "max_col": 0, "min_row": 0, "max_row": 0, "width": 0, "height": 0}
    has_positions = False
    for widget_id, position in positions.items():
        size = _normalize_source_widget_size(sizes.get(widget_id), _SOURCE_WIDGET_DEFAULT_SIZE)
        right = position["col"] + size["cols"]
        bottom = position["row"] + size["rows"]
        if not has_positions:
            bounds.update({"min_col": position["col"], "max_col": right, "min_row": position["row"], "max_row": bottom})
            has_positions = True
            continue
        bounds["min_col"] = min(bounds["min_col"], position["col"])
        bounds["max_col"] = max(bounds["max_col"], right)
        bounds["min_row"] = min(bounds["min_row"], position["row"])
        bounds["max_row"] = max(bounds["max_row"], bottom)
    if has_positions:
        bounds["width"] = bounds["max_col"] - bounds["min_col"]
        bounds["height"] = bounds["max_row"] - bounds["min_row"]
    return bounds



def _space_tool_resolve_space_layout(payload: dict[str, Any]) -> dict[str, Any]:
    raw_ids = payload.get("widgetIds") or payload.get("widget_ids") or []
    widget_ids = [validate_widget_id(widget_id) for widget_id in raw_ids] if isinstance(raw_ids, list) else []
    widget_positions = payload.get("widgetPositions") or payload.get("widget_positions") or {}
    widget_positions = widget_positions if isinstance(widget_positions, dict) else {}
    widget_sizes = payload.get("widgetSizes") or payload.get("widget_sizes") or {}
    widget_sizes = widget_sizes if isinstance(widget_sizes, dict) else {}
    minimized_raw = payload.get("minimizedWidgetIds") or payload.get("minimized_widget_ids") or []
    minimized_set = {validate_widget_id(widget_id) for widget_id in minimized_raw} if isinstance(minimized_raw, list) else set()
    anchor_widget_id = ""
    raw_anchor_widget_id = payload.get("anchorWidgetId") or payload.get("anchor_widget_id") or ""
    if raw_anchor_widget_id:
        anchor_widget_id = validate_widget_id(raw_anchor_widget_id)
    has_anchor_minimized = "anchorMinimized" in payload or "anchor_minimized" in payload
    anchor_minimized = _truthy_bool(payload.get("anchorMinimized", payload.get("anchor_minimized")))
    anchor_position_supplied = "anchorPosition" in payload or "anchor_position" in payload
    anchor_position = payload.get("anchorPosition", payload.get("anchor_position"))
    anchor_size_supplied = "anchorSize" in payload or "anchor_size" in payload
    anchor_size = payload.get("anchorSize", payload.get("anchor_size"))

    entries: list[dict[str, Any]] = []
    for index, widget_id in enumerate(widget_ids):
        stored_position = _normalize_source_widget_position(widget_positions.get(widget_id), _SOURCE_WIDGET_DEFAULT_POSITION)
        preferred_position = (
            _normalize_source_widget_position(anchor_position, widget_positions.get(widget_id) or _SOURCE_WIDGET_DEFAULT_POSITION)
            if widget_id == anchor_widget_id and anchor_position_supplied
            else stored_position
        )
        minimized = anchor_minimized if widget_id == anchor_widget_id and has_anchor_minimized else widget_id in minimized_set
        stored_size = (
            _normalize_source_widget_size(anchor_size, _normalize_source_widget_size(widget_sizes.get(widget_id), _SOURCE_WIDGET_DEFAULT_SIZE))
            if widget_id == anchor_widget_id and anchor_size_supplied
            else _normalize_source_widget_size(widget_sizes.get(widget_id), _SOURCE_WIDGET_DEFAULT_SIZE)
        )
        rendered_size = dict(stored_size)
        if minimized:
            rendered_size["rows"] = 1
        entries.append(
            {
                "index": index,
                "minimized": minimized,
                "preferred_position": preferred_position,
                "rendered_size": rendered_size,
                "stored_size": stored_size,
                "widget_id": widget_id,
            }
        )

    entries.sort(
        key=lambda entry: (
            0 if entry["widget_id"] == anchor_widget_id else 1,
            entry["preferred_position"]["row"],
            entry["preferred_position"]["col"],
            entry["index"],
        )
    )
    positions: dict[str, dict[str, int]] = {}
    rendered_sizes: dict[str, dict[str, int]] = {}
    minimized_map: dict[str, bool] = {}
    occupied_rects: list[dict[str, Any]] = []
    for entry in entries:
        position = _source_find_first_available_position(entry["rendered_size"], occupied_rects, entry["preferred_position"])
        positions[entry["widget_id"]] = position
        rendered_sizes[entry["widget_id"]] = entry["rendered_size"]
        minimized_map[entry["widget_id"]] = bool(entry["minimized"])
        occupied_rects.append(_source_create_rect(entry["widget_id"], position, entry["rendered_size"]))
    return {"positions": positions, "renderedSizes": rendered_sizes, "minimizedMap": minimized_map}



def _space_tool_build_centered_first_fit_layout(payload: dict[str, Any]) -> dict[str, Any]:
    sizes = payload.get("widgetSizes") or payload.get("widget_sizes") or {}
    sizes = sizes if isinstance(sizes, dict) else {}
    entries = _source_build_packing_entries(payload.get("widgetIds") or payload.get("widget_ids") or [], sizes)
    if not entries:
        return {"positions": {}}
    width_threshold = _source_packing_width_threshold(entries, payload.get("viewportCols", payload.get("viewport_cols")))
    positions = _source_build_first_fit_packed_positions(entries, width_threshold)
    bounds = _source_packed_bounds(positions, sizes)
    shift_col = -math.floor(bounds["width"] / 2) - bounds["min_col"]
    shift_row = -math.floor(bounds["height"] / 2) - bounds["min_row"]
    if shift_col or shift_row:
        positions = {
            widget_id: {"col": position["col"] + shift_col, "row": position["row"] + shift_row}
            for widget_id, position in positions.items()
        }
    return {"positions": positions}


def _source_occupied_rects(
    widget_positions: Any,
    widget_sizes: Any,
    offset: dict[str, int],
) -> list[dict[str, Any]]:
    positions = widget_positions if isinstance(widget_positions, dict) else {}
    sizes = widget_sizes if isinstance(widget_sizes, dict) else {}
    rects = []
    for widget_id, raw_position in positions.items():
        safe_widget_id = validate_widget_id(widget_id)
        position = _normalize_source_widget_position(raw_position, _SOURCE_WIDGET_DEFAULT_POSITION)
        size = _normalize_source_widget_size(sizes.get(safe_widget_id), _SOURCE_WIDGET_DEFAULT_SIZE)
        rects.append(
            _source_create_rect(
                safe_widget_id,
                {"col": position["col"] - offset["col"], "row": position["row"] - offset["row"]},
                size,
            )
        )
    return rects


def _space_tool_find_first_fit_widget_placement(payload: dict[str, Any]) -> dict[str, Any]:
    widget_size = _normalize_source_widget_size(payload.get("widgetSize", payload.get("widget_size")), _SOURCE_WIDGET_DEFAULT_SIZE)
    existing_positions = payload.get("existingWidgetPositions") or payload.get("existing_widget_positions") or {}
    existing_sizes = payload.get("existingWidgetSizes") or payload.get("existing_widget_sizes") or {}
    existing_positions = existing_positions if isinstance(existing_positions, dict) else {}
    existing_sizes = existing_sizes if isinstance(existing_sizes, dict) else {}
    bounds = _source_packed_bounds(
        {widget_id: _normalize_source_widget_position(position, _SOURCE_WIDGET_DEFAULT_POSITION) for widget_id, position in existing_positions.items()},
        existing_sizes,
    )
    offset = {"col": bounds["min_col"], "row": bounds["min_row"]} if existing_positions else {"col": 0, "row": 0}
    entry = {"area": widget_size["cols"] * widget_size["rows"], "index": 0, "size": widget_size, "widget_id": "__candidate__"}
    width_threshold = _source_packing_width_threshold([entry], payload.get("viewportCols", payload.get("viewport_cols")), cap_to_total_width=False)
    local_positions = _source_build_first_fit_packed_positions(
        [entry],
        width_threshold,
        occupied_rects=_source_occupied_rects(existing_positions, existing_sizes, offset),
    )
    local_position = local_positions.get("__candidate__", dict(_SOURCE_WIDGET_DEFAULT_POSITION))
    position = {"col": local_position["col"] + offset["col"], "row": local_position["row"] + offset["row"]}
    return {"position": position, "token": f"{position['col']},{position['row']}", "size": widget_size}


def _space_tool_parse_widget_size_token(payload: dict[str, Any]) -> dict[str, Any]:
    """Parse a Space Agent widget-size token without echoing unsafe payload fields."""
    fallback = _normalize_source_widget_size(payload.get("fallback"), _SOURCE_WIDGET_DEFAULT_SIZE)
    raw = payload.get("value", payload.get("token", payload.get("size", "")))
    match = re.match(r"^(\d+)\s*x\s*(\d+)$", str(raw or "").strip().lower())
    size = _normalize_source_widget_size({"cols": match.group(1), "rows": match.group(2)}, fallback) if match else fallback
    return {"token": f"{size['cols']}x{size['rows']}", "size": size}


def _space_tool_build_source_path(name: str, payload: dict[str, Any]) -> str:
    """Build Space Agent-style logical storage paths without touching storage."""
    space_id = validate_space_id(_space_tool_current_id(payload))
    root_path = f"{_SOURCE_SPACES_ROOT_PATH}{space_id}/"
    if name == "space.spaces.buildspacerootpath":
        return root_path
    if name == "space.spaces.buildspacemanifestpath":
        return f"{root_path}{_SOURCE_SPACE_MANIFEST_FILE}"
    if name == "space.spaces.buildspacewidgetspath":
        return f"{root_path}{_SOURCE_SPACE_WIDGETS_DIR}"
    if name == "space.spaces.buildspacewidgetfilepath":
        widget_id = validate_widget_id(_space_tool_widget_id(payload))
        return f"{root_path}{_SOURCE_SPACE_WIDGETS_DIR}{widget_id}{_SOURCE_SPACE_WIDGET_FILE_EXTENSION}"
    if name == "space.spaces.buildspacedatapath":
        return f"{root_path}{_SOURCE_SPACE_DATA_DIR}"
    if name == "space.spaces.buildspaceassetspath":
        return f"{root_path}{_SOURCE_SPACE_ASSETS_DIR}"
    if name == "space.spaces.buildspacescriptspath":
        return f"{root_path}{_SOURCE_SPACE_SCRIPTS_DIR}"
    raise ValueError("Unsupported Capy Spaces path helper")



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
        "kind": _public_display_text_summary(clean_widget["kind"], 80) or "custom",
        "title": _public_display_text_summary(clean_widget["title"], 160) or clean_widget["id"],
        "layout": clean_widget["layout"],
    }
    system = widget.get("system") if isinstance(widget.get("system"), dict) else {}
    panel = str(system.get("panel") or "").strip()
    if clean_widget["kind"] == "system" and panel in _TRUSTED_SYSTEM_WIDGETS:
        summary["system_panel"] = panel
    return summary


def _widget_detail_metadata(widget: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field in _WIDGET_DETAIL_METADATA_FIELDS:
        if field not in widget:
            continue
        summary = _payload_summary(widget.get(field))
        if summary in ({}, [], ""):
            continue
        metadata[field] = summary
    return metadata


def _widget_runtime_contract_summary(widget: dict[str, Any]) -> dict[str, Any]:
    """Return the safe, metadata-only draft runtime contract for a widget.

    Generated widget source remains disabled until a sandboxed viewer/runtime is
    explicitly implemented. This contract gives Spaces tools and detail views a
    stable handshake shape without echoing stored renderer/html/script/data
    bodies or user-supplied secret-looking runtime config.
    """
    clean_widget = _normalize_widget(widget)
    return {
        "mode": "sandbox-contract-draft",
        "widget_id": clean_widget["id"],
        "execution": "generated-code-disabled",
        "allowed_messages": ["capy:ready", "capy:resize", "capy:agent:prompt"],
        "blocked_messages": ["capy:raw:eval", "capy:data:put"],
        "network_policy": {
            "default": "deny",
            "allowed_schemes": ["https"],
            "agent_mediated": True,
        },
        "approval_required_for": [
            "external-navigation",
            "network-fetch",
            "generated-code-enable",
        ],
    }


def _widget_recovery_summary(widget: dict[str, Any]) -> dict[str, Any]:
    clean_widget = _normalize_widget(widget)
    recovery = widget.get("recovery") if isinstance(widget.get("recovery"), dict) else {}
    return {
        "id": clean_widget["id"],
        "kind": clean_widget["kind"],
        "title": _recovery_reason_summary(clean_widget["title"], 160),
        "disabled": bool(recovery.get("disabled")),
        "disabled_reason": _recovery_reason_summary(recovery.get("disabled_reason"), 300),
    }


def _data_slot_summary(slot: dict[str, Any]) -> dict[str, Any] | None:
    try:
        key = validate_data_key(slot.get("key"))
    except ValueError:
        return None
    value_summary = _payload_summary(slot.get("value_summary") if "value_summary" in slot else slot.get("value"))
    metadata_summary = _data_slot_metadata_summary(
        slot.get("metadata_summary") if "metadata_summary" in slot else slot.get("metadata")
    )
    return {
        "key": key,
        "value_summary": value_summary,
        "metadata_summary": metadata_summary,
    }


def _data_slot_metadata_summary(value: Any) -> dict[str, Any]:
    summary = _payload_summary(value if isinstance(value, dict) else {})
    if not isinstance(summary, dict):
        summary = {}
    if isinstance(value, dict) and "source_widget" in value:
        try:
            summary["source_widget"] = validate_widget_id(value.get("source_widget"))
        except ValueError:
            pass
    return summary


def _data_slot_summaries(space: dict[str, Any]) -> list[dict[str, Any]]:
    slots = space.get("shared_data") if isinstance(space.get("shared_data"), dict) else {}
    items: list[dict[str, Any]] = []
    for key in sorted(slots):
        raw = slots.get(key)
        if not isinstance(raw, dict):
            continue
        summary = _data_slot_summary({"key": key, **raw})
        if summary is not None:
            items.append(summary)
    return items


def _context_value(value: Any, limit: int = 500) -> str:
    """Return a single-line value safe for compact agent context."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _active_context_value(value: Any, limit: int = 500) -> str:
    """Return a compact active-space prompt value with unsafe markers redacted."""
    return _recovery_reason_summary(value, limit)


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


def _public_display_text_summary(value: Any, limit: int = 300) -> str:
    """Return safe UI/API display metadata without over-redacting benign labels.

    This is intentionally narrower than recovery-preview redaction: ordinary
    product labels such as "Source Space" or "Daily Data Dashboard" stay useful,
    while executable/generated-code and credential/API-auth markers fail closed.
    """
    text = _context_value(value, limit)
    lowered = text.lower()
    unsafe_pattern = re.compile(
        r"api[_-]?(key|auth)|apiauth|apikey|authorization|bearer\s+[^\s,;]+|"
        r"cookie\s*[:=]|credential|credentials|password|secret(?:[_-][a-z0-9_-]+|\b)|token\s*[:=]|"
        r"<script|</script|javascript:|onerror|onload|renderer|generated[ _-]?code|raw[ _-]?prompt",
        re.IGNORECASE,
    )
    if text and (unsafe_pattern.search(text) or any(marker in lowered for marker in _EXECUTABLE_VALUE_MARKERS)):
        return "[REDACTED]"
    return text


def _recovery_payload_summary(value: Any, depth: int = 0) -> Any:
    """Return a recovery/admin-safe metadata summary for event details."""
    if depth > 3:
        return "[REDACTED]"
    if isinstance(value, dict):
        summary: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            if count >= 20:
                break
            safe_key = _context_value(key, 80)
            if not _payload_key_is_safe(safe_key):
                continue
            child = _recovery_payload_summary(item, depth + 1)
            if child in ({}, [], ""):
                continue
            summary[safe_key] = child
            count += 1
        return summary
    if isinstance(value, (list, tuple)):
        return [_recovery_payload_summary(item, depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return _recovery_reason_summary(value, 300)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _recovery_reason_summary(value, 300)


def _payload_summary(value: Any, depth: int = 0) -> Any:
    """Return a bounded, metadata-safe widget event payload summary.

    Widget events are the bridge toward agent-triggered UI actions, but this
    first slice must not persist or echo generated renderer/html/script bodies
    or obvious secret-bearing fields. Full payload delivery can be added later
    behind explicit capability and sandbox checks.
    """
    if depth > 3:
        return "[omitted]"
    if isinstance(value, dict):
        summary: dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= 50:
                break
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


def _event_summary(event: dict[str, Any], sid: str, current_snapshot: dict[str, Any] | None = None) -> dict[str, Any] | None:
    event_id = str(event.get("event_id") or "")
    if not _event_id_is_safe(event_id) or event.get("space_id") != sid:
        return None
    details = _recovery_payload_summary(event.get("details") or {})
    if not isinstance(details, dict):
        details = {}
    summary = {
        "schema_version": event.get("schema_version", SCHEMA_VERSION),
        "event_id": event_id,
        "event_type": _context_value(event.get("event_type"), 120),
        "space_id": sid,
        "created_at": event.get("created_at"),
        "details": details,
    }
    snapshot = event.get("snapshot")
    if isinstance(snapshot, dict):
        summary["restore_preview"] = _restore_preview_summary(snapshot, sid)
        if isinstance(current_snapshot, dict):
            summary["restore_diff"] = _restore_diff_summary(snapshot, current_snapshot)
    return summary


def _restore_preview_summary(snapshot: dict[str, Any], sid: str) -> dict[str, Any]:
    widgets = snapshot.get("widgets") if isinstance(snapshot.get("widgets"), list) else []
    widget_summaries: list[dict[str, Any]] = []
    for widget in widgets[:5]:
        if not isinstance(widget, dict):
            continue
        try:
            widget_summary = _widget_summary(widget)
        except ValueError:
            continue
        widget_summary["title"] = _recovery_reason_summary(widget_summary.get("title"), 160)
        widget_summaries.append(widget_summary)
    return {
        "space_id": sid,
        "name": _recovery_reason_summary(snapshot.get("name") or sid, 160),
        "description": _recovery_reason_summary(snapshot.get("description") or "", 240),
        "widget_count": len(widgets),
        "widgets": widget_summaries,
    }


def _restore_diff_summary(target_snapshot: dict[str, Any], current_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Summarize what restoring target_snapshot would change, metadata-only."""

    def widget_map(space: dict[str, Any]) -> dict[str, dict[str, Any]]:
        widgets = space.get("widgets") if isinstance(space.get("widgets"), list) else []
        mapped: dict[str, dict[str, Any]] = {}
        for widget in widgets:
            if not isinstance(widget, dict):
                continue
            try:
                summary = _widget_summary(widget)
            except ValueError:
                continue
            widget_id = str(summary["id"])
            if not _payload_key_is_safe(widget_id):
                continue
            mapped[widget_id] = summary
        return mapped

    target_widgets = widget_map(target_snapshot)
    current_widgets = widget_map(current_snapshot)
    target_ids = set(target_widgets)
    current_ids = set(current_widgets)
    widgets_to_add = sorted(target_ids - current_ids)[:20]
    widgets_to_remove = sorted(current_ids - target_ids)[:20]
    widgets_to_update = sorted(
        wid for wid in (target_ids & current_ids) if target_widgets.get(wid) != current_widgets.get(wid)
    )[:20]

    space_fields_to_update: list[str] = []
    for field in ("name", "description", "agent_instructions", "template"):
        if _payload_text_summary(target_snapshot.get(field) or "", 500) != _payload_text_summary(
            current_snapshot.get(field) or "", 500
        ):
            space_fields_to_update.append(field)
    for field in ("layout", "capabilities"):
        if _payload_summary(target_snapshot.get(field) or {}) != _payload_summary(current_snapshot.get(field) or {}):
            space_fields_to_update.append(field)
    target_shared_data = _data_slot_summaries(target_snapshot)
    current_shared_data = _data_slot_summaries(current_snapshot)
    if target_shared_data != current_shared_data:
        space_fields_to_update.append("shared_data")

    widget_count_delta = len(target_widgets) - len(current_widgets)
    has_changes = bool(
        widget_count_delta
        or widgets_to_add
        or widgets_to_remove
        or widgets_to_update
        or space_fields_to_update
    )
    return {
        "has_changes": has_changes,
        "widget_count_delta": widget_count_delta,
        "widgets_to_add": widgets_to_add,
        "widgets_to_remove": widgets_to_remove,
        "widgets_to_update": widgets_to_update,
        "space_fields_to_update": space_fields_to_update,
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
        f"name: {_active_context_value(space.get('name') or sid)}",
    ]
    description = _active_context_value(space.get("description"), 700)
    if description:
        lines.append(f"description: {description}")
    template = _active_context_value(space.get("template"), 120)
    if template:
        lines.append(f"template: {template}")
    instructions = _active_context_value(space.get("agent_instructions") or space.get("instructions"), 1500)
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
                f"{_active_context_value(widget['id'], 80)}|"
                f"{_active_context_value(widget['title'], 160)}|"
                f"{_active_context_value(widget['kind'], 80)}"
            )
        if len(summaries) > 25:
            lines.append(f"- … {len(summaries) - 25} more widget(s) omitted")
    else:
        lines.append("- none")
    shared_data = _data_slot_summaries(space)
    if shared_data:
        lines.append("shared data keys:")
        for item in shared_data[:25]:
            lines.append(f"- {_active_context_value(item['key'], 80)}")
        if len(shared_data) > 25:
            lines.append(f"- … {len(shared_data) - 25} more data slot(s) omitted")
    widget_events = list_widget_events(sid, limit=10)
    if widget_events:
        lines.append("queued widget events (event_id|widget_id|event_name|status):")
        for event in widget_events[:10]:
            lines.append(
                "- "
                f"{_active_context_value(event.get('event_id'), 120)}|"
                f"{_active_context_value(event.get('widget_id'), 80)}|"
                f"{_active_context_value(event.get('event_name'), 120)}|"
                f"{_active_context_value(event.get('status'), 80)}"
            )
        if len(widget_events) > 10:
            lines.append(f"- … {len(widget_events) - 10} more queued widget event(s) omitted")
    revision = _active_context_value(space.get("revision_event_id"), 120)
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
    saved = _write_manifest(space, "space.created", {"name": name})
    return read_space_detail(saved["space_id"])


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


def duplicate_space_metadata_only(space_id: str, *, target_space_id: str | None = None) -> dict[str, Any]:
    """Duplicate a Space through Capy's metadata-only safety boundary."""
    source_id = validate_space_id(space_id)
    source = read_space(source_id)
    source_name = _payload_text_summary(source.get("name") or source_id, 80)
    if not source_name or source_name == "[REDACTED]":
        source_name = "Untitled Space"
    duplicate_name = source_name if source_name.lower().endswith(" copy") else f"{source_name} Copy"
    source_layout = source.get("layout") if isinstance(source.get("layout"), dict) else {}
    source_capabilities = source.get("capabilities") if isinstance(source.get("capabilities"), dict) else {}
    safe_layout: dict[str, Any] = {}
    for key, value in source_layout.items():
        safe_key = str(key)
        if not _payload_key_is_safe(safe_key):
            continue
        if isinstance(value, (int, float, bool)):
            safe_layout[safe_key] = value
        elif isinstance(value, str):
            safe_value = _payload_text_summary(value, 120)
            if safe_value and safe_value != "[REDACTED]":
                safe_layout[safe_key] = safe_value
    payload: dict[str, Any] = {
        "space_id": validate_space_id(target_space_id) if target_space_id else _unique_space_id(duplicate_name),
        "name": duplicate_name,
        "description": _payload_text_summary(source.get("description") or "", 500),
        "agent_instructions": _payload_text_summary(source.get("agent_instructions") or "", 500),
        "template": _payload_text_summary(source.get("template") or "blank", 80) or "blank",
        "layout": safe_layout,
        "widgets": [],
        "capabilities": _payload_summary(source_capabilities),
    }
    widgets = source.get("widgets") if isinstance(source.get("widgets"), list) else []
    payload["widgets"] = [_space_tool_widget_payload(widget) for widget in widgets if isinstance(widget, dict)]
    created = create_space(payload)
    return {"source_space_id": source_id, "space_id": created["space_id"], "revision_event_id": created["revision_event_id"]}


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
        "name": _public_display_text_summary(space.get("name") or space.get("space_id"), 160) or space.get("space_id"),
        "description": _public_display_text_summary(space.get("description", ""), 300),
        "agent_instructions": _public_display_text_summary(space.get("agent_instructions", ""), 500),
        "template": space.get("template", "blank"),
        "created_at": space.get("created_at"),
        "updated_at": space.get("updated_at"),
        "layout": space.get("layout") if isinstance(space.get("layout"), dict) else {},
        "revision_event_id": space.get("revision_event_id"),
        "revision_events": [event_id for event_id in (space.get("revision_events") or []) if _event_id_is_safe(event_id)],
        "recovery": {"safe_mode_available": True},
        "widgets": [],
    }
    shared_data = _data_slot_summaries(space)
    if shared_data:
        detail["shared_data"] = shared_data
    widgets = space.get("widgets") or []
    if isinstance(widgets, list):
        detail["widgets"] = [_widget_summary(widget) for widget in widgets if isinstance(widget, dict)]
    return detail


def set_shared_data_slot(space_id: str, key: str, value: Any, metadata: Any | None = None) -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    sid = validate_space_id(space_id)
    data_key = validate_data_key(key)
    space = read_space(sid)
    shared_data = space.get("shared_data") if isinstance(space.get("shared_data"), dict) else {}
    item = {
        "key": data_key,
        "value_summary": _payload_summary(value),
        "metadata_summary": _data_slot_metadata_summary(metadata if isinstance(metadata, dict) else {}),
    }
    shared_data[data_key] = dict(item)
    space["shared_data"] = shared_data
    saved = _write_manifest(space, "space.data.set", {"key": data_key})
    return {"space_id": sid, "item": read_shared_data_slot(saved["space_id"], data_key)}


def list_shared_data_slots(space_id: str) -> list[dict[str, Any]]:
    if not spaces_enabled():
        return []
    sid = validate_space_id(space_id)
    return _data_slot_summaries(read_space(sid))


def read_shared_data_slot(space_id: str, key: str) -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    sid = validate_space_id(space_id)
    data_key = validate_data_key(key)
    for item in _data_slot_summaries(read_space(sid)):
        if item["key"] == data_key:
            return item
    raise FileNotFoundError("Data slot not found")


def delete_shared_data_slot(space_id: str, key: str) -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    sid = validate_space_id(space_id)
    data_key = validate_data_key(key)
    space = read_space(sid)
    shared_data = space.get("shared_data") if isinstance(space.get("shared_data"), dict) else {}
    if data_key not in shared_data:
        raise FileNotFoundError("Data slot not found")
    shared_data.pop(data_key, None)
    space["shared_data"] = shared_data
    saved = _write_manifest(space, "space.data.delete", {"key": data_key})
    return {
        "space_id": sid,
        "key": data_key,
        "deleted": True,
        "revision_event_id": saved.get("revision_event_id"),
    }


def _research_source_rows(sources: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not isinstance(sources, list):
        return rows
    for item in sources[:20]:
        if isinstance(item, dict):
            title = _payload_text_summary(item.get("title") or item.get("name") or "Source", 160)
            url = _payload_text_summary(item.get("url") or item.get("href") or "", 240)
            notes = _payload_text_summary(item.get("notes") or item.get("summary") or "", 240)
        else:
            title = _payload_text_summary(item, 160)
            url = ""
            notes = ""
        if not title or title == "[REDACTED]":
            title = "Source"
        rows.append({"title": title, "url": url, "notes": notes})
    return rows


def _research_note_items(notes: Any) -> list[str]:
    if isinstance(notes, list):
        raw_items = notes[:20]
    elif notes is None:
        raw_items = []
    else:
        raw_items = [notes]
    return [_payload_text_summary(item, 300) for item in raw_items]


def set_research_progress(
    space_id: str,
    *,
    phase: Any,
    message: Any,
    sources: Any | None = None,
    notes: Any | None = None,
) -> dict[str, Any]:
    """Update Research Harness live-progress widgets as safe metadata.

    This is the next incremental bridge toward the Space Agent research demo:
    agent runs can advance plan/source/note widgets without exposing raw report
    bodies, generated renderers, executable HTML/script, or secret-looking values
    through public Spaces APIs.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    sid = validate_space_id(space_id)
    safe_phase = _payload_text_summary(phase or "working", 120)
    safe_message = _payload_text_summary(message or "Research progress updated.", 240)
    if not safe_phase or safe_phase == "[REDACTED]":
        safe_phase = "working"
    if not safe_message:
        safe_message = "Research progress updated."

    plan_result = patch_widget(
        sid,
        "research-plan",
        {"status": {"phase": safe_phase, "message": safe_message, "progress": "updated"}},
    )
    source_rows = _research_source_rows(sources)
    sources_result = patch_widget(
        sid,
        "research-sources",
        {"table": {"columns": ["title", "url", "notes"], "rows": source_rows, "source_count": len(source_rows)}},
    )
    note_items = _research_note_items(notes)
    notes_result = patch_widget(
        sid,
        "research-notes",
        {"notes": {"status": "updated", "items": note_items, "item_count": len(note_items)}},
    )
    return {
        "space_id": sid,
        "widgets": {
            "plan": read_widget_detail(sid, "research-plan"),
            "sources": read_widget_detail(sid, "research-sources"),
            "notes": read_widget_detail(sid, "research-notes"),
        },
        "revision_event_id": notes_result["revision_event_id"],
        "updated_revision_event_ids": [
            plan_result["revision_event_id"],
            sources_result["revision_event_id"],
            notes_result["revision_event_id"],
        ],
    }


def set_research_artifact(space_id: str, title: Any, markdown: Any) -> dict[str, Any]:
    """Record a Research Harness markdown artifact as safe metadata.

    This is an incremental bridge toward the Space Agent research demo: agent
    runs can mark the summary report ready for export without returning raw
    markdown, generated renderer bodies, or secret-looking payloads through
    public Spaces APIs.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    sid = validate_space_id(space_id)
    text = str(markdown or "")
    safe_title = _payload_text_summary(title or "Research report", 160)
    if not safe_title or safe_title == "[REDACTED]":
        safe_title = "Research report"
    artifact_value = {
        "title": safe_title,
        "format": "markdown",
        "status": "ready",
        "char_count": len(text),
        "line_count": len(text.splitlines()),
        "word_count": len(re.findall(r"\S+", text)),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    artifact = set_shared_data_slot(
        sid,
        "research-summary",
        artifact_value,
        {
            "source_widget": "research-summary",
            "artifact_kind": "markdown",
            "export_pdf": "ready-for-user-request",
        },
    )["item"]
    widget_result = patch_widget(
        sid,
        "research-summary",
        {
            "status": {"artifact": "ready"},
            "export": {"pdf": "ready-for-user-request", "artifact_key": "research-summary"},
        },
    )
    return {
        "space_id": sid,
        "artifact": artifact,
        "widget": widget_result["widget"],
        "revision_event_id": widget_result["revision_event_id"],
    }


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
    persisted_space = read_space_detail(space_id)
    persisted_widgets = list_widgets(space_id)
    persistence_checked = persisted_space.get("space_id") == space_id and len(persisted_widgets) == len(widgets)
    return {
        "ok": True,
        "demo": demo,
        "template": template,
        "mode": "metadata-only-smoke",
        "action": action,
        "space": persisted_space,
        "widgets": widgets,
        "widget_count": len(widgets),
        "persisted_widget_count": len(persisted_widgets),
        "persistence_checked": persistence_checked,
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
    extra: dict[str, Any] = {}

    if demo == "demo_weather_widget":
        weather_patch = {
            "location": "Prague",
            "country": "CZ",
            "units": "metric",
            "status": "observation-ready",
            "current": {
                "condition": "partly cloudy",
                "temperature_c": "18",
                "feels_like_c": "17",
            },
            "summary": "Partly cloudy in Prague; refreshed through agent-mediated weather metadata.",
        }
        patch_widget(space_id, "weather-current", {"weather": weather_patch})
        queued = queue_widget_event(
            space_id,
            "weather-current",
            "widget.refresh",
            {"demo": demo, "location": "Prague", "units": "metric"},
            prompt="Refresh Prague weather metadata through the agent-mediated bridge.",
        )
        queued_events = list_widget_events(space_id, "weather-current")
        action = "weather-observation-recorded"
        extra = {
            "queued_event": queued,
            "queued_event_count": len(queued_events),
            "weather_observation": {"widget": read_widget_detail(space_id, "weather-current")},
            "prompt_flow": {
                "blank_space": True,
                "query": "What is the weather in Prague?",
                "chat_answer_status": "recorded",
                "answer_preview": "Prague is partly cloudy at 18 °C; the answer is now saved as safe widget metadata.",
                "widget_request": "show it to me in a widget",
                "widget_created": True,
                "reload_verified": True,
                "network_mode": "agent-mediated",
            },
        }
    elif demo == "demo_notes_app":
        editor_notes = {
            "status": "draft-saved",
            "format": "markdown",
            "body": "Demo note draft saved through typed Capy Spaces metadata.",
        }
        preview_notes = {
            "format": "markdown",
            "body": "# Demo note\n\nThis markdown preview was saved as metadata-only state.",
        }
        demo_folders = [
            {"id": "folder-inbox", "title": "Inbox"},
            {"id": "folder-demo", "title": "Demo Project"},
        ]
        demo_attachments = {
            "status": "agent-mediated",
            "storage": "agent-mediated",
            "items": [
                {"id": "attachment-demo-markdown", "name": "demo-note.md", "kind": "markdown", "status": "ready"},
                {"id": "attachment-whiteboard", "name": "whiteboard.png", "kind": "image", "status": "planned"},
            ],
        }
        folder_widget = read_widget(space_id, "notes-folders")
        folder_widget["folders"] = demo_folders
        folder_widget["interaction"] = {
            "rename": "metadata-only",
            "create_folder": "metadata-only",
            "active_folder_id": "folder-demo",
        }
        upsert_widget(space_id, folder_widget)
        patch_widget(space_id, "notes-editor", {"notes": editor_notes})
        patch_widget(space_id, "notes-preview", {"notes": preview_notes})
        patch_widget(space_id, "notes-attachments", {"attachments": demo_attachments})
        queued = queue_widget_event(
            space_id,
            "notes-editor",
            "notes.save",
            {"action": "save-note", "demo": demo, "target": "notes-editor"},
            prompt="Save the demo note through the typed metadata-only notes bridge.",
        )
        queued_events = list_widget_events(space_id, "notes-editor")
        action = "notes-draft-saved"
        extra = {
            "queued_event": queued,
            "queued_event_count": len(queued_events),
            "notes_artifact": {
                "folders": read_widget_detail(space_id, "notes-folders"),
                "editor": read_widget_detail(space_id, "notes-editor"),
                "preview": read_widget_detail(space_id, "notes-preview"),
                "attachments": read_widget_detail(space_id, "notes-attachments"),
            },
            "notes_flow": {
                "folders_ready": True,
                "folder_count": len(demo_folders),
                "active_folder": "Demo Project",
                "editor_saved": True,
                "markdown_preview_saved": True,
                "attachments_agent_mediated": True,
                "attachment_count": len(demo_attachments["items"]),
            },
        }
    elif demo == "demo_kanban_board":
        board_columns = [
            (
                "kanban-backlog",
                {
                    "status": "board-ready",
                    "column": "Backlog",
                    "color": "blue",
                    "cards": [{"id": "card-plan", "title": "Plan the first task", "status": "todo"}],
                    "interaction": {"drag_drop": "planned", "edit_cards": "metadata-only"},
                },
            ),
            (
                "kanban-doing",
                {
                    "status": "board-ready",
                    "column": "Doing",
                    "color": "amber",
                    "cards": [
                        {
                            "id": "card-build",
                            "title": "Build metadata-only board preview",
                            "status": "doing",
                        }
                    ],
                    "interaction": {"drag_drop": "planned", "edit_cards": "metadata-only"},
                },
            ),
            (
                "kanban-done",
                {
                    "status": "board-ready",
                    "column": "Done",
                    "color": "green",
                    "cards": [{"id": "card-install", "title": "Install board template", "status": "done"}],
                    "interaction": {"drag_drop": "planned", "edit_cards": "metadata-only"},
                },
            ),
        ]
        for widget_id, kanban_metadata in board_columns:
            patch_widget(space_id, widget_id, {"kanban": kanban_metadata})
        columns = [read_widget_detail(space_id, widget_id) for widget_id, _ in board_columns]
        action = "kanban-board-seeded"
        extra = {
            "kanban_board": {
                "status": "board-ready",
                "column_count": len(columns),
                "columns": columns,
            }
        }
    elif demo == "demo_snake_iterative_repair":
        bug_report = "Snake canvas needs explicit keyboard focus and collision repair before rendering is enabled."
        patch_widget(
            space_id,
            "game-repair-notes",
            {
                "notes": {
                    "status": "repair-queued",
                    "summary": "Agent repair queued for keyboard focus and collision checks.",
                },
                "repair_loop": {"iterative_patch": "queued", "rollback": "revision-history"},
            },
        )
        queued = queue_widget_event(
            space_id,
            "game-repair-notes",
            "agent.repair",
            {"demo": demo, "game": "snake", "issue": "keyboard-focus-and-collision"},
            prompt="Repair the Snake canvas metadata plan: keep generated rendering disabled, require explicit keyboard focus, and prepare collision fixes behind rollback.",
        )
        queued_events = list_widget_events(space_id, "game-repair-notes")
        action = "snake-repair-queued"
        extra = {
            "queued_event": queued,
            "queued_event_count": len(queued_events),
            "snake_repair_flow": {
                "game": "snake",
                "first_attempt": "broken-placeholder",
                "bug_report": bug_report,
                "repair_event": "agent.repair",
                "render_status": "generated-code-disabled",
                "focus_policy": "explicit-click",
                "rollback": "revision-history",
            },
        }
    elif demo == "demo_stock_chart":
        snapshot_rows = [
            {"symbol": "NVDA", "last": "905.10", "change": "+1.8%", "notes": "GPU demand watch"},
            {"symbol": "AAPL", "last": "182.40", "change": "-0.3%", "notes": "services margin watch"},
            {"symbol": "GOOGL", "last": "171.25", "change": "+0.6%", "notes": "AI search watch"},
        ]
        market_snapshot = {
            "status": "market-snapshot-ready",
            "symbols": ["NVDA", "AAPL", "GOOGL"],
            "network_mode": "agent-mediated",
            "rows": snapshot_rows,
        }
        patch_widget(
            space_id,
            "stock-chart",
            {
                "market_data": {
                    "status": "market-snapshot-ready",
                    "series": market_snapshot["symbols"],
                    "network": "agent-mediated",
                    "rows": snapshot_rows,
                }
            },
        )
        patch_widget(
            space_id,
            "stock-watchlist",
            {"watchlist": {"status": "market-snapshot-ready", "rows": snapshot_rows}},
        )
        queued = queue_widget_event(
            space_id,
            "stock-chart",
            "stock.refresh",
            {"demo": demo, "symbols": market_snapshot["symbols"]},
            prompt="Refresh the stock chart snapshot through the agent-mediated market-data bridge.",
        )
        queued_events = list_widget_events(space_id, "stock-chart")
        action = "stock-snapshot-recorded"
        extra = {
            "queued_event": queued,
            "queued_event_count": len(queued_events),
            "stock_snapshot": market_snapshot,
        }
    elif demo == "demo_step_sequencer_piano_roll":
        patch_widget(
            space_id,
            "music-sequencer-grid",
            {
                "status": {"pattern": "demo-pattern-saved", "steps": 16},
                "audio_policy": {
                    "permission": "explicit-user-gesture",
                    "webaudio": "disabled-until-approved",
                    "cleanup": "planned-on-rerender",
                },
            },
        )
        patch_widget(
            space_id,
            "music-piano-roll",
            {
                "interaction": {"keyboard": "explicit-focus", "editing": "metadata-only"},
                "audio_policy": {"permission": "explicit-user-gesture", "cleanup": "planned-on-rerender"},
            },
        )
        queued = queue_widget_event(
            space_id,
            "music-sequencer-grid",
            "audio.pattern.save",
            {"demo": demo, "pattern_steps": 16, "target": "sequencer-and-piano-roll"},
            prompt="Save the demo step sequencer pattern as safe metadata; keep WebAudio disabled until explicit user approval.",
        )
        queued_events = list_widget_events(space_id, "music-sequencer-grid")
        action = "music-pattern-seeded"
        extra = {
            "queued_event": queued,
            "queued_event_count": len(queued_events),
            "music_flow": {
                "sequencer_ready": True,
                "pattern_steps": 16,
                "piano_roll_ready": True,
                "webaudio_permission": "explicit-user-gesture",
                "cleanup": "planned-on-rerender",
            },
        }
    elif demo == "demo_local_agent_control_dashboard":
        patch_widget(
            space_id,
            "service-health",
            {"refresh": {"mode": "agent-mediated", "status": "health-check-queued"}},
        )
        queued = queue_widget_event(
            space_id,
            "service-health",
            "service.status.check",
            {"demo": demo, "checks": ["/health", "api/status"]},
            prompt="Check approved local service health endpoints through the agent-mediated bridge.",
        )
        queued_events = list_widget_events(space_id, "service-health")
        action = "local-service-dashboard-seeded"
        extra = {
            "queued_event": queued,
            "queued_event_count": len(queued_events),
            "service_flow": {
                "api_chat": "metadata-only",
                "browser_panel": "about:blank",
                "health_checks": "queued",
                "settings_review": "metadata-only",
                "network_mode": "explicit-approval",
            },
        }
    elif demo == "demo_time_travel_restore":
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
    elif demo == "demo_research_harness_pdf_export":
        progress = set_research_progress(
            space_id,
            phase="summary",
            message="Summary artifact ready for PDF export.",
            sources=[{"title": "Demo research brief", "url": "https://example.test/research", "notes": "metadata-only smoke"}],
            notes=["Research plan, source review, notes, and summary metadata completed."],
        )
        artifact = set_research_artifact(
            space_id,
            "Research Harness PDF export smoke",
            "# Research Harness PDF export smoke\n\nMetadata-only demo artifact ready for export.",
        )
        rollback_event_id = str(artifact.get("revision_event_id") or "")
        queued = queue_widget_event(
            space_id,
            "research-summary",
            "widget.export.pdf",
            {"artifact": "research-summary", "format": "pdf", "demo": demo},
            prompt="Export the ready research summary artifact as a PDF when approved.",
        )
        restored = restore_revision(space_id, rollback_event_id) if rollback_event_id else {"space": {"widgets": []}}
        queued_events_after_restore = list_widget_events(space_id, "research-summary")
        action = "pdf-export-requested"
        extra = {
            "research_progress": progress,
            "research_artifact": artifact,
            "queued_event": queued,
            "queued_event_count": len(queued_events_after_restore),
            "research_rollback_check": {
                "verified": bool(restored.get("ok") is True and queued_events_after_restore),
                "restored_event_id": rollback_event_id,
                "restored_widget_count": len((restored.get("space") or {}).get("widgets") or []),
                "replayed_after_restore": bool(
                    queued_events_after_restore
                    and queued_events_after_restore[0].get("event_id") == queued.get("event_id")
                ),
            },
        }

    summary = _space_demo_run_summary(demo, template, space_id, action=action)
    summary.update(extra)
    return summary


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


def _space_tool_widget_payload(widget: dict[str, Any]) -> dict[str, Any]:
    """Return a source-widget payload stripped to safe Capy metadata fields."""
    if not isinstance(widget, dict):
        raise ValueError("widget must be an object")
    clean: dict[str, Any] = {}
    if widget.get("id") or widget.get("widget_id"):
        clean["id"] = widget.get("id") or widget.get("widget_id")
    if widget.get("kind") or widget.get("type"):
        clean["kind"] = widget.get("kind") or widget.get("type")
    if widget.get("title") or widget.get("name"):
        clean["title"] = widget.get("title") or widget.get("name")
    if isinstance(widget.get("layout"), dict):
        clean["layout"] = widget["layout"]
    for field in _WIDGET_DETAIL_METADATA_FIELDS:
        if field not in widget:
            continue
        summary = _payload_summary(widget.get(field))
        if summary in ({}, [], ""):
            continue
        clean[field] = summary
    return clean


def _space_tool_source_widget_layout(widget: dict[str, Any]) -> dict[str, Any]:
    """Return a Capy layout from Space Agent widget size/position fields."""
    if isinstance(widget.get("layout"), dict):
        raw_layout = dict(widget["layout"])
        if "cols" in raw_layout and "w" not in raw_layout:
            raw_layout["w"] = raw_layout.get("cols")
        if "rows" in raw_layout and "h" not in raw_layout:
            raw_layout["h"] = raw_layout.get("rows")
        if "col" in raw_layout and "x" not in raw_layout:
            raw_layout["x"] = raw_layout.get("col")
        if "row" in raw_layout and "y" not in raw_layout:
            raw_layout["y"] = raw_layout.get("row")
        return _normalize_widget_layout(raw_layout)
    position = widget.get("position") if isinstance(widget.get("position"), dict) else {}
    size = widget.get("size") if isinstance(widget.get("size"), dict) else {}
    return _normalize_widget_layout(
        {
            "x": widget.get("x", widget.get("col", position.get("x", position.get("col", 0)))),
            "y": widget.get("y", widget.get("row", position.get("y", position.get("row", 0)))),
            "w": widget.get("w", widget.get("cols", size.get("w", size.get("cols", 6)))),
            "h": widget.get("h", widget.get("rows", size.get("h", size.get("rows", 4)))),
            "minimized": widget.get("minimized", False),
        }
    )



def _space_tool_render_widget_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Convert source-style renderWidget input to safe quarantined metadata."""
    widget = payload.get("widget") if isinstance(payload.get("widget"), dict) else payload
    if not isinstance(widget, dict):
        raise ValueError("widget must be an object")
    clean = _space_tool_widget_payload(widget)
    if isinstance(widget.get("metadata"), dict):
        metadata = _payload_summary(widget.get("metadata"))
        if isinstance(metadata, dict) and metadata:
            clean["metadata"] = metadata
    widget_id = _space_tool_widget_id(widget) or _slugify(str(widget.get("title") or widget.get("name") or "widget"))
    clean["id"] = validate_widget_id(widget_id)
    clean["layout"] = _space_tool_source_widget_layout(widget)

    unsafe_payload = {
        str(key): widget.get(key)
        for key in widget
        if str(key or "").strip().lower() != "metadata" and not _payload_key_is_safe(str(key))
    }
    omitted_count = len(unsafe_payload)
    if omitted_count:
        digest = hashlib.sha256(json.dumps(unsafe_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        clean["recovery"] = {
            "disabled": True,
            "disabled_reason": "generated code disabled pending sandbox review",
        }
        clean["content_status"] = {
            "status": "quarantined",
            "reason": "generated-code-disabled",
            "sha256": digest,
            "omitted_field_count": omitted_count,
        }
        permissions = clean.get("permissions") if isinstance(clean.get("permissions"), dict) else {}
        clean["permissions"] = {**permissions, "generated_rendering": "disabled"}
    return clean, omitted_count



def _space_tool_preview_widget_detail(widget: dict[str, Any]) -> dict[str, Any]:
    """Return public widget detail metadata for a non-persisted tool preview."""
    detail = _widget_summary(widget)
    metadata = _widget_detail_metadata(widget)
    content_status = metadata.get("content_status") if isinstance(metadata.get("content_status"), dict) else None
    if content_status:
        content_status.pop("sha256", None)
    if metadata:
        detail["metadata"] = metadata
    recovery = widget.get("recovery") if isinstance(widget.get("recovery"), dict) else {}
    if recovery:
        detail["recovery"] = _payload_summary(recovery)
    return detail


def _space_creator_target_space_id(payload: dict[str, Any]) -> str | None:
    """Return an explicit existing-space target id for creator previews/commits."""
    for key in ("target_space_id", "targetSpaceId", "space_id", "spaceId"):
        raw = payload.get(key)
        if raw:
            return validate_space_id(str(raw))
    return None


def _space_creator_sanitized_draft(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a sanitized creator-loop draft shared by preview and commit gates."""
    explicit_prompt = any(key in payload for key in ("prompt", "request"))
    prompt_text = str(payload.get("prompt") or payload.get("request") or payload.get("description") or "")
    prompt_summary = _payload_text_summary(prompt_text, 500)
    unsafe_prompt_redacted = prompt_summary == "[REDACTED]"
    target_space_id = _space_creator_target_space_id(payload)
    target_space: dict[str, Any] | None = None
    if target_space_id:
        if not _manifest_path(target_space_id).exists():
            raise FileNotFoundError("Target Space not found")
        target_space = read_space(target_space_id)

    raw_space_name = (
        payload.get("spaceName")
        or payload.get("space_name")
        or payload.get("name")
        or (target_space.get("name") if target_space else "Creator Preview")
    )
    safe_space_name = _payload_text_summary(raw_space_name, 120)
    if not safe_space_name or safe_space_name == "[REDACTED]":
        safe_space_name = _payload_text_summary(target_space.get("name"), 120) if target_space else "Creator Preview"
    if not safe_space_name or safe_space_name == "[REDACTED]":
        safe_space_name = "Creator Preview"
    space_id = target_space_id or validate_space_id(_source_slugify_segment(safe_space_name, "creator-preview")[:64])
    space: dict[str, Any] = {"space_id": space_id, "name": safe_space_name}
    raw_description = payload.get("description") or payload.get("summary") or (target_space.get("description") if target_space else "")
    safe_description = _payload_text_summary(raw_description or "", 300)
    if (target_space is not None or explicit_prompt) and safe_description and safe_description != "[REDACTED]":
        space["description"] = safe_description

    raw_widgets = payload.get("widgets") if isinstance(payload.get("widgets"), list) else []
    if not raw_widgets:
        raw_widgets = [
            {
                "widgetId": "creator-preview",
                "name": "Creator Preview",
                "type": "markdown",
                "metadata": {"summary": "Bounded creator preview; commit requires approval."},
            }
        ]

    widget_payloads: list[dict[str, Any]] = []
    widget_details: list[dict[str, Any]] = []
    used_widget_ids: set[str] = set()
    omitted_field_count = 1 if unsafe_prompt_redacted else 0
    for index, raw_widget in enumerate(raw_widgets[:20], start=1):
        if not isinstance(raw_widget, dict):
            continue
        safe_widget, creator_omitted = _space_creator_safe_widget_input(raw_widget, index, used_widget_ids)
        widget_payload, omitted_count = _space_tool_render_widget_payload({"widget": safe_widget})
        widget_payloads.append(widget_payload)
        widget_details.append(_space_tool_preview_widget_detail(widget_payload))
        omitted_field_count += omitted_count + creator_omitted

    return {
        "space": space,
        "widget_payloads": widget_payloads,
        "widget_details": widget_details,
        "safety": {
            "prompt_echoed": False,
            "unsafe_prompt_redacted": unsafe_prompt_redacted,
            "generated_bodies_rendered": False,
            "omitted_field_count": omitted_field_count,
        },
    }


def _space_creator_preview_gates() -> dict[str, bool]:
    return {
        "sandbox_preview_required": True,
        "visual_qa_required": True,
        "approve_commit_required": True,
    }


def _space_creator_preview_spec(draft: dict[str, Any]) -> dict[str, Any]:
    widgets = copy.deepcopy(draft["widget_details"])
    return {"space": copy.deepcopy(draft["space"]), "widgets": widgets, "widget_count": len(widgets)}


def _space_creator_revision_candidate(draft: dict[str, Any], current_space: dict[str, Any]) -> dict[str, Any]:
    """Build the metadata-only manifest shape a creator commit would revise to."""
    space = draft["space"]
    return {
        "schema_version": SCHEMA_VERSION,
        "space_id": space["space_id"],
        "name": space["name"],
        "description": space.get("description", ""),
        "agent_instructions": "",
        "template": "creator-loop",
        "layout": {"columns": 24},
        "widgets": copy.deepcopy(draft["widget_payloads"]),
        "capabilities": {
            "creator_loop": {
                "mode": "metadata-only",
                "sandbox_previewed": False,
                "visual_qa_passed": False,
                "generated_bodies_rendered": False,
            }
        },
    }


def _space_creator_prune_preview_receipts_locked(now: float | None = None) -> None:
    current = time.time() if now is None else now
    expired = [
        preview_id
        for preview_id, receipt in _CREATOR_PREVIEW_RECEIPTS.items()
        if current - float(receipt.get("created_at") or 0) > _CREATOR_PREVIEW_TTL_SECONDS
    ]
    for preview_id in expired:
        _CREATOR_PREVIEW_RECEIPTS.pop(preview_id, None)
    if len(_CREATOR_PREVIEW_RECEIPTS) <= _CREATOR_PREVIEW_CACHE_MAX:
        return
    ordered = sorted(_CREATOR_PREVIEW_RECEIPTS.items(), key=lambda item: float(item[1].get("created_at") or 0))
    for preview_id, _receipt in ordered[: max(0, len(_CREATOR_PREVIEW_RECEIPTS) - _CREATOR_PREVIEW_CACHE_MAX)]:
        _CREATOR_PREVIEW_RECEIPTS.pop(preview_id, None)


def _space_creator_prune_preview_receipts(now: float | None = None) -> None:
    with _CREATOR_PREVIEW_RECEIPTS_LOCK:
        _space_creator_prune_preview_receipts_locked(now)


def _space_creator_store_preview_receipt(draft: dict[str, Any]) -> str:
    preview_id = f"creator-preview-{uuid.uuid4().hex}"
    with _CREATOR_PREVIEW_RECEIPTS_LOCK:
        _space_creator_prune_preview_receipts_locked()
        _CREATOR_PREVIEW_RECEIPTS[preview_id] = {"created_at": time.time(), "draft": copy.deepcopy(draft)}
        _space_creator_prune_preview_receipts_locked()
    return preview_id


def _space_creator_draft_for_commit(payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    preview_id = str(payload.get("preview_id") or payload.get("previewId") or "").strip()
    if not preview_id:
        return _space_creator_sanitized_draft(payload), None
    with _CREATOR_PREVIEW_RECEIPTS_LOCK:
        _space_creator_prune_preview_receipts_locked()
        receipt = _CREATOR_PREVIEW_RECEIPTS.pop(preview_id, None)
    if not receipt or not isinstance(receipt.get("draft"), dict):
        raise ValueError("Creator preview is unavailable or expired")
    return copy.deepcopy(receipt["draft"]), preview_id


def _space_creator_preview_payload(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded, non-persisted creator-loop preview spec.

    This is the first generic creator-loop gate: accept an untrusted prompt plus
    optional proposed widgets, then produce a metadata-only draft contract for
    sandbox preview / visual QA without creating a Space, executing generated
    bodies, or echoing raw prompt/source/auth material.
    """
    draft = _space_creator_sanitized_draft(payload)
    widgets = draft["widget_details"]
    preview_id = _space_creator_store_preview_receipt(draft)
    response = {
        "ok": True,
        "action": name,
        "preview_id": preview_id,
        "stage": "sandbox-preview-required",
        "stored": False,
        "executed": False,
        "gates": _space_creator_preview_gates(),
        "spec": _space_creator_preview_spec(draft),
        "creator_loop": {
            "stage": "bounded-spec-preview",
            "mode": "metadata-only",
            "stored": False,
            "executed": False,
            "requires_sandbox_preview": True,
            "requires_visual_qa": True,
            "commit_requires_revision": True,
        },
        "space": draft["space"],
        "widgets": widgets,
        "widget_count": len(widgets),
        "safety": draft["safety"],
    }
    target_space_id = _space_creator_target_space_id(payload)
    if target_space_id and _manifest_path(target_space_id).exists():
        current_space = read_space(target_space_id)
        candidate = _space_creator_revision_candidate(draft, current_space)
        response["revision_preview"] = _restore_preview_summary(candidate, target_space_id)
        response["revision_diff"] = _restore_diff_summary(candidate, current_space)
    return response


def _space_creator_commit_payload(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a creator-loop draft only after sandbox, visual-QA, and approval gates."""
    sandbox_previewed = _truthy_bool(payload.get("sandbox_previewed") or payload.get("sandboxPreviewed"))
    visual_qa_passed = _truthy_bool(payload.get("visual_qa_passed") or payload.get("visualQaPassed"))
    approve_commit = _truthy_bool(payload.get("approve_commit") or payload.get("approveCommit") or payload.get("commit_approved"))
    if not (sandbox_previewed and visual_qa_passed and approve_commit):
        raise ValueError("Creator commit requires sandbox preview, visual QA, and explicit approval")

    draft, preview_id = _space_creator_draft_for_commit(payload)
    space = draft["space"]
    create_payload = {
        "space_id": space["space_id"],
        "name": space["name"],
        "description": space.get("description", ""),
        "template": "creator-loop",
        "layout": {"columns": 24},
        "widgets": draft["widget_payloads"],
        "capabilities": {
            "creator_loop": {
                "mode": "metadata-only",
                "sandbox_previewed": True,
                "visual_qa_passed": True,
                "generated_bodies_rendered": False,
            }
        },
    }
    revision_preview: dict[str, Any] | None = None
    revision_diff: dict[str, Any] | None = None
    if _manifest_path(space["space_id"]).exists():
        if not spaces_enabled():
            raise RuntimeError("Capy Spaces is disabled")
        existing = read_space(space["space_id"])
        revised_manifest = {
            "schema_version": SCHEMA_VERSION,
            "space_id": space["space_id"],
            "name": space["name"],
            "description": space.get("description", ""),
            "agent_instructions": "",
            "template": "creator-loop",
            "created_at": existing.get("created_at") or time.time(),
            "updated_at": existing.get("updated_at") or time.time(),
            "layout": create_payload["layout"],
            "widgets": create_payload["widgets"],
            "capabilities": create_payload["capabilities"],
            "recovery": existing.get("recovery") if isinstance(existing.get("recovery"), dict) else {"safe_mode_available": True},
            "revision_events": list(existing.get("revision_events") or []),
            "revision_event_id": existing.get("revision_event_id"),
        }
        created = _write_manifest(
            revised_manifest,
            "space.creator.committed",
            {"mode": "metadata-only", "widget_count": len(draft["widget_payloads"])},
        )
        revision_preview = _restore_preview_summary(created, created["space_id"])
        revision_diff = _restore_diff_summary(created, existing)
    else:
        created = create_space(create_payload)
    widgets = [read_widget_detail(created["space_id"], widget["id"]) for widget in draft["widget_payloads"]]
    space_detail = read_space_detail(created["space_id"])
    space_detail["widget_count"] = len(widgets)
    response = {
        "ok": True,
        "action": name,
        "preview_id": preview_id,
        "stage": "revisioned-commit",
        "stored": True,
        "executed": False,
        "creator_loop": {
            "stage": "revisioned-commit",
            "mode": "metadata-only",
            "stored": True,
            "executed": False,
            "sandbox_previewed": True,
            "visual_qa_passed": True,
            "revision_created": bool(created.get("revision_event_id")),
        },
        "space_id": created["space_id"],
        "space": space_detail,
        "widgets": widgets,
        "widget_count": len(widgets),
        "revision_event_id": created.get("revision_event_id"),
        "safety": draft["safety"],
    }
    if revision_preview is not None:
        response["revision_preview"] = revision_preview
    if revision_diff is not None:
        response["revision_diff"] = revision_diff
    return response


def _space_creator_safe_widget_input(raw_widget: dict[str, Any], index: int, used_widget_ids: set[str]) -> tuple[dict[str, Any], int]:
    """Sanitize creator-preview widget identity fields before generic preview handling."""
    omitted = 0
    raw_title = raw_widget.get("title") or raw_widget.get("name") or ""
    safe_title = _payload_text_summary(raw_title, 120)
    title_is_safe = bool(safe_title and safe_title != "[REDACTED]")
    if not title_is_safe:
        safe_title = f"Creator Widget {index}"
        if raw_title:
            omitted += 1

    raw_kind = raw_widget.get("kind") or raw_widget.get("type") or "markdown"
    safe_kind_text = _payload_text_summary(raw_kind, 80)
    if not safe_kind_text or safe_kind_text == "[REDACTED]":
        safe_kind = "markdown"
        omitted += 1
    else:
        safe_kind = _source_slugify_segment(safe_kind_text, "markdown")[:64] or "markdown"

    raw_id = raw_widget.get("id") or raw_widget.get("widget_id") or raw_widget.get("widgetId") or ""
    safe_id_text = _payload_text_summary(raw_id, 80)
    if safe_id_text and safe_id_text != "[REDACTED]":
        widget_id = _source_slugify_segment(safe_id_text, "creator-widget")[:64]
    elif title_is_safe:
        widget_id = _source_slugify_segment(safe_title, f"creator-widget-{index}")[:64]
        if raw_id:
            omitted += 1
    else:
        widget_id = f"creator-widget-{index}"
        if raw_id:
            omitted += 1
    widget_id = validate_widget_id(widget_id or f"creator-widget-{index}")
    base_widget_id = widget_id
    suffix = 2
    while widget_id in used_widget_ids:
        trimmed = base_widget_id[: max(1, 64 - len(str(suffix)) - 1)]
        widget_id = validate_widget_id(f"{trimmed}-{suffix}")
        suffix += 1
    used_widget_ids.add(widget_id)

    safe_widget = dict(raw_widget)
    for prompt_key in ("prompt", "agent_prompt", "agentPrompt"):
        if prompt_key in safe_widget:
            safe_widget.pop(prompt_key, None)
            omitted += 1
    for metadata_field in (*_WIDGET_DETAIL_METADATA_FIELDS, "metadata"):
        if metadata_field in safe_widget:
            safe_value, stripped_count = _space_creator_strip_prompt_metadata(safe_widget[metadata_field])
            safe_widget[metadata_field] = safe_value
            omitted += stripped_count
    safe_widget["id"] = widget_id
    safe_widget["widgetId"] = widget_id
    safe_widget["widget_id"] = widget_id
    safe_widget["title"] = safe_title
    safe_widget["name"] = safe_title
    safe_widget["kind"] = safe_kind
    safe_widget["type"] = safe_kind
    return safe_widget, omitted


def _space_creator_strip_prompt_metadata(value: Any, depth: int = 0) -> tuple[Any, int]:
    """Remove nested prompt-like keys from creator-preview metadata before summarizing."""
    if depth > 12:
        return "[omitted]", 1
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        omitted = max(0, len(value) - 50)
        for index, (key, item) in enumerate(value.items()):
            if index >= 50:
                break
            if "prompt" in str(key).lower():
                omitted += 1
                continue
            clean_item, nested_omitted = _space_creator_strip_prompt_metadata(item, depth + 1)
            clean[str(key)] = clean_item
            omitted += nested_omitted
        return clean, omitted
    if isinstance(value, list):
        clean_items = []
        omitted = max(0, len(value) - 20)
        for item in value[:20]:
            clean_item, nested_omitted = _space_creator_strip_prompt_metadata(item, depth + 1)
            clean_items.append(clean_item)
            omitted += nested_omitted
        return clean_items, omitted
    return value, 0


def _space_tool_widget_patch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return safe widget patch metadata from source-style helper payloads."""
    clean = _space_tool_widget_payload(payload)
    layout_fields = {"layout", "position", "size", "x", "y", "w", "h", "col", "row", "cols", "rows"}
    if any(field in payload for field in layout_fields):
        clean["layout"] = _space_tool_source_widget_layout(payload)
    return clean


def _space_tool_widgets_payload(payload: dict[str, Any], *, bulk: bool) -> list[dict[str, Any]]:
    raw_widgets = payload.get("widgets") if bulk else [payload.get("widget") if isinstance(payload.get("widget"), dict) else payload]
    if bulk and not isinstance(raw_widgets, list):
        raise ValueError("widgets must be a list")
    return [_space_tool_widget_payload(widget) for widget in raw_widgets]


def _space_tool_arg(payload: dict[str, Any], index: int) -> Any:
    """Return a source-style positional helper argument when present."""
    args = payload.get("args")
    if isinstance(args, (list, tuple)) and 0 <= index < len(args):
        return args[index]
    return ""


def _space_tool_current_id(payload: dict[str, Any]) -> str:
    """Return the optional current-space id from a tool payload."""
    raw = (
        payload.get("space_id")
        or payload.get("spaceId")
        or payload.get("active_space_id")
        or payload.get("activeSpaceId")
        or payload.get("current_space_id")
        or payload.get("currentSpaceId")
        or _space_tool_arg(payload, 0)
        or ""
    )
    return str(raw or "").strip()


def _space_tool_widget_id(payload: dict[str, Any]) -> str:
    """Return a widget id from Hermes or Space Agent-style payloads."""
    raw = payload.get("widget_id") or payload.get("widgetId") or payload.get("id") or _space_tool_arg(payload, 1) or _space_tool_arg(payload, 0)
    return str(raw or "").strip()


def _space_tool_widget_ids(payload: dict[str, Any]) -> list[str]:
    """Return widget ids from Hermes or Space Agent-style bulk payloads."""
    raw = payload.get("widget_ids") or payload.get("widgetIds") or []
    if not isinstance(raw, list):
        raise ValueError("widget_ids must be a list")
    return [validate_widget_id(item) for item in raw]


def _space_tool_space_id(payload: dict[str, Any]) -> str:
    """Return a Space id from Hermes or source-style space helper payloads."""
    return str(
        payload.get("space_id")
        or payload.get("spaceId")
        or payload.get("current_space_id")
        or payload.get("currentSpaceId")
        or payload.get("active_space_id")
        or payload.get("activeSpaceId")
        or payload.get("id")
        or _space_tool_arg(payload, 0)
        or ""
    ).strip()


def _space_tool_sanitize_widgets(space: dict[str, Any]) -> None:
    """Keep manifests crossing source-style helpers metadata-only."""
    widgets = space.get("widgets") if isinstance(space.get("widgets"), list) else []
    space["widgets"] = [_space_tool_widget_payload(widget) for widget in widgets if isinstance(widget, dict)]


def _space_tool_layout_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize source-style Space layout fields without generated bodies."""
    raw_widget_ids = payload.get("widget_ids") or payload.get("widgetIds") or []
    if not isinstance(raw_widget_ids, list):
        raise ValueError("widget_ids must be a list")
    widget_ids = [validate_widget_id(item) for item in raw_widget_ids]

    raw_positions = payload.get("widget_positions") or payload.get("widgetPositions") or {}
    if not isinstance(raw_positions, dict):
        raise ValueError("widget_positions must be an object")
    widget_positions: dict[str, dict[str, int]] = {}
    for widget_id, position in raw_positions.items():
        wid = validate_widget_id(widget_id)
        raw = position if isinstance(position, dict) else {}
        widget_positions[wid] = {
            "x": _clamped_int(raw.get("x"), 0, 0, 10_000),
            "y": _clamped_int(raw.get("y"), 0, 0, 10_000),
        }

    raw_sizes = payload.get("widget_sizes") or payload.get("widgetSizes") or {}
    if not isinstance(raw_sizes, dict):
        raise ValueError("widget_sizes must be an object")
    widget_sizes: dict[str, dict[str, int]] = {}
    for widget_id, size in raw_sizes.items():
        wid = validate_widget_id(widget_id)
        raw = size if isinstance(size, dict) else {}
        widget_sizes[wid] = {
            "w": _clamped_int(raw.get("w"), 6, 1, 24),
            "h": _clamped_int(raw.get("h"), 4, 1, 24),
        }

    raw_minimized = payload.get("minimized_widget_ids") or payload.get("minimizedWidgetIds") or []
    if not isinstance(raw_minimized, list):
        raise ValueError("minimized_widget_ids must be a list")
    minimized_widget_ids = [validate_widget_id(item) for item in raw_minimized]

    return {
        "widget_ids": widget_ids,
        "widget_positions": widget_positions,
        "widget_sizes": widget_sizes,
        "minimized_widget_ids": minimized_widget_ids,
    }


def save_space_meta_from_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Save source-style Space metadata through Capy's safe metadata boundary."""
    space_id = validate_space_id(_space_tool_space_id(payload))
    space = read_space(space_id)
    name = _payload_text_summary(payload.get("name") or payload.get("title"), 120)
    if name and name != "[REDACTED]":
        space["name"] = name
    if "description" in payload:
        description = _payload_text_summary(payload.get("description"), 500)
        space["description"] = "" if description == "[REDACTED]" else description
    instructions_raw = (
        payload.get("agent_instructions")
        or payload.get("agentInstructions")
        or payload.get("specialInstructions")
        or payload.get("instructions")
    )
    if instructions_raw is not None:
        instructions = _payload_text_summary(instructions_raw, 800)
        space["agent_instructions"] = "" if instructions == "[REDACTED]" else instructions
    icon = _payload_text_summary(payload.get("icon"), 40)
    if icon and icon != "[REDACTED]":
        space["icon"] = icon
    icon_color = _payload_text_summary(payload.get("icon_color") or payload.get("iconColor"), 40)
    if icon_color and icon_color != "[REDACTED]":
        space["icon_color"] = icon_color
    _space_tool_sanitize_widgets(space)
    saved = _write_manifest(
        space,
        "space.meta.updated",
        {"fields": [key for key in ("name", "description", "agent_instructions", "icon", "icon_color") if key in space]},
    )
    return {"space_id": saved["space_id"], "revision_event_id": saved["revision_event_id"], "space": read_space_detail(saved["space_id"])}


def save_space_layout_from_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Save source-style Space layout metadata without executable/source fields."""
    space_id = validate_space_id(_space_tool_space_id(payload))
    space = read_space(space_id)
    layout = _space_tool_layout_payload(payload)
    space["layout"] = layout
    _space_tool_sanitize_widgets(space)
    saved = _write_manifest(space, "space.layout.updated", {"layout": _payload_summary(layout)})
    return {"space_id": saved["space_id"], "revision_event_id": saved["revision_event_id"], "space": read_space_detail(saved["space_id"])}



def repair_space_layout_from_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply saved source-style layout metadata to widgets safely."""
    space_id = validate_space_id(_space_tool_space_id(payload))
    space = read_space(space_id)
    layout = space.get("layout") if isinstance(space.get("layout"), dict) else {}
    widget_ids = [validate_widget_id(item) for item in (layout.get("widget_ids") or []) if item]
    positions = layout.get("widget_positions") if isinstance(layout.get("widget_positions"), dict) else {}
    sizes = layout.get("widget_sizes") if isinstance(layout.get("widget_sizes"), dict) else {}
    minimized_ids = set(
        validate_widget_id(item) for item in (layout.get("minimized_widget_ids") or []) if item
    )
    affected_ids = set(widget_ids) or set(positions) | set(sizes) | minimized_ids

    widgets = list(space.get("widgets") or [])
    repaired_ids: list[str] = []
    for idx, widget in enumerate(widgets):
        if not isinstance(widget, dict):
            continue
        widget_id = validate_widget_id(widget.get("id"))
        if affected_ids and widget_id not in affected_ids:
            continue
        current_layout = _normalize_widget_layout(widget.get("layout"))
        position = positions.get(widget_id) if isinstance(positions.get(widget_id), dict) else {}
        size = sizes.get(widget_id) if isinstance(sizes.get(widget_id), dict) else {}
        repaired_layout = _normalize_widget_layout(
            {
                "x": position.get("x", current_layout["x"]),
                "y": position.get("y", current_layout["y"]),
                "w": size.get("w", current_layout["w"]),
                "h": size.get("h", current_layout["h"]),
                "minimized": widget_id in minimized_ids if minimized_ids else current_layout["minimized"],
            }
        )
        repaired_widget = dict(widget)
        repaired_widget["layout"] = repaired_layout
        widgets[idx] = _normalize_widget(repaired_widget)
        repaired_ids.append(widget_id)

    space["widgets"] = widgets
    _space_tool_sanitize_widgets(space)
    saved = _write_manifest(space, "space.layout.repaired", {"widget_ids": repaired_ids})
    return {
        "space_id": saved["space_id"],
        "revision_event_id": saved["revision_event_id"],
        "widgets": [widget for widget in list_widgets(saved["space_id"]) if widget["id"] in set(repaired_ids)],
        "widget_count": len(repaired_ids),
        "space": read_space_detail(saved["space_id"]),
    }



def _space_tool_resolve_app_url(payload: dict[str, Any]) -> str:
    """Resolve a Space Agent-style logical app path without exposing raw unsafe inputs."""
    raw = payload.get("logicalPath") or payload.get("logical_path") or payload.get("path") or ""
    normalized_path = str(raw or "").strip()
    if not normalized_path:
        raise ValueError("A logical app path is required")
    if any(marker in normalized_path for marker in ("?", "#", "\\", "\x00")):
        raise ValueError("Unsupported app path")
    if any(part == ".." for part in normalized_path.split("/")):
        raise ValueError("Unsupported app path")
    if normalized_path == "~":
        return "/~/"
    if normalized_path.startswith("~/"):
        return f"/{normalized_path}"
    if normalized_path.startswith("/app/"):
        return _space_tool_resolve_app_url({"path": normalized_path[len("/app/") :]})
    if normalized_path.startswith("/~/"):
        return normalized_path
    if re.match(r"^/(L0|L1|L2)/", normalized_path):
        return normalized_path
    if re.match(r"^(L0|L1|L2)/", normalized_path):
        return f"/{normalized_path}"
    raise ValueError("Unsupported app path")



def _space_tool_template_name(payload: dict[str, Any], default: str = "weather") -> str:
    """Resolve a safe Capy template name from Hermes or Space Agent-style payloads."""
    raw = payload.get("template") or payload.get("template_name") or payload.get("name") or payload.get("id") or ""
    source_path = str(payload.get("sourcePath") or payload.get("source_path") or "").lower()
    template_name = str(raw or "").strip().lower()
    source_aliases = {
        "daily-news": "dashboard",
        "crypto-dashboard": "dashboard",
        "retro-arcade": "game",
        "agent-zero-videos": "service",
    }
    if template_name in source_aliases:
        return source_aliases[template_name]
    for alias, template in source_aliases.items():
        if f"/{alias}/" in source_path or source_path.endswith(f"/{alias}/space.yaml"):
            return template
    return template_name or default


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

    if name in {"space.api.health", "space.health"}:
        return {
            "ok": True,
            "action": name,
            "name": "Capy Spaces",
            "browserAppUrl": "/?panel=capy-spaces",
            "mode": "metadata-only",
            "schema_version": SCHEMA_VERSION,
            "enabled": True,
            "space_count": len(list_spaces()),
            "responsibilities": [
                "metadata-only space and widget manifests",
                "revision history and safe recovery",
                "agent-mediated widget events",
            ],
        }

    if name in {"space.list", "space.spaces", "space.spaces.list", "space.spaces.listspaces"}:
        return {"ok": True, "action": name, "spaces": list_spaces()}
    if name in {"space.spaces.items", "space.spaces.all"}:
        return {"ok": True, "action": name, "spaces": list_spaces()}
    if name == "space.spaces.widgetapiversion":
        return {
            "ok": True,
            "action": name,
            "widget_api_version": 1,
            "runtime": {"mode": "metadata-only", "executed": False},
        }
    if name == "space.spaces.byid":
        spaces = list_spaces()
        return {"ok": True, "action": name, "spaces_by_id": {space["space_id"]: space for space in spaces}}
    if name in {"space.demo.list", "space.demo.runs"}:
        return {"ok": True, "action": name, "demos": list_space_demo_runs()}
    if name in {"space.demo.run", "space_demo_run"}:
        demo_name = data.get("demo") or data.get("name") or data.get("demo_name") or ""
        return {"action": name, **space_demo_run(demo_name)}
    if name in {"space.demo.run_all", "space.demo.run-all", "space_demo_run_all"}:
        return space_demo_run_all()
    if name in {"space.current", "space.current.get", "space.spaces.current", "space.spaces.getcurrentspace"}:
        current_id = _space_tool_current_id(data)
        if not current_id:
            return {"ok": True, "action": name, "active_space_id": None, "space": None}
        space_id = validate_space_id(current_id)
        return {"ok": True, "action": name, "active_space_id": space_id, "space": read_space_detail(space_id)}
    if name == "space.spaces.currentid":
        current_id = _space_tool_current_id(data)
        space_id = validate_space_id(current_id) if current_id else None
        return {"ok": True, "action": name, "active_space_id": space_id, "current_id": space_id}
    if name in {"space.current.context", "space.context", "space.current.prompt_context"}:
        current_id = _space_tool_current_id(data)
        if not current_id:
            return {"ok": True, "action": name, "active_space_id": None, "context": ""}
        space_id = validate_space_id(current_id)
        return {"ok": True, "action": name, "active_space_id": space_id, "context": build_agent_context(space_id)}
    if name in {"space.current.widgets", "space.current.widget.list", "space.current.listwidgets"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        return {"ok": True, "action": name, "active_space_id": space_id, "widgets": list_widgets(space_id)}
    if name in {"space.current.byid", "space.current.widgetsbyid"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        widgets = list_widgets(space_id)
        return {"ok": True, "action": name, "active_space_id": space_id, "widgets_by_id": {widget["id"]: widget for widget in widgets}}
    if name in {"space.current.agentinstructions", "space.current.specialinstructions"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        instructions = read_space_detail(space_id).get("agent_instructions", "")
        key = "agent_instructions" if name.endswith("agentinstructions") else "special_instructions"
        return {"ok": True, "action": name, "active_space_id": space_id, key: instructions}
    if name in {"space.spaces.listwidgets", "space.spaces.widgets"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        return {"ok": True, "action": name, "space_id": space_id, "widgets": list_widgets(space_id)}
    if name in {"space.widget.list", "space.widgets.list", "space.current.widgets.list"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        return {"ok": True, "action": name, "active_space_id": space_id, "widgets": list_widgets(space_id)}
    if name in {"space.spaces.readwidget", "space.spaces.getwidget"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        widget_id = validate_widget_id(_space_tool_widget_id(data))
        return {"ok": True, "action": name, "space_id": space_id, "widget": read_widget_detail(space_id, widget_id)}
    if name in {"space.widget.read", "space.widget.get", "space.current.widget.read", "space.current.widget.get", "space.current.readwidget", "space.current.getwidget"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        widget_id = validate_widget_id(_space_tool_widget_id(data))
        return {"ok": True, "action": name, "active_space_id": space_id, "widget": read_widget_detail(space_id, widget_id)}
    if name in {"space.widget.see", "space.current.widget.see", "space.current.seewidget", "widget.see"}:
        space_id = validate_space_id(_space_tool_current_id(data) if name.startswith("space.current.") else data.get("space_id"))
        widget_id = validate_widget_id(_space_tool_widget_id(data))
        widget = read_widget(space_id, widget_id)
        return {
            "ok": True,
            "action": name,
            "active_space_id": space_id,
            "widget": read_widget_detail(space_id, widget_id),
            "contract": _widget_runtime_contract_summary(widget),
            "events": list_widget_events(space_id, widget_id, data.get("limit", 5)),
        }
    if name in {"space.widget.runtime_contract", "space.current.widget.runtime_contract", "widget.runtime_contract"}:
        space_id = validate_space_id(_space_tool_current_id(data) if name.startswith("space.current.") else data.get("space_id"))
        widget_id = validate_widget_id(data.get("widget_id") or data.get("id"))
        widget = read_widget(space_id, widget_id)
        return {"ok": True, "action": name, "active_space_id": space_id, "contract": _widget_runtime_contract_summary(widget)}
    if name in {"space.template.install", "space.templates.install", "template.install", "space.spaces.installexamplespace", "space.spaces.installtemplate"}:
        template_name = _space_tool_template_name(data, "weather")
        result = install_template(template_name, space_id=data.get("space_id") or None)
        return {"ok": True, "action": name, **result}
    if name in {"space.template.reset", "space.templates.reset", "template.reset"}:
        template_name = _space_tool_template_name(data, "big-bang")
        result = reset_template(template_name, space_id=data.get("space_id") or None)
        return {"ok": True, "action": name, **result}
    if name in {"space.import", "space.package.import", "space.agent.import"}:
        result = import_space_agent_package(data, space_id=data.get("space_id") or None)
        return {"ok": True, "action": name, **result}
    if name in {
        "space.export",
        "space.package.export",
        "space.agent.export",
        "space.export.yaml",
        "space.export.zip",
        "space.current.export",
        "space.current.package.export",
        "space.current.agent.export",
        "space.current.export.yaml",
        "space.current.export.zip",
    }:
        space_id = validate_space_id(_space_tool_current_id(data) if name.startswith("space.current.") else data.get("space_id"))
        export_format = "zip" if name.endswith(".zip") else "yaml" if name.endswith(".yaml") else data.get("format") or "yaml"
        result = export_space_agent_package(space_id, format=export_format)
        return {"ok": True, "action": name, **result}
    if name in {"space.create", "space.spaces.create", "space.spaces.createspace"}:
        created = create_space(_space_tool_create_payload(data))
        space = read_space_detail(created["space_id"])
        space["widget_count"] = len(space.get("widgets") or [])
        return {"ok": True, "action": name, "space": space}
    if name in {"space.creator.preview", "space.creator.spec.preview", "space.spaces.previewcreatorspec"}:
        return _space_creator_preview_payload(name, data)
    if name in {"space.creator.commit", "space.creator.spec.commit", "space.spaces.commitcreatorspec"}:
        return _space_creator_commit_payload(name, data)
    if name in {
        "space.get",
        "space.spaces.get",
        "space.spaces.read",
        "space.spaces.open",
        "space.spaces.getspace",
        "space.spaces.readspace",
        "space.spaces.openspace",
    }:
        space_id = validate_space_id(_space_tool_space_id(data))
        return {"ok": True, "action": name, "space": read_space_detail(space_id)}
    if name in {"space.spaces.reloadcurrentspace", "space.spaces.reloadspace"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        return {"ok": True, "action": name, "space_id": space_id, "space": read_space_detail(space_id)}
    if name in {
        "space.spaces.buildspacerootpath",
        "space.spaces.buildspacemanifestpath",
        "space.spaces.buildspacewidgetspath",
        "space.spaces.buildspacewidgetfilepath",
        "space.spaces.buildspacedatapath",
        "space.spaces.buildspaceassetspath",
        "space.spaces.buildspacescriptspath",
    }:
        return {"ok": True, "action": name, "path": _space_tool_build_source_path(name, data), "paths": {"mode": "metadata-only"}}
    if name in {"space.spaces.normalizespaceid", "space.spaces.normalizewidgetid"}:
        kind = "space" if name.endswith("spaceid") else "widget"
        return {"ok": True, "action": name, **_space_tool_normalize_id_payload(kind, data)}
    if name == "space.spaces.resolveappurl":
        return {"ok": True, "action": name, "url": _space_tool_resolve_app_url(data), "resolve": {"mode": "metadata-only"}}
    if name == "space.spaces.sizetotoken":
        return {"ok": True, "action": name, **_space_tool_size_to_token(data), "mode": "metadata-only"}
    if name == "space.spaces.defaultwidgetsize":
        size = dict(_SOURCE_WIDGET_DEFAULT_SIZE)
        return {"ok": True, "action": name, "token": f"{size['cols']}x{size['rows']}", "size": size, "mode": "metadata-only"}
    if name == "space.spaces.normalizewidgetsize":
        return {"ok": True, "action": name, **_space_tool_size_to_token(data), "mode": "metadata-only"}
    if name == "space.spaces.parsewidgetsizetoken":
        return {"ok": True, "action": name, **_space_tool_parse_widget_size_token(data), "mode": "metadata-only"}
    if name == "space.spaces.defaultwidgetposition":
        position = dict(_SOURCE_WIDGET_DEFAULT_POSITION)
        return {"ok": True, "action": name, "token": f"{position['col']},{position['row']}", "position": position, "mode": "metadata-only"}
    if name in {"space.spaces.normalizewidgetposition", "space.spaces.positiontotoken"}:
        return {"ok": True, "action": name, **_space_tool_position_to_token(data), "mode": "metadata-only"}
    if name == "space.spaces.parsewidgetpositiontoken":
        return {"ok": True, "action": name, **_space_tool_parse_widget_position_token(data), "mode": "metadata-only"}
    if name == "space.spaces.clampwidgetposition":
        return {"ok": True, "action": name, **_space_tool_clamp_widget_position(data), "mode": "metadata-only"}
    if name == "space.spaces.getrenderedwidgetsize":
        return {"ok": True, "action": name, **_space_tool_get_rendered_widget_size(data), "mode": "metadata-only"}
    if name == "space.spaces.buildcenteredfirstfitlayout":
        return {"ok": True, "action": name, **_space_tool_build_centered_first_fit_layout(data), "mode": "metadata-only"}
    if name == "space.spaces.findfirstfitwidgetplacement":
        return {"ok": True, "action": name, **_space_tool_find_first_fit_widget_placement(data), "mode": "metadata-only"}
    if name == "space.spaces.resolvespacelayout":
        return {"ok": True, "action": name, **_space_tool_resolve_space_layout(data), "mode": "metadata-only"}
    if name in {"space.spaces.repositioncurrentspace", "space.current.reposition", "space.current.reposition_viewport"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        request = {
            "resetCamera": bool(data.get("resetCamera") or data.get("reset_camera")),
            "viewport": _payload_summary(data.get("viewport") if isinstance(data.get("viewport"), dict) else {}),
        }
        return {
            "ok": True,
            "action": name,
            "space_id": space_id,
            "space": read_space_detail(space_id),
            "reposition": {"mode": "metadata-only", "applied": False, "request": request},
        }
    if name in {"space.spaces.duplicatespace", "space.spaces.clonespace"}:
        result = duplicate_space_metadata_only(
            _space_tool_current_id(data),
            target_space_id=data.get("target_space_id") or data.get("targetSpaceId") or None,
        )
        space = read_space_detail(result["space_id"])
        space["widget_count"] = len(space.get("widgets") or [])
        return {"ok": True, "action": name, **result, "space": space}
    if name in {"space.spaces.savespacemeta", "space.current.savemeta"}:
        result = save_space_meta_from_tool(data)
        response = {"ok": True, "action": name, **result}
        if name.startswith("space.current."):
            response["active_space_id"] = result["space_id"]
        return response
    if name in {"space.spaces.savespacelayout", "space.current.savelayout"}:
        result = save_space_layout_from_tool(data)
        response = {"ok": True, "action": name, **result}
        if name.startswith("space.current."):
            response["active_space_id"] = result["space_id"]
        return response
    if name == "space.spaces.repairlayout":
        result = repair_space_layout_from_tool(data)
        return {"ok": True, "action": name, **result}
    if name == "space.spaces.rearrangewidgets":
        space_id = validate_space_id(_space_tool_current_id(data))
        raw_widgets = data.get("widgets") or data.get("widgetLayouts") or data.get("widget_layouts") or []
        if not isinstance(raw_widgets, list):
            raise ValueError("widgets must be a list")
        saved_widgets: list[dict[str, Any]] = []
        revision_event_ids: list[str] = []
        for raw_widget in raw_widgets:
            if not isinstance(raw_widget, dict):
                raise ValueError("widget layout must be an object")
            widget_id = validate_widget_id(_space_tool_widget_id(raw_widget) or raw_widget.get("widgetId"))
            position = raw_widget.get("position") if isinstance(raw_widget.get("position"), dict) else {}
            size = raw_widget.get("size") if isinstance(raw_widget.get("size"), dict) else {}
            layout = {
                "x": raw_widget.get("x", raw_widget.get("col", position.get("x", 0))),
                "y": raw_widget.get("y", raw_widget.get("row", position.get("y", 0))),
                "w": raw_widget.get("w", raw_widget.get("cols", size.get("w", 6))),
                "h": raw_widget.get("h", raw_widget.get("rows", size.get("h", 4))),
                "minimized": raw_widget.get("minimized", False),
            }
            result = patch_widget(space_id, widget_id, {"layout": layout})
            revision_event_ids.append(result["revision_event_id"])
            saved_widgets.append(read_widget_detail(space_id, widget_id))
        return {
            "ok": True,
            "action": name,
            "space_id": space_id,
            "space": read_space_detail(space_id),
            "widgets": saved_widgets,
            "widget_count": len(saved_widgets),
            "revision_event_ids": revision_event_ids,
        }
    if name in {"space.spaces.removespace", "space.spaces.deletespace"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        result = delete_space(space_id)
        return {"ok": True, "action": name, **result}
    if name in {"space.spaces.upsertwidget", "space.spaces.upsertwidgets"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        widgets = _space_tool_widgets_payload(data, bulk=name.endswith("upsertwidgets"))
        saved_widgets: list[dict[str, Any]] = []
        revision_event_ids: list[str] = []
        for widget in widgets:
            result = upsert_widget(space_id, widget)
            saved_widgets.append(read_widget_detail(space_id, result["widget"]["id"]))
            revision_event_ids.append(result["revision_event_id"])
        response: dict[str, Any] = {
            "ok": True,
            "action": name,
            "space_id": space_id,
            "widgets": saved_widgets,
            "widget_count": len(saved_widgets),
            "revision_event_ids": revision_event_ids,
        }
        if name.endswith("upsertwidget") and saved_widgets:
            response["widget"] = saved_widgets[0]
            response["revision_event_id"] = revision_event_ids[-1]
        return response
    if name == "space.spaces.definewidget":
        space_id = validate_space_id(_space_tool_current_id(data))
        read_space_detail(space_id)
        definition = data.get("definition") if isinstance(data.get("definition"), dict) else data
        widget_payload, omitted_count = _space_tool_render_widget_payload({"widget": definition})
        return {
            "ok": True,
            "action": name,
            "space_id": space_id,
            "widget": _space_tool_preview_widget_detail(widget_payload),
            "blueprint": {
                "mode": "metadata-only",
                "stored": False,
                "executed": False,
                "omitted_field_count": omitted_count,
            },
        }
    if name == "space.spaces.createwidgetsource":
        space_id = validate_space_id(_space_tool_current_id(data))
        read_space_detail(space_id)
        widget_payload, omitted_count = _space_tool_render_widget_payload(data)
        return {
            "ok": True,
            "action": name,
            "space_id": space_id,
            "widget": _space_tool_preview_widget_detail(widget_payload),
            "blueprint": {
                "mode": "metadata-only",
                "stored": False,
                "executed": False,
                "omitted_field_count": omitted_count,
            },
        }
    if name == "space.spaces.previewwidgetrecord":
        space_id = validate_space_id(_space_tool_current_id(data))
        read_space_detail(space_id)
        widget_payload, omitted_count = _space_tool_render_widget_payload(data)
        widget_detail = _space_tool_preview_widget_detail(widget_payload)
        preview_metadata = widget_payload.get("metadata") if isinstance(widget_payload.get("metadata"), dict) else {}
        if preview_metadata:
            widget_detail.setdefault("metadata", {})["preview_metadata"] = preview_metadata
        return {
            "ok": True,
            "action": name,
            "space_id": space_id,
            "widget": widget_detail,
            "preview": {
                "mode": "metadata-only",
                "stored": False,
                "executed": False,
                "omitted_field_count": omitted_count,
            },
        }
    if name == "space.spaces.renderwidget":
        space_id = validate_space_id(_space_tool_current_id(data))
        widget_payload, omitted_count = _space_tool_render_widget_payload(data)
        result = upsert_widget(space_id, widget_payload)
        widget_id = result["widget"]["id"]
        return {
            "ok": True,
            "action": name,
            "space_id": space_id,
            "widget": read_widget_detail(space_id, widget_id),
            "revision_event_id": result["revision_event_id"],
            "render": {"mode": "metadata-only", "executed": False, "omitted_field_count": omitted_count},
        }
    if name in {"space.spaces.patchwidget", "space.current.patchwidget"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        widget_id = validate_widget_id(_space_tool_widget_id(data))
        patch_payload = data.get("patch") if isinstance(data.get("patch"), dict) else data
        result = patch_widget(space_id, widget_id, _space_tool_widget_patch_payload(patch_payload))
        response = {"ok": True, "action": name, **result, "widget": read_widget_detail(space_id, widget_id)}
        if name.startswith("space.current."):
            response["active_space_id"] = space_id
        return response
    if name == "space.spaces.togglewidgets":
        space_id = validate_space_id(_space_tool_current_id(data))
        widget_ids = _space_tool_widget_ids(data)
        toggled_widgets: list[dict[str, Any]] = []
        revision_event_ids: list[str] = []
        for widget_id in widget_ids:
            current = read_widget(space_id, widget_id)
            layout = _normalize_widget_layout(current.get("layout"))
            layout["minimized"] = not layout["minimized"]
            result = patch_widget(space_id, widget_id, {"layout": layout})
            revision_event_ids.append(result["revision_event_id"])
            toggled_widgets.append(read_widget_detail(space_id, widget_id))
        return {
            "ok": True,
            "action": name,
            "space_id": space_id,
            "space": read_space_detail(space_id),
            "widget_ids": widget_ids,
            "widgets": toggled_widgets,
            "widget_count": len(toggled_widgets),
            "revision_event_ids": revision_event_ids,
        }
    if name in {"space.spaces.deletewidget", "space.spaces.removewidget", "space.current.deletewidget", "space.current.removewidget"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        widget_id = validate_widget_id(_space_tool_widget_id(data))
        result = delete_widget(space_id, widget_id)
        response = {"ok": True, "action": name, **result}
        if name.startswith("space.current."):
            response["active_space_id"] = space_id
        return response
    if name in {
        "space.spaces.removewidgets",
        "space.spaces.deletewidgets",
        "space.current.removewidgets",
        "space.current.deletewidgets",
    }:
        space_id = validate_space_id(_space_tool_current_id(data))
        widget_ids = _space_tool_widget_ids(data)
        revision_event_ids: list[str] = []
        for widget_id in widget_ids:
            result = delete_widget(space_id, widget_id)
            revision_event_ids.append(result["revision_event_id"])
        response = {
            "ok": True,
            "action": name,
            "deleted": True,
            "space_id": space_id,
            "widget_ids": widget_ids,
            "deleted_count": len(widget_ids),
            "revision_event_ids": revision_event_ids,
        }
        if name.startswith("space.current."):
            response["active_space_id"] = space_id
        return response
    if name in {
        "space.spaces.removeallwidgets",
        "space.spaces.deleteallwidgets",
        "space.current.removeallwidgets",
        "space.current.deleteallwidgets",
    }:
        space_id = validate_space_id(_space_tool_current_id(data))
        widget_ids = [widget["id"] for widget in list_widgets(space_id)]
        revision_event_ids = []
        for widget_id in widget_ids:
            result = delete_widget(space_id, widget_id)
            revision_event_ids.append(result["revision_event_id"])
        response = {
            "ok": True,
            "action": name,
            "deleted": True,
            "space_id": space_id,
            "widget_ids": widget_ids,
            "deleted_count": len(widget_ids),
            "revision_event_ids": revision_event_ids,
        }
        if name.startswith("space.current."):
            response["active_space_id"] = space_id
        return response
    if name in {"space.data.set", "space.current.data.set"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        result = set_shared_data_slot(space_id, data.get("key"), data.get("value"), data.get("metadata"))
        return {"ok": True, "action": name, **result}
    if name in {"space.data.list", "space.current.data.list"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        return {"ok": True, "action": name, "space_id": space_id, "items": list_shared_data_slots(space_id)}
    if name in {"space.data.get", "space.current.data.get"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        data_key = validate_data_key(data.get("key"))
        return {"ok": True, "action": name, "space_id": space_id, "item": read_shared_data_slot(space_id, data_key)}
    if name in {"space.data.delete", "space.current.data.delete"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        result = delete_shared_data_slot(space_id, data.get("key"))
        return {"ok": True, "action": name, **result}
    if name in {"space.research.artifact.set", "space.current.research.artifact.set", "space.research.report.set", "space.current.research.report.set"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        result = set_research_artifact(space_id, data.get("title") or data.get("name"), data.get("markdown") or data.get("content") or "")
        return {"ok": True, "action": name, **result}
    if name in {
        "space.research.progress.set",
        "space.research.progress.update",
        "space.current.research.progress.set",
        "space.current.research.progress.update",
    }:
        is_current = name.startswith("space.current.")
        space_id = validate_space_id(_space_tool_current_id(data) if is_current else data.get("space_id"))
        result = set_research_progress(
            space_id,
            phase=data.get("phase") or data.get("status") or "working",
            message=data.get("message") or data.get("summary") or "Research progress updated.",
            sources=data.get("sources"),
            notes=data.get("notes"),
        )
        if is_current:
            result["active_space_id"] = space_id
        return {"ok": True, "action": name, **result}
    if name in {"space.revisions", "space.revision.list", "space.history", "space.current.revisions", "space.current.revision.list", "space.current.history"}:
        is_current = name.startswith("space.current.")
        space_id = validate_space_id(_space_tool_current_id(data) if is_current else data.get("space_id"))
        result = {"ok": True, "action": name, "revisions": list_revision_events(space_id, data.get("limit", 20))}
        if is_current:
            result["active_space_id"] = space_id
        else:
            result["space_id"] = space_id
        return result
    if name in {"space.revision.restore", "space.rollback", "space.restore", "space.current.revision.restore", "space.current.rollback", "space.current.restore"}:
        is_current = name.startswith("space.current.")
        space_id = validate_space_id(_space_tool_current_id(data) if is_current else data.get("space_id"))
        event_id = str(data.get("event_id") or data.get("revision_event_id") or "")
        result = restore_revision(space_id, event_id)
        if is_current:
            result["active_space_id"] = space_id
        return {"action": name, **result}
    if name in {"space.recovery", "space.recovery.snapshot", "space.safe_mode", "space.safe_mode.snapshot"}:
        return {"ok": True, "action": name, "recovery": recovery_snapshot()}
    if name in {"space.recovery.disable", "space.recovery.disable_space", "space.safe_mode.disable"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        result = disable_space_for_recovery(space_id, reason=_payload_text_summary(data.get("reason") or "disabled from recovery", 300))
        return {"ok": True, "action": name, **result}
    if name in {"space.recovery.enable", "space.recovery.enable_space", "space.safe_mode.enable"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        result = enable_space_for_recovery(space_id, reason=_payload_text_summary(data.get("reason") or "enabled from recovery", 300))
        return {"ok": True, "action": name, **result}
    if name in {"space.recovery.disable_widget", "space.widget.recovery.disable", "widget.recovery.disable"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        widget_id = validate_widget_id(data.get("widget_id") or data.get("id"))
        result = disable_widget_for_recovery(space_id, widget_id, reason=_payload_text_summary(data.get("reason") or "disabled from recovery", 300))
        return {"ok": True, "action": name, **result}
    if name in {"space.recovery.enable_widget", "space.widget.recovery.enable", "widget.recovery.enable"}:
        space_id = validate_space_id(_space_tool_current_id(data))
        widget_id = validate_widget_id(data.get("widget_id") or data.get("id"))
        result = enable_widget_for_recovery(space_id, widget_id, reason=_payload_text_summary(data.get("reason") or "enabled from recovery", 300))
        return {"ok": True, "action": name, **result}
    if name in {"space.recovery.disable_module", "space.module.recovery.disable", "module.recovery.disable"}:
        module_id = validate_module_id(data.get("module_id") or data.get("id"))
        result = disable_module_for_recovery(module_id, reason=_payload_text_summary(data.get("reason") or "disabled from recovery", 300))
        return {"ok": True, "action": name, **result}
    if name in {"space.recovery.enable_module", "space.module.recovery.enable", "module.recovery.enable"}:
        module_id = validate_module_id(data.get("module_id") or data.get("id"))
        result = enable_module_for_recovery(module_id, reason=_payload_text_summary(data.get("reason") or "enabled from recovery", 300))
        return {"ok": True, "action": name, **result}
    if name == "widget.list":
        space_id = validate_space_id(data.get("space_id"))
        return {"ok": True, "action": name, "widgets": list_widgets(space_id)}
    if name in {"widget.read", "widget.get"}:
        space_id = validate_space_id(data.get("space_id"))
        widget_id = validate_widget_id(data.get("widget_id") or data.get("id"))
        return {"ok": True, "action": name, "widget": read_widget_detail(space_id, widget_id)}
    if name in {"widget.patch", "space.widget.patch", "space.current.widget.patch"}:
        space_id = validate_space_id(_space_tool_current_id(data) if name == "space.current.widget.patch" else data.get("space_id"))
        widget_id = validate_widget_id(data.get("widget_id") or data.get("id"))
        result = patch_widget(space_id, widget_id, data.get("patch") if isinstance(data.get("patch"), dict) else {})
        return {"ok": True, "action": name, **result}
    if name in {
        "widget.reload",
        "widget.refresh",
        "space.widget.reload",
        "space.widget.refresh",
        "space.current.widget.reload",
        "space.current.widget.refresh",
        "space.current.reloadwidget",
        "space.spaces.reloadwidget",
        "space.spaces.refreshwidget",
    }:
        space_id = validate_space_id(_space_tool_current_id(data))
        widget_id = validate_widget_id(_space_tool_widget_id(data))
        payload = {"action": "reload"}
        if isinstance(data.get("payload"), dict):
            for key, value in data["payload"].items():
                safe_key = str(key or "")
                if safe_key != "action":
                    payload[safe_key] = value
        result = queue_widget_event(
            space_id,
            widget_id,
            "widget.refresh",
            payload,
            prompt=data.get("prompt") or "",
            session_id=data.get("session_id") or "",
        )
        return {"ok": True, "action": name, **result}
    if name in {"widget.events", "widget.event.list", "space.widget.events", "space.widget.event.list", "space.current.widget.events", "space.current.widget.event.list"}:
        space_id = validate_space_id(_space_tool_current_id(data) if name.startswith("space.current.") else data.get("space_id"))
        widget_id_raw = _space_tool_widget_id(data) or None
        widget_id = validate_widget_id(widget_id_raw) if widget_id_raw else None
        return {
            "ok": True,
            "action": name,
            "active_space_id": space_id,
            "events": list_widget_events(space_id, widget_id, data.get("limit", 20)),
        }
    if name in {"widget.event", "space.widget.event", "space.current.widget.event"}:
        space_id = validate_space_id(_space_tool_current_id(data) if name == "space.current.widget.event" else data.get("space_id"))
        widget_id = validate_widget_id(_space_tool_widget_id(data))
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
        summary = _event_summary(event, sid, space)
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
    snapshot_revision_events = [str(rev) for rev in (restored.get("revision_events") or []) if _event_id_is_safe(rev)]
    current_revision_events = [str(rev) for rev in (current.get("revision_events") or []) if _event_id_is_safe(rev)]
    merged_revision_events: list[str] = []
    for rev in [*snapshot_revision_events, *current_revision_events]:
        if rev not in merged_revision_events:
            merged_revision_events.append(rev)
    restored["revision_events"] = merged_revision_events
    saved = _write_manifest(restored, "space.restored", {"restored_event_id": safe_event_id})
    return {
        "ok": True,
        "space": read_space_detail(sid),
        "restored_event_id": safe_event_id,
        "revision_event_id": saved["revision_event_id"],
    }


def restore_widget_revision(space_id: str, event_id: str, widget_id: str) -> dict[str, Any]:
    """Restore one widget from a stored revision snapshot, leaving other widgets intact."""
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    sid = validate_space_id(space_id)
    wid = validate_widget_id(widget_id)
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
    target_widget: dict[str, Any] | None = None
    for widget in snapshot.get("widgets") if isinstance(snapshot.get("widgets"), list) else []:
        if isinstance(widget, dict) and widget.get("id") == wid:
            target_widget = _normalize_widget(widget)
            break
    if target_widget is None:
        raise FileNotFoundError("Widget not found in revision snapshot")

    widgets = current.get("widgets") if isinstance(current.get("widgets"), list) else []
    restored_widgets: list[dict[str, Any]] = []
    replaced = False
    for widget in widgets:
        if isinstance(widget, dict) and widget.get("id") == wid:
            restored_widgets.append(target_widget)
            replaced = True
        elif isinstance(widget, dict):
            restored_widgets.append(_normalize_widget(widget))
    if not replaced:
        restored_widgets.append(target_widget)
    current["widgets"] = restored_widgets
    saved = _write_manifest(current, "widget.restored", {"restored_event_id": safe_event_id, "widget_id": wid})
    return {
        "ok": True,
        "space_id": sid,
        "widget": read_widget_detail(sid, wid),
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
    saved = _write_manifest(space, "space.updated", {"fields": sorted(set(updates or {}) & allowed)})
    return read_space_detail(saved["space_id"])


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


def _space_agent_import_warnings(space_yaml: str, widget_files: dict[str, str]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add_from_text(label: str, text: str) -> None:
        for match in _SPACE_AGENT_UNSUPPORTED_API_RE.findall(str(text or "")):
            api_name = match.rstrip(".,;:)]}'\"")
            if not api_name:
                continue
            key = (label, api_name)
            if key in seen:
                continue
            seen.add(key)
            warnings.append(
                {
                    "type": "unsupported_space_agent_api",
                    "file": label,
                    "api": api_name,
                    "message": "Unsupported Space Agent API reference omitted during import.",
                }
            )

    add_from_text("space.yaml", space_yaml)
    for path, text in sorted(widget_files.items()):
        add_from_text(_safe_zip_entry_name(path), text)
    return warnings


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
    warnings = _space_agent_import_warnings(space_yaml, widget_files)
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
        "warnings": warnings,
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
            "disabled_reason": _recovery_reason_summary(recovery.get("disabled_reason"), 300),
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
            summary = _widget_summary(widget)
            metadata: dict[str, Any] = {}
            for field in ("weather", "event_bridge", "prompt"):
                if isinstance(widget.get(field), dict):
                    field_summary = _payload_summary(widget.get(field))
                    if field_summary not in ({}, [], ""):
                        metadata[field] = field_summary
            if metadata:
                summary["metadata"] = metadata
            summaries.append(summary)
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
    widget = read_widget(space_id, widget_id)
    detail = _widget_summary(widget)
    metadata = _widget_detail_metadata(widget)
    if metadata:
        detail["metadata"] = metadata
    recovery = widget.get("recovery") if isinstance(widget.get("recovery"), dict) else {}
    if recovery:
        detail["recovery"] = _payload_summary(recovery)
    if widget.get("revision_event_id"):
        detail["revision_event_id"] = _payload_text_summary(widget.get("revision_event_id"), 120)
    return detail


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
        "refresh",
        "prompt",
        "interaction",
        "audio_policy",
        "status",
        "weather",
        "market_data",
        "watchlist",
        "chart",
        "table",
        "notes",
        "attachments",
        "browser",
        "kanban",
        "markdown",
        "export",
    }
    changed_fields: list[str] = []
    for key, value in (patch or {}).items():
        safe_key = str(key or "")
        if safe_key not in allowed:
            continue
        if safe_key != "market_data" and not _payload_key_is_safe(safe_key):
            continue
        if safe_key == "layout":
            widget["layout"] = _normalize_widget_layout(value)
        elif safe_key in {"metadata", "permissions", "recovery", "event_bridge", "refresh", "prompt", "interaction", "audio_policy", "status", "weather", "market_data", "watchlist", "chart", "table", "notes", "attachments", "browser", "kanban", "markdown", "export"}:
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
        "event_bridge": {"event_name": "widget.refresh", "status": "ready-for-user-confirmation"},
        "prompt": {
            "placeholder": "Ask Capy to refresh or explain the Prague weather widget",
            "suggested_event": "widget.refresh",
        },
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


def disable_space_for_recovery(space_id: str, *, reason: str = "") -> dict[str, Any]:
    """Mark an entire Space disabled from safe recovery without deleting its manifest."""
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    space = read_space(space_id)
    recovery = space.get("recovery") if isinstance(space.get("recovery"), dict) else {}
    recovery = dict(recovery)
    recovery["safe_mode_available"] = True
    recovery["disabled"] = True
    recovery["disabled_reason"] = _context_value(reason or "disabled from recovery", 300)
    space["recovery"] = recovery
    saved = _write_manifest(space, "space.recovery_disabled", {"reason": recovery["disabled_reason"]})
    return {
        "disabled": True,
        "space_id": saved["space_id"],
        "revision_event_id": saved["revision_event_id"],
    }


def enable_space_for_recovery(space_id: str, *, reason: str = "") -> dict[str, Any]:
    """Re-enable an entire Space from safe recovery without exposing widget bodies."""
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    space = read_space(space_id)
    recovery = space.get("recovery") if isinstance(space.get("recovery"), dict) else {}
    recovery = dict(recovery)
    recovery["safe_mode_available"] = True
    recovery["disabled"] = False
    recovery["disabled_reason"] = ""
    space["recovery"] = recovery
    detail_reason = _context_value(reason or "enabled from recovery", 300)
    saved = _write_manifest(space, "space.recovery_enabled", {"reason": detail_reason})
    return {
        "disabled": False,
        "space_id": saved["space_id"],
        "revision_event_id": saved["revision_event_id"],
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


def _module_summary(module: dict[str, Any]) -> dict[str, Any]:
    recovery = module.get("recovery") if isinstance(module.get("recovery"), dict) else {}
    return {
        "module_id": validate_module_id(module.get("module_id") or module.get("id")),
        "name": _public_display_text_summary(module.get("name") or module.get("module_id") or module.get("id"), 160),
        "description": _public_display_text_summary(module.get("description", ""), 300),
        "scope": _public_display_text_summary(module.get("scope") or "global", 80),
        "disabled": bool(recovery.get("disabled")),
        "disabled_reason": _recovery_reason_summary(recovery.get("disabled_reason"), 300),
        "revision_event_id": module.get("revision_event_id"),
    }


def upsert_recovery_module(module: dict[str, Any]) -> dict[str, Any]:
    """Persist a generated module in quarantine and return metadata-only summary.

    Raw module bodies are retained on disk for repair/rollback, but recovery
    snapshots and tool responses expose only sanitized labels/status.
    """
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    if not isinstance(module, dict):
        raise ValueError("module must be an object")
    mid = validate_module_id(module.get("module_id") or module.get("id"))
    existing: dict[str, Any] = {}
    module_path = _recovery_module_path(mid)
    if module_path.exists():
        try:
            loaded = json.loads(module_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}
    now = time.time()
    stored = dict(existing)
    stored.update(copy.deepcopy(module))
    stored["module_id"] = mid
    stored.setdefault("created_at", existing.get("created_at") or now)
    stored["updated_at"] = now
    stored.setdefault("recovery", existing.get("recovery") if isinstance(existing.get("recovery"), dict) else {"disabled": False, "disabled_reason": ""})
    event_id = _record_event("recovery-modules", "module.quarantined", {"module_id": mid}, snapshot=stored)
    stored["revision_event_id"] = event_id
    _atomic_write_json(module_path, stored)
    return _module_summary(stored)


def read_recovery_module(module_id: str) -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    module_path = _recovery_module_path(module_id)
    if not module_path.exists():
        raise FileNotFoundError(module_id)
    loaded = json.loads(module_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise FileNotFoundError(module_id)
    return loaded


def list_recovery_modules() -> list[dict[str, Any]]:
    if not spaces_enabled():
        return []
    _ensure_dirs()
    modules: list[dict[str, Any]] = []
    for module_path in recovery_modules_dir().glob("*.json"):
        try:
            loaded = json.loads(module_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(loaded, dict):
            continue
        try:
            modules.append(_module_summary(loaded))
        except ValueError:
            continue
    modules.sort(key=lambda item: str(item.get("module_id") or ""))
    return modules


def disable_module_for_recovery(module_id: str, *, reason: str = "") -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    mid = validate_module_id(module_id)
    module = read_recovery_module(mid)
    recovery = module.get("recovery") if isinstance(module.get("recovery"), dict) else {}
    recovery = dict(recovery)
    recovery["disabled"] = True
    recovery["disabled_reason"] = _context_value(reason or "disabled from recovery", 300)
    module["recovery"] = recovery
    module["updated_at"] = time.time()
    event_id = _record_event(
        "recovery-modules",
        "module.recovery_disabled",
        {"module_id": mid, "reason": _recovery_reason_summary(recovery["disabled_reason"])},
        snapshot=module,
    )
    module["revision_event_id"] = event_id
    _atomic_write_json(_recovery_module_path(mid), module)
    return {"disabled": True, "module_id": mid, "revision_event_id": event_id}


def enable_module_for_recovery(module_id: str, *, reason: str = "") -> dict[str, Any]:
    if not spaces_enabled():
        raise RuntimeError("Capy Spaces is disabled")
    mid = validate_module_id(module_id)
    module = read_recovery_module(mid)
    recovery = module.get("recovery") if isinstance(module.get("recovery"), dict) else {}
    recovery = dict(recovery)
    recovery["disabled"] = False
    recovery["disabled_reason"] = ""
    module["recovery"] = recovery
    module["updated_at"] = time.time()
    event_id = _record_event(
        "recovery-modules",
        "module.recovery_enabled",
        {"module_id": mid, "reason": _recovery_reason_summary(reason or "enabled from recovery")},
        snapshot=module,
    )
    module["revision_event_id"] = event_id
    _atomic_write_json(_recovery_module_path(mid), module)
    return {"disabled": False, "module_id": mid, "revision_event_id": event_id}


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


def _recovery_safe_admin_contract() -> dict[str, Any]:
    return {
        "metadata_only": True,
        "generated_widgets_rendered": False,
        "recovery_route": "/api/spaces/recovery",
        "restore_routes": ["/api/spaces/revision/restore", "/api/spaces/revision/restore-widget"],
        "gate_labels": [
            "metadata-only recovery",
            "generated widgets not rendered",
            "rollback controls available",
            "disable and repair controls available",
            "module quarantine available",
        ],
    }


def recovery_snapshot() -> dict[str, Any]:
    """Return safe recovery metadata without rendering/returning widget code."""
    empty_summary = {
        "space_count": 0,
        "widget_count": 0,
        "disabled_space_count": 0,
        "disabled_widget_count": 0,
        "rollback_point_count": 0,
        "queued_event_count": 0,
        "module_count": 0,
        "disabled_module_count": 0,
    }
    if not spaces_enabled():
        return {
            "enabled": False,
            "generated_widgets_rendered": False,
            "safe_admin": _recovery_safe_admin_contract(),
            "summary": empty_summary,
            "spaces": [],
            "modules": [],
        }
    _ensure_dirs()
    spaces: list[dict[str, Any]] = []
    counts = dict(empty_summary)
    modules = list_recovery_modules()
    counts["module_count"] = len(modules)
    counts["disabled_module_count"] = sum(1 for module in modules if module.get("disabled"))
    for manifest in manifests_dir().glob("*/space.json"):
        try:
            space = json.loads(manifest.read_text(encoding="utf-8"))
            summary = _summary(space)
            widgets = space.get("widgets") if isinstance(space.get("widgets"), list) else []
            widget_summaries = [_widget_recovery_summary(widget) for widget in widgets if isinstance(widget, dict)]
            queued_events_by_widget: dict[str, list[dict[str, Any]]] = {}
            for event in list_widget_events(summary["space_id"], limit=100):
                wid = _context_value(event.get("widget_id"), 120)
                if not wid:
                    continue
                queued_events_by_widget.setdefault(wid, []).append(event)
            for widget_summary in widget_summaries:
                if widget_summary.get("disabled"):
                    counts["disabled_widget_count"] += 1
                wid = _context_value(widget_summary.get("id"), 120)
                widget_events = queued_events_by_widget.get(wid) or []
                if not widget_events:
                    continue
                latest = widget_events[0]
                counts["queued_event_count"] += len(widget_events)
                widget_summary["queued_event_count"] = len(widget_events)
                widget_summary["latest_queued_event"] = {
                    "event_id": _context_value(latest.get("event_id"), 120),
                    "event_name": _context_value(latest.get("event_name"), 120),
                    "status": _context_value(latest.get("status") or "queued", 80),
                }
            revisions = list_revision_events(summary["space_id"], 5)
            summary["widgets"] = widget_summaries
            summary["revisions"] = revisions
            counts["space_count"] += 1
            counts["widget_count"] += len(widget_summaries)
            if summary.get("disabled"):
                counts["disabled_space_count"] += 1
            counts["rollback_point_count"] += len(revisions)
            spaces.append(summary)
        except Exception:
            continue
    spaces.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return {
        "enabled": True,
        "schema_version": SCHEMA_VERSION,
        "generated_widgets_rendered": False,
        "safe_admin": _recovery_safe_admin_contract(),
        "summary": counts,
        "spaces": spaces,
        "modules": modules,
    }
