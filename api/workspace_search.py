"""Safe recursive workspace search endpoint.

GET /api/workspace/search?q=<query>&type=name|content|both&limit=50

Provides search-first workspace navigation for Hermex and mobile users.
Always traverses and returns paths relative to the configured workspace root.
Never follows symlinks outside the workspace root.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

_IGNORED_DIRS = frozenset({
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".next",
    ".turbo",
    "target",
})

_MAX_CONTENT_SEARCH_SIZE = 1 * 1024 * 1024  # 1 MB
_PREVIEW_MAX = 200
_SECRET_KEY_PATTERN = re.compile(
    r"\b(api_key|apikey|token|access_token|refresh_token|password|secret"
    r"|authorization|bearer)\b",
    re.IGNORECASE,
)
_SECRET_KV_PATTERN = re.compile(
    r"(\b(?:api_key|apikey|token|access_token|refresh_token|password|secret"
    r"|authorization|bearer)\s*[=:]\s*)([^\s,;'\"\\]+)",
    re.IGNORECASE,
)


def _redact_preview(line: str) -> str:
    line = _SECRET_KV_PATTERN.sub(r"\1[REDACTED]", line)
    line = _SECRET_KEY_PATTERN.sub("[REDACTED]", line)
    return line


def _is_binary_path(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        if b"\x00" in chunk:
            return True
        return False
    except Exception:
        return True


def _workspace_root():
    try:
        from api.config import DEFAULT_WORKSPACE

        ws = DEFAULT_WORKSPACE
        if ws is None:
            return None
        p = Path(ws).expanduser().resolve()
        if not p.exists() or not p.is_dir():
            return None
        return p
    except Exception:
        return None


def _resolve_search_root():
    root = _workspace_root()
    if root is None:
        return None
    try:
        return root.resolve()
    except (OSError, RuntimeError):
        return root


def _safe_search_path(root: Path, candidate: Path) -> bool:
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _search_name(
    root: Path,
    query_lower: str,
    limit: int,
) -> list[dict]:
    results = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirpath = Path(dirpath)
            if not _safe_search_path(root, dirpath):
                continue
            dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
            for name in filenames:
                if name in _IGNORED_DIRS:
                    continue
                name_lower = name.lower()
                if query_lower in name_lower:
                    try:
                        file_path = dirpath / name
                        if not _safe_search_path(root, file_path):
                            continue
                        rel = file_path.relative_to(root)
                        rel_str = rel.as_posix()
                        is_exact = name_lower == query_lower
                        size = file_path.stat().st_size
                        results.append({
                            "path": rel_str,
                            "type": "file",
                            "match_type": "name",
                            "size": size,
                            "line": None,
                            "preview": rel_str,
                            "_exact": is_exact,
                        })
                        if len(results) >= limit:
                            return results
                    except (OSError, ValueError):
                        continue
            for dirname in dirnames:
                if query_lower in dirname.lower():
                    try:
                        dir_path = dirpath / dirname
                        if not _safe_search_path(root, dir_path):
                            continue
                        rel = dir_path.relative_to(root)
                        rel_str = rel.as_posix()
                        is_exact = dirname.lower() == query_lower
                        results.append({
                            "path": rel_str,
                            "type": "dir",
                            "match_type": "name",
                            "size": None,
                            "line": None,
                            "preview": rel_str,
                            "_exact": is_exact,
                        })
                        if len(results) >= limit:
                            return results
                    except (OSError, ValueError):
                        continue
    except (OSError, PermissionError):
        pass
    return results


def _search_content(
    root: Path,
    query_lower: str,
    limit: int,
) -> list[dict]:
    results = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirpath = Path(dirpath)
            if not _safe_search_path(root, dirpath):
                continue
            dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
            for name in filenames:
                if name in _IGNORED_DIRS:
                    continue
                try:
                    file_path = dirpath / name
                    if not _safe_search_path(root, file_path):
                        continue
                    size = file_path.stat().st_size
                    if size > _MAX_CONTENT_SEARCH_SIZE:
                        continue
                    if _is_binary_path(file_path):
                        continue
                    content = file_path.read_text(errors="ignore")
                    lines = content.split("\n")
                    line_num = None
                    preview = None
                    for idx, line in enumerate(lines, 1):
                        if query_lower in line.lower():
                            line_num = idx
                            raw_preview = line.strip()
                            if len(raw_preview) > _PREVIEW_MAX:
                                raw_preview = raw_preview[:_PREVIEW_MAX] + "..."
                            preview = _redact_preview(raw_preview)
                            break
                    if line_num is not None:
                        rel = file_path.relative_to(root)
                        results.append({
                            "path": rel.as_posix(),
                            "type": "file",
                            "match_type": "content",
                            "size": size,
                            "line": line_num,
                            "preview": preview,
                        })
                        if len(results) >= limit:
                            return results
                except (OSError, UnicodeDecodeError, PermissionError):
                    continue
    except (OSError, PermissionError):
        pass
    return results


def _sort_results(name_results: list[dict], content_results: list[dict]) -> list[dict]:
    name_exact = [r for r in name_results if r.get("_exact")]
    name_inexact = [r for r in name_results if not r.get("_exact")]
    name_exact.sort(key=lambda r: r["path"])
    name_inexact.sort(key=lambda r: r["path"])
    content_results.sort(key=lambda r: r["path"])
    return name_exact + name_inexact + content_results


def handle_workspace_search(handler, parsed):
    from api.helpers import j as json_response, bad

    root = _resolve_search_root()
    if root is None:
        return json_response(
            handler,
            {
                "error": "workspace_unavailable",
                "message": "Workspace root is not configured or unavailable.",
            },
            status=503,
        )

    qs = parse_qs(parsed.query or "")
    q_values = qs.get("q", [])
    q = q_values[0].strip() if q_values else ""
    if not q:
        return bad(handler, "q parameter is required and must be non-empty", status=400)

    type_values = qs.get("type", [])
    search_type = type_values[0].strip().lower() if type_values else "both"
    if search_type not in ("name", "content", "both"):
        return bad(handler, "type must be one of: name, content, both", status=400)

    limit_values = qs.get("limit", [])
    try:
        limit = int(limit_values[0].strip()) if limit_values else 50
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 100))

    q_lower = q.lower()

    name_results = []
    content_results = []

    if search_type in ("name", "both"):
        name_results = _search_name(root, q_lower, limit)
        if search_type == "name":
            sorted_results = _sort_results(name_results, [])
            return json_response(handler, {
                "query": q,
                "type": search_type,
                "limit": limit,
                "results": sorted_results,
            })

    if search_type in ("content", "both"):
        remaining = limit
        if search_type == "both":
            remaining = limit - len(name_results)
        if remaining > 0:
            content_results = _search_content(root, q_lower, remaining)

    sorted_results = _sort_results(name_results, content_results)
    return json_response(handler, {
        "query": q,
        "type": search_type,
        "limit": limit,
        "results": sorted_results,
    })
