"""
Hermes Web UI -- Gateway session watcher.

Background daemon thread that polls state.db every 5 seconds for changes
to gateway sessions (telegram, discord, slack, etc.). When changes are
detected, it pushes notifications to all subscribed SSE clients.

This enables real-time session list updates in the sidebar without
requiring any changes to hermes-agent.
"""
import hashlib
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

from api.config import HOME
from api.agent_sessions import read_importable_agent_session_rows

logger = logging.getLogger(__name__)


# ── State hash tracking ─────────────────────────────────────────────────────

def _snapshot_hash(sessions: list) -> str:
    """Create a lightweight hash of session IDs and timestamps for change detection."""
    key = '|'.join(
        f"{s['session_id']}:{s.get('updated_at', 0)}:{s.get('message_count', 0)}"
        for s in sorted(sessions, key=lambda x: x['session_id'])
    )
    return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()


# ── DB resolution (shared pattern with state_sync.py) ──────────────────────

def _get_state_db_path() -> Path:
    """Resolve state.db path for the active profile."""
    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()
    return hermes_home / 'state.db'


def _get_agent_sessions_from_db() -> list:
    """Read all non-webui sessions from state.db.
    Returns list of session dicts, or empty list on any error.
    """
    db_path = _get_state_db_path()
    if not db_path.exists():
        return []

    try:
        sessions = []
        for row in read_importable_agent_session_rows(db_path, limit=200, log=logger):
            sessions.append({
                'session_id': row['id'],
                'title': row['title'] or 'Agent Session',
                'model': row['model'] or None,
                'message_count': row['message_count'] or row['actual_message_count'] or 0,
                'created_at': row['started_at'],
                'updated_at': row['last_activity'] or row['started_at'],
                'source': row['source'] or 'cli',
                'raw_source': row.get('raw_source'),
                'session_source': row.get('session_source'),
                'source_label': row.get('source_label'),
            })
        return sessions
    except Exception:
        return []


# ── GatewayWatcher ──────────────────────────────────────────────────────────

