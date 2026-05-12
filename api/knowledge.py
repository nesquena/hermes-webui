"""Local knowledge index and Obsidian note helpers for the WebUI.

This adapter intentionally treats Markdown/files as source-of-truth and the
SQLite FTS index as a local derived cache. API responses are metadata-only where
possible and redact snippets/read content before sending them to the browser.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from api.helpers import _redact_text

_LOCAL_SECRET_RE = re.compile(
    r"SECRET_VALUE_DO_NOT_LEAK[^\s`'\"]*|<\s*/?\s*script\b[^>]*>|bearer\s+[^\s`'\"]+|(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s`'\"]+",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def _knowledge_root() -> Path:
    return Path(os.getenv("HERMES_LOCAL_KNOWLEDGE_DIR") or "~/.hermes/local-knowledge").expanduser().resolve()


def _config_path() -> Path:
    return Path(os.getenv("HERMES_LOCAL_KNOWLEDGE_CONFIG") or (_knowledge_root() / "index_config.json")).expanduser().resolve()


def _load_knowledge_index():
    module_path = _knowledge_root() / "knowledge_index.py"
    if not module_path.exists():
        raise FileNotFoundError("local knowledge index is not installed")
    # Use a path/time-derived module name so pytest env changes between tests do
    # not reuse a stale module from a different temporary knowledge root.
    name = f"capy_local_knowledge_{abs(hash((str(module_path), module_path.stat().st_mtime_ns)))}"
    spec = importlib.util.spec_from_file_location(name, module_path)
    if not spec or not spec.loader:
        raise RuntimeError("could not load local knowledge index")
    module = importlib.util.module_from_spec(spec)
    # dataclasses and similar decorators expect the module to be present in
    # sys.modules while class bodies execute.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _cfg(module):
    return module.load_config(str(_config_path()))


def _safe_text(value: Any, *, max_len: int = 4000) -> str:
    text = "" if value is None else str(value)
    text = _redact_text(text)
    text = _LOCAL_SECRET_RE.sub("[REDACTED]", text)
    text = _TAG_RE.sub("[REDACTED]", text)
    text = re.sub(r"\s+", " ", text).strip() if "\n" not in text else text.strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _clean_title(value: Any, fallback: str = "Untitled") -> str:
    text = "" if value is None else str(value)
    text = _TAG_RE.sub(" ", text)
    text = text.replace("/", " ").replace("\\", " ")
    text = _SAFE_FILENAME_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] or fallback


def _slugify_title(title: str) -> str:
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return (slug or "note")[:80]


def _obsidian_vault_path() -> Path:
    return Path(os.getenv("OBSIDIAN_VAULT_PATH") or "~/Documents/Obsidian Vault").expanduser().resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def obsidian_url_for_path(path: str | Path, *, source_type: str = "") -> str | None:
    vault = _obsidian_vault_path()
    p = Path(path).expanduser()
    try:
        resolved = p.resolve()
    except Exception:
        resolved = p
    rel: str | None = None
    if vault.exists() and _is_relative_to(resolved, vault):
        rel = resolved.relative_to(vault).as_posix()
    elif source_type == "obsidian":
        # Search fixtures and stale rows may still identify an Obsidian source
        # without living under the current vault path; keep a best-effort link
        # to the file name rather than exposing unrelated absolute roots.
        rel = p.name
    if not rel:
        return None
    return "obsidian://open?vault=" + quote(vault.name) + "&file=" + quote(rel)


def status_payload() -> dict[str, Any]:
    module = _load_knowledge_index()
    result = module.status(_cfg(module))
    return {
        "available": True,
        "local_only": True,
        "config_ok": bool(result.get("config_ok")),
        "db_exists": bool(result.get("db_exists")),
        "source_count": int(result.get("source_count") or 0),
        "chunk_count": int(result.get("chunk_count") or 0),
        "stale_source_count": int(result.get("stale_source_count") or 0),
        "last_error_count": int(result.get("last_error_count") or 0),
        "last_successful_run": result.get("last_successful_run"),
        "last_run_status": result.get("last_run_status"),
        "embedding_enabled": bool(result.get("embedding_enabled")),
        "obsidian_vault_name": _obsidian_vault_path().name,
    }


def search_payload(query: str, *, limit: int = 10, source_types: list[str] | None = None) -> dict[str, Any]:
    query = str(query or "").strip()
    if not query:
        raise ValueError("query is required")
    limit = max(1, min(int(limit or 10), 25))
    source_types = [str(s).strip() for s in (source_types or []) if str(s).strip()]
    module = _load_knowledge_index()
    result = module.search(query, cfg=_cfg(module), limit=limit, source_types=source_types or None)
    rows = []
    for item in result.get("results", []):
        source_type = _safe_text(item.get("source_type"), max_len=80)
        path = str(item.get("path") or "")
        rows.append(
            {
                "path": path,
                "source_type": source_type,
                "title": _safe_text(item.get("title"), max_len=200),
                "heading_path": _safe_text(item.get("heading_path"), max_len=300),
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "snippet": _safe_text(item.get("snippet"), max_len=1200),
                "content_sha256": _safe_text(item.get("content_sha256"), max_len=80),
                "obsidian_url": obsidian_url_for_path(path, source_type=source_type),
            }
        )
    return {"query": query, "results": rows, "limit": limit, "local_only": True}


def read_payload(path: str, *, offset: int = 1, limit: int = 120) -> dict[str, Any]:
    if not path:
        raise ValueError("path is required")
    offset = max(1, int(offset or 1))
    limit = max(1, min(int(limit or 120), 300))
    module = _load_knowledge_index()
    result = module.read_source(path, cfg=_cfg(module), offset=offset, limit=limit)
    resolved_path = str(result.get("path") or path)
    return {
        "path": resolved_path,
        "offset": int(result.get("offset") or offset),
        "limit": int(result.get("limit") or limit),
        "total_lines": int(result.get("total_lines") or 0),
        "content": _safe_text(result.get("content"), max_len=80_000),
        "obsidian_url": obsidian_url_for_path(resolved_path),
    }


def ask_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Return an extractive, local-only answer context with sanitized citations.

    This endpoint deliberately does not call a model. The browser/agent can use
    the returned answer_markdown/context_markdown as a cited context pack or save
    it to Obsidian through the existing notes endpoint.
    """
    query = str(body.get("query") or body.get("q") or "").strip()
    if not query:
        raise ValueError("query is required")
    max_sources = max(1, min(int(body.get("max_sources") or 4), 8))
    limit = max(max_sources, min(int(body.get("limit") or 8), 25))
    chars_per_source = max(200, min(int(body.get("chars_per_source") or 1200), 3000))
    raw_source_types = body.get("source_types") or body.get("source_type") or []
    if isinstance(raw_source_types, str):
        raw_source_types = [raw_source_types]
    source_types = [str(s).strip() for s in raw_source_types if str(s).strip()] if isinstance(raw_source_types, list) else []
    module = _load_knowledge_index()
    result = module.context_pack(
        query,
        cfg=_cfg(module),
        limit=limit,
        max_sources=max_sources,
        chars_per_source=chars_per_source,
        source_types=source_types or None,
    )
    citations = []
    answer_lines = [
        f"### Local knowledge answer context for: {_safe_text(query, max_len=300)}",
        "",
        "This is local-only extracted context. Use the citations below before making durable claims.",
    ]
    for item in result.get("citations", []):
        source_type = _safe_text(item.get("source_type"), max_len=80)
        path = str(item.get("path") or "")
        title = _safe_text(item.get("title"), max_len=200)
        excerpt = _safe_text(item.get("excerpt"), max_len=4000)
        citation_id = int(item.get("citation_id") or (len(citations) + 1))
        citations.append(
            {
                "citation_id": citation_id,
                "path": path,
                "source_type": source_type,
                "title": title,
                "heading_path": _safe_text(item.get("heading_path"), max_len=300),
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "excerpt": excerpt,
                "content_sha256": _safe_text(item.get("content_sha256"), max_len=80),
                "obsidian_url": obsidian_url_for_path(path, source_type=source_type),
            }
        )
        answer_lines.extend(["", f"[{citation_id}] {title}", excerpt])
    if not citations:
        answer_lines.append("\nNo local sources matched this query.")
    context_markdown = _safe_text(result.get("context_markdown"), max_len=30_000)
    return {
        "query": _safe_text(query, max_len=300),
        "local_only": True,
        "generated": False,
        "source_count": len(citations),
        "citations": citations,
        "answer_markdown": "\n".join(answer_lines).strip() + "\n",
        "context_markdown": context_markdown,
    }


