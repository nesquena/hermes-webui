"""Shared helpers for reading Hermes Agent sessions from state.db."""
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


MESSAGING_SOURCES = {
    'discord',
    'slack',
    'telegram',
    'weixin',
}

SOURCE_LABELS = {
    'api_server': 'API',
    'cli': 'CLI',
    'cron': 'Cron',
    'discord': 'Discord',
    'slack': 'Slack',
    'telegram': 'Telegram',
    'tool': 'Tool',
    'webui': 'WebUI',
    'weixin': 'Weixin',
}


def normalize_agent_session_source(raw_source: str | None) -> dict:
    """Return stable source metadata for Hermes Agent session rows.

    ``sessions.source`` is an Agent-level raw value. WebUI needs a smaller,
    durable contract so routes, SSE snapshots, and future sidebar policies do
    not each reimplement raw-source checks.
    """
    raw = str(raw_source or '').strip().lower() or 'unknown'

    if raw == 'webui':
        session_source = 'webui'
    elif raw == 'cli':
        session_source = 'cli'
    elif raw in MESSAGING_SOURCES:
        session_source = 'messaging'
    elif raw == 'cron':
        session_source = 'cron'
    elif raw == 'tool':
        session_source = 'tool'
    elif raw == 'api_server':
        session_source = 'api'
    else:
        session_source = 'other'

    label = SOURCE_LABELS.get(raw)
    if not label:
        label = raw.replace('_', ' ').title() if raw != 'unknown' else 'Agent'

    return {
        'raw_source': None if raw == 'unknown' else raw,
        'session_source': session_source,
        'source_label': label,
    }


def _with_normalized_source(row: dict) -> dict:
    normalized = normalize_agent_session_source(row.get('source'))
    return {**row, **normalized}


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
        # Keep the chain head's visible identity (title, started_at), but
        # point the row at the latest importable segment for navigation AND
        # surface the tip's recency so an actively-used chain bubbles to the
        # top of the sidebar by its true last activity. Without overriding
        # last_activity, a long-lived chain whose tip is being edited NOW
        # would sort by the root's old timestamp and fall below recently
        # touched standalone sessions — exactly the inverse of what a user
        # expects from "Show agent sessions" sorted by activity.
        for key in (
            'id', 'model', 'message_count', 'actual_message_count',
            'ended_at', 'end_reason', 'last_activity',
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


def read_importable_agent_session_rows(
    db_path: Path,
    limit: int = 200,
    log=None,
    exclude_sources: tuple[str, ...] | None = ("cron",),
) -> list[dict]:
    """Return non-WebUI agent sessions projected as importable conversations.

    Hermes Agent can create rows in ``state.db.sessions`` before a session has
    any messages, and long conversations can be split into compression-linked
    rows. WebUI cannot import empty rows and should not show compression
    segments as separate conversations, so both the regular ``/api/sessions``
    path and the gateway SSE watcher use this shared projection.

    By default, omit background/internal sources such as ``cron`` from the WebUI
    sidebar. This mirrors Hermes Agent CLI's session-list behaviour: interactive
    views should stay focused on user-facing conversations, while callers that
    need a source-specific diagnostic view can opt out by passing
    ``exclude_sources=None``.
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

        where_clauses = ["s.source IS NOT NULL", "s.source != 'webui'"]
        params: list[str] = []
        if exclude_sources:
            excluded = tuple(str(source) for source in exclude_sources if source)
            if excluded:
                placeholders = ", ".join("?" for _ in excluded)
                where_clauses.append(f"s.source NOT IN ({placeholders})")
                params.extend(excluded)

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
            WHERE {' AND '.join(where_clauses)}
            GROUP BY s.id
            ORDER BY COALESCE(MAX(m.timestamp), s.started_at) DESC
            """,
            params,
        )
        projected = _project_agent_session_rows([dict(row) for row in cur.fetchall()])
        projected = [_with_normalized_source(row) for row in projected]
        if limit is None:
            return projected
        return projected[:max(0, int(limit))]



def read_session_lineage_metadata(db_path: Path, session_ids: list[str] | set[str]) -> dict[str, dict]:
    """Return compression-lineage metadata for known WebUI sidebar sessions.

    WebUI sessions are persisted as JSON files, but Hermes Agent also mirrors
    them into ``state.db.sessions`` for insights/session history. Compression
    and cross-surface continuation create parent chains there. ``/api/sessions``
    needs to surface that lineage to the sidebar so client-side collapse can
    group logical continuations without mutating or deleting any session files.

    Missing DBs, old schemas, or incomplete rows degrade to an empty mapping.
    """
    wanted = {str(sid) for sid in (session_ids or []) if sid}
    db_path = Path(db_path)
    if not wanted or not db_path.exists():
        return {}

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            session_cols = {row[1] for row in cur.fetchall()}
            if 'parent_session_id' not in session_cols or 'end_reason' not in session_cols:
                return {}
            cur.execute("SELECT id, parent_session_id, end_reason FROM sessions")
            rows = {row['id']: dict(row) for row in cur.fetchall()}
    except Exception:
        return {}

    metadata: dict[str, dict] = {}
    for sid in wanted:
        row = rows.get(sid)
        if not row:
            continue

        parent_id = row.get('parent_session_id')
        if parent_id:
            metadata.setdefault(sid, {})['parent_session_id'] = parent_id

        root_id = sid
        current_id = sid
        segment_count = 1
        seen = {sid}
        while True:
            current = rows.get(current_id)
            parent_id = current.get('parent_session_id') if current else None
            parent = rows.get(parent_id) if parent_id else None
            if not parent or parent_id in seen:
                break
            if parent.get('end_reason') not in {'compression', 'cli_close'}:
                break
            root_id = parent_id
            current_id = parent_id
            seen.add(parent_id)
            segment_count += 1

        if root_id != sid:
            entry = metadata.setdefault(sid, {})
            entry['_lineage_root_id'] = root_id
            entry['_compression_segment_count'] = segment_count

    return metadata
