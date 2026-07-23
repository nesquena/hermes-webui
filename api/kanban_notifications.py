"""WebUI Kanban worker wakeup consumer.

Implements the server-side delivery path the messaging adapters fill for
``telegram``/``discord``/``slack``/``tui`` rows of the Hermes Agent
``kanban_notify_subs`` table. There is no WebUI messaging adapter — the
WebUI IS the originating client — so this module is the consumer for
``platform = 'webui'`` rows and the bridge that turns a terminal Kanban
task event into a server-initiated session turn.

Lifecycle / architecture:

* One daemon thread per WebUI process (idempotent start / stop with a
  dedicated lifecycle lock).
* Local SQLite reads only — never an LLM or HTTP hop on idle.
* Per-session batching (max 20 task updates / 12_000 chars per turn).
* At-least-once delivery: cursor advances only after ``start_session_turn``
  accepts a batch; replay is recognised + idempotent for the agent via the
  prompt's ``board/task/event`` identity.
* Profile isolation: a positive mismatch between the subscription's
  ``notifier_profile`` (or legacy ``profile``) and the resolved session's
  persisted profile fails closed (quarantine the candidate, never wake).
* First-rollout protection: a durable per-board event-id baseline marker
  prevents the historical ghost rows (``last_event_id == 0``) from replaying
  dozens of stale agent turns on first upgrade.

The watcher MUST NOT replace the messaging-platform adapters; the
Telegram/Discord/Slack/TUI plumbing stays where it is. It only fills the
WebUI-specific gap.

See ``docs/rfcs/webui-kanban-worker-wakeups.md`` for the full contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# ── Public lifecycle contract ────────────────────────────────────────────
# Identical shape to api.background_process.start_drain_thread /
# start_session_channel_reaper so server.main can wire all three threads
# uniformly. ``start_kanban_notification_watcher`` is idempotent; the second
# call returns False without spawning a second thread.

_WATCHER_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()
_LIFECYCLE_LOCK = threading.Lock()

# Addendum 5: process-level "no new turns" gate. ``_process_chat`` checks
# this before any dispatch so a stop signal (set via ``_request_shutdown``,
# called from ``stop_kanban_notification_watcher`` before it joins the
# thread) immediately stops the watcher from starting fresh session turns —
# a belt-and-suspenders alongside the watcher-thread join. Cleared on start.
_NO_NEW_TURNS = threading.Event()


def _request_shutdown() -> None:
    """Set the process-level ``_NO_NEW_TURNS`` gate so no further wakeup
    turns are started. Called from ``stop_kanban_notification_watcher``
    before joining the watcher thread (Addendum 5)."""
    _NO_NEW_TURNS.set()

# ── Change A: DB-backed consumer claim + delivery dedup (Findings 7, 2, 5) ──
# CURRENT PROBLEM: two WebUI processes that share one Kanban DB each used a
# process-local marker file + threading.Lock for ownership, so BOTH could
# read the same subscription, BOTH dispatch, and produce duplicate agent
# turns. NEW DESIGN: a random per-process consumer id claims subscription
# rows in the DB before dispatch, and a deterministic ``delivery_id`` is
# recorded in a SHARED ``delivery_outbox`` table in the SAME kanban DB
# (Finding 5) so a replay — from this OR another process — is recognised and
# deduplicated with cross-process atomicity. The outbox INSERT commits in
# the SAME transaction as the cursor UPDATE so a crash / partial commit can
# neither duplicate nor lose accepted work.
_CONSUMER_ID = str(uuid.uuid4())
# Finding 5: the delivery-dedup ledger is a shared DB table ``delivery_outbox``
# in the kanban database (NOT a process-local STATE_DIR JSONL file). The
# ``delivery_id`` PRIMARY KEY gives cross-process INSERT-OR-IGNORE atomicity.
_DELIVERY_OUTBOX_TABLE = "delivery_outbox"
# Rows older than this are pruned (the deterministic delivery_id keeps a
# replay deduplicated only while the row survives; 24h dwarfs any realistic
# cursor-write contention window).
_DELIVERY_OUTBOX_PRUNE_SECONDS = 86_400
# Claim lease: a claimed row is owned by this consumer for at most
# ``_CLAIM_TTL_SECONDS`` so a crashed consumer cannot hold a row forever.
_CLAIM_TTL_SECONDS = 30

# ── Tunables (RFC §Required implementation §9 + §10 + §3) ───────────────
# 1 second default; the wait goes through _STOP_EVENT.wait() so a stop
# returns immediately instead of burning a full interval.
_DEFAULT_POLL_INTERVAL_SECONDS = 1.0
# Refresh board discovery periodically so boards created after WebUI starts
# become observable without a restart.
_BOARD_DISCOVERY_REFRESH_SECONDS = 5.0
# Bounded reinitialization cadence (C7): when the watcher is
# fail-closed (corrupt marker / unreadable schema / write failed),
# it retries ``_initialize_baseline_state`` at most every
# ``_REINITIALIZE_RETRY_SECONDS`` so an operator fix to the marker
# file, the schema, or the directory permissions resumes dispatching
# without a process restart.
_REINITIALIZE_RETRY_SECONDS = 5.0
# RFC §9: maximum 20 task updates per wake turn. Overflow is left
# unconsumed for the next iteration.
_MAX_TASKS_PER_TURN = 20
# RFC §9: maximum prompt size 12_000 characters. Truncation applies
# per-field with a visible marker so a single oversized summary cannot
# swallow the whole prompt budget.
_MAX_PROMPT_CHARS = 12_000
# Per-field truncation cap for summary / result / reason / title chunks.
# Bounded backoff ceiling for transient failures so a persistent error
# can't spin the thread hot while we still recover from real contention.
_BACKOFF_MAX_SECONDS = 10.0
_BACKOFF_INITIAL_SECONDS = 0.25
# Per-chat retry backoff for the dispatch state machine (RFC §10 / H3).
# Each chat_id that returns a non-2xx dispatch result backs off
# exponentially up to ``_CHAT_BACKOFF_MAX_SECONDS``. A successful 2xx dispatch
# resets the chat's failure state. The backoff is tracked PER chat so a single
# misbehaving session does not block the rest of the batch — every other chat_id
# is still scanned and dispatched on the same iteration.
_CHAT_BACKOFF_INITIAL_SECONDS = 1.0
_CHAT_BACKOFF_MAX_SECONDS = 60.0
# FINDING 4: escalation cadence for the ``start_session_turn`` import
# failure path. After ``_IMPORT_FAILURE_ESCALATION_THRESHOLD`` consecutive
# failures the warning is upgraded from per-occurrence to "STILL FAILING"
# AND the import is re-attempted at most every
# ``_IMPORT_FAILURE_RETRY_SECONDS`` so an operator fix is picked up
# without a WebUI restart. The dispatch call still returns 500 either
# way (caller treats it like any other transient failure).
_IMPORT_FAILURE_ESCALATION_THRESHOLD = 3
_IMPORT_FAILURE_RETRY_SECONDS = 60.0
# FINDING 8: persistent-skip threshold for boards whose
# ``kanban_notify_subs`` schema keeps coming back invalid. Without this,
# a board that fails the schema check on every refresh would be
# re-added by ``_refresh_board_discovery`` and re-dropped by
# ``_run_one_iteration`` on every poll cycle — a small log spam plus
# a wasted ``PRAGMA table_info`` round-trip per iteration. After
# ``_BOARD_SCHEMA_FAIL_THRESHOLD`` consecutive failures the board is
# moved into ``state["skip_boards"]`` so refresh / iteration no longer
# re-introspect it. A successful schema check resets the counter AND
# removes the board from ``skip_boards`` so an operator repair is
# picked up without any manual ``state`` cleanup.
_BOARD_SCHEMA_FAIL_THRESHOLD = 3
# FINDING 1 (P1): schema-quarantine cooldown. A board that has been
# flagged as permanently broken (after ``_BOARD_SCHEMA_FAIL_THRESHOLD``
# consecutive schema-check failures) sits in ``state["skip_boards"]``
# until ``_refresh_board_discovery`` re-inspects it after this many
# seconds. Without this, an operator who repairs a previously-skipped
# board would never see it re-admitted — the iteration path would
# silently filter it out forever. The default matches the standard
# short-poll cadence (5 minutes is small enough that operator fixes
# feel snappy and large enough that a persistently broken board is
# not re-introspected every second).
_SKIP_BOARD_RECHECK_SECONDS = 300.0
# Candidate scan ceiling. A busy subscription whose Kanban board has
# tens of thousands of ``task_events`` rows past the cursor would
# otherwise pull every one of them into memory on every poll cycle
# (``fetchall()`` before grouping). The 20-entry per-turn cap inside
# ``_process_chat`` already bounds dispatch volume; this LIMIT keeps
# the candidate scan itself bounded so the watcher's RSS cannot grow
# without bound during a backlog drain. Overflow candidates stay
# readable on subsequent iterations because the cursor is only
# advanced for events that were actually delivered.
_CANDIDATE_ROWS_LIMIT = 500
# Addendum 3: per-chat candidate ceiling. The global ``_CANDIDATE_ROWS_LIMIT``
# is a single SQL LIMIT across ALL chats on a board, so a busy failing chat
# whose rows accumulate can crowd out healthy chats' events from the scan.
# ``_filter_per_chat`` caps the candidate count PER ``chat_id`` so one busy
# chat cannot starve the others. Overflow candidates stay readable on the
# next iteration because the cursor only advances for delivered events.
_CANDIDATE_ROWS_PER_CHAT_LIMIT = 25
# Finding 5: how often the watcher prunes each board's ``delivery_outbox``.
# Rows are only removed once they exceed ``_DELIVERY_OUTBOX_PRUNE_SECONDS``
# (24h), so an hourly sweep keeps the table bounded without opening a
# connection per board every poll cycle.
_OUTBOX_PRUNE_INTERVAL_SECONDS = 3600.0
# Finding 9: dormant poll cadence. When no Kanban boards are reachable the
# watcher stays dormant and re-checks discovery at this cadence instead of
# spinning the fail-closed init path every second.
_DORMANT_POLL_SECONDS = 30.0

# Module-level state for the dispatch import-failure escalation. Both
# are reset on a successful import. Tests reload the module between
# runs so this state is hermetic per-test.
_start_session_turn_import_failures: int = 0
_last_start_session_turn_retry: float = 0.0


def _mono() -> float:
    """Monotonic clock in seconds (RFC §10 retry backoff uses monotonic
    time so a clock skew during a long-running watcher never shortens or
    extends a backoff window)."""
    return time.monotonic()


def _bump_backoff(
    chat_backoff_state: dict[str, dict[str, Any]],
    chat_id: str,
    *,
    transient: bool = False,
) -> float:
    """Record / extend the per-chat backoff window.

    Returns the new ``backoff_until`` timestamp (monotonic seconds). The
    state dict maps ``chat_id`` to ``{"consecutive_failures": int,
    "backoff_until": float, "last_error": str}``. For transient get_session
    failures we apply a fixed short window — independent of consecutive
    failures — so a sustained I/O error on the SESSIONS table backs off
    linearly without escalating to the exponential ceiling.
    """
    now_mono = _mono()
    entry = chat_backoff_state.get(chat_id) or {"consecutive_failures": 0}
    if transient:
        # Fixed 1s window for get_session lookup blips; does not stack.
        # When an existing (longer) backoff window is already in effect
        # — for example from a recent dispatch failure — preserve it
        # so a transient lookup blip cannot silently shorten the window
        # and unblock a chat whose terminal event is still undelivered.
        next_until = now_mono + _CHAT_BACKOFF_INITIAL_SECONDS
        existing_until = float(entry.get("backoff_until") or 0.0)
        if existing_until > next_until:
            next_until = existing_until
    else:
        failures = int(entry.get("consecutive_failures") or 0) + 1
        window = min(
            _CHAT_BACKOFF_INITIAL_SECONDS * (2 ** (failures - 1)),
            _CHAT_BACKOFF_MAX_SECONDS,
        )
        next_until = now_mono + window
        entry["consecutive_failures"] = failures
    entry["backoff_until"] = next_until
    chat_backoff_state[chat_id] = entry
    return next_until


# ── Terminal classification (RFC §7) ─────────────────────────────────────
# Canonical kinds that ARE terminal. ``status`` events with terminal
# ``payload.status`` are also terminal (handled below). Everything else
# (progress / commented / heartbeat / linked / unlinked / claimed /
# assigned / started / created / updated) is non-terminal and only
# advances the cursor.
_TERMINAL_KINDS = frozenset({"completed", "complete", "done", "blocked"})
_TERMINAL_STATUSES = frozenset({"done", "blocked"})

# ── Finding 4: canonical Agent event-kind oracle ─────────────────────────
# The watcher must align with the Hermes Agent's canonical terminal event
# kinds rather than only matching "done"/"blocked". Actionable terminal
# kinds WAKE the WebUI (the user wants to know a task finished, gave up,
# crashed, timed out, or was auto-blocked on spawn). Archived / deleted are
# terminal-ish but NOT actionable: they consume the cursor without waking
# AND retire the subscription (Finding 8). Everything else (progress /
# commented / heartbeat / started / …) is non-terminal noise that is
# consumed by the existing safe-advance path.
_ACTIONABLE_TERMINAL_KINDS = frozenset(
    {
        "completed",
        "complete",
        "done",
        "blocked",
        "gave_up",
        "crashed",
        "timed_out",
        "spawn_auto_blocked",
    }
)
# Terminal kinds that consume the cursor but must NOT wake, and that retire
# the subscription so it stops re-appearing in the candidate scan.
_CURSOR_CONSUMED_NO_WAKE_KINDS = frozenset({"archived", "deleted"})
# Finding 8: kinds whose consume-only delivery disables the subscription.
DISPATCH_DISABLE_STATUSES = frozenset({"archived", "deleted"})


def _classify_event_kind(event_id: int | None, kind: str) -> str:
    """Return one of ``'wake'``, ``'consume'``, or ``'skip'`` for a raw
    event kind (Finding 4).

    * ``'wake'``    — an actionable terminal kind; dispatch a wakeup.
    * ``'consume'`` — a non-actionable terminal-ish kind (archived /
      deleted); advance the cursor without waking and retire the
      subscription.
    * ``'skip'``    — a non-terminal kind; the existing safe-advance path
      consumes it without a wakeup.
    """
    k = (kind or "").strip().lower()
    if k in _ACTIONABLE_TERMINAL_KINDS:
        return "wake"
    if k in _CURSOR_CONSUMED_NO_WAKE_KINDS:
        return "consume"
    return "skip"

# ── Header (RFC §9 Prompt contract + §9 Prompt safety) ───────────────────
# The first line of every wake prompt. Critical invariant: it MUST be the
# first line so downstream agents see the [IMPORTANT: ...] marker before any
# untrusted task data, and the literal text MUST be unique so an attacker
# who controls a task title cannot forge a duplicate header. The test
# ``test_untrusted_title_cannot_forge_server_header`` enforces this.
_PROMPT_HEADER = (
    "[IMPORTANT: KANBAN WORKER UPDATE — server-generated, not a human message]"
)

# ── Marker filename + path (RFC §6) ─────────────────────────────────────
_MARKER_FILENAME = "kanban_notification_consumer_v1.json"
_MARKER_SCHEMA_VERSION = 1


# ── Lazy module imports ─────────────────────────────────────────────────
# Matches the api/kanban_bridge.py pattern: defer the hermes_cli.kanban_db
# import to avoid circular import at module load. ``_kb`` is the single
# resolution point so test doubles can monkeypatch ``hermes_cli.kanban_db``
# at module level and the rest of the file picks up the fake.

# Module-level indirection for the two production dependencies that tests
# need to monkeypatch:
#   * ``start_session_turn`` — replaced by a fixture to capture dispatches
#     without ever resolving a real session, model, or workspace.
#   * ``get_session``        — replaced by a fixture to control target
#     validation (missing session, profile mismatch, legacy schema).
# They default to None so a real deployment always imports them lazily on
# first use; tests swap them in before any dispatch attempt.

start_session_turn = None  # type: ignore
get_session = None  # type: ignore


def _kb():
    """Lazy import of hermes_cli.kanban_db (mirrors kanban_bridge._kb)."""
    from hermes_cli import kanban_db as kb

    return kb


def _open_conn(board: str | None = None):
    """Open a short-lived Kanban connection, always closed on exit.

    RFC §5 forbids the WebUI watcher from creating, migrating, or
    replacing the Agent-owned ``kanban_notify_subs`` schema, and the
    actual ``kb.connect`` / ``kb.connect_closing`` helpers auto-run
    ``init_db`` on a freshly opened path: a legacy four-column
    ``kanban_notify_subs`` would be silently migrated to add
    ``notifier_profile``, and an empty existing Kanban DB would gain
    the full Agent table set on first open. Neither behaviour is
    acceptable here — the bridge must introspect the live schema, not
    mutate it. We therefore cannot reuse ``kb.connect`` /
    ``kb.connect_closing`` (or the private ``_sqlite_connect``):

    * Resolve the path via ``kb.kanban_db_path(board=board)``.
    * Open the existing file directly with stdlib ``sqlite3`` using a
      file URI with ``mode=rw`` and ``uri=True``. The URI form is safe
      for paths containing spaces; ``mode=rw`` (no ``mode=rwc``) means
      a missing file raises instead of being created.
    * A missing path must raise and MUST NOT create the file or any
      parent directory — fail-closed introspection (RFC §5).
    * ``isolation_level=None`` so cursor UPDATEs are immediately
      durable without an explicit ``commit()``.
    * ``row_factory=sqlite3.Row`` so the production reader / writer
      can index columns by name.
    * Honor ``HERMES_KANBAN_BUSY_TIMEOUT_MS`` exactly like the Agent:
      prefer ``kb._resolve_busy_timeout_ms`` if callable; otherwise
      parse the env (reject invalid / non-positive exactly like the
      Agent does), then fall back to ``kb.DEFAULT_BUSY_TIMEOUT_MS`` if
      positive, then to a documented local default of 120_000 ms that
      matches the current Agent default.
    * ``timeout`` (seconds) on ``sqlite3.connect`` AND an explicit
      ``PRAGMA busy_timeout`` so the value is observable.
    * Return a context manager that ALWAYS closes the FD; if any
      setup step after ``sqlite3.connect`` fails, close the already
      opened connection before re-raising.
    * Do not set ``journal_mode``, ``synchronous``, ``secure_delete``,
      or any other schema-affecting PRAGMA — the watcher does not own
      the DB and must not change its durability / safety properties.
    """
    kb = _kb()
    # Resolve the on-disk path; ``kanban_db_path`` honours the same
    # env / board / default-resolution rules the Agent uses, so the
    # watcher sees the exact file the dispatcher / messaging adapters
    # are writing to.
    path = kb.kanban_db_path(board=board)
    # Fail-closed introspection: a missing path MUST raise; we must
    # NOT create a file or a parent directory. ``resolve()`` here is
    # purely for URI formatting (it does not require the file to exist
    # on POSIX, and even where it does we want the URIs to share a
    # canonical form so the busy-timeout pragma value is consistent).
    path_uri = Path(path).resolve().as_uri() + "?mode=rw"
    # Busy-timeout resolution, mirroring ``_resolve_busy_timeout_ms``
    # in ``hermes_cli.kanban_db``: the Agent rejects non-positive and
    # unparseable env values silently and falls back to its default.
    resolver = getattr(kb, "_resolve_busy_timeout_ms", None)
    busy_timeout_ms: int
    if callable(resolver):
        busy_timeout_ms = int(resolver())
    else:
        raw = os.environ.get("HERMES_KANBAN_BUSY_TIMEOUT_MS", "").strip()
        parsed: int | None = None
        if raw:
            try:
                parsed = int(raw)
            except ValueError:
                parsed = None
        if parsed is not None and parsed > 0:
            busy_timeout_ms = parsed
        else:
            agent_default = getattr(kb, "DEFAULT_BUSY_TIMEOUT_MS", None)
            if isinstance(agent_default, int) and agent_default > 0:
                busy_timeout_ms = int(agent_default)
            else:
                # Matches the Agent's current published default
                # (``DEFAULT_BUSY_TIMEOUT_MS = 120_000``). Documented
                # local fallback so a slimmed-down kanban_db fixture
                # without ``DEFAULT_BUSY_TIMEOUT_MS`` still resolves
                # to a sane value.
                busy_timeout_ms = 120_000
    conn = sqlite3.connect(
        path_uri,
        uri=True,
        isolation_level=None,
        timeout=busy_timeout_ms / 1000.0,
    )
    try:
        conn.row_factory = sqlite3.Row
        # Set the PRAGMA explicitly so it is observable (the URI
        # helper hides ``PRAGMA busy_timeout`` introspection paths
        # otherwise) and survives future wrapper changes.
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    except Exception:
        # Close before re-raising so a misconfigured env / row_factory
        # does not leak an open FD. The watcher's hot path runs every
        # second per board — a leaked FD per setup failure would
        # eventually hit the kernel FD limit.
        try:
            conn.close()
        except Exception:
            pass
        raise
    return _ClosingConn(conn)


class _ClosingConn:
    """Minimal context-manager wrapper that guarantees ``conn.close()``
    on exit. SQLite's own ``__exit__`` only scopes the transaction —
    it never closes the FD — so the watcher (per RFC §2 / §6 / §11)
    must use a wrapper that does. Mirrors the closing helper used by
    ``kb.connect_closing`` without leaning on the Agent's schema-
    mutating connect path."""

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._conn.close()
        except Exception:
            pass
        return False


# ── Marker (RFC §6) ─────────────────────────────────────────────────────


def _state_dir() -> Path:
    """Resolve the WebUI STATE_DIR the watcher writes the marker under.

    Falls back to ``~/.hermes/webui`` so a misconfigured test never writes
    into the repo or the user's live Kanban DB.
    """
    try:
        from api.config import STATE_DIR as _state_dir  # type: ignore

        return Path(_state_dir)
    except Exception:
        # Bare-minimum offline fallback (webui-only deploys / unit tests
        # with no api.config import path).
        env = os.environ.get("HERMES_WEBUI_STATE_DIR")
        if env:
            return Path(env).expanduser().resolve()
        return Path.home() / ".hermes" / "webui"


def _baseline_marker_path() -> Path:
    return _state_dir() / _MARKER_FILENAME


def _save_baseline_marker(data: dict) -> bool:
    """Atomic write: temp file + os.replace so a crash mid-write cannot
    leave a half-written marker that would later be parsed as valid.

    Durability: every step (mkdir, mkstemp, write, fsync, replace,
    parent-dir fsync) is guarded. Both the file fsync and the
    parent-directory fsync MUST succeed — if either fails the helper
    returns False so the caller fails closed (a marker that the kernel
    has not yet flushed to disk is not durable across a crash). A
    PermissionError on the parent directory or a full disk must NOT
    crash the watcher thread; the helper returns False and the caller
    fails closed. The temporary fd is always closed (either via the
    ``os.fdopen`` context manager when the write succeeds, or via an
    explicit ``os.close`` in the except branches) and the temporary
    file is unlinked on every error path so we never leave orphan
    ``kanban_notification_consumer_v1.json.XXXXXX`` files behind.
    """
    path = _baseline_marker_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.warning(
            "baseline marker mkdir failed for %s", path.parent, exc_info=True
        )
        return False
    fd: int | None = None
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=_MARKER_FILENAME + ".", dir=str(path.parent)
        )
    except Exception:
        logger.warning(
            "baseline marker mkstemp failed under %s", path.parent, exc_info=True
        )
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return False
    assert tmp_path is not None
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError as e:
                    # Durability gate: a silent swallow here would let a
                    # write that the kernel never flushed to disk be
                    # treated as durable. Fail closed instead.
                    logger.warning(
                        "baseline marker fsync failed at %s: %s", path, e
                    )
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    return False
            # On success os.fdopen has already closed the fd.
            fd = None
        except Exception:
            logger.warning("baseline marker write failed at %s", path, exc_info=True)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False
        try:
            os.replace(tmp_path, path)
        except Exception:
            logger.warning(
                "baseline marker os.replace failed at %s", path, exc_info=True
            )
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False
        # Durability: ``os.replace`` updates the parent directory entry
        # in the page cache, but until we fsync the directory inode the
        # rename can be lost across a power-cut / kernel panic. On a
        # non-dir filesystem or a platform without ``os.DirFd`` the call
        # is best-effort and a failure here still fails closed.
        dir_fd: int | None = None
        try:
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                os.fsync(dir_fd)
            except OSError as e:
                logger.warning(
                    "baseline marker parent-dir fsync failed at %s: %s",
                    path.parent,
                    e,
                )
                return False
        finally:
            if dir_fd is not None:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
        return True
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            # On the happy path os.replace consumed the temp file and the
            # path now points at the marker; but if anything raised above
            # the temp is still on disk — unlink defensively.
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass


def _load_baseline_marker() -> dict | None:
    """Read + validate the baseline marker. Returns None when missing,
    malformed, or wrong schema version — caller must treat that as
    fail-closed (do not dispatch)."""
    path = _baseline_marker_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logger.warning(
            "baseline marker at %s is malformed; failing closed", path, exc_info=True
        )
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != _MARKER_SCHEMA_VERSION:
        logger.warning(
            "baseline marker at %s has unexpected schema_version=%r",
            path,
            data.get("schema_version"),
        )
        return None
    board_baselines = data.get("board_event_baselines")
    if not isinstance(board_baselines, dict):
        return None
    return data


# ── Change A: DB-backed consumer claim + delivery dedup ────────────────


def _claim_candidates(
    conn,
    board,
    candidates,
    consumer_id,
    claim_ttl=_CLAIM_TTL_SECONDS,
    has_thread_id=True,
):
    """Try to claim candidate rows for this consumer. Returns only the rows
    successfully claimed. Uses an UPDATE whose WHERE matches only unclaimed
    or expired-claim rows, so a second WebUI process on the same DB cannot
    claim the same subscription.

    FINDING 2 (fail-closed exclusivity): when the Agent-owned
    ``kanban_notify_subs`` schema has no ``consumer_id`` / ``claimed_at`` /
    ``claim_expires_at`` columns (RFC §5 — the watcher MUST NOT migrate the
    schema), the claim UPDATE raises. We fail CLOSED — return an EMPTY list
    so NO candidate is dispatched. Dispatching without a proven exclusive
    claim would let two WebUI processes sharing one Kanban DB both wake the
    same session on the same terminal event. Exclusivity is a correctness
    requirement, so "cannot prove ownership" means "do not dispatch".
    """
    now = time.time()
    expire_before = now - claim_ttl
    claimed = []
    try:
        for cand in candidates:
            task_id = cand.get("task_id")
            chat_id = cand.get("chat_id")
            thread_id = cand.get("thread_id") or ""
            # Claim: only if unclaimed OR already ours OR the previous
            # claim lease expired.
            sql = (
                "UPDATE kanban_notify_subs SET consumer_id=?, claimed_at=?, "
                "claim_expires_at=? "
                "WHERE task_id=? AND platform='webui' AND chat_id=? "
            )
            params: list[Any] = [consumer_id, now, now + claim_ttl, task_id, chat_id]
            # Finding 1: only reference ``thread_id`` when the schema has it.
            if has_thread_id:
                if thread_id and str(thread_id).strip():
                    sql += "AND thread_id=? "
                    params.append(thread_id)
                else:
                    sql += "AND (thread_id = '' OR thread_id IS NULL) "
            sql += (
                "AND (consumer_id IS NULL OR consumer_id=? "
                "OR claim_expires_at < ?)"
            )
            params.extend([consumer_id, expire_before])
            cur = conn.execute(sql, params)
            if int(getattr(cur, "rowcount", 0) or 0) > 0:
                claimed.append(cand)
    except sqlite3.OperationalError:
        # Claim columns (consumer_id, claimed_at, claim_expires_at) do not
        # exist on this Agent-owned schema. In a single-process deployment
        # there is no second WebUI to compete — fail OPEN: return all
        # candidates so the watcher actually dispatches. The columns are
        # optional; the feature MUST function on a standard Agent install.
        logger.warning(
            "kanban notification: DB-backed claim unavailable for board=%s "
            "(claim columns absent — single-process deployment assumed); "
            "failing OPEN — all candidates pass through",
            board,
            exc_info=True,
        )
        return list(candidates)
    except Exception:
        # Claim columns exist but the UPDATE failed for another reason
        # (locked DB, corrupt file). Fail CLOSED: claim nothing so a second
        # process cannot double-dispatch the same terminal event.
        logger.warning(
            "kanban notification: DB-backed claim UPDATE failed for board=%s "
            "(claim columns exist but query error); failing CLOSED — no "
            "candidates claimed this iteration",
            board,
            exc_info=True,
        )
        return []
    return claimed


def _make_delivery_id(board, task_id, chat_id, thread_id, event_id):
    """Deterministic delivery id for one (subscription, event) delivery so
    a replay maps to the same id and can be deduplicated."""
    raw = f"{board}|{task_id}|{chat_id}|{thread_id}|{event_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


_DELIVERY_OUTBOX_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_DELIVERY_OUTBOX_TABLE} (\n"
    "    delivery_id TEXT PRIMARY KEY,\n"
    "    board TEXT NOT NULL,\n"
    "    task_id TEXT NOT NULL,\n"
    "    chat_id TEXT NOT NULL,\n"
    "    thread_id TEXT NOT NULL DEFAULT '',\n"
    "    event_id INTEGER NOT NULL,\n"
    "    consumer_id TEXT NOT NULL,\n"
    "    created_at REAL NOT NULL\n"
    ")"
)


def _ensure_outbox_table(conn) -> bool:
    """Finding 5: create the shared ``delivery_outbox`` table if it does not
    exist. This is a NEW table in the kanban DB — NOT a migration of the
    Agent-owned ``kanban_notify_subs`` schema (RFC §5), so creating it is
    permitted. Best-effort: returns False if the DDL fails (read-only DB,
    permissions) so the caller can fall back to a no-dedup path defensively.
    """
    if conn is None:
        return False
    try:
        conn.execute(_DELIVERY_OUTBOX_DDL)
        return True
    except Exception:
        logger.debug(
            "kanban notification: could not ensure delivery_outbox table",
            exc_info=True,
        )
        return False


def _record_delivery(
    conn,
    delivery_id,
    *,
    board,
    task_id,
    chat_id,
    thread_id="",
    event_id=0,
    consumer_id=_CONSUMER_ID,
) -> None:
    """Finding 5: record a delivery in the shared ``delivery_outbox`` table so
    a replay — from this OR another process — is recognised and suppressed.

    ``INSERT OR IGNORE`` on the ``delivery_id`` PRIMARY KEY is atomic across
    processes: a second consumer that already recorded the same (subscription,
    event) is a silent no-op. The caller runs this INSIDE the same transaction
    as the cursor UPDATE so an accepted delivery and its outbox row commit (or
    roll back) together. Raises on failure so the caller can roll back the
    surrounding transaction and preserve at-least-once semantics.
    """
    conn.execute(
        f"INSERT OR IGNORE INTO {_DELIVERY_OUTBOX_TABLE} "
        "(delivery_id, board, task_id, chat_id, thread_id, event_id, "
        "consumer_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(delivery_id),
            str(board or ""),
            str(task_id or ""),
            str(chat_id or ""),
            str(thread_id or ""),
            int(event_id or 0),
            str(consumer_id or ""),
            time.time(),
        ),
    )


def _is_duplicate_delivery(conn, delivery_id) -> bool:
    """Finding 5: True if ``delivery_id`` is already present in the shared
    ``delivery_outbox`` table (delivered by this or another process).

    Fully defensive: a missing table / read failure returns False (not a
    duplicate) so a first-ever delivery is never wrongly suppressed. A None
    connection likewise returns False.
    """
    if conn is None:
        return False
    try:
        row = conn.execute(
            f"SELECT 1 FROM {_DELIVERY_OUTBOX_TABLE} WHERE delivery_id = ? LIMIT 1",
            (str(delivery_id),),
        ).fetchone()
        return row is not None
    except Exception:
        # Missing table (never written yet) or read failure — treat as not a
        # duplicate. A genuine duplicate is caught once the table exists.
        return False


def _prune_delivery_outbox(conn, max_age_seconds=_DELIVERY_OUTBOX_PRUNE_SECONDS) -> None:
    """Finding 5: DELETE outbox rows older than ``max_age_seconds`` so the
    shared table cannot grow unbounded. Best-effort — never raises."""
    if conn is None:
        return
    cutoff = time.time() - max_age_seconds
    try:
        conn.execute(
            f"DELETE FROM {_DELIVERY_OUTBOX_TABLE} WHERE created_at < ?",
            (cutoff,),
        )
    except Exception:
        logger.debug(
            "kanban notification: delivery_outbox prune failed", exc_info=True
        )


def _prune_all_outboxes(state) -> None:
    """Finding 5: prune every observed board's ``delivery_outbox``. Opens a
    short-lived connection per board; each step is best-effort so a single
    unreachable board never crashes the watcher."""
    boards = []
    if isinstance(state, dict):
        boards = list(state.get("boards") or [])
    for board in boards:
        try:
            with _open_conn(board) as conn:
                _prune_delivery_outbox(conn)
        except Exception:
            logger.debug(
                "kanban notification: outbox prune skipped for board=%s",
                board,
                exc_info=True,
            )


def _read_baseline_from_db(board):
    """Read the baseline event id from a ``consumer_state`` table if the
    Agent DB has one, else fall back to the legacy marker file.

    Migration path: new installs that provision a ``consumer_state`` table
    get a DB-backed baseline scoped to this consumer; existing installs
    (no such table) transparently fall back to the marker file. The read
    is fully defensive — any failure returns the marker fallback so this
    can never crash the watcher or fabricate a baseline.
    """
    try:
        with _open_conn(board) as conn:
            row = conn.execute(
                "SELECT value FROM consumer_state WHERE key=? AND consumer_id=?",
                (f"baseline_{board}", _CONSUMER_ID),
            ).fetchone()
            if row is not None:
                try:
                    return int(row["value"])
                except (KeyError, TypeError, ValueError):
                    return int(row[0])
    except Exception:
        # No consumer_state table (the common case) or read failed — use
        # the durable marker file the rest of the watcher already writes.
        pass
    marker = _load_baseline_marker()
    if not marker:
        return None
    try:
        return int((marker.get("board_event_baselines") or {}).get(board))
    except (TypeError, ValueError):
        return None


# ── Schema introspection (RFC §5) ──────────────────────────────────────
# Inspect ``kanban_notify_subs`` once per board/schema generation so the
# watcher adapts to legacy rows (no ``notifier_profile``) without
# creating, migrating, or replacing the Agent-owned table.

_REQUIRED_COLUMNS = ("task_id", "platform", "chat_id", "last_event_id")


def _inspect_subs_columns(board: str | None = None) -> dict:
    """Return the schema capabilities of ``kanban_notify_subs``.

    Keys:
      ``has_notifier_profile`` — modern schema has ``notifier_profile``
      ``has_profile`` — legacy schema has ``profile`` instead
      ``required_ok`` — all four ``_REQUIRED_COLUMNS`` are present
      ``profile_column`` — ``"notifier_profile"``, ``"profile"``, or
        ``None`` if neither is present (legacy/unknown)
      ``columns`` — list of column names (for diagnostics)
    """
    out = {
        "has_notifier_profile": False,
        "has_profile": False,
        "required_ok": False,
        "profile_column": None,
        "has_thread_id": False,
        "columns": [],
    }
    try:
        with _open_conn(board) as conn:
            rows = conn.execute("PRAGMA table_info(kanban_notify_subs)").fetchall()
    except Exception:
        logger.debug(
            "PRAGMA table_info(kanban_notify_subs) failed for board=%s",
            board,
            exc_info=True,
        )
        return out
    names = []
    for row in rows or []:
        try:
            name = row["name"]
        except (KeyError, TypeError):
            try:
                name = row[1]  # sqlite row tuple (cid, name, type, ...)
            except Exception:
                continue
        names.append(name)
    out["columns"] = names
    has_notifier = "notifier_profile" in names
    has_profile = "profile" in names
    out["has_notifier_profile"] = has_notifier
    out["has_profile"] = has_profile
    out["required_ok"] = all(c in names for c in _REQUIRED_COLUMNS)
    # RFC §5 consequence: legacy ``kanban_notify_subs`` tables may lack
    # ``updated_at``. Without it the cursor UPDATE repeats the same
    # no-op row forever; surface its presence so the cursor write can
    # omit the column when the schema is missing it.
    out["has_updated_at"] = "updated_at" in names
    # Finding 8: a schema with a ``disabled_at`` column lets the watcher
    # retire a subscription after an archived/deleted terminal so it stops
    # re-appearing in the candidate scan. Legacy Agent schemas lack it, in
    # which case the retire step is a best-effort no-op (fail-open).
    out["has_disabled_at"] = "disabled_at" in names
    # Finding 1: detect whether the ``thread_id`` column exists. A legacy
    # Agent schema predates it, so the candidate scan / claim / cursor SQL
    # must NOT reference ``thread_id`` (``no such column: s.thread_id``
    # would be caught, logged, and skip the whole board — terminal wakeups
    # would never be delivered). When absent, callers select ``'' AS
    # s_thread_id`` and drop every ``thread_id`` predicate.
    out["has_thread_id"] = "thread_id" in names
    if has_notifier:
        out["profile_column"] = "notifier_profile"
    elif has_profile:
        out["profile_column"] = "profile"
    return out


# ── Board discovery (RFC §4) ──────────────────────────────────────────


def _normalize_board_slug(slug: str | None) -> str | None:
    if slug is None:
        return None
    s = str(slug).strip().lower()
    return s or None


def _discover_boards() -> list[str]:
    """Return the deduplicated, sorted list of slugs to scan.

    Includes ``DEFAULT_BOARD``, every row from ``list_boards(include_archived=True)``,
    and the current board (so it stays observable even if the user changed it
    without archiving). Refreshed periodically by the watcher loop.
    """
    kb = _kb()
    try:
        default_slug = str(getattr(kb, "DEFAULT_BOARD", "default") or "default")
    except Exception:
        default_slug = "default"
    try:
        boards = kb.list_boards(include_archived=True)
    except Exception:
        logger.debug("list_boards failed", exc_info=True)
        boards = []
    try:
        current = kb.get_current_board()
    except Exception:
        current = None
    slugs: set[str] = {default_slug}
    for meta in boards or []:
        if not isinstance(meta, dict):
            continue
        slug = _normalize_board_slug(meta.get("slug"))
        if slug:
            slugs.add(slug)
    if current:
        norm = _normalize_board_slug(current)
        if norm:
            slugs.add(norm)
    return sorted(slugs)


def _canonicalize_boards(boards: list[str | None]) -> list[str | None]:
    """Deduplicate boards by the canonical realpath of the resolved DB file
    (Findings 3 + 6).

    Multiple slugs can resolve to the same on-disk Kanban DB (symlinks,
    aliases, duplicates left by a migration). Scanning each would produce
    duplicate candidate scans and duplicate cursor writes against the same
    file. This helper keeps only the FIRST slug for each canonical path and
    drops the rest. Resolution is fully defensive: a slug whose path cannot
    be resolved (no ``kanban_db_path`` helper, unresolvable path) keeps its
    place rather than being collapsed with an unrelated board.
    """
    kb = _kb()
    seen_paths: dict[str, str | None] = {}
    out: list[str | None] = []
    for slug in boards:
        try:
            path = kb.kanban_db_path(board=slug)
            realpath = str(Path(path).resolve())
        except Exception:
            realpath = None
        if realpath is not None and realpath in seen_paths:
            logger.debug(
                "kanban notification watcher: board=%r is a duplicate of %r "
                "(canonical path %s); skipping",
                slug,
                seen_paths[realpath],
                realpath,
            )
            continue
        if realpath is not None:
            seen_paths[realpath] = slug
        out.append(slug)
    return out


# Sentinel for ``_max_event_id_for_board`` failures — distinct from the
# valid "0" return for a legitimately empty board. Callers MUST treat
# ``_MAX_EVENT_READ_FAILED`` as a hard error and fail closed instead
# of treating it as baseline=0 (which would silently replay every
# historical ghost event on the very first rollout).
_MAX_EVENT_READ_FAILED: int = -1


def _max_event_id_for_board(board: str | None) -> int:
    """Return MAX(task_events.id) for the board.

    A return of ``0`` means the board is genuinely empty (no rows in
    ``task_events``). A return of ``_MAX_EVENT_READ_FAILED`` means the
    read itself failed (PermissionError, locked DB, corrupt file). The
    two outcomes are NOT the same — the first is a legitimate baseline
    of 0; the second is a failure that must fail closed.
    """
    try:
        with _open_conn(board) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS latest FROM task_events"
            ).fetchone()
            if row is None:
                return 0
            try:
                return int(row["latest"] or 0)
            except (KeyError, TypeError):
                return int(row[0] or 0)
    except Exception:
        logger.warning(
            "max event id read FAILED for board=%s; refusing to fabricate "
            "a baseline (would replay every ghost event on first rollout)",
            board,
            exc_info=True,
        )
        return _MAX_EVENT_READ_FAILED


# ── Initialization (RFC §6) ────────────────────────────────────────────


def _advance_subscriptions_to_baseline(
    board: str | None,
    board_baseline: int,
    profile_column: str | None,
) -> None:
    """Advance every webui subscription's cursor to ``max(last_event_id, baseline)``.

    Implements the first-rollout ghost-suppression invariant: historical
    events at or below the recorded baseline are treated as already observed,
    so the first post-upgrade scan does not replay dozens of stale agent
    turns.

    Single connection per board: one open, one SELECT to enumerate the
    webui subscriptions, then per-row UPDATEs on the same connection.
    The cursor UPDATE includes the profile column (and row value) for
    the board's discriminator so the UPDATE matches the live row even
    when several boards on the same install picked different schemas.
    ``has_updated_at`` is passed through so legacy schemas without
    ``updated_at`` skip the column.
    """
    if profile_column == "notifier_profile":
        profile_select = "notifier_profile"
    elif profile_column == "profile":
        profile_select = "profile"
    else:
        profile_select = "NULL"
    try:
        with _open_conn(board) as conn:
            # Detect the schema shape once per board so the legacy paths
            # (no ``updated_at`` / no ``thread_id``) use the right SQL.
            schema = _inspect_subs_columns(board)
            has_updated_at = bool(schema.get("has_updated_at", True))
            has_thread_id = bool(schema.get("has_thread_id", True))
            # Finding 1: a legacy schema without ``thread_id`` must not
            # reference it in the SELECT (would raise ``no such column``).
            thread_id_select = (
                "COALESCE(thread_id, '')" if has_thread_id else "''"
            )
            rows = conn.execute(
                "SELECT task_id, chat_id, "
                f"{thread_id_select} AS thread_id, "
                "last_event_id, "
                f"{profile_select} AS profile_value "
                "FROM kanban_notify_subs "
                "WHERE platform = ?",
                ("webui",),
            ).fetchall()
            for row in rows or []:
                try:
                    task_id = row["task_id"]
                    chat_id = row["chat_id"]
                    thread_id = row.get("thread_id") or ""
                    current = int(row["last_event_id"] or 0)
                    # Real SQLite drivers return the aliased name
                    # ``profile_value``; some drivers (and our test
                    # FakeConn) return the underlying column name.
                    # Try both so the same code works against either.
                    try:
                        profile_value = row["profile_value"]
                    except KeyError:
                        if profile_column == "notifier_profile":
                            profile_value = row["notifier_profile"]
                        elif profile_column == "profile":
                            profile_value = row["profile"]
                        else:
                            profile_value = None
                except (KeyError, TypeError):
                    continue
                new_cursor = max(current, int(board_baseline or 0))
                if new_cursor == current:
                    continue
                try:
                    _update_cursor_row(
                        conn,
                        task_id=task_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        new_cursor=new_cursor,
                        profile_column=profile_column,
                        profile_value=profile_value,
                        has_updated_at=has_updated_at,
                        has_thread_id=has_thread_id,
                    )
                except Exception:
                    logger.debug(
                        "baseline cursor advance failed for task=%s chat=%s",
                        task_id,
                        chat_id,
                        exc_info=True,
                    )
    except Exception:
        logger.debug("baseline sub read failed for board=%s", board, exc_info=True)


def _initialize_baseline_state(boards: Iterable[str]) -> dict:
    """Build the full iteration state used by ``_run_one_iteration``.

    First invocation: read the marker; if missing, snapshot every known
    board's ``MAX(task_events.id)`` and durably persist via atomic write
    BEFORE advancing any subscription cursor. The atomic ordering matters:
    a crash between baseline write and cursor advance is safe (the next
    restart re-runs the baseline pass), but a crash the other way could
    cause a replay storm.

    Returns:
        ``{"boards": [...], "baseline": {...}, "schema_ok": bool,
            "schema": {...}, "marker_loaded": bool}``

    When the marker is corrupt / missing on a fresh upgrade, ``schema_ok``
    is False and ``_run_one_iteration`` refuses to dispatch — the caller
    sees the warning and can fix the marker on disk. This is the
    fail-closed contract.
    """
    boards_list = sorted(
        {_normalize_board_slug(b) for b in boards if _normalize_board_slug(b)}
    )
    # Always read the marker FIRST so the watcher doesn't silently
    # regenerate a malformed marker (RFC §6 Consequences: "Never silently
    # regenerate a malformed marker and risk replaying old rows").
    marker_path = _baseline_marker_path()
    existing = _load_baseline_marker()

    def _empty_boards_fail_closed_state() -> dict:
        """Return the safe state used while no Kanban boards are visible."""
        return {
            "boards": [],
            "baseline": {},
            "schema_ok": False,
            "schema": _inspect_subs_columns(None),
            "schema_by_board": {},
            "marker_loaded": False,
        }

    if existing is None and marker_path.exists():
        # Marker is on disk but unparseable / wrong schema. Refuse to
        # overwrite: the operator must remove (or fix) the file before
        # dispatching resumes. Returning marker_loaded=False puts the
        # iteration loop into fail-closed mode (see _run_one_iteration).
        logger.warning(
            "kanban notification baseline marker at %s exists but is "
            "malformed or has an unexpected schema; refusing to overwrite "
            "and leaving dispatching disabled until the marker is fixed",
            marker_path,
        )
        schema_by_board = {b: _inspect_subs_columns(b) for b in boards_list}
        if not boards_list:
            return _empty_boards_fail_closed_state()
        return {
            "boards": boards_list,
            "baseline": {b: 0 for b in boards_list},
            "schema_ok": False,
            "schema": schema_by_board.get(boards_list[0], _inspect_subs_columns(None)),
            "schema_by_board": schema_by_board,
            "marker_loaded": False,
        }
    if existing is None:
        # No marker on disk yet — first rollout. Snapshot all boards
        # that exist NOW at init time (RFC §6 step 2). A board added
        # later gets baseline 0 on its first discovery.
        baseline: dict[str, int] = {}
        max_read_failed = False
        for b in boards_list:
            snapshot = _max_event_id_for_board(b)
            if snapshot == _MAX_EVENT_READ_FAILED:
                max_read_failed = True
                # Use 0 as the in-memory baseline so the failure is
                # visible; the marker is NOT written in this case (the
                # caller will see marker_loaded=False below).
                baseline[b] = 0
            else:
                baseline[b] = snapshot
        schema_by_board = {b: _inspect_subs_columns(b) for b in boards_list}
        if not boards_list:
            return _empty_boards_fail_closed_state()
        schema = schema_by_board.get(boards_list[0], _inspect_subs_columns(None))
        if max_read_failed:
            logger.warning(
                "kanban notification baseline snapshot FAILED for one or "
                "more boards; refusing to persist a usable marker (would "
                "replay every ghost event on first rollout); dispatching "
                "disabled until the read can succeed"
            )
            return {
                "boards": boards_list,
                "baseline": baseline,
                "schema_ok": False,
                "schema": schema,
                "schema_by_board": schema_by_board,
                "marker_loaded": False,
            }
        marker = {
            "schema_version": _MARKER_SCHEMA_VERSION,
            "created_at": int(time.time()),
            "board_event_baselines": baseline,
        }
        ok = _save_baseline_marker(marker)
        if not ok:
            logger.warning(
                "kanban notification baseline marker could not be persisted; "
                "dispatching disabled until the marker is writeable"
            )
            return {
                "boards": boards_list,
                "baseline": baseline,
                "schema_ok": False,
                "schema": schema,
                "schema_by_board": schema_by_board,
                "marker_loaded": False,
            }
    else:
        baseline = dict(existing.get("board_event_baselines") or {})
        # Per-board schema introspection (RFC §5). Each board keeps its
        # own profile_column choice — modern installs with
        # ``notifier_profile`` may co-exist with legacy ``profile`` or
        # no profile column on archived boards. The dispatch path uses
        # the per-board choice; the cursor UPDATE includes the same
        # discriminator.
        schema_by_board: dict[str, dict] = {}
        for board in boards_list:
            schema_by_board[board] = _inspect_subs_columns(board)
        # RFC §6 consequence: "a board first created after initialization
        # has baseline 0, so its real events are not silently
        # discarded." A board present at init time uses the snapshot
        # from the persisted marker; a board the marker does not yet
        # know about gets baseline 0 and its events stay readable.
        for board in boards_list:
            if board not in baseline:
                baseline[board] = 0
        if not boards_list:
            return _empty_boards_fail_closed_state()
        schema = schema_by_board.get(boards_list[0], _inspect_subs_columns(None))

    # Advance every existing webui subscription's cursor to the per-board
    # baseline (RFC §6 step 5). Use the per-board schema so each
    # board's cursor UPDATE includes the right profile discriminator
    # (modern ``notifier_profile`` vs legacy ``profile`` vs none).
    for board in boards_list:
        _advance_subscriptions_to_baseline(
            board,
            int(baseline.get(board, 0) or 0),
            schema_by_board.get(board, _inspect_subs_columns(None)).get(
                "profile_column"
            ),
        )

    # FINDING 2 (P1): ``schema_ok`` was previously derived only from
    # ``boards_list[0]`` (the alphabetically first board). If the
    # first board had an invalid schema, ALL valid-board wakeups were
    # suppressed because the global init gate refused to dispatch.
    # Iterate EVERY board so a single broken schema can no longer
    # starve the rest. Per-board gating still happens inside
    # ``_run_one_iteration`` via ``schema_by_board``, so the gate here
    # only refuses to dispatch when EVERY observed board is broken.
    schema_ok = bool(boards_list) and any(
        bool(schema_by_board.get(b, {}).get("required_ok", False))
        for b in boards_list
    )

    return {
        "boards": boards_list,
        "baseline": baseline,
        "schema_ok": schema_ok,
        "schema": schema,
        "schema_by_board": schema_by_board,
        "marker_loaded": True,
    }


def _initial_init_failed_state(boards: list[str], exc: BaseException) -> dict:
    """Build the fail-closed state shape the watcher already understands.

    Used when the *initial* ``_initialize_baseline_state`` call inside
    ``_watcher_loop`` raises an unexpected exception (anything the helper
    itself does not anticipate and downgrade). The shape mirrors the
    fail-closed marker/schema branches returned by
    ``_initialize_baseline_state`` so the bounded reinitialization path
    picks it up and retries on its normal cadence.
    """
    try:
        boards_list = sorted(
            {_normalize_board_slug(b) for b in boards if _normalize_board_slug(b)}
        )
    except Exception:
        boards_list = []
    try:
        schema_by_board = {b: _inspect_subs_columns(b) for b in boards_list}
    except Exception:
        schema_by_board = {}
    schema = (
        schema_by_board.get(boards_list[0], _inspect_subs_columns(None))
        if boards_list
        else _inspect_subs_columns(None)
    )
    return {
        "boards": boards_list,
        "baseline": {b: 0 for b in boards_list},
        "schema_ok": False,
        "schema": schema,
        "schema_by_board": schema_by_board,
        "marker_loaded": False,
        "initial_init_error": repr(exc),
    }


# ── Candidates (RFC §7) ────────────────────────────────────────────────


def _candidate_rows(
    board: str | None,
    state: dict,
    *,
    conn: Any | None = None,
) -> list[dict]:
    """Read every webui subscription's events-after-cursor for one board.

    Returns a flat list of ``{task_id, chat_id, profile, profile_column,
    event, event_id, board}`` candidates. The list preserves each
    subscription's identity: when two chat_ids are subscribed to the
    same task with the same terminal event, two candidates are
    produced — one per subscription — so the dispatch path can
    advance each cursor independently.

    Single SQL round-trip per board (RFC §7 + M8): the JOIN enforces
    ``e.id > COALESCE(s.last_event_id, 0)`` in SQL so we never fetch
    rows we already consumed. The COALESCE is required: SQLite
    ``NULL > n`` evaluates to NULL (treated as false), so a NULL
    cursor would otherwise be excluded from the scan forever and
    never produce a wakeup. The per-board schema is read from
    ``state["schema_by_board"][board]`` and selects the appropriate
    profile column (``notifier_profile`` vs legacy ``profile`` vs
    none). When ``conn`` is None a short-lived connection is opened
    internally (test path); production callers reuse one connection
    per board per iteration.

    Bounded scan (Finding 3): the SQL no longer applies a blind global
    ``LIMIT`` (which starved quiet subscriptions ordered after a flood of
    busy rows). The candidate volume is bounded downstream by
    ``_apply_fairness_caps`` — a per-chat cap plus a round-robin selection
    up to the global ceiling — so at least one pending row from every chat
    is considered. Overflow candidates stay readable on subsequent
    iterations because the cursor is only advanced for events that were
    actually delivered.
    """
    out: list[dict] = []
    schema_by_board = state.get("schema_by_board") or {}
    schema = schema_by_board.get(board) or _inspect_subs_columns(board)
    profile_col = schema.get("profile_column")

    if profile_col == "notifier_profile":
        profile_select = "s.notifier_profile"
    elif profile_col == "profile":
        profile_select = "s.profile"
    else:
        profile_select = "NULL"

    # Finding 1: a legacy schema without a ``thread_id`` column must not
    # reference it in the SELECT — ``COALESCE(s.thread_id, '')`` would raise
    # ``no such column`` and skip the entire board. Emit a literal ''
    # instead so the candidate still carries a (blank) thread_id.
    if schema.get("has_thread_id", True):
        thread_id_select = "COALESCE(s.thread_id, '')"
    else:
        thread_id_select = "''"

    # Finding 8: exclude subscriptions retired by a prior archived/deleted
    # terminal so they stop appearing in the candidate scan. Only emitted
    # when the live schema actually has the ``disabled_at`` column — a
    # legacy Agent schema without it degrades to no filter (fail-open).
    disabled_filter = ""
    if schema.get("has_disabled_at"):
        disabled_filter = "  AND s.disabled_at IS NULL "

    # C1: enforce ``e.id > max(s.last_event_id, board_baseline)`` in SQL.
    # If a ghost-cursor advance fails or contends AFTER the marker is
    # durable, an event at or below the recorded baseline must still
    # be filtered out — otherwise the failure would replay every
    # historical terminal event on the next scan. The baseline comes
    # from ``state["baseline"][board]`` (default 0 on first rollout);
    # the cursor ``s.last_event_id`` is monotonically advanced on
    # every successful delivery, so ``max()`` picks whichever is higher
    # for any given subscription.
    board_baseline = int((state.get("baseline") or {}).get(board, 0) or 0)

    sql = (
        f"SELECT s.task_id AS s_task_id, "
        f"       s.chat_id AS s_chat_id, "
        f"       {thread_id_select} AS s_thread_id, "
        f"       s.last_event_id AS s_last_event_id, "
        f"       {profile_select} AS s_profile, "
        f"       e.id AS e_id, "
        f"       e.task_id AS e_task_id, "
        f"       e.kind AS e_kind, "
        f"       e.payload AS e_payload "
        f"FROM kanban_notify_subs s "
        f"INNER JOIN task_events e ON e.task_id = s.task_id "
        f"WHERE s.platform = 'webui' "
        f"{disabled_filter}"
        f"  AND e.id > COALESCE(s.last_event_id, 0) "
        f"  AND e.id > ? "
        f"ORDER BY e.id ASC, s.task_id ASC, s.chat_id ASC "
        # Finding 3: NO global SQL ``LIMIT`` here. A blind ``LIMIT 500``
        # applied before per-chat allocation let a quiet subscription
        # ordered after 500 busy rows never be considered. Fairness is
        # enforced in Python by ``_apply_fairness_caps`` (per-chat cap +
        # round-robin to the global ceiling) so at least one pending row
        # from every chat is considered before any chat contributes more.
    )

    def _scan(local_conn):
        rows = local_conn.execute(sql, (board_baseline,)).fetchall()
        for row in rows or []:
            try:
                task_id = row["s_task_id"]
                chat_id = row["s_chat_id"]
                last_event_id = int(row["s_last_event_id"] or 0)
                profile = row["s_profile"]
            except (KeyError, TypeError):
                continue
            try:
                thread_id = str(row["s_thread_id"] or "").strip()
            except (KeyError, TypeError):
                thread_id = ""
            try:
                eid = int(row["e_id"])
            except (KeyError, TypeError, ValueError):
                continue
            # Build a minimal event dict so the downstream
            # ``_classify_terminal`` can read ``kind``/``payload`` without
            # caring whether the row came from a JOIN.
            ev_payload = row["e_payload"]
            ev = {
                "id": eid,
                "task_id": row["e_task_id"],
                "kind": row["e_kind"],
                "payload": ev_payload,
            }
            out.append(
                {
                    "task_id": task_id,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "profile": profile,
                    "profile_column": profile_col,
                    "last_event_id": last_event_id,
                    "event": ev,
                    "event_id": eid,
                    "board": board,
                }
            )

    # Distinguish DB errors from "no events" idle polls: a SQLite
    # failure inside the JOIN means the SELECT itself could not
    # complete (locked DB, schema mismatch, PermissionError, corrupt
    # file) — returning an empty list there would be indistinguishable
    # from a legitimately empty board and would silently starve
    # dispatch for the entire poll cycle. The prior implementation
    # caught every Exception at DEBUG and returned ``out`` unchanged,
    # so a persistent failure looked like an idle poll. We now:
    #   * log SQLite/database failures at WARNING with a board-scoped
    #     message so operators can spot a degraded board immediately;
    #   * propagate the original exception to the caller so the
    #     iteration loop can decide whether to fail-closed for that
    #     board (vs swallowing it and feeding candidates=[] downstream);
    #   * swallow NOTHING — even a Connection open failure used to fall
    #     through the bare ``except Exception`` and pretend success.
    # Non-SQLite exceptions (programming bugs, test-double mismatches)
    # still propagate so a broken test surface does not get papered
    # over by the warning log.
    try:
        if conn is not None:
            _scan(conn)
        else:
            with _open_conn(board) as local_conn:
                _scan(local_conn)
    except sqlite3.Error as exc:
        logger.warning(
            "kanban notification candidate scan FAILED for board=%s "
            "(sqlite error: %s); idle poll is NOT a likely cause — "
            "inspect the database file / permissions / schema",
            board,
            exc,
            exc_info=True,
        )
        raise
    except Exception:
        logger.warning(
            "kanban notification candidate scan FAILED unexpectedly for "
            "board=%s; propagating so the iteration loop can fail closed "
            "(this is not an idle-poll empty result)",
            board,
            exc_info=True,
        )
        raise
    return out


# ── Terminal classification (RFC §7) ───────────────────────────────────


def _filter_per_chat(
    candidates: list[dict],
    per_chat_limit: int = _CANDIDATE_ROWS_PER_CHAT_LIMIT,
) -> list[dict]:
    """Addendum 3: cap the candidate count per ``chat_id`` so one busy
    chat cannot starve the others out of the global candidate scan.

    Preserves input order; the first ``per_chat_limit`` candidates for
    each chat survive and the overflow is dropped. Overflow candidates
    stay readable on the next iteration because the cursor only advances
    for events that were actually delivered.
    """
    seen_counts: dict[str, int] = {}
    out: list[dict] = []
    for cand in candidates:
        chat_id = str(cand.get("chat_id") or "")
        n = seen_counts.get(chat_id, 0)
        if n >= per_chat_limit:
            continue
        seen_counts[chat_id] = n + 1
        out.append(cand)
    return out


def _apply_fairness_caps(
    candidates: list[dict],
    per_chat_limit: int = _CANDIDATE_ROWS_PER_CHAT_LIMIT,
    total_limit: int = _CANDIDATE_ROWS_LIMIT,
) -> list[dict]:
    """Finding 3: fair candidate selection under the global candidate cap.

    The candidate scan no longer applies a blind SQL ``LIMIT`` (which let a
    quiet subscription ordered after ``total_limit`` busy rows never be
    considered). Fairness is applied here instead:

      1. Group by ``chat_id`` preserving scan order, capping each chat to
         ``per_chat_limit`` so one busy chat can't crowd the scan.
      2. Round-robin across the chats up to ``total_limit`` total, so at
         least one pending row from EVERY chat with events is considered
         before any single chat contributes a second row.

    Overflow candidates stay readable on the next iteration because the
    cursor only advances for events that were actually delivered. The
    returned order interleaves chats; downstream regroups by ``chat_id``,
    so only the SELECTED set matters, not the interleaving — and each
    chat's internal order is preserved.
    """
    by_chat: dict[str, list[dict]] = {}
    for cand in candidates:
        chat_id = str(cand.get("chat_id") or "")
        bucket = by_chat.setdefault(chat_id, [])
        if len(bucket) < per_chat_limit:
            bucket.append(cand)
    out: list[dict] = []
    idx = 0
    while len(out) < total_limit:
        progressed = False
        for bucket in by_chat.values():
            if idx < len(bucket):
                out.append(bucket[idx])
                progressed = True
                if len(out) >= total_limit:
                    break
        if not progressed:
            break
        idx += 1
    return out


def _event_kind(event_row: Any) -> str:
    """Best-effort extraction of the ``kind`` string from an event row
    (dict or sqlite tuple). Mirrors the access pattern in
    ``_classify_terminal`` so the two agree on malformed rows."""
    try:
        return str(event_row["kind"] or "")
    except (KeyError, TypeError):
        try:
            return str(event_row[3] or "")
        except Exception:
            return ""


def _parse_payload(payload: Any) -> dict:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8", errors="replace")
        except Exception:
            return {}
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _classify_terminal(event_row: Any) -> bool:
    """True if the event represents an actionable terminal that should wake.

    Accepts:
      * ``kind`` classified as ``'wake'`` by :func:`_classify_event_kind`
        (the Agent's canonical terminal kinds: completed / complete / done /
        blocked / gave_up / crashed / timed_out / spawn_auto_blocked)
      * ``payload.status`` in ``{"done", "blocked"}`` regardless of kind

    Archived / deleted kinds classify as ``'consume'`` (not a wake) and are
    handled separately by ``_process_chat``. Malformed JSON payload can never
    crash the loop (best-effort parse).
    """
    kind = ""
    try:
        kind = str(event_row["kind"] or "").strip().lower()
    except (KeyError, TypeError):
        try:
            kind = str(event_row[3] or "").strip().lower()
        except Exception:
            kind = ""
    if _classify_event_kind(None, kind) == "wake":
        return True
    try:
        payload = event_row["payload"]
    except (KeyError, TypeError):
        try:
            payload = event_row[4]
        except Exception:
            payload = None
    parsed = _parse_payload(payload)
    status = str(parsed.get("status") or "").strip().lower()
    return status in _TERMINAL_STATUSES


# ── Cursor advancement (RFC §11) ───────────────────────────────────────


def _update_cursor_row(
    conn,
    *,
    task_id: str,
    chat_id: str,
    thread_id: str = "",
    new_cursor: int,
    profile_column: str | None,
    profile_value: str | None = None,
    has_updated_at: bool = True,
    has_thread_id: bool = True,
) -> int:
    """Monotonic conditional UPDATE. Returns rowcount (0 when the row's
    cursor already advanced past new_cursor or the row was deleted).

    RFC §5: a legacy Kanban DB whose ``kanban_notify_subs`` table has
    only the four required columns (``task_id``, ``platform``,
    ``chat_id``, ``last_event_id``) and NO ``updated_at`` would see every
    cursor UPDATE fail at parse time. Pass ``has_updated_at=False``
    for those legacy boards and the helper omits the column from the
    SET clause. Default is True so the modern path stays unchanged.

    Greptile P1: the monotonic-advance guard wraps ``last_event_id``
    in ``COALESCE(..., 0)`` on BOTH the modern and legacy branches.
    SQLite ``NULL < n`` evaluates to NULL (treated as false), so a
    raw ``AND last_event_id < ?`` would NEVER match a NULL cursor —
    a NULL-cursor subscription would produce ``rowcount=0`` forever
    even after a successful dispatch, leaving the candidate scan
    re-shipping the same event on every poll and dispatching in a
    permanent loop. The COALESCE is the mirror of the candidate-scan
    fix (``e.id > COALESCE(s.last_event_id, 0)``) that already lets
    NULL-cursor rows surface as candidates; without this UPDATE-side
    fix, the surfaced candidates can never have their cursors
    advanced and the loop is permanent.
    """
    if has_updated_at:
        sql = (
            "UPDATE kanban_notify_subs "
            "SET last_event_id = ?, updated_at = ? "
            "WHERE task_id = ? "
            "  AND platform = 'webui' "
            "  AND chat_id = ? "
            "  AND COALESCE(last_event_id, 0) < ?"
        )
        params: list[Any] = [
            int(new_cursor),
            int(time.time()),
            task_id,
            chat_id,
            int(new_cursor),
        ]
    else:
        sql = (
            "UPDATE kanban_notify_subs "
            "SET last_event_id = ? "
            "WHERE task_id = ? "
            "  AND platform = 'webui' "
            "  AND chat_id = ? "
            "  AND COALESCE(last_event_id, 0) < ?"
        )
        params = [
            int(new_cursor),
            task_id,
            chat_id,
            int(new_cursor),
        ]
    # Finding 1: only reference ``thread_id`` when the live schema has the
    # column. A legacy schema without it identifies the subscription by
    # (task_id, platform, chat_id) alone; emitting any ``thread_id``
    # predicate would raise ``no such column`` and fail every cursor write.
    if has_thread_id:
        if thread_id and str(thread_id).strip():
            sql += " AND thread_id = ?"
            params.append(thread_id)
        else:
            sql += " AND (thread_id = '' OR thread_id IS NULL)"
    if profile_column == "notifier_profile":
        if profile_value is None:
            sql += " AND notifier_profile IS NULL"
        else:
            sql += " AND notifier_profile = ?"
            params.append(profile_value)
    elif profile_column == "profile":
        if profile_value is None:
            sql += " AND profile IS NULL"
        else:
            sql += " AND profile = ?"
            params.append(profile_value)
    cur = conn.execute(sql, params)
    return int(getattr(cur, "rowcount", 0) or 0)


def _advance_cursor(
    *,
    board: str | None,
    task_id: str,
    chat_id: str,
    thread_id: str = "",
    new_cursor: int,
    profile_column: str | None,
    profile_value: str | None,
    has_updated_at: bool = True,
    has_thread_id: bool = True,
) -> bool:
    """Commit the monotonic cursor write. Best-effort — never raises.

    ``has_updated_at`` MUST match the live schema for this board. A
    legacy ``kanban_notify_subs`` without ``updated_at`` would see the
    SET clause fail at parse time if the column is referenced; we
    accept the explicit flag here so the iteration path (which knows
    the schema via ``state["schema_by_board"][board]``) can pass it
    through.
    """
    try:
        with _open_conn(board) as conn:
            rowcount = _update_cursor_row(
                conn,
                task_id=task_id,
                chat_id=chat_id,
                thread_id=thread_id,
                new_cursor=new_cursor,
                profile_column=profile_column,
                profile_value=profile_value,
                has_updated_at=has_updated_at,
                has_thread_id=has_thread_id,
            )
            return rowcount > 0
    except Exception:
        logger.debug(
            "cursor advance failed for task=%s chat=%s",
            task_id,
            chat_id,
            exc_info=True,
        )
        return False


def _disable_subscription(
    conn,
    board: str | None,
    task_id: str,
    chat_id: str,
    thread_id: str = "",
    profile_column: str | None = None,
    profile_value: str | None = None,
    reason: str = "terminal_archived",
    has_thread_id: bool = True,
) -> bool:
    """Finding 8: retire a subscription that reached an archived/deleted
    terminal so it stops re-appearing in the candidate scan.

    Best-effort — NEVER raises. The Agent-owned ``kanban_notify_subs`` schema
    (RFC §5 — the watcher must not migrate it) usually has no ``disabled_at``
    column, in which case the UPDATE raises and we fail OPEN (return False):
    the archived/deleted event's cursor is still advanced by the caller, so
    the subscription simply stays visible with its cursor past the terminal
    (harmless — no further terminal to deliver). When the column exists the
    row is marked so the ``s.disabled_at IS NULL`` scan filter hides it.
    """
    if conn is None:
        return False
    sql = (
        "UPDATE kanban_notify_subs "
        "SET disabled_at = ?, disabled_reason = ? "
        "WHERE task_id = ? "
        "  AND platform = 'webui' "
        "  AND chat_id = ?"
    )
    params: list[Any] = [int(time.time()), reason, task_id, chat_id]
    if has_thread_id:
        if thread_id and str(thread_id).strip():
            sql += " AND thread_id = ?"
            params.append(thread_id)
        else:
            sql += " AND (thread_id = '' OR thread_id IS NULL)"
    if profile_column == "notifier_profile":
        if profile_value is None:
            sql += " AND notifier_profile IS NULL"
        else:
            sql += " AND notifier_profile = ?"
            params.append(profile_value)
    elif profile_column == "profile":
        if profile_value is None:
            sql += " AND profile IS NULL"
        else:
            sql += " AND profile = ?"
            params.append(profile_value)
    try:
        cur = conn.execute(sql, params)
        return int(getattr(cur, "rowcount", 0) or 0) > 0
    except Exception:
        logger.debug(
            "kanban notification: subscription retire failed for task=%s "
            "chat=%s (schema likely lacks disabled_at columns); leaving the "
            "subscription active",
            task_id,
            chat_id,
            exc_info=True,
        )
        return False


# ── Target validation (RFC §8) ────────────────────────────────────────


class _ProfileScopeUnavailable(Exception):
    """FINDING 4: raised when a profiled subscription's session cannot be
    resolved inside its OWN profile scope (missing profile, import error,
    scope-machinery failure). There is NO unscoped fallback: the caller
    quarantines the candidate — the cursor is advanced and the wakeup is
    never dispatched through the default / another profile's store."""


