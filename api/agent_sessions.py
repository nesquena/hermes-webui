"""Shared helpers for reading Hermes Agent sessions from state.db."""
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def read_importable_agent_session_rows(db_path: Path, limit: int = 200, log=None) -> list[dict]:
    """Return non-WebUI agent sessions that have readable message rows.

    Hermes Agent can create rows in ``state.db.sessions`` before a session has
    any messages. WebUI cannot import those rows, so both the regular
    ``/api/sessions`` path and the gateway SSE watcher must filter them the
    same way.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []

    log = log or logger
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Older Hermes Agent versions may not have source tracking. Without a
        # source column we cannot safely distinguish WebUI rows from agent rows.
        cur.execute("PRAGMA table_info(sessions)")
        session_cols = {row[1] for row in cur.fetchall()}
        if 'source' not in session_cols:
            log.warning(
                "agent session listing skipped: state.db at %s has no 'source' column "
                "(older hermes-agent?). Agent sessions unavailable. "
                "Upgrade hermes-agent to fix this.",
                db_path,
            )
            return []

        cur.execute(
            """
            SELECT s.id, s.title, s.model, s.message_count,
                   s.started_at, s.source,
                   COUNT(m.id) AS actual_message_count,
                   MAX(m.timestamp) AS last_activity
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.source IS NOT NULL AND s.source != 'webui'
            GROUP BY s.id
            HAVING COUNT(m.id) > 0
            ORDER BY COALESCE(MAX(m.timestamp), s.started_at) DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(row) for row in cur.fetchall()]
