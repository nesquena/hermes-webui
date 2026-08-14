"""Read-only Codex CLI session bridge.

Codex stores conversations in two places under ``~/.codex``:

* ``state_5.sqlite`` → ``threads`` table: one row per conversation with the
  title, cwd, model, timestamps and a ``rollout_path`` pointing at the
  transcript.
* ``sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``: the transcript itself, one
  JSON object per line tagged with a ``type`` field.

This module mirrors the Claude Code bridge in ``api.models``
(``get_claude_code_sessions`` / ``get_claude_code_session_messages``): the rows
it returns are purely additive, read-only sidebar rows, and every read is
defensive — a missing DB, an unreadable file or a malformed line is skipped
rather than allowed to break WebUI session listing.

Security posture: nothing outside ``~/.codex`` is ever opened. The
``rollout_path`` recorded in the DB is resolved and rejected unless it lands
under ``<codex_home>/sessions``, so a tampered DB row cannot be used to read
arbitrary files. Session ids are ``codex_<uuid>`` and the uuid part is format
validated before it reaches SQLite.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CODEX_SOURCE = 'codex'
CODEX_SOURCE_LABEL = 'Codex'
CODEX_SESSION_ID_PREFIX = f'{CODEX_SOURCE}_'
CODEX_STATE_DB_NAME = 'state_5.sqlite'
CODEX_SESSIONS_DIRNAME = 'sessions'

# Caps mirror the Claude Code bridge so one pathological store can't stall the
# sidebar build or balloon a response.
CODEX_MAX_SESSIONS = 200
# Rollouts larger than this are read from the TAIL only (newest turns), not
# rejected: a Codex conversation must stay visible however long it grows. The
# tail window is sized so the newest ``CODEX_MAX_MESSAGES_PER_FILE`` rendered
# turns — the ones a reader actually wants — fit comfortably even for verbose
# transcripts, while a single parse never unconditionally slurps a multi-GiB
# file into memory.
CODEX_MAX_ROLLOUT_BYTES = 32 * 1024 * 1024
CODEX_TAIL_READ_BYTES = CODEX_MAX_ROLLOUT_BYTES
# Sidebar rows only need a message COUNT, not the parsed content, so each cold
# sidebar scan reads at most this many tail bytes per rollout — bounded
# regardless of how large the transcript has grown. ~96 KiB is enough to hold
# the newest handful of turns (the same ones a reader scanning the sidebar
# cares about) for any realistic line length, and caps a 200-row cold scan at
# ~19 MiB of bounded reads instead of multi-GiB of full parses.
CODEX_SIDEBAR_COUNT_BYTES = 96 * 1024
CODEX_MAX_MESSAGES_PER_FILE = 1000
CODEX_MAX_CONTENT_CHARS = 200_000
CODEX_TITLE_MAX_CHARS = 80

_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)

# Codex replays instruction/context blobs as synthetic ``user`` turns at the top
# of every rollout (AGENTS.md contents, the environment context XML, the
# sandbox permission preamble). They are machine plumbing, not something the
# user typed, so they are dropped from the rendered transcript the same way
# ``developer`` turns are.
#
# Matching is deliberately PRECISE rather than prefix-based: a content part is
# dropped only when, stripped of surrounding whitespace, it is a COMPLETE
# self-contained blob (it starts with the opening token AND ends with the
# matching close). A real user prompt that merely begins with one of these
# tokens — e.g. someone pasting a fragment of AGENTS.md, or asking a question
# that starts with ``<environment_context>`` — continues with their own text,
# lacks the matching close, and is therefore preserved.
_SYNTHETIC_BLOCKS = (
    ('<environment_context>', '</environment_context>'),
    ('<user_instructions>', '</user_instructions>'),
    ('<permissions instructions>', '</permissions instructions>'),
    ('<multi_agent_mode>', '</multi_agent_mode>'),
    ('<honcho-memory', '</honcho-memory>'),
)
# Codex prepends the AGENTS.md body as a header line that immediately wraps the
# instructions in an ``<INSTRUCTIONS>…</INSTRUCTIONS>`` block. Match that whole
# injected shape rather than the bare ``# AGENTS.md instructions`` prefix so a
# user who pastes their own AGENTS.md (or starts a message with that header) is
# not silently dropped.
_AGENTS_MD_INSTRUCTIONS_RE = re.compile(
    r'^# AGENTS\.md instructions\s*\n<INSTRUCTIONS>[\s\S]*</INSTRUCTIONS>\s*\Z'
)

_RENDERED_ROLES = ('user', 'assistant')

_PARSE_CACHE: OrderedDict[tuple, list[dict]] = OrderedDict()
_PARSE_CACHE_LOCK = threading.Lock()
_PARSE_CACHE_MAX = 256


# ── paths ───────────────────────────────────────────────────────────────────


def codex_home(home_dir: Path | str | None = None) -> Path | None:
    """Resolve the Codex home directory without touching real home in tests.

    Mirrors ``_default_claude_code_projects_dir``: an explicit argument wins, then
    ``HERMES_WEBUI_CODEX_HOME``, and a test-state run with neither returns None so
    a test never scans the developer's real ``~/.codex``.
    """
    if home_dir is not None:
        return Path(home_dir).expanduser()
    override = os.getenv('HERMES_WEBUI_CODEX_HOME')
    if override:
        return Path(override).expanduser()
    if os.getenv('HERMES_WEBUI_TEST_STATE_DIR'):
        return None
    return Path.home() / '.codex'


def codex_state_db_path(home_dir: Path | str | None = None) -> Path | None:
    """Absolute path of the Codex ``threads`` SQLite store, or None."""
    root = codex_home(home_dir)
    if root is None:
        return None
    return root / CODEX_STATE_DB_NAME


def codex_state_db_stat_key(home_dir: Path | str | None = None):
    """Cheap (mtime, size) invalidation stamp for the Codex state DB + WAL."""
    db_path = codex_state_db_path(home_dir)
    if db_path is None:
        return None
    parts = []
    for candidate in (db_path, Path(f'{db_path}-wal'), Path(f'{db_path}-shm')):
        try:
            st = candidate.stat()
            parts.append((st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(None)
    return (str(db_path), tuple(parts))


def is_codex_session_id(sid: Any) -> bool:
    """True iff ``sid`` is a well-formed ``codex_<uuid>`` WebUI session id."""
    return thread_id_from_session_id(sid) is not None


def codex_session_id(thread_id: Any) -> str:
    """WebUI session id for a Codex thread uuid."""
    return f'{CODEX_SESSION_ID_PREFIX}{str(thread_id or "").strip()}'


def thread_id_from_session_id(sid: Any) -> str | None:
    """Extract the Codex thread uuid from a session id, or None when invalid.

    Accepts both the prefixed WebUI form (``codex_<uuid>``) and a bare uuid so
    the ``/api/codex/session/<id>`` route can take either.
    """
    text = str(sid or '').strip()
    if not text:
        return None
    if text.startswith(CODEX_SESSION_ID_PREFIX):
        text = text[len(CODEX_SESSION_ID_PREFIX):]
    if not _UUID_RE.match(text):
        return None
    return text.lower()


def _resolve_rollout_path(rollout_path: Any, home: Path) -> Path | None:
    """Validate a DB-recorded rollout path and return it, or None.

    The path must resolve to a regular file under ``<codex_home>/sessions`` and
    must not be reached through a symlink, so a tampered ``threads`` row can
    never point the reader at an arbitrary file on disk.

    A rollout is NEVER rejected for being large: the parser reads the file tail
    (newest turns) when it exceeds ``CODEX_MAX_ROLLOUT_BYTES``, so a long-lived
    Codex conversation stays visible in the sidebar and the viewer instead of
    vanishing once it crosses the byte threshold. See ``_read_rollout_lines``.
    """
    raw = str(rollout_path or '').strip()
    if not raw:
        return None
    try:
        sessions_root = (home / CODEX_SESSIONS_DIRNAME).resolve(strict=False)
        candidate = Path(raw).expanduser()
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=False)
        if resolved != sessions_root and sessions_root not in resolved.parents:
            return None
        if not resolved.is_file():
            return None
    except OSError:
        return None
    return resolved


# ── transcript parsing ──────────────────────────────────────────────────────


def _parse_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Reject NaN/Infinity: a valid epoch is always finite, and emitting a
        # non-finite float would produce invalid JSON (NaN/Infinity are not
        # valid JSON numbers) downstream in the transcript payload.
        if not math.isfinite(value):
            return None
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed if math.isfinite(parsed) else None
    try:
        parsed = datetime.datetime.fromisoformat(
            text.replace('Z', '+00:00')
        ).timestamp()
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


def _extract_text(content: Any) -> str:
    """Flatten a Codex ``payload.content`` list into display text."""
    if content is None:
        return ''
    if isinstance(content, str):
        return content[:CODEX_MAX_CONTENT_CHARS]
    if isinstance(content, dict):
        return _extract_text(content.get('text') or content.get('content'))
    if isinstance(content, list):
        parts: list[str] = []
        used = 0
        for item in content:
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                text = item.get('text') or item.get('content') or ''
            else:
                continue
            if not text:
                continue
            remaining = CODEX_MAX_CONTENT_CHARS - used
            if remaining <= 0:
                break
            chunk = str(text)[:remaining]
            parts.append(chunk)
            used += len(chunk)
        return '\n'.join(parts)
    return str(content)[:CODEX_MAX_CONTENT_CHARS]


def _is_synthetic_user_text(text: str) -> bool:
    """True when ``text`` is a complete Codex-injected context blob.

    A real user turn may legitimately START with one of the injection tokens
    (pasting AGENTS.md for review, quoting an environment block, …) but will
    then carry their own prose after it. We therefore drop a part only when it
    is a COMPLETE self-contained blob — opening token followed, after any body,
    by the matching close — or the exact ``# AGENTS.md instructions`` injection
    shape. Partial / trailing user content is preserved.
    """
    stripped = text.strip()
    if not stripped:
        return False
    for opening, closing_marker in _SYNTHETIC_BLOCKS:
        if stripped.startswith(opening) and stripped.rstrip().endswith(closing_marker):
            return True
    return bool(_AGENTS_MD_INSTRUCTIONS_RE.match(stripped))


def _user_text_without_injected_context(content: Any) -> str:
    """Join a user turn's content parts, dropping Codex-injected context blobs.

    A real user turn can arrive in the same ``content`` list as the AGENTS.md /
    environment-context parts (Codex batches them into one message), so filter
    per part rather than dropping the whole message.
    """
    items = content if isinstance(content, list) else [content]
    kept: list[str] = []
    for item in items:
        text = _extract_text(item)
        if not text.strip() or _is_synthetic_user_text(text):
            continue
        kept.append(text)
    return '\n'.join(kept)


def _rendered_message_from_raw(raw: Any) -> dict[str, Any] | None:
    """Turn one rollout JSONL record into a ``{role, content[, timestamp]}`` dict.

    Returns None when the record is not a renderable user/assistant message
    (session_meta, event_msg, tool traffic, reasoning, developer turns, …) or
    has no non-empty content after dropping injected context.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get('type') != 'response_item':
        return None
    payload = raw.get('payload')
    if not isinstance(payload, dict) or payload.get('type') != 'message':
        return None
    role = str(payload.get('role') or '').strip().lower()
    if role not in _RENDERED_ROLES:
        return None
    if role == 'user':
        content = _user_text_without_injected_context(payload.get('content'))
    else:
        content = _extract_text(payload.get('content'))
    if not content.strip():
        return None
    item: dict[str, Any] = {'role': role, 'content': content}
    ts = _parse_timestamp(raw.get('timestamp') or payload.get('timestamp'))
    if ts is not None:
        item['timestamp'] = ts
    return item


