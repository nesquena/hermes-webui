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

_UNSAFE_OUTPUT_RE = re.compile(
    r"SECRET_VALUE_DO_NOT_LEAK|<\s*/?\s*script\b|<[^>]*>.*\bbody\b|"
    r"api[_ -]?key|api[_ -]?auth|bearer\s+placeholder|\bbearer\s+[A-Za-z0-9._:-]+|"
    r"\btoken\s+[A-Za-z0-9._:-]+|raw\s+prompt|system\s+prompt|developer\s+prompt|"
    r"prompt\s+injection|ignore\s+previous\s+instructions|renderer|render\s*code|"
    r"generated\s+(?:code|body)|generated[_-]?(?:code|body)|widget\s*body|widgetbody|"
    r"\b(?:source\s+code|source\s*[:=]|data\s+payload|data\s*[:=]|html\s*[:=]|body\s*[:=])|"
    r"authorization|credential|password|secret(?!ary)|<[^>]+\bon[a-z]+\s*=",
    re.IGNORECASE,
)

_REPO_PATH_RE = re.compile(r"/Users/bschmidy10/hermes-webui/")
_ERROR_LINE_RE = re.compile(
    r"\b(?:FAILED|ERROR|Traceback|AssertionError|RuntimeError|Exception|Error:)\b",
    re.IGNORECASE,
)
_APPROVAL_LINE_RE = re.compile(r"approval required|requires approval|confirm before|user approval", re.IGNORECASE)


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
        if _UNSAFE_OUTPUT_RE.search(line):
            lines.append("[REDACTED]")
            redacted_count += 1
        else:
            lines.append(line.rstrip())
    return lines, redacted_count


def _collapse_paths(lines: list[str]) -> tuple[list[str], bool]:
    collapsed: list[str] = []
    changed = False
    for line in lines:
        new_line = _REPO_PATH_RE.sub(".../", line)
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

    lines, redacted_count = _redact_lines(output)
    if redacted_count:
        rules_applied.append("redact_unsafe_markers")

    lines, collapsed = _collapse_paths(lines)
    if collapsed:
        rules_applied.append("collapse_paths")

    lines, deduped = _dedupe_consecutive(lines)
    if deduped:
        rules_applied.append("dedupe_repeated_lines")

    error_lines = [line for line in lines if _ERROR_LINE_RE.search(line)]
    approval_lines = [line for line in lines if _APPROVAL_LINE_RE.search(line)]
    if error_lines or (exit_status not in (None, 0)):
        rules_applied.append("preserve_error_blocks")
    if approval_lines:
        rules_applied.append("preserve_approval_prompts")

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
        "tool": str(tool or "unknown")[:80],
        "command": str(command or "")[:240],
        "exit_status": None if exit_status is None else int(exit_status),
        "original_chars": original_chars,
        "compacted_chars": compacted_chars,
        "compacted": bool(compacted),
        "rules_applied": _unique_keep_order(rules_applied),
        "redaction_status": "redacted" if redacted_count else "none",
        "redacted_count": redacted_count,
        "text": text,
    }


__all__ = ["compact_output"]
