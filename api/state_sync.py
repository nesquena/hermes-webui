"""
Hermes Web UI -- state.db sync bridge.

Mirrors WebUI session metadata (token usage, title, model) into the
hermes-agent state.db so that /insights, session lists, and cost
tracking include WebUI activity.

Usage/title mirroring is opt-in via the 'sync_to_insights' setting
(default: off). ``sync_session_cwd`` is not: it only fills the workspace
into the row the Agent already created, so clients that group sessions by
``cwd`` (Hermes Desktop) place WebUI sessions under their workspace.
All operations are wrapped in try/except -- if state.db is unavailable,
locked, or the schema doesn't match, the WebUI continues normally.

The bridge uses absolute token counts (not deltas) because the WebUI
Session object already accumulates totals across turns. This avoids
any double-counting risk.
"""
import logging
import ntpath
import os
import threading
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


def _normalize_session_cwd(workspace) -> str:
    """Canonical text form of a WebUI workspace for ``sessions.cwd``.

    Trailing ``/`` and ``\\`` separators are stripped (``/a/b/`` and ``/a/b``
    are one workspace) so equality checks and prefix grouping stay stable.
    Whitespace is never trimmed: it can be part of a real name. A path
    that is only an anchor is returned untouched: ``/``, a drive root such
    as ``C:\\`` (``C:`` would be drive-relative, a different path) and a UNC
    share root such as ``\\\\host\\share\\``.
    """
    text = str(workspace or "")
    # ``strip()`` only detects blank input: surrounding whitespace is part of a
    # valid directory name (``/x/acme `` is not ``/x/acme``) and is kept.
    if not text.strip():
        return ""
    _drive, rest = ntpath.splitdrive(text)
    if not rest.strip("/\\"):
        return text
    return text.rstrip("/\\")


def sync_session_cwd(session_id: str, workspace, profile: Optional[str] = None, db=None) -> bool:
    """Mirror a WebUI session's workspace into ``sessions.cwd`` in state.db.

    The agent creates the state.db row lazily on the first turn but only
    stamps ``cwd`` for CLI-family sources, so WebUI rows were left with an
    empty ``cwd`` and clients that group sessions by working directory
    (Hermes Desktop) filed them under "Home" instead of their workspace.

    Only an EXISTING row whose ``source`` is ``webui`` is updated; this never
    creates one, so sessions that
    never sent a message stay out of state.db exactly as before. A row that
    already records the same ``cwd`` is left untouched, so the per-row Git
    metadata generation is only bumped on a real workspace change (where the
    stale ``git_branch``/``git_repo_root`` are cleared by
    ``update_session_cwd``). Not gated by ``sync_to_insights``: the row is
    written by the agent regardless of that setting.

    ``db`` lets the streaming path reuse the agent's own SessionDB (already
    bound to the session's profile); it is not closed here. Otherwise the
    profile's state.db is opened via ``_get_state_db(profile=...)`` (#2762).
    Returns True when a row was updated.
    """
    cwd = _normalize_session_cwd(workspace)
    if not session_id or not cwd:
        return False
    owns_db = db is None
    if owns_db:
        # A legacy ``profile=None`` session lives in the root home. Without an
        # explicit name ``_get_state_db`` would fall back to the process-active
        # profile, which this background write must never read.
        db = _get_state_db(profile=profile or "default")
    if not db:
        return False
    try:
        if not hasattr(db, "update_session_cwd"):
            return False
        row = db.get_session(session_id)
        if not row:
            return False
        # Only rows the WebUI owns: an imported or foreign session (CLI, TUI,
        # Desktop, gateway) keeps its own working directory and git metadata.
        if row.get("source") != "webui":
            return False
        if _normalize_session_cwd(row.get("cwd")) == cwd:
            return False
        return db.update_session_cwd(session_id, cwd) is not None
    except Exception:
        logger.debug("Failed to sync session cwd to state.db for %s", session_id)
        return False
    finally:
        if owns_db:
            try:
                db.close()
            except Exception:
                logger.debug("Failed to close state.db")