class GatewayWatcher:
    """Background thread that polls state.db for agent session changes.

    Usage:
        watcher = GatewayWatcher()
        watcher.start()
        q = watcher.subscribe()
        # ... receive change events via q.get() ...
        watcher.unsubscribe(q)
        watcher.stop()
    """

    POLL_INTERVAL = 5  # seconds between polls
    SUBSCRIBER_TIMEOUT = 30  # seconds before sending keepalive comment

    def __init__(self):
        self._subscribers: list[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_hash: str = ''
        self._last_sessions: list = []
        # Track state.db mtime across polls to detect gateway restarts.
        # When gateway restarts, state.db is re-opened and its mtime may
        # advance even if the SQL content is identical (WAL flush, journal
        # file recreation, etc.).  A sudden mtime jump without a matching
        # session-hash change means the gateway recycled its DB connection;
        # the existing sessions are preserved, so we suppress the notification
        # to avoid pushing phantom session-list refreshes to the frontend.
        self._last_db_mtime: float | None = None
        self._db_mtime_reset_threshold: float = 3.0  # ignore mtime shifts <3s

    def start(self):
        """Start the watcher daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name='gateway-watcher')
        self._thread.start()

    def is_alive(self) -> bool:
        """Return True when the poll thread is running.

        Public accessor used by ``/api/sessions/gateway/stream`` probe mode and
        the live SSE handler to detect a watcher instance whose poll thread
        died silently (e.g. uncaught exception in ``_poll_loop``).  Callers
        use this to decide whether to return 503 and trigger the client-side
        polling fallback, instead of handing out an SSE connection that would
        never emit events.
        """
        t = self._thread
        return t is not None and t.is_alive()

    def stop(self):
        """Stop the watcher thread."""
        self._stop_event.set()
        # Wake up any subscribers
        with self._sub_lock:
            for q in self._subscribers:
                try:
                    q.put(None)  # sentinel
                except Exception:
                    logger.debug("Failed to send sentinel to subscriber")
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def subscribe(self) -> queue.Queue:
        """Subscribe to change events. Returns a queue.Queue.
        Events are dicts: {'type': 'sessions_changed', 'sessions': [...]}
        A None sentinel means the watcher is stopping.
        """
        q = queue.Queue(maxsize=10)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        """Remove a subscriber queue."""
        with self._sub_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def _notify_subscribers(self, sessions: list):
        """Push change event to all subscribers."""
        event = {
            'type': 'sessions_changed',
            'sessions': sessions,
        }
        with self._sub_lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)  # remove slow consumers
                except Exception:
                    dead.append(q)
            for q in dead:
                try:
                    self._subscribers.remove(q)
                except ValueError:
                    pass
                # Send a None sentinel so the SSE handler unblocks, closes,
                # and lets the browser's EventSource auto-reconnect.
                try:
                    q.put_nowait(None)
                except Exception:
                    logger.debug("Failed to send sentinel to dead subscriber")

    def _poll_loop(self):
        """Main polling loop. Runs in a daemon thread."""
        while not self._stop_event.is_set():
            try:
                sessions = _get_agent_sessions_from_db()
                current_hash = _snapshot_hash(sessions)
                current_mtime = self._get_db_mtime()

                # Snapshot hash covers session_id + updated_at + message_count
                # from state.db (see _snapshot_hash docstring).  This is the
                # sole notification signal:
                #
                #   same hash = nothing changed → no notification
                #   diff hash = data changed → notify subscribers
                #
                # The mtime-based _detect_gateway_restart() utility answers
                # the separate semantic question "was state.db re-opened?"
                # It is intentionally decoupled from the notify decision
                # because: (a) when hash is identical there is nothing to
                # notify about regardless of why mtime changed, and (b) a
                # hash-diff from real data changes must never be suppressed
                # even if the mtime happens to jump at the same moment.
                # Future maintainers: if you add a field to the sessions
                # payload that should trigger sidebar updates, add it to
                # _snapshot_hash as well.
                is_gateway_restart = self._detect_gateway_restart(
                    current_mtime, current_hash,
                )

                hash_changed = current_hash != self._last_hash
                self._last_hash = current_hash
                self._last_sessions = sessions

                if hash_changed:
                    if is_gateway_restart:
                        logger.debug(
                            "Gateway restart detected with no content change "
                            "(mtime jumped %.1fs, hash identical) — notifying anyway "
                            "for sidebar consistency",
                            current_mtime - (self._last_db_mtime or current_mtime),
                        )
                    self._notify_subscribers(sessions)

                self._last_db_mtime = current_mtime
            except Exception:
                logger.debug("Error in gateway watcher poll loop", exc_info=True)

            # Sleep in small increments so we can stop promptly
            for _ in range(self.POLL_INTERVAL * 10):
                if self._stop_event.is_set():
                    return
                time.sleep(0.1)

    def _get_db_mtime(self) -> float | None:
        """Return state.db mtime (seconds since epoch) or None if unavailable."""
        try:
            path = _get_state_db_path()
            return path.stat().st_mtime if path.exists() else None
        except (OSError, AttributeError):
            return None

    def _detect_gateway_restart(
        self, current_mtime: float | None, current_hash: str
    ) -> bool:
        """Return True when the current poll looks like a gateway restart.

        Heuristic: state.db mtime jumped by more than the threshold,
        AND the session content hash is the same as the previous poll.
        A true gateway restart re-opens the DB (updating mtime) without
        changing session data — unless messages arrived during downtime.

        When the hash differs, it's a real content change — NOT a restart
        — and the notification must be allowed through regardless of the
        mtime delta.  (This prevents suppressing legitimate notifications
        when new messages arrive during a gateway restart window.)
        """
        if current_mtime is None or self._last_db_mtime is None:
            return False
        mtime_delta = current_mtime - self._last_db_mtime
        return mtime_delta > self._db_mtime_reset_threshold and current_hash == self._last_hash


# ── Module-level singleton ─────────────────────────────────────────────────

_watcher: GatewayWatcher | None = None
_watcher_lock = threading.Lock()


def start_watcher():
    """Start the global gateway watcher (idempotent)."""
    global _watcher
    with _watcher_lock:
        if _watcher is None:
            _watcher = GatewayWatcher()
            _watcher.start()


def stop_watcher():
    """Stop the global gateway watcher."""
    global _watcher
    with _watcher_lock:
        if _watcher is not None:
            _watcher.stop()
            _watcher = None


def get_watcher() -> GatewayWatcher | None:
    """Get the global watcher instance (or None if not started)."""
    with _watcher_lock:
        return _watcher
