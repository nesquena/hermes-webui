"""
Hermes Web UI -- Optional state.db sync bridge.

Mirrors WebUI session metadata (token usage, title, model) into the
hermes-agent state.db so that /insights, session lists, and cost
tracking include WebUI activity.

This is opt-in via the 'sync_to_insights' setting (default: off).
All operations are wrapped in try/except -- if state.db is unavailable,
locked, or the schema doesn't match, the WebUI continues normally.

The bridge uses absolute token counts (not deltas) because the WebUI
Session object already accumulates totals across turns. This avoids
any double-counting risk.
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_state_db():
    """Get a SessionDB instance for the active profile's state.db.
    Returns None if hermes_state is not importable or DB is unavailable.
    Each caller is responsible for calling db.close() when done.
    """
    try:
        from hermes_state import SessionDB
    except ImportError:
        return None

    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        logger.debug("Failed to resolve hermes home, using default")
        hermes_home = Path(os.getenv('HERMES_HOME', str(Path.home() / '.hermes')))

    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return None

    try:
        return SessionDB(db_path)
    except Exception:
        logger.debug("Failed to open state.db")
        return None


def sync_session_start(session_id: str, model=None) -> None:
    """Register a WebUI session in state.db (idempotent).
    Called when a session's first message is sent.
    """
    db = _get_state_db()
    if not db:
        return
    try:
        db.ensure_session(
            session_id=session_id,
            source='webui',
            model=model,
        )
    except Exception:
        logger.debug("Failed to sync session start to state.db")
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close state.db")


def sync_session_usage(session_id: str, input_tokens: int=0, output_tokens: int=0,
                       estimated_cost=None, model=None, title: str=None,
                       message_count: int=None) -> None:
    """Update token usage and title for a WebUI session in state.db.
    Called after each turn completes. Uses absolute=True to set totals
    (the WebUI Session already accumulates across turns).
    """
    db = _get_state_db()
    if not db:
        return
    try:
        # Ensure session exists first (idempotent)
        db.ensure_session(session_id=session_id, source='webui', model=model)
        # Set absolute token counts
        db.update_token_counts(
            session_id=session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost,
            model=model,
            absolute=True,
        )
        # Update title if we have one, using the public API
        if title:
            try:
                db.set_session_title(session_id, title)
            except Exception:
                logger.debug("Failed to sync session title to state.db")
        # Update message count
        if message_count is not None:
            try:
                def _set_msg_count(conn):
                    conn.execute(
                        "UPDATE sessions SET message_count = ? WHERE id = ?",
                        (message_count, session_id),
                    )
                db._execute_write(_set_msg_count)
            except Exception:
                logger.debug("Failed to sync message count to state.db")
    except Exception:
        logger.debug("Failed to sync session usage to state.db")
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close state.db")


def _sanitize_title(title: str) -> str:
    """Mirror SessionDB.set_session_title sanitization and the WebUI cap."""
    return (title or "").strip()[:80] or "Untitled"


def _resolve_lineage_root(db_path: Path, session_id: str) -> str:
    """Walk parent_session_id upward to find the lineage root.

    Renames target the chain head so the projection (which prefers the
    head's title) shows the new name. Stops at the first row whose
    ``parent_session_id`` is NULL or unknown, or when the parent's
    ``end_reason`` isn't ``compression`` (only compression chains are
    considered the same logical conversation). Returns ``session_id``
    unchanged on any error.
    """
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
    except Exception:
        return session_id
    try:
        cur = conn.execute("PRAGMA table_info(sessions)")
        cols = {row[1] for row in cur.fetchall()}
        if 'parent_session_id' not in cols:
            return session_id
        current = session_id
        seen = {current}
        for _ in range(64):
            row = conn.execute(
                "SELECT parent_session_id, end_reason FROM sessions WHERE id = ?",
                (current,),
            ).fetchone()
            if not row:
                return current
            parent_id = row[0]
            if not parent_id or parent_id in seen:
                return current
            parent_row = conn.execute(
                "SELECT end_reason FROM sessions WHERE id = ?",
                (parent_id,),
            ).fetchone()
            if not parent_row or parent_row[0] != 'compression':
                return current
            current = parent_id
            seen.add(current)
        return current
    except Exception:
        return session_id
    finally:
        try:
            conn.close()
        except Exception:
            pass


def rename_cli_session(session_id: str, title: str) -> bool:
    """Rename a CLI / agent / gateway-imported session in state.db.

    Used by /api/session/rename when the session is not owned by the WebUI
    (no JSON file in SESSION_DIR). Returns True if the row existed and was
    updated, False if the session_id was not found, or raises ValueError
    on title-uniqueness conflicts (mirrors SessionDB.set_session_title).

    Note on compression chains
    --------------------------
    The sidebar projection (``api.agent_sessions._project_agent_session_rows``)
    collapses a compression chain into a single row that uses the *tip*'s
    session_id for navigation but the chain *head*'s title for visible
    identity. So the rename must hit the head — otherwise the new name
    would never appear after a hard refresh. We walk ``parent_session_id``
    upward from the supplied id (only across compression boundaries) and
    update the lineage root. Updating just one row keeps us compatible
    with ``hermes_state``'s ``UNIQUE INDEX … ON sessions(title) WHERE
    title IS NOT NULL`` — the auto-generated ``#N`` titles on the
    intermediate / tip rows stay distinct.
    """
    # Resolve the state.db path the same way _get_state_db does
    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(Path.home() / '.hermes')))
    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return False

    safe_title = _sanitize_title(title)

    # Walk to the lineage root so the projection (head-prefer title)
    # surfaces the new name on the next refresh.
    target_id = _resolve_lineage_root(db_path, session_id)

    # Path 1: hand the canonical update to SessionDB so it can do its
    # own bookkeeping (event emission, cache invalidation, uniqueness
    # validation, etc.) when the agent's hermes_state package is installed.
    sdb = _get_state_db()
    if sdb is not None:
        try:
            return bool(sdb.set_session_title(target_id, safe_title))
        except ValueError:
            raise
        except Exception:
            # Fall through to raw SQL for degraded environments.
            pass
        finally:
            try:
                sdb.close()
            except Exception:
                pass

    # Path 2: raw SQL fallback (no hermes_state available).
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
    except Exception:
        return False
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?",
            (safe_title, target_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.IntegrityError as exc:
        # Mirror SessionDB.set_session_title's conflict semantics.
        raise ValueError(
            f"Title {safe_title!r} is already in use by another session"
        ) from exc
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
