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


def durable_compression_continuation(session):
    """Return (sealed, resumable tip), without making a recovery write.

    ``sealed`` is tri-state: ``False`` means SQLite verified a live origin,
    ``True`` means it verified a compression-sealed origin, and ``None`` means
    durable authority was unavailable. A known sealed parent without a safe tip
    stays sealed (no sidecar fallback). Older Agent installations without this
    read API retain legacy behavior in callers that support sidecar recovery.
    """
    from api.profiles import _PROFILE_ID_RE, _resolve_profile_home_for_name

    sid = str(getattr(session, "session_id", "") or "")
    profile = str(getattr(session, "profile", None) or "default")
    if not sid or (profile != "default" and not _PROFILE_ID_RE.fullmatch(profile)):
        return None, None
    db = None
    sealed = None
    try:
        from hermes_state import SessionDB

        path = Path(_resolve_profile_home_for_name(profile)) / "state.db"
        if not path.is_file():
            return None, None
        db = SessionDB(path, read_only=True)
        # Establish the complete read API before accepting SQLite authority.
        # Old Agents must retain legacy sidecar recovery, not a sealed null tip.
        for name in ('get_session', 'get_compression_tip'):
            method = getattr(db, name, None)
            if not callable(method):
                return None, None
            try:
                inspect.signature(method).bind(sid)
            except (TypeError, ValueError):
                return None, None
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
        # Only the observed automatic idle closure is resumable here. Explicit
        # resets/closures and unknown future reasons must not become redirects.
        reason = child.get("end_reason")
        if reason not in (None, "", "idle_timeout"):
            return True, None
        if child.get("ended_at") is not None and reason != "idle_timeout":
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
