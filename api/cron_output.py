"""Shared parsing and presentation projection for persisted cron output."""

from __future__ import annotations

import re


_PREAMBLE = re.compile(r"^# Cron Job:[^\r\n]*\r?$", re.MULTILINE)
_PROMPT = re.compile(r"^## Prompt[ \t]*\r?$", re.MULTILINE)
_HEADING = re.compile(r"^#{1,2} Response[ \t]*\r?$", re.MULTILINE)
_ERROR = re.compile(r"^#{1,2} Error[ \t]*\r?$", re.MULTILINE)
_CRON_JOB_LINE = re.compile(r"^# Cron Job:[^\r\n]*\r?$")
_JOB_ID_LINE = re.compile(r"^\*\*Job ID:\*\*[ \t]*[^\r\n]+\r?$")
_RUN_TIME_LINE = re.compile(r"^\*\*Run Time:\*\*[ \t]*[^\r\n]+\r?$")
_SCHEDULE_LINE = re.compile(r"^\*\*Schedule:\*\*[ \t]*[^\r\n]+\r?$")
_MODE_SCRIPT_LINE = re.compile(r"^\*\*Mode:\*\*[ \t]*no_agent \(script\)[ \t]*\r?$")
_FENCE_LINE = re.compile(r"^(`{3,}|~{3,})(?:[^`~]*)$")
_PRODUCER_HEADER = re.compile(r"^#\s+(?:Cron|Monitor)\s+Job\b", re.IGNORECASE)
_USAGE_METADATA_LINE = re.compile(
    r"^\*\*(?:Provider|Model(?: Used)?|Estimated cost|Cost|Duration|Elapsed|Tokens|Status):\*\*[ \t]*"
)


def _opening_lines(text: str):
    """Yield only top-level metadata lines before artifact content begins."""
    fence_char = None
    fence_length = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        fence = _FENCE_LINE.fullmatch(stripped.strip())
        if fence:
            delimiter = fence.group(1)
            if fence_char is None:
                fence_char, fence_length = delimiter[0], len(delimiter)
            elif delimiter[0] == fence_char and len(delimiter) >= fence_length:
                fence_char = None
                fence_length = 0
            continue
        if fence_char is not None:
            continue
        if stripped == "---" or stripped in ("## Prompt", "## Response", "## Error", "# Response", "# Error"):
            if stripped == "## Prompt":
                yield stripped
            break
        yield stripped


def is_legacy_cron_artifact(text: str) -> bool:
    """Return whether an artifact has no producer-owned cron envelope."""
    lines = list(_opening_lines(text if isinstance(text, str) else str(text or "")))
    return not lines or (
        not _CRON_JOB_LINE.fullmatch(lines[0]) and not _PRODUCER_HEADER.match(lines[0])
    )


def resolve_cron_artifact_mode(text: str, *, legacy_job_mode: str = "unknown") -> str:
    """Resolve one artifact's mode, using the current job only for legacy files."""
    raw = text if isinstance(text, str) else str(text or "")
    lines = list(_opening_lines(raw))
    if not lines or not _CRON_JOB_LINE.fullmatch(lines[0]):
        if lines and _PRODUCER_HEADER.match(lines[0]):
            return "unknown"
        return legacy_job_mode if legacy_job_mode in ("agent", "script") else "unknown"

    positions = {}
    counts = {"job_id": 0, "run_time": 0, "schedule": 0, "mode": 0}
    for index, line in enumerate(lines[1:], 1):
        for name, pattern in (("job_id", _JOB_ID_LINE), ("run_time", _RUN_TIME_LINE),
                              ("schedule", _SCHEDULE_LINE), ("mode", _MODE_SCRIPT_LINE)):
            if pattern.fullmatch(line):
                counts[name] += 1
                positions.setdefault(name, index)
                break

    if all(name in positions for name in ("job_id", "run_time", "mode")):
        if (positions["job_id"] < positions["run_time"] < positions["mode"] and
                positions["mode"] == positions["run_time"] + 1):
            return "script"
    if all(name in positions for name in ("job_id", "run_time", "schedule")) and not any(counts[name] > 1 for name in positions):
        prompt = next((index for index, line in enumerate(lines[1:], 1) if _PROMPT.fullmatch(line)), None)
        if (positions["job_id"] < positions["run_time"] < positions["schedule"] and
                prompt is not None and positions["schedule"] < prompt):
            return "agent"
    return "unknown"


