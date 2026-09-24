"""Durable -> sidecar resync markers for WebUI history mutations.

The Agent's state.db is the authoritative transcript; the WebUI JSON sidecar
is a display cache published only AFTER a durable commit (checkpoint restore /
per-message delete). When the durable commit succeeds but the sidecar publish
fails (disk error, lock contention), the two stores are split: the on-disk
sidecar still holds the pre-mutation transcript and could serve — and later
re-import — rows the durable side just removed ("resurrect the archived
suffix" from the PR #7075 review).

The marker file ``<session_id>.resync.json`` (next to the sidecar, same
directory, unsafe ids rejected) is the crash-safe compensation record: it
names the committed correction so ANY later sidecar load can re-apply it
before the transcript is served instead of trusting stale data. It is written
atomically and cleared only after a corrected sidecar has been persisted.

Contract:
  * written only AFTER the durable commit landed (never for a refused mutation);
  * carries the exact display correction (index cut for restore, dropped
    display ids for delete) plus the committed row identity sets;
  * consumed by ``Session.load`` via :func:`apply_resync_marker`; a successful
    correction persists the sidecar and clears the marker;
  * best-effort by design: when the marker cannot be written the caller logs
    and raises — the in-memory session still carries ``needs_state_resync``
    and any later successful save persists the corrected state.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

RESYNC_SUFFIX = ".resync.json"


def _session_dir() -> Optional[Path]:
    try:
        import api.models as models
        return Path(models.SESSION_DIR)
    except Exception:
        return None


def resync_marker_path(session_id: Any) -> Optional[Path]:
    """Marker path for *session_id*, or None for unsafe/missing ids."""
    sid = session_id if isinstance(session_id, str) else None
    if not sid:
        return None
    try:
        import api.models as models
        if not models.is_safe_session_id(sid):
            return None
    except Exception:
        return None
    session_dir = _session_dir()
    if session_dir is None:
        return None
    return session_dir / f"{sid}{RESYNC_SUFFIX}"


def _atomic_write_json(path: Path, data: dict) -> None:
    """Same atomic-replace discipline as the session store (#5854 TOCTOU)."""
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(f".tmp.{os.getpid()}.{threading.current_thread().ident}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def write_resync_marker(session_id: str, *, kind: str, **fields) -> Optional[Path]:
    """Atomically write the resync marker; returns its path (None if unsafe).

    Raises on IO failure — callers treat that as a loud, logged last resort.
    """
    path = resync_marker_path(session_id)
    if path is None:
        return None
    payload = {
        "session_id": session_id,
        "kind": kind,
        "created_at": time.time(),
        **fields,
    }
    _atomic_write_json(path, payload)
    return path


def read_resync_marker(session_id: Any) -> Optional[dict]:
    """Load the marker for *session_id*; None when absent/unreadable/mismatched."""
    path = resync_marker_path(session_id)
    if path is None:
        return None
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("session_id") != session_id:
        return None
    return data


def clear_resync_marker(session_id: Any) -> None:
    path = resync_marker_path(session_id)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _display_stamp(message: Any) -> Optional[int]:
    """Durable id stamped on a display row (``_row_id`` family), or None."""
    if not isinstance(message, dict):
        return None
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


def _read_active_durable_ids(session_id: str, profile: Optional[str]) -> Optional[set[int]]:
    """Active durable row ids for one session (same NULL-active rule as the
    WebUI reader: ``active IS NULL OR active != 0``). None on any failure —
    callers must then skip index-based cutting rather than guess.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        import sqlite3

        from api.models import _agent_state_db_path
    except Exception:
        return None
    try:
        db_path = _agent_state_db_path(profile=profile or None)
    except Exception:
        return None
    if not db_path or not os.path.exists(str(db_path)):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except Exception:
        return None
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(messages)")
        columns = {str(row[1]) for row in cur.fetchall()}
        if not {"id", "session_id"} <= columns:
            return None
        where = "session_id = ?"
        if "active" in columns:
            where += " AND (active IS NULL OR active != 0)"
        cur.execute(f"SELECT id FROM messages WHERE {where}", (session_id,))
        return {int(r[0]) for r in cur.fetchall()}
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _session_messages(session) -> list:
    messages = getattr(session, "messages", None)
    return list(messages) if isinstance(messages, list) else []


def apply_resync_marker(session) -> bool:
    """Re-apply a pending durable correction to a freshly loaded session.

    Restore markers re-cut the transcript at the committed survivor boundary
    (with two guards against cutting rows a LATER writer legitimately added:
    the survivor set must cover every stamped row in the kept prefix, and the
    index cut is skipped when a row with an ACTIVE durable stamp sits beyond
    it). Delete markers drop exactly the display rows whose durable rows were
    removed — id-based, so later appends are untouched. On a successful
    correction the sidecar is re-persisted and the marker cleared.

    Never raises: a broken marker must not brick session load. Returns True
    only when the on-disk sidecar was corrected AND persisted.
    """
    try:
        session_id = getattr(session, "session_id", None)
        marker = read_resync_marker(session_id)
        if marker is None:
            return False
        messages = _session_messages(session)
        survivors: set[int] = set()
        for raw in marker.get("survivor_row_ids") or []:
            try:
                survivors.add(int(raw))
            except (TypeError, ValueError):
                continue
        archived: set[int] = set()
        for raw in marker.get("archived_row_ids") or []:
            try:
                archived.add(int(raw))
            except (TypeError, ValueError):
                continue
        kind = marker.get("kind")
        changed = False

        if kind == "restore":
            keep = marker.get("keep_index")
            if not isinstance(keep, int) or isinstance(keep, bool) or keep < 0:
                return False
            # 1) Drop rows the durable commit archived (exact removal by stamp).
            if archived and messages:
                filtered = [m for m in messages if _display_stamp(m) not in archived]
                if len(filtered) != len(messages):
                    messages = filtered
                    changed = True
            # 2) Index cut — only when it cannot cut legit later content.
            if keep < len(messages):
                cut_ok = True
                tail = messages[keep:]
                if any(
                    (stamp := _display_stamp(m)) is not None and stamp in survivors
                    for m in tail
                ):
                    # The list was rewritten around the survivors; the index no
                    # longer describes a prefix — leave it to stamp-based drops.
                    cut_ok = False
                else:
                    active_ids = _read_active_durable_ids(session_id, marker.get("profile"))
                    if active_ids is None:
                        # Cannot prove the tail is stale — do not cut by index.
                        cut_ok = False
                    elif any(
                        (stamp := _display_stamp(m)) is not None and stamp in active_ids
                        for m in tail
                    ):
                        # A newer, still-active row sits past the cut: cutting
                        # would hide it. Index cut is unsafe.
                        cut_ok = False
                if cut_ok:
                    messages = messages[:keep]
                    changed = True
                    for attr, key in (
                        ("truncation_watermark", "watermark"),
                        ("truncation_boundary", "boundary"),
                    ):
                        value = marker.get(key)
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            try:
                                setattr(session, attr, float(value))
                            except Exception:
                                pass
        elif kind == "delete":
            drop = set()
            for raw in marker.get("drop_display_ids") or []:
                if raw is not None:
                    drop.add(str(raw))
            if drop:
                filtered = [
                    m for m in messages
                    if not (isinstance(m, dict) and str(m.get("id")) in drop)
                ]
                if len(filtered) != len(messages):
                    messages = filtered
                    changed = True
                elif not any(
                    isinstance(m, dict) and str(m.get("id")) in drop for m in messages
                ):
                    # Rows already gone (an earlier save landed): the correction
                    # is satisfied — the marker can be retired below.
                    changed = True
        else:
            return False

        if not changed:
            return False
        try:
            session.messages = messages
        except Exception:
            return False
        try:
            session.save(touch_updated_at=False, skip_index=True)
        except Exception:
            logger.warning(
                "durable_sync: corrected %s in memory but could not persist the sidecar;"
                " marker kept for the next load",
                session_id,
                exc_info=True,
            )
            return False
        clear_resync_marker(session_id)
        logger.info("durable_sync: re-applied committed correction for %s (%s)", session_id, kind)
        return True
    except Exception:
        logger.warning("durable_sync: marker apply failed", exc_info=True)
        return False
