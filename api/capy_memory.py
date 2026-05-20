"""Clean-room Capy Memory Tree primitives.

This module stores and exposes only bounded, sanitized summaries. Retrieved
memory is advisory context; it must not bypass Spaces safety gates, prompt
injection checks, approval gates, or rollback/recovery controls.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import ntpath
import os
import re
import socket
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_MAX_SCAN_DEPTH = 24
_MAX_SCAN_NODES = 2_000
_MAX_TEXT_LEN = 500
_MAX_REFRESH_FETCH_BYTES = 64 * 1024
_REFRESH_FETCH_TIMEOUT_SECONDS = 8
_REFRESH_ALLOWED_CONTENT_TYPES = (
    "text/html",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/json",
)

_UNSAFE_KEY_RE = re.compile(
    r"renderer|html|script|source|data|code|body|generated[_-]?code|generatedbody|rendercode|widgetbody|"
    r"api[_-]?key|api[_-]?auth|apiauth|authorization|bearer|token|secret|password|credential|"
    r"^on[a-z]+$",
    re.IGNORECASE,
)

_UNSAFE_VALUE_RE = re.compile(
    r"SECRET_VALUE_DO_NOT_LEAK|<\s*/?\s*script\b|bearer\s+placeholder|raw\s+prompt|"
    r"system\s+prompt|developer\s+prompt|prompt\s+injection|ignore\s+previous\s+instructions|"
    r"<[^>]+\bon[a-z]+\s*=",
    re.IGNORECASE,
)

_UNSAFE_PUBLIC_VALUE_RE = re.compile(
    r"SECRET_VALUE_DO_NOT_LEAK|<\s*/?\s*script\b|<[^>]+>|bearer\b|api[ _-]?key|api[ _-]?auth|"
    r"\b(?:sk|pk)-(?:live|test)(?:[-_][A-Za-z0-9]+)*\b|gh[pousr]_[A-Za-z0-9_]+|"
    r"renderer|rendercode|generated[_ -]?code|raw\s+prompt|ignore\s+previous\s+instructions|"
    r"credential|password|secret(?!ary)|token(?!ization)|authorization|cookie|"
    r"(?:^|[._/\s])on(?:click|load|error|submit|change|mouseover|focus|blur)(?:$|[._/\s])|"
    r"(?:^|[._/\s])(?:html|script|source|data|body|code)(?:$|[._/\s])|"
    r"(?:html|script|source|data|body|code)(?:panel|widget|module|source|body)",
    re.IGNORECASE,
)

_MANIFEST_PUBLIC_VALUE_RE = re.compile(
    r"SECRET_VALUE_DO_NOT_LEAK|<\s*/?\s*script\b|<[^>]+>|bearer\b|api[ _-]?key|api[ _-]?auth|"
    r"\b(?:sk|pk)-(?:live|test)(?:[-_][A-Za-z0-9]+)*\b|gh[pousr]_[A-Za-z0-9_]+|"
    r"renderer|rendercode|generated[_ -]?code|raw\s+prompt|ignore\s+previous\s+instructions|"
    r"credential|password|secret(?!ary)|token(?!ization)|authorization|"
    r"(?:^|[._/\s])on(?:click|load|error|submit|change|mouseover|focus|blur)(?:$|[._/\s])|"
    r"(?:^|[._/\s])(?:html|script|body|code)(?:$|[._/\s])|"
    r"(?:html|script|body|code)(?:panel|widget|module|source|body)",
    re.IGNORECASE,
)

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._:-]+")

_REFRESH_BLOCKED_VALUE_RE = re.compile(
    r"SECRET_VALUE_DO_NOT_LEAK|<\s*/?\s*script\b|<[^>]+>|bearer\b|api[ _-]?key|api[ _-]?auth|"
    r"\b(?:sk|pk)-(?:live|test)(?:[-_][A-Za-z0-9]+)*\b|gh[pousr]_[A-Za-z0-9_]+|"
    r"renderer|rendercode|generated[_ -]?code|raw\s+prompt|raw\s+fetched\s+body|"
    r"ignore\s+previous\s+instructions|credential|password|secret(?!ary)|token(?!ization)|"
    r"authorization|cookie|"
    r"(?:^|[._/\s])on(?:click|load|error|submit|change|mouseover|focus|blur)(?:$|[._/\s])",
    re.IGNORECASE,
)

_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    origin_uri TEXT NOT NULL,
    origin_kind TEXT NOT NULL DEFAULT 'local',
    space_id TEXT,
    artifact_ref TEXT,
    content_sha256 TEXT,
    freshness_status TEXT NOT NULL DEFAULT 'unknown',
    last_ingested_at TEXT,
    last_checked_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_type_status ON sources(source_type, freshness_status);
CREATE INDEX IF NOT EXISTS idx_sources_space ON sources(space_id);
CREATE INDEX IF NOT EXISTS idx_sources_updated ON sources(updated_at);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    source_ref TEXT NOT NULL,
    content_path TEXT NOT NULL,
    summary TEXT NOT NULL,
    approx_tokens INTEGER NOT NULL DEFAULT 0,
    lifecycle_status TEXT NOT NULL DEFAULT 'admitted',
    redaction_status TEXT NOT NULL DEFAULT 'none',
    start_line INTEGER,
    end_line INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_lifecycle ON chunks(lifecycle_status);
CREATE INDEX IF NOT EXISTS idx_chunks_source_ref ON chunks(source_ref);

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    hotness_score REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_kind_hotness ON entities(kind, hotness_score);

CREATE TABLE IF NOT EXISTS chunk_entities (
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    score REAL NOT NULL DEFAULT 1,
    PRIMARY KEY(chunk_id, entity_id)
);

CREATE TABLE IF NOT EXISTS summary_nodes (
    node_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    level INTEGER NOT NULL,
    summary_path TEXT NOT NULL,
    child_refs_json TEXT NOT NULL DEFAULT '[]',
    redaction_status TEXT NOT NULL DEFAULT 'none',
    sealed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summary_scope ON summary_nodes(scope, scope_id, level);
CREATE INDEX IF NOT EXISTS idx_summary_sealed ON summary_nodes(sealed_at);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    leased_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, leased_until);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(kind, dedupe_key);
"""

_TABLES = ("sources", "chunks", "entities", "chunk_entities", "summary_nodes", "jobs")


def memory_tree_root() -> Path:
    return Path(os.getenv("CAPY_MEMORY_TREE_ROOT") or "~/.hermes/capy-memory-tree").expanduser().resolve()


def memory_tree_db_path() -> Path:
    configured = os.getenv("CAPY_MEMORY_TREE_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    return memory_tree_root() / "capy-memory-tree.sqlite3"


def memory_tree_vault_path() -> Path:
    configured = os.getenv("CAPY_MEMORY_TREE_VAULT")
    if configured:
        return Path(configured).expanduser().resolve()
    return memory_tree_root() / "vault"


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = (db_path or memory_tree_db_path()).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_memory_tree(db_path: Path | None = None) -> dict[str, Any]:
    """Create local Memory Tree storage and return safe metadata."""
    db = (db_path or memory_tree_db_path()).expanduser().resolve()
    vault = memory_tree_vault_path()
    vault.mkdir(parents=True, exist_ok=True)
    with _connect(db) as conn:
        conn.executescript(_SCHEMA_SQL)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    tables = [row[0] for row in rows]
    return {
        "available": True,
        "local_only": True,
        "db_exists": db.exists(),
        "db_path": str(db),
        "vault_path": str(vault),
        "tables": tables,
    }


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0] if row else 0)


