"""Clean-room Capy Memory Tree primitives.

This module stores and exposes only bounded, sanitized summaries. Retrieved
memory is advisory context; it must not bypass Spaces safety gates, prompt
injection checks, approval gates, or rollback/recovery controls.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

_MAX_SCAN_DEPTH = 24
_MAX_SCAN_NODES = 2_000
_MAX_TEXT_LEN = 500

_UNSAFE_KEY_RE = re.compile(
    r"renderer|html|script|source|data|code|body|generated[_-]?code|generatedbody|rendercode|widgetbody|"
    r"api[_-]?key|apiauth|authorization|bearer|token|secret|password|credential|"
    r"^on[a-z]+$",
    re.IGNORECASE,
)

_UNSAFE_VALUE_RE = re.compile(
    r"SECRET_VALUE_DO_NOT_LEAK|<\s*/?\s*script\b|bearer\s+placeholder|raw\s+prompt|"
    r"system\s+prompt|developer\s+prompt|prompt\s+injection|ignore\s+previous\s+instructions|"
    r"<[^>]+\bon[a-z]+\s*=",
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
    "init_memory_tree",
    "memory_status",
    "memory_tree_db_path",
    "memory_tree_root",
    "memory_tree_vault_path",
]