def _iter_rollout_lines(path: Path) -> tuple[Any, bool]:
    """Return ``(line_iter, tail_truncated)`` for a rollout, tail-reading if large.

    ``tail_truncated`` is True when the file exceeded ``CODEX_TAIL_READ_BYTES``
    and only its tail was read — i.e. some older records never reached the
    parser at all. The tail window is newest-last, so the dropped prefix is
    always the oldest turns.

    A mid-line cut at the tail boundary is discarded (the partial first line is
    skipped) so iteration always starts on a complete JSONL record. The returned
    iterator is a plain list iterator (no open file handle) so the caller never
    has to close anything.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return iter(()), False
    read_bytes = min(size, CODEX_TAIL_READ_BYTES)
    tail_truncated = size > CODEX_TAIL_READ_BYTES
    try:
        with path.open('rb') as bf:
            if tail_truncated:
                bf.seek(-read_bytes, os.SEEK_END)
            chunk = bf.read()
    except OSError:
        return iter(()), False
    text = chunk.decode('utf-8', errors='replace')
    if tail_truncated:
        # Drop the bytes before the first newline: they are the back half of a
        # line the head read never saw, so it is not a complete JSONL record.
        nl = text.find('\n')
        if nl >= 0:
            text = text[nl + 1:]
        else:
            text = ''
    return iter(text.splitlines()), tail_truncated


def _parse_codex_rollout_impl(
    path: Path, *, max_messages: int = CODEX_MAX_MESSAGES_PER_FILE
) -> tuple[list[dict], bool]:
    """Parse one rollout JSONL; returns ``(messages, truncated)``.

    ``truncated`` is True when older turns were omitted — either because the
    rollout holds more rendered messages than ``max_messages``, or because the
    file exceeded ``CODEX_TAIL_READ_BYTES`` and only its tail was read. Callers
    decide how to surface it (Detail flags it; the sidebar just caps its
    message_count). In both cases the retained window is the NEWEST turns.
    """
    messages: list[dict] = []
    truncated = False
    line_iter, tail_truncated = _iter_rollout_lines(path)
    try:
        for line in line_iter:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except Exception:
                continue
            item = _rendered_message_from_raw(raw)
            if item is None:
                continue
            if len(messages) >= max_messages:
                truncated = True
            if len(messages) == max_messages:
                messages.pop(0)
            messages.append(item)
    except OSError:
        return [], False
    except Exception:
        logger.debug('Codex rollout parse failed for %s', path, exc_info=True)
        return [], False
    if tail_truncated:
        truncated = True
    return messages, truncated


def parse_codex_rollout(
    path: Path, *, max_messages: int = CODEX_MAX_MESSAGES_PER_FILE
) -> list[dict]:
    """Parse one rollout JSONL into ``{role, content, timestamp}`` messages.

    Only ``response_item`` records whose ``payload.type`` is ``message`` and whose
    role is user/assistant are rendered. ``session_meta``, ``event_msg``,
    ``turn_context``, tool traffic (``function_call``/``function_call_output``),
    reasoning and ``developer``-role turns are skipped.

    The transcript is append-only and grows newest-last, so the most recent
    turns are the tail of the file. To keep a newly-finished conversation's
    latest turns (the ones a reader actually wants) instead of the stale head,
    this keeps the **newest** ``max_messages`` rendered messages.
    """
    messages, _truncated = _parse_codex_rollout_impl(path, max_messages=max_messages)
    return messages



def _parse_codex_rollout_cached_impl(
    path: Path, *, max_messages: int = CODEX_MAX_MESSAGES_PER_FILE
) -> tuple[list[dict], bool]:
    """``_parse_codex_rollout_impl`` memoized on the file's stat signature.

    The sidebar build parses every visible rollout to get a message count, and
    those files are append-only and rarely change between polls, so the stat
    signature makes the warm cost one ``os.stat`` per file. Any append or
    in-place edit moves the signature and misses the cache, so a stale parse is
    not reachable.
    """
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size, st.st_ctime_ns, int(max_messages))
    except OSError:
        return _parse_codex_rollout_impl(path, max_messages=max_messages)

    with _PARSE_CACHE_LOCK:
        hit = _PARSE_CACHE.get(key)
        if hit is not None:
            _PARSE_CACHE.move_to_end(key)
            return (list(hit[0]), hit[1])

    parsed = _parse_codex_rollout_impl(path, max_messages=max_messages)

    with _PARSE_CACHE_LOCK:
        if key not in _PARSE_CACHE:
            _PARSE_CACHE[key] = parsed
            _PARSE_CACHE.move_to_end(key)
            while len(_PARSE_CACHE) > _PARSE_CACHE_MAX:
                _PARSE_CACHE.popitem(last=False)
    return (list(parsed[0]), parsed[1])


def parse_codex_rollout_cached(
    path: Path, *, max_messages: int = CODEX_MAX_MESSAGES_PER_FILE
) -> list[dict]:
    """``parse_codex_rollout`` memoized on the file's (mtime_ns, size, ctime_ns).

    Keeps the historical single-list return shape for sidebar/simple callers.
    """
    messages, _truncated = _parse_codex_rollout_cached_impl(
        path, max_messages=max_messages
    )
    return messages


def parse_codex_rollout_detail_cached(
    path: Path, *, max_messages: int = CODEX_MAX_MESSAGES_PER_FILE
) -> tuple[list[dict], bool]:
    """Like ``parse_codex_rollout_cached`` but also reports ``truncated``.

    Backs the Detail endpoint so a reader of a very long Codex conversation can
    be told the render window dropped the oldest turns.
    """
    return _parse_codex_rollout_cached_impl(path, max_messages=max_messages)


def clear_codex_parse_cache() -> None:
    """Drop all memoized Codex rollout parses (test/lifecycle hook)."""
    with _PARSE_CACHE_LOCK:
        _PARSE_CACHE.clear()
        _COUNT_CACHE.clear()


def _count_codex_rollout_messages_impl(
    path: Path, *, max_bytes: int = CODEX_SIDEBAR_COUNT_BYTES
) -> int:
    """Count renderable user/assistant messages in a rollout's tail, cheaply.

    Bounded by ``max_bytes`` per file so a 200-row cold sidebar scan reads at
    most ~``200 * max_bytes`` regardless of transcript size. The count is the
    number of newest rendered messages within that tail window; for sessions
    longer than the window it is therefore a lower bound on the true count,
    which is exactly what a sidebar badge needs (the viewer still loads the full
    newest-1000 window on open). Reuses ``_rendered_message_from_raw`` so the
    count matches what the real parser would render.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    read_bytes = min(size, max_bytes)
    try:
        with path.open('rb') as bf:
            if size > max_bytes:
                bf.seek(-read_bytes, os.SEEK_END)
            chunk = bf.read()
    except OSError:
        return 0
    text = chunk.decode('utf-8', errors='replace')
    if size > max_bytes:
        nl = text.find('\n')
        text = text[nl + 1:] if nl >= 0 else ''
    count = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except Exception:
            continue
        if _rendered_message_from_raw(raw) is not None:
            count += 1
    return count


