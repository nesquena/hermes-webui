"""Wake WebUI sessions for async-delegation completions delivered by the Gateway.

With the Gateway chat backend the agent runs in the Gateway process, so its
``async_delegation`` completions never reach this process's
``process_registry.completion_queue`` (``api.background_process`` drain).
For api_server sessions the Gateway instead persists each completion as a
delivery row in the profile's state.db (``display_kind`` ``async_delegation_complete``,
or ``hidden`` for presentation-suppressed notices) and deliberately starts no turn: the client owns the next turn
(``gateway.wake.persist_delegation_delivery``). Without a consumer here the
parent agent only sees the result when the user next types.

This poller leases those rows with the Agent's reservation API
(``SessionDB.reserve_caller_history_deliveries``), copies each result once into
the session's model-facing ``context_messages`` (keyed by row id, tagged
``_source: process_wakeup`` so the transcript shows it as a wakeup notice, not a
user turn), and starts a wakeup turn whose prompt is a FIXED nudge. The results
and the lease (``delegation_reservation``, bound to the wake turn's stream id)
are written by ``start_session_turn``'s ``on_admitted`` hook, under the session
lock, in the same save that admits the wake turn: a turn that loses the race to
a human send publishes nothing. Only the worker of THAT stream settles the
lease: committed once the Gateway accepts the run (runs API: run id admitted;
legacy transport: response opened), released if the turn ends before that. The
sidecar keeps the lease until the database settle succeeds; a failed commit is
retried by the next poll. A restart before settlement reattaches the run and
settles it, or the lease expires and the next poll retries. Refused wakes, and
wakes the Gateway never accepted, back off per session.
Without reserve+commit the poller stays inert.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 5.0
BACKOFF_MAX_S = 300.0
RESERVATION_OWNER = "hermes-webui"
RESERVATION_TTL_S = 120.0
WAKE_PROMPT = ("[IMPORTANT: A delegated subagent finished. Its result is already in your "
               "conversation history above; review it and continue the task.]")
_REQUIRED_API = ("reserve_caller_history_deliveries", "commit_caller_history_deliveries",
                 "release_caller_history_deliveries")
# Rows older than this at first sight are left for the next user turn to fold.
MAX_ROW_AGE_S = 6 * 3600

_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.Lock()
_BACKOFF: dict[tuple[str, str], tuple[int, float]] = {}  # (db, state sid) -> (failures, retry at)
_FLOOR: dict[str, int] = {}  # db -> lowest message id the next poll scans (rowid high-water mark)

_PENDING_SQL = (
    "SELECT session_id, MIN(id) FROM messages WHERE id >= ? AND role = 'user'"
    " AND display_kind IN ('async_delegation_complete', 'hidden')"
    " AND coalesce(json_extract(display_metadata, '$.delegation_id'), '') != ''"
    " AND json_extract(display_metadata, '$.caller_history_consumed') IS NULL"
    " AND timestamp >= ? GROUP BY session_id"
)


def _profile_state_dbs() -> list[tuple[str, Path]]:
    from api.profiles import _DEFAULT_HERMES_HOME

    base = Path(_DEFAULT_HERMES_HOME)
    out = [("default", base / "state.db")]
    profiles_dir = base / "profiles"
    if profiles_dir.is_dir():
        out += [(p.name, p / "state.db") for p in sorted(profiles_dir.iterdir()) if p.is_dir()]
    return [(name, path) for name, path in out if path.is_file()]


def _pending_session_ids(db_path: Path, since: float) -> list[str]:
    """Sessions with pending deliveries. Scans only ids >= the lowest id still pending
    last time (a rowid range), so the steady-state poll is O(new rows), not O(table)."""
    import sqlite3

    key = str(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        top = conn.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0]
        rows = conn.execute(_PENDING_SQL, (_FLOOR.get(key, 0), since)).fetchall()
    finally:
        conn.close()
    # Rows stay pending until committed, so the lowest pending id is a safe floor.
    _FLOOR[key] = min((r[1] for r in rows), default=top + 1)
    return [r[0] for r in rows]


def _session_db_cls():
    from hermes_state import SessionDB

    return SessionDB


def _claim_api_available() -> bool:
    try:
        cls = _session_db_cls()
    except Exception:
        return False
    return all(callable(getattr(cls, name, None)) for name in _REQUIRED_API)


def _sidecar_profile_matches(sidecar: Path, profile: str) -> bool:
    import json
    from api.profiles import _profiles_match

    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and _profiles_match(data.get("profile"), profile)


def _webui_session_id(db_path: Path, session_id: str, profile: str) -> str | None:
    """The WebUI sidecar of *profile* that owns *session_id*, following compression lineage upward."""
    import sqlite3
    from api.config import SESSION_DIR

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        sid, seen = session_id, set()
        while sid and sid not in seen:
            sidecar = Path(SESSION_DIR) / f"{sid}.json"
            if sidecar.is_file():
                return sid if _sidecar_profile_matches(sidecar, profile) else None
            seen.add(sid)
            row = conn.execute(
                "SELECT p.id FROM sessions s JOIN sessions p ON p.id = s.parent_session_id"
                " WHERE s.id = ? AND p.end_reason = 'compression'", (sid,)).fetchone()
            sid = row[0] if row else None
        return None
    finally:
        conn.close()


def _start_wakeup(webui_sid: str, on_admitted) -> bool:
    """True once the wake turn is persisted as the session's pending turn."""
    from api.routes import start_session_turn

    try:
        resp = start_session_turn(webui_sid, WAKE_PROMPT, source="process_wakeup",
                                  on_admitted=on_admitted) or {}
    except Exception:
        logger.warning("gateway delegation wakeup raised for %s", webui_sid, exc_info=True)
        return False
    status = int(resp.get("_status", 200) or 200)
    if status >= 400 and status != 409:
        logger.warning("gateway delegation wakeup failed for %s: %s %r", webui_sid, status, resp.get("error"))
    return status < 400 and bool(resp.get("stream_id"))


