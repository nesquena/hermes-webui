"""Read-only Session Recall operator payload and local sidecar search.

Slice 6 keeps recall manual and proof-bearing: it searches local session JSON
sidecars without invoking agent tools, mutating sessions, or writing memory/skill
state. Results are bounded, redacted, and include enough evidence for later local
promotion flows to remain review-only.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

PAYLOAD_VERSION = 1
MODE = "session-recall-read-only"
_RECENCY_THRESHOLD_SECONDS = 30 * 24 * 60 * 60
_MAX_TITLE_LEN = 160
_SOURCE_FAILURE_ISSUE_MAX_LEN = 240
_SECRET_REDACTION = "[redacted]"

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(password|passwd|pwd|api[_-]?key|token|access[_-]?token|refresh[_-]?token|secret)\b\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|xox[baprs]?)-[A-Za-z0-9._/=-]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{12,}(?![A-Za-z0-9_])", re.IGNORECASE),
)


def build_operator_session_recall_payload(
    query_text: Any,
    limit: Any = 20,
    per_session: Any = 2,
    all_profiles: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Build a read-only Session Recall response payload."""

    generated_at = float(time.time() if now is None else now)
    normalized_limit = _coerce_int_clamped(limit, default=20, minimum=1, maximum=50)
    normalized_per_session = _coerce_int_clamped(per_session, default=2, minimum=1, maximum=5)
    query = _clean_query(query_text)
    query_payload = {
        "text": query,
        "limit": normalized_limit,
        "per_session": normalized_per_session,
        "all_profiles": bool(all_profiles),
    }

    if not query:
        return _payload(
            generated_at=generated_at,
            status="unknown",
            query=query_payload,
            sources=[],
            results=[],
            issues=["query is required for session recall"],
        )

    issues: list[str] = []
    try:
        session_rows = _read_session_recall_sources(all_profiles=bool(all_profiles))
    except Exception as exc:  # Keep source failures honest without fabricating results.
        issue = _source_failure_issue(exc)
        return _payload(
            generated_at=generated_at,
            status="unknown",
            query=query_payload,
            sources=[_unknown_source_descriptor(issue)],
            results=[],
            issues=[issue],
        )
    if not isinstance(session_rows, list):
        session_rows = []

    results = _search_session_rows(
        session_rows,
        query=query,
        limit=normalized_limit,
        per_session=normalized_per_session,
        now=generated_at,
    )

    return _payload(
        generated_at=generated_at,
        status="unknown" if not session_rows else "live",
        query=query_payload,
        sources=_source_descriptors(session_rows),
        results=results,
        issues=issues,
    )


