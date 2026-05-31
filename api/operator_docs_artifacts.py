"""Read-only Docs/Artifact Browser operator payloads.

Slice 7 catalogs fixed, allowlisted local docs/artifact roots and returns
metadata-only list cards. Explicit previews are handled separately and remain
bounded/read-only; this module never executes tools, mutates state, dispatches
work, or fabricates demo items when sources are missing.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PAYLOAD_VERSION = 1
MODE = "docs-artifacts-read-only"
PREVIEW_MODE = "docs-artifacts-preview-read-only"
MAX_LIST_ITEMS = 50
MAX_PREVIEW_BYTES = 24000
MAX_SOURCE_BYTES = 200_000
RECENT_SECONDS = 30 * 24 * 60 * 60
SECRET_REDACTION = "[redacted]"

WORKSPACE_ROOT = Path("/mnt/c/Users/malac/.openclaw/workspace/main")
WEBUI_ROOT = Path(__file__).resolve().parents[1]

SOURCE_SPECS: dict[str, dict[str, Any]] = {
    "plans": {
        "type": "directory",
        "label": "Plans / handoffs",
        "path": WORKSPACE_ROOT / ".hermes" / "plans",
        "display_path": ".hermes/plans",
        "includes": [{"glob": "*.md", "kind": "auto"}],
    },
    "deep_research_briefs": {
        "type": "directory",
        "label": "Deep research briefs",
        "path": WORKSPACE_ROOT / "obsidian-vault" / "Agent-Kimi" / "Deep Research Briefs",
        "display_path": "obsidian-vault/Agent-Kimi/Deep Research Briefs",
        "includes": [{"glob": "*.md", "kind": "brief"}, {"glob": "manifest.json", "kind": "artifact_manifest"}],
    },
    "agent_shared_active_plan": {
        "type": "file",
        "label": "Shared active plan",
        "path": WORKSPACE_ROOT / "obsidian-vault" / "Agent-Shared" / "ACTIVE PLAN.md",
        "display_path": "obsidian-vault/Agent-Shared/ACTIVE PLAN.md",
        "kind": "plan",
    },
    "generated_artifacts": {
        "type": "directory",
        "label": "Generated artifacts",
        "path": WORKSPACE_ROOT / "artifacts",
        "display_path": "artifacts",
        "includes": [{"glob": "*/action-summary.json", "kind": "action_summary"}, {"glob": "*/manifest.json", "kind": "artifact_manifest"}],
    },
    "state_summaries": {
        "type": "directory",
        "label": "Selected state summaries",
        "path": WORKSPACE_ROOT / "state",
        "display_path": "state",
        "includes": [
            {"path": "hermes_reverse_prompt_latest.json", "kind": "state_summary"},
            {"path": "hermes_youtube_recommendations_2026-05-20.json", "kind": "state_summary"},
            {"path": "hermes_operator_kanban_hardening_2026-05-20.json", "kind": "state_summary"},
        ],
    },
    "webui_changelog": {
        "type": "file",
        "label": "WebUI changelog",
        "path": WEBUI_ROOT / "CHANGELOG.md",
        "display_path": "CHANGELOG.md",
        "kind": "changelog",
    },
}

EXCLUDED_PARTS = {
    "raw",
    "_recovered_historical_workspace_main_20260521t032323z",
    ".obsidian",
    "memory",
    "memory-ledger",
    "sessions",
    "attachments",
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".cache",
    "build",
    "dist",
    ".next",
}

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b[A-Za-z0-9_-]*(?:password|passwd|pwd|api[_-]?key|token|access[_-]?token|refresh[_-]?token|secret)\b\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|xox[baprs]?)-[A-Za-z0-9._/=-]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{12,}(?![A-Za-z0-9_])", re.IGNORECASE),
)
_SECRET_NAME_PATTERN = re.compile(
    r"(?:password|passwd|pwd|api[_-]?key|token|access[_-]?token|refresh[_-]?token|secret)",
    re.IGNORECASE,
)


def build_operator_docs_artifacts_payload(
    query_text: Any = "",
    kind: Any = "all",
    root: Any = "all",
    limit: Any = MAX_LIST_ITEMS,
    now: float | None = None,
) -> dict[str, Any]:
    """Build a versioned read-only docs/artifacts list payload."""

    generated_at = float(time.time() if now is None else now)
    normalized_query = _clean_text(query_text)
    normalized_kind = _clean_filter(kind, default="all")
    normalized_root = _clean_filter(root, default="all")
    normalized_limit = _coerce_int_clamped(limit, default=MAX_LIST_ITEMS, minimum=1, maximum=100)
    query = {
        "text": normalized_query,
        "kind": normalized_kind,
        "root": normalized_root,
        "limit": normalized_limit,
    }

    source_specs = SOURCE_SPECS if isinstance(SOURCE_SPECS, Mapping) else {}
    if not source_specs:
        return _payload(
            generated_at=generated_at,
            status="unknown",
            summary="Docs/artifacts browser has no configured sources; no items were fabricated.",
            query=query,
            sources=[],
            items=[],
            issues=["docs/artifacts source registry is empty or unavailable"],
        )

    all_items: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    issues: list[str] = []
    for root_id, raw_spec in source_specs.items():
        source, items = _catalog_source(str(root_id), raw_spec, now=generated_at)
        sources.append(source)
        all_items.extend(items)
        if source.get("issue"):
            issues.append(f"{source['id']}: {source['issue']}")
        for item in items:
            issues.extend(f"{item['root_id']}/{item['relative_path']}: {issue}" for issue in item.get("issues", []))

    filtered_items = _filter_items(
        all_items,
        query_text=normalized_query,
        kind=normalized_kind,
        root=normalized_root,
    )
    filtered_items = sorted(filtered_items, key=lambda item: (-float(item.get("mtime") or 0.0), item.get("display_path", "")))
    filtered_items = filtered_items[:normalized_limit]

    status = _catalog_status(sources, all_items)
    source_count = sum(1 for source in sources if source.get("state") != "unknown" and source.get("count", 0) > 0)
    if filtered_items:
        summary = f"{len(filtered_items)} docs/artifacts from {source_count} sources"
    elif issues:
        summary = f"0 docs/artifacts from configured roots; {len(issues)} source issue{'s' if len(issues) != 1 else ''}"
    else:
        summary = "0 docs/artifacts from configured roots"

    return _payload(
        generated_at=generated_at,
        status=status,
        summary=summary,
        query=query,
        sources=sources,
        items=filtered_items,
        issues=issues,
    )


def build_operator_docs_artifact_preview_payload(item_id: Any, now: float | None = None) -> dict[str, Any]:
    """Build a read-only preview payload for an opaque catalog item id."""

    generated_at = float(time.time() if now is None else now)
    raw_item_id = "" if item_id is None else str(item_id)
    normalized_item_id = _clean_text(item_id)
    if _is_suspicious_item_id(raw_item_id):
        return _preview_payload(
            generated_at=generated_at,
            status="unknown",
            item=_unknown_preview_item("[rejected]"),
            preview=_empty_preview(),
            issues=["rejected malformed docs/artifacts item id"],
        )

    if not normalized_item_id:
        return _preview_payload(
            generated_at=generated_at,
            status="unknown",
            item=_unknown_preview_item(""),
            preview=_empty_preview(),
            issues=["missing or unknown docs/artifacts item id"],
        )

    target = _find_preview_target(normalized_item_id, now=generated_at)
    if target is None:
        return _preview_payload(
            generated_at=generated_at,
            status="unknown",
            item=_unknown_preview_item(normalized_item_id),
            preview=_empty_preview(),
            issues=[f"missing or unknown docs/artifacts item id: {normalized_item_id}"],
        )

    item, resolved_path, root = target
    if not item.get("preview_available"):
        size = _coerce_int_clamped(item.get("size_bytes"), default=0, minimum=0, maximum=10**15)
        if size > MAX_SOURCE_BYTES:
            issue = f"preview unavailable: source exceeds {MAX_SOURCE_BYTES} byte preview limit"
        else:
            issue = "preview unavailable for this docs/artifacts item"
        return _preview_payload(
            generated_at=generated_at,
            status="unknown",
            item=item,
            preview=_empty_preview(),
            issues=[issue, *item.get("issues", [])],
        )

    try:
        resolved = resolved_path.resolve(strict=True)
        resolved.relative_to(root)
        stat = resolved.stat()
    except Exception as exc:  # pragma: no cover - filesystem race/permission edge
        return _preview_payload(
            generated_at=generated_at,
            status="unknown",
            item=item,
            preview=_empty_preview(),
            issues=[f"preview unavailable: {_short_error(exc)}"],
        )

    suffix = resolved.suffix.lower()
    if stat.st_size > MAX_SOURCE_BYTES:
        return _preview_payload(
            generated_at=generated_at,
            status="unknown",
            item={**item, "preview_available": False, "size_bytes": int(stat.st_size)},
            preview=_empty_preview(),
            issues=[f"preview unavailable: source exceeds {MAX_SOURCE_BYTES} byte preview limit"],
        )
    if suffix not in {".md", ".txt", ".json"}:
        return _preview_payload(
            generated_at=generated_at,
            status="unknown",
            item=item,
            preview=_empty_preview(),
            issues=["preview unavailable for this file type"],
        )

    try:
        if suffix == ".json":
            preview = _json_preview(resolved, item=item)
        else:
            preview = _text_preview(resolved)
    except json.JSONDecodeError as exc:
        return _preview_payload(
            generated_at=generated_at,
            status="unknown",
            item={**item, "preview_available": False},
            preview=_empty_preview(),
            issues=[f"malformed JSON: {exc.msg}", *item.get("issues", [])],
        )
    except Exception as exc:  # pragma: no cover - filesystem race/permission edge
        return _preview_payload(
            generated_at=generated_at,
            status="unknown",
            item=item,
            preview=_empty_preview(),
            issues=[f"preview unavailable: {_short_error(exc)}"],
        )

    return _preview_payload(
        generated_at=generated_at,
        status="live",
        item=item,
        preview=preview,
        issues=item.get("issues", []),
    )


def _catalog_source(root_id: str, raw_spec: Any, *, now: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spec = raw_spec if isinstance(raw_spec, Mapping) else {}
    source_type = _clean_filter(spec.get("type") or spec.get("kind"), default="directory")
    display_path = _redact_text(_clean_text(spec.get("display_path") or root_id))
    source = {
        "id": _redact_text(_clean_filter(root_id, default="unknown")),
        "kind": "file" if source_type == "file" else "directory",
        "label": _redact_text(_clean_text(spec.get("label") or root_id)),
        "display_path": display_path,
        "state": "unknown",
        "count": 0,
    }

    raw_path = spec.get("path")
    if raw_path is None:
        source["issue"] = "path unavailable"
        return source, []

    path = Path(raw_path)
    include_issues: list[str] = []
    try:
        if source_type == "file":
            if path.is_symlink():
                source["issue"] = "source root symlink rejected"
                return source, []
            if not path.exists():
                source["issue"] = "missing"
                return source, []
            if not path.is_file():
                source["issue"] = "not a regular file"
                return source, []
            root = path.parent.resolve(strict=True)
            item = _item_from_path(root_id, spec, root, path, spec.get("kind") or "artifact", now=now)
            items = [item] if item else []
        else:
            if path.is_symlink():
                source["issue"] = "source root symlink rejected"
                return source, []
            if not path.exists():
                source["issue"] = "missing"
                return source, []
            if not path.is_dir():
                source["issue"] = "not a directory"
                return source, []
            root = path.resolve(strict=True)
            items = []
            included_paths, include_issues = _iter_included_paths(root, spec)
            for candidate, include_kind in included_paths:
                item = _item_from_path(root_id, spec, root, candidate, include_kind, now=now)
                if item:
                    items.append(item)
    except Exception as exc:  # pragma: no cover - platform filesystem edge
        source["issue"] = f"unreadable: {_short_error(exc)}"
        return source, []

    source["count"] = len(items)
    source_has_item_issues = any(item.get("issues") for item in items)
    source["state"] = "unknown" if include_issues or source_has_item_issues or not items else "live"
    if include_issues:
        source["issue"] = "; ".join(_redact_text(issue) for issue in include_issues[:5])
    elif source_has_item_issues:
        source["issue"] = "one or more allowlisted files have metadata issues"
    elif not items:
        source["issue"] = "no matching allowlisted files"
    return source, items


def _iter_included_paths(root: Path, spec: Mapping[str, Any]) -> tuple[list[tuple[Path, str]], list[str]]:
    includes = spec.get("includes")
    if not isinstance(includes, list) or not includes:
        includes = [{"glob": "*", "kind": spec.get("kind") or "artifact"}]
    results: list[tuple[Path, str]] = []
    issues: list[str] = []
    seen: set[Path] = set()
    for include in includes:
        if not isinstance(include, Mapping):
            continue
        include_kind = _clean_filter(include.get("kind"), default=_clean_filter(spec.get("kind"), default="artifact"))
        candidates: list[Path] = []
        exact_include_path = _clean_text(include.get("path")) if include.get("path") is not None else ""
        if exact_include_path:
            candidates = [root / exact_include_path]
        else:
            pattern = _clean_text(include.get("glob") or "*")
            try:
                candidates = sorted(root.glob(pattern))
            except Exception as exc:
                issues.append(f"include glob rejected: {_short_error(exc)}")
                continue
        for candidate in candidates:
            display_candidate = _redact_summary_text(exact_include_path or _safe_relative_label(root, candidate))
            if candidate in seen:
                continue
            if candidate.is_symlink():
                issues.append(f"symlink include rejected: {display_candidate}")
                continue
            if not candidate.exists():
                if exact_include_path:
                    issues.append(f"missing include path: {display_candidate}")
                continue
            if not candidate.is_file():
                if exact_include_path:
                    issues.append(f"include path is not a regular file: {display_candidate}")
                continue
            try:
                rel = candidate.resolve(strict=True).relative_to(root)
            except Exception:
                issues.append(f"include path outside allowlisted root rejected: {display_candidate}")
                continue
            if _is_excluded_relative(rel):
                continue
            seen.add(candidate)
            results.append((candidate, include_kind))
    return results, issues


def _item_from_path(root_id: str, spec: Mapping[str, Any], root: Path, path: Path, include_kind: Any, *, now: float) -> dict[str, Any] | None:
    try:
        resolved = path.resolve(strict=True)
        rel = resolved.relative_to(root)
    except Exception:
        return None
    if _is_excluded_relative(rel):
        return None

    rel_original = rel.as_posix()
    display_base = _clean_text(spec.get("display_path") or root_id)
    if _clean_filter(spec.get("type"), default="directory") == "file":
        rel_original = path.name
        display_original = display_base
    else:
        display_original = f"{display_base.rstrip('/')}/{rel_original}" if display_base else rel_original

    stat = resolved.stat()
    suffix = resolved.suffix.lower()
    kind = _classify_kind(root_id, rel_original, suffix, include_kind)
    metadata, metadata_issues = _metadata_for_file(resolved, suffix)
    issues = [_redact_text(issue) for issue in metadata_issues]
    has_malformed_json = any("malformed json" in issue.casefold() for issue in issues)
    freshness = _freshness_label(stat.st_mtime, now=now)
    if has_malformed_json:
        freshness = {"label": "unknown", "reason": "malformed JSON metadata"}
    relative_path = _redact_text(rel_original)
    display_path = _redact_text(display_original)
    title = _redact_text(_title_from_path(rel_original, kind))
    if relative_path != rel_original:
        title = SECRET_REDACTION

    return {
        "id": _stable_item_id(root_id, rel_original),
        "kind": kind,
        "root_id": _redact_text(root_id),
        "title": title,
        "relative_path": relative_path,
        "display_path": display_path,
        "extension": suffix,
        "size_bytes": int(stat.st_size),
        "mtime": float(stat.st_mtime),
        "freshness": freshness,
        "preview_available": bool(stat.st_size <= MAX_SOURCE_BYTES and suffix in {".md", ".txt", ".json"} and not has_malformed_json),
        "metadata": metadata,
        "issues": issues,
    }


def _metadata_for_file(path: Path, suffix: str) -> tuple[dict[str, Any], list[str]]:
    metadata: dict[str, Any] = {"line_count": 0, "json_keys": [], "ranked_action_count": 0, "avoid_count": 0}
    issues: list[str] = []
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            issues.append(f"source exceeds {MAX_SOURCE_BYTES} byte metadata limit")
            return metadata, issues
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        metadata["line_count"] = len(text.splitlines())
        if suffix == ".json":
            data = json.loads(text)
            if isinstance(data, dict):
                metadata["json_keys"] = sorted(_redact_metadata_label(key) for key in data.keys())[:20]
                metadata["ranked_action_count"] = len(data.get("ranked_actions")) if isinstance(data.get("ranked_actions"), list) else 0
                metadata["avoid_count"] = len(data.get("avoid")) if isinstance(data.get("avoid"), list) else 0
            else:
                issues.append("JSON metadata top-level is not an object")
    except json.JSONDecodeError as exc:
        issues.append(f"malformed JSON: {exc.msg}")
    except Exception as exc:  # pragma: no cover - filesystem race/permission edge
        issues.append(f"unreadable metadata: {_short_error(exc)}")
    return metadata, issues


def _payload(
    *,
    generated_at: float,
    status: str,
    summary: str,
    query: dict[str, Any],
    sources: list[dict[str, Any]],
    items: list[dict[str, Any]],
    issues: list[str],
) -> dict[str, Any]:
    return {
        "version": PAYLOAD_VERSION,
        "mode": MODE,
        "generated_at": generated_at,
        "status": status,
        "summary": summary,
        "query": query,
        "sources": sources,
        "items": items,
        "count": len(items),
        "issues": [_redact_text(issue) for issue in issues],
        "would_execute": False,
    }


def _empty_preview() -> dict[str, Any]:
    return {
        "format": "metadata-only",
        "text": "",
        "truncated": False,
        "bytes_read": 0,
        "max_bytes": MAX_PREVIEW_BYTES,
    }


def _preview_payload(
    *,
    generated_at: float,
    status: str,
    item: dict[str, Any],
    preview: dict[str, Any],
    issues: list[Any],
) -> dict[str, Any]:
    return {
        "version": PAYLOAD_VERSION,
        "mode": PREVIEW_MODE,
        "generated_at": generated_at,
        "status": status,
        "item": item,
        "preview": preview,
        "issues": [_redact_text(issue) for issue in issues],
        "would_execute": False,
    }


def _unknown_preview_item(item_id: Any) -> dict[str, Any]:
    return {
        "id": _redact_text(item_id),
        "root_id": "unknown",
        "relative_path": "",
        "display_path": "",
        "kind": "unknown",
        "title": "",
        "preview_available": False,
    }


def _is_suspicious_item_id(raw_item_id: str) -> bool:
    text = raw_item_id.strip()
    if "\x00" in raw_item_id:
        return True
    if not text:
        return False
    if text.startswith(("/", "\\", "~")):
        return True
    if "://" in text:
        return True
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return True
    return bool(re.search(r"(^|[\\/])\.\.($|[\\/])", text))


def _find_preview_target(item_id: str, *, now: float) -> tuple[dict[str, Any], Path, Path] | None:
    source_specs = SOURCE_SPECS if isinstance(SOURCE_SPECS, Mapping) else {}
    for root_id, raw_spec in source_specs.items():
        target = _preview_target_in_source(str(root_id), raw_spec, item_id, now=now)
        if target is not None:
            return target
    return None


def _preview_target_in_source(root_id: str, raw_spec: Any, item_id: str, *, now: float) -> tuple[dict[str, Any], Path, Path] | None:
    spec = raw_spec if isinstance(raw_spec, Mapping) else {}
    source_type = _clean_filter(spec.get("type") or spec.get("kind"), default="directory")
    raw_path = spec.get("path")
    if raw_path is None:
        return None
    path = Path(raw_path)
    try:
        if source_type == "file":
            if path.is_symlink() or not path.exists() or not path.is_file():
                return None
            root = path.parent.resolve(strict=True)
            return _preview_target_for_candidate(
                root_id,
                spec,
                root,
                path,
                spec.get("kind") or "artifact",
                item_id,
                now=now,
            )
        if path.is_symlink() or not path.exists() or not path.is_dir():
            return None
        root = path.resolve(strict=True)
        included_paths, _include_issues = _iter_included_paths(root, spec)
        for candidate, include_kind in included_paths:
            target = _preview_target_for_candidate(root_id, spec, root, candidate, include_kind, item_id, now=now)
            if target is not None:
                return target
    except Exception:  # pragma: no cover - platform filesystem edge
        return None
    return None


def _preview_target_for_candidate(
    root_id: str,
    spec: Mapping[str, Any],
    root: Path,
    candidate: Path,
    include_kind: Any,
    item_id: str,
    *,
    now: float,
) -> tuple[dict[str, Any], Path, Path] | None:
    try:
        resolved = candidate.resolve(strict=True)
        rel = resolved.relative_to(root)
    except Exception:
        return None
    if _is_excluded_relative(rel):
        return None
    source_type = _clean_filter(spec.get("type"), default="directory")
    relative_for_id = candidate.name if source_type == "file" else rel.as_posix()
    if _stable_item_id(root_id, relative_for_id) != item_id:
        return None
    item = _item_from_path(root_id, spec, root, candidate, include_kind, now=now)
    if item is None or item.get("id") != item_id:
        return None
    return item, resolved, root


def _text_preview(path: Path) -> dict[str, Any]:
    max_bytes = max(0, int(MAX_PREVIEW_BYTES))
    size = int(path.stat().st_size)
    with path.open("rb") as handle:
        raw = handle.read(max_bytes)
    text = raw.decode("utf-8-sig", errors="replace")
    return {
        "format": "text",
        "text": _redact_preview_text(text),
        "truncated": size > len(raw),
        "bytes_read": len(raw),
        "max_bytes": max_bytes,
    }


def _json_preview(path: Path, *, item: Mapping[str, Any]) -> dict[str, Any]:
    max_bytes = max(0, int(MAX_SOURCE_BYTES))
    size = int(path.stat().st_size)
    with path.open("rb") as handle:
        raw = handle.read(max_bytes)
    text = raw.decode("utf-8-sig", errors="replace")
    data = json.loads(text)
    return {
        "format": "json-summary",
        "text": _json_summary_text(data, item=item),
        "truncated": size > len(raw),
        "bytes_read": len(raw),
        "max_bytes": max_bytes,
    }


def _json_summary_text(data: Any, *, item: Mapping[str, Any]) -> str:
    lines: list[str] = []
    kind = _redact_summary_text(item.get("kind") or "json")
    if kind:
        lines.append(f"kind: {kind}")

    if isinstance(data, Mapping):
        keys = [_redact_metadata_label(key) for key in data.keys()]
        if keys:
            shown = sorted(keys)[:20]
            suffix = f" (+{len(keys) - len(shown)} more)" if len(keys) > len(shown) else ""
            lines.append(f"top_level_keys: {', '.join(shown)}{suffix}")
        for key in ("ranked_actions", "avoid", "briefs", "artifacts", "items", "sources", "actions", "recommendations"):
            if key in data:
                lines.append(f"{key}: {_json_count_or_summary(data[key])}")
        for key in (
            "brief_path",
            "artifact_dir",
            "manifest_path",
            "source_path",
            "output_path",
            "workspace_path",
            "workspace",
        ):
            if key in data:
                lines.append(f"{key}: {_json_scalar_summary(data[key])}")
        for key in ("generated_at", "created_at", "updated_at", "status", "summary", "title"):
            if key in data:
                lines.append(f"{key}: {_json_scalar_summary(data[key])}")
    elif isinstance(data, list):
        lines.append(f"json_array: {len(data)}")
    else:
        lines.append(f"json_value: {_json_scalar_summary(data)}")

    return _redact_summary_text("\n".join(lines))


def _json_count_or_summary(value: Any) -> str:
    if isinstance(value, list):
        return str(len(value))
    if isinstance(value, Mapping):
        return f"{len(value)} keys"
    return _json_scalar_summary(value)


def _json_scalar_summary(value: Any) -> str:
    if isinstance(value, str):
        redacted = _redact_summary_text(value)
        if redacted != value:
            return redacted
        if len(value) > 120:
            return f"string ({len(value)} chars)"
        return redacted
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, list):
        return f"{len(value)} items"
    if isinstance(value, Mapping):
        return f"{len(value)} keys"
    return _redact_summary_text(type(value).__name__)


def _redact_summary_text(value: Any) -> str:
    text = _redact_text(value)
    text = re.sub(r"\\\\[^\r\n]+", "[path]", text)
    text = re.sub(r"\b[A-Za-z]:[\\/][^\r\n]+", "[path]", text)
    text = re.sub(r"(?<![\w:])/[^\r\n]+", "[path]", text)
    return text


def _redact_preview_text(value: Any) -> str:
    return _redact_summary_text(value)


def _redact_metadata_label(value: Any) -> str:
    text = _redact_summary_text(value)
    if _SECRET_NAME_PATTERN.search(text):
        return SECRET_REDACTION
    return text


def _filter_items(items: list[dict[str, Any]], *, query_text: str, kind: str, root: str) -> list[dict[str, Any]]:
    query_fold = query_text.casefold()
    filtered: list[dict[str, Any]] = []
    for item in items:
        if kind != "all" and item.get("kind") != kind:
            continue
        if root != "all" and item.get("root_id") != root:
            continue
        if query_fold:
            haystack = " ".join(str(item.get(key, "")) for key in ("title", "relative_path", "display_path", "root_id", "kind")).casefold()
            if query_fold not in haystack:
                continue
        filtered.append(item)
    return filtered


def _safe_relative_label(root: Path, candidate: Path) -> str:
    try:
        return candidate.relative_to(root).as_posix()
    except Exception:
        return candidate.name


def _is_excluded_relative(rel: Path) -> bool:
    parts = {part.casefold() for part in rel.parts}
    return any(part in EXCLUDED_PARTS for part in parts)


def _classify_kind(root_id: str, relative_path: str, suffix: str, include_kind: Any) -> str:
    raw_kind = _clean_filter(include_kind, default="artifact")
    if raw_kind != "auto":
        return raw_kind
    lower = relative_path.casefold()
    if root_id == "plans":
        if "execution-strategy" in lower or "meta" in lower:
            return "meta_plan"
        if "handoff" in lower or "slice-" in lower:
            return "handoff"
        return "plan"
    if suffix == ".md":
        return "brief"
    if suffix == ".json":
        return "artifact_manifest"
    return "artifact"


def _title_from_path(relative_path: str, kind: str) -> str:
    name = Path(relative_path).name
    stem = name[: -len(Path(name).suffix)] if Path(name).suffix else name
    stem = re.sub(r"^20\d{2}-\d{2}-\d{2}[_-]\d{6}[_-]?", "", stem)
    title = stem.replace("_", " ").replace("-", " ").strip()
    if not title:
        title = kind.replace("_", " ")
    if title.isupper():
        return title
    return " ".join(word.capitalize() if word else word for word in title.split())


def _freshness_label(mtime: float | None, *, now: float) -> dict[str, str]:
    if not mtime:
        return {"label": "unknown", "reason": "missing timestamp"}
    age = max(0.0, now - float(mtime))
    if age <= RECENT_SECONDS:
        return {"label": "current", "reason": "file mtime under 30 days old"}
    return {"label": "historical", "reason": "file mtime older than 30 days"}


def _catalog_status(sources: list[dict[str, Any]], items: list[dict[str, Any]]) -> str:
    if any(source.get("state") == "unknown" for source in sources):
        return "unknown"
    if items:
        return "live"
    return "unknown"


def _stable_item_id(root_id: str, relative_path: str) -> str:
    digest = hashlib.sha256(f"{root_id}\0{relative_path}".encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"da_{digest}"


def _redact_text(value: Any) -> str:
    text = _clean_text(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(SECRET_REDACTION, text)
    return text


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def _clean_filter(value: Any, *, default: str) -> str:
    cleaned = _clean_text(value)
    return cleaned or default


def _coerce_int_clamped(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _short_error(exc: BaseException) -> str:
    text = str(exc)
    text = re.sub(r"\\\\[^'\"]+", "[path]", text)
    text = re.sub(r"[A-Za-z]:[\\/][^'\"]+", "[path]", text)
    text = re.sub(r"/[^'\"]+", "[path]", text)
    return _redact_text(text)[:240]
