"""Read-only per-session async-delegation status (#8019 contributor slice).

The #8019 report is an observability gap, not a routing defect: async-delegation
completions are delivered to their dispatch-time owner by design, and ``/new``
must not rewrite that return address. What a client cannot do today is learn,
*before* switching away, that a session still owns live delegations.

This module is the read-only advisory slice the maintainer approved:

* resolve the authenticated request profile and exact WebUI owner;
* distinguish work still running from completions waiting for delivery;
* report an unavailable source as ``unknown``, never as zero — a zero read by
  a pre-switch guard would silently bless an unsafe switch;
* return counts, plus live child metadata when the in-process registry can be
  read (id, status, tool name, count, age): no prompts, results, secrets,
  paths, or rows belonging to another profile;
* stay advisory: a snapshot before switching is not an atomic guarantee that no
  work can arrive afterwards.

Two sources are consulted, deliberately independently:

``durable``
    The profile's own ``state.db`` ``async_delegations`` table (Agent-side
    ledger, shared per profile via HERMES_HOME). Opened read-only through a
    private URI connection so this probe never takes the process-global
    ``_cron_env_lock`` (#7295) and never mutates process environment.
``memory``
    The Agent's in-process registry via ``has_live_for_session`` — the first
    place a fresh dispatch lands, before the durable insert. Absent when the
    Agent package is not importable from this process, which is reported as
    ``None`` (unknown), not ``False``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from api.agent_sessions import state_db_readonly_uri

logger = logging.getLogger(__name__)

# Mirrors tools/async_delegation.py:_LIVE_STATES (running/stalling/finalizing).
_LIVE_STATES = ("running", "stalling", "finalizing")
# Terminal states whose completion can still be waiting for delivery.
# Full producer contract (tools/async_delegation.py): completed/error/
# interrupted/cancelled are normal finalizations; ``stalled`` is written by
# the stall watchdog's forced ``_finalize``; ``unknown`` by
# ``recover_abandoned_delegations`` when the owning process disappeared. Both
# persist with delivery_state='pending', so omitting them tells a pre-switch
# client "nothing is waiting" exactly when a completion IS waiting.
_TERMINAL_STATES = (
    "completed",
    "error",
    "interrupted",
    "cancelled",
    "stalled",
    "unknown",
)

# Durable ledger columns (tools/async_delegation.py:_persist_dispatch maps the
# dispatch-time session_key INTO origin_session; the in-memory record keeps the
# session_key field name). origin_ui_session_id / parent_session_id match.
_SELECTOR_COLUMNS = ("origin_ui_session_id", "origin_session", "parent_session_id")

# Live-child rows are metadata the CLI monitor already shows (tool name, not
# the goal or any transcript). A value that does not look like an identifier
# is dropped rather than forwarded.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_LIVE_CHILD_STATUSES = frozenset({"running", "stalling", "finalizing"})
_TERMINAL_STATES_SET = frozenset(_TERMINAL_STATES)


def _profile_state_db_path(profile: str | None = None) -> Path | None:
    """Locate the state.db that owns *profile*'s delegation ledger.

    Returns None when the profile home cannot be resolved — an unavailable
    source the caller must report as unknown, never as a zero count.
    """
    try:
        from api.models import _active_state_db_path, _get_profile_home

        if isinstance(profile, str) and profile:
            return _get_profile_home(profile) / "state.db"
        return _active_state_db_path()
    except Exception:
        logger.debug("delegation status: profile db path unavailable", exc_info=True)
        return None


def _memory_live_probe(session_id: str) -> bool | None:
    """Best-effort in-process live-delegation probe; None means unknown.

    ``tools.async_delegation`` holds the Agent's in-memory dispatch registry.
    It is importable only when this process runs inside/alongside the Agent;
    absence or any failure is an unknown, never a False.
    """
    if not session_id:
        return None
    try:
        from tools.async_delegation import has_live_for_session

        return bool(has_live_for_session(origin_ui_session_id=session_id))
    except Exception:
        return None


def _durable_counts(db_path: Path | None, session_id: str) -> tuple[int, int] | None:
    """Return (running, awaiting_delivery) for *session_id* from *db_path*.

    None means the durable source could not be read (unresolvable profile
    home, corrupt/locked/missing table) — the caller must surface that as
    unknown, not as zero.
    """
    if db_path is None:
        # Resolver failed: the source is unavailable, which is NOT the same
        # as a ledger this profile never created. Unknown, never zero.
        return None
    try:
        if not db_path.exists():
            # No state.db at all is a truthful zero: this profile has never
            # persisted a delegation ledger.
            return 0, 0
        # Percent-encode the path like every other read-only state.db reader
        # (api.agent_sessions): a raw f"file:{path}" lets '%xx', '?' or '#' in a
        # profile home re-address the read to another file or drop ?mode=ro.
        uri = state_db_readonly_uri(db_path.resolve())
    except OSError:
        # An unreadable profile directory is an unavailable source, not a 500.
        logger.debug("delegation status: cannot stat %s", db_path, exc_info=True)
        return None
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        where = " OR ".join(f"{col} = ?" for col in _SELECTOR_COLUMNS)
        rows = conn.execute(
            f"SELECT state, delivery_state FROM async_delegations WHERE {where}",
            (session_id, session_id, session_id),
        ).fetchall()
    except sqlite3.Error:
        logger.debug("delegation status: durable read failed for %s", db_path, exc_info=True)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    running = sum(1 for state, _ in rows if (state or "") in _LIVE_STATES)
    awaiting = sum(
        1
        for state, delivery in rows
        if (state or "") in _TERMINAL_STATES and (delivery or "") == "pending"
    )
    return running, awaiting


def _token(value: Any, *, pattern: re.Pattern[str] = _TOKEN_RE) -> str:
    text = str(value or "").strip()
    if not text or pattern.fullmatch(text) is None:
        return ""
    return text


def _load_subagent_records() -> list[dict[str, Any]] | None:
    """Snapshot the in-process subagent registry, or None when it cannot be read.

    ``list_active_subagents`` strips the child agent object, and the session id
    the sidebar already uses lives on that object. The snapshot is taken under
    the registry lock and only the fields ``_children_from_records`` keeps are
    returned to callers. An import failure is unknown (``None``), not an empty
    list: an empty list means the registry was readable and nothing matched.
    """
    try:
        from tools.delegate_tool_registry import (
            _active_subagents,
            _active_subagents_lock,
        )
    except Exception:
        logger.debug("delegation status: subagent registry unavailable", exc_info=True)
        return None
    try:
        with _active_subagents_lock:
            return [dict(record) for record in _active_subagents.values()]
    except Exception:
        logger.debug("delegation status: subagent registry read failed", exc_info=True)
        return None


def _owner_ids(record: dict[str, Any]) -> set[str]:
    owners = set()
    for key in ("owner_session_id", "owner_agent_session_id"):
        token = str(record.get(key) or "").strip()
        if token:
            owners.add(token)
    return owners


def _record_belongs(
    record: dict[str, Any],
    session_id: str,
    by_id: dict[str, dict[str, Any]],
    seen: set[str],
) -> bool:
    subagent_id = str(record.get("subagent_id") or "").strip()
    if subagent_id and subagent_id in seen:
        return False
    if subagent_id:
        seen.add(subagent_id)
    if session_id in _owner_ids(record):
        return True
    parent_id = str(record.get("parent_id") or "").strip()
    parent = by_id.get(parent_id) if parent_id else None
    if parent is None:
        return False
    return _record_belongs(parent, session_id, by_id, seen)


def _child_session_id(record: dict[str, Any]) -> str | None:
    agent = record.get("agent")
    raw = getattr(agent, "session_id", None) if agent is not None else None
    token = _token(raw, pattern=_SESSION_ID_RE)
    return token or None


def _children_from_records(
    records: list[dict[str, Any]],
    session_id: str,
    now: float,
) -> list[dict[str, Any]]:
    """Public child rows for one session. No goal, path, transcript, or result."""
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        subagent_id = str(record.get("subagent_id") or "").strip()
        if subagent_id:
            by_id[subagent_id] = record
    children: list[dict[str, Any]] = []
    for record in by_id.values():
        if not _record_belongs(record, session_id, by_id, set()):
            continue
        started = record.get("started_at")
        age: int | None
        if isinstance(started, (int, float)):
            age = max(0, int(now - float(started)))
        else:
            age = None
        try:
            tool_count = int(record.get("tool_count") or 0)
        except (TypeError, ValueError):
            tool_count = 0
        if tool_count < 0:
            tool_count = 0
        status = _token(record.get("status")) or "running"
        if status not in _LIVE_CHILD_STATUSES | _TERMINAL_STATES_SET:
            status = "running"
        children.append({
            "subagent_id": _token(record.get("subagent_id")),
            "status": status,
            "last_tool": _token(record.get("last_tool")),
            "tool_count": tool_count,
            "age_seconds": age,
            "child_session_id": _child_session_id(record),
        })
    children.sort(key=lambda row: (row.get("age_seconds") is None, row.get("age_seconds") or 0, row["subagent_id"]))
    return children


def _memory_children(session_id: str) -> list[dict[str, Any]] | None:
    """Live children for *session_id*, or None when the registry cannot be read."""
    if not session_id:
        return None
    records = _load_subagent_records()
    if records is None:
        return None
    try:
        return _children_from_records(records, session_id, time.time())
    except Exception:
        logger.debug("delegation status: child snapshot failed", exc_info=True)
        return None


def session_delegation_status(session_id: str, *, profile: str | None = None) -> dict[str, Any]:
    """Advisory read-only snapshot of one session's live async delegations.

    Returns the existing counts unchanged. ``children`` is an extra list of
    live subagent metadata when the in-process registry can be read, and
    ``None`` when it cannot — never an empty list standing in for "unknown".
    Each child is an id, a status token, a tool name, a count, and an age.
    Goals, transcripts, paths, and results are not included.

    ``status`` is ``"ok"`` when the durable ledger was readable and
    ``"unknown"`` when it was not — in the unknown case the count fields are
    ``None`` so a caller can never mistake "could not check" for "nothing is
    running". ``memory_live`` is a bool when the in-process Agent registry is
    reachable and ``None`` when it is not.
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        return {
            "session_id": session_id,
            "status": "unknown",
            "running": None,
            "awaiting_delivery": None,
            "memory_live": None,
            "children": None,
        }

    durable = _durable_counts(_profile_state_db_path(profile), session_id)
    try:
        memory_live = _memory_live_probe(session_id)
    except Exception:
        # A raising probe is an unknown, never a False and never a 500.
        logger.debug("delegation status: memory probe raised", exc_info=True)
        memory_live = None

    if durable is None:
        status = "unknown"
        running: int | None = None
        awaiting: int | None = None
    else:
        status = "ok"
        running, awaiting = durable
    try:
        children = _memory_children(session_id)
    except Exception:
        logger.debug("delegation status: child probe raised", exc_info=True)
        children = None
    return {
        "session_id": session_id,
        "status": status,
        "running": running,
        "awaiting_delivery": awaiting,
        "memory_live": memory_live,
        "children": children,
    }