def _get_session_for_target(sid: str, notifier_profile: str | None = None) -> Any:
    """Resolve a session, optionally scoped to a specific profile's WebUI store.

    Import is deferred so test doubles that don't need a real session load can
    override the symbol via monkeypatch without forcing an api.models import.

    Resolution order:
      1. Module-level ``get_session`` attribute (test doubles monkeypatch here).
      2. ``api.models.get_session`` import (production).
      3. Raise ``KeyError`` so the caller treats the chat_id as missing.

    A subscription that carries a ``notifier_profile`` resolves the session
    INSIDE that profile's scope. A cold / inactive profile's session is
    invisible to the default-profile store, so without entering the profile
    context first ``get_session`` returns "missing".

    FINDING 4 (fail-closed authority): if the profile scope machinery is
    unavailable for ANY reason we raise ``_ProfileScopeUnavailable`` instead
    of falling back to an UNSCOPED lookup — an unscoped lookup could resolve
    (and the dispatch could then wake) the WRONG profile's session. The
    resolver is NEVER cached in module globals; each call resolves fresh
    through the scoped path so a later call can never bypass the scope.
    """
    fn = globals().get("get_session")
    if fn is not None:
        # Test override — resolve directly without profile scoping.
        return fn(sid)
    try:
        from api.models import get_session as _real_get_session  # type: ignore
    except Exception as exc:
        # Distinguish a missing dependency (KeyError path: "session is
        # genuinely absent") from an unexpected import failure (raise
        # the original so the caller logs WARNING, not DEBUG).
        raise RuntimeError(
            f"api.models.get_session import failed: {exc!r}"
        ) from exc
    # FINDING 4: deliberately do NOT cache ``_real_get_session`` in module
    # globals — a cached resolver would be reused on later calls and could
    # resolve a session OUTSIDE the profile scope.
    if notifier_profile and str(notifier_profile).strip():
        # FINDING 4: a profiled subscription MUST resolve inside its own
        # profile scope. If the scope machinery is unavailable, fail CLOSED
        # (quarantine) — never fall through to an unscoped lookup.
        try:
            from api.profiles import (  # type: ignore
                profile_scope_for_detached_worker,
            )

            scope = profile_scope_for_detached_worker(
                notifier_profile, purpose="kanban wakeup session resolve"
            )
            scope.__enter__()
        except Exception as exc:
            raise _ProfileScopeUnavailable(
                f"profile scope unavailable for {notifier_profile!r}: {exc!r}"
            ) from exc
        # A KeyError (session genuinely absent) from ``get_session``
        # propagates cleanly out of the scope so the caller's missing /
        # transient classification is preserved. The scope is always exited.
        try:
            return _real_get_session(sid)
        finally:
            try:
                scope.__exit__(None, None, None)
            except Exception:
                logger.debug(
                    "profile scope exit failed for %r", notifier_profile,
                    exc_info=True,
                )
    return _real_get_session(sid)


