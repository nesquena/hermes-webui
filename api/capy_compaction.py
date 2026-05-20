"""Clean-room output compaction helpers for Capy/Hermes context surfaces.

The compactor returns bounded, metadata-only receipts suitable for model context
or UI status cards. It never stores or returns raw unsafe prompt/source/auth
markers; redacted lines are replaced wholesale with ``[REDACTED]``.
"""
from __future__ import annotations

import re
from typing import Any

_MIN_MAX_CHARS = 200
_MAX_MAX_CHARS = 20_000
_DEFAULT_MAX_CHARS = 4_000

_SECRET_SHAPED_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9_-]{10,}|"
    r"ghp_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|"
    r"gho_[A-Za-z0-9]{10,}|ghu_[A-Za-z0-9]{10,}|ghs_[A-Za-z0-9]{10,}|ghr_[A-Za-z0-9]{10,}|"
    r"AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|hf_[A-Za-z0-9]{10,}|SG\.[A-Za-z0-9_-]{10,}"
    r")(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)

_UNSAFE_OUTPUT_RE = re.compile(
    r"SECRET_VALUE_DO_NOT_LEAK|<\s*/?\s*script\b|<[^>]*>.*\bbody\b|"
    r"api[_ -]?key|api[_ -]?auth|access[_ -]?token\s*[:=]|refresh[_ -]?token\s*[:=]|"
    r"client[_ -]?secret\s*[:=]|cookie\s*[:=]|token\s*[:=]|bearer\s*[:=]|"
    r"bearer\s+placeholder|\bbearer\s+[A-Za-z0-9._:-]+|"
    r"\btoken\s+[A-Za-z0-9._:-]+|raw\s+prompt|system\s+prompt|developer\s+prompt|"
    r"prompt\s+injection|ignore\s+previous\s+instructions|renderer|render\s*code|"
    r"generated\s+(?:code|body)|generated[_-]?(?:code|body)|widget\s*body|widgetbody|"
    r"\b(?:source\s+code|source\s*[:=]|data\s+payload|data\s*[:=]|html\s*[:=]|body\s*[:=])|"
    r"/Users/|/home/|/root/|/private/|/var/|/tmp/|/etc/|~/|[A-Za-z]:\\|https?://|://|file:/|"
    r"authorization|credential|password|secret(?!ary)|<[^>]+\bon[a-z]+\s*=",
    re.IGNORECASE,
)

_REPO_PATH_RE = re.compile(r"/Users/bschmidy10/hermes-webui/[^\s:]+")
_ERROR_LINE_RE = re.compile(
    r"\b(?:FAILED|ERROR|Traceback|AssertionError|RuntimeError|Exception|Error:)\b",
    re.IGNORECASE,
)
_APPROVAL_LINE_RE = re.compile(r"approval required|requires approval|confirm before|user approval", re.IGNORECASE)
_SAFE_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,160}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,80}$")
_PATH_OR_URL_RE = re.compile(r"/Users/|/home/|/root/|/private/|/var/|/tmp/|/etc/|~/|[A-Za-z]:\\|https?://|://|file:/", re.IGNORECASE)


def _validate_max_chars(max_chars: int | None) -> int:
    if max_chars is None:
        value = _DEFAULT_MAX_CHARS
    elif isinstance(max_chars, bool):
        raise ValueError("max_chars must be an integer")
    else:
        try:
            value = int(max_chars)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_chars must be an integer") from exc
    if value < _MIN_MAX_CHARS or value > _MAX_MAX_CHARS:
        raise ValueError(f"max_chars must be between {_MIN_MAX_CHARS} and {_MAX_MAX_CHARS}")
    return value


def _redact_lines(text: str) -> tuple[list[str], int]:
    redacted_count = 0
    lines: list[str] = []
    for line in text.splitlines() or [""]:
        if _UNSAFE_OUTPUT_RE.search(line) or _SECRET_SHAPED_RE.search(line):
            lines.append("[REDACTED]")
            redacted_count += 1
        else:
            lines.append(line.rstrip())
    return lines, redacted_count