def _read_session_recall_sources(all_profiles: bool = False) -> list[dict[str, Any]]:
    """Return read-only local session rows from WebUI sidecar JSON files.

    Kept deliberately narrow and monkeypatchable. Production reads only bounded
    top-level session JSON sidecars from ``api.config.SESSION_DIR`` and skips
    private/index files. It avoids broader session enumerators or model loaders
    because those paths can reconcile, mutate, or otherwise do more than a
    recall search needs.
    """

    del all_profiles  # Reserved for a later multi-profile reader.
    from api import config

    session_dir = Path(getattr(config, "SESSION_DIR"))
    paths = sorted(
        (p for p in session_dir.glob("*.json") if not p.name.startswith("_")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        messages = data.get("messages")
        rows.append(
            {
                "session_id": data.get("session_id") or path.stem,
                "title": data.get("title"),
                "profile": data.get("profile"),
                "source_label": data.get("source_label") or "WebUI",
                "source_tag": data.get("source_tag") or "webui",
                "updated_at": data.get("updated_at"),
                "last_message_at": data.get("last_message_at"),
                "messages": messages if isinstance(messages, list) else [],
            }
        )
    return rows


def _payload(
    *,
    generated_at: float,
    status: str,
    query: dict[str, Any],
    sources: list[Any],
    results: list[Any],
    issues: list[str],
) -> dict[str, Any]:
    return {
        "version": PAYLOAD_VERSION,
        "mode": MODE,
        "generated_at": generated_at,
        "status": status,
        "query": query,
        "sources": sources,
        "results": results,
        "count": len(results),
        "issues": issues,
        "would_execute": False,
    }


def _search_session_rows(
    rows: list[dict[str, Any]],
    *,
    query: str,
    limit: int,
    per_session: int,
    now: float,
) -> list[dict[str, Any]]:
    query_fold = query.casefold()
    results: list[dict[str, Any]] = []

    for row in rows:
        if len(results) >= limit:
            break
        if not isinstance(row, dict):
            continue
        session = _session_payload(row)
        session_id = session.get("session_id") or ""
        session_match_count = 0

        messages = row.get("messages")
        if isinstance(messages, list):
            for message_index, message in enumerate(messages):
                if session_match_count >= per_session or len(results) >= limit:
                    break
                text = _message_text(message.get("content") if isinstance(message, dict) else message)
                if not text or query_fold not in text.casefold():
                    continue
                role = str(message.get("role") or "unknown") if isinstance(message, dict) else "unknown"
                timestamp = message.get("timestamp") if isinstance(message, dict) else None
                content_hash = _content_hash(text)
                snippet = _bounded_snippet(text, query)
                results.append(
                    _result_payload(
                        session=session,
                        match={
                            "type": "message",
                            "message_index": message_index,
                            "role": role,
                            "timestamp": _coerce_timestamp(timestamp),
                            "content_hash": content_hash,
                            "snippet": snippet,
                        },
                        recency=_recency_label(timestamp, now),
                        source=_source_evidence(
                            session_id=session_id,
                            message_index=message_index,
                            content_hash=content_hash,
                            quote=snippet,
                        ),
                        query=query,
                    )
                )
                session_match_count += 1

    return results


def _result_payload(
    *,
    session: dict[str, Any],
    match: dict[str, Any],
    recency: dict[str, str],
    source: dict[str, Any],
    query: str,
) -> dict[str, Any]:
    return {
        "id": _stable_result_id(
            session.get("session_id"),
            match.get("type"),
            match.get("message_index"),
            match.get("content_hash"),
            query,
        ),
        "session": session,
        "match": match,
        "recency": recency,
        "promotion": {
            "task": {
                "enabled": True,
                "mode": "local_commitment_draft",
                "would_execute": False,
                "source": dict(source),
            },
            "memory_review": {
                "enabled": True,
                "mode": "local_memory_skill_review_proposal",
                "would_execute": False,
                "source_evidence": [dict(source)],
            },
        },
    }


def _source_evidence(
    *,
    session_id: str,
    message_index: Any,
    content_hash: str,
    quote: str,
) -> dict[str, Any]:
    return {
        "kind": "session_message",
        "session_id": session_id,
        "message_index": message_index,
        "content_hash": content_hash,
        "quote": quote,
    }


def _session_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": str(row.get("session_id") or ""),
        "title": _bounded_title(row.get("title")),
        "profile": str(row.get("profile") or "default"),
        "source_label": str(row.get("source_label") or "WebUI"),
        "source_tag": str(row.get("source_tag") or "webui"),
    }


def _source_descriptors(rows: list[Any]) -> list[dict[str, Any]]:
    if not rows:
        return []
    return [
        {
            "id": "webui_sessions",
            "kind": "session_json",
            "state": "live",
            "count": len(rows),
        }
    ]


def _unknown_source_descriptor(issue: str) -> dict[str, Any]:
    return {
        "id": "webui_sessions",
        "kind": "session_json",
        "state": "unknown",
        "issue": issue,
        "count": 0,
    }


def _source_failure_issue(exc: Exception) -> str:
    message = _redact_text(_collapse_ws(str(exc)))
    if message:
        return _bound_issue(f"session recall source unavailable: {type(exc).__name__}: {message}")
    return _bound_issue(f"session recall source unavailable: {type(exc).__name__}")


def _bound_issue(issue: Any, max_len: int = _SOURCE_FAILURE_ISSUE_MAX_LEN) -> str:
    text = _redact_text(_collapse_ws(str(issue)))
    max_len = max(40, int(max_len or _SOURCE_FAILURE_ISSUE_MAX_LEN))
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _clean_query(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def _normalize_query(value: Any) -> str:
    return _clean_query(value)


def _coerce_int_clamped(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    return _coerce_int_clamped(value, default=default, minimum=minimum, maximum=maximum)


def _message_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_message_text(item) for item in value) if part)
    if isinstance(value, dict):
        if "text" in value and (value.get("type") in (None, "text") or isinstance(value.get("text"), str)):
            return _message_text(value.get("text"))
        if "content" in value:
            return _message_text(value.get("content"))
        return ""
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _bounded_snippet(text: Any, query: Any, max_len: int = 280) -> str:
    max_len = max(20, int(max_len or 280))
    normalized = _collapse_ws(_message_text(text))
    query_text = _clean_query(query)
    if not normalized:
        return ""

    redacted = _redact_text(normalized)
    if len(redacted) <= max_len:
        return redacted

    idx = redacted.casefold().find(query_text.casefold()) if query_text else -1
    if idx < 0:
        body = redacted[: max_len - 1]
        return body + "…"

    start = max(0, idx - max_len // 2)
    end = min(len(redacted), start + max_len)
    start = max(0, end - max_len)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(redacted) else ""
    body_len = max_len - len(prefix) - len(suffix)
    body = redacted[start : start + body_len]
    snippet = f"{prefix}{body}{suffix}"
    return snippet if len(snippet) <= max_len else snippet[: max_len - 1] + "…"


def _bounded_title(value: Any, max_len: int = _MAX_TITLE_LEN) -> str:
    title = _collapse_ws(_message_text(value)) or "Untitled"
    if len(title) > max_len:
        title = title[: max_len - 1] + "…"
    return _redact_text(title)


def _collapse_ws(value: str) -> str:
    return " ".join(str(value or "").split())


def _content_hash(text: Any) -> str:
    raw = _message_text(text)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _recency_label(timestamp: Any, now: Any) -> dict[str, str]:
    parsed = _coerce_timestamp(timestamp)
    if parsed is None:
        return {"label": "unknown", "reason": "timestamp missing"}
    try:
        now_ts = float(now)
    except (TypeError, ValueError):
        now_ts = time.time()
    if now_ts - parsed >= _RECENCY_THRESHOLD_SECONDS:
        return {"label": "historical", "reason": "message timestamp is 30+ days old"}
    return {"label": "recent", "reason": "message timestamp is under 30 days old"}


def _coerce_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _redact_text(value: Any) -> str:
    text = str(value or "")

    def replace_key_value(match: re.Match[str]) -> str:
        key = match.group(1)
        return f"{key}={_SECRET_REDACTION}"

    if _SECRET_PATTERNS:
        text = _SECRET_PATTERNS[0].sub(replace_key_value, text)
        for pattern in _SECRET_PATTERNS[1:]:
            text = pattern.sub(_SECRET_REDACTION, text)
    return text


def _stable_result_id(*parts: Any) -> str:
    joined = "\x1f".join("" if part is None else str(part) for part in parts)
    return "sr_" + hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()[:24]