def memory_status() -> dict[str, Any]:
    """Return bounded local Memory Tree status without reading source bodies."""
    db = memory_tree_db_path()
    if not db.exists():
        return {
            "available": True,
            "local_only": True,
            "db_exists": False,
            "source_count": 0,
            "chunk_count": 0,
            "stale_source_count": 0,
            "last_error_count": 0,
            "refresh_job_count": 0,
        }
    with _connect(db) as conn:
        conn.executescript(_SCHEMA_SQL)
        return {
            "available": True,
            "local_only": True,
            "db_exists": True,
            "source_count": _count(conn, "SELECT COUNT(*) FROM sources"),
            "chunk_count": _count(conn, "SELECT COUNT(*) FROM chunks"),
            "stale_source_count": _count(conn, "SELECT COUNT(*) FROM sources WHERE freshness_status = 'stale'"),
            "last_error_count": _count(conn, "SELECT COUNT(*) FROM sources WHERE last_error IS NOT NULL AND last_error != ''"),
            "refresh_job_count": _count(
                conn,
                "SELECT COUNT(*) FROM jobs WHERE kind = 'source.refresh' AND status IN ('pending', 'leased', 'completing')",
            ),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _lease_until_marker(seconds: int = 300) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return f"{expires_at.isoformat()}|{uuid.uuid4().hex}"


def _safe_origin_uri(value: Any, *, source_id: str) -> str:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return f"capy-memory://{source_id}"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return f"capy-memory://{source_id}"
    if parts.scheme in {"http", "https"} and parts.netloc:
        safe_path = _safe_text(parts.path or "/", limit=240) or "/"
        normalized_path = re.sub(r"[^a-z0-9]+", " ", unquote(safe_path).lower()).strip()
        if _UNSAFE_PUBLIC_VALUE_RE.search(safe_path) or "raw prompt" in normalized_path:
            safe_path = "/"
        try:
            port = parts.port
        except ValueError:
            port = None
        host = _safe_text(parts.hostname or "", limit=200)
        if not host:
            return f"capy-memory://{source_id}"
        netloc = host
        if port is not None:
            netloc = f"{host}:{port}"
        return urlunsplit((parts.scheme, netloc, safe_path, "", ""))
    text = _safe_text(raw.split("#", 1)[0].split("?", 1)[0], limit=500)
    if not text or _UNSAFE_PUBLIC_VALUE_RE.search(text):
        return f"capy-memory://{source_id}"
    return text


def _safe_refresh_interval(value: Any) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = 3600
    return max(60, min(interval, 604800))


def register_source_reference(record: dict[str, Any]) -> dict[str, Any]:
    """Register a source for future auto-fetch without fetching or storing bodies."""
    if not isinstance(record, dict):
        raise ValueError("source reference must be a mapping")
    init_memory_tree()
    origin_seed = _safe_origin_uri(record.get("origin_uri") or record.get("url"), source_id="source")
    fallback_id = "source-" + _sha256(origin_seed)[:12]
    source_id = _safe_public_id(record.get("source_id") or record.get("id"), fallback=fallback_id)
    origin_uri = _safe_origin_uri(record.get("origin_uri") or record.get("url"), source_id=source_id)
    display_name = _safe_public_text(
        record.get("title") or record.get("display_name") or record.get("name"),
        limit=200,
    ) or source_id
    refresh_interval = _safe_refresh_interval(record.get("refresh_interval_seconds"))
    now = _now_iso()
    job_id = "cmt-job-" + _sha256(f"source.refresh:{source_id}")[:24]
    payload = {
        "source_id": source_id,
        "origin_uri": origin_uri,
        "refresh_interval_seconds": refresh_interval,
    }
    with _connect() as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO sources (
                source_id, source_type, display_name, origin_uri, origin_kind, space_id,
                artifact_ref, content_sha256, freshness_status, last_checked_at, created_at, updated_at
            ) VALUES (?, 'source_registry', ?, ?, 'auto_fetch', NULL, NULL, NULL, 'stale', NULL, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                display_name=excluded.display_name,
                origin_uri=excluded.origin_uri,
                origin_kind='auto_fetch',
                freshness_status='stale',
                updated_at=excluded.updated_at
            """,
            (source_id, display_name, origin_uri, now, now),
        )
        existing_row = conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        existing_status = str(existing_row[0]) if existing_row else ""
        existing_active = existing_status in {"pending", "leased", "completing"}
        if existing_row is None:
            conn.execute(
                """
                INSERT INTO jobs (job_id, kind, dedupe_key, payload_json, status, attempts, created_at, updated_at)
                VALUES (?, 'source.refresh', ?, ?, 'pending', 0, ?, ?)
                """,
                (job_id, source_id, json.dumps(payload, sort_keys=True, separators=(",", ":")), now, now),
            )
        elif existing_status == "pending":
            conn.execute(
                "UPDATE jobs SET payload_json = ?, updated_at = ? WHERE job_id = ?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")), now, job_id),
            )
        elif existing_status in {"leased", "completing"}:
            pass
        else:
            conn.execute(
                """
                UPDATE jobs
                SET payload_json = ?, status = 'pending', attempts = 0, leased_until = NULL, last_error = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (json.dumps(payload, sort_keys=True, separators=(",", ":")), now, job_id),
            )
    return {
        "ok": True,
        "local_only": True,
        "source_id": source_id,
        "origin_uri": origin_uri,
        "origin_kind": "auto_fetch",
        "job_id": job_id,
        "queued": not existing_active,
        "metadata_only": True,
    }


def _safe_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_iso_timestamp(value: Any) -> str:
    if not isinstance(value, _PUBLIC_SCALAR_TYPES):
        return ""
    raw = str(value).strip()
    if not raw or _UNSAFE_VALUE_RE.search(raw) or _UNSAFE_PUBLIC_VALUE_RE.search(raw):
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.replace(microsecond=0).isoformat()


_LOCAL_KNOWLEDGE_PATH_RE = re.compile(r"(^~?[/\\])|([A-Za-z]:[/\\])|([/\\][^\s]+)")
_LOCAL_KNOWLEDGE_RUN_STATUSES = {"ok", "success", "stale", "error", "failed", "failure", "unknown"}


def _safe_local_knowledge_text(value: Any, *, limit: int, fallback: str = "") -> str:
    text = _safe_public_text(value, limit=limit)
    if not text or _LOCAL_KNOWLEDGE_PATH_RE.search(text):
        return fallback
    return text


def _safe_local_knowledge_run_status(value: Any) -> str:
    text = _safe_public_text(value, limit=80).lower()
    if not text:
        return "unknown"
    first_token = re.split(r"\s+", text, maxsplit=1)[0]
    if first_token in _LOCAL_KNOWLEDGE_RUN_STATUSES:
        return first_token
    if _LOCAL_KNOWLEDGE_PATH_RE.search(text):
        return "unknown"
    return text if text in _LOCAL_KNOWLEDGE_RUN_STATUSES else "unknown"


def _local_knowledge_freshness(status: dict[str, Any]) -> str:
    run_status = _safe_local_knowledge_run_status(status.get("last_run_status"))
    if (
        status.get("available") is False
        or status.get("config_ok") is False
        or status.get("db_exists") is False
        or _safe_nonnegative_int(status.get("last_error_count")) > 0
        or run_status in {"error", "failed", "failure"}
    ):
        return "error"
    if _safe_nonnegative_int(status.get("stale_source_count")) > 0 or run_status == "stale":
        return "stale"
    if _safe_nonnegative_int(status.get("source_count")) > 0 or _safe_nonnegative_int(status.get("chunk_count")) > 0:
        return "ok"
    return "unknown"


def _upsert_local_knowledge_source(
    *,
    source_id: str,
    source_type: str,
    display_name: str,
    origin_uri: str,
    freshness_status: str,
    checked_at: str,
    last_error: str = "",
) -> dict[str, Any]:
    safe_source_id = _safe_public_id(source_id, fallback="local-knowledge-source")
    safe_source_type = _safe_public_text(source_type, limit=80) or "local_knowledge_source"
    safe_display_name = _safe_public_text(display_name, limit=200) or safe_source_id
    safe_origin_uri = _safe_origin_uri(origin_uri, source_id=safe_source_id)
    safe_freshness = freshness_status if freshness_status in {"ok", "stale", "error", "unknown"} else "unknown"
    safe_checked_at = _safe_iso_timestamp(checked_at) or _now_iso()
    safe_last_error = _safe_text(last_error, limit=120) if last_error in {
        "local knowledge unavailable",
        "local knowledge source unavailable",
    } else ""
    safe_last_error = safe_last_error or None
    now = _now_iso()
    with _connect() as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO sources (
                source_id, source_type, display_name, origin_uri, origin_kind, space_id,
                artifact_ref, content_sha256, freshness_status, last_ingested_at,
                last_checked_at, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'local_knowledge', NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                source_type=excluded.source_type,
                display_name=excluded.display_name,
                origin_uri=excluded.origin_uri,
                origin_kind='local_knowledge',
                freshness_status=excluded.freshness_status,
                last_ingested_at=excluded.last_ingested_at,
                last_checked_at=excluded.last_checked_at,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                safe_source_id,
                safe_source_type,
                safe_display_name,
                safe_origin_uri,
                safe_freshness,
                safe_checked_at,
                safe_checked_at,
                safe_last_error,
                now,
                now,
            ),
        )
    result = {
        "source_id": safe_source_id,
        "source_type": safe_source_type,
        "origin_kind": "local_knowledge",
        "origin_uri": safe_origin_uri,
        "freshness_status": safe_freshness,
        "metadata_only": True,
    }
    if safe_last_error:
        result["last_error"] = safe_last_error
    return result


