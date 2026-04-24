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


def rename_cli_session(session_id: str, title: str) -> bool:
    """Rename a CLI / agent / gateway-imported session in state.db.

    Used by /api/session/rename when the session is not owned by the WebUI
    (no JSON file in SESSION_DIR). Returns True if the row existed and was
    updated, False if the session_id was not found, or raises ValueError
    on title-uniqueness conflicts (mirrors SessionDB.set_session_title).

    Implementation note: we try SessionDB.set_session_title first because it
    enforces title sanitization and uniqueness. If SessionDB cannot be opened
    (e.g. the on-disk state.db schema doesn't match the installed
    hermes_state version), we fall back to a defensive raw SQL UPDATE so the
    WebUI rename feature still works in degraded environments.
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

    # Path 1: preferred — go through SessionDB so sanitization / uniqueness apply.
    db = _get_state_db()
    if db is not None:
        try:
            return bool(db.set_session_title(session_id, title))
        except ValueError:
            raise
        except Exception:
            pass  # fall through to the raw-SQL fallback
        finally:
            try:
                db.close()
            except Exception:
                pass

    # Path 2: defensive raw SQL fallback — used when SessionDB schema migration
    # fails (e.g. older state.db missing columns that the installed hermes_state
    # version expects).  Cap title length the same way the WebUI does.
    import sqlite3
    safe_title = (title or "").strip()[:80] or "Untitled"
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            # Uniqueness check (mirrors SessionDB.set_session_title behaviour)
            conflict = conn.execute(
                "SELECT id FROM sessions WHERE title = ? AND id != ?",
                (safe_title, session_id),
            ).fetchone()
            if conflict:
                raise ValueError(
                    f"Title {safe_title!r} is already in use by session {conflict[0]}"
                )
            cursor = conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                (safe_title, session_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()
    except ValueError:
        raise
    except Exception:
        return False