def _publish_results(s, rows: list[dict], reservation: dict) -> None:
    """Append each reserved result once to *s*'s model-facing context and record the lease.

    Runs as ``on_admitted`` under the session lock; the caller's chat-start save persists it
    together with the admitted wake turn (and its launch-failure rollback undoes it)."""
    context = list(getattr(s, "context_messages", None) or [])
    if not context:
        context = [m for m in (getattr(s, "messages", None) or []) if isinstance(m, dict)
                   and m.get("role") in ("user", "assistant") and not m.get("_error")]
    have = {m.get("_delegation_delivery_id") for m in context if isinstance(m, dict)}
    added = [{"role": "user", "content": r["content"], "_source": "process_wakeup",
              "_delegation_delivery_id": r["id"], "timestamp": time.time()}
             for r in rows if r["id"] not in have]
    s.context_messages = context + added
    s.delegation_reservation = reservation


def _record_outcome(res: dict, accepted: bool) -> None:
    """Back-off bookkeeping for a settled wake: accepted resets it, unaccepted bumps it."""
    key = (str(res.get("db")), str(res.get("sid")))
    if accepted:
        _BACKOFF.pop(key, None)
        return
    fails = _BACKOFF.get(key, (0, 0.0))[0] + 1
    _BACKOFF[key] = (fails, time.time() + min(BACKOFF_MAX_S, POLL_INTERVAL_S * 2 ** fails))


def _held_reservation(webui_sid: str, stream_id) -> dict | None:
    from api.config import _get_session_agent_lock
    from api.models import get_session

    with _get_session_agent_lock(webui_sid):
        res = getattr(get_session(webui_sid), "delegation_reservation", None)
    if not isinstance(res, dict) or (stream_id is not None and res.get("stream_id") != stream_id):
        return None
    return res


def _clear_reservation(webui_sid: str, token: str, **mark) -> None:
    """Drop (or, with *mark*, annotate) the sidecar lease if it is still *token*'s."""
    from api.config import _get_session_agent_lock
    from api.models import get_session

    with _get_session_agent_lock(webui_sid):
        s = get_session(webui_sid)
        res = getattr(s, "delegation_reservation", None)
        if isinstance(res, dict) and res.get("token") == token:
            s.delegation_reservation = {**res, **mark} if mark else None
            s.save(touch_updated_at=False)


def settle_reservation(webui_sid: str, stream_id, accepted: bool) -> None:
    """Commit (Gateway accepted the wake run) or release the lease of *stream_id*'s wake turn.

    Only the stream the lease was published with may settle it, so a concurrent human turn
    never consumes it. The sidecar keeps the lease until the database write succeeds; an
    accepted lease whose commit failed is marked and re-committed by the next poll. Never raises."""
    res = None
    try:
        res = _held_reservation(webui_sid, stream_id)
        if res is None:
            return
        db = _session_db_cls()(Path(res["db"]))
        try:
            if accepted:
                db.commit_caller_history_deliveries(res["token"], res["owner"])
            else:
                db.release_caller_history_deliveries(reservation_token=res["token"], owner=res["owner"])
        finally:
            db.close()
    except Exception:
        logger.warning("gateway delegation reservation settle failed for %s", webui_sid, exc_info=True)
        if accepted and res is not None:
            try:
                _clear_reservation(webui_sid, res["token"], accepted=True)
            except Exception:
                logger.warning("gateway delegation reservation mark failed for %s", webui_sid, exc_info=True)
        return
    _record_outcome(res, accepted)
    try:
        _clear_reservation(webui_sid, res["token"])
    except Exception:
        logger.warning("gateway delegation reservation clear failed for %s", webui_sid, exc_info=True)