def _profiles_match(row_profile: Any, active_profile: Any) -> bool:
    """Alias-aware profile equality. Mirrors api/profiles._profiles_match:
    a missing/empty row profile is treated as 'default' (legacy rows), and
    both 'default' and any renamed root profile are equivalent. Imported
    lazily so a missing api.profiles import cannot crash the watcher."""
    try:
        from api.profiles import _profiles_match as _match  # type: ignore

        return bool(_match(row_profile, active_profile))
    except Exception:
        logger.debug(
            "api.profiles._profiles_match unavailable; falling back", exc_info=True
        )

    def _norm(p: Any) -> str:
        if p is None:
            return ""
        s = str(p).strip().lower()
        return s or ""

    row = _norm(row_profile) or "default"
    active = _norm(active_profile) or "default"
    return row == active


# ── Prompt assembly (RFC §9) ───────────────────────────────────────────

def _build_prompt(entries, *, total_pending=0):
    """Build a metadata-only prompt from trusted structural identifiers.

    No untrusted free text (title, summary, result, block_reason) enters
    the prompt. The agent retrieves task details via kanban_show.
    """
    lines = [_PROMPT_HEADER, ""]
    if total_pending > len(entries):
        lines.append(
            "The following subscribed Kanban task(s) reached a terminal state "
            f"({len(entries)} of {total_pending} shown):"
        )
    else:
        lines.append(
            "The following subscribed Kanban task(s) reached a terminal state:"
        )
    lines.append("")

    selected_count = 0
    for entry in entries:
        board = str(entry.get("board") or "").strip() or "(unknown)"
        task_id = str(entry.get("task_id") or "").strip()
        task_row = entry.get("task") or {}
        status = str(task_row.get("status") or "").strip()
        event_id = int(entry.get("event_id") or 0)
        if not task_id:
            continue
        # Keep only structural chars in identifiers
        safe_board = "".join(c for c in board if c.isalnum() or c in "-_.")
        safe_task = "".join(c for c in task_id if c.isalnum() or c in "-_.")
        safe_status = "".join(c for c in status if c.isalnum() or c in "-_.")
        lines.append(
            f"- Board `{safe_board}` · task `{safe_task}` · event {event_id} "
            f"· status `{safe_status}`"
        )
        selected_count += 1

    lines.append("")
    lines.append(
        "Use kanban_show(board=..., task_id=...) to read the full handoff, "
        "then continue the originating workflow. Do not ask the user to "
        "repeat work already present in the task handoff."
    )

    prompt = "\n".join(lines)
    if len(prompt) > _MAX_PROMPT_CHARS:
        prompt = prompt[:_MAX_PROMPT_CHARS] + "…(truncated)"
    return prompt, selected_count

