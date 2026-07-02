"""History prompt suggestions for the WebUI composer.

Scans WebUI session JSON files for past user messages and returns
matches based on partial text input. Uses a simple LRU cache so
repeated queries don't re-scan the full session directory every time.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_SUGGESTIONS = 8
_MIN_QUERY_LENGTH = 1
_CACHE_TTL_SECONDS = 30  # rebuild cache every 30s at most

# ── In-memory cache ──────────────────────────────────────────────────────────
_cache: dict[str, Any] = {
    "user_messages": [],       # list of {"text": str, "session_id": str, "session_title": str, "ts": float}
    "built_at": 0.0,
    "session_dir_mtime": 0.0,
}


def _session_dir_mtime(session_dir: Path) -> float:
    """Return the latest mtime across all session JSON files."""
    latest = 0.0
    if not session_dir.exists():
        return latest
    try:
        for p in session_dir.glob("*.json"):
            if p.name == "_index.json":
                continue
            try:
                m = p.stat().st_mtime
                if m > latest:
                    latest = m
            except OSError:
                continue
    except Exception:
        pass
    return latest


def _rebuild_cache(session_dir: Path) -> None:
    """Scan all session JSON files and extract user messages."""
    messages: list[dict[str, Any]] = []
    if not session_dir.exists():
        _cache["user_messages"] = messages
        _cache["built_at"] = time.time()
        _cache["session_dir_mtime"] = _session_dir_mtime(session_dir)
        return

    seen: set[str] = set()
    for p in sorted(session_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.name == "_index.json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue

        session_id = str(data.get("session_id") or p.stem)
        session_title = str(data.get("title") or "Untitled")
        raw_messages = data.get("messages") or []
        if not isinstance(raw_messages, list):
            continue

        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            if role != "user":
                continue
            content = msg.get("content", "")
            if not content or not isinstance(content, str):
                continue
            text = content.strip()
            if not text or len(text) < 2:
                continue

            # Deduplicate identical messages across sessions
            dedup_key = text.lower()[:120]
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            ts = 0.0
            try:
                ts = float(msg.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                ts = 0.0

            messages.append({
                "text": text,
                "session_id": session_id,
                "session_title": session_title,
                "ts": ts,
            })

    _cache["user_messages"] = messages
    _cache["built_at"] = time.time()
    _cache["session_dir_mtime"] = _session_dir_mtime(session_dir)
    logger.debug(
        "History suggestions cache rebuilt: %d messages from %s",
        len(messages),
        session_dir,
    )


def _ensure_cache(session_dir: Path) -> None:
    """Rebuild cache if stale or missing."""
    now = time.time()
    if _cache["built_at"] and (now - _cache["built_at"]) < _CACHE_TTL_SECONDS:
        # TTL not expired; check if files changed
        current_mtime = _session_dir_mtime(session_dir)
        if current_mtime <= _cache["session_dir_mtime"]:
            return
    _rebuild_cache(session_dir)


def get_suggestions(
    query: str,
    session_dir: Path,
    max_results: int = _MAX_SUGGESTIONS,
) -> list[dict[str, Any]]:
    """Return up to ``max_results`` user-message suggestions matching ``query``.

    Results are sorted by relevance (prefix match > substring match > fuzzy)
    then by recency.
    """
    q = query.strip().lower()
    if not q or len(q) < _MIN_QUERY_LENGTH:
        return []

    _ensure_cache(session_dir)

    scored: list[tuple[int, float, dict[str, Any]]] = []

    for msg in _cache["user_messages"]:
        text_lower = msg["text"].lower()

        if q == text_lower:
            # Exact match — highest priority
            score = 100
        elif text_lower.startswith(q):
            # Prefix match — high priority
            score = 80
        elif q in text_lower:
            # Substring match
            score = 40
        else:
            # Simple word-level matching
            q_words = set(q.split())
            text_words = set(text_lower.split())
            common = q_words & text_words
            if common:
                score = 10 + len(common) * 5
            else:
                continue

        scored.append((score, msg["ts"], msg))

    # Sort by score descending, then by timestamp descending
    scored.sort(key=lambda x: (-x[0], -x[1]))

    results = []
    seen_texts: set[str] = set()
    for _score, _ts, msg in scored:
        dedup = msg["text"].lower()[:200]
        if dedup in seen_texts:
            continue
        seen_texts.add(dedup)
        results.append({
            "text": msg["text"][:300],
            "session_id": msg["session_id"],
            "session_title": msg["session_title"],
        })
        if len(results) >= max_results:
            break

    return results
