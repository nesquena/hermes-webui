"""Clean-room Capy Memory Tree primitives.

This module stores and exposes only bounded, sanitized summaries. Retrieved
memory is advisory context; it must not bypass Spaces safety gates, prompt
injection checks, approval gates, or rollback/recovery controls.
"""
from __future__ import annotations

import hashlib
import ntpath
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MAX_SCAN_DEPTH = 24
_MAX_SCAN_NODES = 2_000
_MAX_TEXT_LEN = 500

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

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._:-]+")

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
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


def relevant_memory_for_space(space_id: str, *, limit: int = 5) -> dict[str, Any]:
    """Return recent sanitized snippets for one Space."""
    safe_space_id = _safe_text(space_id, limit=160)
    if not safe_space_id:
        raise ValueError("space_id is required")
    limit = max(1, min(int(limit or 5), 25))
    init_memory_tree()
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_SQL)
        rows = conn.execute(
            """
            SELECT s.source_id, c.chunk_id, s.source_type, s.display_name, s.origin_uri,
                   s.space_id, c.summary, c.redaction_status
            FROM chunks c
            JOIN sources s ON s.source_id = c.source_id
            WHERE s.space_id = ?
            ORDER BY s.updated_at DESC, c.updated_at DESC
            LIMIT ?
            """,
            (safe_space_id, limit),
        ).fetchall()
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
    space_id, dropped = _safe_public_id_with_drop(event.get("space_id") or event.get("spaceId"), fallback="space")
    dropped_field_count += dropped
    widget_id, dropped = _safe_public_id_with_drop(event.get("widget_id") or event.get("widgetId") or event.get("id"))
    dropped_field_count += dropped
    event_id, dropped = _safe_public_id_with_drop(event.get("event_id") or event.get("eventId"))
    dropped_field_count += dropped
    event_name, dropped = _safe_public_text_with_drop(event.get("event_name") or event.get("eventName") or event.get("name"), limit=120, fallback="widget.event")
    dropped_field_count += dropped
    status, dropped = _safe_public_text_with_drop(event.get("status"), limit=80, fallback="queued")
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
    space_id = _safe_id(space.get("space_id") or space.get("id"), fallback="space")
    name = _safe_text(space.get("name") or space_id, limit=200) or space_id
    description = _safe_text(space.get("description"), limit=700)
    template = _safe_text(space.get("template"), limit=160)
    revision = _safe_text(space.get("revision_event_id"), limit=160)

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
            widget_id = _safe_id(raw_widget.get("id"), fallback="widget")
            title = _safe_text(raw_widget.get("title"), limit=200) or widget_id
            kind = _safe_text(raw_widget.get("kind"), limit=120)
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
    "memory_status",
    "memory_tree_db_path",
    "memory_tree_root",
    "memory_tree_vault_path",
    "relevant_memory_for_space",
    "search_memory",
]
