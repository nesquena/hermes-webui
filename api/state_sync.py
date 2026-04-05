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
import os
from pathlib import Path


def _get_state_db():
    """Get a HermesState instance for the active profile's state.db.
    Returns None if hermes_state is not importable or DB is unavailable.
    """
    try:
        from hermes_state import HermesState
    except ImportError:
        return None

    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(Path.home() / '.hermes')))

    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return None

    try:
        return HermesState(str(db_path))
    except Exception:
        return None


def sync_session_start(session_id, model=None):
    """Register a WebUI session in state.db (idempotent).
    Called when a session's first message is sent.
    """
    try:
        db = _get_state_db()
        if not db:
            return
        db.ensure_session(
            session_id=session_id,
            source='webui',
            model=model,
        )
    except Exception:
        pass  # never crash the WebUI for sync failures


def sync_session_usage(session_id, input_tokens=0, output_tokens=0,
                       estimated_cost=None, model=None, title=None):
    """Update token usage and title for a WebUI session in state.db.
    Called after each turn completes. Uses absolute=True to set totals
    (the WebUI Session already accumulates across turns).
    """
    try:
        db = _get_state_db()
        if not db:
            return
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
        # Update title if we have one
        if title:
            try:
                db._execute_write(
                    "UPDATE sessions SET title = ? WHERE id = ?",
                    (title, session_id),
                )
            except Exception:
                pass
    except Exception:
        pass  # never crash the WebUI for sync failures
