"""
Session creator attribution — sidecar SQLite DB.

Rationale: state.db belongs to hermes-agent core. Modifying its schema risks
being clobbered by an upstream migration (same lesson learned with auth.db).
We own a separate DB at HERMES_WEBUI_HOME/session_attribution.db.

Table: session_creators(
    session_id  TEXT PRIMARY KEY,
    created_by_json TEXT NOT NULL,   -- JSON blob matching the created_by spec
    created_at  INTEGER NOT NULL     -- Unix timestamp
)

created_by JSON schema:
{
  "source": "webui" | "slack" | "kanban" | "cron" | "api" | "unknown",
  "user_id": null | str,          -- auth.db user id (webui only)
  "user_email": null | str,       -- auth.db email (webui only)
  "display_name": null | str,     -- best human-readable label
  "platform_user_id": null | str, -- Slack UID etc.
  "agent_identity": null | str,   -- hermes profile / bot name
  "created_at_iso": str           -- ISO 8601 UTC
}
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_UNKNOWN_CREATED_BY: dict[str, Any] = {"source": "unknown", "display_name": None}

# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

def _attribution_db_path() -> Path:
    """Return path to session_attribution.db (created on demand)."""
    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    db_dir = hermes_home / "webui"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "session_attribution.db"


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_creators (
            session_id       TEXT PRIMARY KEY,
            created_by_json  TEXT NOT NULL,
            created_at       INTEGER NOT NULL
        )
        """
    )
    conn.commit()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_attribution_db_path()), timeout=5.0)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_webui_created_by(
    user: dict | None,
    agent_identity: str | None = None,
) -> dict:
    """Build created_by dict for a WebUI-originated session.

    *user* is the dict returned by ``api.userauth.get_session_user()``.
    """
    if not user:
        return {"source": "unknown", "display_name": None, "created_at_iso": _now_iso()}

    email: str | None = user.get("email")
    display_name: str | None = user.get("display_name")
    if not display_name and email:
        display_name = email.split("@")[0]

    return {
        "source": "webui",
        "user_id": str(user.get("id", "")) or None,
        "user_email": email or None,
        "display_name": display_name,
        "platform_user_id": None,
        "agent_identity": agent_identity or None,
        "created_at_iso": _now_iso(),
    }


def build_slack_created_by(
    slack_user_id: str | None = None,
    display_name: str | None = None,
    agent_identity: str | None = None,
) -> dict:
    """Build created_by dict for a Slack-originated session."""
    return {
        "source": "slack",
        "user_id": None,
        "user_email": None,
        "display_name": display_name or None,
        "platform_user_id": slack_user_id or None,
        "agent_identity": agent_identity or None,
        "created_at_iso": _now_iso(),
    }


def build_kanban_created_by(agent_identity: str | None) -> dict:
    """Build created_by dict for a kanban-worker-originated session."""
    return {
        "source": "kanban",
        "user_id": None,
        "user_email": None,
        "display_name": None,
        "platform_user_id": None,
        "agent_identity": agent_identity or None,
        "created_at_iso": _now_iso(),
    }


def build_cron_created_by(agent_identity: str | None) -> dict:
    """Build created_by dict for a cron/scheduled-job-originated session."""
    return {
        "source": "cron",
        "user_id": None,
        "user_email": None,
        "display_name": None,
        "platform_user_id": None,
        "agent_identity": agent_identity or "cron-job",
        "created_at_iso": _now_iso(),
    }


def build_api_created_by(key_label: str | None) -> dict:
    """Build created_by dict for a direct API-key-originated session."""
    display = f"API: {key_label}" if key_label else "API"
    return {
        "source": "api",
        "user_id": None,
        "user_email": None,
        "display_name": display,
        "platform_user_id": None,
        "agent_identity": None,
        "created_at_iso": _now_iso(),
    }


