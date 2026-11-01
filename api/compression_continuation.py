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


# Accidental closes that Hermes keeps resumable (fallback for older Agents
# without hermes_state_common.is_automatic_end_reason).
_RESUMABLE_END_REASONS = frozenset({
    "idle_timeout", "startup_orphan_reap", "ws_orphan_reap",
})
# Explicit conversation boundaries never become redirects, whatever the Agent says.
_BOUNDARY_END_REASONS = frozenset({
    "reset", "user_closed", "compression", "new_session",
})


def _is_resumable_end_reason(reason):
    """True when a tip closed this way may still receive the next turn."""
    if reason in (None, ""):
        return True
    if not isinstance(reason, str) or reason in _BOUNDARY_END_REASONS:
        return False
    if reason in _RESUMABLE_END_REASONS:
        return True
    try:
        from hermes_state_common import is_automatic_end_reason
        return is_automatic_end_reason(reason) is True
    except Exception:
        return False


def durable_compression_continuation(session):
    """Return (sealed, resumable tip), without making a recovery write.

    A known sealed parent without a safe tip stays sealed (no sidecar fallback).
    Older Agent installations without this read API retain legacy behavior.
    """
    from api.profiles import _PROFILE_ID_RE, _resolve_profile_home_for_name

    sid = str(getattr(session, "session_id", "") or "")
    profile = str(getattr(session, "profile", None) or "default")
    if not sid or (profile != "default" and not _PROFILE_ID_RE.fullmatch(profile)):
        return False, None
    db = None
    sealed = False
    try:
        from hermes_state import SessionDB

        path = Path(_resolve_profile_home_for_name(profile)) / "state.db"
        if not path.is_file():
            return False, None
        db = SessionDB(path, read_only=True)
        # Establish the complete read API before accepting SQLite authority.
        # Old Agents must retain legacy sidecar recovery, not a sealed null tip.
        for name in ('get_session', 'get_compression_tip'):
            method = getattr(db, name, None)
            if not callable(method):
                return False, None
            try:
                inspect.signature(method).bind(sid)
            except (TypeError, ValueError):
                return False, None
        parent = db.get_session(sid)
        if not parent or parent.get("end_reason") != "compression":
            return False, None
        sealed = True
        tip = db.get_compression_tip(sid)
        if not tip or tip == sid:
            return True, None
        child = db.get_session(tip)
        if not child or not _is_local_interactive_persisted_source(child.get("source")):
            return True, None
        for row in (parent, child):
            if row.get("profile_name") not in (None, "", profile):
                return True, None
        # Only accidental automatic closures (idle/orphan sweeps) are resumable
        # here. Explicit resets/closures and unknown reasons must not redirect.
        reason = child.get("end_reason")
        if not _is_resumable_end_reason(reason):
            return True, None
        if child.get("ended_at") is not None and reason in (None, ""):
            return True, None
        if db.get_compression_tip(sid) != tip:
            return True, None
        return True, tip
    except (ImportError, AttributeError, TypeError):
        return sealed, None
    except Exception:
        logger.debug("Could not resolve durable compression continuation", exc_info=True)
        return sealed, None
    finally:
        if db is not None:
            db.close()