def _local_knowledge_source_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    return [row for row in rows[:100] if isinstance(row, dict)]


def _local_knowledge_row_record(row: dict[str, Any], *, fallback_checked_at: str) -> dict[str, Any]:
    raw_path = str(row.get("path") or "")
    source_type_seed = _safe_local_knowledge_text(row.get("source_type"), limit=80, fallback="local")
    title_seed = _safe_local_knowledge_text(row.get("title"), limit=180)
    indexed_seed = _safe_iso_timestamp(row.get("indexed_at"))
    source_hash_seed = raw_path or json.dumps(
        {
            "source_type": source_type_seed,
            "title": title_seed,
            "indexed_at": indexed_seed,
            "exists_now": row.get("exists_now") is not False,
            "has_error": _is_present_public_value(row.get("last_error")),
        },
        sort_keys=True,
    )
    source_hash = _sha256(source_hash_seed)[:16]
    exists_now = row.get("exists_now") is not False
    has_error = _is_present_public_value(row.get("last_error"))
    freshness = "stale" if not exists_now else "error" if has_error else "ok"
    safe_type = source_type_seed or "local"
    safe_title = title_seed or f"Local knowledge {safe_type} source"
    indexed_at = indexed_seed or fallback_checked_at
    return _upsert_local_knowledge_source(
        source_id=f"local-knowledge-source-{source_hash}",
        source_type="local_knowledge_source",
        display_name=safe_title,
        origin_uri=f"capy-knowledge://item/{source_hash}",
        freshness_status=freshness,
        checked_at=indexed_at,
        last_error="local knowledge source unavailable" if freshness in {"stale", "error"} and has_error else "",
    )


