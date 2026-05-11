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
import json
import logging
import shutil
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


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
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return -1
    if not isinstance(data, dict):
        return -1
    msgs = data.get('messages')
    return len(msgs) if isinstance(msgs, list) else -1


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
    tmp_path = session_path.with_suffix('.json.recover.tmp')
    try:
        shutil.copyfile(bak_path, tmp_path)
        tmp_path.replace(session_path)
    except OSError as exc:
        logger.warning("recover_session: copy failed for %s: %s", session_path, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {**status, "restored": False, "error": str(exc)}
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
        if _msg_count(bak_path) < 0:
            continue
        session_id = live_path.stem
        if not _state_db_has_session(session_id, state_db_path):
            logger.info(
                "recover_all_sessions_on_startup: skipped orphan backup %s; "
                "state.db has no live session row",
                bak_path.name,
            )
            continue
        paths.append(live_path)
    return paths


def _read_state_db_missing_sidecar_rows(session_dir: Path, state_db_path: Path | None) -> list[dict]:
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
            rows = []
            for row in conn.execute(
                f"""
                SELECT id, source, {title_expr}, {model_expr}, {started_expr},
                       {parent_expr}, {msg_count_expr}
                FROM sessions
                WHERE source = 'webui'
                ORDER BY COALESCE(started_at, 0) DESC
                """
            ).fetchall():
                data = dict(row)
                sid = str(data.get('id') or '').strip()
                if not sid or (session_dir / f"{sid}.json").exists():
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
                if not message_rows:
                    continue
                data['messages'] = message_rows
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
    return {
        'session_id': row.get('id'),
        'title': row.get('title') or 'Recovered WebUI Session',
        'workspace': '',
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
        tmp = target.with_suffix('.json.reconcile.tmp')
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            details.append({'session_id': sid, 'materialized': False, 'error': str(exc)})
            continue
        materialized += 1
        details.append({'session_id': sid, 'materialized': True, 'messages': len(payload.get('messages') or [])})
    return {'scanned': len(rows), 'materialized': materialized, 'details': details}


def _new_audit_item(
    session_id: str,
    kind: str,
    category: str,
    recommendation: str,
    live_messages: int = -1,
    bak_messages: int = -1,
) -> dict:
    return {
        "session_id": session_id,
        "kind": kind,
        "category": category,
        "recommendation": recommendation,
        "live_messages": live_messages,
        "bak_messages": bak_messages,
    }


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

    for bak_path in sorted(session_dir.glob('*.json.bak')):
        live_path = bak_path.with_suffix('')
        if live_path.exists() or live_path.name.startswith('_'):
            continue
        bak_messages = _msg_count(bak_path)
        session_id = live_path.stem
        if bak_messages < 0:
            items.append(_new_audit_item(
                session_id, "malformed_orphan_backup", "unsafe_to_repair", "manual_review", -1, bak_messages
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
            items.append(_new_audit_item(
                session_id, "index_missing_file", "repairable", "rebuild_index"
            ))
        for session_id in sorted(live_ids - index_ids):
            items.append(_new_audit_item(
                session_id, "index_missing_entry", "repairable", "rebuild_index",
                _msg_count(session_dir / f"{session_id}.json"), -1,
            ))

    for row in _read_state_db_missing_sidecar_rows(session_dir, state_db_path):
        sid = str(row.get('id') or '')
        items.append(_new_audit_item(
            sid,
            "state_db_missing_sidecar",
            "repairable",
            "materialize_from_state_db",
            -1,
            -1,
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
            from api.models import _write_session_index
            _write_session_index(updates=None)
        except Exception as exc:
            logger.warning("repair_safe_session_recovery: index rebuild after state.db reconciliation failed: %s", exc)
    after = audit_session_recovery(session_dir, state_db_path=state_db_path)
    unsafe_remaining = int((after.get("summary") or {}).get("unsafe_to_repair") or 0)
    repairable_remaining = int((after.get("summary") or {}).get("repairable") or 0)
    return {
        "ok": unsafe_remaining == 0 and repairable_remaining == 0,
        "repaired": int(backup_repair.get("restored") or 0) + int(sidecar_repair.get("materialized") or 0),
        "before": before,
        "backup_repair": backup_repair,
        "sidecar_repair": sidecar_repair,
        "after": after,
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
    scanned = 0
    restored = 0
    details: list[dict] = []
    live_paths = [path for path in sorted(session_dir.glob('*.json')) if not path.name.startswith('_')]
    orphan_paths = _orphaned_backup_live_paths(session_dir, state_db_path=state_db_path)
    for path in [*live_paths, *orphan_paths]:
        # Skip non-session JSON files in the same dir:
        # - ``_index.json`` is a top-level list of session metadata
        # - any future non-session JSON marked with the ``_`` convention is
        #   skipped automatically (project convention for system files in
        #   directories that otherwise hold user data)
        scanned += 1
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
                from api.models import _write_session_index
                _write_session_index(updates=None)
            except Exception as exc:
                logger.warning("recover_all_sessions_on_startup: index rebuild failed: %s", exc)
    return {
        "scanned": scanned,
        "restored": restored,
        "orphaned_backups": len(orphan_paths),
        "details": details,
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