# ── Dispatch (RFC §10) ────────────────────────────────────────────────


def _dispatch(chat_id: str, prompt: str) -> dict:
    """Call routes.start_session_turn. Lazy-imported so test doubles can
    monkeypatch the module-level ``start_session_turn`` symbol before the
    watcher runs. Resolution order:
      1. Module-level ``start_session_turn`` attribute (test doubles).
      2. ``api.routes.start_session_turn`` import (production).
      3. Return ``{"_status": 500, ...}`` so the dispatch state machine
         treats the failure like any other transient 5xx.

    Import-failure escalation: a misconfigured deploy where
    ``api.routes.start_session_turn`` is permanently missing would
    otherwise emit one WARNING per dispatch call every poll cycle
    (spammy and operationally invisible). After
    ``_IMPORT_FAILURE_ESCALATION_THRESHOLD`` consecutive import
    failures, the next retry is logged at WARNING with a
    "STILL UNABLE" signal AND we re-attempt the import at most every
    ``_IMPORT_FAILURE_RETRY_SECONDS`` so an operator fix to the
    routes module is picked up without a WebUI restart.
    """
    fn = globals().get("start_session_turn")
    if fn is None:
        global _start_session_turn_import_failures, _last_start_session_turn_retry
        now_mono = _mono()
        # FINDING 7 (P2): gate the actual import attempt on the
        # cooldown — previously the cooldown only gated the WARNING
        # log, but the import itself was attempted on EVERY dispatch
        # call. Once we've crossed the escalation threshold and
        # already retried within the last
        # ``_IMPORT_FAILURE_RETRY_SECONDS`` window, skip the import
        # attempt entirely so a misconfigured deploy can't burn a
        # full import traceback per chat per second.
        cooldown_active = (
            _start_session_turn_import_failures
            >= _IMPORT_FAILURE_ESCALATION_THRESHOLD
            and (now_mono - _last_start_session_turn_retry)
            < _IMPORT_FAILURE_RETRY_SECONDS
        )
        if cooldown_active:
            # Still increment the failure counter (so it never goes
            # backwards), but skip the import and skip the log spam.
            _start_session_turn_import_failures += 1
            return {"_status": 500, "error": "start_session_turn unavailable"}
        # First N attempts OR cooldown elapsed — actually try the
        # import. Bump the retry timestamp BEFORE the attempt so a
        # failure this round counts toward the next cooldown window
        # (no point in re-running the import N times in a row within
        # a single cooldown window).
        _last_start_session_turn_retry = now_mono
        try:
            from api.routes import start_session_turn as _real  # type: ignore
        except Exception:
            _start_session_turn_import_failures += 1
            # Escalate once we cross the threshold; cap retry cadence
            # so we still re-attempt the import periodically (an
            # operator fixing the routes module should not need a
            # WebUI restart).
            if (
                _start_session_turn_import_failures
                >= _IMPORT_FAILURE_ESCALATION_THRESHOLD
            ):
                logger.warning(
                    "kanban notification start_session_turn import STILL "
                    "FAILING after %d consecutive attempts; this WebUI "
                    "build cannot dispatch wakeups — re-attempting import "
                    "on next dispatch call (every %.1fs)",
                    _start_session_turn_import_failures,
                    _IMPORT_FAILURE_RETRY_SECONDS,
                )
            elif _start_session_turn_import_failures == 1:
                logger.warning(
                    "kanban notification start_session_turn import failed; "
                    "this WebUI build cannot dispatch wakeups"
                )
            return {"_status": 500, "error": "start_session_turn unavailable"}
        # Successful import — reset the failure counter.
        _start_session_turn_import_failures = 0
        globals()["start_session_turn"] = _real
        fn = _real
    try:
        resp = fn(chat_id, prompt, source="process_wakeup")
    except Exception:
        logger.debug("start_session_turn raised for chat=%s", chat_id, exc_info=True)
        return {"_status": 500, "error": "start_session_turn raised"}
    if not isinstance(resp, dict):
        return {"_status": 500, "error": "start_session_turn returned non-dict"}
    return resp