def register_local_knowledge_sources(
    status: dict[str, Any] | None = None,
    *,
    source_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bridge the existing local knowledge index into Memory Tree source metadata.

    This deliberately registers provenance/freshness records only. It does not
    read, copy, chunk, or persist local knowledge file bodies into the Memory
    Tree vault; content remains behind the existing local knowledge APIs.
    """
    init_memory_tree()
    if status is None:
        try:
            from api.knowledge import sources_payload, status_payload

            status = status_payload()
            if source_rows is None:
                source_rows = sources_payload(limit=100).get("sources", [])
        except Exception:  # noqa: BLE001 - route/status must fail closed without leaking paths/errors.
            status = {"available": False, "local_only": True, "config_ok": False, "db_exists": False}
            source_rows = []
    if not isinstance(status, dict):
        raise ValueError("local knowledge status must be a mapping")

    freshness = _local_knowledge_freshness(status)
    checked_at = _safe_iso_timestamp(status.get("last_successful_run")) or _now_iso()
    local_rows = _local_knowledge_source_rows(source_rows or [])
    index_freshness = "error" if freshness == "error" else "ok" if local_rows else freshness
    sources = [
        _upsert_local_knowledge_source(
            source_id="local-knowledge-index",
            source_type="local_knowledge_index",
            display_name="Local knowledge index",
            origin_uri="capy-knowledge://local-index",
            freshness_status=index_freshness,
            checked_at=checked_at,
            last_error="local knowledge unavailable" if index_freshness == "error" else "",
        )
    ]
    sources.extend(
        _local_knowledge_row_record(row, fallback_checked_at=checked_at)
        for row in local_rows
    )
    return {
        "ok": True,
        "local_only": True,
        "metadata_only": True,
        "registered_source_count": len(sources),
        "sources": sources,
        "knowledge_summary": {
            "available": bool(status.get("available", True)),
            "source_count": _safe_nonnegative_int(status.get("source_count")),
            "chunk_count": _safe_nonnegative_int(status.get("chunk_count")),
            "stale_source_count": _safe_nonnegative_int(status.get("stale_source_count")),
            "last_error_count": _safe_nonnegative_int(status.get("last_error_count")),
            "last_run_status": _safe_local_knowledge_run_status(status.get("last_run_status")),
        },
    }


def list_source_refresh_jobs(*, limit: int = 10) -> dict[str, Any]:
    """Return bounded metadata-only pending source refresh jobs."""
    limit = max(1, min(int(limit or 10), 25))
    init_memory_tree()
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_SQL)
        rows = conn.execute(
            """
            SELECT job_id, kind, payload_json, status, attempts, created_at, updated_at
            FROM jobs
            WHERE kind = 'source.refresh' AND status IN ('pending', 'leased', 'completing')
            ORDER BY created_at ASC, updated_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    jobs = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        source_id = _safe_public_id(payload.get("source_id"), fallback="")
        origin_uri = _safe_origin_uri(payload.get("origin_uri"), source_id=source_id or "source")
        jobs.append({
            "job_id": _safe_public_id(row["job_id"], fallback=""),
            "kind": "source.refresh",
            "source_id": source_id,
            "origin_uri": origin_uri,
            "status": _safe_public_text(row["status"], limit=40) or "pending",
            "attempts": max(0, int(row["attempts"] or 0)),
            "created_at": _safe_text(row["created_at"], limit=80),
            "updated_at": _safe_text(row["updated_at"], limit=80),
        })
    return {"local_only": True, "limit": limit, "jobs": jobs}


def _refresh_content_type(headers: Any) -> str:
    try:
        raw = headers.get("Content-Type") or headers.get("content-type") or ""
    except AttributeError:
        raw = ""
    return str(raw).split(";", 1)[0].strip().lower()


def _refresh_charset(headers: Any) -> str:
    try:
        raw = headers.get("Content-Type") or headers.get("content-type") or ""
    except AttributeError:
        raw = ""
    match = re.search(r"charset=([A-Za-z0-9._:-]+)", str(raw), flags=re.IGNORECASE)
    return match.group(1) if match else "utf-8"


class _NoRefreshRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 - urllib hook name.
        raise RuntimeError("refresh fetcher disabled")


def _refresh_open(request: Request, *, timeout: int = _REFRESH_FETCH_TIMEOUT_SECONDS):
    opener = build_opener(_NoRefreshRedirect)
    return opener.open(request, timeout=timeout)


class _RefreshMetadataParser(HTMLParser):
    _HIDDEN_TAGS = {"script", "style", "noscript", "template", "svg"}
    _DESCRIPTION_MARKERS = {"description", "og:description", "twitter:description"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.description_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._HIDDEN_TAGS:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        if tag == "title":
            self.in_title = True
            return
        if tag != "meta":
            return
        attr_map = {str(key).lower(): (value or "") for key, value in attrs}
        marker = (attr_map.get("name") or attr_map.get("property") or "").strip().lower()
        content = attr_map.get("content") or ""
        if marker in self._DESCRIPTION_MARKERS and content:
            self.description_parts.append(content)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1
            return
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.hidden_depth or not self.in_title:
            return
        self.title_parts.append(data)


def _bounded_refresh_summary(text: Any, *, limit: int = 1_200) -> str:
    if not isinstance(text, _PUBLIC_SCALAR_TYPES):
        return ""
    raw = str(text)
    parts = [part.strip() for part in re.split(r"[\r\n]+|(?<=[.!?])\s+", raw) if part.strip()]
    if not parts:
        parts = [raw]
    safe_parts: list[str] = []
    for part in parts:
        cleaned = _safe_text(part, limit=limit)
        if not cleaned or _REFRESH_BLOCKED_VALUE_RE.search(cleaned):
            continue
        safe_parts.append(cleaned)
        if len(" ".join(safe_parts)) >= limit:
            break
    return _safe_text(" ".join(safe_parts), limit=limit)


def _refresh_record_from_json(source_id: str, origin_uri: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("refresh failed")
    title = _safe_public_text(payload.get("title") or payload.get("name") or payload.get("display_name"), limit=200)
    summary = _bounded_refresh_summary(payload.get("summary") or payload.get("description") or payload.get("abstract"))
    if not summary:
        raise ValueError("refresh failed")
    return {
        "metadata_only": True,
        "title": title or source_id,
        "summary": summary,
        "origin_uri": _safe_origin_uri(origin_uri, source_id=source_id),
    }


def _refresh_record_from_text(source_id: str, origin_uri: str, text: str, *, content_type: str) -> dict[str, Any]:
    source_text = text
    title = ""
    summary = ""
    if content_type == "text/html":
        parser = _RefreshMetadataParser()
        parser.feed(source_text)
        parser.close()
        title = _safe_public_text(" ".join(parser.title_parts), limit=200)
        summary = _bounded_refresh_summary(" ".join(parser.description_parts))
    else:
        lines = []
        for line in source_text.splitlines():
            if re.match(r"^\s*(summary|description)\s*:", line, flags=re.IGNORECASE):
                lines.append(re.sub(r"^\s*(summary|description)\s*:\s*", "", line, flags=re.IGNORECASE))
        summary = _bounded_refresh_summary(" ".join(lines))
    if not summary:
        raise ValueError("refresh failed")
    return {
        "metadata_only": True,
        "title": title or source_id,
        "summary": summary,
        "origin_uri": _safe_origin_uri(origin_uri, source_id=source_id),
    }


def _default_source_refresh_fetcher(*, source_id: str, origin_uri: str) -> dict[str, Any]:
    safe_source_id = _safe_public_id(source_id, fallback="source")
    safe_origin_uri = _safe_origin_uri(origin_uri, source_id=safe_source_id)
    if not _source_refresh_allowed(safe_origin_uri):
        raise RuntimeError("refresh fetcher disabled")
    request = Request(
        safe_origin_uri,
        headers={
            "User-Agent": "Capy-Memory-Refresh/1.0",
            "Accept": "text/html,text/plain,text/markdown,application/json;q=0.8",
        },
    )
    with _refresh_open(request, timeout=_REFRESH_FETCH_TIMEOUT_SECONDS) as response:
        final_url = getattr(response, "geturl", lambda: safe_origin_uri)()
        if not _source_refresh_allowed(_safe_origin_uri(final_url, source_id=safe_source_id)):
            raise RuntimeError("refresh fetcher disabled")
        content_type = _refresh_content_type(response.headers)
        if content_type not in _REFRESH_ALLOWED_CONTENT_TYPES:
            raise RuntimeError("refresh fetcher disabled")
        raw = response.read(_MAX_REFRESH_FETCH_BYTES + 1)
        if len(raw) > _MAX_REFRESH_FETCH_BYTES:
            raw = raw[:_MAX_REFRESH_FETCH_BYTES]
        text = raw.decode(_refresh_charset(response.headers), errors="replace")
    if content_type == "application/json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("refresh fetcher disabled") from exc
        return _refresh_record_from_json(safe_source_id, safe_origin_uri, payload)
    return _refresh_record_from_text(safe_source_id, safe_origin_uri, text, content_type=content_type)


def _refresh_allowed_hosts() -> set[str]:
    return {
        host.strip().strip("[]").rstrip(".").lower()
        for host in (os.getenv("CAPY_MEMORY_REFRESH_ALLOWED_HOSTS") or "").split(",")
        if host.strip()
    }


def _source_refresh_ip_blocked(address: Any) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _source_refresh_allowed(origin_uri: str) -> bool:
    try:
        parts = urlsplit(origin_uri)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"}:
        return False
    hostname = (parts.hostname or "").strip().rstrip(".").lower()
    if not hostname:
        return False
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            socket.inet_aton(hostname)
        except OSError:
            address = None
        else:
            # inet_aton accepts legacy IPv4 spellings such as 2130706433,
            # 0x7f000001, 017700000001, and 127.1. Reject them rather than
            # treating non-canonical numeric hosts as arbitrary DNS names.
            return False
    else:
        if _source_refresh_ip_blocked(address):
            return False

    allowed_hosts = _refresh_allowed_hosts()
    if not allowed_hosts:
        return False
    if hostname in allowed_hosts:
        return True
    if address is not None and str(address).lower() in allowed_hosts:
        return True
    return False


def _safe_refresh_summary_with_drop(value: Any, *, limit: int = 1_200) -> tuple[str, int]:
    if not isinstance(value, _PUBLIC_SCALAR_TYPES):
        return "", 1 if _is_present_public_value(value) else 0
    text = _safe_text(value, limit=limit)
    if not text:
        return "", 1 if _is_present_public_value(value) else 0
    if _REFRESH_BLOCKED_VALUE_RE.search(text):
        return "", 1
    return text, 0


def _source_refresh_record(source_id: str, origin_uri: str, fetched: Any) -> dict[str, Any]:
    if not isinstance(fetched, dict):
        raise ValueError("refresh result must be a mapping")
    if fetched.get("metadata_only") is not True:
        raise ValueError("refresh result must be metadata-only")
    dropped_field_count = _scan_for_unsafe(fetched)
    title, dropped = _safe_public_text_with_drop(
        fetched.get("title") or fetched.get("name") or fetched.get("display_name"),
        limit=200,
        fallback=source_id,
    )
    title_preflight_text = title if dropped == 0 else ""
    dropped_field_count += dropped
    summary, dropped = _safe_refresh_summary_with_drop(
        fetched.get("summary") or fetched.get("description") or fetched.get("abstract"),
        limit=1_200,
    )
    dropped_field_count += dropped
    if not summary:
        raise ValueError("refresh result did not include a safe summary")
    safe_origin_uri = _safe_origin_uri(origin_uri, source_id=source_id)
    body = "\n".join([
        f"# {title}",
        "",
        "## Refresh summary",
        f"- source_id: {source_id}",
        f"- origin_uri: {safe_origin_uri}",
        "- metadata_only: True",
        "- advisory_context: True",
        "",
        summary,
    ]).strip() + "\n"
    content_sha256 = _sha256(body)
    redaction_status = "dropped_fields" if dropped_field_count else "none"
    frontmatter = _frontmatter({
        "source_id": source_id,
        "source_type": "source_refresh_summary",
        "origin_uri": safe_origin_uri,
        "content_sha256": content_sha256,
        "redaction_status": redaction_status,
        "metadata_only": True,
    })
    return {
        "source_id": source_id,
        "chunk_id": "cmt-chunk-" + _sha256(f"source_refresh_summary:{source_id}:{content_sha256}")[:24],
        "source_type": "source_refresh_summary",
        "origin_uri": safe_origin_uri,
        "space_id": "",
        "content_sha256": content_sha256,
        "redaction_status": redaction_status,
        "dropped_field_count": dropped_field_count,
        "prompt_preflight_text": "\n".join(part for part in (title_preflight_text, summary) if part),
        "markdown": frontmatter + "\n\n" + body,
    }


def _safe_refresh_error(_exc: BaseException) -> str:
    return "refresh failed"


def _source_refresh_progress_run_id(*, source_id: str, job_id: str) -> str:
    source_seed = _safe_text(source_id, limit=500) or "source"
    job_seed = _safe_text(job_id, limit=500) or "job"
    source_token = _sha256(f"source.refresh.progress.source:{source_seed}")[:24]
    job_token = _sha256(f"source.refresh.progress.job:{job_seed}")[:24]
    return f"memory-ingest:src-{source_token}:job-{job_token}"


def _record_source_refresh_progress(event_type: str, *, source_id: str, job_id: str) -> None:
    """Best-effort metadata-only progress producer for source refresh ingest."""
    try:
        from api.capy_progress import record_progress_event

        record_progress_event(
            {
                "event_type": event_type,
                "run_id": _source_refresh_progress_run_id(source_id=source_id, job_id=job_id),
            }
        )
    except Exception:  # noqa: BLE001 - progress telemetry must not fail refresh work.
        return


def _refresh_lease_owned(job_id: str, lease_marker: str) -> bool:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT 1
            FROM jobs
            WHERE job_id = ? AND kind = 'source.refresh' AND status = 'leased' AND leased_until = ?
            """,
            (job_id, lease_marker),
        ).fetchone() is not None


def _refresh_mark_completing_if_owned(job_id: str, lease_marker: str) -> bool:
    completing_at = _now_iso()
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE jobs
            SET status = 'completing', updated_at = ?
            WHERE job_id = ? AND kind = 'source.refresh' AND status = 'leased' AND leased_until = ?
            """,
            (completing_at, job_id, lease_marker),
        )
        return cursor.rowcount == 1


def run_source_refresh_jobs(*, limit: int = 5, fetcher: Any | None = None) -> dict[str, Any]:
    """Lease queued source.refresh jobs and store sanitized advisory summaries only."""
    limit = max(1, min(int(limit or 5), 25))
    init_memory_tree()
    fetch = fetcher or _default_source_refresh_fetcher
    now = _now_iso()
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_SQL)
        rows = conn.execute(
            """
            SELECT job_id, payload_json, attempts
            FROM jobs
            WHERE kind = 'source.refresh'
              AND (
                status = 'pending'
                OR (status IN ('leased', 'completing') AND (leased_until IS NULL OR leased_until < ?))
              )
            ORDER BY attempts ASC, updated_at ASC, created_at ASC, job_id ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        lease_rows: list[dict[str, Any]] = []
        for row in rows:
            lease_marker = _lease_until_marker(300)
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'leased', attempts = attempts + 1, leased_until = ?, last_error = NULL, updated_at = ?
                WHERE job_id = ?
                  AND kind = 'source.refresh'
                  AND (
                status = 'pending'
                OR (status IN ('leased', 'completing') AND (leased_until IS NULL OR leased_until < ?))
              )
                """,
                (lease_marker, now, row["job_id"], now),
            )
            if cursor.rowcount != 1:
                continue
            leased_row = dict(row)
            leased_row["lease_marker"] = lease_marker
            leased_row["lease_attempts"] = max(1, int(row["attempts"] or 0) + 1)
            lease_rows.append(leased_row)

    results: list[dict[str, Any]] = []
    for row in lease_rows:
        job_id = _safe_public_id(row.get("job_id"), fallback="")
        lease_marker = str(row.get("lease_marker") or "")
        preflight_receipt: dict[str, Any] | None = None
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except json.JSONDecodeError:
            payload = {}
        source_id = _safe_public_id(payload.get("source_id"), fallback="source")
        origin_uri = _safe_origin_uri(payload.get("origin_uri"), source_id=source_id)
        try:
            _record_source_refresh_progress("memory.ingest.started", source_id=source_id, job_id=job_id)
            if not _source_refresh_allowed(origin_uri):
                raise ValueError("refresh failed")
            fetched = fetch(source_id=source_id, origin_uri=origin_uri)
            record = _source_refresh_record(source_id, origin_uri, fetched)
            from api.capy_policy import prompt_preflight

            preflight_receipt = prompt_preflight(record.get("prompt_preflight_text") or record.get("markdown", ""), boundary="auto_fetched_source")
            if preflight_receipt.get("status") == "block":
                raise ValueError("refresh failed")
            if not _refresh_lease_owned(job_id, lease_marker):
                continue
            if not _refresh_mark_completing_if_owned(job_id, lease_marker):
                continue
            receipt = ingest_source(record)
            completed_at = _now_iso()
            with _connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'completed', leased_until = NULL, last_error = NULL, updated_at = ?
                    WHERE job_id = ? AND kind = 'source.refresh' AND status = 'completing' AND leased_until = ?
                    """,
                    (completed_at, job_id, lease_marker),
                )
                if cursor.rowcount != 1:
                    continue
                conn.execute(
                    """
                    UPDATE sources
                    SET origin_kind = 'metadata_only', freshness_status = 'ok', last_checked_at = ?, last_error = NULL, updated_at = ?
                    WHERE source_id = ?
                    """,
                    (completed_at, completed_at, source_id),
                )
            _record_source_refresh_progress("memory.ingest.completed", source_id=source_id, job_id=job_id)
            results.append({
                "job_id": job_id,
                "source_id": source_id,
                "status": "completed",
                "chunk_id": receipt["chunk_id"],
                "prompt_preflight": preflight_receipt,
                "metadata_only": True,
            })
        except Exception as exc:  # noqa: BLE001 - failures are captured as safe metadata for retry/status.
            error = _safe_refresh_error(exc)
            failed_at = _now_iso()
            attempts = max(1, int(row.get("lease_attempts") or 1))
            preflight_blocked = isinstance(preflight_receipt, dict) and preflight_receipt.get("status") == "block"
            next_status = "pending" if preflight_blocked else ("failed" if attempts >= 3 else "pending")
            with _connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, leased_until = NULL, last_error = ?, updated_at = ?
                    WHERE job_id = ?
                      AND kind = 'source.refresh'
                      AND status IN ('leased', 'completing')
                      AND leased_until = ?
                    """,
                    (next_status, error, failed_at, job_id, lease_marker),
                )
                if cursor.rowcount != 1:
                    continue
                conn.execute(
                    """
                    UPDATE sources
                    SET freshness_status = 'error', last_checked_at = ?, last_error = ?, updated_at = ?
                    WHERE source_id = ?
                    """,
                    (failed_at, error, failed_at, source_id),
                )
            _record_source_refresh_progress("memory.ingest.failed", source_id=source_id, job_id=job_id)
            failure_result = {
                "job_id": job_id,
                "source_id": source_id,
                "status": next_status,
                "error": error,
                "metadata_only": True,
            }
            if isinstance(preflight_receipt, dict):
                failure_result["prompt_preflight"] = preflight_receipt
            results.append(failure_result)
    return {
        "local_only": True,
        "metadata_only": True,
        "limit": limit,
        "processed": len(results),
        "jobs": results,
    }