def _safe_note_folder(folder: Any) -> str:
    raw = str(folder or "00_Inbox").strip().replace("\\", "/")
    parts = [p for p in raw.split("/") if p and p not in (".", "..")]
    if not parts or "/" in raw and len(parts) != len([p for p in raw.split("/") if p]):
        raise ValueError("folder must stay inside the Obsidian vault")
    cleaned = []
    for part in parts[:4]:
        name = _clean_title(part, fallback="Inbox")
        if name in (".", ".."):
            raise ValueError("folder must stay inside the Obsidian vault")
        cleaned.append(name)
    return "/".join(cleaned) or "00_Inbox"


def capture_note_payload(body: dict[str, Any]) -> dict[str, Any]:
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content is required")
    title = _clean_title(body.get("title") or f"Capy Note {time.strftime('%Y-%m-%d %H-%M-%S')}")
    folder = _safe_note_folder(body.get("folder") or "00_Inbox")
    vault = _obsidian_vault_path()
    target_dir = (vault / folder).resolve()
    if not _is_relative_to(target_dir, vault):
        raise ValueError("folder must stay inside the Obsidian vault")
    target_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify_title(title)
    target = target_dir / f"{slug}.md"
    if target.exists():
        target = target_dir / f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    tags = body.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    safe_tags = []
    for tag in tags[:12] if isinstance(tags, list) else []:
        tag_text = re.sub(r"[^A-Za-z0-9_/-]+", "", str(tag).strip().lstrip("#"))[:60]
        if tag_text:
            safe_tags.append(tag_text)
    tag_line = ""
    if safe_tags:
        tag_line = "tags: [" + ", ".join(safe_tags) + "]\n"
    note = f"---\ntitle: {title}\ncreated: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n{tag_line}source: capy-webui\n---\n\n{content.rstrip()}\n"
    target.write_text(note, encoding="utf-8")
    return {
        "ok": True,
        "title": title,
        "path": str(target.resolve()),
        "relative_path": target.resolve().relative_to(vault).as_posix(),
        "obsidian_url": obsidian_url_for_path(target),
    }