_COUNT_CACHE: OrderedDict[tuple, int] = OrderedDict()
_COUNT_CACHE_LOCK = threading.Lock()
_COUNT_CACHE_MAX = 512


def count_codex_rollout_messages_cached(
    path: Path, *, max_bytes: int = CODEX_SIDEBAR_COUNT_BYTES
) -> int:
    """``_count_codex_rollout_messages_impl`` memoized on the file's stat signature.

    A warm sidebar poll resolves each row's message_count with one ``os.stat``
    instead of re-reading the tail; any append moves the signature and misses.
    """
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size, st.st_ctime_ns, int(max_bytes))
    except OSError:
        return _count_codex_rollout_messages_impl(path, max_bytes=max_bytes)

    with _COUNT_CACHE_LOCK:
        hit = _COUNT_CACHE.get(key)
        if hit is not None:
            _COUNT_CACHE.move_to_end(key)
            return hit

    count = _count_codex_rollout_messages_impl(path, max_bytes=max_bytes)

    with _COUNT_CACHE_LOCK:
        if key not in _COUNT_CACHE:
            _COUNT_CACHE[key] = count
            _COUNT_CACHE.move_to_end(key)
            while len(_COUNT_CACHE) > _COUNT_CACHE_MAX:
                _COUNT_CACHE.popitem(last=False)
    return count