_CWD_SYNC_LOCK = threading.Lock()
_CWD_SYNC_THREADS: set = set()
_CWD_SYNC_THREADS_LOCK = threading.Lock()


def sync_session_cwd_background(resolve) -> threading.Thread:
    """Run :func:`sync_session_cwd` off the caller's thread.

    The cwd mirror is optional metadata, but ``SessionDB`` writes retry for up
    to ~20 s under contention. Stream cleanup and the workspace-update response
    must not wait for that, so the write runs on a daemon thread.

    ``resolve`` returns ``(session_id, workspace, profile)`` or ``None`` and is
    called *when the write runs*, under a module lock that serialises these
    writes. It must look the session up by id at that moment rather than close
    over a ``Session`` object: that object can be replaced (LRU eviction, disk
    reload) between scheduling and running, and a stale one would write an
    older workspace. Resolved that way, overlapping syncs converge on the
    latest value whatever order they run in. Failures are
    logged at debug level and never reach the caller.
    """
    def _worker():
        try:
            with _CWD_SYNC_LOCK:
                target = resolve()
                if target:
                    session_id, workspace, profile = target
                    sync_session_cwd(session_id, workspace, profile=profile)
        except Exception:
            logger.debug("Background session cwd sync failed", exc_info=True)
        finally:
            with _CWD_SYNC_THREADS_LOCK:
                _CWD_SYNC_THREADS.discard(threading.current_thread())

    thread = threading.Thread(target=_worker, name="webui-cwd-sync", daemon=True)
    with _CWD_SYNC_THREADS_LOCK:
        _CWD_SYNC_THREADS.add(thread)
    thread.start()
    return thread


def drain_cwd_syncs(timeout: float = 5.0) -> bool:
    """Wait for in-flight background cwd syncs (tests, orderly shutdown)."""
    with _CWD_SYNC_THREADS_LOCK:
        pending = list(_CWD_SYNC_THREADS)
    for thread in pending:
        thread.join(timeout)
    return not any(thread.is_alive() for thread in pending)


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


def persist_session_title_authority(
    session_id: str,
    title: str,
    profile: Optional[str] = None,
    *,
    source: str = "user",
):
    """Persist an explicit title/reset to Agent state before sidecar projection."""
    db = _get_state_db(profile=profile, strict=True)
    if db is None:
        return title, source
    try:
        db.ensure_session(session_id=session_id, source="webui")
        candidate = db.sanitize_title(title) if hasattr(db, "sanitize_title") else title
        # A reset is absence of a title, not a globally unique literal name.
        # Keep the WebUI placeholder out of Agent's unique-title namespace.
        resetting = source == "derived" and title == "Untitled"
        if resetting:
            candidate = None
        if source == "user":
            db.set_session_title(session_id, candidate)
        elif hasattr(db, "get_session_title_source") and hasattr(db, "_execute_write"):
            db._execute_write(
                lambda conn: conn.execute(
                    "UPDATE sessions SET title = ?, title_source = ? WHERE id = ?",
                    (candidate, source, session_id),
                )
            )
        elif resetting and hasattr(db, "_execute_write"):
            # Pre-provenance Agent stores have _execute_write but no title_source.
            db._execute_write(lambda conn: conn.execute(
                "UPDATE sessions SET title = NULL WHERE id = ?", (session_id,)))
        elif hasattr(db, "set_auto_title"):
            db.set_auto_title(session_id, candidate, source=source)
        elif resetting:
            raise RuntimeError("Agent DB cannot reset the session title")
        else:
            db.set_auto_title_if_empty(session_id, candidate)
        persisted = _read_title_state(db, session_id)
        if resetting:
            if persisted[0] is not None or (hasattr(db, "get_session_title_source") and persisted[1] != source):
                raise RuntimeError("Agent DB did not reset the session title")
            return title, source
        if not persisted[0]:
            raise RuntimeError("Agent DB did not persist a session title")
        return persisted
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
