"""Checkpoint-target resolution for the WebUI "Restore checkpoint" feature.

Desktop parity: ``restoreToMessage`` in
apps/desktop/src/app/session/hooks/use-prompt-actions/index.ts rewinds the
conversation to the strict prefix *before* a chosen user turn. The WebUI stores
the transcript twice — a display sidecar (``s.messages`` / ``context_messages``)
and the durable state.db rows — and the two stores drift:

  * legacy sidecar rows carry no ``_row_id`` stamp at all,
  * stamps go stale after a history rewrite (the durable row is archived and a
    fresh copy gets a new id), so a stamped checkpoint can fail to resolve,
  * display-only artifacts exist (repeated/joined rows produced by append-only
    merges) that have no single durable twin.

The durable side of a restore is a *soft archive* of the active rows at/after
the checkpoint (``active=0``, recoverable history — the same contract as the
gateway's ``truncate_before_row_id``). This module resolves, for every user
turn in a displayed transcript, the durable row at which that archive starts.
Resolution is intentionally monotonic and fail-closed:

  1. ``exact`` — the display turn matches an active durable user row by
     normalized content, or by a still-valid ``_row_id`` stamp. The walk over
     display turns advances a single cursor over the durable rows, so repeated
     text ("kontynuuj", re-sent prompts) cannot re-order matches.
  2. ``anchor`` — unmapped turns are bracketed by their nearest mapped
     neighbours and the cut is the first durable row in that window whose
     timestamp is at/after the target's own timestamp (rows before it are
     pre-target history — e.g. the previous turn's reply — and must stay).
  3. ``anchor-next`` — when no row in the window qualifies by timestamp, the
     cut moves to the next mapped turn's durable row (keeps the whole window).
  4. ``display-only`` — nothing durable is attributable to the target or
     later; the restore still truncates the sidecar/model context but archives
     no durable rows.

The module is read-only: it never writes the sidecar or state.db. Failures
degrade to "no plan" (callers fall back to the stamp-only contract).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Strip the WebUI's workspace banner before comparing user text; the banner is
# prepended to the wire form (api_content) while `content` keeps the raw text.
_WORKSPACE_PREFIX_RE = re.compile(r"^\s*\[Workspace::v1:[^\]]*\]\s*", re.IGNORECASE)
# Only the head of a message participates in identity matching — prompts are
# long and their stable prefix is what distinguishes them. 600 chars get
# normalized, 400 are compared (keeps the walk cheap on huge user rows).
_MATCH_INPUT_LIMIT = 600
_MATCH_TEXT_LIMIT = 400
# Tolerance when attributing durable rows to an unmapped display turn by
# timestamp. Sidecar/durable writes for the same message land within the same
# second; 3s covers replay/recovery rewrites without swallowing the previous
# turn's reply rows (those are always older than the target prompt).
_ANCHOR_DRIFT_SECONDS = 3.0

_USER_ROLE = "user"


def _text_value(value: Any) -> str:
    """Flatten a message content value to plain text (mirrors _extract_text)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
        return " ".join(parts)
    if value is None:
        return ""
    return str(value)


def _norm_text(value: Any, *, limit: int = _MATCH_TEXT_LIMIT) -> str:
    """Normalized head of a message text for identity comparison."""
    text = _text_value(value)
    if not text:
        return ""
    text = text[:_MATCH_INPUT_LIMIT]
    text = _WORKSPACE_PREFIX_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_user_message(message: Any) -> bool:
    return isinstance(message, dict) and str(message.get("role") or "").lower() == _USER_ROLE


