"""Wake WebUI sessions for async-delegation completions delivered by the Gateway.

With the Gateway chat backend the agent runs in the Gateway process, so its
``async_delegation`` completions never reach this process's
``process_registry.completion_queue`` (``api.background_process`` drain).
For api_server sessions the Gateway instead persists each completion as a
delivery row in the profile's state.db (``display_kind`` ``async_delegation_complete``,
or ``hidden`` for presentation-suppressed notices) and deliberately starts no turn: the client owns the next turn
(``gateway.wake.persist_delegation_delivery``). Without a consumer here the
parent agent only sees the result when the user next types.

This poller claims those rows with the Agent's own exactly-once primitive
(``SessionDB.claim_caller_history_deliveries``, the same claim the Gateway's
next run uses to fold them) and starts a wakeup turn for idle sessions. A turn
that is not accepted hands its rows back (``release_caller_history_deliveries``),
so a failed or refused start never strands a completion: the next poll, or the
Gateway's next-run fold, claims it again. Not covered: a WebUI process exit
between the claim commit and the release/accepted start leaves those rows
claimed; closing that window needs an expiring reservation in the Agent API.
Without both methods it stays inert.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 5.0
# Rows older than this at first sight are left for the next user turn to fold.
MAX_ROW_AGE_S = 6 * 3600

_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.Lock()

_PENDING_SQL = (
    "SELECT DISTINCT session_id FROM messages WHERE role = 'user'"
    " AND display_kind IN ('async_delegation_complete', 'hidden')"
    " AND coalesce(json_extract(display_metadata, '$.delegation_id'), '') != ''"
    " AND json_extract(display_metadata, '$.caller_history_consumed') IS NULL"
    " AND timestamp >= ?"
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
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        return [row[0] for row in conn.execute(_PENDING_SQL, (since,))]
    finally:
        conn.close()


def _claim_api_available() -> bool:
    from hermes_state import SessionDB

    return all(callable(getattr(SessionDB, name, None))
               for name in ("claim_caller_history_deliveries", "release_caller_history_deliveries"))


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


def _start_wakeup(webui_sid: str, prompt: str) -> bool:
    """True once the turn is accepted; any other outcome leaves the rows to be released."""
    from api.routes import start_session_turn

    try:
        resp = start_session_turn(webui_sid, prompt, source="process_wakeup") or {}
    except Exception:
        logger.warning("gateway delegation wakeup raised for %s", webui_sid, exc_info=True)
        return False
    status = int(resp.get("_status", 200) or 200)
    if status >= 400 and status != 409:
        logger.warning("gateway delegation wakeup failed for %s: %s %r", webui_sid, status, resp.get("error"))
    return status < 400


def _claim_and_wake(db_path: Path, state_sid: str, webui_sid: str) -> int:
    """Claim the session's unconsumed delivery rows and start one wakeup turn, or hand them back."""
    from api.background_process import _session_has_active_turn
    from hermes_state import SessionDB

    if _session_has_active_turn(webui_sid):
        return 0  # rows stay unclaimed: the running turn's successor folds them, or the next poll
    db = SessionDB(db_path)
    try:
        rows = db.claim_caller_history_deliveries(state_sid)
        texts = [r["content"] for r in rows if isinstance(r.get("content"), str) and r["content"].strip()]
        if texts and _start_wakeup(webui_sid, "\n\n".join(texts)):
            return 1
        if texts:
            db.release_caller_history_deliveries(state_sid, [r["id"] for r in rows])
        return 0
    finally:
        db.close()


def poll_once(since: float) -> int:
    woken = 0
    for profile, db_path in _profile_state_dbs():
        try:
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
    """Start the poller when chat runs on the Gateway backend. Never raises."""
    global _THREAD
    try:
        from api.config import get_config
        from api.gateway_chat import webui_gateway_chat_enabled

        if not webui_gateway_chat_enabled(get_config()):
            return False
        if not _claim_api_available():
            logger.info("gateway delegation poller inactive: installed Hermes Agent lacks the delivery claim API")
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
