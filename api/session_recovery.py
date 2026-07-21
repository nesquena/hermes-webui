"""
Session recovery from .bak snapshots — last line of defense against
data-loss bugs like #1558.

``Session.save()`` writes a ``<sid>.json.bak`` snapshot of the previous
state whenever an incoming save would shrink the messages array. This
module reads those snapshots back and restores any session whose live
file has fewer messages than its backup, or whose live file is missing
while a valid backup remains.

Three integration points:

1. ``recover_all_sessions_on_startup()`` — called from server.py at boot,
   scans the session dir, restores any session whose JSON has fewer
   messages than its .bak, and recreates a missing ``<sid>.json`` from an
   orphaned ``<sid>.json.bak`` when the canonical state DB still has that
   session. Idempotent: a clean run is a no-op.

2. ``recover_session(sid)`` — single-session helper backing the
   ``POST /api/session/recover`` endpoint, so users can re-run recovery
   manually if their session was open through a server restart.

3. ``inspect_session_recovery_status(sid)`` — read-only audit returning
   message counts for the live JSON, the .bak, and a recommendation.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path

from api.turn_journal import (
    derive_turn_journal_states,
    is_terminal_turn_event,
    iter_turn_journal_session_ids,
    read_turn_journal,
)

logger = logging.getLogger(__name__)


_COMPRESSION_TRANSACTION_VERSION = 2
_COMPRESSION_TRANSACTION_PHASES = {
    "initializing",
    "prepared",
    "reserved",
    "source_archived",
    "sidecar_published",
    "migrations_complete",
    "source_restored",
}


def _compression_transaction_paths(session_dir: Path, new_sid: str) -> dict[str, Path]:
    root = session_dir / "_compression_transactions"
    return {
        "root": root,
        "intent": root / f"{new_sid}.json",
        "sidecar_backup": root / f"{new_sid}.source-sidecar.bak",
        "index_backup": root / f"{new_sid}.source-index.bak",
    }


def _write_compression_transaction_intent(
    session_dir: Path,
    intent: dict,
) -> dict:
    from api.models import _durable_replace_bytes

    validated = _validate_compression_transaction_intent(
        session_dir,
        intent,
    )
    paths = _compression_transaction_paths(
        session_dir,
        validated["new_session_id"],
    )
    _durable_replace_bytes(
        paths["intent"],
        json.dumps(validated, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return validated


def _validate_compression_transaction_intent(
    session_dir: Path,
    intent: dict,
    *,
    expected_new_sid: str | None = None,
) -> dict:
    from api.models import _is_session_incarnation_token, is_safe_session_id

    if not isinstance(intent, dict) or intent.get("version") != _COMPRESSION_TRANSACTION_VERSION:
        raise ValueError("invalid compression transaction version")
    old_sid = intent.get("old_session_id")
    new_sid = intent.get("new_session_id")
    if (
        not isinstance(old_sid, str)
        or not isinstance(new_sid, str)
        or not is_safe_session_id(old_sid)
        or not is_safe_session_id(new_sid)
        or old_sid == new_sid
        or (expected_new_sid is not None and new_sid != expected_new_sid)
    ):
        raise ValueError("invalid compression transaction identity")
    phase = intent.get("phase")
    if phase not in _COMPRESSION_TRANSACTION_PHASES:
        raise ValueError("invalid compression transaction phase")
    token = intent.get("incarnation_token")
    if token is not None and not _is_session_incarnation_token(token):
        raise ValueError("invalid compression transaction incarnation")
    if phase not in {"initializing", "prepared"} and token is None:
        raise ValueError("compression transaction phase requires an incarnation")
    source = intent.get("source")
    if not isinstance(source, dict):
        raise ValueError("invalid compression transaction source backup")
    paths = _compression_transaction_paths(session_dir, new_sid)
    expected = {
        "sidecar": paths["sidecar_backup"].name,
        "index": paths["index_backup"].name,
    }
    for key in ("sidecar", "index"):
        record = source.get(key)
        if not isinstance(record, dict) or not isinstance(record.get("existed"), bool):
            raise ValueError("invalid compression transaction backup record")
        if record.get("backup") != expected[key]:
            raise ValueError("compression transaction backup path mismatch")
        digest = record.get("sha256")
        if record["existed"]:
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError("invalid compression transaction backup digest")
        elif digest is not None:
            raise ValueError("absent compression source cannot have a digest")
    return dict(intent)


def _stage_compression_transaction_source(
    session_dir: Path,
    old_sid: str,
    new_sid: str,
) -> dict:
    """Durably record exact source bytes before either session identity mutates."""
    from api.models import (
        _durable_replace_bytes,
        _path_entry_exists,
    )

    paths = _compression_transaction_paths(session_dir, new_sid)
    sidecar = session_dir / f"{old_sid}.json"
    index = session_dir / "_index.json"
    if not _path_entry_exists(sidecar):
        raise RuntimeError("Compression source sidecar is missing")
    sidecar_bytes = sidecar.read_bytes()
    _read_recovery_payload(sidecar, old_sid)
    index_existed = _path_entry_exists(index)
    index_bytes = index.read_bytes() if index_existed else b""
    intent = {
        "version": _COMPRESSION_TRANSACTION_VERSION,
        "old_session_id": old_sid,
        "new_session_id": new_sid,
        "incarnation_token": None,
        "phase": "initializing",
        "source": {
            "sidecar": {
                "existed": True,
                "backup": paths["sidecar_backup"].name,
                "sha256": hashlib.sha256(sidecar_bytes).hexdigest(),
            },
            "index": {
                "existed": index_existed,
                "backup": paths["index_backup"].name,
                "sha256": (
                    hashlib.sha256(index_bytes).hexdigest()
                    if index_existed
                    else None
                ),
            },
        },
    }
    intent = _write_compression_transaction_intent(session_dir, intent)
    _durable_replace_bytes(paths["sidecar_backup"], sidecar_bytes)
    if index_existed:
        _durable_replace_bytes(paths["index_backup"], index_bytes)
    intent["phase"] = "prepared"
    return _write_compression_transaction_intent(session_dir, intent)


def _advance_compression_transaction(
    session_dir: Path,
    intent: dict,
    phase: str,
    *,
    token: str | None = None,
) -> dict:
    updated = dict(intent)
    updated["phase"] = phase
    if token is not None:
        updated["incarnation_token"] = token
    return _write_compression_transaction_intent(session_dir, updated)


def _restore_compression_transaction_source(
    session_dir: Path,
    intent: dict,
) -> None:
    """Restore the old sidecar and global index byte-for-byte from durable backups."""
    from api.models import _durable_replace_bytes, _durable_unlink

    validated = _validate_compression_transaction_intent(session_dir, intent)
    paths = _compression_transaction_paths(
        session_dir,
        validated["new_session_id"],
    )
    targets = {
        "sidecar": session_dir / f"{validated['old_session_id']}.json",
        "index": session_dir / "_index.json",
    }
    for key in ("sidecar", "index"):
        record = validated["source"][key]
        if not record["existed"]:
            _durable_unlink(targets[key])
            continue
        backup = paths[f"{key}_backup"]
        payload = backup.read_bytes()
        if hashlib.sha256(payload).hexdigest() != record["sha256"]:
            raise RuntimeError("Compression source backup digest mismatch")
        _durable_replace_bytes(targets[key], payload)


def _retire_compression_transaction(session_dir: Path, new_sid: str) -> None:
    from api.models import _durable_unlink

    paths = _compression_transaction_paths(session_dir, new_sid)
    _durable_unlink(paths["sidecar_backup"])
    _durable_unlink(paths["index_backup"])
    _durable_unlink(paths["intent"])


def _read_recovery_payload(path: Path, expected_sid: str) -> dict:
    from api.models import _load_and_validate_session_payload
    from api.session_media import verify_serialized_session_media_payload

    payload = _load_and_validate_session_payload(path, expected_sid)
    verify_serialized_session_media_payload(payload, expected_sid)
    return payload


def _msg_count(p: Path) -> int:
    """Return the number of messages in a session JSON file, or -1 on read/parse error.

    Returns -1 for any non-session-shape file:
    - File can't be read (OSError)
    - Top-level isn't valid JSON or is invalid (JSONDecodeError, ValueError)
    - Top-level isn't a dict (AttributeError on .get) — e.g. ``_index.json``
      which is a top-level list of session metadata, not a session itself.
      The startup recovery scanner globs ``*.json`` and would otherwise
      crash on the first non-dict file it encounters.
    """
    expected_sid = (
        p.name[: -len(".json.bak")]
        if p.name.endswith(".json.bak")
        else p.stem
    )
    try:
        data = _read_recovery_payload(p, expected_sid)
    except (OSError, json.JSONDecodeError, ValueError):
        return -1
    if not isinstance(data, dict):
        return -1
    msgs = data.get('messages')
    return len(msgs) if isinstance(msgs, list) else -1


def _rebuild_recovery_session_index(session_dir: Path) -> None:
    """Rebuild ``session_dir/_index.json`` from persisted sidecars only.

    Recovery repair/audit operates on a concrete sidecar directory. Unlike the
    live sidebar path, its rebuilt index must not include unrelated in-memory
    ``Session`` cache entries whose backing JSON files are absent from this
    directory; those would immediately audit as ``index_missing_file`` rows.
    """
    from api.models import _load_session_from_path

    entry_map: dict[str, dict] = {}
    for path in sorted(session_dir.glob('*.json')):
        if path.name.startswith('_'):
            continue
        session = _load_session_from_path(path)
        if not session:
            continue
        entry = session.compact()
        session_id = entry.get('session_id')
        if not session_id:
            continue
        existing = entry_map.get(session_id)
        if existing is None or entry.get('message_count', 0) > existing.get('message_count', 0):
            entry_map[session_id] = entry

    entries = sorted(
        entry_map.values(),
        key=lambda entry: entry.get('updated_at', 0),
        reverse=True,
    )
    index_path = session_dir / '_index.json'
    tmp = index_path.with_suffix(f'.tmp.recovery.{uuid.uuid4().hex}')
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(entries, ensure_ascii=False, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, index_path)
        from api.models import _fsync_parent_directory

        _fsync_parent_directory(index_path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _session_records_intentional_compress_shrink(session_path: Path) -> bool:
    """Return True when the live sidecar records an intentional context shrink.

    Manual ``/compress`` keeps the visible transcript but replaces the
    model-facing ``context_messages`` with a smaller compacted prefix. That
    operation must not be treated as accidental data loss by the #1558
    ``.bak`` safeguard or startup recovery (#4836).

    NOTE: this only reports *whether* the live session was intentionally
    compressed. It must NOT, on its own, decide to skip ``.bak`` recovery —
    a session can be manually compressed and *then* later suffer a genuine
    #1558 ``messages``-array loss. The caller pairs this with a freshness
    check (the backup must predate the compression) so a real post-compress
    loss is still recovered. See ``inspect_session_recovery_status``.
    """
    try:
        data = _read_recovery_payload(session_path, session_path.stem)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False

    context_messages = data.get('context_messages')
    messages = data.get('messages')
    has_shorter_context = (
        isinstance(context_messages, list)
        and isinstance(messages, list)
        and len(context_messages) < len(messages)
    )
    anchor_summary = str(data.get('compression_anchor_summary') or '').strip()
    anchor_key = data.get('compression_anchor_message_key')
    mode = str(data.get('compression_anchor_mode') or '').strip().lower()
    watermark = data.get('truncation_watermark')

    if mode == 'manual':
        return True
    if has_shorter_context and anchor_summary and anchor_key is not None:
        return True
    if has_shorter_context and watermark is not None:
        return True
    return False


def _backup_predates_intentional_shrink(session_path: Path, bak_path: Path) -> bool:
    """True when the ``.bak`` captured the PRE-compression transcript.

    The intentional-compress guard (#4836) must only suppress recovery for a
    backup whose restore would *undo* the user's deliberate shrink — i.e. a
    backup taken before ``/compress`` ran, still carrying the large
    UNCOMPRESSED ``context_messages``. A backup written *after* the
    compression (a later save that lost data) carries the ALREADY-COMPRESSED
    context and MUST still be recoverable.

    Time can't tell these apart (the ``.bak`` and main file are written in the
    same ``save()`` milliseconds apart), so we use the same content-semantic
    signal reconciliation uses: the compaction marker. A post-compression
    backup carries the ``[context compaction…]`` marker in its
    ``context_messages`` (it persists across saves); a pre-compression backup
    does not. We suppress recovery ONLY when the backup is genuinely
    pre-compression — its context lacks the compaction marker AND is larger
    than the live compressed context (the shrink hasn't been applied to it).
    If the backup already carries the marker, it post-dates the compression →
    a real loss → recover. The length clause is a secondary guard for the rare
    compression that emits no marker; whenever in doubt we fail OPEN (return
    False, allow recovery) — for a data-loss safeguard, recovering real data
    is always the safer error. Fail OPEN on any read/parse error too.
    """
    try:
        live = _read_recovery_payload(session_path, session_path.stem)
        bak = _read_recovery_payload(bak_path, session_path.stem)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(live, dict) or not isinstance(bak, dict):
        return False

    bak_ctx = bak.get('context_messages')
    bak_ctx = bak_ctx if isinstance(bak_ctx, list) else []

    # If the backup's context already carries the compaction marker, the backup
    # post-dates the compression (the marker persists across saves) → it is a
    # recoverable post-compression snapshot, never a shrink-undoing one.
    try:
        from api.models import _context_messages_include_compression_marker
        if _context_messages_include_compression_marker(bak_ctx):
            return False
    except Exception:
        # If the marker check is unavailable, fall through to the length guard.
        logger.debug("compaction-marker check unavailable in recovery", exc_info=True)

    live_ctx = live.get('context_messages')
    live_ctx_len = len(live_ctx) if isinstance(live_ctx, list) else 0
    bak_ctx_len = len(bak_ctx)

    # Secondary guard (unmarked compression): a pre-compression backup's context
    # is larger than the live compressed context (the shrink hasn't been applied
    # to it). A backup whose context is already <= the live compressed context
    # post-dates the compression and represents recoverable post-compress data.
    return bak_ctx_len > live_ctx_len


def _session_records_clear_sentinel(session_path: Path, bak_path: Path) -> bool:
    """Return True when the live sidecar records a provenanced clear sentinel.

    The live sidecar must carry the explicit /api/session/clear marker, and
    the backup must not carry the same marker. Same-generation backups stay
    recoverable; unreadable or partial matches fail open.
    """
    try:
        data = _read_recovery_payload(session_path, session_path.stem)
        bak = _read_recovery_payload(bak_path, session_path.stem)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(bak, dict):
        return False
    clear_generation = data.get('clear_generation')
    if not isinstance(clear_generation, str) or not clear_generation:
        return False
    if bak.get('clear_generation') == clear_generation:
        return False
    expected = {
        'messages': [],
        'context_messages': [],
        'truncation_watermark': 0.0,
        'truncation_boundary': 0.0,
        'active_stream_id': None,
        'pending_user_message': None,
        'pending_attachments': [],
        'pending_started_at': None,
        'pending_user_source': None,
    }
    for key, value in expected.items():
        if key not in data or data.get(key) != value:
            return False
    return True


def _live_supersedes_backup_by_clear_generation(session_path: Path, bak_path: Path) -> bool:
    """Return True when the live sidecar provably post-dates the backup via a
    clear sentinel even though the user has since sent NEW messages.

    Scope: this handles ONLY the post-clear-message case that the exact-empty
    sentinel (_session_records_clear_sentinel) can't. After /api/session/clear
    stamps a unique ``clear_generation`` and resets the truncation boundary to
    0.0, a pre-clear ``.json.bak`` is stale — restoring it would resurrect
    cleared history on top of the post-clear message. We require: live carries a
    ``clear_generation`` the backup lacks, live has a NON-EMPTY transcript, and
    the live boundary still shows the clear reset (watermark == boundary == 0.0).
    Empty clear-shaped sidecars stay governed by the exact-empty sentinel and its
    existing recovery semantics. Same-generation backups stay recoverable;
    unreadable/partial reads fail open (return False -> normal recovery), so a
    genuine crash-loss is never suppressed.
    """
    try:
        data = _read_recovery_payload(session_path, session_path.stem)
        bak = _read_recovery_payload(bak_path, session_path.stem)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(bak, dict):
        return False
    clear_generation = data.get('clear_generation')
    if not isinstance(clear_generation, str) or not clear_generation:
        return False
    if bak.get('clear_generation') == clear_generation:
        return False
    live_messages = data.get('messages')
    if not isinstance(live_messages, list) or len(live_messages) == 0:
        return False
    if data.get('truncation_watermark') != 0.0 or data.get('truncation_boundary') != 0.0:
        return False
    return True


def inspect_session_recovery_status(session_path: Path) -> dict:
    """Return a status dict describing whether recovery is recommended.

    {
      "session_id": "...",
      "live_messages": int,    # -1 if live file unreadable
      "bak_messages": int,     # -1 if no .bak or unreadable
      "recommend": "restore" | "no_action" | "no_backup",
    }
    """
    bak_path = session_path.with_suffix('.json.bak')
    live_count = _msg_count(session_path)
    if not bak_path.exists():
        return {
            "session_id": session_path.stem,
            "live_messages": live_count,
            "bak_messages": -1,
            "recommend": "no_backup",
        }
    bak_count = _msg_count(bak_path)
    if bak_count > live_count:
        if (
            _session_records_clear_sentinel(session_path, bak_path)
            or _live_supersedes_backup_by_clear_generation(session_path, bak_path)
        ):
            return {
                "session_id": session_path.stem,
                "live_messages": live_count,
                "bak_messages": bak_count,
                "recommend": "no_action",
                "intentional_clear_truncate": True,
            }
        if (
            _session_records_intentional_compress_shrink(session_path)
            and _backup_predates_intentional_shrink(session_path, bak_path)
        ):
            return {
                "session_id": session_path.stem,
                "live_messages": live_count,
                "bak_messages": bak_count,
                "recommend": "no_action",
                "intentional_compress_shrink": True,
            }
        return {
            "session_id": session_path.stem,
            "live_messages": live_count,
            "bak_messages": bak_count,
            "recommend": "restore",
        }
    return {
        "session_id": session_path.stem,
        "live_messages": live_count,
        "bak_messages": bak_count,
        "recommend": "no_action",
    }


def recover_session(session_path: Path) -> dict:
    """Run focused recovery under the same cross-process SID authority as save."""
    from api.models import SESSION_DIR, _session_publication_authority

    if session_path.parent.resolve() == Path(SESSION_DIR).resolve():
        with _session_publication_authority(session_path.stem):
            return _recover_session_under_authority(session_path)
    return _recover_session_under_authority(session_path)


def _recover_session_under_authority(session_path: Path) -> dict:
    """Restore session_path from its .bak when the bak has more messages.

    Returns a status dict identical to ``inspect_session_recovery_status``
    plus a "restored" boolean.
    """
    status = inspect_session_recovery_status(session_path)
    if status["recommend"] != "restore":
        return {**status, "restored": False}
    bak_path = session_path.with_suffix('.json.bak')
    # Stage the recovery via a tmp copy + atomic replace so a crash mid-restore
    # cannot leave a half-written session.json.
    tmp_path = session_path.with_suffix(f'.json.recover.tmp.{uuid.uuid4().hex}')
    try:
        # Validate the backup's embedded identity before it can replace the
        # current sidecar or influence any media namespace decision.
        _read_recovery_payload(bak_path, session_path.stem)
        shutil.copyfile(bak_path, tmp_path)
        _read_recovery_payload(tmp_path, session_path.stem)
        with open(tmp_path, 'rb') as handle:
            os.fsync(handle.fileno())
        tmp_path.replace(session_path)
        from api.models import _fsync_parent_directory

        _fsync_parent_directory(session_path)
    except OSError as exc:
        logger.warning("recover_session: copy failed for %s: %s", session_path, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {**status, "restored": False, "error": str(exc)}
    try:
        from api.config import SESSION_DIR
        from api.models import retry_session_retention_cleanup

        if session_path.parent.resolve() == Path(SESSION_DIR).resolve():
            cleanup = retry_session_retention_cleanup(session_path.stem, "prune")
            if not cleanup["ok"]:
                raise RuntimeError(f"media cleanup residuals: {cleanup['residuals']!r}")
    except Exception as exc:
        logger.warning("recover_session: media prune failed for %s: %s", session_path, exc)
        return {
            **status,
            "restored": True,
            "media_cleanup_pending": True,
            "error": str(exc),
        }
    logger.warning(
        "recover_session: restored %s from .bak (live=%d → bak=%d messages). "
        "See #1558 for the data-loss class this guards against.",
        session_path.name, status["live_messages"], status["bak_messages"],
    )
    return {**status, "restored": True}


def _state_db_has_session(session_id: str, state_db_path: Path | None) -> bool:
    """Return whether state.db still knows this session.

    The check is deliberately fail-open: recovery must not be prevented by a
    locked, absent, or older-schema state DB. When a DB is readable and has no
    row, treat the orphan backup as a tombstoned/deleted session and skip it.
    """
    if state_db_path is None or not state_db_path.exists():
        return True
    try:
        with sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True) as conn:
            cur = conn.execute(
                "select 1 from sqlite_master where type='table' and name='sessions'"
            )
            if cur.fetchone() is None:
                return True
            cur = conn.execute("select 1 from sessions where id = ? limit 1", (session_id,))
            return cur.fetchone() is not None
    except Exception as exc:
        logger.debug("state_db session tombstone check failed for %s: %s", session_id, exc)
        return True


def _orphaned_backup_live_paths(
    session_dir: Path,
    state_db_path: Path | None = None,
) -> list[Path]:
    """Return live ``<sid>.json`` paths whose ``<sid>.json.bak`` exists.

    ``Path.glob('*.json')`` does not see orphan backups because their suffix is
    ``.bak``. Existing startup recovery only handled shrunken live files; this
    helper covers the crash shape where the live sidecar is gone but the rescue
    copy remains.
    """
    paths: list[Path] = []
    for bak_path in sorted(session_dir.glob('*.json.bak')):
        live_path = bak_path.with_suffix('')
        if live_path.name.startswith('_') or live_path.exists():
            continue
        session_id = live_path.stem
        if _msg_count(bak_path) < 0:
            continue
        # A WebUI session the user deleted must not be resurrected from its
        # surviving .bak on the next boot (#5498). Use the DURABLE tombstone
        # only — not the _index.json heuristic — so a genuine crash that loses
        # the sidecar while its index entry survives is still restored (the
        # crash-recovery case this helper exists for).
        if _durable_tombstone_marks_deleted_webui_session(session_dir, session_id):
            logger.info(
                "recover_all_sessions_on_startup: skipped orphan backup %s; "
                "session is tombstoned as a deleted WebUI session",
                bak_path.name,
            )
            continue
        if not _state_db_has_session(session_id, state_db_path):
            logger.info(
                "recover_all_sessions_on_startup: skipped orphan backup %s; "
                "state.db has no live session row",
                bak_path.name,
            )
            continue
        paths.append(live_path)
    return paths


def _read_state_db_missing_sidecar_rows(
    session_dir: Path,
    state_db_path: Path | None,
    *,
    include_empty: bool = False,
) -> list[dict]:
    """Return WebUI-origin state.db rows whose JSON sidecar is missing."""
    if state_db_path is None or not state_db_path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            session_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            message_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            if not {'id', 'source'}.issubset(session_cols):
                return []
            title_expr = _sql_optional_col('title', session_cols)
            model_expr = _sql_optional_col('model', session_cols)
            started_expr = _sql_optional_col('started_at', session_cols, '0')
            parent_expr = _sql_optional_col('parent_session_id', session_cols)
            msg_count_expr = _sql_optional_col('message_count', session_cols, '0')
            workspace_expr = _sql_optional_col('workspace', session_cols)
            worktree_path_expr = _sql_optional_col('worktree_path', session_cols)
            worktree_branch_expr = _sql_optional_col('worktree_branch', session_cols)
            worktree_repo_root_expr = _sql_optional_col('worktree_repo_root', session_cols)
            worktree_created_at_expr = _sql_optional_col('worktree_created_at', session_cols)
            rows = []
            for row in conn.execute(
                f"""
                SELECT id, source, {title_expr}, {model_expr}, {started_expr},
                       {parent_expr}, {msg_count_expr}, {workspace_expr},
                       {worktree_path_expr}, {worktree_branch_expr},
                       {worktree_repo_root_expr}, {worktree_created_at_expr}
                FROM sessions
                WHERE source = 'webui'
                ORDER BY COALESCE(started_at, 0) DESC
                """
            ).fetchall():
                data = dict(row)
                sid = str(data.get('id') or '').strip()
                if not sid or (session_dir / f"{sid}.json").exists():
                    continue
                # Only the DURABLE delete tombstone suppresses state.db sidecar
                # repair. The _index.json heuristic must NOT gate this path: a
                # genuine crash that loses the sidecar while its index entry
                # survives (no durable tombstone) is exactly the case this repair
                # exists for, and on master it materialized the sidecar. Using
                # _marks_deleted_webui_session() here (which ORs in the index
                # heuristic) wrongly classified that crash as a delete and
                # stopped recovery (#5504 Codex/Opus finding).
                tombstoned = _durable_tombstone_marks_deleted_webui_session(session_dir, sid)
                if tombstoned and not include_empty:
                    continue
                message_rows: list[dict] = []
                if {'session_id', 'role', 'content'}.issubset(message_cols):
                    order = "timestamp, id" if 'timestamp' in message_cols and 'id' in message_cols else "rowid"
                    ts_expr = 'timestamp' if 'timestamp' in message_cols else 'NULL AS timestamp'
                    for msg in conn.execute(
                        f"SELECT role, content, {ts_expr} FROM messages WHERE session_id = ? ORDER BY {order}",
                        (sid,),
                    ).fetchall():
                        message = {
                            'role': msg['role'],
                            'content': msg['content'] or '',
                        }
                        if msg['timestamp'] is not None:
                            message['timestamp'] = msg['timestamp']
                        message_rows.append(message)
                if not message_rows and not include_empty:
                    continue
                data['messages'] = message_rows
                data['_state_db_empty_messages'] = not message_rows
                if tombstoned:
                    data['_state_db_deleted_webui_tombstone'] = True
                rows.append(data)
            return rows
    except Exception as exc:
        logger.debug("state_db sidecar reconciliation scan failed for %s: %s", state_db_path, exc)
        return []


def _sql_optional_col(name: str, columns: set[str], fallback: str = "NULL") -> str:
    return name if name in columns else f"{fallback} AS {name}"


def _state_db_row_to_sidecar(row: dict) -> dict:
    try:
        from api.agent_sessions import normalize_agent_session_source
    except Exception:
        normalize_agent_session_source = None
    source = str(row.get('source') or '').strip().lower()
    source_meta = normalize_agent_session_source(source) if normalize_agent_session_source else {
        'raw_source': source or None,
        'session_source': source or None,
        'source_label': source.title() if source else None,
    }
    started_at = row.get('started_at') or 0
    messages = row.get('messages') if isinstance(row.get('messages'), list) else []
    last_ts = messages[-1].get('timestamp') if messages and isinstance(messages[-1], dict) else started_at
    workspace_value = row.get('workspace') or ''
    compression_recovery = row.get('compression_recovery')
    if not isinstance(compression_recovery, dict):
        compression_recovery = {}
    return {
        'session_id': row.get('id'),
        'title': row.get('title') or 'Recovered WebUI Session',
        'workspace': workspace_value if isinstance(workspace_value, str) else '',
        'message_count': row.get('message_count') if isinstance(row.get('message_count'), int) else len(messages),
        'worktree_path': row.get('worktree_path') or None,
        'worktree_branch': row.get('worktree_branch') or None,
        'worktree_repo_root': row.get('worktree_repo_root') or None,
        'worktree_created_at': row.get('worktree_created_at') or None,
        'model': row.get('model') or 'unknown',
        'model_provider': None,
        'created_at': started_at,
        'updated_at': last_ts or started_at,
        'pinned': False,
        'archived': False,
        'project_id': None,
        'profile': None,
        'input_tokens': 0,
        'output_tokens': 0,
        'estimated_cost': None,
        'personality': None,
        'active_stream_id': None,
        'pending_user_message': None,
        'pending_attachments': [],
        'pending_started_at': None,
        'compression_anchor_visible_idx': None,
        'compression_anchor_message_key': None,
        'compression_anchor_summary': None,
        'context_length': None,
        'threshold_tokens': None,
        'last_prompt_tokens': None,
        'compression_recovery': compression_recovery,
        'recommended_recovery_action': row.get('recommended_recovery_action') or None,
        'compression_recovery_source_session_id': row.get('compression_recovery_source_session_id') or None,
        'compression_recovery_action': row.get('compression_recovery_action') or None,
        'gateway_routing': None,
        'gateway_routing_history': [],
        'llm_title_generated': False,
        'parent_session_id': row.get('parent_session_id'),
        'is_cli_session': False,
        'source_tag': source or None,
        **source_meta,
        'enabled_toolsets': None,
        'composer_draft': {},
        'messages': messages,
        'tool_calls': [],
        '_recovered_from_state_db': True,
    }


def recover_missing_sidecars_from_state_db(session_dir: Path, state_db_path: Path | None) -> dict:
    """Materialize missing WebUI JSON sidecars from canonical state.db rows."""
    rows = _read_state_db_missing_sidecar_rows(session_dir, state_db_path)
    materialized = 0
    details: list[dict] = []
    session_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        sid = str(row.get('id') or '').strip()
        if not sid:
            continue
        target = session_dir / f"{sid}.json"
        if target.exists():
            continue
        payload = _state_db_row_to_sidecar(row)
        # Per-process/per-thread tmp suffix to avoid corruption under
        # concurrent reconciliation calls (matches api/models.py:484
        # Session.save() convention).
        tmp_suffix = f".json.reconcile.tmp.{os.getpid()}.{threading.current_thread().ident}"
        tmp = target.with_suffix(tmp_suffix)
        detail_recorded = False
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            details.append({'session_id': sid, 'materialized': False, 'error': str(exc)})
            continue
        # Atomic create-or-fail: os.link() refuses to overwrite an existing
        # target. Closes the TOCTOU window between the target.exists() check
        # above and the rename — a concurrent Session.save() for the same SID
        # will win and we silently skip rather than overwrite a live sidecar.
        materialized_now = False
        try:
            os.link(str(tmp), str(target))
            materialized_now = True
        except FileExistsError:
            # Live sidecar appeared between the check and the link — keep it.
            pass
        except OSError as exc:
            details.append({'session_id': sid, 'materialized': False, 'error': str(exc)})
            detail_recorded = True
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        if materialized_now:
            materialized += 1
            details.append({'session_id': sid, 'materialized': True, 'messages': len(payload.get('messages') or [])})
        elif not detail_recorded:
            details.append({'session_id': sid, 'materialized': False, 'skipped': 'sidecar_appeared_during_reconcile'})
    return {'scanned': len(rows), 'materialized': materialized, 'details': details}


def _new_audit_item(
    session_id: str,
    kind: str,
    category: str,
    recommendation: str,
    live_messages: int = -1,
    bak_messages: int = -1,
    **extra,
) -> dict:
    item = {
        "session_id": session_id,
        "kind": kind,
        "category": category,
        "recommendation": recommendation,
        "live_messages": live_messages,
        "bak_messages": bak_messages,
    }
    item.update(extra)
    return item


def _read_index_session_ids(index_path: Path) -> set[str]:
    try:
        data = json.loads(index_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    ids: set[str] = set()
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get('session_id'), str):
            ids.add(entry['session_id'])
    return ids


def _index_marks_deleted_webui_session(session_dir: Path, sid: str) -> bool:
    """Return True when _index.json has a WebUI-like entry whose sidecar is missing.

    This is a delete-route heuristic for cases where index pruning and durable
    tombstone recording both failed; it can also match other sidecar-loss modes.
    """
    if not sid or (session_dir / f"{sid}.json").exists():
        return False
    index_path = session_dir / '_index.json'
    if not index_path.exists():
        return False
    try:
        data = json.loads(index_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, list):
        return False
    for entry in data:
        if not isinstance(entry, dict) or entry.get('session_id') != sid:
            continue
        srcs = [
            str(entry.get('source_tag') or '').strip().lower(),
            str(entry.get('raw_source') or '').strip().lower(),
            str(entry.get('session_source') or '').strip().lower(),
        ]
        explicit = [src for src in srcs if src]
        if any(src in ('webui', 'fork') for src in explicit):
            return True
        if explicit:
            return False
        is_cli = entry.get('is_cli_session') is True
        is_read_only = bool(entry.get('read_only') or entry.get('is_read_only'))
        return not (is_cli or is_read_only)
    return False


def _durable_tombstone_marks_deleted_webui_session(session_dir: Path, sid: str) -> bool:
    """Return True when the durable WebUI delete tombstone contains sid."""
    if not sid or (session_dir / f"{sid}.json").exists():
        return False
    try:
        from api import models as _models

        if Path(_models.SESSION_DIR).resolve() == session_dir.resolve():
            return sid in _models._load_webui_deleted_session_tombstone()
    except Exception:
        return True
    tombstone_path = session_dir / '_deleted_webui_sessions.json'
    try:
        raw = json.loads(tombstone_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError, ValueError):
        return True
    if not isinstance(raw, dict):
        return True
    try:
        version = int(raw.get('version', 0))
    except (TypeError, ValueError):
        return True
    if version != 1:
        return True
    ids = raw.get('ids')
    if not isinstance(ids, list):
        return True
    return sid in {str(value).strip() for value in ids if str(value or '').strip()}


def _marks_deleted_webui_session(session_dir: Path, sid: str) -> bool:
    return (
        _index_marks_deleted_webui_session(session_dir, sid)
        or _durable_tombstone_marks_deleted_webui_session(session_dir, sid)
    )


def audit_session_recovery(session_dir: Path, state_db_path: Path | None = None) -> dict:
    """Read-only audit of session recovery state.

    The audit intentionally does not mutate files. It classifies only the safe
    recovery primitives this module knows how to perform: backup restores and
    derived index rebuilds. Call ``recover_all_sessions_on_startup`` separately
    for safe repairs.
    """
    if not session_dir.exists():
        return {
            "status": "ok",
            "summary": {"ok": 0, "repairable": 0, "unsafe_to_repair": 0},
            "items": [],
        }

    items: list[dict] = []
    live_paths = sorted(p for p in session_dir.glob('*.json') if not p.name.startswith('_'))
    live_ids = {p.stem for p in live_paths}
    state_db_missing_rows = _read_state_db_missing_sidecar_rows(
        session_dir,
        state_db_path,
        include_empty=True,
    )
    state_db_deleted_webui_ids = {
        str(row.get('id') or '')
        for row in state_db_missing_rows
        if row.get('_state_db_deleted_webui_tombstone')
    }

    for live_path in live_paths:
        status = inspect_session_recovery_status(live_path)
        if status.get('recommend') == 'restore':
            items.append(_new_audit_item(
                status['session_id'],
                "shrunken_live",
                "repairable",
                "restore_from_bak",
                status.get('live_messages', -1),
                status.get('bak_messages', -1),
            ))

    # Track sids already classified as deleted-webui-tombstone from the orphan
    # .bak branch below, so the later state_db_missing_rows loop doesn't emit a
    # duplicate audit item for the same sid (both a surviving .bak and a state.db
    # row can exist for one deleted session). (#5504)
    _bak_tombstoned_ids: set[str] = set()

    for bak_path in sorted(session_dir.glob('*.json.bak')):
        live_path = bak_path.with_suffix('')
        if live_path.exists() or live_path.name.startswith('_'):
            continue
        session_id = live_path.stem
        bak_messages = _msg_count(bak_path)
        if bak_messages < 0:
            items.append(_new_audit_item(
                session_id, "malformed_orphan_backup", "unsafe_to_repair", "manual_review", -1, bak_messages
            ))
        elif _durable_tombstone_marks_deleted_webui_session(session_dir, session_id):
            # The user deleted this WebUI session; its surviving .bak must NOT be
            # reported as repairable. DURABLE tombstone only — the _index.json
            # heuristic must not suppress a genuine crash whose index survived
            # (that case is legitimately repairable). Matches the startup-recovery
            # skip + the state.db recovery path (#5504 Codex/Opus finding).
            _bak_tombstoned_ids.add(session_id)
            items.append(_new_audit_item(
                session_id,
                "state_db_deleted_webui_tombstone",
                "unsafe_to_repair",
                "deleted_session_skipped",
                -1,
                bak_messages,
            ))
        elif _state_db_has_session(session_id, state_db_path):
            items.append(_new_audit_item(
                session_id, "orphan_backup", "repairable", "restore_from_bak", -1, bak_messages
            ))
        else:
            items.append(_new_audit_item(
                session_id,
                "orphan_backup_without_state_row",
                "unsafe_to_repair",
                "manual_review",
                -1,
                bak_messages,
            ))

    index_path = session_dir / '_index.json'
    if index_path.exists():
        index_ids = _read_index_session_ids(index_path)
        for session_id in sorted(index_ids - live_ids):
            if (
                session_id in state_db_deleted_webui_ids
                or _durable_tombstone_marks_deleted_webui_session(session_dir, session_id)
            ):
                continue
            items.append(_new_audit_item(
                session_id, "index_missing_file", "repairable", "rebuild_index"
            ))
        for session_id in sorted(live_ids - index_ids):
            items.append(_new_audit_item(
                session_id, "index_missing_entry", "repairable", "rebuild_index",
                _msg_count(session_dir / f"{session_id}.json"), -1,
            ))

    for row in state_db_missing_rows:
        sid = str(row.get('id') or '')
        if sid in _bak_tombstoned_ids:
            # Already emitted a deleted-webui-tombstone audit item from the
            # orphan .bak branch above (surviving .bak + state.db row both exist
            # for this one deleted session) — don't double-count. (#5504)
            continue
        if row.get('_state_db_deleted_webui_tombstone'):
            items.append(_new_audit_item(
                sid,
                "state_db_deleted_webui_tombstone",
                "unsafe_to_repair",
                "deleted_session_skipped",
                -1,
                -1,
            ))
            continue
        if row.get('_state_db_empty_messages'):
            items.append(_new_audit_item(
                sid,
                "state_db_orphan_webui_row",
                "unsafe_to_repair",
                "manual_review",
                -1,
                -1,
            ))
            continue
        items.append(_new_audit_item(
            sid,
            "state_db_missing_sidecar",
            "repairable",
            "materialize_from_state_db",
            -1,
            -1,
        ))

    for session_id in iter_turn_journal_session_ids(session_dir):
        journal = read_turn_journal(session_id, session_dir=session_dir)
        states, _ = derive_turn_journal_states(journal.get('events') or [])
        live_path = session_dir / f"{session_id}.json"
        live_messages = _msg_count(live_path)
        existing_user_messages: set[str] = set()
        try:
            payload = _read_recovery_payload(live_path, session_id)
            for message in payload.get('messages') or []:
                if isinstance(message, dict) and message.get('role') == 'user':
                    existing_user_messages.add(str(message.get('content') or '').strip())
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        for turn_id, event in sorted(states.items()):
            if is_terminal_turn_event(event):
                continue
            content = str(event.get('content') or '').strip()
            if not content or content in existing_user_messages:
                continue
            items.append(_new_audit_item(
                session_id,
                "turn_journal_pending_turn",
                "repairable",
                "audit_only_pending_turn_journal",
                live_messages,
                -1,
                turn_id=turn_id,
                event=str(event.get('event') or ''),
            ))

    summary = {"ok": len(live_paths), "repairable": 0, "unsafe_to_repair": 0}
    for item in items:
        category = item.get('category')
        if category in summary:
            summary[category] += 1
    if summary["unsafe_to_repair"]:
        overall = "needs_manual_review"
    elif summary["repairable"]:
        overall = "warn"
    else:
        overall = "ok"
    return {"status": overall, "summary": summary, "items": items}


def repair_safe_session_recovery(session_dir: Path, state_db_path: Path | None = None) -> dict:
    """Run safe, deterministic session recovery repairs.

    This mutates only repairable classes already handled by startup recovery:
    shrunken live sidecars and orphan backups that are not tombstoned by a
    readable state.db. Unsafe audit findings remain for manual review.
    """
    before = audit_session_recovery(session_dir, state_db_path=state_db_path)
    backup_repair = recover_all_sessions_on_startup(
        session_dir,
        rebuild_index=True,
        state_db_path=state_db_path,
    )
    sidecar_repair = recover_missing_sidecars_from_state_db(session_dir, state_db_path)
    if sidecar_repair.get('materialized'):
        try:
            _rebuild_recovery_session_index(session_dir)
        except Exception as exc:
            logger.warning("repair_safe_session_recovery: index rebuild after state.db reconciliation failed: %s", exc)
    after = audit_session_recovery(session_dir, state_db_path=state_db_path)
    unsafe_remaining = int((after.get("summary") or {}).get("unsafe_to_repair") or 0)
    repairable_remaining = int((after.get("summary") or {}).get("repairable") or 0)
    clean = unsafe_remaining == 0 and repairable_remaining == 0
    return {
        "clean": clean,
        "ok": clean,
        "repaired": int(backup_repair.get("restored") or 0) + int(sidecar_repair.get("materialized") or 0),
        "before": before,
        "backup_repair": backup_repair,
        "sidecar_repair": sidecar_repair,
        "after": after,
    }


def recover_incomplete_compression_transactions(session_dir: Path) -> dict:
    """Finalize or roll back durable compression intents after process death."""
    from api.models import (
        SESSION_DIR,
        Session,
        _INDEX_WRITE_LOCK,
        _clear_session_incarnation_claim,
        _cross_process_file_lock,
        _durable_unlink,
        _session_publication_authority,
        _write_session_index,
        rollback_reserved_session_destination,
    )

    if session_dir.resolve() != Path(SESSION_DIR).resolve():
        return {"finalized": 0, "rolled_back": 0, "residuals": []}
    root = session_dir / "_compression_transactions"
    try:
        paths = sorted(root.glob("*.json"))
    except FileNotFoundError:
        paths = []
    finalized = 0
    rolled_back = 0
    residuals = []
    for path in paths:
        try:
            intent = json.loads(path.read_bytes())
            sid = path.stem
            if isinstance(intent, dict) and intent.get("version") == 2:
                intent = _validate_compression_transaction_intent(
                    session_dir,
                    intent,
                    expected_new_sid=sid,
                )
                old_sid = intent["old_session_id"]
                phase = intent["phase"]
                token = intent.get("incarnation_token")
                with contextlib.ExitStack() as authority:
                    for locked_sid in sorted({old_sid, sid}):
                        authority.enter_context(
                            _session_publication_authority(locked_sid)
                        )
                    authority.enter_context(_INDEX_WRITE_LOCK)
                    authority.enter_context(
                        _cross_process_file_lock("session-index")
                    )
                    if phase == "migrations_complete":
                        sidecar = session_dir / f"{sid}.json"
                        payload = _read_recovery_payload(sidecar, sid)
                        if payload.get("publication_incarnation") != token:
                            raise ValueError(
                                "compression continuation incarnation mismatch"
                            )
                        session = Session(**payload)
                        _write_session_index(updates=[session])
                        _retire_compression_transaction(session_dir, sid)
                        finalized += 1
                        continue
                    if phase == "source_restored":
                        # The source bytes are already restored, but a prior
                        # destination cleanup may have retained a quarantined
                        # media entry rather than risk unlinking a replacement.
                        # Keep this intent until that exact destination retry
                        # succeeds; retiring it here would lose the retry
                        # authority while leaving the SID blocked by its
                        # cleanup residual.
                        result = rollback_reserved_session_destination(
                            sid,
                            token,
                            durable_intent_path=path,
                        )
                        if not result.get("ok"):
                            raise RuntimeError(
                                "compression rollback residuals: "
                                f"{result.get('residuals')!r}"
                            )
                        _retire_compression_transaction(session_dir, sid)
                        rolled_back += 1
                        continue
                    if phase in {"initializing", "prepared"}:
                        # Neither source nor destination may mutate before the
                        # reserved phase. Partial backup staging is disposable.
                        _retire_compression_transaction(session_dir, sid)
                        rolled_back += 1
                        continue
                    _restore_compression_transaction_source(session_dir, intent)
                    intent = _advance_compression_transaction(
                        session_dir,
                        intent,
                        "source_restored",
                    )
                    result = rollback_reserved_session_destination(
                        sid,
                        token,
                        durable_intent_path=path,
                    )
                    if not result.get("ok"):
                        raise RuntimeError(
                            "compression rollback residuals: "
                            f"{result.get('residuals')!r}"
                        )
                    _retire_compression_transaction(session_dir, sid)
                    rolled_back += 1
                    continue
            token = intent.get("incarnation_token") if isinstance(intent, dict) else None
            phase = intent.get("phase") if isinstance(intent, dict) else None
            if (
                not isinstance(intent, dict)
                or intent.get("version") != 1
                or intent.get("new_session_id") != sid
                or not isinstance(token, str)
                or not token
                or phase not in {"prepared", "sidecar_published", "migrations_complete"}
            ):
                raise ValueError("invalid compression transaction intent")
            sidecar = session_dir / f"{sid}.json"
            if sidecar.exists():
                payload = _read_recovery_payload(sidecar, sid)
                if payload.get("publication_incarnation") != token:
                    raise ValueError("compression continuation incarnation mismatch")
                session = Session(**payload)
                _write_session_index(updates=[session])
                _clear_session_incarnation_claim(sid, expected_token=token)
                _durable_unlink(path)
                finalized += 1
                continue
            result = rollback_reserved_session_destination(sid, token)
            if not result.get("ok"):
                raise RuntimeError(f"compression rollback residuals: {result.get('residuals')!r}")
            _durable_unlink(path)
            rolled_back += 1
        except Exception as exc:
            logger.warning("compression transaction recovery failed for %s", path, exc_info=True)
            residuals.append(
                {
                    "transaction": path.name,
                    "error": type(exc).__name__,
                }
            )
    return {
        "finalized": finalized,
        "rolled_back": rolled_back,
        "residuals": residuals,
    }


def recover_all_sessions_on_startup(
    session_dir: Path,
    rebuild_index: bool = False,
    state_db_path: Path | None = None,
) -> dict:
    """Scan session_dir for shrunken/orphaned sessions and restore from .bak.

    Returns {"scanned": N, "restored": M, "orphaned_backups": K, "details": [...]}.
    """
    if not session_dir.exists():
        return {"scanned": 0, "restored": 0, "orphaned_backups": 0, "details": []}
    compression_transactions = recover_incomplete_compression_transactions(session_dir)
    restored = 0
    details: list[dict] = []
    live_paths = [path for path in sorted(session_dir.glob('*.json')) if not path.name.startswith('_')]
    orphan_paths = _orphaned_backup_live_paths(session_dir, state_db_path=state_db_path)
    # Only sessions with a backup can be restored through this startup path.
    # Older code called recover_session() for every live sidecar, and
    # inspect_session_recovery_status() read the complete JSON file before even
    # checking whether <sid>.json.bak existed. Large WebUI installs therefore
    # parsed the entire session corpus on every boot even when there was
    # nothing to recover. Keep the public scanned count compatible, but limit
    # expensive reads to actual recovery candidates.
    recovery_paths = [path for path in live_paths if path.with_suffix('.json.bak').exists()]
    scanned = len(live_paths) + len(orphan_paths)
    for path in [*recovery_paths, *orphan_paths]:
        try:
            result = recover_session(path)
        except Exception as exc:
            # Defensive: a malformed session file shouldn't break recovery
            # for the rest. Log and continue.
            logger.warning(
                "recover_all_sessions_on_startup: skipped %s due to %s: %s",
                path.name, type(exc).__name__, exc,
            )
            continue
        if result.get("restored"):
            restored += 1
            details.append(result)
    if restored:
        logger.warning(
            "recover_all_sessions_on_startup: restored %d/%d sessions from .bak. "
            "If you weren't expecting this, check the session list for missing "
            "messages — see #1558.", restored, scanned,
        )
    if rebuild_index:
        try:
            if restored or not (session_dir / '_index.json').exists():
                _rebuild_recovery_session_index(session_dir)
        except Exception as exc:
            logger.warning("recover_all_sessions_on_startup: index rebuild failed: %s", exc)
    return {
        "scanned": scanned,
        "restored": restored,
        "orphaned_backups": len(orphan_paths),
        "details": details,
        "compression_transactions": compression_transactions,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Audit Hermes WebUI session recovery state")
    parser.add_argument("--audit", action="store_true", help="run a read-only recovery audit")
    parser.add_argument("--session-dir", type=Path, required=True, help="path to WebUI sessions directory")
    parser.add_argument("--state-db", type=Path, default=None, help="optional Hermes state.db path")
    parser.add_argument("--repair-safe", action="store_true", help="run safe deterministic repairs after auditing")
    args = parser.parse_args()
    if args.repair_safe:
        report = repair_safe_session_recovery(args.session_dir, state_db_path=args.state_db)
    elif args.audit:
        report = audit_session_recovery(args.session_dir, state_db_path=args.state_db)
    else:
        parser.error("choose --audit or --repair-safe")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
