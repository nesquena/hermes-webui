"""Hermes Web UI -- Session model and in-memory session store."""
import collections
import json
import logging
import os
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

import api.config as _cfg
from api.config import (
    SESSION_DIR, SESSION_INDEX_FILE, SESSIONS, SESSIONS_MAX,
    LOCK, STREAMS, STREAMS_LOCK, DEFAULT_WORKSPACE, DEFAULT_MODEL, PROJECTS_FILE, HOME,
    get_effective_default_model, _get_session_agent_lock,
)
from api.workspace import get_last_workspace
from api.agent_sessions import read_importable_agent_session_rows, read_session_lineage_metadata

logger = logging.getLogger(__name__)
CLI_VISIBLE_SESSION_LIMIT = 20

# ---------------------------------------------------------------------------
# Stale temp-file cleanup
# ---------------------------------------------------------------------------
# Both Session.save() and _write_session_index() use the atomic-write pattern:
#   write to  <path>.tmp.<pid>.<tid>  →  os.replace() to final path
# If the process crashes between write and replace the .tmp file is left
# behind.  Because the name embeds pid + tid, leftover files can never be
# reused by a different process/thread, so they are safe to remove on the
# next startup.  _cleanup_stale_tmp_files() is called from the full-rebuild
# path of _write_session_index (i.e. at first index access / startup) and
# removes any *.tmp.* file whose mtime is older than one hour.
# ---------------------------------------------------------------------------

_STALE_TMP_AGE_SECONDS = 3600  # 1 hour

# Serializes index writers so concurrent Session.save() calls cannot race on
# stale baselines while still allowing LOCK to be released before disk I/O.
_INDEX_WRITE_LOCK = threading.RLock()


def _cleanup_stale_tmp_files() -> None:
    """Best-effort removal of stale ``*.tmp.*`` files from SESSION_DIR.

    Only files whose mtime is older than ``_STALE_TMP_AGE_SECONDS`` are
    removed so that in-flight writes from a long-running sibling process
    are not disturbed.  Errors are logged and swallowed — this must never
    prevent startup.
    """
    cutoff = time.time() - _STALE_TMP_AGE_SECONDS
    try:
        for p in SESSION_DIR.glob('*.tmp.*'):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    logger.debug("Cleaned up stale tmp file: %s", p.name)
            except OSError:
                pass  # best-effort
    except Exception:
        pass  # SESSION_DIR may not exist yet; that's fine


def _index_entry_exists(session_id: str, in_memory_ids=None) -> bool:
    """Return True if an index entry still has backing state.

    A session can legitimately exist either as a persisted JSON file or as an
    in-memory Session object that has not been flushed yet.  This helper is used
    to prune stale `_index.json` rows left behind after session-id rotation or
    file removal.
    """
    if not session_id:
        return False
    if in_memory_ids is None:
        with LOCK:
            in_memory_ids = set(SESSIONS.keys())
    if session_id in in_memory_ids:
        return True
    p = SESSION_DIR / f'{session_id}.json'
    return p.exists()