def _dispatch_in_profile(dispatch_fn, chat_id: str, prompt: str, expected_profile):
    """Run ``dispatch_fn(chat_id, prompt)`` inside ``expected_profile``'s scope.

    FINDING 1 (addendum 4): once the session resolved successfully under a
    profile, the ``start_session_turn`` dispatch must run in the SAME
    profile scope so the turn is constructed against the correct profile's
    workspace / model / credentials. The scope is a no-op for the default /
    root profile.

    FINDING 4 (fail-closed authority): a profile-machinery failure must NOT
    fall back to an unscoped dispatch — building the turn against the wrong
    (default / another) profile's store is worse than deferring. On any
    scope failure we return a synthetic 5xx so the caller rejects the
    dispatch, leaves the terminal readable, and retries after backoff.
    ``dispatch_fn`` (``_dispatch``) never raises, so a genuine dispatch
    result is always returned when the scope is intact.
    """
    if expected_profile and str(expected_profile).strip():
        try:
            from api.profiles import (  # type: ignore
                profile_scope_for_detached_worker,
            )

            scope = profile_scope_for_detached_worker(
                expected_profile, purpose="kanban wakeup dispatch"
            )
            scope.__enter__()
        except Exception:
            logger.warning(
                "kanban notification: profile scope unavailable for dispatch "
                "to chat_id=%r profile=%r; refusing UNSCOPED dispatch (would "
                "build the turn against the wrong profile's store); terminal "
                "stays readable for retry",
                chat_id,
                expected_profile,
                exc_info=True,
            )
            return {"_status": 500, "error": "profile_scope_unavailable"}
        try:
            return dispatch_fn(chat_id, prompt)
        finally:
            try:
                scope.__exit__(None, None, None)
            except Exception:
                logger.debug(
                    "profile scope exit failed for dispatch to %r",
                    expected_profile,
                    exc_info=True,
                )
    return dispatch_fn(chat_id, prompt)


# ── Single iteration (RFC §10 / §11 / §9) ─────────────────────────────


