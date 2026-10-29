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
* return metadata/counts only: no prompts, results, secrets, or rows belonging
  to another profile;
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
import sqlite3
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


def session_delegation_status(session_id: str, *, profile: str | None = None) -> dict[str, Any]:
    """Advisory read-only snapshot of one session's live async delegations.

    Returns counts only. ``status`` is ``"ok"`` when the durable ledger was
    readable and ``"unknown"`` when it was not — in the unknown case the count
    fields are ``None`` so a caller can never mistake "could not check" for
    "nothing is running". ``memory_live`` is a bool when the in-process Agent
    registry is reachable and ``None`` when it is not.
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        return {
            "session_id": session_id,
            "status": "unknown",
            "running": None,
            "awaiting_delivery": None,
            "memory_live": None,
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
    return {
        "session_id": session_id,
        "status": status,
        "running": running,
        "awaiting_delivery": awaiting,
        "memory_live": memory_live,
    }