def _safe_vault_file(source_id: str) -> Path:
    safe_source_id = _safe_id(source_id, fallback="source")
    vault = memory_tree_vault_path().resolve()
    vault.mkdir(parents=True, exist_ok=True)
    path = (vault / f"{safe_source_id}.md").resolve()
    try:
        path.relative_to(vault)
    except ValueError as exc:
        raise ValueError("memory content path escaped vault") from exc
    return path


def _snippet(markdown: str, query: str = "", *, limit: int = 700) -> str:
    lines = [line.strip() for line in markdown.splitlines() if line.strip() and line.strip() != "---"]
    if query:
        query_lower = query.lower()
        for line in lines:
            if query_lower in line.lower():
                return _safe_text(line, limit=limit)
    text = " ".join(lines)
    return _safe_text(text, limit=limit)


def _public_hit(row: sqlite3.Row | tuple[Any, ...], *, query: str = "") -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        data = dict(row)
    else:
        keys = ["source_id", "chunk_id", "source_type", "display_name", "origin_uri", "space_id", "summary", "redaction_status"]
        data = dict(zip(keys, row))
    return {
        "source_id": _safe_text(data.get("source_id"), limit=160),
        "chunk_id": _safe_text(data.get("chunk_id"), limit=160),
        "source_type": _safe_text(data.get("source_type"), limit=80),
        "title": _safe_text(data.get("display_name"), limit=200),
        "origin_uri": _safe_text(data.get("origin_uri"), limit=300),
        "space_id": _safe_text(data.get("space_id"), limit=160),
        "snippet": _snippet(str(data.get("summary") or ""), query=query),
        "redaction_status": _safe_text(data.get("redaction_status"), limit=80),
    }


