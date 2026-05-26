"""Desktop pet routes and support helpers.

This module intentionally owns the optional desktop pet surface so the main
WebUI router only needs thin dispatch hooks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from api.agent_sessions import MESSAGING_SOURCES, is_cli_session_row, is_cli_session_row_visible
from api.config import STATE_DIR, STREAMS, STREAMS_LOCK
from api.config import load_settings, save_settings
from api.helpers import _redact_text, _sanitize_error, bad, j, t
from api.models import Session, all_sessions, get_session
from api.profiles import _profiles_match, get_active_profile_name

logger = logging.getLogger(__name__)

DEFAULT_PET_SKIN_ID = "keeper"
DESKTOP_PET_ENABLED_SETTING_KEY = "desktop_pet_enabled"

DEFAULT_PET_SKIN_LAYOUT = {
    "columns": 8,
    "rows": 9,
    "frameWidth": 192,
    "frameHeight": 208,
    "states": [
        {"name": "idle", "row": 0, "frames": 6},
        {"name": "running-right", "row": 1, "frames": 8},
        {"name": "running-left", "row": 2, "frames": 8},
        {"name": "waving", "row": 3, "frames": 4},
        {"name": "jumping", "row": 4, "frames": 5},
        {"name": "failed", "row": 5, "frames": 8},
        {"name": "waiting", "row": 6, "frames": 6},
        {"name": "running", "row": 7, "frames": 6},
        {"name": "review", "row": 8, "frames": 6},
    ],
}
_PET_ACTION_TEXT_MAX_CHARS = 140
_PET_NAVIGATION_COMMANDS: list[dict] = []
_PET_NAVIGATION_TTL_SECONDS = 60
_PET_NAVIGATION_MAX_COMMANDS = 20
_PET_NAVIGATION_LOCK = threading.Lock()
# Wall-clock time of the most recent WebUI bridge poll of /api/pet/navigation.
# Used to tell whether a live WebUI tab exists to consume + ack a command.
_PET_NAVIGATION_LAST_POLL_AT = 0.0
# A backgrounded bridge polls ~1/s (throttled), so a few seconds of slack avoids
# false negatives for a live-but-backgrounded tab while still detecting a closed
# tab quickly.
_PET_BRIDGE_POLL_FRESH_SECONDS = 4.0
# osascript-driven browser control (tab reuse / window focus) needs the macOS
# Automation permission.  When the Hermes process lacks it, every probe fails
# after ~150-300ms; iterating all known browsers burns ~1s of dead time on each
# click.  Cache the verdict so repeat clicks skip the dead probes and fall
# straight through to the permission-free open(1) activation path
# (_foreground_pet_browser_app).  Re-checked periodically in case the user
# grants permission later.
_PET_AUTOMATION_LOCK = threading.Lock()
_PET_AUTOMATION_AVAILABLE: bool | None = None
_PET_AUTOMATION_CHECKED_AT = 0.0
_PET_AUTOMATION_RECHECK_SECONDS = 60.0
_PET_WEBUI_BROWSER_HINT_LOCK = threading.Lock()
_PET_WEBUI_BROWSER_HINT: dict[str, float | str] = {"app": "", "seen_at": 0.0}
_PET_LAUNCH_LOCK = threading.Lock()
_PET_INSTALL_LOCK = threading.Lock()
_PET_COMPLETION_FALLBACK_SECONDS = 10 * 60
_PET_ATTENTION_STREAMING_STATE_LOCK = threading.Lock()
_PET_ATTENTION_STREAMING_STATE: dict[str, dict[str, float | bool]] = {}
_PET_ATTENTION_COMPLETED_STATE: dict[str, dict[str, float]] = {}


def _desktop_pet_preference_payload() -> dict:
    settings = load_settings()
    configured = DESKTOP_PET_ENABLED_SETTING_KEY in settings
    return {
        "ok": True,
        "enabled": bool(settings.get(DESKTOP_PET_ENABLED_SETTING_KEY)),
        "configured": bool(configured),
    }


def _set_desktop_pet_preference_enabled(enabled: bool) -> dict:
    saved = save_settings({DESKTOP_PET_ENABLED_SETTING_KEY: bool(enabled)})
    return {
        "ok": True,
        "enabled": bool(saved.get(DESKTOP_PET_ENABLED_SETTING_KEY)),
        "configured": True,
    }


def _handle_pet_preference(handler, body: dict | None = None) -> bool:
    if body is None:
        return j(handler, _desktop_pet_preference_payload())
    if not isinstance(body, dict) or "enabled" not in body:
        return bad(handler, "enabled is required")
    return j(handler, _set_desktop_pet_preference_enabled(bool(body.get("enabled"))))


def _all_profiles_query_flag(parsed_url) -> bool:
    raw = parse_qs(parsed_url.query).get("all_profiles", [""])[0].strip().lower()
    return raw in ("1", "true", "yes", "on")


def _pet_static_path(*parts: str) -> Path:
    return (Path(__file__).parent.parent / "static" / Path(*parts)).resolve()


def _repo_root() -> Path:
    return Path(__file__).parent.parent.resolve()


def _pet_client_is_loopback(handler) -> bool:
    try:
        address = getattr(handler, "client_address", None)
        if not address:
            return False
        return ip_address(str(address[0])).is_loopback
    except Exception:
        return False


def _pet_message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    if message.get("hidden") or message.get("is_hidden"):
        return ""
    value = message.get("content")
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                part_type = str(item.get("type") or "text")
                if part_type not in ("text", "output_text"):
                    continue
                if isinstance(item.get("text"), str):
                    parts.append(item.get("text"))
        text = "\n".join(parts)
    else:
        text = ""
    return " ".join(text.split()).strip()


def _pet_bubble_text(text: str) -> str:
    lines = []
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line or line in {"---", "***", "___"}:
            continue
        line = line.lstrip("#").strip()
        line = line.lstrip(">•-*0123456789.、)） ").strip()
        if line:
            lines.append(line)
    cleaned = " ".join(" ".join(lines).split()).strip()
    while cleaned and not cleaned[0].isalnum():
        cleaned = cleaned[1:].strip()
    return cleaned


def _pet_truncate_text(text: str, max_chars: int = _PET_ACTION_TEXT_MAX_CHARS) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    clipped = cleaned[: max(0, max_chars - 3)].rstrip()
    boundary = max(clipped.rfind(" "), clipped.rfind("，"), clipped.rfind("。"), clipped.rfind("、"), clipped.rfind(","))
    if boundary >= max_chars // 2:
        clipped = clipped[:boundary].rstrip()
    return f"{clipped}..."


def _pet_latest_assistant_final_text(session_id: str) -> str:
    try:
        session = Session.load(session_id)
    except Exception:
        session = None
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "assistant":
            text = _pet_message_text(message)
            if text:
                return _redact_text(_pet_bubble_text(text))
    return ""


def _pet_session_is_running(session: dict) -> bool:
    return bool(
        session.get("is_streaming")
        or session.get("active_stream_id")
        or session.get("pending_user_message")
        or session.get("has_pending_user_message")
    )


def _pet_source_value(value: object) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if text.endswith(" session"):
        text = text[: -len(" session")].strip()
    return text


def _pet_session_is_external(session: dict) -> bool:
    if not isinstance(session, dict):
        return False
    session_source = _pet_source_value(session.get("session_source"))
    if session_source in {"messaging", "external-agent", "external agent", "cli"}:
        return True
    source_values = {
        _pet_source_value(session.get("source")),
        _pet_source_value(session.get("source_tag")),
        _pet_source_value(session.get("raw_source")),
        _pet_source_value(session.get("source_label")),
        _pet_source_value(session.get("platform")),
    }
    messaging_sources = {source.replace("_", "-") for source in MESSAGING_SOURCES}
    if source_values & messaging_sources:
        return True
    if is_cli_session_row(session):
        return True
    return bool(session.get("is_cli_session") or session.get("read_only"))


def _pet_pending_approval(session_id: str) -> tuple[dict | None, int]:
    if not session_id:
        return None, 0
    try:
        from tools.approval import _lock as approval_lock
        from tools.approval import _pending as approval_pending
    except Exception:
        return None, 0
    try:
        with approval_lock:
            queue = approval_pending.get(session_id)
            if isinstance(queue, list):
                pending = dict(queue[0]) if queue else None
                return pending, len(queue)
            if queue:
                return dict(queue), 1
    except Exception:
        logger.debug("failed to inspect pet approval state for %s", session_id, exc_info=True)
    return None, 0


def _pet_pending_clarify(session_id: str) -> tuple[dict | None, int]:
    if not session_id:
        return None, 0
    try:
        from api import clarify

        pending = clarify.get_pending(session_id)
        if pending:
            return dict(pending), 1
    except Exception:
        logger.debug("failed to inspect pet clarify state for %s", session_id, exc_info=True)
    return None, 0




def _pet_stable_action_key(action_type: str, session_id: str, payload: dict) -> str:
    if not isinstance(payload, dict):
        payload = {}
    for field in ("approval_id", "clarify_id", "id", "request_id", "run_id"):
        value = str(payload.get(field) or "").strip()
        if value:
            return f"{action_type}:{value}"
    if action_type == "clarify":
        stable_payload = {
            "question": payload.get("question") or payload.get("description") or "",
            "choices": payload.get("choices_offered") or payload.get("choices") or payload.get("options") or [],
            "requested_at": payload.get("requested_at") or payload.get("created_at") or "",
            "expires_at": payload.get("expires_at") or payload.get("deadline") or "",
        }
    else:
        stable_payload = {
            "description": payload.get("description") or "",
            "command": payload.get("command") or "",
            "created_at": payload.get("created_at") or payload.get("requested_at") or "",
        }
    raw = json.dumps(stable_payload, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{action_type}:{session_id}:{digest}"

def _pet_action_required(session_id: str) -> dict | None:
    approval, approval_count = _pet_pending_approval(session_id)
    if approval:
        text = str(approval.get("description") or approval.get("command") or "").strip()
        if approval.get("command") and approval.get("description"):
            text = f"{approval.get('description')}: {approval.get('command')}"
        text = _pet_truncate_text(_redact_text(_pet_bubble_text(text)))
        return {
            "type": "approval",
            "key": _pet_stable_action_key("approval", session_id, approval),
            "count": approval_count,
            "text": text,
            "command": _pet_truncate_text(str(approval.get("command") or "").strip()),
            "description": _pet_truncate_text(str(approval.get("description") or "").strip()),
            "approval_id": str(approval.get("approval_id") or approval.get("request_id") or approval.get("id") or ""),
        }
    clarify, clarify_count = _pet_pending_clarify(session_id)
    if clarify:
        text = str(clarify.get("question") or clarify.get("description") or "").strip()
        text = _pet_truncate_text(_redact_text(_pet_bubble_text(text)))
        raw_choices = clarify.get("choices_offered") or clarify.get("choices") or clarify.get("options") or []
        choices = [_pet_truncate_text(str(choice).strip()) for choice in raw_choices if str(choice).strip()][:6]
        return {
            "type": "clarify",
            "key": _pet_stable_action_key("clarify", session_id, clarify),
            "count": clarify_count,
            "text": text,
            "choices": choices,
            "clarify_id": str(clarify.get("clarify_id") or ""),
        }
    return None


def _pet_latest_visible_assistant_process_text(session: dict) -> str:
    sid = str(session.get("session_id") or "")
    run_id = str(session.get("active_stream_id") or "")
    if not sid or not run_id:
        return ""
    try:
        from api.run_journal import read_run_events

        journal = read_run_events(sid, run_id)
    except Exception:
        return ""
    segments: list[str] = []
    current: list[str] = []

    def flush_current() -> None:
        text = "".join(current).strip()
        if text:
            segments.append(text)
        current.clear()

    for event in journal.get("events") or []:
        if not isinstance(event, dict):
            continue
        name = str(event.get("event") or event.get("type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if name == "token":
            text = str(payload.get("text") or "")
            if text:
                current.append(text)
            continue
        if name == "interim_assistant":
            if payload.get("already_streamed"):
                flush_current()
                continue
            flush_current()
            text = str(payload.get("text") or "").strip()
            if text:
                segments.append(text)
            continue
        if name == "tool":
            flush_current()
            continue
    flush_current()
    text = _pet_bubble_text(segments[-1]) if segments else ""
    return _redact_text(text) if text else ""


def _display_rows_without_stale_pet_streams(session_rows) -> list[dict]:
    """Return pet-only rows with dead stream ids hidden without mutating sessions."""
    display_rows: list[dict] = []
    for row in session_rows:
        if not isinstance(row, dict):
            continue
        display_row = dict(row)
        sid = row.get("session_id")
        stream_id = row.get("active_stream_id")
        if not sid or not stream_id or row.get("is_streaming") is True:
            display_rows.append(display_row)
            continue
        with STREAMS_LOCK:
            stream_alive = stream_id in STREAMS
        if stream_alive:
            display_rows.append(display_row)
            continue
        display_row["active_stream_id"] = None
        display_row["is_streaming"] = False
        display_row["pending_user_message"] = None
        display_row["has_pending_user_message"] = False
        display_rows.append(display_row)
    return display_rows



def _pet_json_query_param(parsed, name: str) -> dict:
    raw = parse_qs(parsed.query).get(name, [""])[0]
    if not raw or len(raw) > 20000:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _pet_unread_state_from_query(parsed) -> dict:
    return {
        "viewed_counts": _pet_json_query_param(parsed, "viewed_counts"),
        "completion_unread": _pet_json_query_param(parsed, "completion_unread"),
    }


def _pet_attention_update_completion_state(rows: list[dict]) -> None:
    now = time.time()
    seen: set[str] = set()
    with _PET_ATTENTION_STREAMING_STATE_LOCK:
        for row in rows:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("session_id") or "").strip()
            if not sid:
                continue
            running = _pet_session_is_running(row)
            seen.add(sid)
            previous = _PET_ATTENTION_STREAMING_STATE.get(sid)
            try:
                message_count = float(row.get("message_count") or 0.0)
            except (TypeError, ValueError):
                message_count = 0.0
            if previous and previous.get("running") and not running:
                _PET_ATTENTION_COMPLETED_STATE[sid] = {
                    "ready_at": now,
                    "message_count": message_count,
                }
            _PET_ATTENTION_STREAMING_STATE[sid] = {
                "running": bool(running),
                "last_seen": now,
            }
            if running and sid in _PET_ATTENTION_COMPLETED_STATE:
                _PET_ATTENTION_COMPLETED_STATE.pop(sid, None)
        cutoff = now - _PET_COMPLETION_FALLBACK_SECONDS
        for sid, state in list(_PET_ATTENTION_COMPLETED_STATE.items()):
            if float(state.get("ready_at", 0.0)) < cutoff:
                _PET_ATTENTION_COMPLETED_STATE.pop(sid, None)
        for sid, state in list(_PET_ATTENTION_STREAMING_STATE.items()):
            if sid in seen:
                continue
            if now - float(state.get("last_seen", 0.0)) > _PET_COMPLETION_FALLBACK_SECONDS * 3:
                _PET_ATTENTION_STREAMING_STATE.pop(sid, None)


def _pet_session_has_recent_ready_completion(
    sid: str,
    now: float | None = None,
    *,
    message_count: float = 0.0,
    viewed_count: float | None = None,
) -> bool:
    if not sid:
        return False
    when = float(now if now is not None else time.time())
    cutoff = when - _PET_COMPLETION_FALLBACK_SECONDS
    with _PET_ATTENTION_STREAMING_STATE_LOCK:
        ready_at = _PET_ATTENTION_COMPLETED_STATE.get(sid)
        if ready_at is None:
            return False
        ready_time = ready_at.get("ready_at", 0.0)
        if float(ready_time) < cutoff:
            _PET_ATTENTION_COMPLETED_STATE.pop(sid, None)
            return False
        completion_count = float(ready_at.get("message_count", 0.0))
        if viewed_count is not None and viewed_count >= completion_count:
            _PET_ATTENTION_COMPLETED_STATE.pop(sid, None)
            return False
        return message_count <= completion_count


def _pet_session_ready_from_webui_unread(session: dict, unread_state: dict) -> bool:
    sid = str(session.get("session_id") or "")
    if not sid:
        return False
    completion_unread = unread_state.get("completion_unread") if isinstance(unread_state, dict) else {}
    if isinstance(completion_unread, dict) and sid in completion_unread:
        return True
    try:
        message_count = float(session.get("message_count") or 0)
    except (TypeError, ValueError):
        return False
    viewed_counts = unread_state.get("viewed_counts") if isinstance(unread_state, dict) else {}
    if not isinstance(viewed_counts, dict) or sid not in viewed_counts:
        return _pet_session_has_recent_ready_completion(sid, message_count=message_count, viewed_count=None)
    try:
        viewed_count = float(viewed_counts.get(sid) or 0)
    except (TypeError, ValueError):
        viewed_count = -1.0
    if message_count > viewed_count:
        return True
    return _pet_session_has_recent_ready_completion(sid, message_count=message_count, viewed_count=viewed_count)


def _pet_attention_session(session: dict, unread_state: dict | None = None) -> dict:
    sid = str(session.get("session_id") or "")
    title = _redact_text(str(session.get("display_title") or session.get("title") or "Session"))
    running = _pet_session_is_running(session)
    action_required = _pet_action_required(sid) if sid else None
    ready = False if action_required or running else _pet_session_ready_from_webui_unread(session, unread_state or {})
    status = "action_required" if action_required else ("running" if running else ("ready" if ready else "idle"))
    process_text = action_required.get("text") if action_required else (
        _pet_latest_visible_assistant_process_text(session)
        if running
        else (_pet_latest_assistant_final_text(sid) if ready and sid else "")
    )
    return {
        "session_id": sid,
        "status": status,
        "title": title,
        "message_count": int(session.get("message_count") or 0),
        "last_message_at": session.get("last_message_at") or session.get("updated_at") or 0,
        "updated_at": session.get("updated_at") or 0,
        "running": running,
        "started_at": float(session.get("started_at") or 0),
        "action_required": bool(action_required),
        "action_required_type": action_required.get("type") if action_required else "",
        "action_required_key": action_required.get("key") if action_required else "",
        "action_required_count": int(action_required.get("count") or 0) if action_required else 0,
        "action_required_command": action_required.get("command", "") if action_required else "",
        "action_required_description": action_required.get("description", "") if action_required else "",
        "action_required_approval_id": action_required.get("approval_id", "") if action_required else "",
        "action_required_choices": action_required.get("choices", []) if action_required else [],
        "action_required_clarify_id": action_required.get("clarify_id", "") if action_required else "",
        "process_text": process_text,
        "is_cli_session": bool(session.get("is_cli_session")),
        "source_label": session.get("source_label") or session.get("source_tag") or "",
    }


def _handle_pet_page(handler, template: str = "index.html") -> bool:
    try:
        from api.auth import csrf_token_for_session, is_auth_enabled, parse_cookie, verify_session
        from api.updates import WEBUI_VERSION

        if template not in {"index.html", "bubbles.html"}:
            return bad(handler, "unknown desktop pet page", status=404)
        version_token = quote(WEBUI_VERSION, safe="")
        pet_asset_version = quote(_desktop_pet_asset_version(WEBUI_VERSION), safe="")
        csrf_token = ""
        try:
            if is_auth_enabled():
                cookie_val = parse_cookie(handler)
                if cookie_val and verify_session(cookie_val):
                    csrf_token = csrf_token_for_session(cookie_val) or ""
        except Exception:
            csrf_token = ""
        html = (
            _pet_static_path("desktop_pet", template)
            .read_text(encoding="utf-8")
            .replace("__WEBUI_VERSION__", version_token)
            .replace("__DESKTOP_PET_ASSET_VERSION__", pet_asset_version)
            .replace("__CSRF_TOKEN_JSON__", json.dumps(csrf_token))
        )
        return t(handler, html, content_type="text/html; charset=utf-8")
    except Exception as exc:
        logger.exception("failed to serve desktop pet")
        return j(handler, {"error": _sanitize_error(exc)}, status=500)


def _desktop_pet_asset_version(base_version: str) -> str:
    digest = hashlib.sha256(str(base_version or "").encode("utf-8"))
    for rel_path in ("pet.css", "pet.js", "bubbles.js"):
        target = _pet_static_path("desktop_pet", rel_path)
        try:
            stat = target.stat()
        except OSError:
            digest.update(rel_path.encode("utf-8"))
            digest.update(b":missing")
            continue
        digest.update(rel_path.encode("utf-8"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(str(stat.st_size).encode("ascii"))
    return f"{base_version}-{digest.hexdigest()[:12]}"


def _handle_pet_attention(handler, parsed) -> bool:
    try:
        limit = int(parse_qs(parsed.query).get("limit", ["30"])[0])
    except (TypeError, ValueError):
        limit = 30
    limit = min(50, max(1, limit))
    rows = _display_rows_without_stale_pet_streams(all_sessions())
    rows_by_sid = {str(s.get("session_id") or ""): s for s in rows if s.get("session_id")}
    try:
        from api import config as _live_config

        with _live_config.ACTIVE_RUNS_LOCK:
            active_runs = [dict(raw or {}) for raw in (_live_config.ACTIVE_RUNS or {}).values()]
        for run in active_runs:
            sid = str(run.get("session_id") or "").strip()
            stream_id = str(run.get("stream_id") or "").strip()
            if not sid:
                continue
            try:
                session = get_session(sid, metadata_only=True)
                row = session.compact(include_runtime=True, active_stream_ids={stream_id} if stream_id else set())
            except Exception:
                row = {"session_id": sid}
            if stream_id:
                row["active_stream_id"] = stream_id
                row["is_streaming"] = True
            if run.get("started_at"):
                row["started_at"] = float(run.get("started_at"))
                row["updated_at"] = max(float(row.get("updated_at") or 0), float(run.get("started_at") or 0))
                row["last_message_at"] = max(float(row.get("last_message_at") or 0), float(run.get("started_at") or 0))
            rows_by_sid[sid] = {**rows_by_sid.get(sid, {}), **row}
        rows = list(rows_by_sid.values())
    except Exception:
        logger.debug("failed to merge active runs into pet attention rows", exc_info=True)

    rows, active_profile = _filter_pet_attention_rows(rows, parsed)
    unread_state = _pet_unread_state_from_query(parsed)
    _pet_attention_update_completion_state(rows)
    items = [_pet_attention_session(s, unread_state) for s in rows]
    items = [item for item in items if item.get("status") != "idle"]
    items.sort(
        key=lambda item: (
            3 if item.get("status") == "action_required" else (2 if item.get("status") == "ready" else 1),
            item.get("last_message_at") or item.get("updated_at") or 0,
        ),
        reverse=True,
    )
    items = items[:limit]
    return j(handler, {"sessions": items, "active_profile": active_profile, "server_time": time.time()})


def _filter_pet_attention_rows(rows: list[dict], parsed) -> tuple[list[dict], str]:
    active_profile = get_active_profile_name()
    if not _all_profiles_query_flag(parsed):
        rows = [s for s in rows if _profiles_match(s.get("profile"), active_profile)]
    rows = [s for s in rows if not s.get("archived")]
    settings = load_settings()
    show_external_sessions = bool(settings.get("show_cli_sessions"))
    if show_external_sessions:
        rows = [
            s
            for s in rows
            if not _pet_session_is_external(s) or is_cli_session_row_visible(s)
        ]
    else:
        rows = [s for s in rows if not _pet_session_is_external(s)]
    return rows, active_profile



def _default_pet_skin_layout() -> dict:
    return json.loads(json.dumps(DEFAULT_PET_SKIN_LAYOUT))


def _normalize_pet_skin_layout(raw_layout: object) -> dict | None:
    """Return a safe normalized spritesheet layout or None when invalid."""
    if raw_layout in (None, ""):
        return _default_pet_skin_layout()
    if not isinstance(raw_layout, dict):
        return None
    try:
        columns = int(raw_layout.get("columns"))
        rows = int(raw_layout.get("rows"))
        frame_width = int(raw_layout.get("frameWidth"))
        frame_height = int(raw_layout.get("frameHeight"))
    except (TypeError, ValueError):
        return None
    if not (1 <= columns <= 64 and 1 <= rows <= 64 and 1 <= frame_width <= 4096 and 1 <= frame_height <= 4096):
        return None
    raw_states = raw_layout.get("states")
    if not isinstance(raw_states, list) or not raw_states:
        return None
    states: list[dict] = []
    seen: set[str] = set()
    for raw_state in raw_states:
        if not isinstance(raw_state, dict):
            return None
        name = str(raw_state.get("name") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name in seen:
            return None
        try:
            row = int(raw_state.get("row"))
            frames = int(raw_state.get("frames"))
        except (TypeError, ValueError):
            return None
        if not (0 <= row < rows and 1 <= frames <= columns):
            return None
        seen.add(name)
        states.append({"name": name, "row": row, "frames": frames})
    required = {str(item.get("name")) for item in DEFAULT_PET_SKIN_LAYOUT["states"]}
    if not required.issubset(seen):
        return None
    return {
        "columns": columns,
        "rows": rows,
        "frameWidth": frame_width,
        "frameHeight": frame_height,
        "states": states,
    }

def _pet_skin_url(skin_id: str, sprite_rel: str) -> str:
    parts = [quote(part, safe="") for part in Path(sprite_rel).parts]
    return f"/static/pets/{quote(skin_id, safe='')}/{'/'.join(parts)}"


def _available_pet_skins() -> list[dict]:
    pets_root = _pet_static_path("pets")
    if not pets_root.exists():
        return []
    skins = []
    for skin_dir in sorted(p for p in pets_root.iterdir() if p.is_dir()):
        manifest_path = skin_dir / "pet.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("failed to read pet skin manifest %s", manifest_path, exc_info=True)
            continue
        skin_id = str(manifest.get("id") or skin_dir.name).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", skin_id) or skin_id != skin_dir.name:
            continue
        sprite_rel = str(manifest.get("spritesheetPath") or "spritesheet.webp").strip()
        sprite_parts = Path(sprite_rel).parts
        if not sprite_rel or Path(sprite_rel).is_absolute() or ".." in sprite_parts:
            continue
        sprite_path = (skin_dir / sprite_rel).resolve()
        try:
            sprite_path.relative_to(skin_dir.resolve())
        except ValueError:
            continue
        if not sprite_path.is_file():
            continue
        layout = _normalize_pet_skin_layout(manifest.get("layout"))
        if not layout:
            continue
        skins.append(
            {
                "id": skin_id,
                "displayName": str(manifest.get("displayName") or skin_id).strip() or skin_id,
                "description": str(manifest.get("description") or ""),
                "spritesheetPath": sprite_rel,
                "spritesheetUrl": _pet_skin_url(skin_id, sprite_rel),
                "layout": layout,
            }
        )
    skins.sort(key=lambda item: (item["id"] != DEFAULT_PET_SKIN_ID, item["displayName"].lower()))
    return skins


def _handle_pet_skins(handler, parsed) -> bool:
    return j(handler, {"skins": _available_pet_skins(), "default": DEFAULT_PET_SKIN_ID, "server_time": time.time()})


def _desktop_pet_processes() -> list[dict]:
    patterns = ("hermes-desktop-pet", "Hermes Desktop Pet")
    processes: list[dict] = []
    try:
        if os.name == "nt":
            known_paths = {str(Path(path).resolve()).lower() for path in _desktop_pet_known_process_paths()}
            ps = shutil.which("powershell") or shutil.which("powershell.exe")
            if not ps:
                return processes
            script = """