# ── threads table ───────────────────────────────────────────────────────────

_THREAD_COLUMNS = (
    'id',
    'rollout_path',
    'created_at',
    'updated_at',
    'source',
    'cwd',
    'title',
    'name',
    'preview',
    'first_user_message',
    'model',
    'model_provider',
    'git_branch',
    'git_origin_url',
    'archived',
)


def _read_thread_rows(
    db_path: Path,
    *,
    limit: int,
    thread_id: str | None = None,
) -> list[dict]:
    """Read ``threads`` rows read-only, tolerating older schemas.

    Columns are intersected with what the DB actually has so a Codex version
    that predates (or renames) e.g. ``preview``/``name`` degrades to fewer
    fields instead of raising.
    """
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=0.5)
    except Exception:
        return []
    try:
        conn.row_factory = sqlite3.Row
        with closing(conn):
            conn.execute('PRAGMA busy_timeout=500')
            available = {
                str(row[1])
                for row in conn.execute('PRAGMA table_info(threads)').fetchall()
            }
            if 'id' not in available or 'rollout_path' not in available:
                return []
            columns = [c for c in _THREAD_COLUMNS if c in available]
            select = ', '.join(f'"{c}"' for c in columns)
            order = 'updated_at' if 'updated_at' in available else 'created_at'
            if thread_id is not None:
                sql = f'SELECT {select} FROM threads WHERE id = ? LIMIT 1'
                params: tuple = (thread_id,)
            else:
                where = ' WHERE COALESCE(archived, 0) = 0' if 'archived' in available else ''
                sql = (
                    f'SELECT {select} FROM threads{where} '
                    f'ORDER BY {order} DESC, id DESC LIMIT ?'
                )
                params = (int(limit),)
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except Exception:
        logger.debug('Codex threads read failed for %s', db_path, exc_info=True)
        return []


