"""
Session recovery from .bak snapshots — last line of defense against
data-loss bugs like #1558.

``Session.save()`` writes a ``<sid>.json.bak`` snapshot of the previous
state whenever an incoming save would shrink the messages array. This
module reads those snapshots back and restores any session whose live
file has fewer messages than its backup.

Three integration points:

1. ``recover_all_sessions_on_startup()`` — called from server.py at boot,
   scans the session dir, restores any session whose JSON has fewer
   messages than its .bak. Idempotent: a clean run is a no-op.

2. ``recover_session(sid)`` — single-session helper backing the
   ``POST /api/session/recover`` endpoint, so users can re-run recovery
   manually if their session was open through a server restart.

3. ``inspect_session_recovery_status(sid)`` — read-only audit returning
   message counts for the live JSON, the .bak, and a recommendation.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def _msg_count(p: Path) -> int:
    """Return the number of messages in a session JSON file, or -1 on read/parse error.

    Returns -1 for any non-session-shape file:
    - File can't be read (OSError)
    - Top-level isn't valid JSON or is invalid (JSONDecodeError, ValueError)
    - Top-level isn't a dict (AttributeError on .get) — e.g. ``_index.json``
      which is a top-level list of session metadata, not a session itself.
      The startup recovery scanner globs ``*.json`` and would otherwise
      crash on the first non-dict file it encounters.
    """
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return -1
    if not isinstance(data, dict):
        return -1
    msgs = data.get('messages')
    return len(msgs) if isinstance(msgs, list) else -1


def inspect_session_recovery_status(session_path: Path) -> dict:
    """Return a status dict describing whether recovery is recommended.

    {
      "session_id": "...",
      "live_messages": int,    # -1 if live file unreadable
      "bak_messages": int,     # -1 if no .bak or unreadable
      "recommend": "restore" | "no_action" | "no_backup",
    }
    """
    bak_path = session_path.with_suffix('.json.bak')
    live_count = _msg_count(session_path)
    if not bak_path.exists():
        return {
            "session_id": session_path.stem,
            "live_messages": live_count,
            "bak_messages": -1,
            "recommend": "no_backup",
        }
    bak_count = _msg_count(bak_path)
    if bak_count > live_count:
        return {
            "session_id": session_path.stem,
            "live_messages": live_count,
            "bak_messages": bak_count,
            "recommend": "restore",
        }
    return {
        "session_id": session_path.stem,
        "live_messages": live_count,
        "bak_messages": bak_count,
        "recommend": "no_action",
    }


def recover_session(session_path: Path) -> dict:
    """Restore session_path from its .bak when the bak has more messages.

    Returns a status dict identical to ``inspect_session_recovery_status``
    plus a "restored" boolean.
    """
    status = inspect_session_recovery_status(session_path)
    if status["recommend"] != "restore":
        return {**status, "restored": False}
    bak_path = session_path.with_suffix('.json.bak')
    # Stage the recovery via a tmp copy + atomic replace so a crash mid-restore
    # cannot leave a half-written session.json.
    tmp_path = session_path.with_suffix('.json.recover.tmp')
    try:
        shutil.copyfile(bak_path, tmp_path)
        tmp_path.replace(session_path)
    except OSError as exc:
        logger.warning("recover_session: copy failed for %s: %s", session_path, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {**status, "restored": False, "error": str(exc)}
    logger.warning(
        "recover_session: restored %s from .bak (live=%d → bak=%d messages). "
        "See #1558 for the data-loss class this guards against.",
        session_path.name, status["live_messages"], status["bak_messages"],
    )
    return {**status, "restored": True}


def recover_all_sessions_on_startup(session_dir: Path) -> dict:
    """Scan session_dir for shrunken sessions, restore each from its .bak.

    Returns {"scanned": N, "restored": M, "details": [...]}.
    """
    if not session_dir.exists():
        return {"scanned": 0, "restored": 0, "details": []}
    scanned = 0
    restored = 0
    details: list[dict] = []
    for path in session_dir.glob('*.json'):
        # Skip non-session JSON files in the same dir:
        # - ``_index.json`` is a top-level list of session metadata
        # - any future non-session JSON marked with the ``_`` convention is
        #   skipped automatically (project convention for system files in
        #   directories that otherwise hold user data)
        if path.name.startswith('_'):
            continue
        scanned += 1
        try:
            result = recover_session(path)
        except Exception as exc:
            # Defensive: a malformed session file shouldn't break recovery
            # for the rest. Log and continue.
            logger.warning(
                "recover_all_sessions_on_startup: skipped %s due to %s: %s",
                path.name, type(exc).__name__, exc,
            )
            continue
        if result.get("restored"):
            restored += 1
            details.append(result)
    if restored:
        logger.warning(
            "recover_all_sessions_on_startup: restored %d/%d sessions from .bak. "
            "If you weren't expecting this, check the session list for missing "
            "messages — see #1558.", restored, scanned,
        )
    return {"scanned": scanned, "restored": restored, "details": details}
