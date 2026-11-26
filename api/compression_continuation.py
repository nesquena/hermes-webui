"""Read-only routing hints for externally compressed WebUI sessions.

SQLite owns compression lineage; sidecar snapshot flags may predate a
Desktop/CLI rotation. Never reopen a sealed parent or mutate Agent state here.
"""
import inspect
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_local_interactive_persisted_source(source):
    return isinstance(source, str) and source in {
        "webui", "tui", "cli", "desktop", "acp",
    }


class _StoreCompressionReads:
    """``SessionDB``'s compression reads over the storage interface's read-only handle."""

    def __init__(self, opened):
        self._opened = opened
        self._reader = opened.__enter__()

    def get_session(self, session_id):
        record = self._reader.sessions.get(session_id)
        return None if record is None else record.as_session_row()

    def get_compression_tip(self, session_id):
        return str(self._reader.lineage.tip(session_id))

    def get_compression_lineage(self, session_id):
        return [str(sid) for sid in self._reader.lineage.lineage(session_id)]

    def close(self):
        self._opened.__exit__(None, None, None)


def open_compression_reads(path):
    """A read-only handle on one profile's ``state.db`` with ``get_session``,
    ``get_compression_tip`` and ``get_compression_lineage``; the caller closes it.

    Agents whose ``hermes_state.SessionDB`` carries those reads keep it. On newer
    Agents ``hermes_state.SessionDB`` is a deprecated stand-in without them, and
    the reads go through ``persistence_assembly.open_persistence_reader`` (never
    creates or migrates the store). Any other ``SessionDB`` is returned as is,
    so a missing read keeps the callers' legacy fallbacks.
    """
    try:
        from hermes_state import SessionDB
    except ImportError:
        SessionDB = None
    stand_in = SessionDB is None or (
        getattr(SessionDB, "__module__", None) == "hermes_state"
        and not callable(getattr(SessionDB, "get_compression_tip", None))
    )
    if stand_in:
        try:
            from persistence_assembly import open_persistence_reader
        except ImportError:
            open_persistence_reader = None
        if open_persistence_reader is not None:
            return _StoreCompressionReads(open_persistence_reader(Path(path)))
    if SessionDB is None:
        raise ImportError("hermes_state.SessionDB is unavailable")
    return SessionDB(path, read_only=True)


def compression_seal_state(session):
    """Return (sealed, resumable_tip, state), without making a recovery write.

    ``state`` separates a confirmed dead lineage from one that merely could
    not be confirmed on this pass — callers that DESTROY queued state on a
    null tip (wakeup admission drops a 404 and clears the whole deferred
    queue) must not treat a transient read outcome as deletion:

      "unsealed"          -- not a durable compression parent; (False, None).
      "open"              -- sealed with a resumable tip; (True, tip).
      "closed_confirmed"  -- sealed; the tip row was read cleanly and is
                             explicitly closed (session_reset etc.), deleted,
                             or structurally invalid. Terminal.
      "unconfirmed"       -- sealed, but the tip could not be confirmed this
                             pass: no tip row yet (rotation may still be
                             mid-write), the tip churned between reads, or a
                             read failed after the seal was found. Retryable.

    A known sealed parent without a safe tip stays sealed (no sidecar fallback).
    Older Agent installations without this read API retain legacy behavior.
    """
    from api.profiles import _PROFILE_ID_RE, _resolve_profile_home_for_name

    sid = str(getattr(session, "session_id", "") or "")
    profile = str(getattr(session, "profile", None) or "default")
    if not sid or (profile != "default" and not _PROFILE_ID_RE.fullmatch(profile)):
        return False, None, "unsealed"
    db = None
    sealed = False
    try:
        path = Path(_resolve_profile_home_for_name(profile)) / "state.db"
        if not path.is_file():
            return False, None, "unsealed"
        db = open_compression_reads(path)
        # Establish the complete read API before accepting SQLite authority.
        # Old Agents must retain legacy sidecar recovery, not a sealed null tip.
        for name in ('get_session', 'get_compression_tip'):
            method = getattr(db, name, None)
            if not callable(method):
                return False, None, "unsealed"
            try:
                inspect.signature(method).bind(sid)
            except (TypeError, ValueError):
                return False, None, "unsealed"
        parent = db.get_session(sid)
        if not parent or parent.get("end_reason") != "compression":
            return False, None, "unsealed"
        sealed = True
        tip = db.get_compression_tip(sid)
        if not tip or tip == sid:
            # No tip row yet. The rotation write may still be landing; a
            # missing tip is not a confirmed deletion.
            return True, None, "unconfirmed"
        child = db.get_session(tip)
        if not child:
            # The durable tip names a row that is gone: confirmed deletion.
            return True, None, "closed_confirmed"
        if not _is_local_interactive_persisted_source(child.get("source")):
            return True, None, "closed_confirmed"
        for row in (parent, child):
            if row.get("profile_name") not in (None, "", profile):
                return True, None, "closed_confirmed"
        # Only the observed automatic idle closure is resumable here. Explicit
        # resets/closures and unknown future reasons must not become redirects.
        reason = child.get("end_reason")
        if reason not in (None, "", "idle_timeout"):
            return True, None, "closed_confirmed"
        if child.get("ended_at") is not None and reason != "idle_timeout":
            return True, None, "closed_confirmed"
        if db.get_compression_tip(sid) != tip:
            # The tip changed under us mid-read: a newer rotation is in
            # flight. Retryable, not terminal.
            return True, None, "unconfirmed"
        return True, tip, "open"
    except (ImportError, AttributeError, TypeError):
        return sealed, None, ("unconfirmed" if sealed else "unsealed")
    except Exception:
        logger.debug("Could not resolve durable compression continuation", exc_info=True)
        return sealed, None, ("unconfirmed" if sealed else "unsealed")
    finally:
        if db is not None:
            db.close()


def durable_compression_continuation(session):
    """Return (sealed, resumable tip), without making a recovery write.

    Thin wrapper over :func:`compression_seal_state` for callers that do not
    distinguish a confirmed-closed tip from an unconfirmable one. Callers
    that discard or clear queued wakeup state on a null tip must use the
    three-value form and refuse retryably on "unconfirmed".
    """
    sealed, tip, _state = compression_seal_state(session)
    return sealed, tip