def _display_stamp(message: dict) -> Optional[int]:
    """Return the durable ``_row_id``-family stamp on a display message."""
    for key in ("_row_id", "_db_persisted_row_id", "row_id"):
        raw = message.get(key)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def load_active_user_rows(session: Any) -> Optional[list[dict]]:
    """Active durable user rows for the session's state.db, oldest first.

    Returns ``None`` when the durable side cannot be read (missing db, no
    messages table) — callers then keep the stamp-only behaviour. Returns
    ``[]`` when the session has no active durable user rows at all.
    """
    sid = getattr(session, "session_id", None)
    if not sid and isinstance(session, str):
        sid = session
    if not sid:
        return None
    profile = getattr(session, "profile", None) or None
    try:
        import sqlite3
        from api.models import _agent_state_db_path
    except Exception:
        return None
    try:
        db_path = _agent_state_db_path(profile=profile)
    except Exception:
        return None
    if not db_path:
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except Exception:
        return None
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(messages)")
        columns = {str(row["name"]) for row in cur.fetchall()}
        if not {"id", "role", "content"} <= columns:
            return None
        select_parts = ["id", "role", "content"]
        select_parts.append("api_content" if "api_content" in columns else "NULL AS api_content")
        select_parts.append("timestamp" if "timestamp" in columns else "NULL AS timestamp")
        where = "session_id=? AND LOWER(COALESCE(role,''))=? "
        if "active" in columns:
            where += "AND (active IS NULL OR active != 0) "
        cur.execute(
            f"SELECT {', '.join(select_parts)} FROM messages WHERE {where}ORDER BY id",
            (str(sid), _USER_ROLE),
        )
        rows: list[dict] = []
        for row in cur.fetchall():
            try:
                row_id = int(row["id"])
            except (TypeError, ValueError):
                continue
            rows.append({
                "id": row_id,
                "role": _USER_ROLE,
                "ts": _as_float(row["timestamp"]),
                "content": row["content"],
                "api_content": row["api_content"],
            })
        return rows
    except Exception as exc:
        logger.debug("checkpoint_map: durable read failed for %s: %s", sid, exc)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _rows_match(display_message: dict, durable_row: dict) -> bool:
    """Content identity between a display user turn and a durable user row.

    Compares every normalized variant pair (raw ``content`` and wire
    ``api_content`` on both sides). Multi-part/None content degrades to a
    role-only match on both-user text absence — callers position matches with
    the monotonic walk, so a role-only match is only accepted in order.
    """
    if not _is_user_message(display_message):
        return False
    if str(durable_row.get("role") or "").lower() != _USER_ROLE:
        return False
    display_texts = []
    for value in (display_message.get("content"), display_message.get("api_content")):
        text = _norm_text(value)
        if text and text not in display_texts:
            display_texts.append(text)
    durable_texts = []
    for value in (durable_row.get("content"), durable_row.get("api_content")):
        text = _norm_text(value)
        if text and text not in durable_texts:
            durable_texts.append(text)
    if not display_texts or not durable_texts:
        # No textual identity available on either side — accept only when
        # both sides are structurally empty (never for real prompts).
        return not display_texts and not durable_texts
    return any(a == b for a in display_texts for b in durable_texts)


def _walk_mappings(display_turns: list[tuple[int, dict]], durable_rows: list[dict]) -> dict[int, int]:
    """Monotonic cursor walk: display index -> durable index (exact matches)."""
    mapping: dict[int, int] = {}
    cursor = 0
    for display_idx, message in display_turns:
        stamp = _display_stamp(message)
        match = None
        if stamp is not None:
            for pos in range(cursor, len(durable_rows)):
                if durable_rows[pos]["id"] == stamp and _rows_match(message, durable_rows[pos]):
                    match = pos
                    break
        if match is None:
            for pos in range(cursor, len(durable_rows)):
                if _rows_match(message, durable_rows[pos]):
                    match = pos
                    break
        if match is not None:
            mapping[display_idx] = match
            cursor = match + 1
    return mapping