def _collapse_paths(lines: list[str]) -> tuple[list[str], bool]:
    collapsed: list[str] = []
    changed = False
    for line in lines:
        new_line = _REPO_PATH_RE.sub(".../[REDACTED_PATH]", line)
        changed = changed or new_line != line
        collapsed.append(new_line)
    return collapsed, changed


def _dedupe_consecutive(lines: list[str]) -> tuple[list[str], bool]:
    if not lines:
        return [], False
    result: list[str] = []
    changed = False
    previous = lines[0]
    count = 1
    for line in lines[1:]:
        if line == previous:
            count += 1
            continue
        result.append(f"{previous} (repeated {count}x)" if count > 1 else previous)
        changed = changed or count > 1
        previous = line
        count = 1
    result.append(f"{previous} (repeated {count}x)" if count > 1 else previous)
    changed = changed or count > 1
    return result, changed


def _unique_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _safe_scalar_text(value: Any, *, limit: int = 120, allow_spaces: bool = True) -> str | None:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None
    text = text[:limit]
    if _UNSAFE_OUTPUT_RE.search(text) or _PATH_OR_URL_RE.search(text) or _SECRET_SHAPED_RE.search(text):
        return None
    if not allow_spaces and not _SAFE_TOKEN_RE.match(text):
        return None
    return text


def _safe_artifact_handle_entries(entries: list[Any] | None) -> list[dict[str, str]]:
    if not isinstance(entries, list):
        return []
    retained: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries[:20]:
        if not isinstance(entry, dict):
            continue
        kind = _safe_scalar_text(entry.get("kind"), limit=40, allow_spaces=False)
        raw_handle = re.sub(r"\s+", " ", str(entry.get("handle") or "")).strip()[:160]
        handle = raw_handle if raw_handle and not _UNSAFE_OUTPUT_RE.search(raw_handle) and not _PATH_OR_URL_RE.search(raw_handle) and not _SECRET_SHAPED_RE.search(raw_handle) else None
        label = _safe_scalar_text(entry.get("label"), limit=120, allow_spaces=True)
        if not kind or not handle or not label:
            continue
        if not _SAFE_HANDLE_RE.match(handle) or ".." in handle or "//" in handle or "\\" in handle:
            continue
        item = {"kind": kind, "handle": handle, "label": label}
        marker = (kind, handle, label)
        if marker in seen:
            continue
        seen.add(marker)
        retained.append(item)
        if len(retained) >= 8:
            break
    return retained


def _safe_citation_entries(entries: list[Any] | None) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    retained: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries[:20]:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("citation_id")
        citation_id: int | str | None
        if isinstance(raw_id, bool) or raw_id is None:
            citation_id = None
        elif isinstance(raw_id, int):
            citation_id = raw_id
        else:
            citation_id = _safe_scalar_text(raw_id, limit=40, allow_spaces=False)
        source_type = _safe_scalar_text(entry.get("source_type"), limit=40, allow_spaces=False)
        title = _safe_scalar_text(entry.get("title"), limit=120, allow_spaces=True)
        if citation_id is None or not source_type or not title:
            continue
        item = {"citation_id": citation_id, "source_type": source_type, "title": title}
        marker = (str(citation_id), source_type, title)
        if marker in seen:
            continue
        seen.add(marker)
        retained.append(item)
        if len(retained) >= 8:
            break
    return retained


def _safe_receipt_metadata(value: Any, *, limit: int = 240) -> str | None:
    text = _safe_scalar_text(value, limit=limit, allow_spaces=True)
    if not text:
        return None
    return text


