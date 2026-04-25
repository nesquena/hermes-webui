"""Shared helpers for reading Hermes Agent sessions from state.db."""
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def _optional_col(name: str, columns: set[str], fallback: str = "NULL") -> str:
    return f"s.{name}" if name in columns else f"{fallback} AS {name}"


def _is_compression_continuation(parent: dict | None, child: dict) -> bool:
    """Mirror Hermes Agent's compression-child guard.

    A child is a continuation only when the parent ended because of compression
    and the child started after that compression boundary. Plain parent/child
    relationships are left alone for future subagent-tree work.
    """
    if not parent:
        return False
    if parent.get('end_reason') != 'compression':
        return False
    ended_at = parent.get('ended_at')
    if ended_at is None:
        return False
    try:
        return float(child.get('started_at') or 0) >= float(ended_at)
    except (TypeError, ValueError):
        return False


def _project_agent_session_rows(rows: list[dict]) -> list[dict]:
    """Collapse compression chains into one logical sidebar row.

    The visible conversation should still look like the original chain head
    (title and timestamps), while importing should use the latest importable
    segment so the user continues from the current compressed state.
    """
    rows_by_id = {row['id']: row for row in rows}
    children_by_parent: dict[str, list[dict]] = {}
    continuation_child_ids = set()

    for row in rows:
        parent_id = row.get('parent_session_id')
        if not parent_id:
            continue
        children_by_parent.setdefault(parent_id, []).append(row)
        if _is_compression_continuation(rows_by_id.get(parent_id), row):
            continuation_child_ids.add(row['id'])

    for children in children_by_parent.values():
        children.sort(key=lambda row: row.get('started_at') or 0, reverse=True)

    def compression_tip(row: dict) -> tuple[dict | None, int]:
        current = row
        seen = {row['id']}
        latest_importable = row if (row.get('actual_message_count') or 0) > 0 else None
        segment_count = 1
        for _ in range(len(rows_by_id) + 1):
            candidates = [
                child for child in children_by_parent.get(current['id'], [])
                if child['id'] not in seen and _is_compression_continuation(current, child)
            ]
            if not candidates:
                return latest_importable, segment_count
            current = candidates[0]
            seen.add(current['id'])
            segment_count += 1
            if (current.get('actual_message_count') or 0) > 0:
                latest_importable = current
        return latest_importable, segment_count

    projected = []
    for row in rows:
        if row['id'] in continuation_child_ids:
            continue

        segment_count = 1
        tip = row
        if row.get('end_reason') == 'compression':
            tip, segment_count = compression_tip(row)
        if not tip or (tip.get('actual_message_count') or 0) <= 0:
            continue

        if tip is row:
            projected.append(dict(row))
            continue

        merged = dict(row)
        # Keep the chain head's visible metadata, but point the row at the
        # latest importable segment. That avoids rewriting history in the
        # sidebar while still continuing from the compressed current state.
        for key in (
            'id', 'model', 'message_count', 'actual_message_count',
            'ended_at', 'end_reason',
        ):
            if key in tip:
                merged[key] = tip[key]
        if not merged.get('title'):
            merged['title'] = tip.get('title')
        if not merged.get('source'):
            merged['source'] = tip.get('source')
        merged['_lineage_root_id'] = row['id']
        merged['_lineage_tip_id'] = tip['id']
        merged['_compression_segment_count'] = segment_count
        projected.append(merged)

    projected.sort(
        key=lambda row: row.get('last_activity') or row.get('started_at') or 0,
        reverse=True,
    )
    return projected


def read_importable_agent_session_rows(db_path: Path, limit: int = 200, log=None) -> list[dict]:
    """Return non-WebUI agent sessions projected as importable conversations.

    Hermes Agent can create rows in ``state.db.sessions`` before a session has
    any messages, and long conversations can be split into compression-linked
    rows. WebUI cannot import empty rows and should not show compression
    segments as separate conversations, so both the regular ``/api/sessions``
    path and the gateway SSE watcher use this shared projection.
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

        parent_expr = _optional_col('parent_session_id', session_cols)
        ended_expr = _optional_col('ended_at', session_cols)
        end_reason_expr = _optional_col('end_reason', session_cols)

        cur.execute(
            f"""
            SELECT s.id, s.title, s.model, s.message_count,
                   s.started_at, s.source,
                   {parent_expr},
                   {ended_expr},
                   {end_reason_expr},
                   COUNT(m.id) AS actual_message_count,
                   MAX(m.timestamp) AS last_activity
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.source IS NOT NULL AND s.source != 'webui'
            GROUP BY s.id
            ORDER BY COALESCE(MAX(m.timestamp), s.started_at) DESC
            """,
        )
        projected = _project_agent_session_rows([dict(row) for row in cur.fetchall()])
        if limit is None:
            return projected
        return projected[:max(0, int(limit))]