def ingest_source(record: dict[str, Any]) -> dict[str, Any]:
    """Persist one sanitized canonical source record idempotently."""
    init_memory_tree()
    source_id = _safe_id(record.get("source_id"), fallback="source")
    chunk_id = _safe_id(record.get("chunk_id"), fallback=f"{source_id}-chunk")
    source_type = _safe_text(record.get("source_type"), limit=80) or "unknown"
    space_id = _safe_text(record.get("space_id"), limit=160)
    origin_uri = _safe_text(record.get("origin_uri"), limit=500) or f"capy-memory://{source_id}"
    markdown = str(record.get("markdown") or "")
    if not markdown.strip():
        raise ValueError("canonical record markdown is required")
    if _UNSAFE_VALUE_RE.search(markdown):
        raise ValueError("canonical record markdown contains unsafe source content")
    content_sha256 = _safe_text(record.get("content_sha256"), limit=80) or _sha256(markdown)
    display_name = _snippet(markdown, limit=160) or source_id
    redaction_status = _safe_text(record.get("redaction_status"), limit=80) or "none"
    content_path = _safe_vault_file(source_id)
    existed = content_path.exists()
    content_path.write_text(markdown, encoding="utf-8")
    now = _now_iso()
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_SQL)
        source_exists = conn.execute("SELECT 1 FROM sources WHERE source_id = ?", (source_id,)).fetchone() is not None
        conn.execute(
            """
            INSERT INTO sources (
                source_id, source_type, display_name, origin_uri, origin_kind, space_id,
                artifact_ref, content_sha256, freshness_status, last_ingested_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'local', ?, ?, ?, 'ok', ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                source_type=excluded.source_type,
                display_name=excluded.display_name,
                origin_uri=excluded.origin_uri,
                space_id=excluded.space_id,
                artifact_ref=excluded.artifact_ref,
                content_sha256=excluded.content_sha256,
                freshness_status='ok',
                last_ingested_at=excluded.last_ingested_at,
                last_error=NULL,
                updated_at=excluded.updated_at
            """,
            (source_id, source_type, display_name, origin_uri, space_id, str(content_path), content_sha256, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO chunks (
                chunk_id, source_id, source_ref, content_path, summary, approx_tokens,
                lifecycle_status, redaction_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'admitted', ?, ?, ?)
            ON CONFLICT(chunk_id) DO UPDATE SET
                source_id=excluded.source_id,
                source_ref=excluded.source_ref,
                content_path=excluded.content_path,
                summary=excluded.summary,
                approx_tokens=excluded.approx_tokens,
                lifecycle_status='admitted',
                redaction_status=excluded.redaction_status,
                updated_at=excluded.updated_at
            """,
            (
                chunk_id,
                source_id,
                origin_uri,
                str(content_path),
                markdown,
                max(1, len(markdown.split())),
                redaction_status,
                now,
                now,
            ),
        )
    return {
        "ok": True,
        "local_only": True,
        "source_id": source_id,
        "chunk_id": chunk_id,
        "content_path": str(content_path),
        "created": not (source_exists or existed),
    }


def search_memory(query: str, *, space_id: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Search sanitized Memory Tree snippets with bounded metadata results."""
    query_text = _safe_text(query, limit=200)
    if not query_text:
        raise ValueError("query is required")
    limit = max(1, min(int(limit or 10), 25))
    init_memory_tree()
    pattern = f"%{query_text.lower()}%"
    params: list[Any] = [pattern]
    sql = """
        SELECT s.source_id, c.chunk_id, s.source_type, s.display_name, s.origin_uri,
               s.space_id, c.summary, c.redaction_status
        FROM chunks c
        JOIN sources s ON s.source_id = c.source_id
        WHERE lower(c.summary) LIKE ?
    """
    if space_id:
        sql += " AND s.space_id = ?"
        params.append(_safe_text(space_id, limit=160))
    sql += " ORDER BY s.updated_at DESC, c.updated_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_SQL)
        rows = conn.execute(sql, params).fetchall()
    return {
        "query": query_text,
        "limit": limit,
        "space_id": _safe_text(space_id, limit=160) if space_id else None,
        "local_only": True,
        "results": [_public_hit(row, query=query_text) for row in rows],
    }


def relevant_memory_for_space(space_id: str, *, limit: int = 5, exclude_auto_ingested: bool = False) -> dict[str, Any]:
    """Return recent sanitized snippets for one Space."""
    safe_space_id = _safe_text(space_id, limit=160)
    if not safe_space_id:
        raise ValueError("space_id is required")
    limit = max(1, min(int(limit or 5), 25))
    init_memory_tree()
    sql = """
            SELECT s.source_id, c.chunk_id, s.source_type, s.display_name, s.origin_uri,
                   s.space_id, c.summary, c.redaction_status
            FROM chunks c
            JOIN sources s ON s.source_id = c.source_id
            WHERE s.space_id = ?
    """
    params: list[Any] = [safe_space_id]
    if exclude_auto_ingested:
        sql += " AND s.origin_uri NOT LIKE ?"
        params.append("%ingest=auto%")
    sql += """
            ORDER BY s.updated_at DESC, c.updated_at DESC
            LIMIT ?
    """
    params.append(limit)
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_SQL)
        rows = conn.execute(sql, params).fetchall()
    return {
        "space_id": safe_space_id,
        "limit": limit,
        "local_only": True,
        "results": [_public_hit(row) for row in rows],
    }


def _safe_text(value: Any, *, limit: int = _MAX_TEXT_LEN) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if _UNSAFE_VALUE_RE.search(text):
        return ""
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _safe_id(value: Any, *, fallback: str = "unknown") -> str:
    text = _safe_text(value, limit=160)
    text = _SAFE_ID_RE.sub("-", text).strip("-._:")
    return text or fallback


def _scan_for_unsafe(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> int:
    """Return count of unsafe fields/values and fail closed on complex input."""
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if depth > _MAX_SCAN_DEPTH or nodes[0] > _MAX_SCAN_NODES:
        raise ValueError("source metadata is too deep or too complex to canonicalize safely")

    dropped = 0
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if _UNSAFE_KEY_RE.search(key_text):
                dropped += 1
                continue
            dropped += _scan_for_unsafe(item, depth=depth + 1, nodes=nodes)
        return dropped
    if isinstance(value, (list, tuple)):
        for item in value:
            dropped += _scan_for_unsafe(item, depth=depth + 1, nodes=nodes)
        return dropped
    if isinstance(value, str) and _UNSAFE_VALUE_RE.search(value):
        return 1
    return 0


def _frontmatter(data: dict[str, Any]) -> str:
    lines = ["---"]
    for key in sorted(data):
        val = _safe_text(data[key], limit=1_000)
        lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_PUBLIC_SCALAR_TYPES = (str, int, float, bool)


def _is_present_public_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _safe_public_text(value: Any, *, limit: int = _MAX_TEXT_LEN) -> str:
    if not isinstance(value, _PUBLIC_SCALAR_TYPES):
        return ""
    text = _safe_text(value, limit=limit)
    if not text or _UNSAFE_PUBLIC_VALUE_RE.search(text):
        return ""
    return text


def _safe_public_id(value: Any, *, fallback: str = "") -> str:
    if not isinstance(value, _PUBLIC_SCALAR_TYPES):
        return fallback
    raw_text = _safe_text(value, limit=160)
    if not raw_text or _UNSAFE_PUBLIC_VALUE_RE.search(raw_text):
        return fallback
    text = _safe_id(value, fallback="")
    if not text or _UNSAFE_PUBLIC_VALUE_RE.search(text):
        return fallback
    return text


def _safe_public_text_with_drop(value: Any, *, limit: int = _MAX_TEXT_LEN, fallback: str = "") -> tuple[str, int]:
    safe = _safe_public_text(value, limit=limit)
    if safe:
        return safe, 0
    return fallback, 1 if _is_present_public_value(value) else 0


def _safe_public_id_with_drop(value: Any, *, fallback: str = "") -> tuple[str, int]:
    safe = _safe_public_id(value)
    if safe:
        return safe, 0
    return fallback, 1 if _is_present_public_value(value) else 0


def _safe_public_id_list(value: Any, *, limit: int = 12) -> tuple[list[str], int]:
    if not isinstance(value, list):
        return [], 1 if _is_present_public_value(value) else 0
    raw_items = value
    safe_items: list[str] = []
    dropped = 0
    for item in raw_items[: max(0, limit)]:
        raw_value = item
        if isinstance(item, dict):
            raw_value = item.get("id") or item.get("widget_id") or item.get("widgetId") or item.get("name")
        safe = _safe_public_id(raw_value)
        if safe:
            safe_items.append(safe)
        else:
            dropped += 1
    if len(raw_items) > limit:
        dropped += len(raw_items) - limit
    return safe_items, dropped


def _safe_manifest_public_text_with_drop(value: Any, *, limit: int = _MAX_TEXT_LEN, fallback: str = "") -> tuple[str, int]:
    if not isinstance(value, _PUBLIC_SCALAR_TYPES):
        return fallback, 1 if _is_present_public_value(value) else 0
    text = _safe_text(value, limit=limit)
    if text and not _MANIFEST_PUBLIC_VALUE_RE.search(text):
        return text, 0
    return fallback, 1 if _is_present_public_value(value) else 0


def _safe_manifest_public_id_with_drop(value: Any, *, fallback: str = "") -> tuple[str, int]:
    if not isinstance(value, _PUBLIC_SCALAR_TYPES):
        return fallback, 1 if _is_present_public_value(value) else 0
    raw_text = _safe_text(value, limit=160)
    if raw_text and not _MANIFEST_PUBLIC_VALUE_RE.search(raw_text):
        safe = _safe_id(value, fallback="")
        if safe and not _MANIFEST_PUBLIC_VALUE_RE.search(safe):
            return safe, 0
    return fallback, 1 if _is_present_public_value(value) else 0


def _canonical_artifact_record(
    *,
    source_type: str,
    space_id: str,
    body_title: str,
    body_lines: list[str],
    origin_uri: str,
    dropped_field_count: int,
) -> dict[str, Any]:
    body = "\n".join([f"# {body_title}", "", *body_lines]).strip() + "\n"
    content_sha256 = _sha256(body)
    source_id_seed = f"{source_type}:{space_id}:{origin_uri}:{content_sha256}"
    source_id = "cmt-src-" + _sha256(source_id_seed)[:24]
    chunk_id = "cmt-chunk-" + _sha256(f"{source_id}:0:{content_sha256}")[:24]
    redaction_status = "dropped_fields" if dropped_field_count else "none"
    frontmatter = _frontmatter(
        {
            "source_id": source_id,
            "source_type": source_type,
            "origin_uri": origin_uri,
            "space_id": space_id,
            "content_sha256": content_sha256,
            "redaction_status": redaction_status,
        }
    )
    return {
        "source_id": source_id,
        "chunk_id": chunk_id,
        "source_type": source_type,
        "origin_uri": origin_uri,
        "space_id": space_id,
        "content_sha256": content_sha256,
        "redaction_status": redaction_status,
        "dropped_field_count": dropped_field_count,
        "markdown": frontmatter + "\n\n" + body,
    }


def canonicalize_space_revision_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, metadata-only memory record for a Space revision event."""
    if not isinstance(event, dict):
        raise ValueError("space revision event must be a mapping")

    dropped_field_count = _scan_for_unsafe(event)
    space_id, dropped = _safe_public_id_with_drop(event.get("space_id") or event.get("spaceId"), fallback="space")
    dropped_field_count += dropped
    event_id, dropped = _safe_public_id_with_drop(event.get("event_id") or event.get("eventId") or event.get("revision_event_id"))
    dropped_field_count += dropped
    event_type, dropped = _safe_public_text_with_drop(
        event.get("event_type") or event.get("eventType") or event.get("type"),
        limit=120,
        fallback="space.revision",
    )
    dropped_field_count += dropped
    reason, dropped = _safe_public_text_with_drop(event.get("reason") or event.get("checkpoint_reason"), limit=400)
    dropped_field_count += dropped
    timeline_state, dropped = _safe_public_text_with_drop(event.get("timeline_state") or event.get("timelineState"), limit=80)
    dropped_field_count += dropped

    body_lines: list[str] = ["## Revision metadata"]
    _append_line(body_lines, "space_id", space_id)
    _append_line(body_lines, "event_id", event_id)
    _append_line(body_lines, "event_type", event_type)
    _append_line(body_lines, "reason", reason)
    _append_line(body_lines, "timeline_state", timeline_state)

    raw_restore_diff = event.get("restore_diff")
    restore_diff = raw_restore_diff if isinstance(raw_restore_diff, dict) else {}
    if not isinstance(raw_restore_diff, dict) and _is_present_public_value(raw_restore_diff):
        dropped_field_count += 1
    if restore_diff:
        body_lines.extend(["", "## Restore diff"])
        for field in ("widgets_to_add", "widgets_to_update", "widgets_to_remove"):
            items, dropped = _safe_public_id_list(restore_diff.get(field), limit=20)
            dropped_field_count += dropped
            if items:
                body_lines.append(f"- {field}: {', '.join(items)}")

    origin = f"capy-space://{space_id}/revision/{event_id or _sha256(event_type)[:12]}"
    return _canonical_artifact_record(
        source_type="space_revision_event",
        space_id=space_id,
        body_title="Space revision event",
        body_lines=body_lines,
        origin_uri=origin,
        dropped_field_count=dropped_field_count,
    )


def canonicalize_space_widget_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, metadata-only memory record for a Space widget event."""
    if not isinstance(event, dict):
        raise ValueError("space widget event must be a mapping")

    dropped_field_count = _scan_for_unsafe(event)
    raw_details = event.get("details")
    details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
    space_id, dropped = _safe_public_id_with_drop(event.get("space_id") or event.get("spaceId"), fallback="space")
    dropped_field_count += dropped
    widget_id, dropped = _safe_public_id_with_drop(
        event.get("widget_id") or event.get("widgetId") or event.get("id") or details.get("widget_id") or details.get("widgetId")
    )
    dropped_field_count += dropped
    event_id, dropped = _safe_public_id_with_drop(event.get("event_id") or event.get("eventId"))
    dropped_field_count += dropped
    event_name, dropped = _safe_public_text_with_drop(
        event.get("event_name") or event.get("eventName") or event.get("name") or details.get("event_name") or details.get("eventName"),
        limit=120,
        fallback="widget.event",
    )
    dropped_field_count += dropped
    status, dropped = _safe_public_text_with_drop(event.get("status") or details.get("status"), limit=80, fallback="queued")
    dropped_field_count += dropped

    body_lines: list[str] = ["## Widget event metadata"]
    _append_line(body_lines, "space_id", space_id)
    _append_line(body_lines, "widget_id", widget_id)
    _append_line(body_lines, "event_id", event_id)
    _append_line(body_lines, "event_name", event_name)
    _append_line(body_lines, "status", status)

    origin = f"capy-space://{space_id}/widget/{widget_id or 'widget'}/event/{event_id or _sha256(event_name)[:12]}"
    return _canonical_artifact_record(
        source_type="space_widget_event",
        space_id=space_id,
        body_title="Space widget event",
        body_lines=body_lines,
        origin_uri=origin,
        dropped_field_count=dropped_field_count,
    )


def canonicalize_visual_qa_report(report: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, metadata-only memory record for a visual QA report."""
    if not isinstance(report, dict):
        raise ValueError("visual QA report must be a mapping")

    dropped_field_count = _scan_for_unsafe(report)
    space_id, dropped = _safe_public_id_with_drop(report.get("space_id") or report.get("spaceId"), fallback="space")
    dropped_field_count += dropped
    surface, dropped = _safe_public_text_with_drop(report.get("surface") or report.get("title"), limit=200, fallback="Visual QA")
    dropped_field_count += dropped
    status, dropped = _safe_public_text_with_drop(report.get("status") or report.get("result"), limit=80, fallback="unknown")
    dropped_field_count += dropped
    raw_screenshot_path = report.get("screenshot_path")
    screenshot_value = ntpath.basename(raw_screenshot_path) if isinstance(raw_screenshot_path, str) else raw_screenshot_path
    screenshot_name, dropped = _safe_public_text_with_drop(screenshot_value, limit=240)
    dropped_field_count += dropped

    body_lines: list[str] = ["## Visual QA metadata"]
    _append_line(body_lines, "space_id", space_id)
    _append_line(body_lines, "surface", surface)
    _append_line(body_lines, "status", status)
    _append_line(body_lines, "screenshot", screenshot_name)

    raw_findings = report.get("findings")
    if isinstance(raw_findings, list):
        findings: list[Any] = raw_findings
    else:
        findings = []
        if _is_present_public_value(raw_findings):
            dropped_field_count += 1
    safe_findings: list[str] = []
    for finding in findings[:10]:
        safe = _safe_public_text(finding, limit=300)
        if safe:
            safe_findings.append(safe)
        else:
            dropped_field_count += 1
    if len(findings) > 10:
        dropped_field_count += len(findings) - 10
    if safe_findings:
        body_lines.extend(["", "## Findings"])
        body_lines.extend(f"- {finding}" for finding in safe_findings)

    origin = f"capy-space://{space_id}/visual-qa/{_sha256(surface + status + screenshot_name)[:12]}"
    return _canonical_artifact_record(
        source_type="visual_qa_report",
        space_id=space_id,
        body_title="Visual QA report",
        body_lines=body_lines,
        origin_uri=origin,
        dropped_field_count=dropped_field_count,
    )


def _append_line(lines: list[str], label: str, value: Any) -> None:
    safe = _safe_text(value)
    if safe:
        lines.append(f"- {label}: {safe}")


def canonicalize_space_manifest(space: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, metadata-only memory record for a Space manifest."""
    if not isinstance(space, dict):
        raise ValueError("space manifest must be a mapping")

    dropped_field_count = _scan_for_unsafe(space)
    space_id, dropped = _safe_manifest_public_id_with_drop(space.get("space_id") or space.get("id"), fallback="space")
    dropped_field_count += dropped
    name, dropped = _safe_manifest_public_text_with_drop(space.get("name") or space_id, limit=200, fallback=space_id)
    dropped_field_count += dropped
    description, dropped = _safe_manifest_public_text_with_drop(space.get("description"), limit=700)
    dropped_field_count += dropped
    template, dropped = _safe_manifest_public_text_with_drop(space.get("template"), limit=160)
    dropped_field_count += dropped
    revision, dropped = _safe_manifest_public_id_with_drop(space.get("revision_event_id"), fallback="")
    dropped_field_count += dropped

    body_lines: list[str] = [f"# {name}", "", "## Space metadata"]
    _append_line(body_lines, "space_id", space_id)
    _append_line(body_lines, "name", name)
    _append_line(body_lines, "description", description)
    _append_line(body_lines, "template", template)
    if space.get("metadata_only") is not None:
        body_lines.append(f"- metadata_only: {bool(space.get('metadata_only'))}")
    _append_line(body_lines, "revision_event_id", revision)

    widgets = space.get("widgets") if isinstance(space.get("widgets"), list) else []
    if widgets:
        body_lines.extend(["", "## Widgets"])
        for raw_widget in widgets[:50]:
            if not isinstance(raw_widget, dict):
                dropped_field_count += 1
                continue
            widget_id, dropped = _safe_manifest_public_id_with_drop(raw_widget.get("id"), fallback="widget")
            dropped_field_count += dropped
            title, dropped = _safe_manifest_public_text_with_drop(raw_widget.get("title"), limit=200, fallback=widget_id)
            dropped_field_count += dropped
            kind, dropped = _safe_manifest_public_text_with_drop(raw_widget.get("kind"), limit=120)
            dropped_field_count += dropped
            parts = [widget_id, title]
            if kind:
                parts.append(kind)
            body_lines.append("- " + " | ".join(parts))

    body = "\n".join(body_lines).strip() + "\n"
    content_sha256 = _sha256(body)
    source_id_seed = f"space_manifest:{space_id}:{content_sha256}"
    source_id = "cmt-src-" + _sha256(source_id_seed)[:24]
    chunk_id = "cmt-chunk-" + _sha256(f"{source_id}:0:{content_sha256}")[:24]
    redaction_status = "dropped_fields" if dropped_field_count else "none"
    frontmatter = _frontmatter(
        {
            "source_id": source_id,
            "source_type": "space_manifest",
            "origin_uri": f"capy-space://{space_id}",
            "space_id": space_id,
            "content_sha256": content_sha256,
            "redaction_status": redaction_status,
        }
    )
    markdown = frontmatter + "\n\n" + body

    return {
        "source_id": source_id,
        "chunk_id": chunk_id,
        "source_type": "space_manifest",
        "origin_uri": f"capy-space://{space_id}",
        "space_id": space_id,
        "content_sha256": content_sha256,
        "redaction_status": redaction_status,
        "dropped_field_count": dropped_field_count,
        "markdown": markdown,
    }


__all__ = [
    "canonicalize_space_manifest",
    "canonicalize_space_revision_event",
    "canonicalize_space_widget_event",
    "canonicalize_visual_qa_report",
    "ingest_source",
    "init_memory_tree",
    "list_source_refresh_jobs",
    "memory_status",
    "memory_tree_db_path",
    "memory_tree_root",
    "memory_tree_vault_path",
    "register_local_knowledge_sources",
    "register_source_reference",
    "relevant_memory_for_space",
    "run_source_refresh_jobs",
    "search_memory",
]
