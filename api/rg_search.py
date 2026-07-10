"""
Fast session content search: rg for candidate discovery + partial JSON load.

The critical optimization: when depth < message_count, we only parse the first
`depth` messages from each candidate file instead of loading the complete JSON.
This avoids loading multi-megabyte session files just to search the first few messages.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from api.models import SESSION_DIR, all_sessions, get_session
from api.routes import _session_search_message_text, _session_search_preview, _redact_text


# ── Fast partial-JSON loader ───────────────────────────────────────────
# Session JSON format:
#   { "session_id": "...", "title": "...", ..., "messages": [{...}, {...}, ...] }
# We truncate the file after the Nth message and parse only the truncated JSON.

def _load_messages_head(filepath: Path, n: int) -> list[dict]:
    """
    Load only the first `n` messages from a session JSON file.
    Falls back to full load for small files or parsing failures.
    Returns empty list on error.
    """
    if n <= 0:
        return []  # use get_session() for full transcript search

    try:
        raw = filepath.read_text("utf-8")
    except Exception:
        return []

    # Find the start of the messages array
    idx = raw.find('"messages"')
    if idx < 0:
        return []

    arr_start = raw.find("[", idx)
    if arr_start < 0:
        return []

    # Fast path: if n messages will clearly fit within some budget, we can
    # scan forward to find the end of the Nth message. Track brace nesting.
    pos = arr_start + 1
    depth = 0
    obj_count = 0
    in_string = False
    escape = False

    start = pos  # start of current object
    last_good_end = pos  # after last complete object's closing }

    while pos < len(raw):
        ch = raw[pos]

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            pos += 1
            continue

        if ch == '"':
            in_string = True
            pos += 1
            continue

        if ch == "{":
            if depth == 0:
                start = pos
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                obj_count += 1
                last_good_end = pos + 1  # past the closing brace
                if obj_count >= n:
                    break
        elif ch == "[" and depth == 0:
            # Nested array at top level (unlikely but safe)
            depth -= 1  # treat like object for nesting
        elif ch == "]" and depth == 0:
            # End of messages array before we found enough messages
            last_good_end = pos + 1
            break

        pos += 1

    if obj_count == 0:
        return []

    # Build truncated JSON: everything up to the end of the Nth message,
    # then close the messages array and the outer object.
    truncated = raw[:last_good_end] + "]"
    # Find the matching close of the outermost object
    # The raw file should end with "}" - close it properly
    # Actually, the truncated content ends with the Nth message's }
    # and we added "]". We need the whole thing to be valid JSON:
    # { ..., "messages": [{...}, {...}] }
    # The raw[:last_good_end] includes everything up to and including
    # the Nth message's closing brace. We need to close the array + object.
    # But raw[:last_good_end] might already have the metadata part
    # that ends before messages array starts. So the structure is:
    # {..."messages":[ {...first}, {...second} ]}
    # Truncate: {..."messages":[ {...first}, {...second} ]
    # Then append: ]}
    truncated = raw[:last_good_end] + "]}"

    try:
        data = json.loads(truncated)
        return data.get("messages", [])
    except (json.JSONDecodeError, Exception):
        # Fall back to full load on parse failure
        try:
            data = json.loads(raw)
            return data.get("messages", [])
        except Exception:
            return []


def _truncated_session_text(filepath: Path, depth: int, query: str) -> str | None:
    """
    Search first `depth` messages in a session file for `query`.
    Returns the matching message text if found, None otherwise.
    Much faster than Session.load() for depth-limited searches on large files.
    """
    if depth > 0:
        msgs = _load_messages_head(filepath, depth)
    else:
        # Full load for depth=0
        try:
            data = json.loads(filepath.read_text("utf-8"))
            msgs = data.get("messages", [])
        except Exception:
            return None

    for m in msgs:
        c = _session_search_message_text(m)
        if query in str(c).lower():
            return c
    return None


# ── Main search function ──────────────────────────────────────────────

def rg_search_sessions(
    query: str,
    *,
    depth: int = 5,
    content: bool = True,
    all_profiles: bool = False,
    active_profile: str | None = None,
    session_dir: Path | None = None,
    fast_load: bool = True,
) -> list[dict]:
    """
    Search session content using ripgrep + partial JSON loading.

    Args:
        query: Search string (case-insensitive).
        depth: Number of leading messages to scan per session (0 = all).
        content: If False, only title matches.
        all_profiles: Include sessions from all profiles.
        active_profile: Current profile name for filtering.
        session_dir: Override session directory (default: SESSION_DIR).
        fast_load: Use truncated-JSON loading instead of Session.load().
    """
    q = query.lower().strip()
    if not q:
        return []

    # ── Phase 1: Get session metadata ──────────────────────────────────
    sessions = all_sessions()
    if not all_profiles and active_profile:
        sessions = [
            s for s in sessions
            if s.get("profile") == active_profile
        ]

    if not content:
        return [
            dict(s, match_type="title")
            for s in sessions
            if q in (s.get("title") or "").lower()
        ]

    # ── Phase 2: rg candidate discovery ────────────────────────────────
    sd = session_dir or SESSION_DIR
    try:
        proc = subprocess.run(
            ["rg", "-l", "-g", "*.json", "-i", "--", q, str(sd)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 1:
            rg_matched: set[str] = set()
        else:
            rg_matched = {Path(line.strip()).stem for line in proc.stdout.strip().split("\n") if line.strip()}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        rg_matched = {str(s.get("session_id")) for s in sessions if s.get("session_id")}

    # ── Phase 3: Build results ─────────────────────────────────────────
    results: list[dict] = []
    seen_ids: set[str] = set()

    for s in sessions:
        sid = s.get("session_id")
        if not sid:
            continue

        # Title match (always checked)
        if q in (s.get("title") or "").lower():
            item = dict(s, match_type="title")
            results.append(item)
            seen_ids.add(sid)
            continue

        # Only content-search candidates rg found
        if sid not in rg_matched:
            continue

        # Skip sessions with 0 messages (metadata says so)
        msg_count = s.get("message_count") or 0
        if msg_count == 0:
            continue

        try:
            if fast_load and depth > 0 and msg_count > depth:
                # Quick path: parse only first `depth` messages from file
                fpath = sd / f"{sid}.json"
                matched_text = _truncated_session_text(fpath, depth, q)
            else:
                # Legacy path: load full session
                sess = get_session(sid)
                if not sess:
                    continue
                msgs = sess.messages[:depth] if depth else sess.messages
                matched_text = None
                for m in msgs:
                    c = _session_search_message_text(m)
                    if q in str(c).lower():
                        matched_text = c
                        break

            if matched_text is not None:
                item = dict(s, match_type="content")
                preview = _session_search_preview(matched_text, q)
                if preview:
                    item["match_preview"] = _redact_text(preview)
                results.append(item)
        except Exception:
            pass

    return results