def _thread_title(row: dict) -> str:
    for key in ('name', 'title', 'preview', 'first_user_message'):
        text = ' '.join(str(row.get(key) or '').split())
        if text:
            return text[:CODEX_TITLE_MAX_CHARS]
    return 'Codex Session'


def _thread_timestamp(row: dict, key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sidebar_row(row: dict, message_count: int, workspace: str) -> dict:
    created_at = _thread_timestamp(row, 'created_at')
    updated_at = _thread_timestamp(row, 'updated_at') or created_at
    return {
        'session_id': codex_session_id(row.get('id')),
        'title': _thread_title(row),
        'workspace': workspace,
        'model': str(row.get('model') or '') or 'codex',
        'model_provider': str(row.get('model_provider') or '') or None,
        'message_count': int(message_count or 0),
        'created_at': created_at,
        'updated_at': updated_at,
        'last_message_at': updated_at,
        'pinned': False,
        'archived': False,
        'project_id': None,
        'profile': None,
        'source_tag': CODEX_SOURCE,
        'raw_source': CODEX_SOURCE,
        'session_source': 'external_agent',
        'source_label': CODEX_SOURCE_LABEL,
        'is_cli_session': True,
        'read_only': True,
        'cwd': str(row.get('cwd') or '') or None,
        'git_branch': str(row.get('git_branch') or '') or None,
    }


def get_codex_sessions(
    home_dir: Path | str | None = None,
    *,
    max_sessions: int = CODEX_MAX_SESSIONS,
    default_workspace: str | None = None,
) -> list[dict]:
    """Read Codex threads as read-only external-agent sidebar rows.

    Returns ``[]`` when Codex is not installed, the state DB is missing, or
    anything goes wrong — the bridge is additive and never raises into the
    session-list build.
    """
    home = codex_home(home_dir)
    if home is None:
        return []
    db_path = codex_state_db_path(home_dir if home_dir is not None else home)
    if db_path is None:
        return []
    try:
        if not db_path.is_file():
            return []
    except OSError:
        return []

    sessions: list[dict] = []
    for row in _read_thread_rows(db_path, limit=max_sessions):
        if thread_id_from_session_id(row.get('id')) is None:
            continue
        path = _resolve_rollout_path(row.get('rollout_path'), home)
        if path is None:
            continue
        # Sidebar rows only need a message COUNT, so use the bounded tail
        # counter (a few KiB per file) instead of a full parse of up to 1000
        # messages per rollout. A cold scan of the full sidebar thus stays
        # bounded regardless of how large individual transcripts have grown.
        message_count = count_codex_rollout_messages_cached(path)
        if message_count <= 0:
            # Bounded read saw no rendered message: confirm with a full parse so
            # a short session whose only turn sits just past the tail window is
            # still listed (and so genuinely-empty rollouts are skipped).
            if not parse_codex_rollout_cached(path):
                continue
        workspace = str(row.get('cwd') or '') or (default_workspace or '')
        sessions.append(_sidebar_row(row, message_count, workspace))
    sessions.sort(
        key=lambda s: s.get('last_message_at') or s.get('updated_at') or 0,
        reverse=True,
    )
    return sessions


def get_codex_session_messages_and_truncated(
    sid: Any, home_dir: Path | str | None = None
) -> tuple[list[dict], bool]:
    """Return ``(messages, truncated)`` for one read-only Codex session.

    ``truncated`` is True when older turns were omitted (the rollout held more
    than ``CODEX_MAX_MESSAGES_PER_FILE`` rendered messages, or exceeded the tail
    read window). The real WebUI viewer path (``import_cli`` → ``GET /api/session``)
    uses this to surface an "earlier turns omitted" notice, since that path does
    not go through ``GET /api/codex/session/<id>`` where ``truncated`` already
    lives.
    """
    thread_id = thread_id_from_session_id(sid)
    if thread_id is None:
        return [], False
    home = codex_home(home_dir)
    if home is None:
        return [], False
    db_path = codex_state_db_path(home_dir if home_dir is not None else home)
    if db_path is None:
        return [], False
    try:
        if not db_path.is_file():
            return [], False
    except OSError:
        return [], False
    rows = _read_thread_rows(db_path, limit=1, thread_id=thread_id)
    if not rows:
        return [], False
    path = _resolve_rollout_path(rows[0].get('rollout_path'), home)
    if path is None:
        return [], False
    return parse_codex_rollout_detail_cached(path)


def get_codex_session_messages(sid: Any, home_dir: Path | str | None = None) -> list[dict]:
    """Return the rendered messages for one read-only Codex session.

    Note: ``get_codex_session_detail`` is the primary entry point and also
    reports ``truncated``.  This helper returns only the message list for
    callers that do not need the truncation flag; callers on the real viewer
    path that DO need it should use
    :func:`get_codex_session_messages_and_truncated`.
    """
    messages, _truncated = get_codex_session_messages_and_truncated(sid, home_dir)
    return messages


def get_codex_session_detail(
    sid: Any, home_dir: Path | str | None = None
) -> dict | None:
    """Metadata + messages for one Codex session, or None when unknown.

    Backs ``GET /api/codex/session/<id>``.
    """
    thread_id = thread_id_from_session_id(sid)
    if thread_id is None:
        return None
    home = codex_home(home_dir)
    if home is None:
        return None
    db_path = codex_state_db_path(home_dir if home_dir is not None else home)
    if db_path is None or not db_path.is_file():
        return None
    rows = _read_thread_rows(db_path, limit=1, thread_id=thread_id)
    if not rows:
        return None
    row = rows[0]
    path = _resolve_rollout_path(row.get('rollout_path'), home)
    if path is not None:
        messages, truncated = parse_codex_rollout_detail_cached(path)
    else:
        messages, truncated = [], False
    detail = _sidebar_row(row, len(messages), str(row.get('cwd') or ''))
    detail['messages'] = messages
    detail['truncated'] = bool(truncated)
    detail['source'] = str(row.get('source') or '') or None
    detail['first_user_message'] = str(row.get('first_user_message') or '') or None
    detail['git_origin_url'] = str(row.get('git_origin_url') or '') or None
    return detail