$items = Get-CimInstance Win32_Process -Filter "Name = 'hermes-desktop-pet.exe'" |
  Select-Object ProcessId,CommandLine,ExecutablePath
$items | ConvertTo-Json -Compress
"""
            result = subprocess.run(
                [ps, "-NoProfile", "-Command", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return processes
            try:
                payload = json.loads(result.stdout)
            except Exception:
                return processes
            rows = payload if isinstance(payload, list) else [payload]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    pid = int(row.get("ProcessId") or 0)
                except (TypeError, ValueError):
                    pid = 0
                exe = str(row.get("ExecutablePath") or "").strip()
                exe_path = str(Path(exe).resolve()).lower() if exe else ""
                if pid and exe_path in known_paths:
                    processes.append({"pid": pid, "command": str(row.get("CommandLine") or exe), "path": exe})
            return processes
        result = subprocess.run(
            ["ps", "eww", "-axo", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return processes
        current_pid = str(os.getpid())
        known_paths = _desktop_pet_known_process_paths()
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if not parts or parts[0] == current_pid:
                continue
            command = parts[1] if len(parts) > 1 else ""
            try:
                first_arg = shlex.split(command)[0] if command else ""
            except ValueError:
                first_arg = command.split(None, 1)[0] if command else ""
            command_name = Path(first_arg).name
            first_path = str(Path(first_arg).expanduser().resolve()) if first_arg.startswith("/") else first_arg
            known_app_exec = any(path and path in command for path in known_paths if "/Hermes Desktop Pet.app/" in path)
            if command_name in patterns and first_path in known_paths:
                processes.append({"pid": int(parts[0]), "command": command})
            elif known_app_exec:
                processes.append({"pid": int(parts[0]), "command": command})
    except Exception:
        logger.debug("failed to inspect desktop pet process", exc_info=True)
    return processes


def _desktop_pet_known_process_paths() -> set[str]:
    root = _repo_root()
    desktop_pet_dir = root / "desktop-pet"
    exe_name = "hermes-desktop-pet.exe" if os.name == "nt" else "hermes-desktop-pet"
    paths: set[str] = set()
    if sys.platform == "darwin":
        for app_path in (
            Path("/Applications/Hermes Desktop Pet.app"),
            Path.home() / "Applications" / "Hermes Desktop Pet.app",
            desktop_pet_dir / "src-tauri" / "target" / "release" / "bundle" / "macos" / "Hermes Desktop Pet.app",
            desktop_pet_dir / "src-tauri" / "target" / "debug" / "bundle" / "macos" / "Hermes Desktop Pet.app",
        ):
            paths.add(str((app_path / "Contents" / "MacOS" / "hermes-desktop-pet").resolve()))
    for profile in ("release", "debug"):
        paths.add(str((desktop_pet_dir / "src-tauri" / "target" / profile / exe_name).resolve()))
    return paths


def _desktop_pet_registry_path() -> Path:
    return STATE_DIR / "desktop-pet.json"


def _read_desktop_pet_registry() -> dict:
    try:
        payload = json.loads(_desktop_pet_registry_path().read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _write_desktop_pet_registry(payload: dict) -> None:
    path = _desktop_pet_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _normalize_desktop_pet_base_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _desktop_pet_process_base_url(process: dict) -> str:
    direct_base_url = _normalize_desktop_pet_base_url(str(process.get("base_url") or ""))
    if direct_base_url:
        return direct_base_url
    command = str(process.get("command") or "")
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    for part in parts[1:]:
        if part.startswith("HERMES_DESKTOP_PET_WEBUI_BASE="):
            return _normalize_desktop_pet_base_url(part.split("=", 1)[1])
    try:
        pid = int(process.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    registry = _read_desktop_pet_registry()
    if pid and int(registry.get("pid") or 0) == pid and _pid_is_running(pid):
        return _normalize_desktop_pet_base_url(str(registry.get("base_url") or ""))
    return ""


def _desktop_pet_process_matches_base(process: dict, webui_base_url: str) -> bool:
    expected = _normalize_desktop_pet_base_url(webui_base_url)
    if not expected:
        return True
    actual = _desktop_pet_process_base_url(process)
    if actual:
        return actual == expected
    return expected == "http://127.0.0.1:8787"


def _desktop_pet_process_has_known_base(process: dict) -> bool:
    return bool(_desktop_pet_process_base_url(process))


def _desktop_pet_process_running(webui_base_url: str = "") -> bool:
    return any(_desktop_pet_process_matches_base(process, webui_base_url) for process in _desktop_pet_processes())


def _terminate_desktop_pet_processes(processes: list[dict]) -> dict:
    if os.name == "nt":
        closed = 0
        errors: list[str] = []
        for process in processes:
            pid = int(process.get("pid") or 0)
            if not pid:
                continue
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                closed += 1
            else:
                errors.append(_sanitize_error(result.stderr or result.stdout))
        return {"ok": not errors, "closed": closed, "error": "; ".join(error for error in errors if error)}
    closed = 0
    for process in processes:
        pid = int(process.get("pid") or 0)
        if not pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            closed += 1
        except ProcessLookupError:
            closed += 1
        except Exception:
            logger.debug("failed to terminate desktop pet pid %s", pid, exc_info=True)
    return {"ok": True, "closed": closed, "error": ""}


def _close_desktop_pet_processes() -> dict:
    with _PET_LAUNCH_LOCK:
        processes = _desktop_pet_processes()
        if not processes:
            return {"ok": True, "closed": 0, "running": False}
        result = _terminate_desktop_pet_processes(processes)
        deadline = time.time() + 2
        while time.time() < deadline:
            if not _desktop_pet_process_running():
                return {"ok": result.get("ok", True), "closed": result.get("closed", 0), "running": False, "error": result.get("error", "")}
            time.sleep(0.1)
        for process in _desktop_pet_processes():
            pid = int(process.get("pid") or 0)
            if not pid:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                logger.debug("failed to kill desktop pet pid %s", pid, exc_info=True)
        return {"ok": result.get("ok", True), "closed": result.get("closed", 0), "running": _desktop_pet_process_running(), "error": result.get("error", "")}


def _desktop_pet_shell_source_mtime() -> float:
    root = _repo_root()
    src_dir = root / "desktop-pet" / "src-tauri"
    paths = [
        root / "desktop-pet" / "package.json",
        src_dir / "Cargo.toml",
        src_dir / "build.rs",
        src_dir / "tauri.conf.json",
        src_dir / "src" / "main.rs",
    ]
    capabilities = src_dir / "capabilities"
    if capabilities.is_dir():
        paths.extend(capabilities.glob("*.json"))
    mtimes = []
    for path in paths:
        try:
            if path.is_file():
                mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else 0.0


def _desktop_pet_app_executable(app_path: Path) -> Path:
    exe_name = "hermes-desktop-pet.exe" if os.name == "nt" else "hermes-desktop-pet"
    if sys.platform == "darwin":
        return app_path / "Contents" / "MacOS" / exe_name
    return app_path


def _desktop_pet_artifact_mtime(candidate: dict) -> float:
    artifact = candidate.get("artifact")
    if not artifact:
        return 0.0
    try:
        path = Path(str(artifact))
        return path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        return 0.0


def _desktop_pet_candidate_is_current(candidate: dict, source_mtime: float | None = None) -> bool:
    if candidate.get("kind") == "tauri-dev":
        return True
    artifact_mtime = _desktop_pet_artifact_mtime(candidate)
    if artifact_mtime <= 0:
        return False
    return artifact_mtime >= (source_mtime if source_mtime is not None else _desktop_pet_shell_source_mtime())


def _desktop_pet_launch_candidates(*, include_stale: bool = False, include_dev: bool = False) -> list[dict]:
    root = _repo_root()
    desktop_pet_dir = root / "desktop-pet"
    exe_name = "hermes-desktop-pet.exe" if os.name == "nt" else "hermes-desktop-pet"
    source_mtime = _desktop_pet_shell_source_mtime()
    candidates: list[dict] = []

    def add_candidate(candidate: dict) -> None:
        artifact_mtime = _desktop_pet_artifact_mtime(candidate)
        current = _desktop_pet_candidate_is_current(candidate, source_mtime)
        enriched = {
            **candidate,
            "source_mtime": source_mtime,
            "artifact_mtime": artifact_mtime,
            "stale": not current,
        }
        if current or include_stale:
            candidates.append(enriched)

    if sys.platform == "darwin":
        for app_path in (
            Path("/Applications/Hermes Desktop Pet.app"),
            Path.home() / "Applications" / "Hermes Desktop Pet.app",
            desktop_pet_dir / "src-tauri" / "target" / "release" / "bundle" / "macos" / "Hermes Desktop Pet.app",
            desktop_pet_dir / "src-tauri" / "target" / "debug" / "bundle" / "macos" / "Hermes Desktop Pet.app",
        ):
            if app_path.exists():
                add_candidate({"kind": "app", "argv": [str(_desktop_pet_app_executable(app_path))], "cwd": root, "artifact": _desktop_pet_app_executable(app_path)})
    for profile in ("release", "debug"):
        binary = desktop_pet_dir / "src-tauri" / "target" / profile / exe_name
        if binary.is_file():
            add_candidate({"kind": f"{profile}-binary", "argv": [str(binary)], "cwd": root, "artifact": binary})
    if include_dev:
        npm = shutil.which("npm")
        if npm and (desktop_pet_dir / "package.json").is_file() and (desktop_pet_dir / "node_modules").is_dir():
            candidates.append({"kind": "tauri-dev", "argv": [npm, "run", "dev"], "cwd": desktop_pet_dir, "source_mtime": source_mtime, "artifact_mtime": 0.0, "stale": False})
    return candidates


def _run_pet_setup_command(argv: list[str], cwd: Path, *, timeout: int) -> dict:
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Timed out running {' '.join(argv[:2])}"}
    except Exception as exc:
        return {"ok": False, "error": _sanitize_error(exc)}
    if result.returncode == 0:
        return {"ok": True}
    output = "\n".join(part.strip() for part in (result.stderr, result.stdout) if part and part.strip())
    return {"ok": False, "error": _sanitize_error(output or f"Command failed: {' '.join(argv)}")}


def _prepare_desktop_pet_shell() -> dict:
    with _PET_INSTALL_LOCK:
        existing = _desktop_pet_launch_candidates()
        if existing:
            return {
                "ok": True,
                "installed": True,
                "method": existing[0]["kind"],
                "steps": ["found-shell", "loaded-assets"],
            }
        root = _repo_root()
        desktop_pet_dir = root / "desktop-pet"
        if not desktop_pet_dir.is_dir():
            return {"ok": False, "error": "Desktop pet source is missing."}
        npm = shutil.which("npm")
        if npm and (desktop_pet_dir / "package.json").is_file() and (desktop_pet_dir / "node_modules").is_dir():
            result = _run_pet_setup_command([npm, "run", "build"], desktop_pet_dir, timeout=600)
        else:
            cargo = shutil.which("cargo")
            if not cargo:
                return {"ok": False, "error": "Rust cargo is required to build the desktop pet shell."}
            result = _run_pet_setup_command(
                [cargo, "build", "--manifest-path", str(desktop_pet_dir / "src-tauri" / "Cargo.toml")],
                root,
                timeout=600,
            )
        if not result.get("ok"):
            return result
        candidates = _desktop_pet_launch_candidates()
        if not candidates:
            return {"ok": False, "error": "Desktop pet build completed, but no launchable shell was found."}
        return {
            "ok": True,
            "installed": True,
            "method": candidates[0]["kind"],
            "steps": ["built-shell", "loaded-assets"],
        }


def _launch_desktop_pet_process(webui_base_url: str = "") -> dict:
    with _PET_LAUNCH_LOCK:
        running_processes = _desktop_pet_processes()
        if running_processes and any(_desktop_pet_process_matches_base(process, webui_base_url) for process in running_processes):
            return {"ok": True, "already_running": True, "method": "existing"}
        if running_processes and webui_base_url:
            known_mismatched = [process for process in running_processes if _desktop_pet_process_has_known_base(process)]
            unknown = [process for process in running_processes if not _desktop_pet_process_has_known_base(process)]
            if unknown:
                return {
                    "ok": False,
                    "error": "Desktop pet is already running but has not registered its WebUI base URL.",
                    "running": True,
                    "method": "existing-unknown",
                }
            _terminate_desktop_pet_processes(known_mismatched)
            deadline = time.time() + 2
            while time.time() < deadline and _desktop_pet_processes():
                time.sleep(0.1)
        candidates = _desktop_pet_launch_candidates()
        if not candidates:
            return {
                "ok": False,
                "error": "Desktop pet shell is not built or installed.",
                "hint": "Build it from desktop-pet/ or install the packaged app, then try again.",
            }
        log_dir = Path.home() / ".hermes" / "webui"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "desktop-pet-launch.log"
        last_error = ""
        for candidate in candidates:
            log_file = None
            try:
                log_file = log_path.open("ab")
                env = os.environ.copy()
                normalized_base_url = _normalize_desktop_pet_base_url(webui_base_url)
                if normalized_base_url:
                    env["HERMES_DESKTOP_PET_WEBUI_BASE"] = normalized_base_url
                process = subprocess.Popen(
                    candidate["argv"],
                    cwd=str(candidate["cwd"]),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=(os.name != "nt"),
                    env=env,
                )
                if normalized_base_url:
                    _write_desktop_pet_registry({"pid": process.pid, "base_url": normalized_base_url, "registered_at": time.time(), "source": "launch"})
                return {
                    "ok": True,
                    "already_running": False,
                    "method": candidate["kind"],
                    "pid": process.pid,
                }
            except Exception as exc:
                last_error = _sanitize_error(exc)
                logger.debug("desktop pet launch candidate failed: %s", candidate.get("kind"), exc_info=True)
            finally:
                if log_file is not None:
                    try:
                        log_file.close()
                    except Exception:
                        pass
        return {"ok": False, "error": last_error or "Desktop pet launch failed."}


def _handle_pet_launch(handler, body: dict) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet launch is only available from this machine", status=403)
    try:
        result = _launch_desktop_pet_process(_pet_webui_base_url(handler))
        status = 200 if result.get("ok") else 409
        return j(handler, result, status=status)
    except Exception as exc:
        logger.exception("failed to launch desktop pet")
        return j(handler, {"ok": False, "error": _sanitize_error(exc)}, status=500)


def _handle_pet_register(handler, body: dict) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet registration is only available from this machine", status=403)
    try:
        pid = int(body.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return bad(handler, "desktop pet pid is required", status=400)
    base_url = _normalize_desktop_pet_base_url(str(body.get("base_url") or "")) or _pet_webui_base_url(handler)
    if not base_url:
        return bad(handler, "desktop pet base URL is invalid", status=400)
    _write_desktop_pet_registry({"pid": pid, "base_url": base_url, "registered_at": time.time(), "source": "pet"})
    return j(handler, {"ok": True, "pid": pid, "base_url": base_url})


def _handle_pet_install(handler, body: dict) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet install is only available from this machine", status=403)
    try:
        result = _prepare_desktop_pet_shell()
        return j(handler, result, status=200 if result.get("ok") else 409)
    except Exception as exc:
        logger.exception("failed to prepare desktop pet")
        return j(handler, {"ok": False, "error": _sanitize_error(exc)}, status=500)


def _handle_pet_status(handler, body: dict) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet status is only available from this machine", status=403)
    try:
        candidates = _desktop_pet_launch_candidates()
        stale_candidates = _desktop_pet_launch_candidates(include_stale=True)
        first = candidates[0] if candidates else (stale_candidates[0] if stale_candidates else {})
        return j(
            handler,
            {
                "ok": True,
                "installed": bool(candidates),
                "running": _desktop_pet_process_running(_pet_webui_base_url(handler)),
                "method": first.get("kind", ""),
                "stale": bool(stale_candidates) and not bool(candidates),
                "source_mtime": _desktop_pet_shell_source_mtime(),
                "artifact_mtime": first.get("artifact_mtime", 0.0),
            },
        )
    except Exception as exc:
        logger.exception("failed to inspect desktop pet status")
        return j(handler, {"ok": False, "error": _sanitize_error(exc)}, status=500)


def _handle_pet_close(handler, body: dict) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet close is only available from this machine", status=403)
    try:
        result = _close_desktop_pet_processes()
        return j(handler, result, status=200 if result.get("ok") else 409)
    except Exception as exc:
        logger.exception("failed to close desktop pet")
        return j(handler, {"ok": False, "error": _sanitize_error(exc)}, status=500)


def _pet_request_base(handler) -> tuple[str, str]:
    scheme = str(handler.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip().lower()
    if scheme not in {"http", "https"}:
        scheme = "http"
    host = str(handler.headers.get("Host") or "127.0.0.1:8787").split(",")[0].strip().lower()
    if host.startswith("0.0.0.0"):
        host = "127.0.0.1" + host[len("0.0.0.0") :]
    loopback_host = re.fullmatch(r"(localhost|127(?:\.[0-9]{1,3}){3}|\[::1\]|::1)(:[0-9]{1,5})?", host)
    if not loopback_host:
        host = "127.0.0.1:8787"
    return scheme, host



def _pet_webui_base_url(handler) -> str:
    scheme, host = _pet_request_base(handler)
    return f"{scheme}://{host}"

def _pet_open_url(handler, session_id: str, *, draft: str = "", autosend: bool = False) -> str:
    sid = str(session_id or "").strip()
    if not sid or not re.fullmatch(r"[A-Za-z0-9_.-]+", sid):
        raise ValueError("invalid session_id")
    scheme, host = _pet_request_base(handler)
    query = {}
    if draft:
        query["draft"] = str(draft)
    suffix = ("?" + urlencode(query)) if query else ""
    return f"{scheme}://{host}/session/{quote(sid, safe='')}{suffix}"


def _queue_pet_session_navigation(handler, body: dict) -> dict:
    sid = str(body.get("session_id") or "").strip()
    url = _pet_open_url(
        handler,
        sid,
        draft=str(body.get("draft") or ""),
        autosend=bool(body.get("autosend")),
    )
    try:
        get_session(sid, metadata_only=True)
    except KeyError:
        raise ValueError("session not found")
    command = {
        "id": uuid.uuid4().hex,
        "session_id": sid,
        "draft": str(body.get("draft") or ""),
        "autosend": bool(body.get("autosend")),
        "url": url,
        "created_at": time.time(),
    }
    with _PET_NAVIGATION_LOCK:
        _PET_NAVIGATION_COMMANDS.append(command)
        _trim_pet_navigation_commands_locked(now=command["created_at"])
    return command


def _queue_and_focus_pet_session_navigation(handler, body: dict) -> dict:
    command = _queue_pet_session_navigation(handler, body)
    reused = False
    if sys.platform == "darwin" and not _pet_bridge_recently_polled():
        # Bridge is not live — no WebUI tab will pick up the queued command.
        # Fall back to a direct AppleScript URL reuse (hard navigation) so the
        # user sees the session immediately.  When the bridge IS live we skip
        # this step: the bridge will call loadSession(sid) for a smooth in-page
        # transition without a full page reload.
        reused = _reuse_existing_pet_browser_tab(command.get("url", ""))
    command["reused"] = bool(reused)
    command["focused"] = bool(reused)
    return command


def _ack_pet_navigation_command(command_id: str) -> bool:
    command_id = str(command_id or "").strip()
    if not command_id:
        return False
    with _PET_NAVIGATION_LOCK:
        for command in _PET_NAVIGATION_COMMANDS:
            if command.get("id") == command_id:
                command["acked_at"] = time.time()
                return True
    return False


def _wait_for_pet_navigation_ack(command_id: str, *, timeout: float = 1.6) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _PET_NAVIGATION_LOCK:
            for command in _PET_NAVIGATION_COMMANDS:
                if command.get("id") == command_id:
                    if command.get("acked_at"):
                        return True
                    break
            else:
                return False
        time.sleep(0.05)
    return False


def _fallback_open_pet_browser_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname or ""
    if host not in {"localhost", "127.0.0.1", "::1"}:
        return False
    try:
        if sys.platform == "darwin":
            return _open_pet_url_with_macos_browser_script(url)
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception:
        logger.debug("failed to fallback-open desktop pet session url", exc_info=True)
    return False


def _run_pet_browser_open_script(app_name: str, script: str, url: str) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e", script, url],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        logger.debug("failed to run pet browser open script for %s", app_name, exc_info=True)
        return False
    if result.returncode != 0:
        logger.debug("pet browser open script for %s failed: %s", app_name, result.stderr.strip())
        return False
    return result.stdout.strip() == "opened"


def _run_pet_browser_detected_open_script(script: str, url: str, host_candidates: list[str]) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e", script, url, *host_candidates],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except Exception:
        logger.debug("failed to run detected WebUI browser open script", exc_info=True)
        return False
    if result.returncode != 0:
        logger.debug("detected WebUI browser open script failed: %s", result.stderr.strip())
        return False
    return result.stdout.strip().startswith("opened:")


def _launch_pet_browser_open_url(app_name: str, url: str) -> bool:
    try:
        subprocess.Popen(
            ["open", "-a", app_name, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        logger.debug("failed to launch desktop pet browser open for %s", app_name, exc_info=True)
        return False
    return True


def _pet_browser_app_from_user_agent(user_agent: str) -> str:
    ua = user_agent or ""
    if "Edg/" in ua or "EdgA/" in ua or "EdgiOS/" in ua:
        return "Microsoft Edge"
    if "OPR/" in ua or "Opera/" in ua:
        return ""
    if "Chrome/" in ua or "CriOS/" in ua:
        return "Google Chrome"
    if "Safari/" in ua and "Chrome/" not in ua and "Chromium/" not in ua:
        return "Safari"
    return ""


def _record_pet_webui_browser_hint(handler) -> None:
    app_name = _pet_browser_app_from_user_agent(str(handler.headers.get("User-Agent", "")))
    if not app_name:
        return
    with _PET_WEBUI_BROWSER_HINT_LOCK:
        _PET_WEBUI_BROWSER_HINT["app"] = app_name
        _PET_WEBUI_BROWSER_HINT["seen_at"] = time.time()


def _recent_pet_webui_browser_hint(*, max_age_seconds: float = 15.0) -> str:
    with _PET_WEBUI_BROWSER_HINT_LOCK:
        app_name = str(_PET_WEBUI_BROWSER_HINT.get("app") or "")
        seen_at = float(_PET_WEBUI_BROWSER_HINT.get("seen_at") or 0)
    if not app_name or time.time() - seen_at > max_age_seconds:
        return ""
    return app_name


_PET_BROWSER_APPS = ["Google Chrome", "Microsoft Edge", "Brave Browser", "Arc", "Safari"]


def _ordered_pet_browser_apps() -> list[str]:
    """Order browsers so the most-recently-seen WebUI browser is tried first.

    Each tab-reuse/focus probe spawns a separate `osascript` subprocess, so
    looping every installed browser adds hundreds of ms of dead time before the
    real browser is reached (worst case Safari, which is last). The pet bridge
    records the WebUI's browser from its User-Agent on each navigation poll;
    using that hint lets the common case resolve in a single osascript call.
    """
    hint = _recent_pet_webui_browser_hint()
    if hint in _PET_BROWSER_APPS:
        return [hint] + [name for name in _PET_BROWSER_APPS if name != hint]
    return list(_PET_BROWSER_APPS)


def _detect_pet_webui_browser_app(url: str) -> str:
    host_candidates = _pet_browser_host_candidates(url)
    if not host_candidates:
        return ""
    script = r'''
on run argv
  set hostCandidates to {}
  repeat with idx from 1 to count of argv
    set end of hostCandidates to item idx of argv
  end repeat
  set browserNames to {"Google Chrome", "Microsoft Edge", "Brave Browser", "Arc", "Safari"}
  tell application "System Events"
    repeat with appName in browserNames
      if exists (process appName) then
        tell process appName
          repeat with w in windows
            set windowTitle to name of w
            if windowTitle contains "Hermes" then return appName as text
            repeat with hostText in hostCandidates
              if windowTitle contains hostText then return appName as text
            end repeat
          end repeat
        end tell
      end if
    end repeat
  end tell
  return "not-found"
end run
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script, *host_candidates],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.2,
        )
    except Exception:
        logger.debug("failed to detect desktop pet WebUI browser", exc_info=True)
        return ""
    if result.returncode != 0:
        logger.debug("desktop pet WebUI browser detection failed: %s", result.stderr.strip())
        return ""
    app_name = result.stdout.strip()
    return app_name if app_name in {"Google Chrome", "Microsoft Edge", "Brave Browser", "Arc", "Safari"} else ""


def _macos_browser_open_script(app_name: str) -> str:
    if app_name == "Safari":
        return r'''
on run argv
  set targetUrl to item 1 of argv
  tell application "Safari"
    activate
    if (count of windows) = 0 then make new document
    set URL of current tab of front window to targetUrl
    set index of front window to 1
  end tell
  tell application "System Events" to set frontmost of process "Safari" to true
  return "opened"
end run
'''
    safe_names = {"Google Chrome", "Microsoft Edge", "Brave Browser", "Arc"}
    if app_name not in safe_names:
        return ""
    return f'''
on run argv
  set targetUrl to item 1 of argv
  tell application "{app_name}"
    activate
    if (count of windows) = 0 then make new window
    set URL of active tab of front window to targetUrl
    set index of front window to 1
  end tell
  tell application "System Events" to set frontmost of process "{app_name}" to true
  return "opened"
end run
'''


def _open_pet_url_with_macos_browser_script(url: str) -> bool:
    # Open the loopback session URL in the system default browser. The caller
    # (_fallback_open_pet_browser_url) has already validated it as a loopback
    # http(s) URL. We intentionally do NOT gate on a recently-seen WebUI browser
    # hint: `open url` targets the default browser and needs no hint, and the
    # hint is empty in the exact case the fallback exists for — a cold start with
    # no open WebUI tab. Gating here made a bubble click open nothing.
    try:
        result = subprocess.run(
            ["open", url],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        logger.debug("failed to open desktop pet url in default browser", exc_info=True)
        return False
    if result.returncode != 0:
        logger.debug("default browser open failed for desktop pet url: %s", result.stderr.strip())
        return False
    return True


def _handle_pet_navigation_ack(handler, body: dict) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet navigation ack is only available from this machine", status=403)
    ok = _ack_pet_navigation_command(str(body.get("id") or ""))
    return j(handler, {"ok": ok, "server_time": time.time()}, status=200 if ok else 404)

def _pet_bridge_recently_polled() -> bool:
    """True when a WebUI tab polled the navigation bridge recently enough that it
    can still consume + ack a queued command."""
    return (time.time() - _PET_NAVIGATION_LAST_POLL_AT) <= _PET_BRIDGE_POLL_FRESH_SECONDS


def _handle_pet_navigation(handler, parsed) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet navigation is only available from this machine", status=403)
    global _PET_NAVIGATION_LAST_POLL_AT
    _PET_NAVIGATION_LAST_POLL_AT = time.time()
    _record_pet_webui_browser_hint(handler)
    since = str(parse_qs(parsed.query).get("since", [""])[0] or "")
    now = time.time()
    with _PET_NAVIGATION_LOCK:
        _trim_pet_navigation_commands_locked(now=now)
        command = _next_pet_navigation_command_locked(since)
    return j(handler, {"command": command or None, "server_time": time.time()})


def _trim_pet_navigation_commands_locked(*, now: float) -> None:
    cutoff = now - _PET_NAVIGATION_TTL_SECONDS
    _PET_NAVIGATION_COMMANDS[:] = [
        command for command in _PET_NAVIGATION_COMMANDS if float(command.get("created_at") or 0) >= cutoff
    ][-_PET_NAVIGATION_MAX_COMMANDS:]


def _next_pet_navigation_command_locked(since: str) -> dict:
    pending = [command for command in _PET_NAVIGATION_COMMANDS if not command.get("acked_at")]
    if not pending:
        return {}
    if not since:
        return dict(pending[0])
    seen_since = False
    for command in _PET_NAVIGATION_COMMANDS:
        if command.get("id") == since:
            seen_since = True
            continue
        if seen_since and not command.get("acked_at"):
            return dict(command)
    return dict(pending[-1]) if not any(command.get("id") == since for command in _PET_NAVIGATION_COMMANDS) else {}


def _pet_browser_host_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    candidates = []
    if parsed.netloc:
        candidates.append(parsed.netloc)
    if parsed.port:
        candidates.extend(
            [
                f"127.0.0.1:{parsed.port}",
                f"localhost:{parsed.port}",
                f"0.0.0.0:{parsed.port}",
            ]
        )
        if parsed.port == 8787:
            candidates.extend(["127.0.0.1:8790", "localhost:8790", "0.0.0.0:8790"])
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _pet_automation_maybe_available() -> bool:
    """Return False only while a recent osascript probe confirmed Automation is blocked.

    Used to short-circuit the AppleScript browser-control helpers so a process
    without macOS Automation permission does not burn ~1s probing every known
    browser on each click.  After _PET_AUTOMATION_RECHECK_SECONDS we optimistically
    allow another probe in case the user granted permission in the meantime.
    """
    with _PET_AUTOMATION_LOCK:
        if (
            _PET_AUTOMATION_AVAILABLE is False
            and (time.time() - _PET_AUTOMATION_CHECKED_AT) < _PET_AUTOMATION_RECHECK_SECONDS
        ):
            return False
    return True


def _pet_record_automation_result(available: bool) -> None:
    global _PET_AUTOMATION_AVAILABLE, _PET_AUTOMATION_CHECKED_AT
    with _PET_AUTOMATION_LOCK:
        _PET_AUTOMATION_AVAILABLE = bool(available)
        _PET_AUTOMATION_CHECKED_AT = time.time()


def _run_pet_browser_reuse_script(app_name: str, script: str, url: str, host_candidates: list[str]) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e", script, url, *host_candidates],
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
        )
    except Exception:
        # A timeout or spawn failure is transient — do not poison the Automation
        # cache, just report failure for this attempt.
        logger.debug("failed to run pet browser reuse script for %s", app_name, exc_info=True)
        return False
    if result.returncode != 0:
        stderr = result.stderr or ""
        lowered = stderr.lower()
        if "-1743" in stderr or "not authorized" in lowered or "not allowed" in lowered:
            # The Hermes process lacks Automation permission for the browser.
            # Remember so later clicks skip the dead osascript probes and fall
            # straight through to open(1) activation.
            _pet_record_automation_result(False)
        logger.debug("pet browser reuse script for %s failed: %s", app_name, stderr.strip())
        return False
    # A clean exit (even "not-running"/"not-found") proves Automation works.
    _pet_record_automation_result(True)
    return result.stdout.strip() == "reused"


def _reuse_existing_pet_browser_tab(url: str) -> bool:
    if not _pet_automation_maybe_available():
        return False
    host_candidates = _pet_browser_host_candidates(url)
    if not host_candidates:
        return False
    chromium_script_template = r'''
on run argv
  set targetUrl to item 1 of argv
  set hostCandidates to {}
  repeat with idx from 2 to count of argv
    set end of hostCandidates to item idx of argv
  end repeat
  tell application "System Events" to set isRunning to exists (process "{app_name}")
  if isRunning is false then return "not-running"
  tell application "{app_name}"
    repeat with w in windows
      set tabIndex to 1
      repeat with t in tabs of w
        set tabUrl to URL of t
        repeat with hostText in hostCandidates
          if tabUrl contains hostText then
            set URL of t to targetUrl
            set active tab index of w to tabIndex
            set index of w to 1
            activate
            tell application "System Events" to set frontmost of process "{app_name}" to true
            return "reused"
          end if
        end repeat
        set tabIndex to tabIndex + 1
      end repeat
    end repeat
  end tell
  return "not-found"
end run
'''
    safari_script = r'''
on run argv
  set targetUrl to item 1 of argv
  set hostCandidates to {}
  repeat with idx from 2 to count of argv
    set end of hostCandidates to item idx of argv
  end repeat
  tell application "System Events" to set isRunning to exists (process "Safari")
  if isRunning is false then return "not-running"
  tell application "Safari"
    repeat with w in windows
      repeat with t in tabs of w
        set tabUrl to URL of t
        repeat with hostText in hostCandidates
          if tabUrl contains hostText then
            set URL of t to targetUrl
            set current tab of w to t
            set index of w to 1
            activate
            tell application "System Events" to set frontmost of process "Safari" to true
            return "reused"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "not-found"
end run
'''
    for app_name in _ordered_pet_browser_apps():
        if app_name == "Safari":
            ok = _run_pet_browser_reuse_script("Safari", safari_script, url, host_candidates)
        else:
            script = chromium_script_template.replace("{app_name}", app_name)
            ok = _run_pet_browser_reuse_script(app_name, script, url, host_candidates)
        if ok:
            return True
    return False


def _focus_existing_pet_browser_tab(url: str) -> bool:
    if not _pet_automation_maybe_available():
        return False
    host_candidates = _pet_browser_host_candidates(url)
    if not host_candidates:
        return False
    chromium_script_template = r'''
on run argv
  set hostCandidates to {}
  repeat with idx from 2 to count of argv
    set end of hostCandidates to item idx of argv
  end repeat
  tell application "System Events" to set isRunning to exists (process "{app_name}")
  if isRunning is false then return "not-running"
  tell application "{app_name}"
    repeat with w in windows
      set tabIndex to 1
      repeat with t in tabs of w
        set tabUrl to URL of t
        repeat with hostText in hostCandidates
          if tabUrl contains hostText then
            set active tab index of w to tabIndex
            set index of w to 1
            activate
            tell application "System Events" to set frontmost of process "{app_name}" to true
            return "reused"
          end if
        end repeat
        set tabIndex to tabIndex + 1
      end repeat
    end repeat
  end tell
  return "not-found"
end run
'''
    safari_script = r'''
on run argv
  set hostCandidates to {}
  repeat with idx from 2 to count of argv
    set end of hostCandidates to item idx of argv
  end repeat
  tell application "System Events" to set isRunning to exists (process "Safari")
  if isRunning is false then return "not-running"
  tell application "Safari"
    repeat with w in windows
      repeat with t in tabs of w
        set tabUrl to URL of t
        repeat with hostText in hostCandidates
          if tabUrl contains hostText then
            set current tab of w to t
            set index of w to 1
            activate
            tell application "System Events" to set frontmost of process "Safari" to true
            return "reused"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "not-found"
end run
'''
    for app_name in _ordered_pet_browser_apps():
        if app_name == "Safari":
            ok = _run_pet_browser_reuse_script("Safari", safari_script, url, host_candidates)
        else:
            script = chromium_script_template.replace("{app_name}", app_name)
            ok = _run_pet_browser_reuse_script(app_name, script, url, host_candidates)
        if ok:
            return True
    return _focus_existing_pet_browser_window_by_title()


def _focus_existing_pet_browser_window_by_title() -> bool:
    if not _pet_automation_maybe_available():
        return False
    script = r'''
on run argv
  set browserNames to {"Google Chrome", "Microsoft Edge", "Brave Browser", "Arc", "Safari"}
  tell application "System Events"
    repeat with appName in browserNames
      if exists (process appName) then
        tell process appName
          repeat with w in windows
            set windowTitle to name of w
            if windowTitle contains "Hermes" or windowTitle contains "8787" or windowTitle contains "8788" or windowTitle contains "8790" then
              set frontmost to true
              try
                perform action "AXRaise" of w
              end try
              return "reused"
            end if
          end repeat
        end tell
      end if
    end repeat
  end tell
  return "not-found"
end run
'''
    return _run_pet_browser_reuse_script("System Events", script, "", [])


def _foreground_pet_browser_app() -> bool:
    """Bring the running WebUI browser to the foreground using open(1).

    Unlike the AppleScript-based helpers (_focus_existing_pet_browser_*), this
    does not require the macOS 'Automation' TCC permission for the Hermes
    process.  open(1) activates the application via LaunchServices, which works
    unconditionally on macOS regardless of the Accessibility/Automation grant.

    Iterates _ordered_pet_browser_apps() so the browser that last served the
    WebUI is tried first.  Returns True as soon as a running browser is found
    and the activate succeeds.
    """
    if sys.platform != "darwin":
        return False
    for app_name in _ordered_pet_browser_apps():
        try:
            # pgrep -x matches the exact process name — no special permission
            # required.  We only activate a browser that is already running so
            # that we do not accidentally launch an unrelated application.
            chk = subprocess.run(
                ["pgrep", "-x", app_name],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if chk.returncode != 0:
                continue
        except Exception:
            logger.debug("pgrep check failed for %s", app_name, exc_info=True)
            continue
        try:
            result = subprocess.run(
                ["open", "-a", app_name],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                return True
        except Exception:
            logger.debug("open -a failed for %s", app_name, exc_info=True)
    return False


def _handle_pet_open_session(handler, body: dict) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet session navigation is only available from this machine", status=403)
    try:
        command = _queue_and_focus_pet_session_navigation(handler, body)
        ack_focused = False
        if command.get("reused"):
            # An existing browser tab was already navigated to the session URL and
            # brought to the front, so the session is visibly open. Skip the bridge
            # ack wait (up to 1.6s of dead time that only kept the pet spinner up).
            # The queued command stays in the queue and the WebUI bridge still
            # consumes it on its next poll for a clean in-page route.
            consumed = False
        elif _pet_bridge_recently_polled():
            consumed = _wait_for_pet_navigation_ack(str(command.get("id") or ""))
            if consumed and not command.get("focused") and sys.platform == "darwin":
                # _focus_existing_pet_browser_tab switches the browser's ACTIVE
                # tab to the session tab (and raises its window), so it must run
                # first: the bridge navigates the WebUI tab in-page, but that tab
                # is often a background tab in a multi-tab window.  The title-based
                # window raise only fronts the window without switching tabs, so on
                # its own it would surface the wrong (currently-active) tab.  open(1)
                # activation is the permission-free last resort.
                ack_focused = (
                    _focus_existing_pet_browser_tab(str(command.get("url") or ""))
                    or _focus_existing_pet_browser_window_by_title()
                    or _foreground_pet_browser_app()
                )
            elif not consumed and sys.platform == "darwin":
                # Bridge ack timed out — the live tab may be busy or the poll
                # interval missed the window.  Fall back to the same hard
                # URL-change path used for cold starts so the user sees
                # navigation immediately rather than a silent failure.
                reused_late = _reuse_existing_pet_browser_tab(str(command.get("url") or ""))
                command["reused"] = bool(reused_late)
                command["focused"] = bool(reused_late)
        else:
            # Cold start: no WebUI tab has polled the navigation bridge recently, so
            # there is no live page to consume + ack the command. Don't burn the
            # ack timeout waiting for a reply that cannot come — fall straight
            # through to opening the session in a fresh browser tab.
            consumed = False
        opened = False if (consumed or command.get("reused")) else _fallback_open_pet_browser_url(str(command.get("url") or ""))
        return j(
            handler,
            {
                "ok": True,
                "queued": True,
                "consumed": bool(consumed),
                "opened": bool(opened),
                "focused": bool(command.get("focused") or ack_focused),
                "reused": bool(command.get("reused")),
                "command": command,
                "url": command.get("url", ""),
            },
        )
    except ValueError as exc:
        return bad(handler, str(exc), status=400)
    except Exception as exc:
        logger.exception("failed to open pet session")
        return j(handler, {"ok": False, "url": "", "error": _sanitize_error(exc)}, status=500)


def handle_get(handler, parsed) -> bool:
    if parsed.path == "/pet":
        _handle_pet_page(handler)
        return True
    if parsed.path == "/pet/bubbles":
        _handle_pet_page(handler, "bubbles.html")
        return True
    if parsed.path == "/api/pet/attention":
        _handle_pet_attention(handler, parsed)
        return True
    if parsed.path == "/api/pet/skins":
        _handle_pet_skins(handler, parsed)
        return True
    if parsed.path == "/api/pet/preference":
        _handle_pet_preference(handler)
        return True
    if parsed.path == "/api/pet/navigation":
        _handle_pet_navigation(handler, parsed)
        return True
    return False


def handle_post(handler, parsed, body: dict) -> bool:
    if parsed.path == "/api/pet/status":
        _handle_pet_status(handler, body)
        return True
    if parsed.path == "/api/pet/install":
        _handle_pet_install(handler, body)
        return True
    if parsed.path == "/api/pet/launch":
        _handle_pet_launch(handler, body)
        return True
    if parsed.path == "/api/pet/register":
        _handle_pet_register(handler, body)
        return True
    if parsed.path == "/api/pet/close":
        _handle_pet_close(handler, body)
        return True
    if parsed.path == "/api/pet/preference":
        _handle_pet_preference(handler, body)
        return True
    if parsed.path == "/api/pet/navigation_ack":
        _handle_pet_navigation_ack(handler, body)
        return True
    if parsed.path == "/api/pet/open_session":
        _handle_pet_open_session(handler, body)
        return True
    return False