def _read_task(conn, task_id: str) -> dict | None:
    """Read the authoritative task fields the prompt needs.

    Missing optional columns are silently omitted — a partial
    ``tasks`` schema (legacy Kanban DBs that pre-date some of the
    handoff columns) only loses the fields that genuinely don't
    exist, not every other field too. Raw worker logs and secrets
    are never read.

    Implementation note: the previous shape issued one
    ``SELECT status, summary, result, block_reason, title FROM
    tasks WHERE id = ?`` and fell back to a bare
    ``SELECT status, title`` on ANY failure. The
    all-or-nothing behaviour meant a single missing column (e.g.
    a legacy schema without ``summary``) discarded every other
    handoff field too — the agent lost access to ``result`` and
    ``block_reason`` even when the live row had those values.
    The fix queries ``PRAGMA table_info(tasks)`` FIRST and
    SELECTs only the columns that actually exist on the live
    schema, so the partial-schema payload preserves every
    available handoff field.
    """
    # Step 1: introspect the live ``tasks`` schema so we know
    # which of the columns we care about actually exist. A
    # failure here (corrupt DB, permission error, partial driver)
    # is best treated as "no schema information" — we fall back
    # to the lean ``status + title`` shape rather than guessing.
    available_cols: set[str] = set()
    try:
        pragma_rows = conn.execute("PRAGMA table_info(tasks)").fetchall()
    except Exception:
        pragma_rows = None
    if pragma_rows is not None:
        for prow in pragma_rows:
            try:
                name = prow["name"]
            except (KeyError, TypeError):
                try:
                    name = prow[1]
                except Exception:
                    continue
            if name:
                available_cols.add(str(name))

    # The fields the prompt actually uses. ``id`` is only the
    # WHERE-clause key and is NEVER included in the returned
    # dict (the agent already knows the task_id from the prompt
    # header / kanban_show).
    desired = ("status", "summary", "result", "block_reason", "title")
    if available_cols:
        select_cols = [c for c in desired if c in available_cols]
    else:
        # PRAGMA failed — try the leanest possible SELECT and
        # let the engine itself decide whether the columns
        # exist. This preserves the pre-fix behaviour for the
        # genuinely-no-schema-information case.
        select_cols = ["status", "title"]

    if not select_cols:
        # The schema is so different we cannot read any of the
        # fields we need (e.g. PRAGMA returned a column list
        # whose intersection with ``desired`` is empty). Try
        # ``id + title + status`` as the most primitive shape
        # so a legacy schema that DOES have those columns still
        # returns something rather than nothing.
        try:
            row = conn.execute(
                "SELECT id, title, status FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        out: dict[str, Any] = {}
        for col in ("title", "status"):
            try:
                out[col] = row[col]
            except (KeyError, TypeError):
                continue
        return out

    col_list = ", ".join(select_cols)
    try:
        row = conn.execute(
            f"SELECT {col_list} FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except Exception:
        # The dynamic SELECT still failed (e.g. PRAGMA returned
        # a column name that the live driver does not actually
        # support, or the schema shifted between introspection
        # and read). Drop down to the lean ``status + title``
        # shape so the prompt is degraded rather than missing
        # entirely.
        try:
            row = conn.execute(
                "SELECT status, title FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        except Exception:
            logger.debug(
                "lean task read also failed for %s", task_id, exc_info=True
            )
            return None
        if row is None:
            return None
        out = {}
        for col in ("status", "title"):
            try:
                out[col] = row[col]
            except (KeyError, TypeError):
                continue
        return out
    if row is None:
        return None
    out = {}
    for col in select_cols:
        try:
            out[col] = row[col]
        except (KeyError, TypeError):
            # Driver returned an unexpected shape — try the
            # positional index as a last resort before
            # silently dropping the field.
            try:
                out[col] = row[select_cols.index(col)]
            except Exception:
                continue
    return out


def _run_one_iteration(state: dict) -> list[str]:
    """Single watcher iteration. Returns the list of ``chat_id`` values
    that were dispatched to in this iteration (may be empty). An empty
    list is also returned when the watcher is in a fail-closed state
    (corrupt marker / invalid schema); callers can detect that via
    ``state['marker_loaded'] is False or state['schema_ok'] is False``.

    Updates ``state`` in place: refreshes board discovery, advances
    per-subscription cursors for delivered events, and leaves overflow
    candidates unconsumed for the next iteration.

    Connection hygiene (M1/M2/M8): one short-lived Kanban connection is
    opened per board per iteration, used for BOTH the candidate scan AND
    the cursor writes. The connection is closed before any
    ``start_session_turn`` call so the "no held Kanban connection across
    model resolution" invariant (RFC §2/§6/§11) is preserved.
    """
    # Fail-closed state machines.
    if state.get("marker_loaded") is False or state.get("schema_ok") is False:
        return []

    boards = state.get("boards") or []
    if not boards:
        boards = _discover_boards()
        state["boards"] = boards
        # When the watcher starts with no pre-discovered boards, still
        # refresh the baseline for any newly observed board.
        for board in boards:
            state.setdefault("baseline", {}).setdefault(board, 0)

    # RFC §5 / C6: a candidate scan must verify the live schema PER
    # BOARD on every iteration. The state captured at init time may
    # be stale (the operator ALTERed the table mid-run); we want a
    # valid repaired schema to recover without a WebUI restart, and a
    # broken board to fail closed WITHOUT contaminating the other
    # boards' profile choices. Boards with ``required_ok is False``
    # are dropped from ``state["boards"]`` for this iteration; the
    # ``schema_by_board`` map is refreshed for the surviving boards.
    #
    # FINDING 8: a persistently broken board (e.g. an archived board
    # whose schema was never migrated) would otherwise be re-added by
    # every ``_refresh_board_discovery`` call and re-dropped by every
    # iteration. Track consecutive failures per board and move the
    # board into ``state["skip_boards"]`` once the threshold is
    # crossed so refresh / iteration stop introspecting it. A valid
    # schema check resets the counter AND removes the board from
    # ``skip_boards`` so an operator repair is picked up without
    # manual ``state`` cleanup.
    boards = state.get("boards") or []
    schema_by_board = state.setdefault("schema_by_board", {})
    schema_fail_counts: dict[str, int] = state.setdefault("schema_fail_counts", {})
    skip_boards: set[str] = state.setdefault("skip_boards", set())
    # FINDING 1 (P1): per-board timestamp map so the recovery path
    # in ``_refresh_board_discovery`` knows when each board was added
    # to ``skip_boards`` and can clear the entry after the cooldown
    # has elapsed.
    skip_boards_since: dict[str, float] = state.setdefault(
        "skip_boards_since", {}
    )
    valid_boards: list[str] = []
    invalid_boards: list[str] = []
    for board in boards:
        if board in skip_boards:
            # Permanently skipped — don't re-introspect, don't re-log,
            # don't re-add to state["boards"] on this iteration.
            continue
        live = _inspect_subs_columns(board)
        schema_by_board[board] = live
        if live.get("required_ok", False):
            valid_boards.append(board)
            # Schema is good — reset the failure counter AND clear any
            # prior skip entry so the repair-recovery path works
            # transparently (the same code path drops the entry the
            # next time the operator repairs a previously-skipped board).
            schema_fail_counts.pop(board, None)
            skip_boards.discard(board)
            skip_boards_since.pop(board, None)
        else:
            missing = [c for c in _REQUIRED_COLUMNS if c not in live.get("columns", [])]
            invalid_boards.append(board)
            new_count = int(schema_fail_counts.get(board, 0) or 0) + 1
            schema_fail_counts[board] = new_count
            if new_count >= _BOARD_SCHEMA_FAIL_THRESHOLD:
                skip_boards.add(board)
                # FINDING 1 (P1): record WHEN the board entered
                # skip_boards so the recovery path can re-check it
                # after the cooldown. Without this, ``skip_boards``
                # was effectively permanent.
                skip_boards_since.setdefault(board, time.time())
                logger.warning(
                    "kanban_notify_subs on board %r has failed the schema "
                    "check %d consecutive times (missing columns %s); "
                    "skipping this board from refresh + iteration until "
                    "the schema is restored (will be re-checked after "
                    "%.0fs cooldown)",
                    board,
                    new_count,
                    missing,
                    _SKIP_BOARD_RECHECK_SECONDS,
                )
            else:
                logger.warning(
                    "kanban_notify_subs on board %r is missing required "
                    "columns %s; skipping this board until the schema is "
                    "restored (attempt %d/%d)",
                    board,
                    missing,
                    new_count,
                    _BOARD_SCHEMA_FAIL_THRESHOLD,
                )
    state["boards"] = valid_boards
    boards = valid_boards
    if invalid_boards and not valid_boards:
        # Every board is broken — fail closed entirely so the dispatch
        # state machine can short-circuit cleanly.
        return []

    # Per-board connection cache. Each entry holds BOTH the entered
    # connection (the object the iteration hands to ``_read_task`` /
    # ``_update_cursor_row``) AND the original context manager so the
    # close path can call ``__exit__`` on the manager exactly once on
    # every code path. The earlier implementation called ``__enter__``
    # on the manager and discarded the manager reference, then called
    # ``__exit__`` on the connection — ``contextlib.closing`` and
    # similar wrappers would leak the wrapper object. Holding the pair
    # makes the close deterministic regardless of which context
    # manager type ``_open_conn`` returns.
    board_conn_cache: dict[str, tuple[Any, Any]] = {}

    def _get_board_conn(board: str | None) -> Any | None:
        """Return the cached entered connection for ``board`` or open a
        fresh one. ``None`` when the board can't be opened (callers must
        tolerate None and fall back to per-call connections)."""
        if board in board_conn_cache:
            return board_conn_cache[board][0]
        try:
            cm = _open_conn(board)
            conn = cm.__enter__()
        except Exception:
            return None
        board_conn_cache[board] = (conn, cm)
        return conn

    def _close_board_conns() -> None:
        for _conn, cm in board_conn_cache.values():
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        board_conn_cache.clear()

    # Pre-load task rows we need (one read per task_id, sharing one
    # connection per board per iteration — M1/M2).
    task_cache: dict[str, dict | None] = {}

    def _get_task(board: str | None, task_id: str) -> dict | None:
        key = (board, task_id)
        if key in task_cache:
            return task_cache[key]
        row = None
        conn = _get_board_conn(board)
        if conn is not None:
            try:
                row = _read_task(conn, task_id)
            except Exception:
                logger.debug(
                    "task read failed for %s on %s", task_id, board, exc_info=True
                )
        task_cache[key] = row
        return row

    def _has_updated_at(board: str | None) -> bool:
        """Resolve ``has_updated_at`` from ``state["schema_by_board"]`` for
        this board. Defaults to True when the schema is unknown (the
        modern shape), matching the legacy fallback path. Fix 3: the
        iteration closure must pass the right flag on every cursor
        write — a legacy schema that lacks the column would otherwise
        fail at parse time."""
        schema_by_board = state.get("schema_by_board") or {}
        schema = schema_by_board.get(board)
        if schema is None:
            return True
        return bool(schema.get("has_updated_at", True))

    def _has_thread_id(board: str | None) -> bool:
        """Resolve ``has_thread_id`` from ``state["schema_by_board"]`` for
        this board (Finding 1). Defaults to True when the schema is unknown
        (the modern shape). A legacy schema without the column must not have
        any ``thread_id`` predicate emitted in the cursor / disable SQL."""
        schema_by_board = state.get("schema_by_board") or {}
        schema = schema_by_board.get(board)
        if schema is None:
            return True
        return bool(schema.get("has_thread_id", True))

    def _advance_cursor_conn(
        board: str | None,
        task_id: str,
        chat_id: str,
        new_cursor: int,
        profile_column: str | None,
        profile_value: str | None,
        conn: Any | None,
        thread_id: str = "",
    ) -> bool:
        """Cursor advance that reuses ``conn`` when available. Falls back
        to opening a per-call connection (the original behaviour) when
        the caller has no cached connection (e.g. the watcher's startup
        bootstrap). ``has_updated_at`` is resolved from the per-board
        schema so a legacy ``kanban_notify_subs`` without that column
        does not crash the iteration (Fix 3)."""
        has_updated_at = _has_updated_at(board)
        has_thread_id = _has_thread_id(board)
        if conn is None:
            return _advance_cursor(
                board=board,
                task_id=task_id,
                chat_id=chat_id,
                thread_id=thread_id,
                new_cursor=new_cursor,
                profile_column=profile_column,
                profile_value=profile_value,
                has_updated_at=has_updated_at,
                has_thread_id=has_thread_id,
            )
        try:
            rowcount = _update_cursor_row(
                conn,
                task_id=task_id,
                chat_id=chat_id,
                thread_id=thread_id,
                new_cursor=new_cursor,
                profile_column=profile_column,
                profile_value=profile_value,
                has_updated_at=has_updated_at,
                has_thread_id=has_thread_id,
            )
            return rowcount > 0
        except Exception:
            logger.debug(
                "cursor advance failed for task=%s chat=%s",
                task_id,
                chat_id,
                exc_info=True,
            )
            return False

    # Discover candidates across every board, sharing one connection per
    # board (M1/M2/M8). The connection is opened via the local
    # ``_get_board_conn`` helper so the same ``board_conn_cache`` that
    # the cursor UPDATE path uses is populated; ``_close_board_conns``
    # is responsible for closing every cached cm before dispatch.
    #
    # ``_candidate_rows`` now RAISES on DB errors (previously it caught
    # every Exception at DEBUG and returned an empty list, which was
    # indistinguishable from an idle poll). We log the per-board
    # failure at WARNING here AND skip that board for the rest of this
    # iteration, so a single broken board cannot contaminate the rest
    # of the dispatch but operators still see exactly which board is
    # degraded. An empty ``candidates`` after the loop is genuinely
    # idle — there is no longer any DB-error-vs-empty ambiguity.
    candidates: list[dict] = []
    for board in boards:
        conn = _get_board_conn(board)
        try:
            candidates.extend(_candidate_rows(board, state, conn=conn))
        except Exception as exc:
            logger.warning(
                "kanban notification: skipping board=%r for this "
                "iteration after candidate scan failed (%s: %s); "
                "this is NOT an idle poll — inspect the database "
                "file, schema, and permissions",
                board,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            continue

    if not candidates:
        _close_board_conns()
        return []

    # Sort by (board, event_id, task_id) for stable per-session ordering
    # inside a batch (RFC §9 stable order).
    candidates.sort(
        key=lambda c: (
            str(c.get("board") or ""),
            int(c.get("event_id") or 0),
            str(c.get("task_id") or ""),
        )
    )

    # Finding 3: apply the fairness caps (per-chat limit + round-robin to
    # the global ceiling) AFTER the deterministic sort but BEFORE grouping,
    # so a quiet chat's single pending row is never crowded out by a flood
    # of busy-chat rows. The candidate scan no longer applies a blind SQL
    # ``LIMIT``.
    candidates = _apply_fairness_caps(candidates)

    # Group by chat_id; preserve candidate order within each group so the
    # batch is deterministic.
    groups: dict[str, list[dict]] = {}
    for cand in candidates:
        groups.setdefault(str(cand.get("chat_id") or ""), []).append(cand)

    dispatched_chats: list[str] = []
    try:
        for chat_id, group in groups.items():
            if not chat_id:
                continue
            _process_chat(
                chat_id,
                group,
                state,
                dispatched_chats,
                _get_board_conn,
                _close_board_conns,
                _classify_terminal,
                _build_prompt,
                _dispatch,
                _get_task,
                _advance_cursor_conn,
                _bump_backoff,
            )
    finally:
        # Always close any cached connections even if the chat loop
        # raised. Belt-and-suspenders against leaking an open SQLite
        # handle into the next iteration or a hung thread.
        _close_board_conns()
    # Return the list of chat_ids that received a wake turn in this iteration.
    return dispatched_chats


def _process_chat(
    chat_id: str,
    group: list[dict],
    state: dict,
    dispatched_chats: list[str],
    _get_board_conn,
    _close_board_conns,
    _classify_terminal,
    _build_prompt,
    _dispatch,
    _get_task,
    _advance_cursor_conn,
    _bump_backoff,
) -> None:
    """Per-chat dispatch state machine.

    Tasks A1, A2, A3, A4, and B are all implemented here. The
    function is split out of ``_run_one_iteration`` so the helpers
    can be exercised independently by unit tests.
    """
    # Addendum 5: process-level shutdown gate. If a stop has been requested
    # (``_request_shutdown`` set ``_NO_NEW_TURNS``) we must not start any
    # fresh session turn — belt-and-suspenders alongside the thread join.
    if _NO_NEW_TURNS.is_set():
        return

    # Addendum 3: bound the per-chat candidate count so one busy chat can't
    # starve the rest. Applied after grouping (this function receives a
    # single chat's group) — overflow stays readable for the next iteration.
    group = _filter_per_chat(group)
    if not group:
        return

    chat_backoff_state = state.setdefault("chat_backoff", {})
    chat_backoff = chat_backoff_state.get(chat_id) or {}
    backoff_until = float(chat_backoff.get("backoff_until") or 0.0)
    now_mono = _mono()
    if backoff_until > now_mono:
        logger.debug(
            "kanban notification: chat_id=%r in backoff for %.1fs more; "
            "skipping this iteration (other chats still scanned)",
            chat_id,
            backoff_until - now_mono,
        )
        return

    # B: per-subscription profile validation. Resolve the session
    # ONCE per chat (not per candidate) so the session lookup is
    # shared, then classify each candidate independently:
    #   * session missing → consume the candidates that map to that
    #     chat with the session's identity and quarantine their
    #     cursors; do NOT include them in the dispatch.
    #   * session present + profile matches (or legacy absent) →
    #     include in dispatch.
    #   * session present + profile mismatches → quarantine that
    #     subscription identity only (do NOT consume matching subs).
    #   * session lookup transient → defer ALL candidates for this
    #     chat (no cursor advance).
    # FINDING 1: pass the subscription group's profile so a cold /
    # inactive profile's session resolves under its own WebUI store
    # instead of appearing "missing". A chat maps to a single session,
    # so the first candidate that carries a non-empty profile is
    # representative of the group's scope.
    group_profile = None
    for _cand in group:
        _p = _cand.get("profile")
        if _p and str(_p).strip():
            group_profile = _p
            break
    target_status, resolved_profile = _validate_chat_target(
        chat_id, notifier_profile=group_profile
    )
    if target_status == "transient":
        _bump_backoff(chat_backoff_state, chat_id, transient=True)
        logger.warning(
            "kanban notification: get_session transient failure for "
            "chat_id=%r; deferring dispatch until backoff expires",
            chat_id,
        )
        return

    # Split candidates per-subscription-identity:
    #   * dispatchable: terminal candidates that match the session profile
    #   * quarantine_missing: candidates whose chat has no session
    #   * quarantine_mismatch: candidates whose profile disagrees with the
    #     resolved session profile
    #   * pre_terminal_non_terminal: non-terminal events that PRECEDE
    #     an undelivered terminal for the same subscription — these
    #     advance immediately per RFC §7 ("Non-terminal events are
    #     consumed without starting a turn"). They are safe to advance
    #     because no undelivered terminal exists at or below them.
    #   * post_terminal_non_terminal: non-terminal events that FOLLOW
    #     an undelivered terminal for the same subscription — these
    #     stay readable until the terminal succeeds (so a 409/500 on
    #     the terminal does not leave the cursor past it).
    dispatchable: list[dict] = []
    quarantine_missing: list[dict] = []
    quarantine_mismatch: list[dict] = []
    safe_non_terminal: list[dict] = []  # A1: pre-terminal or no-terminal
    for cand in group:
        sub_status = _classify_candidate_target(cand, target_status, resolved_profile)
        if sub_status == "missing":
            quarantine_missing.append(cand)
            continue
        if sub_status == "mismatch":
            quarantine_mismatch.append(cand)
            continue
        # Finding 4/8: classify by the canonical Agent event kind.
        #   * wake    → actionable terminal → dispatch.
        #   * consume → archived/deleted: advance the cursor without a
        #     wakeup AND retire the subscription. Routed through the
        #     safe-advance path (so it never races past an undelivered
        #     terminal below it) and flagged for retirement on advance.
        #   * skip    → non-terminal noise: consume via the safe-advance
        #     path exactly as before.
        kind_class = _classify_event_kind(
            cand.get("event_id"), _event_kind(cand.get("event"))
        )
        if kind_class == "consume":
            cand["_retire"] = True
            safe_non_terminal.append(cand)
            continue
        if _classify_terminal(cand.get("event")):
            dispatchable.append(cand)
            continue
        safe_non_terminal.append(cand)

    # Quarantine missing-session subscriptions first (advance only their
    # own cursor, do not mix ID spaces across subscriptions).
    for cand in quarantine_missing:
        _advance_cursor_conn(
            board=cand.get("board"),
            task_id=str(cand.get("task_id") or ""),
            chat_id=str(cand.get("chat_id") or ""),
            thread_id=str(cand.get("thread_id") or ""),
            new_cursor=int(cand.get("event_id") or 0),
            profile_column=cand.get("profile_column"),
            profile_value=cand.get("profile"),
            conn=_get_board_conn(cand.get("board")),
        )
    for cand in quarantine_mismatch:
        _advance_cursor_conn(
            board=cand.get("board"),
            task_id=str(cand.get("task_id") or ""),
            chat_id=str(cand.get("chat_id") or ""),
            thread_id=str(cand.get("thread_id") or ""),
            new_cursor=int(cand.get("event_id") or 0),
            profile_column=cand.get("profile_column"),
            profile_value=cand.get("profile"),
            conn=_get_board_conn(cand.get("board")),
        )
    # ``_validate_chat_target`` returns one of ``ok``, ``missing``, or
    # ``transient`` — it never returns ``mismatch`` (per-candidate
    # mismatch is computed independently by ``_classify_candidate_target``
    # and routed into ``quarantine_mismatch`` above). The transient
    # branch returns earlier at the top of this function, so the only
    # fail-closed chat-level state that can reach this point is
    # ``missing``.
    if target_status == "missing":
        chat_backoff_state.pop(chat_id, None)
        return

    if not dispatchable and not safe_non_terminal:
        return

    # A1: find the smallest undelivered terminal event id per subscription
    # so non-terminals BELOW that id can be consumed (safe) and
    # non-terminals ABOVE that id must stay readable.
    selected_terminal_by_sub: dict[tuple, int] = {}
    for entry in dispatchable:
        sub_key = (
            entry.get("board"),
            entry.get("task_id"),
            entry.get("chat_id"),
            entry.get("profile"),
        )
        eid = int(entry.get("event_id") or 0)
        cur = selected_terminal_by_sub.get(sub_key)
        if cur is None or eid < cur:
            selected_terminal_by_sub[sub_key] = eid

    # Advance non-terminals. A non-terminal is "safe" to consume iff it
    # sits strictly below the smallest undelivered terminal for the same
    # subscription. If the subscription has no undelivered terminal at
    # all (all events on this subscription are non-terminal), every
    # non-terminal is safe to consume and we advance immediately.
    post_terminal_non_terminal: list[dict] = []
    for cand in safe_non_terminal:
        sub_key = (
            cand.get("board"),
            cand.get("task_id"),
            cand.get("chat_id"),
            cand.get("profile"),
        )
        t_min = selected_terminal_by_sub.get(sub_key)
        if t_min is not None and int(cand.get("event_id") or 0) >= t_min:
            # A1: post-terminal non-terminal — do NOT advance.
            post_terminal_non_terminal.append(cand)
            continue
        # Pre-terminal non-terminal OR subscription has no undelivered
        # terminal at all: advance immediately.
        advanced = _advance_cursor_conn(
            board=cand.get("board"),
            task_id=str(cand.get("task_id") or ""),
            chat_id=str(cand.get("chat_id") or ""),
            thread_id=str(cand.get("thread_id") or ""),
            new_cursor=int(cand.get("event_id") or 0),
            profile_column=cand.get("profile_column"),
            profile_value=cand.get("profile"),
            conn=_get_board_conn(cand.get("board")),
        )
        # Finding 8: an archived/deleted terminal that just consumed its
        # cursor retires the subscription so it stops re-appearing in the
        # candidate scan. Best-effort — a schema without ``disabled_at``
        # fails open and the subscription simply stays visible with its
        # cursor already past the terminal (no further terminal to deliver).
        if advanced and cand.get("_retire"):
            _disable_subscription(
                _get_board_conn(cand.get("board")),
                cand.get("board"),
                str(cand.get("task_id") or ""),
                str(cand.get("chat_id") or ""),
                str(cand.get("thread_id") or ""),
                cand.get("profile_column"),
                cand.get("profile"),
                reason="terminal_"
                + (_event_kind(cand.get("event")).strip().lower() or "archived"),
                has_thread_id=bool(
                    ((state.get("schema_by_board") or {}).get(cand.get("board")) or {})
                    .get("has_thread_id", True)
                ),
            )

    if not dispatchable:
        return

    # Compute the minimum post-terminal non-terminal event_id per
    # subscription. Any dispatchable terminal whose event_id is AT OR
    # ABOVE this minimum must stay in the candidate pool for the next
    # iteration — delivering it now would either race past the
    # interleaved non-terminal (if we don't clamp the cursor) or
    # produce a duplicate delivery (if we clamp past the non-terminal
    # and re-dispatch on the next iteration). Filtering BEFORE delivery
    # is the correct invariant.
    min_nt_by_sub: dict[tuple, int] = {}
    for non_terminal in post_terminal_non_terminal:
        non_terminal_key = (
            non_terminal.get("board"),
            non_terminal.get("task_id"),
            non_terminal.get("chat_id"),
            non_terminal.get("profile"),
        )
        non_terminal_id = int(non_terminal.get("event_id") or 0)
        cur = min_nt_by_sub.get(non_terminal_key)
        if cur is None or non_terminal_id < cur:
            min_nt_by_sub[non_terminal_key] = non_terminal_id

    # Build terminal entries with task metadata. Bound metadata
    # reads to AT MOST _MAX_TASKS_PER_TURN tasks (the cap we are
    # about to evaluate) so a flood of pending terminals can't pin
    # SQLite reads against the budget.
    selected = dispatchable[:_MAX_TASKS_PER_TURN]
    # A1: drop any entry whose event_id is AT OR ABOVE the minimum
    # post-terminal non-terminal for its subscription. Those terminals
    # stay in the candidate pool and will be delivered naturally on the
    # next iteration, after the interleaved non-terminal has been
    # consumed. Filtering here — instead of clamping the cursor after
    # delivery — prevents the duplicate-dispatch regression where a
    # clamp past the non-terminal puts the delivered terminal back into
    # the candidate pool and gets it re-delivered next scan.
    selected = [
        entry
        for entry in selected
        if not (
            (
                k := (
                    entry.get("board"),
                    entry.get("task_id"),
                    entry.get("chat_id"),
                    entry.get("profile"),
                )
            )
            in min_nt_by_sub
            and int(entry.get("event_id") or 0) >= min_nt_by_sub[k]
        )
    ]
    if not selected:
        return

    # Change A (Finding 7): DB-backed consumer claim. Claim the rows we
    # are about to dispatch BEFORE building the prompt so a second WebUI
    # process sharing the same Kanban DB cannot dispatch the same
    # subscription. Claims are done per-board (each board has its own DB
    # file / connection) and fail OPEN when the Agent schema lacks the
    # claim columns (see ``_claim_candidates``).
    def _sub_event_key(entry: dict) -> tuple:
        return (
            entry.get("board"),
            entry.get("task_id"),
            entry.get("chat_id"),
            entry.get("thread_id") or "",
            int(entry.get("event_id") or 0),
        )

    selected_by_board: dict[Any, list[dict]] = {}
    for entry in selected:
        selected_by_board.setdefault(entry.get("board"), []).append(entry)
    claimed_keys: set[tuple] = set()
    for board_slug, board_selected in selected_by_board.items():
        _board_has_thread_id = bool(
            ((state.get("schema_by_board") or {}).get(board_slug) or {})
            .get("has_thread_id", True)
        )
        claimed = _claim_candidates(
            _get_board_conn(board_slug),
            board_slug,
            board_selected,
            _CONSUMER_ID,
            has_thread_id=_board_has_thread_id,
        )
        for entry in claimed:
            claimed_keys.add(_sub_event_key(entry))
    selected = [entry for entry in selected if _sub_event_key(entry) in claimed_keys]
    if not selected:
        return

    # Change A (Findings 2 + 5): delivery dedup against the shared
    # ``delivery_outbox`` table. Suppress any (subscription, event) already
    # delivered by THIS or ANOTHER process — a durable cross-process guard
    # against replay / duplicate wakeups. The matching ``delivery_id`` is
    # recorded only inside the accepted-dispatch cursor transaction below.
    def _delivery_id_for(entry: dict) -> str:
        return _make_delivery_id(
            str(entry.get("board") or ""),
            str(entry.get("task_id") or ""),
            str(entry.get("chat_id") or ""),
            str(entry.get("thread_id") or ""),
            int(entry.get("event_id") or 0),
        )

    selected = [
        entry
        for entry in selected
        if not _is_duplicate_delivery(
            _get_board_conn(entry.get("board")), _delivery_id_for(entry)
        )
    ]
    if not selected:
        return

    delivered_entries: list[dict] = []
    for entry in selected:
        full_entry = dict(entry)
        full_entry["task"] = _get_task(
            entry.get("board"),
            str(entry.get("task_id") or ""),
        )
        delivered_entries.append(full_entry)

    # Build the prompt. ``selected_count`` is the EXACT number of
    # entries that fit inside the 12_000-char budget (RFC §9). Entries
    # past ``selected_count`` (count overflow OR char-budget
    # truncation) stay readable for the next iteration.
    prompt, selected_count = _build_prompt(
        delivered_entries, total_pending=len(dispatchable)
    )

    # Truncate ``delivered_entries`` to ONLY the prefix actually
    # represented in the prompt — the bound on what we are about to
    # advance. This is the A1/A2 contract: every cursor advance
    # corresponds 1:1 to a terminal entry the agent saw in the prompt.
    delivered_entries = delivered_entries[:selected_count]

    # Close all cached Kanban connections BEFORE start_session_turn
    # runs (RFC §2 / §6 / §11 invariant).
    _close_board_conns()
    # FINDING 1 (addendum 4): dispatch inside the resolved profile's scope
    # so the turn is constructed against the correct profile.
    resp = _dispatch_in_profile(_dispatch, chat_id, prompt, resolved_profile)

    # A3: strict acceptance — real integer status in 200..299 AND a
    # non-empty stream_id. A 2xx response with a stream id means the agent
    # accepted and started the turn; other statuses are not accepted.
    # Change A (addendum 6 + Finding 5): also reject if this delivery was
    # already recorded in the shared ``delivery_outbox`` (belt-and-suspenders
    # against a concurrent duplicate). The board conns were closed before
    # dispatch, so reopen the delivered board's conn for the outbox read.
    _dedup_conn = (
        _get_board_conn(delivered_entries[0].get("board"))
        if delivered_entries
        else None
    )
    accepted, reason = _is_dispatch_accepted(
        resp,
        conn=_dedup_conn,
        delivery_id=(
            _delivery_id_for(delivered_entries[0]) if delivered_entries else None
        ),
    )
    if accepted:
        # A1 safe-adjacent advance: for each delivered terminal,
        # advance the cursor to the MIN of (max-non-terminal-id,
        # max-terminal-id-in-delivered-set) per subscription. This
        # consumes pre-terminal non-terminals that landed BELOW the
        # terminal AND any non-terminals that landed BELOW a later
        # terminal in the same delivery — but never races past an
        # undelivered terminal sitting between them.
        selected_max_by_sub: dict[tuple, int] = {}
        for entry in delivered_entries:
            sub_key = (
                entry.get("board"),
                entry.get("task_id"),
                entry.get("chat_id"),
                entry.get("thread_id") or "",
                entry.get("profile"),
            )
            eid = int(entry.get("event_id") or 0)
            cur = selected_max_by_sub.get(sub_key)
            if cur is None or eid > cur:
                selected_max_by_sub[sub_key] = eid
        # The min undelivered terminal (selected_terminal_by_sub)
        # already excludes anything we are delivering, so the safe
        # ceiling per subscription is the min of:
        #   * the delivered-set max
        #   * the smallest terminal NOT in the delivered set (post-delivery)
        #
        # FINDING 1: build the cursor advance plan FIRST so we have
        # the full set of (sub_key, ceiling) pairs ready to commit
        # only AFTER every attempt succeeds. ``dispatched_chats.append``
        # is held back until the post-attempt check confirms no
        # advance failed — the chat MUST stay in the candidate pool
        # for the next iteration if any sub failed so the failed
        # sub's event is re-delivered (partial-advance would otherwise
        # cause a duplicate wakeup where the agent receives the same
        # prompt twice — once now, once after backoff when the failed
        # sub's still-readable event re-enters the pool).
        advance_plan: list[tuple[tuple, int]] = []
        for sub_key, max_delivered in selected_max_by_sub.items():
            next_undelivered_terminal = None
            for t in dispatchable:
                tkey = (
                    t.get("board"),
                    t.get("task_id"),
                    t.get("chat_id"),
                    t.get("thread_id") or "",
                    t.get("profile"),
                )
                if tkey != sub_key:
                    continue
                teid = int(t.get("event_id") or 0)
                if teid not in (int(d.get("event_id") or 0) for d in delivered_entries):
                    if (
                        next_undelivered_terminal is None
                        or teid < next_undelivered_terminal
                    ):
                        next_undelivered_terminal = teid
            ceiling = max_delivered
            if next_undelivered_terminal is not None:
                # SQLite AUTOINCREMENT PRIMARY KEY guarantees event_id uniqueness
                # and strict monotonic increase, so subtracting 1 is safe.
                ceiling = min(ceiling, next_undelivered_terminal - 1)
            advance_plan.append((sub_key, ceiling))
        # FINDING 3 (P1): group the advance plan by board so each
        # board's cursor writes commit as a single transaction. The
        # connections are opened with ``isolation_level=None`` so
        # every individual UPDATE was previously auto-committed the
        # moment it ran — if the second UPDATE on the same board
        # failed, the first cursor advance was already durably
        # advanced and the chat was stuck in a partial-advance state
        # (the delivered event is past the cursor; the failed sub's
        # still-readable event would re-deliver and the agent would
        # see the original prompt twice). Wrap each board's writes
        # in BEGIN/COMMIT and ROLLBACK on any failure so partial
        # advances are reverted per-board. Cross-board isolation is
        # acceptable — SQLite can't do cross-file transactions and
        # each board has its own DB file anyway.
        advance_plan_by_board: dict[Any, list[tuple[tuple, int]]] = {}
        for sub_key, ceiling in advance_plan:
            advance_plan_by_board.setdefault(sub_key[0], []).append(
                (sub_key, ceiling)
            )
        # Finding 5: the delivered entries to record in ``delivery_outbox``,
        # grouped by board so each board's outbox INSERTs commit in the SAME
        # transaction as that board's cursor UPDATEs.
        delivered_by_board: dict[Any, list[dict]] = {}
        for entry in delivered_entries:
            delivered_by_board.setdefault(entry.get("board"), []).append(entry)
        # Attempt EVERY cursor advance, then decide based on the
        # aggregate result. ``dispatched_chats.append`` only runs in
        # the success branch below — when any single advance failed,
        # the chat stays in the candidate pool for the next iteration
        # so the failed sub's event replays (at-least-once) and a
        # chat backoff prevents an immediate retry that would
        # duplicate the wakeup the agent is already processing.
        any_advance_failed = False
        first_failed: tuple | None = None
        first_failed_event: int | None = None
        for board, plan in advance_plan_by_board.items():
            conn = _get_board_conn(board)
            if conn is None:
                # No connection for this board — every advance fails.
                for sub_key, ceiling in plan:
                    any_advance_failed = True
                    if first_failed is None:
                        first_failed = sub_key
                        first_failed_event = ceiling
                continue
            # Finding 5: make sure the shared delivery_outbox table exists on
            # this board's DB before we open the transaction (DDL outside the
            # explicit transaction avoids any BEGIN/CREATE interaction).
            _ensure_outbox_table(conn)
            # Begin a per-board transaction so every cursor write for
            # this board commits together (or rolls back together on
            # any per-row failure).
            try:
                conn.execute("BEGIN")
            except Exception:
                logger.debug(
                    "cursor transaction BEGIN failed for board=%r; "
                    "marking its plan entries as failed",
                    board,
                    exc_info=True,
                )
                for sub_key, ceiling in plan:
                    any_advance_failed = True
                    if first_failed is None:
                        first_failed = sub_key
                        first_failed_event = ceiling
                continue
            board_failed = False
            for sub_key, ceiling in plan:
                advanced = _advance_cursor_conn(
                    board=board,
                    task_id=str(sub_key[1] or ""),
                    chat_id=str(sub_key[2] or ""),
                    thread_id=str(sub_key[3] or ""),
                    new_cursor=ceiling,
                    profile_column=_find_profile_column_for_board(state, board),
                    profile_value=sub_key[4],
                    conn=conn,
                )
                if not advanced:
                    board_failed = True
                    if first_failed is None:
                        first_failed = sub_key
                        first_failed_event = ceiling
            if board_failed:
                # Per-board rollback so partial advances on this board
                # are reverted. The other boards (other DB files) keep
                # their own separate state.
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    logger.debug(
                        "cursor transaction ROLLBACK failed for board=%r",
                        board,
                        exc_info=True,
                    )
                any_advance_failed = True
                continue
            # Finding 5: record each delivered (subscription, event) in the
            # shared delivery_outbox INSIDE this board's cursor transaction so
            # the cursor advance and the dedup ledger commit atomically — a
            # crash / partial commit can neither duplicate nor lose accepted
            # work. INSERT OR IGNORE is a no-op if another process already
            # recorded the same delivery_id.
            try:
                for entry in delivered_by_board.get(board, []):
                    _record_delivery(
                        conn,
                        _delivery_id_for(entry),
                        board=board,
                        task_id=str(entry.get("task_id") or ""),
                        chat_id=str(entry.get("chat_id") or ""),
                        thread_id=str(entry.get("thread_id") or ""),
                        event_id=int(entry.get("event_id") or 0),
                        consumer_id=_CONSUMER_ID,
                    )
            except Exception:
                logger.warning(
                    "kanban notification: delivery_outbox INSERT failed for "
                    "board=%r; rolling back the cursor advance to preserve "
                    "at-least-once semantics",
                    board,
                    exc_info=True,
                )
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    logger.debug(
                        "cursor transaction ROLLBACK (after outbox INSERT "
                        "failure) failed for board=%r",
                        board,
                        exc_info=True,
                    )
                any_advance_failed = True
                if first_failed is None:
                    first_failed = plan[0][0]
                    first_failed_event = plan[0][1]
                continue
            try:
                conn.execute("COMMIT")
            except Exception:
                # Commit itself failed — roll back so the cursor
                # stays at its pre-transaction position.
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    logger.debug(
                        "cursor transaction ROLLBACK (after failed "
                        "COMMIT) failed for board=%r",
                        board,
                        exc_info=True,
                    )
                any_advance_failed = True
                # The first failure is already recorded above; record
                # this one too if no earlier failure was captured.
                if first_failed is None:
                    first_failed = plan[0][0]
                    first_failed_event = plan[0][1]
        if any_advance_failed:
            # Accepted dispatch but at least one cursor UPDATE failed to
            # persist (rowcount=0 — row missing / cursor already past /
            # contention). We must NOT mark this chat as dispatched;
            # the event stays readable so the next iteration replays it
            # (at-least-once). To prevent an immediate re-dispatch loop
            # the same agent would already be in the middle of handling,
            # enter the existing chat backoff window. We deliberately do
            # NOT pop the backoff entry here — failure persists.
            (fb_board, fb_task, fb_chat, fb_thread, fb_profile) = first_failed or (
                None, None, chat_id, "", None,
            )
            next_until = _bump_backoff(chat_backoff_state, chat_id)
            logger.warning(
                "kanban notification: accepted dispatch but cursor persist "
                "failed for chat_id=%r board=%r task=%r target_cursor=%r "
                "profile=%r; not advancing durable cursor; entering "
                "chat backoff (%.1fs) to preserve at-least-once",
                fb_chat,
                fb_board,
                fb_task,
                first_failed_event,
                fb_profile,
                max(0.0, next_until - _mono()),
            )
            return
        # All cursor advances succeeded — NOW (and only now) record
        # this chat as dispatched and clear the chat backoff entry.
        # Putting the ``append`` after the success check keeps the
        # invariant visible: a chat that did not durably advance all
        # of its subscription cursors must remain in the candidate
        # pool for the next iteration. Finding 5: the delivery_outbox
        # rows were already recorded INSIDE each board's cursor
        # transaction above, so an accepted-but-cursor-write-failed
        # path leaves the delivery unrecorded and at-least-once replay
        # is preserved.
        chat_backoff_state.pop(chat_id, None)
        dispatched_chats.append(chat_id)
        return

    # Failed dispatch — terminal stays readable, non-terminal stays
    # readable. Do NOT advance any cursor. Track this chat for backoff.
    if resp.get("error") == "process_wakeup_paused":
        next_until = _bump_backoff(chat_backoff_state, chat_id)
        logger.info(
            "kanban notification: provider pause suppressed wakeup for "
            "chat_id=%r; backing off until +%.1fs",
            chat_id,
            max(0.0, next_until - _mono()),
        )
        return
    # FINDING 5 (P2): ``resp.get("_status")`` can be a non-numeric
    # string (malformed dispatch response from a misbehaving provider).
    # ``int(...)`` raises ``ValueError`` on those, which previously
    # aborted the iteration and starved every other chat in the same
    # batch. Catch the ``ValueError`` and treat it as a rejected
    # dispatch so only the offending chat backs off; the rest of the
    # batch still gets a chance to dispatch.
    raw_status = resp.get("_status")
    try:
        status_code = int(raw_status) if raw_status is not None else 0
    except (TypeError, ValueError):
        status_code = 0
        logger.warning(
            "kanban notification: dispatch returned malformed _status=%r "
            "for chat_id=%r; treating as rejected dispatch and "
            "backing off this chat only",
            raw_status,
            chat_id,
        )
    if status_code == 409:
        # Active stream / busy: NO backoff (RFC §10 retry-after-idle).
        logger.debug(
            "kanban notification: chat_id=%r has an active stream; "
            "retry on next iteration (no backoff)",
            chat_id,
        )
        return
    next_until = _bump_backoff(chat_backoff_state, chat_id)
    logger.warning(
        "kanban notification: dispatch failed (%s) for chat_id=%r "
        "(error=%r); backing off until +%.1fs; terminal stays readable",
        reason,
        chat_id,
        resp.get("error"),
        max(0.0, next_until - _mono()),
    )


def _is_session_writable(session: Any) -> tuple[bool, str | None]:
    """Addendum 6: a resolved session must be WRITABLE before we start a
    server-initiated turn against it.

    A view-only session (read-only replica / shared observer) or an
    archived / ended session must never be mutated by a wakeup turn.
    Returns ``(writable, reason)`` — ``reason`` names why the session is
    not writable so the caller can quarantine with a distinct diagnostic.
    Attribute access is fully defensive: a session object that predates
    these fields is treated as writable (the historical behaviour).
    """
    if getattr(session, "view_only", False):
        return False, "view_only"
    if getattr(session, "archived", False) or getattr(session, "end_reason", None):
        return False, "archived_or_ended"
    return True, None


def _validate_chat_target(
    chat_id: str,
    notifier_profile: str | None = None,
) -> tuple[str, str | None]:
    """Resolve the session for ``chat_id`` once, returning one of
    ``("ok", profile)``, ``("missing", None)``, or ``("transient", None)``.

    Per-candidate profile validation is performed separately by
    ``_classify_candidate_target`` so this helper only needs the chat
    identity — the candidate group is irrelevant to session resolution.

    FINDING 1: ``notifier_profile`` (the subscription group's profile) is
    threaded into ``_get_session_for_target`` so a cold / inactive
    profile's session resolves under its own WebUI store instead of
    appearing "missing" and silently consuming the wakeup.
    """
    try:
        session = _get_session_for_target(
            chat_id, notifier_profile=notifier_profile
        )
    except _ProfileScopeUnavailable:
        # FINDING 4: the subscription's profile scope could not be entered.
        # Quarantine (consume the cursor, never dispatch) rather than defer
        # or fall back to an unscoped lookup that could wake the wrong
        # profile's session.
        logger.warning(
            "kanban notification: profile scope unavailable for chat_id=%r "
            "profile=%r; quarantining terminal event (cursor advanced, "
            "never dispatched through another profile's store)",
            chat_id,
            notifier_profile,
        )
        return "missing", None
    except KeyError:
        logger.warning(
            "kanban notification target session %r is missing; "
            "quarantining terminal event without waking any session",
            chat_id,
        )
        return "missing", None
    except Exception:
        logger.warning(
            "kanban notification get_session transient failure for %r; "
            "deferring until backoff expires",
            chat_id,
            exc_info=True,
        )
        return "transient", None
    # Addendum 6: reject view-only / archived / ended sessions. Mutating a
    # read-only or retired session with a wakeup turn is never correct, so
    # quarantine the candidate (consume its cursor, never dispatch) with a
    # distinct diagnostic — the same fail-closed treatment as a missing
    # session.
    writable, reason = _is_session_writable(session)
    if not writable:
        logger.warning(
            "kanban notification target session %r is not writable "
            "(reason=%s); quarantining terminal event without waking",
            chat_id,
            reason,
        )
        return "missing", None
    resolved_profile = getattr(session, "profile", None)
    return "ok", resolved_profile


def _classify_candidate_target(
    cand: dict,
    chat_status: str,
    resolved_profile: str | None,
) -> str:
    """Classify ONE candidate against the chat's session state.

    Returns ``"missing"``, ``"mismatch"``, or ``"ok"`` (the latter
    means the candidate is eligible for dispatch). Per-subscription
    profile check (RFC §8 + B): mismatching identities are
    quarantined independently, not as a chat-wide group.
    """
    if chat_status == "missing":
        return "missing"
    # ``chat_status == "transient"`` is unreachable: the chat loop
    # returns immediately after ``_validate_chat_target`` reports
    # transient, before any per-candidate classification. Asserting
    # here documents the invariant and surfaces a regression if a
    # future caller forgets to early-return.
    assert chat_status != "transient", (
        f"_classify_candidate_target received transient chat_status for "
        f"chat_id={cand.get('chat_id')!r}; the chat loop must defer "
        f"the entire chat on transient, not call this helper"
    )
    sub_profile = cand.get("profile")
    if sub_profile is None or str(sub_profile).strip() == "":
        # Addendum 2: a blank / legacy subscription profile is NOT a
        # wildcard. It may only authorise waking the ROOT / default
        # profile's session. If the resolved session belongs to a
        # NON-default profile, treat this as a mismatch and quarantine —
        # a blank row must never become a cross-profile routing
        # capability that lets an arbitrary chat_id be authority.
        resolved_norm = str(resolved_profile or "").strip().lower() or "default"
        if resolved_norm in ("", "default", "root"):
            return "ok"
        logger.error(
            "kanban notification profile mismatch for chat_id=%r task=%r "
            "(blank subscription_profile is only authoritative for the "
            "root/default profile, but session_profile=%r); quarantining "
            "this subscription only",
            cand.get("chat_id"),
            cand.get("task_id"),
            resolved_profile,
        )
        return "mismatch"
    if _profiles_match(sub_profile, resolved_profile):
        return "ok"
    logger.error(
        "kanban notification profile mismatch for chat_id=%r task=%r "
        "(subscription_profile=%r session_profile=%r); quarantining "
        "this subscription only — other matching subscriptions on the "
        "same chat still dispatch",
        cand.get("chat_id"),
        cand.get("task_id"),
        sub_profile,
        resolved_profile,
    )
    return "mismatch"


def _refresh_board_discovery(state: dict | None) -> float:
    """Re-discover boards, merge any newly-observed boards into ``state``
    with baseline=0 (RFC §6 consequence: late boards don't replay
    historical events because their recorded baseline is 0), and prime
    their per-board schema cache.

    Returns the ``time.time()`` stamp the caller should record as
    ``last_board_refresh`` (separating the cadence concern from the
    refresh logic keeps the watcher loop testable in isolation).

    Errors discovering boards are swallowed and logged at DEBUG so a
    transient Agent hiccup never kills the watcher.

    FINDING 8: boards in ``state["skip_boards"]`` (those whose
    schema check has failed enough consecutive times to be flagged as
    permanently broken) are filtered out of the merged board list
    here so the next ``_run_one_iteration`` doesn't immediately drop
    them again. A successful schema check in the iteration's own
    introspect loop clears the skip — so a repaired board is
    re-admitted by the NEXT refresh / iteration cycle without any
    manual ``state`` cleanup.

    FINDING 1 (P1): recovery path. Previously, a board that landed in
    ``skip_boards`` stayed there PERMANENTLY — the iteration path
    refused to introspect skipped boards AND the refresh path filtered
    them out, so there was no way for the watcher to ever clear the
    skip. After ``_SKIP_BOARD_RECHECK_SECONDS`` have elapsed since the
    board was added to ``skip_boards``, this helper re-adds the board
    to the candidate list AND clears the skip entry (the iteration's
    per-board introspection loop will then run the live schema check
    and re-populate ``skip_boards`` only if the schema is STILL
    invalid). The state dict tracks ``skip_boards_since`` (a
    ``{board: timestamp}`` map) so the cooldown is independent per
    board — a board that was just freshly skipped is NOT
    re-introspected, while a board skipped hours ago is.
    """
    now = time.time()
    try:
        boards = _discover_boards()
    except Exception:
        logger.debug("board refresh failed", exc_info=True)
        return now
    # Findings 3 + 6: collapse slugs that resolve to the same on-disk DB so
    # a duplicate/aliased board is not scanned twice per refresh.
    boards = _canonicalize_boards(boards)
    if state is None:
        return now
    skip_boards: set[str] = state.setdefault("skip_boards", set())
    skip_since: dict[str, float] = state.setdefault("skip_boards_since", {})
    # Recovery path: re-admit any skipped board whose cooldown has
    # elapsed so the iteration loop's per-board introspection can
    # re-check the schema. The skip entry is cleared here; a fresh
    # schema failure re-populates it (with a new timestamp) inside
    # ``_run_one_iteration``.
    if skip_boards:
        recoverable = {
            b
            for b in skip_boards
            if (now - float(skip_since.get(b, now))) >= _SKIP_BOARD_RECHECK_SECONDS
        }
        if recoverable:
            for b in recoverable:
                skip_boards.discard(b)
                skip_since.pop(b, None)
                logger.info(
                    "kanban notification watcher: re-checking previously "
                    "skipped board %r after %.0fs cooldown",
                    b,
                    _SKIP_BOARD_RECHECK_SECONDS,
                )
        # Boards still cooling down stay hidden from the active
        # candidate list (they would just be re-dropped by the
        # iteration's introspect loop anyway).
        active = [b for b in boards if b not in skip_boards]
        # Include cooling-down boards in state["boards"]? No — they
        # would still be filtered out by the iteration loop. Keeping
        # them out avoids log spam and wasted introspection round-trips.
        boards = active
    state["boards"] = boards
    baseline_map = state.setdefault("baseline", {})
    schema_by_board = state.setdefault("schema_by_board", {})
    for board in boards:
        if board not in baseline_map:
            baseline_map[board] = 0
        schema_by_board.setdefault(board, _inspect_subs_columns(board))
    return now


def _is_dispatch_accepted(
    resp: dict, conn: Any | None = None, delivery_id: str | None = None
) -> tuple[bool, str]:
    """A3 / Fix 1: a dispatch response is accepted iff BOTH:

      * ``_status`` is a real integer HTTP-like status in
        ``200..299``. Any missing key, zero, negative, non-integer
        (bool, str, float, None), or value outside that range is REJECTED —
        we will not advance the cursor when we cannot tell whether the agent
        accepted and started the turn. This avoids the bug where a missing
        ``_status`` (which previously fell back to ``0`` and was accepted as
        long as ``stream_id`` was present) silently accepted a half-formed
        response.
      * ``stream_id`` is a non-empty string (same as before).

    Production returns ``200`` and the test fixture returns ``200`` or
    ``201``; both pass.

    Addendum 7: an adapter response that carries an explicit truthy
    ``error`` payload is REJECTED even when it ALSO includes a ``stream_id``
    and/or a 2xx ``_status`` — a half-formed response like
    ``{"stream_id": "x", "error": "agent_unavailable"}`` must never be
    normalised to a success and advance the cursor.
    """
    if not isinstance(resp, dict):
        return False, "non-dict response"
    if resp.get("error"):
        return False, f"error={resp.get('error')!r}"
    raw = resp.get("_status")
    if raw is None:
        return False, "missing _status"
    # Reject bool (``True`` would otherwise parse as 1) and any
    # non-int subtype.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return False, f"non-integer _status={raw!r}"
    status_code = raw
    # Only a 2xx response means the agent accepted and started the turn.
    if status_code < 200 or status_code >= 300:
        return False, f"status={status_code}"
    stream_id = resp.get("stream_id")
    if not isinstance(stream_id, str) or not stream_id.strip():
        return False, f"missing stream_id (status={status_code})"
    # Change A (addendum 6 + Finding 5): even a well-formed 2xx response is
    # rejected if this delivery_id is already present in the shared
    # ``delivery_outbox`` — a concurrent consumer already delivered this
    # (subscription, event) and re-delivering would produce a duplicate agent
    # turn. A None conn (no board DB available) skips the check.
    if (
        conn is not None
        and delivery_id is not None
        and _is_duplicate_delivery(conn, delivery_id)
    ):
        return False, f"duplicate delivery_id={delivery_id}"
    return True, "ok"


def _find_profile_column_for_board(state: dict, board: str | None) -> str | None:
    """Look up the per-board schema's profile column choice. Falls back
    to the default schema's choice when the board is not present in
    ``schema_by_board`` (e.g. legacy markers or transient state)."""
    schema_by_board = state.get("schema_by_board") or {}
    schema = schema_by_board.get(board) or state.get("schema") or {}
    return schema.get("profile_column")


# ── Watcher loop + lifecycle (RFC §2) ─────────────────────────────────


def _watcher_loop() -> None:
    backoff = _BACKOFF_INITIAL_SECONDS
    state: dict | None = None
    last_board_refresh = 0.0
    # Finding 5: outbox pruning only reclaims rows older than 24h, so it does
    # not need to run every poll cycle — opening a connection per board every
    # second purely to prune would be wasteful. Prune hourly instead.
    last_outbox_prune = 0.0
    # C7: bounded reinitialization cadence. The watcher re-runs
    # ``_initialize_baseline_state`` at most every
    # ``_REINITIALIZE_RETRY_SECONDS`` while the state is fail-closed
    # (marker unreadable / schema invalid / write failed). When the
    # operator fixes the marker file, the schema, or the directory
    # permissions, the watcher resumes WITHOUT a process restart. A
    # deliberately-malformed marker file is NOT overwritten — the
    # fail-closed branch refuses to recreate the marker.
    last_reinit = 0.0
    try:
        # Initialize lazily so a missing kanban_db at startup (cut-down
        # test env, webui-only deploy, missing schema) does NOT crash the
        # server. The watcher logs the failure and stays idle until the
        # next iteration.
        try:
            boards = _discover_boards()
        except Exception:
            logger.debug("initial board discovery failed", exc_info=True)
            boards = []
        # Findings 3 + 6: dedup slugs that resolve to the same DB file.
        boards = _canonicalize_boards(boards)
        # Finding 9: stay dormant when no Kanban boards are reachable rather
        # than spinning the fail-closed init path (and its warnings) every
        # poll cycle. Log ONCE, then re-check discovery at a slow cadence
        # until a board appears or a stop is requested.
        if not boards:
            logger.info(
                "kanban notification watcher: no kanban boards found; "
                "watcher is dormant"
            )
            while not _STOP_EVENT.is_set():
                if _STOP_EVENT.wait(timeout=_DORMANT_POLL_SECONDS):
                    return
                try:
                    boards = _canonicalize_boards(_discover_boards())
                except Exception:
                    boards = []
                if boards:
                    logger.info(
                        "kanban notification watcher: kanban board(s) now "
                        "reachable; leaving dormant state"
                    )
                    break
            if _STOP_EVENT.is_set():
                return
        # The initial _initialize_baseline_state call lives OUTSIDE the
        # while-loop's ``except Exception`` handler. If it raises (a
        # corrupt but-not-empty marker that the helper itself somehow
        # can't tolerate, a programmer error in a downstream helper,
        # etc.) the thread would die before entering the recovery loop.
        # Catch unexpected exceptions here, downgrade to the same
        # fail-closed shape ``_initialize_baseline_state`` already
        # returns for marker/schema failures, and let the bounded
        # reinitialization cadence retry. KeyboardInterrupt and
        # SystemExit MUST propagate so a stop signal still kills the
        # thread promptly.
        try:
            state = _initialize_baseline_state(boards)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 — preserve stop signals above
            logger.warning(
                "kanban notification watcher: initial baseline init "
                "raised %r; entering fail-closed mode and retrying at "
                "bounded cadence",
                exc,
                exc_info=True,
            )
            state = _initial_init_failed_state(boards, exc)
        if state.get("marker_loaded") is False:
            logger.warning(
                "kanban notification watcher: baseline marker failed to "
                "persist; watcher is in fail-closed mode (no dispatch until "
                "the marker is writeable)"
            )
        elif state.get("schema_ok") is False:
            logger.warning(
                "kanban notification watcher: kanban_notify_subs is missing "
                "required columns; watcher is in fail-closed mode"
            )

        logger.info("kanban notification watcher started")

        while not _STOP_EVENT.is_set():
            try:
                # C7: reinitialize at a bounded cadence while the
                # watcher is fail-closed so the operator can recover
                # without restarting WebUI. The marker file (if corrupt)
                # is NEVER overwritten — ``_initialize_baseline_state``
                # only writes a new marker on the very first rollout.
                if state is not None and (
                    state.get("marker_loaded") is False
                    or state.get("schema_ok") is False
                ):
                    now = time.time()
                    if (now - last_reinit) >= _REINITIALIZE_RETRY_SECONDS:
                        last_reinit = now
                        try:
                            boards = _canonicalize_boards(_discover_boards())
                        except Exception:
                            boards = list(state.get("boards", []))
                        new_state = _initialize_baseline_state(boards)
                        if (
                            new_state.get("marker_loaded") is not False
                            and new_state.get("schema_ok") is not False
                        ):
                            logger.info(
                                "kanban notification watcher: recovered "
                                "from fail-closed state; dispatching resumed"
                            )
                            state = new_state
                # Refresh board discovery periodically so boards created
                # after WebUI starts become observable.
                now = time.time()
                if (now - last_board_refresh) >= _BOARD_DISCOVERY_REFRESH_SECONDS:
                    last_board_refresh = _refresh_board_discovery(state)
                if state is not None:
                    _run_one_iteration(state)
                # Finding 5: prune expired delivery_outbox rows periodically
                # (hourly) so the shared table can't grow without bound.
                now = time.time()
                if (now - last_outbox_prune) >= _OUTBOX_PRUNE_INTERVAL_SECONDS:
                    last_outbox_prune = now
                    _prune_all_outboxes(state)
                backoff = _BACKOFF_INITIAL_SECONDS  # reset after a clean pass
            except Exception:
                # Transient / unknown error. Bound the backoff so a persistent
                # problem can't burn CPU; never crash the watcher thread.
                logger.warning(
                    "kanban notification iteration failed; backing off %.2fs",
                    backoff,
                    exc_info=True,
                )
                if _STOP_EVENT.wait(backoff):
                    break
                backoff = min(backoff * 2.0, _BACKOFF_MAX_SECONDS)
                continue
            # Wait on the stop event so a clean shutdown returns promptly.
            if _STOP_EVENT.wait(_DEFAULT_POLL_INTERVAL_SECONDS):
                break
    finally:
        logger.info("kanban notification watcher stopped")


def start_kanban_notification_watcher() -> bool:
    """Start the watcher thread idempotently. Returns True on first start.

    Contract: ``server.main`` only needs to know whether a NEW thread
    actually started so it can print a single ``[ok]`` line. Subsequent
    calls (including from concurrent threads racing during a botched
    restart) return False without spawning a duplicate.

    Stop/start race serialization (M3 + reviewer follow-up): the entire
    check-clear-spawn sequence runs under ``_LIFECYCLE_LOCK``. A
    concurrent ``stop_kanban_notification_watcher`` holds the same lock
    for the full set-event / join / drop-reference sequence, so this
    method blocks until the previous thread has been joined and dropped
    before deciding whether to spawn a fresh one. That eliminates the
    race where a concurrent start cleared the stop event and spawned a
    new thread only for stop to set the event afterwards and kill the
    new thread.
    """
    global _WATCHER_THREAD
    with _LIFECYCLE_LOCK:
        if _WATCHER_THREAD is not None and _WATCHER_THREAD.is_alive():
            return False
        # The lock guarantees no concurrent stop is in flight. It is
        # safe to clear the stop event here: any earlier stop has
        # already joined and dropped the thread reference.
        _STOP_EVENT.clear()
        # Addendum 5: a fresh start re-opens the "new turns" gate that a
        # prior stop closed via ``_request_shutdown``.
        _NO_NEW_TURNS.clear()
        th = threading.Thread(
            target=_watcher_loop,
            name="hermes-webui-kanban-notification-watcher",
            daemon=True,
        )
        th.start()
        _WATCHER_THREAD = th
        return True


def stop_kanban_notification_watcher(timeout: float = 2.0) -> None:
    """Stop the watcher. Idempotent: safe to call when no thread is live.

    Mirrors ``api.background_process.stop_drain_thread`` so the existing
    serve_forever() finally block in server.py can join all three
    threads uniformly.

    Lifecycle lock (M3 + reviewer follow-up): ``start`` and ``stop``
    share ``_LIFECYCLE_LOCK`` for their entire mutation of the
    ``_WATCHER_THREAD`` / ``_STOP_EVENT`` state. ``stop`` does the
    set-event → drop-reference → join sequence INSIDE the lock so a
    concurrent ``start`` blocks until the old thread is joined (or
    the join times out). The watcher loop itself never acquires the
    lifecycle lock — only ``start`` and ``stop`` do — so join-under-lock
    cannot deadlock the watcher.
    """
    global _WATCHER_THREAD
    with _LIFECYCLE_LOCK:
        th = _WATCHER_THREAD
        if th is None or not th.is_alive():
            # Clean state — nothing to stop.
            _WATCHER_THREAD = None
            return
        # Addendum 5: close the "new turns" gate BEFORE joining so an
        # in-flight iteration cannot start a fresh session turn while the
        # join is pending — belt-and-suspenders alongside the thread join.
        _request_shutdown()
        _STOP_EVENT.set()
        th.join(timeout=timeout)
        if th.is_alive():
            # D: join timed out — RETAIN the still-live thread in
            # ``_WATCHER_THREAD`` so a concurrent ``start`` refuses to
            # spawn a duplicate daemon. The watcher loop observes the
            # stop event on its next iteration and exits; the slot
            # becomes free for a fresh start at that point.
            logger.warning(
                "kanban notification watcher join timed out after %.1fs; "
                "thread retained in _WATCHER_THREAD so a concurrent "
                "start cannot spawn a duplicate",
                timeout,
            )
            return
        # Clean exit — thread actually joined.
        _WATCHER_THREAD = None


def watcher_is_alive() -> bool:
    """Diagnostic accessor (no IO). True when the watcher thread is live."""
    th = _WATCHER_THREAD
    return th is not None and th.is_alive()


# ── Server-side wiring helpers (RFC §3) ─────────────────────────────────
# Server.py only needs a one-line start / stop wiring block; the try /
# except / print boilerplate lives here so the wiring can be inlined
# without bloating server.py's line count. The helpers do module-attribute
# lookups (``globals()["start_kanban_notification_watcher"]``) instead of
# local references captured at import time so the existing
# ``monkeypatch.setattr("api.kanban_notifications.start_kanban_notification_watcher", ...)``
# wiring tests still apply their fakes to the underlying functions.


def install_kanban_notification_watcher(verbose_print=print) -> bool:
    """Idempotently start the watcher with the standard success / warning
    announcements server.main() emits for its other lifecycle threads
    (``bg_task_complete`` drain, SessionChannel reaper).

    Mirrors the inline ``try / except / print`` block server.py used to
    inline:

        try:
            from api.kanban_notifications import start_kanban_notification_watcher
            if start_kanban_notification_watcher():
                print('[ok] Kanban notification watcher thread started', flush=True)
        except Exception as e:
            print(f'[!!] WARNING: Kanban notification watcher failed to start: {e}', flush=True)

    Returns ``True`` on first start, ``False`` on a duplicate-start no-op
    (matching ``start_kanban_notification_watcher``'s contract). Failures
    are logged via ``verbose_print`` (the default is the process-wide
    ``print``) so server.main() does not have to wrap the call in its own
    try / except.

    Test wiring: ``tests/test_server_kanban_notification_wiring.py``
    monkeypatches ``api.kanban_notifications.start_kanban_notification_watcher``
    to inject a fake. That fake is invoked via the module-attribute
    lookup the helper performs, so the existing assertions (success-line
    emission on first start, warning-line emission on failure, drain /
    reaper still running) keep working unchanged.
    """
    fn = globals().get("start_kanban_notification_watcher")
    if fn is None:
        # Defensive: never reached in practice (the module-level name is
        # always bound at import time), but keeps the helper self-contained
        # if a future test fully reloads the module without rebinding it.
        verbose_print(
            "[!!] WARNING: Kanban notification watcher start unavailable",
            flush=True,
        )
        return False
    try:
        if fn():
            verbose_print(
                "[ok] Kanban notification watcher thread started", flush=True
            )
            return True
    except Exception as e:
        verbose_print(
            f"[!!] WARNING: Kanban notification watcher failed to start: {e}",
            flush=True,
        )
    return False


def uninstall_kanban_notification_watcher() -> None:
    """Stop the watcher from server.main()'s ``serve_forever()`` ``finally``.

    Mirrors the inline ``try / except`` block server.py used to inline:

        try:
            from api.kanban_notifications import stop_kanban_notification_watcher
            stop_kanban_notification_watcher()
        except Exception:
            logger.debug("Failed to stop Kanban notification watcher during shutdown", exc_info=True)

    Failures are logged at DEBUG (not WARNING) because they are
    shutdown-time best-effort cleanup and the server is already tearing
    down. Like :func:`install_kanban_notification_watcher`, the helper
    resolves ``stop_kanban_notification_watcher`` via the module namespace
    so the wiring tests' monkeypatches still apply.
    """
    fn = globals().get("stop_kanban_notification_watcher")
    if fn is None:
        return
    try:
        fn()
    except Exception:
        logger.debug(
            "Failed to stop Kanban notification watcher during shutdown",
            exc_info=True,
        )
