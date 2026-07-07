"""Shared helpers for reading Hermes Agent sessions from state.db."""
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)


MESSAGING_SOURCES = {
    'discord',
    'email',
    'wecom',
    'wecom_callback',
    'slack',
    'telegram',
    'weixin',
}

CLI_MIN_UNTITLED_MESSAGE_COUNT = 6
CLI_MIN_UNTITLED_USER_MESSAGE_COUNT = 2

SOURCE_LABELS = {
    'api_server': 'API',
    'cli': 'CLI',
    'cron': 'Cron',
    'discord': 'Discord',
    'email': 'Email',
    'wecom': 'WeCom',
    'wecom_callback': 'WeCom Callback',
    'slack': 'Slack',
    'telegram': 'Telegram',
    'tool': 'Tool',
    'tui': 'TUI',
    'webhook': 'Webhook',
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
    elif raw in {'cli', 'tui'}:
        session_source = 'cli'
    elif raw in MESSAGING_SOURCES:
        session_source = 'messaging'
    elif raw == 'cron':
        session_source = 'cron'
    elif raw == 'webhook':
        session_source = 'webhook'
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


def _safe_lower(value) -> str:
    return str(value or "").strip().lower()


def _normalize_source_name(value: object) -> str:
    source = _safe_lower(value)
    if not source:
        return ""
    if source.endswith(" session"):
        source = source[:-len(" session")].strip()
    return source


def _looks_like_default_cli_title(row: dict) -> bool:
    """Return True when a CLI row looks like framework-generated metadata."""
    title = _safe_lower(row.get("title"))
    if not title or title == "untitled":
        return True
    if title in {"cli", "cli session"}:
        return True

    source_candidates = {
        _normalize_source_name(row.get("source")),
        _normalize_source_name(row.get("session_source")),
        _normalize_source_name(row.get("source_tag")),
        _normalize_source_name(row.get("raw_source")),
        _normalize_source_name(row.get("source_label")),
    }
    source_candidates.discard("")
    source_candidates.add("cli")
    return any(title == f"{candidate} session" for candidate in source_candidates)


def _as_positive_int(value) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _as_score(*values) -> float:
    """First numerically-coercible value as a float, else 0.0.

    Used to score lineage tips by recency. ``last_message_at`` comes from
    ``MAX(timestamp)`` and is normally a numeric epoch, but older/non-standard
    state.db schemas can store an ISO-8601 *text* timestamp. Rather than letting
    a non-numeric value raise ValueError (which previously escaped the DB
    try-block and dropped all lineage metadata), fall through to the next
    candidate (e.g. ``started_at``).
    """
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _count_user_turns(row: dict) -> int:
    user_turns = row.get("actual_user_message_count")
    if user_turns is None:
        user_turns = row.get("user_message_count")
    if user_turns is None:
        messages = row.get("messages") or []
        if isinstance(messages, list):
            return sum(
                1
                for msg in messages
                if _safe_lower(msg.get("role") if isinstance(msg, dict) else msg) == "user"
            )
        return 0
    return _as_positive_int(user_turns)


def _has_cli_lineage(row: dict) -> bool:
    segment_count = _as_positive_int(row.get("_compression_segment_count"))
    return segment_count > 1 or bool(row.get("_lineage_root_id"))


def is_cli_session_row(row: dict) -> bool:
    """Return True for rows that should be treated as CLI-imported sessions."""
    if not isinstance(row, dict):
        return False
    source = _safe_lower(row.get("session_source"))
    source_tag = _safe_lower(row.get("source_tag"))
    raw_source = _safe_lower(row.get("raw_source"))
    source_name = _safe_lower(row.get("source"))
    source_label = _safe_lower(row.get("source_label"))
    if "webui" in {source, source_tag, raw_source, source_name, source_label}:
        return False
    # 'subagent' is a delegated delegate_task child: view-only, owned by the
    # runner, never a writable WebUI/CLI session (#5307). Classify it non-CLI so
    # sidebar rows and every is_cli_session_row() consumer keep it out of the
    # CLI/writable treatment.
    non_cli_sources = MESSAGING_SOURCES | {"cron", "webhook", "tool", "api", "api_server", "subagent"}
    if {source, source_tag, raw_source, source_name, source_label} & non_cli_sources:
        return False
    if source == "messaging":
        return False
    if source == "cli":
        return True
    if (
        source_tag in {"cli", "tui"}
        or raw_source in {"cli", "tui"}
        or source_name in {"cli", "tui"}
        or source_label in {"cli", "tui"}
    ):
        return True

    # Legacy imported CLI rows may only be marked as CLI in sidebar metadata.
    # Keep this conservative to avoid treating messaging sessions as CLI.
    return bool(
        row.get("is_cli_session")
        and source not in MESSAGING_SOURCES
        and source_tag not in MESSAGING_SOURCES
        and raw_source not in MESSAGING_SOURCES
        and source_name not in MESSAGING_SOURCES
        and _looks_like_default_cli_title(row)
    )


def is_cli_session_row_visible(row: dict) -> bool:
    """Return whether a CLI-related row should remain visible in the sidebar."""
    if not isinstance(row, dict):
        return False
    if not is_cli_session_row(row):
        return True

    actual_message_count = _as_positive_int(row.get("actual_message_count"))
    message_count = actual_message_count or _as_positive_int(row.get("message_count"))
    if message_count <= 0:
        return False

    if (
        actual_message_count > 0
        and _count_user_turns(row) > 0
        and row.get("ended_at") is None
        and not row.get("end_reason")
    ):
        return True

    if "tui" in {
        _normalize_source_name(row.get("source")),
        _normalize_source_name(row.get("source_tag")),
        _normalize_source_name(row.get("raw_source")),
        _normalize_source_name(row.get("source_label")),
    }:
        return True

    if _has_cli_lineage(row):
        return True

    if not _looks_like_default_cli_title(row):
        return True

    return _count_user_turns(row) >= CLI_MIN_UNTITLED_USER_MESSAGE_COUNT


def _is_continuation_session(parent: dict | None, child: dict | None) -> bool:
    """Return True when ``child`` is the next segment of the same conversation.

    Compression rotates session ids automatically. A manual CLI close followed
    by ``hermes -c`` also records a new child session; for sidebar projection it
    should continue the same visible conversation rather than becoming a
    separate child-session row. Plain parent/child links that started before the
    parent's ended boundary remain child sessions.

    Do not collapse lineage across raw sources. A WebUI session that continues
    from a Telegram/CLI/etc. parent must remain visible as its own surface-owned
    conversation; otherwise the tip inherits the root's title/source metadata and
    can disappear under messaging/sidebar policies.
    """
    if not parent or not child:
        return False
    if str(child.get('session_source') or '').strip().lower() == 'fork':
        return False
    parent_source = str(parent.get('source') or '').strip().lower()
    child_source = str(child.get('source') or '').strip().lower()
    if parent_source and child_source and parent_source != child_source:
        return False
    if parent.get('end_reason') not in {'compression', 'cli_close'}:
        return False
    ended_at = parent.get('ended_at')
    if ended_at is None:
        # Older state.db rows/tests may not have ended_at populated. Preserve
        # the historical contract that compression/cli_close parent links are
        # continuations when no boundary timestamp is available.
        return True
    try:
        return float(child.get('started_at') or 0) >= float(ended_at)
    except (TypeError, ValueError):
        return False


def _continuation_root_id(rows_by_id: dict[str, dict], session_id: str | None) -> str | None:
    """Return the visible lineage root for ``session_id`` by walking continuations."""
    if not session_id:
        return None
    root_id = str(session_id)
    current_id = root_id
    seen = {current_id}
    for _ in range(len(rows_by_id) + 1):
        current = rows_by_id.get(current_id)
        parent_id = current.get('parent_session_id') if current else None
        parent = rows_by_id.get(parent_id) if parent_id else None
        if not parent or not _is_continuation_session(parent, current):
            return root_id
        if parent_id in seen:
            return root_id
        root_id = str(parent_id)
        current_id = str(parent_id)
        seen.add(current_id)
    return root_id


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
        parent = rows_by_id.get(parent_id)
        if _is_continuation_session(parent, row):
            continuation_child_ids.add(row['id'])
        else:
            row['relationship_type'] = 'child_session'
            row['parent_title'] = parent.get('title') if parent else None
            row['parent_source'] = parent.get('source') if parent else None
            parent_root = _continuation_root_id(rows_by_id, parent_id)
            if parent_root:
                row['_parent_lineage_root_id'] = parent_root

    for children in children_by_parent.values():
        children.sort(key=lambda row: row.get('started_at') or 0, reverse=True)

    def compression_tip(row: dict) -> tuple[dict | None, int]:
        """Return the freshest importable continuation descendant for ``row``.

        Compression parents can have multiple continuation-looking children when
        a stale segment is resumed after a newer compressed branch already
        exists. Picking the newest *direct* child can hide the branch whose
        deeper descendant has the actual latest activity. Walk all reachable
        continuation descendants and select by real message activity instead.
        """
        latest_importable = row if (row.get('actual_message_count') or 0) > 0 else None
        segment_count = 0
        best_depth = 1
        best_score = (
            _as_score(latest_importable.get('last_activity'), latest_importable.get('started_at'))
            if latest_importable
            else 0
        )
        stack: list[tuple[dict, int]] = [(row, 1)]
        seen: set[str] = set()

        while stack:
            current, depth = stack.pop()
            current_id = current.get('id')
            if not current_id or current_id in seen:
                continue
            seen.add(current_id)
            segment_count += 1

            current_score = _as_score(current.get('last_activity'), current.get('started_at'))
            if (
                (current.get('actual_message_count') or 0) > 0
                and (current_score > best_score or (current_score == best_score and depth >= best_depth))
            ):
                latest_importable = current
                best_depth = depth
                best_score = current_score
            for child in children_by_parent.get(current_id, []):
                child_id = child.get('id')
                if not child_id or child_id in seen:
                    continue
                if not _is_continuation_session(current, child):
                    continue
                stack.append((child, depth + 1))

        return latest_importable, max(segment_count, 1)

    projected = []
    for row in rows:
        if row['id'] in continuation_child_ids:
            continue

        segment_count = 1
        tip = row
        if row.get('end_reason') in {'compression', 'cli_close'}:
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
            'id', 'model', 'message_count', 'actual_message_count', 'actual_user_message_count',
            'ended_at', 'end_reason', 'last_activity',
        ):
            if key in tip:
                merged[key] = tip[key]
        if str(tip.get('source') or '').strip().lower() == 'tui':
            # TUI continuation rows are user-visible session segments (#6, #17,
            # ...), not opaque compression snapshots. Keep navigation pointed at
            # the latest tip and show that tip's title so the newest conversation
            # can be found by its visible TUI name.
            if tip.get('title'):
                merged['title'] = tip.get('title')
            if tip.get('source'):
                merged['source'] = tip.get('source')
        else:
            if not merged.get('title'):
                merged['title'] = tip.get('title')
            if not merged.get('source'):
                merged['source'] = tip.get('source')
        merged['_lineage_root_id'] = row['id']
        merged['_lineage_tip_id'] = tip['id']
        merged['_compression_segment_count'] = segment_count
        projected.append(merged)

    projected.sort(
        key=lambda row: _as_score(row.get('last_activity'), row.get('started_at')),
        reverse=True,
    )
    return projected


