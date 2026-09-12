"""
Hermes Web UI -- Optional state.db sync bridge.

Mirrors WebUI session metadata (token usage, title, model) into the
hermes-agent state.db so that /insights, session lists, and cost
tracking include WebUI activity.

This is opt-in via the 'sync_to_insights' setting (default: off).
All operations are wrapped in try/except -- if state.db is unavailable,
locked, or the schema doesn't match, the WebUI continues normally.

The bridge uses absolute token counts (not deltas) because the WebUI
Session object already accumulates totals across turns. This avoids
any double-counting risk.
"""
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TitleChangedError(RuntimeError):
    """The title snapshot no longer owns an explicit regeneration."""


def _get_state_db(profile: Optional[str] = None, *, strict: bool = False):
    """Get a SessionDB instance for a profile's state.db.

    When ``profile`` is provided the function resolves *that* profile's
    home directory directly (via ``_resolve_profile_home_for_name``).
    If resolution fails (unknown profile name, IO error, etc.) the
    function returns ``None`` rather than silently falling back to
    ``HERMES_HOME`` — silently routing the write to the wrong DB
    would defeat the point of the explicit-profile path (#2762).

    When ``profile`` is None it falls back to the TLS-based
    ``get_active_hermes_home()`` lookup for backward compatibility,
    with a final ``HERMES_HOME`` fallback only on that path. TLS may be
    unset in background/worker threads, in which case the lookup falls
    through to the process-global active profile and can write to the
    wrong DB. Callers that know the session's profile (e.g.
    ``sync_session_usage`` after a stream completes on a background
    thread) should pass it explicitly to avoid that race.

    Returns None if hermes_state is not importable, the explicit
    profile cannot be resolved, or the DB is unavailable. Each caller
    is responsible for calling db.close() when done.
    """
    try:
        from hermes_state import SessionDB
    except ImportError:
        return None

    if profile is not None:
        # Explicit-profile path — a resolution failure here MUST NOT
        # silently fall back to HERMES_HOME or the caller's "write to
        # the named profile" contract is broken (the original #2762
        # symptom: writes leaking into the wrong profile's state.db).
        #
        # Defense-in-depth (per #2827 maintainer review): validate the
        # name shape BEFORE handing it to ``_resolve_profile_home_for_name``.
        # The resolver itself rarely raises — for an invalid-but-non-
        # malicious name (e.g. one that fails ``_PROFILE_ID_RE``) it
        # quietly returns ``_DEFAULT_HERMES_HOME``, which is the exact
        # leak we're trying to prevent on the explicit-profile path.
        # Validating up-front turns that quiet leak into an explicit
        # "refuse + log + return None" so the contract is "write to
        # the EXACT named profile, or write nowhere."
        try:
            from api.profiles import (
                _resolve_profile_home_for_name,
                _PROFILE_ID_RE,
                _is_root_profile,
            )
            if not (_is_root_profile(profile) or _PROFILE_ID_RE.fullmatch(profile)):
                logger.warning(
                    "state_sync: refusing invalid profile name %r — skipping "
                    "write rather than leaking to the default state.db (#2762).",
                    profile,
                )
                if strict:
                    raise ValueError('Invalid title profile')
                return None
            hermes_home = Path(_resolve_profile_home_for_name(profile)).expanduser().resolve()
        except Exception:
            logger.warning(
                "state_sync: could not resolve profile %r — skipping write rather "
                "than leaking to the active profile (#2762).", profile,
            )
            if strict:
                raise
            return None
    else:
        # Implicit / TLS-fallback path — preserves pre-#2762 behavior
        # for any caller that doesn't pass profile= explicitly.
        try:
            from api.profiles import get_active_hermes_home
            hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
        except Exception:
            logger.debug("Failed to resolve hermes home, using default")
            hermes_home = Path(os.getenv('HERMES_HOME', str(Path.home() / '.hermes')))

    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return None

    try:
        return SessionDB(db_path)
    except Exception:
        logger.debug("Failed to open state.db")
        if strict:
            raise
        return None


def sync_session_start(session_id: str, model=None, profile: Optional[str] = None) -> None:
    """Register a WebUI session in state.db (idempotent).
    Called when a session's first message is sent.

    ``profile`` lets the caller name the target state.db explicitly,
    avoiding the TLS-vs-background-thread mismatch in #2762. When
    omitted, the active profile is resolved from TLS (then process
    globals) as before.
    """
    db = _get_state_db(profile=profile)
    if not db:
        return
    try:
        db.ensure_session(
            session_id=session_id,
            source='webui',
            model=model,
        )
    except Exception:
        logger.debug("Failed to sync session start to state.db")
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close state.db")


