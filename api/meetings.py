"""Neo Meetings — local-first meeting storage and room URL generation."""

import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from api.config import MEETINGS_FILE

_LOCK = threading.RLock()

MEETING_STATUS_VALUES = {"planned", "active", "finished", "processed"}
OBJECTIVE_VALUES = {"alinhamento", "homologacao", "fechamento_sprint", "briefing", "suporte", "outro"}

MEET_BASE_URL = os.getenv("NEO_MEET_BASE_URL", "https://meet.jit.si")

PARTICIPANT_ROLES = {"host", "client", "team", "guest"}


def _normalize_participants(raw: list | None) -> list[dict]:
    """Accept both legacy string[] and structured object[] formats."""
    if not raw:
        return []
    result = []
    for item in raw:
        if isinstance(item, str):
            name = item.strip()
            if name:
                result.append({"name": name, "email": "", "whatsapp": "", "role": "guest"})
        elif isinstance(item, dict):
            name = (item.get("name") or "").strip()
            if name:
                result.append({
                    "name": name,
                    "email": (item.get("email") or "").strip(),
                    "whatsapp": (item.get("whatsapp") or "").strip(),
                    "role": item.get("role", "guest") if item.get("role") in PARTICIPANT_ROLES else "guest",
                })
    return result


def _now() -> float:
    return time.time()


def _generate_room_slug(project: str, title: str) -> str:
    ts = time.strftime("%Y%m%d-%H%M")
    suffix = secrets.token_hex(3)
    slug = f"{project}-{ts}-{suffix}".lower()
    return "".join(c if c.isalnum() or c == "-" else "-" for c in slug)


def _load_store() -> list[dict]:
    if not MEETINGS_FILE.exists():
        return []
    try:
        data = json.loads(MEETINGS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        for m in data:
            if "participants" in m:
                m["participants"] = _normalize_participants(m["participants"])
        return data
    except (json.JSONDecodeError, OSError):
        return []


def _save_store(meetings: list[dict]) -> None:
    MEETINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEETINGS_FILE.write_text(json.dumps(meetings, ensure_ascii=False, indent=2), encoding="utf-8")


def load_meetings() -> list[dict]:
    with _LOCK:
        return _load_store()


def create_meeting(
    title: str,
    project: str,
    objective: str = "alinhamento",
    participants: list | None = None,
) -> dict:
    room_slug = _generate_room_slug(project, title)
    meeting = {
        "id": str(uuid.uuid4()),
        "title": title.strip(),
        "project": project.strip(),
        "objective": objective if objective in OBJECTIVE_VALUES else "outro",
        "participants": _normalize_participants(participants),
        "room_slug": room_slug,
        "room_url": f"{MEET_BASE_URL}/{room_slug}",
        "status": "planned",
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "summary": None,
    }
    with _LOCK:
        store = _load_store()
        store.insert(0, meeting)
        _save_store(store)
    return meeting


def get_meeting(meeting_id: str) -> dict | None:
    with _LOCK:
        for m in _load_store():
            if m["id"] == meeting_id:
                return m
    return None


def start_meeting(meeting_id: str) -> dict | None:
    with _LOCK:
        store = _load_store()
        for m in store:
            if m["id"] == meeting_id:
                m["status"] = "active"
                m["started_at"] = _now()
                _save_store(store)
                return m
    return None


def finish_meeting(meeting_id: str) -> dict | None:
    with _LOCK:
        store = _load_store()
        for m in store:
            if m["id"] == meeting_id:
                m["status"] = "finished"
                m["finished_at"] = _now()
                _save_store(store)
                return m
    return None


def update_summary(meeting_id: str, summary: dict[str, Any]) -> dict | None:
    with _LOCK:
        store = _load_store()
        for m in store:
            if m["id"] == meeting_id:
                m["summary"] = summary
                m["status"] = "processed"
                _save_store(store)
                return m
    return None