def read_importable_agent_session_rows(
    db_path: Path,
    limit: int | None = 200,
    log=None,
    exclude_sources: tuple[str, ...] | None = ("cron", "webui"),
    include_sources: tuple[str, ...] | None = None,
) -> list[dict]:
    """Return agent sessions projected as importable conversations.

    Hermes Agent can create rows in ``state.db.sessions`` before a session has
    any messages, and long conversations can be split into compression-linked
    rows. WebUI cannot import empty rows and should not show compression
    segments as separate conversations, so both the regular ``/api/sessions``
    path and the gateway SSE watcher use this shared projection.

    By default, omit background/internal sources such as ``cron`` from the WebUI
    sidebar. This mirrors Hermes Agent CLI's session-list behaviour: interactive
    views should stay focused on user-facing conversations, while callers that
    need a source-specific diagnostic view can opt out by passing
    ``exclude_sources=None``. ``include_sources`` is an additional narrowing
    filter; callers that want an include-only query should explicitly pass
    ``exclude_sources=None`` so the default exclusions do not also apply.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []

    log = log or logger
    # Open read-only for this projection/listing path: it is a pure read, and
    # holding a write-capable handle on the live (multi-GB, WAL) state.db while
    # the agent streams into it adds needless checkpoint/lock surface (#5455).
    # The defensive index self-heal below still runs, but through a separate
    # short-lived writable connection on the rare missing-index path only.
    read_only_uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(read_only_uri, uri=True)
    except sqlite3.Error as exc:
        log.warning(
            "agent session listing read-only open failed for %s; falling back to writable connection: %s",
            db_path,
            exc,
        )
        conn = sqlite3.connect(str(db_path))
    with closing(conn):
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Older Hermes Agent versions may not have source tracking. Without a
        # source column we cannot safely distinguish WebUI rows from agent rows.
        cur.execute("PRAGMA table_info(sessions)")
        session_cols = {row[1] for row in cur.fetchall()}
        cur.execute("PRAGMA table_info(messages)")
        message_cols = {row[1] for row in cur.fetchall()}
        if 'source' not in session_cols:
            log.warning(
                "agent session listing skipped: state.db at %s has no 'source' column "
                "(older hermes-agent?). Agent sessions unavailable. "
                "Upgrade hermes-agent to fix this.",
                db_path,
            )
            return []

        parent_expr = _optional_col('parent_session_id', session_cols)
        session_source_expr = _optional_col('session_source', session_cols)
        ended_expr = _optional_col('ended_at', session_cols)
        end_reason_expr = _optional_col('end_reason', session_cols)
        user_id_expr = _optional_col('user_id', session_cols)
        chat_id_expr = _optional_col('chat_id', session_cols)
        chat_type_expr = _optional_col('chat_type', session_cols)
        thread_id_expr = _optional_col('thread_id', session_cols)
        session_key_expr = _optional_col('session_key', session_cols)
        origin_chat_id_expr = _optional_col('origin_chat_id', session_cols)
        origin_user_id_expr = _optional_col('origin_user_id', session_cols)
        platform_expr = _optional_col('platform', session_cols)
        # Older/minimal state.db schemas can have NO ``messages`` table at all,
        # or a ``messages`` table without a ``session_id`` / ``timestamp`` column.
        # The projection SQL below joins ``messages`` and aggregates
        # ``MAX(m.timestamp)`` unconditionally, so on those schemas the query
        # raised ``sqlite3.OperationalError`` — which the caller
        # (``get_cli_sessions``) swallows into an empty list, silently hiding
        # ALL imported/CLI/agent sessions from the sidebar. Detect the columns
        # and degrade gracefully (mirrors ``read_session_lineage_metadata``):
        # only join/aggregate ``messages`` when it's actually usable, otherwise
        # fall back to the per-session ``s.message_count`` / ``s.started_at``. (#3762)
        messages_has_session_id = 'session_id' in message_cols
        messages_has_timestamp = 'timestamp' in message_cols
        use_messages_join = messages_has_session_id
        count_col = 'id' if 'id' in message_cols else 'session_id'

        # Defensive index prime (#3887). The normal candidate-ordering shape uses
        # the agent's standard ``idx_messages_session ON messages(session_id,
        # timestamp)`` index; without it, large cron-only scans degrade badly.
        # Writable dbs self-heal by recreating the index. Read-only or locked dbs
        # fall back to the pre-aggregated cron-only path below instead of failing.
        messages_index_present = False
        if messages_has_session_id and messages_has_timestamp:
            try:
                cur.execute("PRAGMA index_list(messages)")
                messages_index_present = any(str(row[1]) == "idx_messages_session" for row in cur.fetchall())
            except sqlite3.Error:
                messages_index_present = False
            if not messages_index_present:
                # Self-heal via a separate writable connection so the common
                # (index-present) path keeps its read-only handle. On a truly
                # read-only/locked db this fails and we degrade to the
                # pre-aggregated cron-only path below, exactly as before.
                try:
                    with closing(sqlite3.connect(str(db_path))) as _heal:
                        _heal.execute(
                            "CREATE INDEX IF NOT EXISTS idx_messages_session "
                            "ON messages(session_id, timestamp)"
                        )
                        _heal.commit()
                    messages_index_present = True
                except sqlite3.Error:
                    pass  # read-only db / locked / older schema — degrade gracefully

        if use_messages_join:
            actual_count_expr = f"COUNT(m.{count_col})"
            if 'role' in message_cols:
                user_message_count_expr = "COUNT(CASE WHEN LOWER(m.role) = 'user' THEN 1 END)"
            else:
                user_message_count_expr = f"COUNT(m.{count_col})"
            last_activity_expr = "MAX(m.timestamp)" if messages_has_timestamp else "NULL"
            join_clause = "LEFT JOIN messages m ON m.session_id = s.id"
            group_by_clause = "GROUP BY s.id"
        else:
            # No usable messages table: use the denormalized per-session counts
            # and ``started_at`` so the rows still surface in the sidebar.
            actual_count_expr = "s.message_count"
            user_message_count_expr = "s.message_count"
            last_activity_expr = "NULL"
            join_clause = ""
            group_by_clause = ""

        order_by_clause = "ORDER BY s.started_at DESC"
        latest_messages_cte = None
        candidate_order_clause = "ORDER BY s.started_at DESC"

        where_clauses = ["s.source IS NOT NULL"]
        params: list[object] = []
        included = ()
        if include_sources:
            included = tuple(str(source) for source in include_sources if source)
            if included:
                placeholders = ", ".join("?" for _ in included)
                where_clauses.append(f"s.source IN ({placeholders})")
                params.extend(included)
        if exclude_sources:
            excluded = tuple(str(source) for source in exclude_sources if source)
            if excluded:
                placeholders = ", ".join("?" for _ in excluded)
                where_clauses.append(f"s.source NOT IN ({placeholders})")
                params.extend(excluded)

        use_preaggregated_candidate_order = (
            use_messages_join
            and messages_has_timestamp
            and included == ("cron",)
            and not messages_index_present
        )
        if use_preaggregated_candidate_order:
            order_by_clause = "ORDER BY COALESCE(MAX(m.timestamp), s.started_at) DESC"
            latest_messages_cte = (
                "latest_messages AS (\n"
                "                    SELECT mx.session_id AS session_id, MAX(mx.timestamp) AS last_message_at\n"
                "                    FROM messages mx\n"
                "                    GROUP BY mx.session_id\n"
                "                )"
            )
            candidate_order_clause = "ORDER BY COALESCE(lm.last_message_at, s.started_at) DESC, s.started_at DESC"
        elif use_messages_join and messages_has_timestamp:
            order_by_clause = "ORDER BY COALESCE(MAX(m.timestamp), s.started_at) DESC"
            candidate_order_clause = (
                "ORDER BY COALESCE(\n"
                "                        (SELECT MAX(mx.timestamp) FROM messages mx WHERE mx.session_id = s.id),\n"
                "                        s.started_at\n"
                "                    ) DESC,\n"
                "                    s.started_at DESC"
            )

        select_sql = f"""
            SELECT s.id, s.title, s.model, s.message_count,
                   s.started_at, s.source,
                   {session_source_expr},
                   {user_id_expr},
                   {chat_id_expr},
                   {chat_type_expr},
                   {thread_id_expr},
                   {session_key_expr},
                   {origin_chat_id_expr},
                   {origin_user_id_expr},
                   {platform_expr},
                   {parent_expr},
                   {ended_expr},
                   {end_reason_expr},
                   {actual_count_expr} AS actual_message_count,
                   {user_message_count_expr} AS actual_user_message_count,
                   {last_activity_expr} AS last_activity
        """
        if limit is not None:
            result_limit = max(0, int(limit))
            if result_limit == 0:
                return []
            # The sidebar only needs a small visible window. Bound the expensive
            # messages join to a recent-activity candidate set instead of
            # aggregating every historical Hermes state.db session before
            # slicing in Python. The candidate ordering must include the latest
            # message timestamp, not only ``started_at``: long-lived CLI sessions
            # can be resumed days later and should still surface at the top.
            # Oversampling preserves room for hidden compression segments or
            # other rows filtered after projection.
            candidate_limit = max(result_limit * 8, result_limit)
            if latest_messages_cte:
                candidate_cte = (
                    "WITH {latest_messages_cte}, candidates AS (\n"
                    "                    SELECT s.id\n"
                    "                    FROM sessions s\n"
                    "                    LEFT JOIN latest_messages lm ON lm.session_id = s.id\n"
                    "                    WHERE {where_clause}\n"
                    "                    {candidate_order_clause}\n"
                    "                    LIMIT ?\n"
                    "                )"
                ).format(
                    latest_messages_cte=latest_messages_cte,
                    where_clause=" AND ".join(where_clauses),
                    candidate_order_clause=candidate_order_clause,
                )
            else:
                candidate_cte = (
                    "WITH candidates AS (\n"
                    "                    SELECT s.id\n"
                    "                    FROM sessions s\n"
                    "                    WHERE {where_clause}\n"
                    "                    {candidate_order_clause}\n"
                    "                    LIMIT ?\n"
                    "                )"
                ).format(
                    where_clause=" AND ".join(where_clauses),
                    candidate_order_clause=candidate_order_clause,
                )

            cur.execute(
                f"""
                {candidate_cte}
                {select_sql}
                FROM sessions s
                JOIN candidates c ON c.id = s.id
                {join_clause}
                {group_by_clause}
                {order_by_clause}
                """,
                [*params, candidate_limit],
            )
        else:
            cur.execute(
                f"""
                {select_sql}
                FROM sessions s
                {join_clause}
                WHERE {' AND '.join(where_clauses)}
                {group_by_clause}
                {order_by_clause}
                """,
                params,
            )
        projected = _project_agent_session_rows([dict(row) for row in cur.fetchall()])
        projected = [_with_normalized_source(row) for row in projected]
        projected = [row for row in projected if is_cli_session_row_visible(row)]
        if limit is None:
            return projected
        return projected[:max(0, int(limit))]



def _lineage_report_row(row: dict, role: str) -> dict:
    updated_at = row.get('ended_at') if row.get('ended_at') is not None else row.get('started_at')
    return {
        'session_id': row.get('id'),
        'role': role,
        'title': row.get('title'),
        'source': row.get('source'),
        'started_at': row.get('started_at'),
        'updated_at': updated_at,
        'end_reason': row.get('end_reason'),
        'active': row.get('ended_at') is None,
        'archived': False,
    }


def _empty_lineage_report(session_id: str, *, found: bool = False) -> dict:
    return {
        'mutation': False,
        'found': found,
        'session_id': session_id,
        'lineage_key': session_id,
        'tip_session_id': session_id,
        'total_segments': 0,
        'materialized_segments': 0,
        'segments': [],
        'children': [],
        'manual_review': False,
    }


def read_session_lineage_report(db_path: Path, session_id: str | None, max_hops: int = 20) -> dict:
    """Return a bounded, read-only lifecycle report for a session lineage.

    This helper intentionally reports only facts that can be derived from
    ``state.db.sessions`` without mutating WebUI JSON, archiving rows, or
    deleting historical segments. It mirrors the sidebar continuation rules so
    a future UI/PR can explain which rows are hidden compression/cli-close
    segments and which child-session branches remain distinct.
    """
    sid = str(session_id or '').strip()
    if not sid:
        return _empty_lineage_report('')
    db_path = Path(db_path)
    if not db_path.exists():
        return _empty_lineage_report(sid)

    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            session_cols = {row[1] for row in cur.fetchall()}
            required = {'id', 'parent_session_id', 'end_reason'}
            if not required.issubset(session_cols):
                return _empty_lineage_report(sid)

            source_expr = _optional_col('source', session_cols)
            session_source_expr = _optional_col('session_source', session_cols)
            title_expr = _optional_col('title', session_cols)
            started_expr = _optional_col('started_at', session_cols, '0')
            ended_expr = _optional_col('ended_at', session_cols)
            end_reason_expr = _optional_col('end_reason', session_cols)
            parent_expr = _optional_col('parent_session_id', session_cols)

            def fetch_one(row_id: str | None) -> dict | None:
                if not row_id:
                    return None
                cur.execute(
                    f"""
                    SELECT s.id,
                           {source_expr},
                           {session_source_expr},
                           {title_expr},
                           {started_expr},
                           {parent_expr},
                           {ended_expr},
                           {end_reason_expr}
                    FROM sessions s
                    WHERE s.id = ?
                    """,
                    (row_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None

            target = fetch_one(sid)
            if not target:
                return _empty_lineage_report(sid)

            segments = [target]
            current = target
            seen = {sid}
            manual_review = False
            for _hop in range(max(0, int(max_hops))):
                parent_id = current.get('parent_session_id')
                parent = fetch_one(parent_id)
                if not parent or parent_id in seen:
                    manual_review = bool(parent_id and parent_id in seen)
                    break
                if not _is_continuation_session(parent, current):
                    break
                segments.append(parent)
                seen.add(parent_id)
                current = parent
            else:
                manual_review = True

            segment_ids = {row['id'] for row in segments}
            child_rows: list[dict] = []
            parent_ids = [row['id'] for row in segments]
            children_by_parent: dict[str, list[dict]] = {pid: [] for pid in parent_ids}
            if parent_ids:
                placeholders = ','.join('?' * len(parent_ids))
                cur.execute(
                    f"""
                    SELECT s.id,
                           {source_expr},
                           {session_source_expr},
                           {title_expr},
                           {started_expr},
                           {parent_expr},
                           {ended_expr},
                           {end_reason_expr}
                    FROM sessions s
                    WHERE s.parent_session_id IN ({placeholders})
                    """,
                    parent_ids,
                )
                for child_row in cur.fetchall():
                    child = dict(child_row)
                    parent_id = child.get('parent_session_id')
                    if parent_id in children_by_parent:
                        children_by_parent[parent_id].append(child)
            for parent in segments:
                parent_children = children_by_parent.get(parent['id'], [])
                parent_children.sort(key=lambda row: row.get('started_at') or 0, reverse=True)
                for child in parent_children:
                    if child['id'] in segment_ids:
                        continue
                    if _is_continuation_session(parent, child):
                        # A continuation outside the selected path means the
                        # lineage is branched or the caller selected an older
                        # segment. Report manual review rather than proposing
                        # destructive cleanup candidates.
                        manual_review = True
                        continue
                    child_rows.append(child)
    except Exception:
        return _empty_lineage_report(sid)

    root_id = segments[-1]['id'] if segments else sid
    tip_id = segments[0]['id'] if segments else sid
    return {
        'mutation': False,
        'found': True,
        'session_id': sid,
        'lineage_key': root_id,
        'tip_session_id': tip_id,
        'total_segments': len(segments),
        'materialized_segments': len(segments),
        'segments': [
            _lineage_report_row(row, 'tip' if idx == 0 else 'hidden_segment')
            for idx, row in enumerate(segments)
        ],
        'children': [_lineage_report_row(row, 'child_session') for row in child_rows],
        'manual_review': manual_review,
    }


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
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            session_cols = {row[1] for row in cur.fetchall()}
            if 'parent_session_id' not in session_cols or 'end_reason' not in session_cols:
                return {}
            session_source_expr = _optional_col('session_source', session_cols)
            source_expr = _optional_col('source', session_cols)
            message_count_expr = _optional_col('message_count', session_cols, '0')
            # Scoped fetch via PRIMARY KEY + idx_sessions_parent rather than a
            # full table scan. The sessions table grows unbounded over time
            # (1000+ rows is normal, 10000+ for power users), and this function
            # runs on every sidebar refresh — a full SELECT was ~50x slower
            # than the indexed lookup at 1000 rows and scales linearly.
            #
            # Fetch the wanted ids first, then chase parent_session_id chains
            # in batches until no new ids appear. Each batch hits PRIMARY KEY
            # so it's effectively O(N) lookups. Then walk continuation children
            # from the materialized ancestors so branchy compression lineages can
            # mark the real freshest tip, not just the newest direct sibling.
            #
            # IN-clause is chunked to 500 to stay under SQLITE_MAX_VARIABLE_NUMBER
            # on older sqlite (Python 3.9 ships sqlite 3.31 which defaults to 999;
            # newer Python ships sqlite 3.32+ at 32766). On a power user with
            # 2000+ sessions in the sidebar, an unchunked first hop would raise
            # `OperationalError: too many SQL variables`, get swallowed by the
            # except below, and silently disable lineage collapse forever.
            # (Opus pre-release review of v0.50.251, SHOULD-FIX 2.)
            IN_CHUNK = 500
            rows: dict[str, dict] = {}
            to_fetch = set(wanted)
            # Cap walk depth to bound worst-case query count. Real lineage
            # chains seen in production are <10 segments; anything longer is
            # almost certainly pathological data and not worth chasing.
            for _hop in range(20):
                if not to_fetch:
                    break
                fetch_list = list(to_fetch)
                to_fetch = set()
                for i in range(0, len(fetch_list), IN_CHUNK):
                    chunk = fetch_list[i:i + IN_CHUNK]
                    placeholders = ','.join('?' * len(chunk))
                    cur.execute(
                        f"""
                        SELECT s.id, {source_expr}, {session_source_expr}, s.title, s.started_at, s.parent_session_id, s.ended_at, s.end_reason, {message_count_expr}
                        FROM sessions s
                        WHERE s.id IN ({placeholders})
                        """,
                        chunk,
                    )
                    for row in cur.fetchall():
                        rows[row['id']] = dict(row)
                # Queue up parents we haven't fetched yet.
                for sid in fetch_list:
                    parent_id = rows.get(sid, {}).get('parent_session_id')
                    if parent_id and parent_id not in rows and parent_id not in to_fetch:
                        to_fetch.add(parent_id)

            # Fetch descendants from the discovered ancestors using the parent
            # index. This keeps the sidebar read scoped while still giving the
            # collapse metadata enough information to choose the active branch.
            to_expand = set(rows)
            expanded: set[str] = set()
            for _hop in range(20):
                frontier = [sid for sid in to_expand if sid not in expanded]
                if not frontier:
                    break
                to_expand = set()
                for i in range(0, len(frontier), IN_CHUNK):
                    chunk = frontier[i:i + IN_CHUNK]
                    placeholders = ','.join('?' * len(chunk))
                    cur.execute(
                        f"""
                        SELECT s.id, {source_expr}, {session_source_expr}, s.title, s.started_at, s.parent_session_id, s.ended_at, s.end_reason, {message_count_expr}
                        FROM sessions s
                        WHERE s.parent_session_id IN ({placeholders})
                        """,
                        chunk,
                    )
                    for row in cur.fetchall():
                        child = dict(row)
                        rows[child['id']] = child
                        parent_id = child.get('parent_session_id')
                        parent = rows.get(str(parent_id)) if parent_id else None
                        if parent and child['id'] not in expanded and _is_continuation_session(parent, child):
                            to_expand.add(child['id'])
                expanded.update(frontier)

            message_stats: dict[str, dict] = {}
            cur.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'messages'")
            has_messages_table = cur.fetchone() is not None
            # Older/minimal state.db schemas can have a `messages` table WITHOUT a
            # `timestamp` column (or with a non-numeric one). Detect the columns
            # rather than gating on table existence alone: require `session_id`,
            # and only select MAX(timestamp) when that column is actually present
            # so the query can't raise and collapse the whole lineage metadata.
            messages_has_session_id = False
            messages_has_timestamp = False
            if has_messages_table:
                cur.execute("PRAGMA table_info(messages)")
                _message_cols = {row[1] for row in cur.fetchall()}
                messages_has_session_id = 'session_id' in _message_cols
                messages_has_timestamp = 'timestamp' in _message_cols
            use_messages_query = has_messages_table and messages_has_session_id
            row_ids = list(rows)
            if use_messages_query:
                last_at_expr = "MAX(timestamp) AS last_message_at" if messages_has_timestamp else "NULL AS last_message_at"
                for i in range(0, len(row_ids), IN_CHUNK):
                    chunk = row_ids[i:i + IN_CHUNK]
                    placeholders = ','.join('?' * len(chunk))
                    cur.execute(
                        f"""
                        SELECT session_id, COUNT(*) AS actual_message_count, {last_at_expr}
                        FROM messages
                        WHERE session_id IN ({placeholders})
                        GROUP BY session_id
                        """,
                        chunk,
                    )
                    for row in cur.fetchall():
                        message_stats[row['session_id']] = dict(row)
            for sid, row in rows.items():
                stats = message_stats.get(sid) or {}
                if use_messages_query:
                    row['actual_message_count'] = int(stats.get('actual_message_count') or 0)
                else:
                    row['actual_message_count'] = int(row.get('message_count') or 0)
                row['last_message_at'] = stats.get('last_message_at')
    except Exception:
        return {}

    children_by_parent: dict[str, list[dict]] = {}
    for row in rows.values():
        parent_id = row.get('parent_session_id')
        if parent_id:
            children_by_parent.setdefault(parent_id, []).append(row)

    def continuation_root_and_depth(sid: str) -> tuple[str, int]:
        root_id = sid
        current_id = sid
        depth = 1
        seen = {sid}
        while True:
            current = rows.get(current_id)
            raw_parent_id = current.get('parent_session_id') if current else None
            parent_id = str(raw_parent_id) if raw_parent_id else ''
            if not parent_id:
                break
            parent = rows.get(parent_id)
            if not parent or parent_id in seen:
                break
            if not _is_continuation_session(parent, current):
                break
            root_id = parent_id
            current_id = parent_id
            seen.add(parent_id)
            depth += 1
        return root_id, depth

    def freshest_continuation_tip(root_id: str) -> tuple[str, int]:
        best_id = root_id
        best_depth = 1
        segment_count = 0
        best_score = _as_score(rows.get(root_id, {}).get('last_message_at'), rows.get(root_id, {}).get('started_at'))
        stack: list[tuple[str, int]] = [(root_id, 1)]
        seen: set[str] = set()
        while stack:
            current_id, depth = stack.pop()
            if current_id in seen:
                continue
            seen.add(current_id)
            current = rows.get(current_id)
            if not current:
                continue
            segment_count += 1
            actual_count = int(current.get('actual_message_count') or 0)
            score = _as_score(current.get('last_message_at'), current.get('started_at'))
            if actual_count > 0 and (score > best_score or (score == best_score and depth >= best_depth)):
                best_id = current_id
                best_depth = depth
                best_score = score
            for child in children_by_parent.get(current_id, []):
                if _is_continuation_session(current, child):
                    stack.append((child['id'], depth + 1))

        return best_id, max(segment_count, best_depth)

    lineage_tip_cache: dict[str, tuple[str, int]] = {}
    metadata: dict[str, dict] = {}
    for sid in wanted:
        row = rows.get(sid)
        if not row:
            continue

        state_title = str(row.get('title') or '').strip()
        if state_title:
            metadata.setdefault(sid, {})['_state_db_title'] = state_title
        state_source = str(row.get('source') or '').strip().lower()
        if state_source:
            entry = metadata.setdefault(sid, {})
            entry['_state_db_source'] = state_source
            source_meta = normalize_agent_session_source(state_source)
            entry['_state_db_source_tag'] = state_source
            entry['_state_db_raw_source'] = source_meta.get('raw_source')
            entry['_state_db_session_source'] = source_meta.get('session_source')
            entry['_state_db_source_label'] = source_meta.get('source_label')

        parent_id = row.get('parent_session_id')
        parent_row = rows.get(parent_id) if parent_id else None
        if parent_id and parent_row:
            entry = metadata.setdefault(sid, {})
            entry['parent_session_id'] = parent_id
            if not _is_continuation_session(parent_row, row):
                entry['relationship_type'] = 'child_session'
                entry['parent_title'] = parent_row.get('title')
                entry['parent_source'] = parent_row.get('source')
                parent_source = str(parent_row.get('source') or '').strip().lower()
                child_source = str(row.get('source') or '').strip().lower()
                if parent_source and child_source and parent_source != child_source:
                    entry['_cross_surface_child_session'] = True
                parent_root = _continuation_root_id(rows, parent_id)
                if parent_root:
                    entry['_parent_lineage_root_id'] = parent_root
                    if parent_root not in lineage_tip_cache:
                        lineage_tip_cache[parent_root] = freshest_continuation_tip(parent_root)
                    entry['_parent_lineage_tip_id'] = lineage_tip_cache[parent_root][0]
                continue

        root_id, segment_count = continuation_root_and_depth(sid)

        if root_id != sid:
            entry = metadata.setdefault(sid, {})
            entry['_lineage_root_id'] = root_id
            if root_id not in lineage_tip_cache:
                lineage_tip_cache[root_id] = freshest_continuation_tip(root_id)
            tip_id, tip_depth = lineage_tip_cache[root_id]
            entry['_lineage_tip_id'] = tip_id
            entry['_compression_segment_count'] = max(segment_count, tip_depth)

    return metadata