def build_restore_plan(session: Any, messages: Optional[list]) -> dict[int, dict]:
    """Resolve every display user turn to its durable checkpoint cut.

    Returns ``{display_index: {"cut_row_id": int|None, "mode": str}}``. A
    missing index means "no durable information at all" (stamp-only fallback
    applies). ``cut_row_id=None`` with mode ``display-only`` means there is
    nothing durable to archive for that target.
    """
    history = [m for m in (messages or []) if isinstance(m, dict)]
    durable_rows = load_active_user_rows(session)
    if durable_rows is None:
        return {}
    display_turns = [(i, m) for i, m in enumerate(history) if _is_user_message(m)]
    if not display_turns:
        return {}
    plan: dict[int, dict] = {}
    if not durable_rows:
        for display_idx, _message in display_turns:
            plan[display_idx] = {"cut_row_id": None, "mode": "display-only"}
        return plan
    mapping = _walk_mappings(display_turns, durable_rows)
    mapped_positions = sorted(mapping.items())
    mapped_cursor = 0
    previous_durable = -1
    for display_idx, message in display_turns:
        while (mapped_cursor < len(mapped_positions)
               and mapped_positions[mapped_cursor][0] < display_idx):
            previous_durable = mapped_positions[mapped_cursor][1]
            mapped_cursor += 1
        if display_idx in mapping:
            plan[display_idx] = {
                "cut_row_id": durable_rows[mapping[display_idx]]["id"],
                "mode": "exact",
            }
            continue
        next_durable = None
        if (mapped_cursor < len(mapped_positions)
                and mapped_positions[mapped_cursor][0] > display_idx):
            next_durable = mapped_positions[mapped_cursor][1]
        window = durable_rows[
            previous_durable + 1:(next_durable if next_durable is not None else len(durable_rows))
        ]
        target_ts = _as_float(message.get("timestamp")) or _as_float(message.get("_ts"))
        cut_row_id = None
        mode = None
        if target_ts is not None:
            for row in window:
                row_ts = row.get("ts")
                if row_ts is not None and row_ts >= target_ts - _ANCHOR_DRIFT_SECONDS:
                    cut_row_id = row["id"]
                    mode = "anchor"
                    break
        if cut_row_id is None and next_durable is not None:
            cut_row_id = durable_rows[next_durable]["id"]
            mode = "anchor-next"
        if cut_row_id is not None:
            plan[display_idx] = {"cut_row_id": cut_row_id, "mode": mode}
        else:
            plan[display_idx] = {"cut_row_id": None, "mode": "display-only"}
    return plan


def annotate_restore_targets(session: Any, full_messages, served_messages=None):
    """Return the served transcript with ``_restore_*`` annotations attached.

    ``full_messages`` is the merged transcript the display indices refer to;
    ``served_messages`` is the (possibly windowed) slice sent to the client.
    Messages are matched by object identity first (window slices share the
    dict objects) and by message id as a fallback. Originals are never
    mutated — annotated rows are shallow copies. Any failure degrades to
    "no annotations", never to a failed session load.
    """
    fallback = served_messages if served_messages is not None else full_messages
    try:
        full = [m for m in (full_messages or []) if isinstance(m, dict)]
        if not full:
            return fallback
        plan = build_restore_plan(session, full)
        if not plan:
            return fallback
        index_by_identity: dict[int, int] = {}
        index_by_message_id: dict[str, int] = {}
        for idx, message in enumerate(full):
            index_by_identity[id(message)] = idx
            mid = message.get("id")
            if mid is not None and _is_user_message(message):
                index_by_message_id.setdefault(str(mid), idx)
        out = []
        for message in (served_messages if served_messages is not None else full):
            if not isinstance(message, dict) or not _is_user_message(message):
                out.append(message)
                continue
            display_idx = index_by_identity.get(id(message))
            if display_idx is None:
                mid = message.get("id")
                if mid is not None:
                    display_idx = index_by_message_id.get(str(mid))
            entry = plan.get(display_idx) if display_idx is not None else None
            if not entry:
                out.append(message)
                continue
            annotated = dict(message)
            annotated["_restore_ready"] = True
            annotated["_restore_mode"] = entry.get("mode") or "unknown"
            if entry.get("cut_row_id") is not None:
                annotated["_restore_row_id"] = int(entry["cut_row_id"])
            out.append(annotated)
        return out
    except Exception as exc:
        logger.debug("checkpoint_map: annotation skipped: %s", exc)
        return fallback