def cron_artifact_metadata_head(text: str) -> str:
    """Return only contiguous producer metadata before arbitrary artifact content."""
    raw = text if isinstance(text, str) else str(text or "")
    metadata = []
    for line in _opening_lines(raw):
        if line in ("## Prompt", "## Response", "# Response", "## Error", "# Error", "---"):
            break
        if not line.strip():
            continue
        if (_CRON_JOB_LINE.fullmatch(line) or _JOB_ID_LINE.fullmatch(line) or
                _RUN_TIME_LINE.fullmatch(line) or _SCHEDULE_LINE.fullmatch(line) or
                _MODE_SCRIPT_LINE.fullmatch(line) or _USAGE_METADATA_LINE.match(line)):
            metadata.append(line)
            continue
        break
    return "\n".join(metadata)


def _top_level_marker_spans(text: str, start: int = 0) -> dict[str, list[tuple[int, int]]]:
    """Collect bounded top-level marker spans in one forward scan."""
    fence_char = None
    fence_length = 0
    spans: dict[str, list[tuple[int, int]]] = {"prompt": [], "response": [], "error": []}
    offset = 0
    for line in text.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        stripped = line.rstrip("\n")
        fence = _FENCE_LINE.fullmatch(stripped.strip())
        if fence:
            delimiter = fence.group(1)
            if fence_char is None:
                fence_char, fence_length = delimiter[0], len(delimiter)
            elif delimiter[0] == fence_char and len(delimiter) >= fence_length:
                fence_char = None
                fence_length = 0
            continue
        if fence_char is not None or line_start < start:
            continue
        for name, pattern in (("prompt", _PROMPT), ("response", _HEADING), ("error", _ERROR)):
            if len(spans[name]) >= 2:
                continue
            match = pattern.fullmatch(stripped)
            if match and not stripped.lstrip().startswith(">"):
                spans[name].append((line_start + match.start(), line_start + match.end()))
    return spans


def parse_cron_output_artifact(text: str, *, job_mode: str = "unknown", legacy: bool = False) -> dict:
    """Return one fail-closed, raw-preserving projection for a cron artifact."""
    raw = text if isinstance(text, str) else str(text or "")
    base = {"kind": "raw", "response": None, "diagnostics": raw,
            "raw": raw, "fallback_reason": None}
    if job_mode == "script":
        base["fallback_reason"] = "script_mode"
        return base
    if job_mode != "agent":
        base["fallback_reason"] = "unknown_mode"
        return base
    preamble = _PREAMBLE.match(raw)
    marker_spans = _top_level_marker_spans(raw, preamble.end() if preamble else 0)
    prompt_candidates = marker_spans["prompt"]
    prompt = prompt_candidates[0] if len(prompt_candidates) == 1 else None
    if not preamble or not prompt or prompt[0] <= preamble.end():
        if not legacy:
            base["fallback_reason"] = "malformed_preamble"
            return base
        errors = marker_spans["error"]
        if errors:
            base["fallback_reason"] = "error_output"
            return base
        candidates = marker_spans["response"]
        if len(candidates) != 1:
            base["fallback_reason"] = "missing_marker" if not candidates else "ambiguous_marker"
            return base
        marker_start, marker_end = candidates[0]
        response = raw[marker_end:].lstrip("\r\n")
        if not response:
            base["fallback_reason"] = "empty_response"
            return base
        return {"kind": "agent", "response": response,
                "diagnostics": raw[:marker_start].rstrip(), "raw": raw,
                "fallback_reason": None}
    errors = marker_spans["error"]
    if errors:
        base["fallback_reason"] = "error_output"
        return base
    candidates = [span for span in marker_spans["response"] if span[0] >= prompt[1]]
    if len(candidates) != 1:
        base["fallback_reason"] = "missing_marker" if not candidates else "ambiguous_marker"
        return base
    marker_start, marker_end = candidates[0]
    response = raw[marker_end:].lstrip("\r\n")
    if not response:
        base["fallback_reason"] = "empty_response"
        return base
    return {"kind": "agent", "response": response,
            "diagnostics": raw[:marker_start].rstrip(), "raw": raw,
            "fallback_reason": None}


def bounded_cron_projection(projection: dict, limit: int) -> dict:
    """Bound display fields while retaining the exact raw artifact."""
    result = dict(projection)
    for field in ("response", "diagnostics"):
        value = result.get(field)
        if isinstance(value, str) and limit >= 0 and len(value) > limit:
            result[field] = value[:limit]
            result[f"{field}_truncated"] = True
        else:
            result[f"{field}_truncated"] = False
    return result