def record_session_creator(session_id: str, created_by: dict) -> None:
    """Persist creator metadata for *session_id* (INSERT OR REPLACE).

    Idempotent — safe to call multiple times. Best-effort: failures are logged
    but never propagated to the caller so session creation is never blocked.
    """
    if not session_id:
        return
    try:
        payload = json.dumps(created_by, ensure_ascii=False)
        with closing(_get_conn()) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO session_creators
                    (session_id, created_by_json, created_at)
                VALUES (?, ?, ?)
                """,
                (session_id, payload, int(time.time())),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to record session creator for %s: %s", session_id, exc)


def get_session_creator(session_id: str) -> dict:
    """Return the stored created_by dict for *session_id*.

    Falls back to ``{"source": "unknown", "display_name": null}`` if nothing
    is stored or the JSON is unparseable.
    """
    if not session_id:
        return _UNKNOWN_CREATED_BY.copy()
    try:
        with closing(_get_conn()) as conn:
            row = conn.execute(
                "SELECT created_by_json FROM session_creators WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return _UNKNOWN_CREATED_BY.copy()
        try:
            data = json.loads(row["created_by_json"])
        except (json.JSONDecodeError, TypeError):
            return _UNKNOWN_CREATED_BY.copy()
        if not isinstance(data, dict) or not data.get("source"):
            return _UNKNOWN_CREATED_BY.copy()
        return data
    except Exception as exc:
        logger.warning("Failed to fetch session creator for %s: %s", session_id, exc)
        return _UNKNOWN_CREATED_BY.copy()


def get_many_session_creators(session_ids: list[str]) -> dict[str, dict]:
    """Batch-fetch created_by dicts for a list of session IDs.

    Returns a dict keyed by session_id. Missing / malformed entries map to
    the unknown default.  Sessions not in *session_ids* are not included.
    """
    if not session_ids:
        return {}
    result: dict[str, dict] = {}
    try:
        wanted = [str(s) for s in session_ids if s]
        IN_CHUNK = 500
        with closing(_get_conn()) as conn:
            for i in range(0, len(wanted), IN_CHUNK):
                chunk = wanted[i: i + IN_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT session_id, created_by_json FROM session_creators WHERE session_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    sid = row["session_id"]
                    try:
                        data = json.loads(row["created_by_json"])
                    except (json.JSONDecodeError, TypeError):
                        data = None
                    if not isinstance(data, dict) or not data.get("source"):
                        data = _UNKNOWN_CREATED_BY.copy()
                    result[sid] = data
    except Exception as exc:
        logger.warning("Failed to batch-fetch session creators: %s", exc)
    # Fill missing entries with the unknown default
    for sid in session_ids:
        if sid not in result:
            result[sid] = _UNKNOWN_CREATED_BY.copy()
    return result


# ---------------------------------------------------------------------------
# Request-level helper: infer creator from incoming headers (all paths)
# ---------------------------------------------------------------------------

# Header names injected by the slack-router when it spawns a WebUI session
_HEADER_CREATOR_SOURCE = "X-Hermes-Creator-Source"
_HEADER_CREATOR_USER_ID = "X-Hermes-Creator-User-Id"
_HEADER_CREATOR_DISPLAY_NAME = "X-Hermes-Creator-Display-Name"
_HEADER_CREATOR_AGENT_IDENTITY = "X-Hermes-Creator-Agent-Identity"


def infer_creator_from_headers(headers: dict) -> dict | None:
    """Read X-Hermes-Creator-* headers and build a created_by dict.

    Returns None if no creator headers are present (caller should fall back
    to cookie-based WebUI inference).
    """
    # Case-insensitive header lookup
    lowered = {k.lower(): v for k, v in headers.items()}
    source = lowered.get(_HEADER_CREATOR_SOURCE.lower(), "").strip().lower()
    if not source:
        return None

    user_id = lowered.get(_HEADER_CREATOR_USER_ID.lower(), "").strip() or None
    display_name = lowered.get(_HEADER_CREATOR_DISPLAY_NAME.lower(), "").strip() or None
    agent_identity = lowered.get(_HEADER_CREATOR_AGENT_IDENTITY.lower(), "").strip() or None

    if source == "slack":
        return build_slack_created_by(
            slack_user_id=user_id,
            display_name=display_name,
            agent_identity=agent_identity,
        )
    if source == "kanban":
        return build_kanban_created_by(agent_identity=agent_identity or user_id)
    if source == "cron":
        return build_cron_created_by(agent_identity=agent_identity or display_name)
    if source == "api":
        return build_api_created_by(key_label=display_name or agent_identity)
    # Unknown source in header — still record it rather than silently ignoring
    return {
        "source": source,
        "user_id": user_id,
        "user_email": None,
        "display_name": display_name,
        "platform_user_id": None,
        "agent_identity": agent_identity,
        "created_at_iso": _now_iso(),
    }


def infer_creator_from_env() -> dict | None:
    """Read HERMES_CREATED_BY env var (kanban/cron dispatcher path).

    Returns None if the env var is absent or malformed.
    """
    raw = os.environ.get("HERMES_CREATED_BY", "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("HERMES_CREATED_BY is not valid JSON: %r", raw[:200])
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("created_at_iso"):
        data["created_at_iso"] = _now_iso()
    return data