def _write_session_index(updates=None):
    """Update the session index file.

    When *updates* is provided (a list of Session objects whose compact
    entries should be refreshed), this does a targeted in-place update of
    the existing index — O(1) for single-session changes.  When *updates*
    is None, a full rebuild is performed (used on startup / first call).

    LOCK protects in-memory state snapshots and payload construction only;
    disk I/O (write/flush/fsync/replace) always runs outside LOCK.
    """
    _tmp = SESSION_INDEX_FILE.with_suffix(f'.tmp.{os.getpid()}.{threading.current_thread().ident}')

    with _INDEX_WRITE_LOCK:
        # Lazy full-rebuild path — used when index doesn't exist yet.
        if updates is None or not SESSION_INDEX_FILE.exists():
            _cleanup_stale_tmp_files()  # best-effort sweep on startup / first call
            entries = []
            for p in SESSION_DIR.glob('*.json'):
                if p.name.startswith('_'):
                    continue
                try:
                    s = Session.load(p.stem)
                    if s:
                        entries.append(s.compact())
                except Exception:
                    logger.debug("Failed to load session from %s", p)

            with LOCK:
                existing_ids = {e.get('session_id') for e in entries}
                for s in SESSIONS.values():
                    if s.session_id not in existing_ids:
                        entries.append(s.compact())
                entries.sort(key=lambda s: s.get('updated_at', 0), reverse=True)
                _payload = json.dumps(entries, ensure_ascii=False, indent=2)

            try:
                with open(_tmp, 'w', encoding='utf-8') as f:
                    f.write(_payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(_tmp, SESSION_INDEX_FILE)
            except Exception:
                # Best-effort cleanup of stale tmp on failure
                try:
                    _tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            return

        # Fast path: patch existing index with updated sessions.
        # This avoids loading every session file on every single save().
        _fallback = False
        try:
            with LOCK:
                existing = json.loads(SESSION_INDEX_FILE.read_text(encoding='utf-8'))
                in_memory_ids = set(SESSIONS.keys())

                # Avoid N filesystem exists() checks under LOCK by collecting
                # on-disk IDs once.
                on_disk_ids = {
                    p.stem
                    for p in SESSION_DIR.glob('*.json')
                    if not p.name.startswith('_')
                }

                existing = [
                    e for e in existing
                    if (e.get('session_id') in in_memory_ids or e.get('session_id') in on_disk_ids)
                ]

                # Build lookup of updated entries
                updated_map = {s.session_id: s.compact() for s in updates}
                existing_ids = {e.get('session_id') for e in existing}
                # Add any updated entries not yet in the index
                for sid, entry in updated_map.items():
                    if sid not in existing_ids:
                        existing.append(entry)
                # Replace matching entries in-place
                for i, e in enumerate(existing):
                    sid = e.get('session_id')
                    if sid in updated_map:
                        existing[i] = updated_map[sid]
                existing.sort(key=lambda s: s.get('updated_at', 0), reverse=True)
                _payload = json.dumps(existing, ensure_ascii=False, indent=2)

            try:
                with open(_tmp, 'w', encoding='utf-8') as f:
                    f.write(_payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(_tmp, SESSION_INDEX_FILE)
            except Exception:
                try:
                    _tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
        except Exception:
            _fallback = True

    if _fallback:
        # Corrupt or missing index — fall back to full rebuild (called outside LOCK to avoid deadlock)
        _write_session_index(updates=None)


def _active_stream_ids():
    with STREAMS_LOCK:
        return set(STREAMS.keys())


def _is_streaming_session(active_stream_id, active_stream_ids):
    return bool(active_stream_id and active_stream_id in active_stream_ids)

def _session_sort_timestamp(session):
    if isinstance(session, dict):
        return session.get('last_message_at') or session.get('updated_at') or 0
    return _last_message_timestamp(getattr(session, 'messages', None)) or getattr(session, 'updated_at', 0) or 0


def _message_timestamp(message):
    if not isinstance(message, dict):
        return None
    raw = message.get('_ts') or message.get('timestamp')
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _last_message_timestamp(messages):
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get('role') == 'tool':
            continue
        ts = _message_timestamp(message)
        if ts:
            return ts
    return None


def _message_role(message):
    if not isinstance(message, dict):
        return ''
    return str(message.get('role', '')).strip().lower()


def _find_top_level_json_key(text, key):
    """Return the byte offset of a top-level JSON object key, if present."""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            start = i
            i += 1
            escaped = False
            chars = []
            while i < n:
                c = text[i]
                if escaped:
                    chars.append(c)
                    escaped = False
                elif c == '\\':
                    escaped = True
                elif c == '"':
                    break
                else:
                    chars.append(c)
                i += 1
            if i >= n:
                return None
            if depth == 1 and ''.join(chars) == key:
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                if j < n and text[j] == ':':
                    return start
        elif ch in '{[':
            depth += 1
        elif ch in '}]':
            depth -= 1
        i += 1
    return None


def _read_metadata_json_prefix(path, max_prefix_bytes=65536):
    """Read only the metadata portion before the top-level messages array."""
    buf = ''
    with open(path, 'r', encoding='utf-8') as f:
        while len(buf.encode('utf-8')) < max_prefix_bytes:
            chunk = f.read(4096)
            if not chunk:
                return None
            buf += chunk
            messages_pos = _find_top_level_json_key(buf, 'messages')
            if messages_pos is None:
                continue
            prefix = buf[:messages_pos].rstrip()
            if prefix.endswith(','):
                prefix = prefix[:-1].rstrip()
            return f'{prefix}\n}}'
    return None


def _lookup_index_message_count(session_id):
    """Return the indexed message count without loading the full session file."""
    try:
        entries = json.loads(SESSION_INDEX_FILE.read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if entry.get('session_id') != session_id:
            continue
        count = entry.get('message_count')
        if isinstance(count, int) and count >= 0:
            return count
        try:
            count = int(count)
        except (TypeError, ValueError):
            return None
        return count if count >= 0 else None
    return None


class Session:
    def __init__(self, session_id: str=None, title: str='Untitled',
                 workspace=str(DEFAULT_WORKSPACE), model=DEFAULT_MODEL,
                 model_provider=None,
                 messages=None, created_at=None, updated_at=None,
                 tool_calls=None, pinned: bool=False, archived: bool=False,
                 project_id: str=None, profile=None,
                 input_tokens: int=0, output_tokens: int=0, estimated_cost=None,
                 personality=None,
                 active_stream_id: str=None,
                 pending_user_message: str=None,
                 pending_attachments=None,
                 pending_started_at=None,
                 context_messages=None,
                 compression_anchor_visible_idx=None,
                 compression_anchor_message_key=None,
                 context_length=None, threshold_tokens=None,
                 last_prompt_tokens=None,
                parent_session_id: str=None,
                enabled_toolsets=None,
                **kwargs):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.title = title
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.model = model
        self.model_provider = str(model_provider).strip().lower() if model_provider else None
        self.messages = messages or []
        self.tool_calls = tool_calls or []
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or time.time()
        self.pinned = bool(pinned)
        self.archived = bool(archived)
        self.project_id = project_id or None
        self.profile = profile
        self.input_tokens = input_tokens or 0
        self.output_tokens = output_tokens or 0
        self.estimated_cost = estimated_cost
        self.personality = personality
        self.active_stream_id = active_stream_id
        self.pending_user_message = pending_user_message
        self.pending_attachments = pending_attachments or []
        self.pending_started_at = pending_started_at
        self.context_messages = context_messages if isinstance(context_messages, list) else []
        self.compression_anchor_visible_idx = compression_anchor_visible_idx
        self.compression_anchor_message_key = compression_anchor_message_key
        self.context_length = context_length
        self.threshold_tokens = threshold_tokens
        self.last_prompt_tokens = last_prompt_tokens
        self.parent_session_id = parent_session_id
        self.is_cli_session = bool(kwargs.get('is_cli_session', False))
        self.source_tag = kwargs.get('source_tag')
        self.raw_source = kwargs.get('raw_source')
        self.session_source = kwargs.get('session_source')
        self.source_label = kwargs.get('source_label')
        self.enabled_toolsets = enabled_toolsets  # List[str] or None — per-session toolset override
        self._metadata_message_count = None

    @property
    def path(self):
        return SESSION_DIR / f'{self.session_id}.json'

    def save(self, touch_updated_at: bool = True, skip_index: bool = False) -> None:
        # ── #1558 P0 guard ──────────────────────────────────────────────
        # Refuse to save a session that was loaded with metadata_only=True.
        # Such sessions have messages=[] (it's the whole point of the partial
        # load), and save() unconditionally writes self.messages to disk via
        # an atomic os.replace(). Saving a metadata-only stub thus wipes the
        # full conversation history — which is exactly the v0.50.279
        # _clear_stale_stream_state() regression that lost users 1000+
        # message conversations. Any caller that needs to mutate persisted
        # fields on a metadata-only session must reload with
        # metadata_only=False first.
        if getattr(self, '_loaded_metadata_only', False):
            raise RuntimeError(
                f"Refusing to save metadata-only session {self.session_id!r}: "
                f"would atomically overwrite on-disk messages with []. "
                f"Reload with metadata_only=False before mutating state. "
                f"See #1558."
            )
        if touch_updated_at:
            self.updated_at = time.time()
        # Write metadata fields first so load_metadata_only() can read them
        # without parsing the full messages array (which may be 400KB+).
        # Fields are listed in the order they should appear in the JSON file.
        METADATA_FIELDS = [
            'session_id', 'title', 'workspace', 'model', 'model_provider', 'created_at', 'updated_at',
            'pinned', 'archived', 'project_id', 'profile',
            'input_tokens', 'output_tokens', 'estimated_cost',
            'personality', 'active_stream_id',
            'pending_user_message', 'pending_attachments', 'pending_started_at',
            'compression_anchor_visible_idx', 'compression_anchor_message_key',
            'context_length', 'threshold_tokens', 'last_prompt_tokens',
            'parent_session_id',
            'is_cli_session', 'source_tag', 'raw_source', 'session_source', 'source_label',
            'enabled_toolsets',
        ]
        meta = {k: getattr(self, k, None) for k in METADATA_FIELDS}
        meta['messages'] = self.messages
        meta['tool_calls'] = self.tool_calls
        # Fields not in METADATA_FIELDS (e.g. last_usage, message_count) go at the end
        extra = {k: v for k, v in self.__dict__.items()
                 if k not in METADATA_FIELDS and k not in ('messages', 'tool_calls')
                 and not k.startswith('_')}
        payload = json.dumps({**meta, **extra}, ensure_ascii=False, indent=2)

        # ── #1558 backup safeguard ──────────────────────────────────────
        # Before overwriting the session file, copy the previous version to
        # ``<sid>.json.bak`` IFF the previous file has more messages than the
        # incoming payload. The asymmetric guard means:
        #   * Normal grow-the-conversation saves never produce a backup
        #     (incoming messages >= existing) — keeps disk overhead near zero.
        #   * Any save that would shrink the messages array (the failure mode
        #     of #1558, plus anything similar in the future) leaves a recoverable
        #     snapshot of the pre-shrink state on disk.
        # The recovery path is api/session_recovery.py — at server startup and
        # via /api/session/recover, sessions whose JSON has fewer messages than
        # their .bak get restored automatically.
        try:
            if self.path.exists():
                existing_text = self.path.read_text(encoding='utf-8')
                try:
                    existing = json.loads(existing_text)
                    existing_msg_count = len(existing.get('messages') or [])
                except (json.JSONDecodeError, ValueError):
                    existing_msg_count = -1  # corrupt → always back up
                incoming_msg_count = len(self.messages or [])
                if existing_msg_count > incoming_msg_count:
                    bak_path = self.path.with_suffix('.json.bak')
                    # SHOULD-FIX #2 (Opus): atomic write via tmp+replace,
                    # mirroring the main save() pattern below. Prevents a
                    # torn .bak from a crash mid-write or a concurrent
                    # backup-producing save. Recovery defends against a
                    # torn .bak (JSONDecodeError → no_action), so the
                    # failure mode pre-fix was "backup is lost"; with
                    # this fix the backup either lands cleanly or doesn't
                    # land at all.
                    try:
                        bak_tmp = bak_path.with_suffix(
                            f'.bak.tmp.{os.getpid()}.{threading.current_thread().ident}'
                        )
                        with open(bak_tmp, 'w', encoding='utf-8') as bf:
                            bf.write(existing_text)
                            bf.flush()
                            os.fsync(bf.fileno())
                        os.replace(bak_tmp, bak_path)
                    except OSError:
                        # Backup is best-effort; main save proceeds regardless.
                        try:
                            bak_tmp.unlink(missing_ok=True)
                        except Exception:
                            pass
        except OSError:
            pass

        tmp = self.path.with_suffix(f'.tmp.{os.getpid()}.{threading.current_thread().ident}')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        if not skip_index:
            _write_session_index(updates=[self])

    @classmethod
    def load(cls, sid):
        # Validate session ID format to prevent path traversal
        if not sid or not all(c in '0123456789abcdefghijklmnopqrstuvwxyz_' for c in sid):
            return None
        p = SESSION_DIR / f'{sid}.json'
        if not p.exists():
            return None
        return cls(**json.loads(p.read_text(encoding='utf-8')))

    @classmethod
    def load_metadata_only(cls, sid):
        """Load only the compact metadata fields, skipping the messages array.

        Session JSON files have metadata fields (session_id, title, model, etc.)
        at the top level, before the large messages array. Read only up to the
        top-level "messages" field and synthesize a small metadata-only object.
        Falls back to load() for legacy or unexpected file layouts.
        """
        if not sid or not all(c in '0123456789abcdefghijklmnopqrstuvwxyz_' for c in sid):
            return None
        p = SESSION_DIR / f'{sid}.json'
        if not p.exists():
            return None
        try:
            prefix = _read_metadata_json_prefix(p)
            if not prefix:
                return cls.load(sid)
            parsed = json.loads(prefix)
            needed = {'session_id', 'title', 'created_at', 'updated_at'}
            if not needed.issubset(parsed.keys()):
                return cls.load(sid)
            parsed['messages'] = []
            parsed['tool_calls'] = []
            session = cls(**parsed)
            session._metadata_message_count = _lookup_index_message_count(sid)
            # Mark this session as a metadata-only stub. save() refuses to write
            # such a session because doing so would atomically replace the
            # on-disk JSON with messages=[], wiping the conversation. Any
            # caller that needs to mutate persisted state on a metadata-only
            # session must reload it with metadata_only=False first.
            # See #1558 — v0.50.279 _clear_stale_stream_state() data-loss bug.
            session._loaded_metadata_only = True
            return session
        except Exception:
            # Corrupt prefix or decode error — fall back to full load
            return cls.load(sid)

    def compact(self, include_runtime=False, active_stream_ids=None) -> dict:
        active_stream_ids = active_stream_ids if active_stream_ids is not None else set()
        has_pending_user_message = bool(self.pending_user_message)
        message_count = (
            self._metadata_message_count
            if self._metadata_message_count is not None
            else len(self.messages)
        )
        if has_pending_user_message:
            message_count = max(message_count, 1)
        last_message_at = _last_message_timestamp(self.messages) or self.updated_at
        if has_pending_user_message and self.pending_started_at:
            last_message_at = self.pending_started_at
        return {
            'session_id': self.session_id,
            'title': self.title,
            'workspace': self.workspace,
            'model': self.model,
            'model_provider': self.model_provider,
            'message_count': message_count,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'last_message_at': last_message_at,
            'pinned': self.pinned,
            'archived': self.archived,
            'project_id': self.project_id,
            'profile': self.profile,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'estimated_cost': self.estimated_cost,
            'personality': self.personality,
            'compression_anchor_visible_idx': self.compression_anchor_visible_idx,
            'compression_anchor_message_key': self.compression_anchor_message_key,
            'context_length': self.context_length,
            'threshold_tokens': self.threshold_tokens,
            'last_prompt_tokens': self.last_prompt_tokens,
            # Only emit 'parent_session_id' when set (the /branch fork link, #1342).
            # Sessions without a fork must not leak None — see test_session_lineage_metadata_api.
            **({'parent_session_id': self.parent_session_id} if self.parent_session_id else {}),
            'user_message_count': sum(
                1 for message in self.messages if _message_role(message) == 'user'
            ) if isinstance(self.messages, list) else 0,
            'active_stream_id': self.active_stream_id,
            'pending_user_message': self.pending_user_message,
            'has_pending_user_message': has_pending_user_message,
            'is_cli_session': self.is_cli_session,
            'source_tag': self.source_tag,
            'raw_source': self.raw_source,
            'session_source': self.session_source,
            'source_label': self.source_label,
            'enabled_toolsets': self.enabled_toolsets,
            'is_streaming': _is_streaming_session(
                self.active_stream_id, active_stream_ids
            ) if include_runtime else False,
        }

def _get_profile_home(profile) -> Path:
    """Resolve the hermes agent home directory for the given profile.

    Prefers the profile-specific helper from api.profiles; falls back to the
    HERMES_HOME environment variable or ~/.hermes, expanding ~ correctly.
    """
    try:
        from api.profiles import get_hermes_home_for_profile
        return Path(get_hermes_home_for_profile(profile))
    except ImportError:
        return Path(os.environ.get('HERMES_HOME') or '~/.hermes').expanduser()


def _apply_core_sync_or_error_marker(
    session,
    core_path,
    stream_id_for_recheck=None,
    *,
    require_stream_dead=True,
) -> bool:
    """Inner repair logic. Must be called with the per-session lock already held.

    Re-checks session state under the lock, then either syncs messages from the
    core transcript (if present and non-empty) or restores the pending user
    message as a recovered user turn and appends an error marker.

    stream_id_for_recheck: when provided, repair bails if session.active_stream_id
    changed (e.g. context compression rotated it).  The cache-miss repair path
    also requires the stream to be absent from active streams; the streaming
    thread's final fallback passes require_stream_dead=False because it runs
    before its own stream is removed from STREAMS.

    Returns True if repair was applied, False if the re-check bailed out.
    Must never raise — caller is responsible for exception handling.
    """
    sid = session.session_id
    # Bail if pending is unset — nothing to repair.
    if not session.pending_user_message:
        return False
    if stream_id_for_recheck is not None:
        # Bail if active_stream_id rotated between the pre-lock check and now.
        # Cache-miss repair must also skip if the stream is alive again, but the
        # streaming thread's final fallback runs before removing its own stream
        # from STREAMS and must be allowed to repair that same active stream.
        if session.active_stream_id != stream_id_for_recheck:
            return False
        if require_stream_dead and session.active_stream_id in _active_stream_ids():
            return False

    # When messages is already non-empty, do not overwrite history from any core
    # transcript. The pending user turn may still be the only durable copy of a
    # prompt submitted just before a server restart, so materialize it before
    # clearing runtime stream state.
    if len(session.messages) != 0:
        _pending_text = " ".join(str(session.pending_user_message or "").split())
        _already_checkpointed = False
        if _pending_text and session.messages:
            _last_msg = session.messages[-1]
            if isinstance(_last_msg, dict) and _last_msg.get('role') == 'user':
                _last_text = " ".join(str(_last_msg.get('content') or "").split())
                _already_checkpointed = _last_text == _pending_text
        _recovered_ts = int(time.time())
        if isinstance(session.pending_started_at, (int, float)) and session.pending_started_at > 0:
            _recovered_ts = int(session.pending_started_at)
        if not _already_checkpointed:
            recovered = {
                'role': 'user',
                'content': session.pending_user_message,
                'timestamp': _recovered_ts,
                '_recovered': True,
            }
            if session.pending_attachments:
                recovered['attachments'] = list(session.pending_attachments)
            session.messages.append(recovered)
        session.active_stream_id = None
        session.pending_user_message = None
        session.pending_attachments = []
        session.pending_started_at = None
        session.messages.append({
            'role': 'assistant',
            'content': '**Previous turn did not complete.**',
            'timestamp': int(time.time()),
            '_error': True,
        })
        session.save()
        logger.info(
            "Session %s: recovered pending user turn (messages non-empty), added error marker",
            sid,
        )
        return True

    # ── messages *is* empty ─ full repair ─────────────────────────────────

    if core_path.exists():
        with open(core_path, encoding='utf-8') as f:
            core = json.load(f)
        core_messages = core.get('messages', [])
        if core_messages:
            session.messages = core_messages
            session.tool_calls = core.get('tool_calls', [])
            for field in ('input_tokens', 'output_tokens', 'estimated_cost'):
                if core.get(field) is not None:
                    setattr(session, field, core[field])
            session.active_stream_id = None
            session.pending_user_message = None
            session.pending_attachments = []
            session.pending_started_at = None
            session.save()
            logger.info(
                "Session %s: synced %d messages from core transcript",
                sid, len(core_messages),
            )
            return True

    # Core missing or empty — restore the pending user message as a recovered
    # user turn (preserving the draft), then append an error marker.
    if session.pending_user_message:
        # Use the original send time if available so the recovered turn
        # appears in the correct chronological position.
        _recovered_ts = int(time.time())
        if isinstance(session.pending_started_at, (int, float)) and session.pending_started_at > 0:
            _recovered_ts = int(session.pending_started_at)
        recovered: dict = {
            'role': 'user',
            'content': session.pending_user_message,
            'timestamp': _recovered_ts,
            '_recovered': True,
        }
        if session.pending_attachments:
            recovered['attachments'] = list(session.pending_attachments)
        session.messages.append(recovered)
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.messages.append({
        'role': 'assistant',
        'content': '**Previous turn did not complete.**',
        'timestamp': int(time.time()),
        '_error': True,
    })
    session.save()
    logger.info("Session %s: no core transcript found, added error marker", sid)
    return True


# ── _repair_stale_pending grace period (#1624) ─────────────────────────────
#
# Defense-in-depth against a narrow race between the streaming thread clearing
# pending_user_message and STREAMS.pop(stream_id). Without this guard, any
# fast turn (e.g. command approval) that exits the thread before the on-disk
# pending clear has flushed gets misdiagnosed as a crashed turn, producing a
# spurious "Previous turn did not complete." marker.
#
# 30s covers the worst-case post-loop persistence window: LLM finishing a tool
# batch + lock contention with the checkpoint thread + a multi-MB session.save.
# A legitimately crashed turn whose pending_started_at is < 30s old will not
# repair on the first get_session() call, but WILL repair on the next call
# after the grace period elapses (typically the user's next interaction).
#
# Missing/falsy pending_started_at (legacy sidecars from before that field
# existed, or any path that forgot to set it) is treated as "old enough" so
# repair still recovers them — preserves current behavior for legacy data.
_REPAIR_STALE_PENDING_GRACE_SECONDS = 30


def _repair_stale_pending(session) -> bool:
    """Recover a sidecar stuck with messages=[] and stale pending state.

    Fires only when messages is empty, pending_user_message is set,
    active_stream_id is set, the stream is no longer alive, AND the turn is
    older than _REPAIR_STALE_PENDING_GRACE_SECONDS (#1624).

    Uses a non-blocking lock acquire so a caller that already holds the
    per-session lock (e.g. retry_last, undo_last, cancel_stream) cannot
    deadlock when get_session() triggers this on a cache miss.

    Returns True if repair was applied, False otherwise.
    Must never raise — all errors are caught and logged.
    """
    # Capture the stream id seen at pre-check time; the under-lock re-check in
    # _apply_core_sync_or_error_marker uses this to detect a rotated active_stream_id
    # (e.g. context compression) or a stream that came back alive.
    _seen_stream_id = session.active_stream_id
    if (not session.pending_user_message
            or not _seen_stream_id
            or _seen_stream_id in _active_stream_ids()):
        return False

    # Grace-period guard: bail if the turn is too fresh to be a real crash.
    # Falsy pending_started_at (None, 0, missing) means "old enough" — preserve
    # legacy-data recovery semantics for sessions that pre-date the field.
    _started = getattr(session, 'pending_started_at', None)
    if _started:
        try:
            _age = time.time() - float(_started)
        except (TypeError, ValueError):
            _age = float('inf')
        if _age < _REPAIR_STALE_PENDING_GRACE_SECONDS:
            logger.debug(
                "_repair_stale_pending: skipping repair for session %s — "
                "pending_started_at age=%.1fs < %ds grace window",
                session.session_id, _age, _REPAIR_STALE_PENDING_GRACE_SECONDS,
            )
            return False
    else:
        # Treat missing/falsy pending_started_at as "old enough" (legacy data).
        _age = float('inf')

    sid = session.session_id
    if not sid or not all(c in '0123456789abcdefghijklmnopqrstuvwxyz_' for c in sid):
        return False

    try:
        profile_home = _get_profile_home(session.profile)
        core_path = profile_home / 'sessions' / f'session_{sid}.json'

        lock = _get_session_agent_lock(sid)
        # Non-blocking acquire: bail immediately if the caller already holds this
        # lock (e.g. retry_last, undo_last, cancel_stream). Blocking would deadlock
        # because _get_session_agent_lock returns a non-reentrant threading.Lock.
        if not lock.acquire(blocking=False):
            logger.debug(
                "_repair_stale_pending: lock contended, skipping repair for session %s", sid,
            )
            return False
        try:
            # Telemetry (#1624): log legitimate repair firings so the next batch
            # of user reports tells us whether the underlying race still fires
            # post-fix. Rate-limit by age (Opus pre-release SHOULD-FIX): WARNING
            # for the diagnostically valuable race window (< 5 min — actual
            # leak-path candidates that slipped past the grace guard) and DEBUG
            # for the long-tail (orphaned sidecars from prior process lifetimes)
            # so reconnect loops on stuck sessions don't flood the log.
            _DIAG_WARN_WINDOW_SECONDS = 300  # 5 min
            _age_str = ('inf' if _age == float('inf') else f'{_age:.1f}s')
            _log = logger.warning if _age < _DIAG_WARN_WINDOW_SECONDS else logger.debug
            _log(
                "_repair_stale_pending firing: session=%s stream_id=%s pending_age=%s",
                sid, _seen_stream_id, _age_str,
            )
            return _apply_core_sync_or_error_marker(
                session, core_path, stream_id_for_recheck=_seen_stream_id,
            )
        finally:
            lock.release()
    except Exception:
        logger.exception("_repair_stale_pending failed for session %s", sid)
        return False


def get_session(sid, metadata_only=False):
    """Load a session, optionally with metadata only (skipping the messages array).

    Metadata-only loads intentionally do not populate the full-session cache.
    Otherwise a later full load could return a compact object with an empty
    messages list. Use this when you only need compact() metadata and not the
    actual message history (e.g., for fast sidebar switching).
    """
    with LOCK:
        if sid in SESSIONS:
            SESSIONS.move_to_end(sid)  # LRU: mark as recently used
            return SESSIONS[sid]
    if metadata_only:
        s = Session.load_metadata_only(sid)
        if s:
            return s
    else:
        s = Session.load(sid)
    if s:
        with LOCK:
            SESSIONS[sid] = s
            SESSIONS.move_to_end(sid)
            while len(SESSIONS) > SESSIONS_MAX:
                SESSIONS.popitem(last=False)  # evict least recently used
        if not metadata_only:
            try:
                repaired = _repair_stale_pending(s)
                # If repair had to bail because the per-session lock was held,
                # do not pin the still-stale sidecar in the LRU cache forever.
                # Leaving it cached would prevent future get_session() calls from
                # re-entering the cache-miss repair path after the lock holder exits.
                if not repaired and (len(s.messages) == 0
                        and s.pending_user_message
                        and s.active_stream_id
                        and s.active_stream_id not in _active_stream_ids()):
                    with LOCK:
                        if SESSIONS.get(sid) is s:
                            SESSIONS.pop(sid, None)
            except Exception:
                pass  # repair is best-effort
        return s
    raise KeyError(sid)

def new_session(workspace=None, model=None, profile=None, model_provider=None, project_id=None):
    """Create a new in-memory session.

    The session lives in the SESSIONS dict only — no disk write happens until
    the first message is appended (#1171 follow-up).  This avoids the
    "ghost Untitled session on disk" pile-up that occurred when users clicked
    New Conversation, reloaded the page, or completed onboarding without ever
    sending a message.  Subsequent code paths that populate state immediately
    (btw / background agent at api/routes.py) call ``s.save()`` themselves
    after setting title/messages, and ``_handle_chat_start`` saves the
    session as soon as the user actually sends a message — both are the
    natural first-write moments for a real session.

    Crash-safety: if the process exits between session creation and first
    message, the session is lost.  Since it had no messages, there is
    nothing to lose.

    *profile* — when supplied by the caller (e.g. from the request body sent
    by the active browser tab), it is used directly so that concurrent clients
    on different profiles don't fight over a shared process-global.  If not
    supplied, we fall back to the process-level active profile (the pre-#798
    behaviour, preserved for calls that originate outside a request context).
    """
    if profile is None:
        # Fallback: read process-level global (single-client or startup path)
        try:
            from api.profiles import get_active_profile_name
            profile = get_active_profile_name()
        except ImportError:
            profile = None
    effective_model = model or get_effective_default_model()
    s = Session(
        workspace=workspace or get_last_workspace(),
        model=effective_model,
        model_provider=model_provider,
        profile=profile,
        project_id=project_id,
    )
    with LOCK:
        SESSIONS[s.session_id] = s
        SESSIONS.move_to_end(s.session_id)
        while len(SESSIONS) > SESSIONS_MAX:
            SESSIONS.popitem(last=False)
    return s

def _hide_from_default_sidebar(session: dict) -> bool:
    """Return True for internal/background sessions hidden from the default list."""
    sid = str(session.get('session_id') or '')
    source = session.get('source_tag') or session.get('source')
    return source == 'cron' or sid.startswith('cron_')


def _active_state_db_path() -> Path:
    """Return state.db for the active Hermes profile, degrading to HERMES_HOME."""
    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()
    return hermes_home / 'state.db'


def _enrich_sidebar_lineage_metadata(sessions: list[dict]) -> None:
    """Attach state.db compression lineage metadata used by sidebar collapse."""
    try:
        metadata = read_session_lineage_metadata(
            _active_state_db_path(),
            {s.get('session_id') for s in sessions},
        )
    except Exception:
        return
    for session in sessions:
        sid = session.get('session_id')
        if sid in metadata:
            session.update(metadata[sid])


def all_sessions():
    active_stream_ids = _active_stream_ids()
    # Phase C: try index first for O(1) read; fall back to full scan
    if SESSION_INDEX_FILE.exists():
        try:
            index = json.loads(SESSION_INDEX_FILE.read_text(encoding='utf-8'))
            index = [
                s for s in index
                if _index_entry_exists(s.get('session_id'))
            ]
            backfilled = []
            for i, s in enumerate(index):
                if 'last_message_at' not in s:
                    full = Session.load(s.get('session_id'))
                    if full:
                        index[i] = full.compact()
                        backfilled.append(full)
            if backfilled:
                try:
                    _write_session_index(updates=backfilled)
                except Exception:
                    logger.debug("Failed to persist last_message_at backfill")
            for s in index:
                s['is_streaming'] = _is_streaming_session(
                    s.get('active_stream_id'),
                    active_stream_ids,
                )
            # Overlay any in-memory sessions that may be newer than the index
            index_map = {s['session_id']: s for s in index}
            with LOCK:
                for s in SESSIONS.values():
                    index_map[s.session_id] = s.compact(
                        include_runtime=True,
                        active_stream_ids=active_stream_ids,
                    )
            result = sorted(index_map.values(), key=lambda s: (s.get('pinned', False), _session_sort_timestamp(s)), reverse=True)
            # Hide empty Untitled sessions from the UI entirely — they are ephemeral
            # scratch pads that only become real once the first message is sent (#1171).
            # No grace window: a 0-message Untitled session is never shown in the list
            # regardless of age. This means page refreshes and accidental New Conversation
            # clicks never leave orphan entries in the sidebar.
            #
            # Exception: sessions with active_stream_id set are actively streaming (#1327).
            # #1184 deferred the first save() until the first message, so during the
            # initial streaming turn the session still looks like Untitled+0-messages.
            # Without this exemption, navigating away during a long first turn causes
            # the session to vanish from the sidebar.
            result = [s for s in result if not (
                s.get('title', 'Untitled') == 'Untitled'
                and s.get('message_count', 0) == 0
                and not s.get('active_stream_id')
                and not s.get('has_pending_user_message')
            )]
            result = [s for s in result if not _hide_from_default_sidebar(s)]
            # Backfill: sessions created before Sprint 22 have no profile tag.
            # Attribute them to 'default' so the client profile filter works correctly.
            for s in result:
                if not s.get('profile'):
                    s['profile'] = 'default'
            _enrich_sidebar_lineage_metadata(result)
            return result
        except Exception:
            logger.debug("Failed to load session index, falling back to full scan")
    # Full scan fallback
    out = []
    for p in SESSION_DIR.glob('*.json'):
        if p.name.startswith('_'): continue
        try:
            s = Session.load(p.stem)
            if s: out.append(s)
        except Exception:
            logger.debug("Failed to load session from %s", p)
    for s in SESSIONS.values():
        if all(s.session_id != x.session_id for x in out): out.append(s)
    out.sort(key=lambda s: (getattr(s, 'pinned', False), _session_sort_timestamp(s)), reverse=True)
    # Hide empty Untitled sessions from the UI entirely — kept consistent with the
    # index-path filter above. No grace window: a 0-message Untitled session is
    # never shown regardless of age (#1171).  Same streaming exemption as above (#1327).
    result = [s.compact(include_runtime=True, active_stream_ids=active_stream_ids) for s in out if not (
        s.title == 'Untitled'
        and len(s.messages) == 0
        and not s.active_stream_id
        and not s.pending_user_message
    )]
    result = [s for s in result if not _hide_from_default_sidebar(s)]
    for s in result:
        if not s.get('profile'):
            s['profile'] = 'default'
    _enrich_sidebar_lineage_metadata(result)
    return result


def title_from(messages, fallback: str='Untitled'):
    """Derive a session title from the first user message."""
    for m in messages:
        if m.get('role') == 'user':
            c = m.get('content', '')
            if isinstance(c, list):
                c = ' '.join(p.get('text', '') for p in c if isinstance(p, dict) and p.get('type') == 'text')
            text = str(c).strip()
            if text:
                return text[:64]
    return fallback


# ── Project helpers ──────────────────────────────────────────────────────────

_PROJECTS_MIGRATION_LOCK = threading.Lock()
_projects_migrated = False


def _backfill_project_profiles_if_needed(projects: list) -> bool:
    """Tag any legacy untagged projects (`profile` missing) with a sensible default.

    Strategy:
      1. For each untagged project, look at the sessions assigned to it via
         the session index. If any session carries a profile, take that
         profile.  Most installs are single-profile so this picks up the
         right answer for everyone.
      2. Otherwise default to 'default'.

    Returns True if any project was mutated. Safe to call repeatedly — once
    every project is tagged, this is a no-op. Runs at most once per process
    (cached via the module-level _projects_migrated flag) but the result is
    persisted so it's a one-time write.
    """
    untagged = [p for p in projects if not p.get('profile')]
    if not untagged:
        return False

    # Build session_id -> profile map for the untagged project_ids.
    session_profile_by_project: dict[str, str] = {}
    if SESSION_INDEX_FILE.exists():
        try:
            entries = json.loads(SESSION_INDEX_FILE.read_text(encoding='utf-8'))
            untagged_ids = {p['project_id'] for p in untagged if p.get('project_id')}
            for e in entries:
                pid = e.get('project_id')
                if pid in untagged_ids and e.get('profile'):
                    # First session profile wins for the project.
                    session_profile_by_project.setdefault(pid, e['profile'])
        except Exception:
            logger.debug("Failed to read session index for project profile backfill")

    mutated = False
    for p in untagged:
        inferred = session_profile_by_project.get(p.get('project_id'), 'default')
        p['profile'] = inferred
        mutated = True
    return mutated


def load_projects(*, _migrate: bool = True) -> list:
    """Load project list from disk. Returns list of project dicts.

    On first call, runs a one-time migration to back-fill the `profile` field
    on legacy untagged projects (#1614). Disable via `_migrate=False` for
    callsites that want the raw on-disk shape (test fixtures, e.g.).
    """
    global _projects_migrated
    if not PROJECTS_FILE.exists():
        return []
    try:
        projects = json.loads(PROJECTS_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []
    if _migrate and not _projects_migrated:
        with _PROJECTS_MIGRATION_LOCK:
            # Re-check inside the lock — another thread may have raced.
            if _projects_migrated:
                # Per Opus advisor on stage-293: another thread completed
                # migration and wrote new state to disk while we waited for
                # the lock. Our `projects` snapshot is the pre-migration
                # version; re-read so the caller doesn't see stale untagged
                # rows (which a mutation route could then write back,
                # silently overwriting the migration).
                try:
                    return json.loads(PROJECTS_FILE.read_text(encoding='utf-8'))
                except Exception:
                    return projects
            if _backfill_project_profiles_if_needed(projects):
                try:
                    save_projects(projects)
                    _projects_migrated = True
                except Exception:
                    logger.debug("Failed to persist project profile backfill")
                    # Leave _projects_migrated False so a future call retries.
            else:
                # Nothing to migrate — already tagged.
                _projects_migrated = True
    return projects

def save_projects(projects) -> None:
    """Write project list to disk."""
    PROJECTS_FILE.write_text(json.dumps(projects, ensure_ascii=False, indent=2), encoding='utf-8')


CRON_PROJECT_NAME = 'Cron Jobs'
_CRON_PROJECT_LOCK = threading.Lock()


def ensure_cron_project() -> str:
    """Return the project_id of the system "Cron Jobs" project for the active profile.

    Each profile gets its own "Cron Jobs" project so cron-spawned sessions in
    profile A don't surface under the cron chip of profile B (#1614). Lookup
    keys on (name, profile) — a legacy untagged "Cron Jobs" project (no
    `profile` field) is treated as belonging to whichever profile first calls
    this in a given install, then re-tagged.

    Thread-safe and idempotent.  Returns a 12-char hex project_id string.
    """
    from api.profiles import get_active_profile_name, _is_root_profile

    active = get_active_profile_name() or 'default'
    with _CRON_PROJECT_LOCK:
        projects = load_projects()
        # Look for an existing per-profile cron project. Match either an exact
        # profile tag or the renamed-root alias (a 'default'-tagged project
        # under a renamed root, or a renamed-root-tagged project under
        # 'default'). _is_root_profile is the canonical alias check.
        for p in projects:
            if p.get('name') != CRON_PROJECT_NAME:
                continue
            row_profile = p.get('profile')
            if row_profile == active:
                return p['project_id']
            if _is_root_profile(row_profile or 'default') and _is_root_profile(active):
                return p['project_id']
        # Reuse a legacy untagged cron project — back-tag it to the active profile.
        for p in projects:
            if p.get('name') == CRON_PROJECT_NAME and not p.get('profile'):
                p['profile'] = active
                save_projects(projects)
                return p['project_id']
        # Otherwise create a new one tagged with the active profile.
        project_id = uuid.uuid4().hex[:12]
        projects.append({
            'project_id': project_id,
            'name': CRON_PROJECT_NAME,
            'color': '#6366f1',
            'profile': active,
            'created_at': time.time(),
        })
        save_projects(projects)
        return project_id


def is_cron_session(session_id: str, source_tag: str = None) -> bool:
    """Return True if a session originates from a cron job."""
    if source_tag == 'cron':
        return True
    sid = str(session_id or '')
    return sid.startswith('cron_')



def import_cli_session(
    session_id: str,
    title: str,
    messages,
    model: str='unknown',
    profile=None,
    created_at=None,
    updated_at=None,
):
    """Create a new WebUI session populated with CLI messages.
    Returns the Session object.
    """
    s = Session(
        session_id=session_id,
        title=title,
        workspace=get_last_workspace(),
        model=model,
        messages=messages,
        profile=profile,
        created_at=created_at,
        updated_at=updated_at,
    )
    s.save(touch_updated_at=False)
    return s


# ── CLI session bridge ──────────────────────────────────────────────────────

def get_cli_sessions() -> list:
    """Read CLI sessions from the agent's SQLite store and return them as
    dicts in a format the WebUI sidebar can render alongside local sessions.

    Returns empty list if the SQLite DB is missing or any error occurs -- the
    bridge is purely additive and never crashes the WebUI.
    """
    import os
    cli_sessions = []

    # Use the active WebUI profile's HERMES_HOME to find state.db.
    # The active profile is determined by what the user has selected in the UI
    # (stored in the server's runtime config). This means:
    #   - default profile  -> ~/.hermes/state.db
    #   - named profile X  -> ~/.hermes/profiles/X/state.db
    # We resolve the active profile's home directory rather than just using
    # HERMES_HOME (which is the server's launch profile, not necessarily the
    # active one after a profile switch).
    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()

    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return cli_sessions

    # Try to resolve the active CLI profile so imported sessions integrate
    # with the WebUI profile filter (available since Sprint 22).
    try:
        from api.profiles import get_active_profile_name
        _cli_profile = get_active_profile_name()
    except ImportError:
        _cli_profile = None  # older agent -- fall back to no profile

    # Memoize the cron project ID for this scan so we don't pay a lock-acquire +
    # disk-read of projects.json per cron session in the loop below.
    # Resolved lazily on the first cron session we encounter.
    _cron_pid_cache = [None]  # list-as-cell so the closure can mutate
    def _cron_pid():
        if _cron_pid_cache[0] is None:
            _cron_pid_cache[0] = ensure_cron_project()
        return _cron_pid_cache[0]

    try:
        for row in read_importable_agent_session_rows(
            db_path,
            limit=CLI_VISIBLE_SESSION_LIMIT,
            log=logger,
            exclude_sources=None,
        ):
            sid = row['id']
            raw_ts = row['last_activity'] or row['started_at']
            # Prefer the CLI session's own profile from the DB; fall back to
            # the active CLI profile so sidebar filtering works either way.
            profile = _cli_profile  # CLI DB has no profile column; use active profile

            _source = row['source'] or 'cli'
            _title = row['title']
            if not _title and _source == 'cron' and sid.startswith('cron_'):
                # Extract job_id from session ID (cron_{job_id}_{timestamp})
                # and look up the human-friendly job name from jobs.json
                parts = sid.split('_')
                if len(parts) >= 3:
                    _job_id = parts[1]
                    try:
                        _jobs_path = hermes_home / 'cron' / 'jobs.json'
                        if _jobs_path.exists():
                            import json as _json
                            _jobs_data = _json.loads(_jobs_path.read_text())
                            for _j in _jobs_data.get('jobs', []):
                                if _j.get('id') == _job_id:
                                    _title = _j.get('name') or _title
                                    break
                    except Exception:
                        pass  # degrade gracefully
            # If a WebUI JSON file exists for this session (e.g. previously
            # imported or renamed in the sidebar), prefer its title over the
            # state.db title.  This fixes rename-not-persisting for CLI sessions
            # after compression chain extension (#1486).
            try:
                _webui_meta = Session.load_metadata_only(sid)
                if _webui_meta and getattr(_webui_meta, 'title', None):
                    _title = _webui_meta.title
            except Exception:
                pass
            _display_title = _title or f'{_source.title()} Session'
            cli_sessions.append({
                'session_id': sid,
                'title': _display_title,
                'workspace': str(get_last_workspace()),
                'model': row['model'] or None,
                'message_count': row['message_count'] or row['actual_message_count'] or 0,
                'created_at': row['started_at'],
                'updated_at': raw_ts,
                'pinned': False,
                'archived': False,
                'project_id': _cron_pid() if is_cron_session(sid, _source) else None,
                'profile': profile,
                'source_tag': _source,
                'raw_source': row.get('raw_source'),
                'user_id': row.get('user_id'),
                'chat_id': row.get('chat_id') or row.get('origin_chat_id'),
                'chat_type': row.get('chat_type'),
                'thread_id': row.get('thread_id'),
                'session_key': row.get('session_key'),
                'platform': row.get('platform'),
                'session_source': row.get('session_source'),
                'source_label': row.get('source_label'),
                'parent_session_id': row.get('parent_session_id'),
                'parent_title': row.get('parent_title'),
                'parent_source': row.get('parent_source'),
                'relationship_type': row.get('relationship_type'),
                '_parent_lineage_root_id': row.get('_parent_lineage_root_id'),
                'end_reason': row.get('end_reason'),
                'actual_message_count': row.get('actual_message_count'),
                'user_message_count': row.get('actual_user_message_count'),
                '_lineage_root_id': row.get('_lineage_root_id'),
                '_lineage_tip_id': row.get('_lineage_tip_id'),
                '_compression_segment_count': row.get('_compression_segment_count'),
                'is_cli_session': True,
            })
    except Exception as _cli_err:
        # DB schema changed, locked, or corrupted -- log warning so admins can diagnose.
        # Still degrade gracefully (don't crash the WebUI).
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "get_cli_sessions() failed — check state.db schema or path (%s): %s",
            db_path, _cli_err,
        )
        return []

    return cli_sessions


def get_cli_session_messages(sid) -> list:
    """Read messages for a single CLI session from the SQLite store.
    Returns a list of {role, content, timestamp} dicts.
    Returns empty list on any error.
    """
    import os
    try:
        import sqlite3
    except ImportError:
        return []

    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()
    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return []

    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT role, content, timestamp
                FROM messages
                WHERE session_id = ?
                ORDER BY timestamp ASC
            """, (sid,))
            msgs = []
            for row in cur.fetchall():
                msgs.append({
                    'role': row['role'],
                    'content': row['content'],
                    'timestamp': row['timestamp'],
                })
    except Exception:
        return []
    return msgs


def count_conversation_rounds(sid: str, since: float | None = None) -> int:
    """Count conversation rounds for a session from state.db.

    A "round" = one user message + one agent reply.  Consecutive user
    messages are merged into a single round so that multi-part questions
    don't inflate the count.

    Parameters
    ----------
    sid : str
        Gateway session ID (e.g. ``20260430_151231_7209a0``).
    since : float | None
        Unix timestamp.  If provided, only messages **after** this
        timestamp are counted.

    Returns
    -------
    int
        Number of complete conversation rounds.
    """
    import os, sqlite3, datetime

    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()
    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return 0

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT role, timestamp FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
                (sid,),
            )
            rows = cur.fetchall()
    except Exception:
        return 0

    rounds = 0
    seen_user = False          # have we seen a user msg in the current round?
    seen_agent_after_user = False  # have we seen an agent reply after that user msg?

    for row in rows:
        role = (row['role'] or '').strip().lower()
        ts_raw = row['timestamp']

        # Parse timestamp and apply the ``since`` filter.
        if since is not None and ts_raw is not None:
            try:
                if isinstance(ts_raw, (int, float)):
                    ts_val = float(ts_raw)
                else:
                    # ISO-8601 string
                    ts_val = datetime.datetime.fromisoformat(
                        str(ts_raw).replace('Z', '+00:00')
                    ).timestamp()
                if ts_val <= since:
                    continue
            except Exception:
                pass

        if role == 'user':
            if seen_user and not seen_agent_after_user:
                # Consecutive user message — merge into current round.
                pass
            elif seen_user and seen_agent_after_user:
                # Previous round completed, starting a new one.
                rounds += 1
                seen_agent_after_user = False
            seen_user = True
        elif role == 'assistant':
            if seen_user:
                seen_agent_after_user = True

    # Close the last round if it was completed.
    if seen_user and seen_agent_after_user:
        rounds += 1

    return rounds


CONVERSATION_ROUND_THRESHOLD = 10


def delete_cli_session(sid) -> bool:
    """Delete a CLI session from state.db (messages + session row).
    Returns True if deleted, False if not found or error.
    """
    import os
    try:
        import sqlite3
    except ImportError:
        return False

    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()
    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return False

    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            cur.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            conn.commit()
            return cur.rowcount > 0
    except Exception:
        return False
