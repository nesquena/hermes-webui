"""External memory approval-queue helpers for Hermes WebUI.

This module is intentionally provider-oriented. Built-in or custom memory
systems can appear in the WebUI by registering a small SQLite-backed review
queue via ``external_memory_providers.json`` in the active Hermes home.

No provider endpoints, hostnames, IP addresses, or collection names are baked
into the public source tree. Optional indexing settings must come from the
provider registration config, the provider config file, or environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALID_CANDIDATE_STATES = {"candidate", "approved", "rejected"}


class ExternalMemoryError(RuntimeError):
    pass


class ExternalMemoryNotFound(ExternalMemoryError):
    pass


class ExternalMemoryNotConfigured(ExternalMemoryError):
    pass


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    db_path: Path
    config_path: Path | None = None
    kind: str = "custom"
    capabilities: tuple[str, ...] = ("review", "search", "approve", "reject", "delete", "edit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "db_path": str(self.db_path),
            "config_path": str(self.config_path) if self.config_path else "",
            "available": self.db_path.exists() or self.db_path.parent.exists(),
        }


def _safe_provider_id(value: str | None) -> str:
    provider = (value or "").strip().lower().replace("-", "_")
    if not provider or not all(ch.isalnum() or ch == "_" for ch in provider):
        raise ValueError("invalid external memory provider id")
    return provider


def _custom_provider_specs(hermes_home: str | Path) -> list[ProviderSpec]:
    home = Path(hermes_home).expanduser()
    path = home / "external_memory_providers.json"
    if not path.exists():
        return []
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw_items = parsed.get("providers") if isinstance(parsed, dict) else parsed
    if not isinstance(raw_items, list):
        return []
    specs: list[ProviderSpec] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            provider_id = _safe_provider_id(str(item.get("id") or item.get("name") or ""))
        except ValueError:
            continue
        db_raw = str(item.get("db_path") or "").strip()
        if not db_raw:
            continue
        db = Path(db_raw).expanduser()
        if not db.is_absolute():
            db = home / db
        cfg_raw = str(item.get("config_path") or "").strip()
        cfg = Path(cfg_raw).expanduser() if cfg_raw else None
        if cfg and not cfg.is_absolute():
            cfg = home / cfg
        specs.append(
            ProviderSpec(
                id=provider_id,
                label=str(item.get("label") or provider_id.replace("_", " ").title()),
                db_path=db,
                config_path=cfg,
                kind="custom",
            )
        )
    return specs


def provider_specs(hermes_home: str | Path) -> list[ProviderSpec]:
    specs = _custom_provider_specs(hermes_home)
    seen: set[str] = set()
    unique: list[ProviderSpec] = []
    for spec in specs:
        if spec.id in seen:
            continue
        seen.add(spec.id)
        unique.append(spec)
    return unique


def list_providers(hermes_home: str | Path) -> dict[str, Any]:
    providers = [spec.to_dict() for spec in provider_specs(hermes_home)]
    active = providers[0]["id"] if providers else ""
    return {"ok": True, "active": active, "providers": providers}


def get_provider_spec(hermes_home: str | Path, provider: str | None = None) -> ProviderSpec:
    specs = provider_specs(hermes_home)
    if not specs:
        raise ExternalMemoryNotFound("no external memory providers configured")
    provider_id = _safe_provider_id(provider) if provider else specs[0].id
    for spec in specs:
        if spec.id == provider_id:
            return spec
    raise ExternalMemoryNotFound(f"external memory provider not found: {provider_id}")


def load_config(hermes_home: str | Path, provider: str | None = None) -> dict[str, Any]:
    spec = get_provider_spec(hermes_home, provider)
    data: dict[str, Any] = {}
    if spec.config_path and spec.config_path.exists():
        parsed = json.loads(spec.config_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            data.update(parsed)
    env_map = {
        "ollama_url": "HERMES_EXTERNAL_MEMORY_OLLAMA_URL",
        "qdrant_url": "HERMES_EXTERNAL_MEMORY_QDRANT_URL",
        "qdrant_collection": "HERMES_EXTERNAL_MEMORY_QDRANT_COLLECTION",
        "embed_model": "HERMES_EXTERNAL_MEMORY_EMBED_MODEL",
    }
    for key, env_name in env_map.items():
        if not data.get(key) and os.environ.get(env_name):
            data[key] = os.environ[env_name]
    data.setdefault("timeout", 10)
    return data


def _require_indexing_config(cfg: dict[str, Any]) -> None:
    missing = [key for key in ("ollama_url", "embed_model", "qdrant_url", "qdrant_collection") if not cfg.get(key)]
    if missing:
        raise ExternalMemoryNotConfigured(
            "external memory indexing is not configured; missing " + ", ".join(missing)
        )


def ensure_db(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute(
            """
            create table if not exists candidates(
                id text primary key,
                text text not null,
                source text not null default 'agent',
                metadata_json text not null default '{}',
                state text not null default 'candidate',
                content_sha256 text not null,
                created_at real not null,
                updated_at real not null
            )
            """
        )
        con.execute("create index if not exists idx_candidates_state on candidates(state)")
        con.execute("create index if not exists idx_candidates_created_at on candidates(created_at)")
    return path


def _connect(hermes_home: str | Path, provider: str | None = None) -> sqlite3.Connection:
    spec = get_provider_spec(hermes_home, provider)
    path = ensure_db(spec.db_path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _row_to_candidate(row: sqlite3.Row, provider: str) -> dict[str, Any]:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except Exception:
        metadata = {}
    return {
        "provider": provider,
        "id": row["id"],
        "text": row["text"],
        "source": row["source"],
        "metadata": metadata if isinstance(metadata, dict) else {},
        "state": row["state"],
        "content_sha256": row["content_sha256"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _coerce_limit(value: int | str | None, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _coerce_offset(value: int | str | None) -> int:
    try:
        parsed = int(value if value is not None else 0)
    except (TypeError, ValueError):
        parsed = 0
    return max(0, parsed)


def list_candidates(hermes_home: str | Path, *, provider: str | None = None, state: str | None = None, limit: int | str = 100, offset: int | str = 0) -> dict[str, Any]:
    spec = get_provider_spec(hermes_home, provider)
    provider_id = spec.id
    limit = _coerce_limit(limit, default=100, maximum=500)
    offset = _coerce_offset(offset)
    requested_state = (state or "all").strip().lower()
    state_filter = None if requested_state in ("", "all") else requested_state
    if state_filter and state_filter not in VALID_CANDIDATE_STATES:
        raise ValueError("state must be one of: candidate, approved, rejected, all")
    with _connect(hermes_home, provider_id) as con:
        if state_filter:
            rows = con.execute("select * from candidates where state=? order by created_at desc limit ? offset ?", (state_filter, limit, offset)).fetchall()
            total = con.execute("select count(*) from candidates where state=?", (state_filter,)).fetchone()[0]
        else:
            rows = con.execute("select * from candidates order by created_at desc limit ? offset ?", (limit, offset)).fetchall()
            total = con.execute("select count(*) from candidates").fetchone()[0]
    candidates = [_row_to_candidate(r, provider_id) for r in rows]
    return {"ok": True, "provider": provider_id, "state": state_filter or "all", "limit": limit, "offset": offset, "count": len(candidates), "total": total, "candidates": candidates}


def get_candidate(hermes_home: str | Path, candidate_id: str, *, provider: str | None = None) -> dict[str, Any]:
    spec = get_provider_spec(hermes_home, provider)
    provider_id = spec.id
    with _connect(hermes_home, provider_id) as con:
        row = con.execute("select * from candidates where id=?", (candidate_id,)).fetchone()
    return {"ok": True, "provider": provider_id, "candidate": _row_to_candidate(row, provider_id) if row else None}


def _set_candidate_state(hermes_home: str | Path, candidate_id: str, state: str, metadata_update: dict[str, Any] | None = None, *, provider: str | None = None) -> dict[str, Any]:
    spec = get_provider_spec(hermes_home, provider)
    provider_id = spec.id
    current = get_candidate(hermes_home, candidate_id, provider=provider_id)["candidate"]
    if not current:
        raise ExternalMemoryNotFound(f"candidate not found: {candidate_id}")
    metadata = dict(current.get("metadata") or {})
    if metadata_update:
        metadata.update(metadata_update)
    now = time.time()
    with _connect(hermes_home, provider_id) as con:
        con.execute("update candidates set state=?, metadata_json=?, updated_at=? where id=?", (state, json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, candidate_id))
    return get_candidate(hermes_home, candidate_id, provider=provider_id)


def update_candidate_text(hermes_home: str | Path, candidate_id: str, text: str, *, provider: str | None = None) -> dict[str, Any]:
    spec = get_provider_spec(hermes_home, provider)
    provider_id = spec.id
    text = (text or "").strip()
    if not text:
        raise ValueError("text is required")
    current = get_candidate(hermes_home, candidate_id, provider=provider_id)["candidate"]
    if not current:
        raise ExternalMemoryNotFound(f"candidate not found: {candidate_id}")
    if current.get("state") == "approved":
        raise ValueError("approved external memory cannot be edited; create a new candidate instead")
    metadata = dict(current.get("metadata") or {})
    metadata["edited_at"] = time.time()
    now = time.time()
    content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with _connect(hermes_home, provider_id) as con:
        con.execute("update candidates set text=?, content_sha256=?, metadata_json=?, updated_at=? where id=?", (text, content_sha, json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, candidate_id))
    result = get_candidate(hermes_home, candidate_id, provider=provider_id)
    result["ok"] = True
    return result


def reject_candidate(hermes_home: str | Path, candidate_id: str, *, provider: str | None = None, reason: str = "") -> dict[str, Any]:
    result = _set_candidate_state(hermes_home, candidate_id, "rejected", {"review_reason": reason, "reviewed_at": time.time()}, provider=provider)
    result["ok"] = True
    return result


def delete_candidate(hermes_home: str | Path, candidate_id: str, *, provider: str | None = None) -> dict[str, Any]:
    spec = get_provider_spec(hermes_home, provider)
    provider_id = spec.id
    with _connect(hermes_home, provider_id) as con:
        cur = con.execute("delete from candidates where id=?", (candidate_id,))
    if cur.rowcount == 0:
        raise ExternalMemoryNotFound(f"candidate not found: {candidate_id}")
    return {"ok": True, "provider": provider_id, "deleted": candidate_id}


def approve_candidate(hermes_home: str | Path, candidate_id: str, *, provider: str | None = None) -> dict[str, Any]:
    spec = get_provider_spec(hermes_home, provider)
    provider_id = spec.id
    current = get_candidate(hermes_home, candidate_id, provider=provider_id)["candidate"]
    if not current:
        raise ExternalMemoryNotFound(f"candidate not found: {candidate_id}")
    index_result = index_candidate(hermes_home, current, provider=provider_id)
    result = _set_candidate_state(hermes_home, candidate_id, "approved", {"approved_at": time.time(), "qdrant_point_id": index_result.get("point_id", "")}, provider=provider_id)
    result["index"] = index_result
    return result


def _request_json(url: str, payload: dict[str, Any], *, timeout: float, method: str = "POST") -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    parsed = json.loads(raw) if raw else {}
    if not isinstance(parsed, dict):
        raise ExternalMemoryError(f"response from {url} must be a JSON object")
    return parsed


def _post_json(url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    return _request_json(url, payload, timeout=timeout, method="POST")


def embed_text(cfg: dict[str, Any], text: str) -> list[float]:
    _require_indexing_config(cfg)
    base = str(cfg["ollama_url"]).rstrip("/")
    model = str(cfg["embed_model"])
    timeout = float(cfg.get("timeout", 10))
    try:
        response = _post_json(f"{base}/api/embed", {"model": model, "input": text}, timeout=timeout)
        embeddings = response.get("embeddings")
        if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
            return [float(v) for v in embeddings[0]]
    except Exception:
        pass
    response = _post_json(f"{base}/api/embeddings", {"model": model, "prompt": text}, timeout=timeout)
    embedding = response.get("embedding")
    if not isinstance(embedding, list):
        raise ExternalMemoryError("embedding response missing embedding")
    return [float(v) for v in embedding]


def qdrant_upsert(cfg: dict[str, Any], point_id: str, vector: list[float], payload: dict[str, Any]) -> None:
    _require_indexing_config(cfg)
    base = str(cfg["qdrant_url"]).rstrip("/")
    collection = str(cfg["qdrant_collection"])
    timeout = float(cfg.get("timeout", 10))
    _request_json(f"{base}/collections/{collection}/points?wait=true", {"points": [{"id": point_id, "vector": vector, "payload": payload}]}, timeout=timeout, method="PUT")


def index_candidate(hermes_home: str | Path, candidate: dict[str, Any], *, provider: str | None = None) -> dict[str, Any]:
    provider_id = get_provider_spec(hermes_home, provider or candidate.get("provider")).id
    cfg = load_config(hermes_home, provider_id)
    _require_indexing_config(cfg)
    vector = embed_text(cfg, candidate["text"])
    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"external-memory:{provider_id}:{candidate['id']}"))
    metadata = candidate.get("metadata") or {}
    payload = {
        "text": candidate["text"],
        "summary": candidate["text"],
        "state": "active",
        "memory_id": candidate["id"],
        "memory_provider": provider_id,
        "memory_type": metadata.get("type", "external_memory"),
        "source_type": candidate.get("source", "candidate"),
        "source": f"external_memory:{provider_id}",
        "content_sha256": hashlib.sha256(candidate["text"].encode("utf-8")).hexdigest(),
        "metadata": metadata,
    }
    qdrant_upsert(cfg, point_id, vector, payload)
    return {"point_id": point_id}


def search_external_memory(hermes_home: str | Path, query: str, *, provider: str | None = None, limit: int | str = 5) -> dict[str, Any]:
    spec = get_provider_spec(hermes_home, provider)
    provider_id = spec.id
    query = (query or "").strip()
    if not query:
        raise ValueError("q is required")
    limit = _coerce_limit(limit, default=5, maximum=50)
    pattern = f"%{query}%"
    with _connect(hermes_home, provider_id) as con:
        rows = con.execute(
            """
            select * from candidates
            where text like ? or metadata_json like ? or source like ?
            order by case when state='approved' then 0 when state='candidate' then 1 else 2 end,
                     updated_at desc
            limit ?
            """,
            (pattern, pattern, pattern, limit),
        ).fetchall()
    results = [_row_to_candidate(row, provider_id) for row in rows]
    return {"ok": True, "provider": provider_id, "q": query, "limit": limit, "count": len(results), "results": results}