def _bound_lines(lines: list[str], *, required: list[str], max_chars: int) -> str:
    joined = "\n".join(lines).strip()
    if len(joined) <= max_chars:
        return joined

    prefix = ["[output compacted]"]
    required_unique = _unique_keep_order(required)
    selected: list[str] = prefix[:]
    selected.extend(required_unique)

    remaining_budget = max_chars - len("\n".join(selected)) - 40
    if remaining_budget > 0:
        for line in lines:
            if line in required_unique or line in selected:
                continue
            candidate = selected + [line]
            if len("\n".join(candidate)) > max_chars - 25:
                break
            selected.append(line)

    text = "\n".join(selected).strip()
    if len(text) <= max_chars:
        return text

    # Required lines themselves can be huge. Preserve their beginnings rather
    # than dropping exit/error/approval evidence entirely.
    clipped: list[str] = []
    used = 0
    for line in selected:
        budget = max(0, max_chars - used - (1 if clipped else 0))
        if budget <= 0:
            break
        if len(line) > budget:
            clipped.append(line[: max(0, budget - 1)].rstrip() + "…")
            break
        clipped.append(line)
        used += len(line) + (1 if clipped else 0)
    return "\n".join(clipped).strip()


def compact_output(
    output: str,
    *,
    tool: str = "unknown",
    command: str | None = None,
    exit_status: int | None = None,
    max_chars: int | None = None,
    artifact_handles: list[Any] | None = None,
    citations: list[Any] | None = None,
) -> dict[str, Any]:
    """Return a bounded, redacted compaction receipt for tool/browser output.

    The receipt keeps nonzero exit status, first/last error lines, and approval
    prompts visible while trimming noise. ``output`` must be raw text; callers
    should not pass structured objects because stringifying them can expose
    fields the caller intended to keep private.
    """
    if not isinstance(output, str):
        raise ValueError("output must be text")
    limit = _validate_max_chars(max_chars)
    original_chars = len(output)
    rules_applied: list[str] = []

    lines = output.splitlines() or [""]

    lines, collapsed = _collapse_paths(lines)
    if collapsed:
        rules_applied.append("collapse_paths")

    redacted_lines, redacted_count = _redact_lines("\n".join(lines))
    lines = redacted_lines
    if redacted_count:
        rules_applied.append("redact_unsafe_markers")

    lines, deduped = _dedupe_consecutive(lines)
    if deduped:
        rules_applied.append("dedupe_repeated_lines")

    error_lines = [line for line in lines if _ERROR_LINE_RE.search(line)]
    approval_lines = [line for line in lines if _APPROVAL_LINE_RE.search(line)]
    if error_lines or (exit_status not in (None, 0)):
        rules_applied.append("preserve_error_blocks")
    if approval_lines:
        rules_applied.append("preserve_approval_prompts")

    retained_artifact_handles = _safe_artifact_handle_entries(artifact_handles)
    retained_citations = _safe_citation_entries(citations)
    if retained_artifact_handles:
        rules_applied.append("retain_artifact_handles")
    if retained_citations:
        rules_applied.append("retain_citations")

    required: list[str] = []
    if exit_status is not None:
        required.append(f"exit_status: {int(exit_status)}")
    if error_lines:
        required.append(error_lines[0])
        if error_lines[-1] != error_lines[0]:
            required.append(error_lines[-1])
    required.extend(approval_lines)

    candidate_lines = list(lines)
    if required:
        # Put required metadata early so severe failures/prompts survive even
        # when later character bounding removes noisy context.
        candidate_lines = _unique_keep_order(required + candidate_lines)

    candidate_text = "\n".join(candidate_lines).strip()
    if len(candidate_text) > limit:
        rules_applied.append("cap_section_chars")
    text = _bound_lines(candidate_lines, required=required, max_chars=limit)
    compacted_chars = len(text)
    compacted = compacted_chars < original_chars or bool(rules_applied)

    return {
        "tool": _safe_receipt_metadata(tool, limit=80) or "unknown",
        "command": (_safe_receipt_metadata(command, limit=240) or "[REDACTED]") if command is not None else None,
        "exit_status": None if exit_status is None else int(exit_status),
        "original_chars": original_chars,
        "compacted_chars": compacted_chars,
        "compacted": bool(compacted),
        "rules_applied": _unique_keep_order(rules_applied),
        "redaction_status": "redacted" if redacted_count else "none",
        "redacted_count": redacted_count,
        "retained_artifact_handles": retained_artifact_handles,
        "retained_citations": retained_citations,
        "text": text,
    }


__all__ = ["compact_output"]