def _retry_accepted_commit(webui_sid: str) -> bool:
    """Re-commit a lease whose wake run the Gateway accepted but whose commit failed.
    True while it is still unsettled (the session must not be re-reserved)."""
    res = _held_reservation(webui_sid, None)
    if res is None or not res.get("accepted"):
        return False
    settle_reservation(webui_sid, res.get("stream_id"), accepted=True)
    return _held_reservation(webui_sid, None) is not None


def _profile_uses_gateway(profile: str) -> bool:
    from api.config import get_config_for_profile_home
    from api.gateway_chat import webui_gateway_chat_enabled
    from api.profiles import get_hermes_home_for_profile

    return webui_gateway_chat_enabled(get_config_for_profile_home(get_hermes_home_for_profile(profile)))


def _claim_and_wake(db_path: Path, state_sid: str, webui_sid: str) -> int:
    """Reserve the session's pending rows, store them once, and wake with a fixed prompt.

    The Gateway worker commits the lease once the run is accepted (``settle_reservation``).
    """
    from api.background_process import _session_has_active_turn

    key = (str(db_path), state_sid)
    if _BACKOFF.get(key, (0, 0.0))[1] > time.time() or _session_has_active_turn(webui_sid):
        return 0  # rows stay pending: the running turn's successor folds them, or a later poll
    if _retry_accepted_commit(webui_sid):
        return 0
    db = _session_db_cls()(db_path)
    try:
        reserved = db.reserve_caller_history_deliveries(state_sid, RESERVATION_OWNER, RESERVATION_TTL_S)
        rows = [r for r in reserved if isinstance(r.get("content"), str) and r["content"].strip()]
        blank = [r["id"] for r in reserved if r not in rows]
        if blank:  # nothing to deliver: settle them as the Gateway fold would
            db.commit_caller_history_deliveries(blank, RESERVATION_OWNER)
        if not rows:
            return 0
        lease = {"db": str(db_path), "sid": state_sid, "token": rows[0]["reservation_token"],
                 "owner": RESERVATION_OWNER}

        def on_admitted(s, stream_id):
            _publish_results(s, rows, {**lease, "stream_id": stream_id})

        if _start_wakeup(webui_sid, on_admitted):
            return 1  # the wake turn's worker settles the lease and the back-off
        # Not admitted: nothing was published (or the launch rollback removed it).
        db.release_caller_history_deliveries(reservation_token=lease["token"], owner=RESERVATION_OWNER)
        _record_outcome(lease, accepted=False)
        return 0
    finally:
        db.close()


def poll_once(since: float) -> int:
    woken = 0
    for profile, db_path in _profile_state_dbs():
        try:
            if not _profile_uses_gateway(profile):
                continue  # in-process profiles never get Gateway deliveries: don't scan their state.db
            session_ids = _pending_session_ids(db_path, since)
        except Exception:
            logger.debug("gateway delegation poll failed for profile %s", profile, exc_info=True)
            continue
        for sid in session_ids:
            try:
                webui_sid = _webui_session_id(db_path, sid, profile)
                if webui_sid:
                    woken += _claim_and_wake(db_path, sid, webui_sid)
            except Exception:
                logger.warning("gateway delegation wakeup raised for %s", sid, exc_info=True)
    return woken


def _loop() -> None:
    since = time.time() - MAX_ROW_AGE_S
    while not _STOP.wait(POLL_INTERVAL_S):
        poll_once(since)


def start_gateway_delegation_poller() -> bool:
    """Start the poller (any profile may chat through the Gateway; checked per row). Never raises."""
    global _THREAD
    try:
        if not _claim_api_available():
            logger.info("gateway delegation poller inactive: installed Hermes Agent lacks the delivery reserve/commit API")
            return False
        with _LOCK:
            if _THREAD is not None and _THREAD.is_alive():
                return False
            _STOP.clear()
            _THREAD = threading.Thread(target=_loop, name="hermes-webui-gateway-deleg-wakeup", daemon=True)
            _THREAD.start()
        return True
    except Exception:
        logger.warning("gateway delegation poller failed to start", exc_info=True)
        return False


def stop_gateway_delegation_poller(timeout: float = 2.0) -> None:
    _STOP.set()
    th = _THREAD
    if th is not None and th.is_alive():
        th.join(timeout=timeout)