def sync_session_usage(session_id: str, input_tokens: int=0, output_tokens: int=0,
                       estimated_cost=None, model=None, title: Optional[str] = None,
                       message_count: Optional[int] = None, profile: Optional[str] = None,
                       cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                       api_call_count: Optional[int] = None, title_source: str = 'derived') -> None:
    """Update token usage and title for a WebUI session in state.db.
    Called after each turn completes. Uses absolute=True to set totals
    (the WebUI Session already accumulates across turns).

    ``profile`` lets the caller name the target state.db explicitly,
    which is what fixes #2762: this function is invoked from the
    agent streaming worker thread, where the request-thread's TLS
    profile context has not been propagated. Without an explicit
    profile, the TLS lookup falls back to the process-global active
    profile and writes the session's usage to the wrong state.db
    (e.g. ``hiyuki``'s instead of the cookie-switched ``maiko``'s).
    """
    db = _get_state_db(profile=profile)
    if not db:
        return
    try:
        # Ensure session exists first (idempotent)
        db.ensure_session(session_id=session_id, source='webui', model=model)
        # Set absolute token counts. WebUI's sidecar already accumulates
        # input/output/cache totals across turns, so mirror the same absolute
        # values into state.db. Omitting cache counters makes insights/reporting
        # show false 0% hit rates even when the live stream and sidecar saw warm
        # prefix reads.
        db.update_token_counts(
            session_id=session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            api_call_count=api_call_count,
            estimated_cost_usd=estimated_cost,
            model=model,
            absolute=True,
        )
        # Update title if we have one, using the public API
        if title:
            try:
                if title_source == 'user':
                    db.set_session_title(session_id, title)
                elif hasattr(db, 'set_auto_title'):
                    # Usage mirrors may contain a first-message placeholder.
                    # Never consume the Agent's one-time derived -> llm upgrade.
                    db.set_auto_title(session_id, title, source=title_source)
                else:
                    db.set_auto_title_if_empty(session_id, title)
            except Exception:
                logger.debug("Failed to sync session title to state.db")
        # Update message count
        if message_count is not None:
            try:
                def _set_msg_count(conn):
                    conn.execute(
                        "UPDATE sessions SET message_count = ? WHERE id = ?",
                        (message_count, session_id),
                    )
                db._execute_write(_set_msg_count)
            except Exception:
                logger.debug("Failed to sync message count to state.db")
    except Exception:
        logger.debug("Failed to sync session usage to state.db")
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close state.db")


def _read_title_state(db, session_id):
    if hasattr(db, 'get_session_title_source'):
        row = db._read_one("SELECT title, title_source FROM sessions WHERE id = ?", (session_id,))
        return (row['title'], row['title_source']) if row else (None, None)
    return db.get_session_title(session_id), None


def get_session_title_state(session_id: str, profile: Optional[str] = None):
    """Read the canonical (title, provenance), or None when Agent DB is absent.

    A nonempty title with unknown provenance is protected like a user title.
    Unlike optional insights sync, an unreadable existing DB must fail closed.
    """
    db = _get_state_db(profile=profile, strict=True)
    if db is None:
        return None
    try:
        return _read_title_state(db, session_id)
    finally:
        db.close()


def sync_session_title(session_id: str, title: str, profile: Optional[str] = None,
                       *, expected=None, replace: bool = False, explicit: bool = False):
    """Resolve a generated title BEFORE publishing it to the WebUI sidecar.

    Return the persisted (title, source), including collision suffixes or a
    racing user rename. With no Agent DB, retain standalone WebUI behavior.
    Refresh/explicit regeneration compare against the pre-generation snapshot;
    automatic refresh never replaces user/unknown provenance. Errors propagate
    so callers cannot advertise a title that the canonical DB rejected.
    """
    db = _get_state_db(profile=profile, strict=True)
    if db is None:
        return title, 'llm'
    try:
        db.ensure_session(session_id=session_id, source='webui')
        modern = hasattr(db, 'get_session_title_source') and hasattr(db, 'set_auto_title')
        candidate = db.sanitize_title(title) if hasattr(db, 'sanitize_title') else title
        for attempt in range(3):
            try:
                if modern and replace and expected is not None:
                    # Agent's set_auto_title only upgrades provenance; equal-rank
                    # refresh is deliberately a separate, snapshot-fenced write.
                    # BEGIN IMMEDIATE (owned by _execute_write) serializes the
                    # collision check and CAS with Agent/CLI title writers.
                    def update(conn, candidate=candidate):
                        row = conn.execute(
                            "SELECT title, title_source, hidden FROM sessions WHERE id = ?", (session_id,)
                        ).fetchone()
                        if row is None or (row['title'], row['title_source']) != expected:
                            if explicit:
                                raise TitleChangedError('Session title changed while generating; retry explicitly')
                            return
                        if row['title'] and not explicit and row['title_source'] not in ('derived', 'llm'):
                            return
                        if row['hidden'] and row['title'] == getattr(db, 'CANONICAL_BOT_CHAT_TITLE', 'Bot Chat'):
                            return
                        if conn.execute("SELECT 1 FROM sessions WHERE title = ? AND id != ?",
                                        (candidate, session_id)).fetchone():
                            raise ValueError('Title collision')
                        conn.execute(
                            "UPDATE sessions SET title = ?, title_source = ? WHERE id = ? AND title IS ? AND title_source IS ?",
                            (candidate, 'llm', session_id, *expected),
                        )
                    db._execute_write(update)
                elif modern:
                    db.set_auto_title(session_id, candidate, source='llm')
                else:
                    # Old Agents can fill a blank title but cannot safely refresh
                    # one without provenance/CAS. Read back their winning value.
                    db.set_auto_title_if_empty(session_id, candidate)
                break
            except ValueError:
                if attempt == 2:
                    raise
                candidate = db.get_next_title_in_lineage(candidate)
        persisted = _read_title_state(db, session_id)
        if not persisted[0]:
            raise RuntimeError('Agent DB did not persist a session title')
        return persisted
    finally:
        db.close()
