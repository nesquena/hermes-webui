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

import json
import logging
import os
import tempfile
import threading
import time
import unicodedata
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
_WATCHER_STARTED_AT: float | None = None

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
_FIELD_TRUNCATE_CHARS = 600
# Bounded backoff ceiling for transient failures so a persistent error
# can't spin the thread hot while we still recover from real contention.
_BACKOFF_MAX_SECONDS = 10.0
_BACKOFF_INITIAL_SECONDS = 0.25
# Per-chat retry backoff for the dispatch state machine (RFC §10 / H3).
# Each chat_id that returns a non-2xx dispatch result backs off
# exponentially up to ``_CHAT_BACKOFF_MAX_SECONDS``. A successful dispatch
# (``status_code < 400``) resets the chat's failure state. The backoff is
# tracked PER chat so a single misbehaving session does not block the
# rest of the batch — every other chat_id is still scanned and dispatched
# on the same iteration.
_CHAT_BACKOFF_INITIAL_SECONDS = 1.0
_CHAT_BACKOFF_MAX_SECONDS = 60.0


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
        next_until = now_mono + _CHAT_BACKOFF_INITIAL_SECONDS
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

# ── Header (RFC §9 Prompt contract + §9 Prompt safety) ───────────────────
# The first line of every wake prompt. Critical invariant: it MUST be the
# first line so downstream agents see the [IMPORTANT: ...] marker before any
# untrusted task data, and the literal text MUST be unique so an attacker
# who controls a task title cannot forge a duplicate header. The test
# ``test_untrusted_title_cannot_forge_server_header`` enforces this.
_PROMPT_HEADER = (
    "[IMPORTANT: KANBAN WORKER UPDATE — server-generated, not a human message]"
)
_PROMPT_INTRO = "The following subscribed Kanban task(s) reached a terminal state:"
_PROMPT_FOOTER = (
    "Read the relevant task handoff with kanban_show if more context is "
    "needed, then continue the originating workflow. Do not ask the user "
    "to repeat work already present in the task handoff."
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

    Returns a context manager. Mirrors api/kanban_bridge._conn so a sqlite
    FDs don't pin stale -wal/-shm snapshots across the long-lived watcher.
    """
    kb = _kb()
    try:
        kb.init_db(board=board)
    except Exception:
        logger.debug("init_db failed for board=%s", board, exc_info=True)
    closing = getattr(kb, "connect_closing", None)
    if closing is not None:
        return closing(board=board)
    return kb.connect(board=board)


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

    Every step (mkdir, mkstemp, write, fsync, replace) is guarded. A
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
                except OSError:
                    pass
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
            rows = conn.execute(
                "SELECT task_id, chat_id, last_event_id, "
                f"{profile_select} AS profile_value "
                "FROM kanban_notify_subs "
                "WHERE platform = ?",
                ("webui",),
            ).fetchall()
            # Detect updated_at once per board so the legacy-no-updated_at
            # path uses the right SQL shape.
            schema = _inspect_subs_columns(board)
            has_updated_at = bool(schema.get("has_updated_at", True))
            for row in rows or []:
                try:
                    task_id = row["task_id"]
                    chat_id = row["chat_id"]
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
                        new_cursor=new_cursor,
                        profile_column=profile_column,
                        profile_value=profile_value,
                        has_updated_at=has_updated_at,
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

    return {
        "boards": boards_list,
        "baseline": baseline,
        "schema_ok": schema.get("required_ok", False),
        "schema": schema,
        "schema_by_board": schema_by_board,
        "marker_loaded": True,
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
    ``e.id > s.last_event_id`` in SQL so we never fetch rows we already
    consumed. The per-board schema is read from
    ``state["schema_by_board"][board]`` and selects the appropriate
    profile column (``notifier_profile`` vs legacy ``profile`` vs
    none). When ``conn`` is None a short-lived connection is opened
    internally (test path); production callers reuse one connection
    per board per iteration.
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
        f"       s.last_event_id AS s_last_event_id, "
        f"       {profile_select} AS s_profile, "
        f"       e.id AS e_id, "
        f"       e.task_id AS e_task_id, "
        f"       e.kind AS e_kind, "
        f"       e.payload AS e_payload "
        f"FROM kanban_notify_subs s "
        f"INNER JOIN task_events e ON e.task_id = s.task_id "
        f"WHERE s.platform = 'webui' "
        f"  AND e.id > s.last_event_id "
        f"  AND e.id > ? "
        f"ORDER BY e.id ASC, s.task_id ASC, s.chat_id ASC"
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
                    "profile": profile,
                    "profile_column": profile_col,
                    "last_event_id": last_event_id,
                    "event": ev,
                    "event_id": eid,
                    "board": board,
                }
            )

    try:
        if conn is not None:
            _scan(conn)
        else:
            with _open_conn(board) as local_conn:
                _scan(local_conn)
    except Exception:
        logger.debug("candidate scan failed for board=%s", board, exc_info=True)
    return out


# ── Terminal classification (RFC §7) ───────────────────────────────────


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
    """True if the event represents a task reaching ``done`` / ``blocked``.

    Accepts:
      * ``kind`` in ``{"completed", "complete", "done", "blocked"}``
      * ``payload.status`` in ``{"done", "blocked"}`` regardless of kind

    Malformed JSON payload can never crash the loop (best-effort parse).
    """
    kind = ""
    try:
        kind = str(event_row["kind"] or "").strip().lower()
    except (KeyError, TypeError):
        try:
            kind = str(event_row[3] or "").strip().lower()
        except Exception:
            kind = ""
    if kind in _TERMINAL_KINDS:
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
    new_cursor: int,
    profile_column: str | None,
    profile_value: str | None = None,
    has_updated_at: bool = True,
) -> int:
    """Monotonic conditional UPDATE. Returns rowcount (0 when the row's
    cursor already advanced past new_cursor or the row was deleted).

    RFC §5: a legacy Kanban DB whose ``kanban_notify_subs`` table has
    only the four required columns (``task_id``, ``platform``,
    ``chat_id``, ``last_event_id``) and NO ``updated_at`` would see every
    cursor UPDATE fail at parse time. Pass ``has_updated_at=False``
    for those legacy boards and the helper omits the column from the
    SET clause. Default is True so the modern path stays unchanged.
    """
    if has_updated_at:
        sql = (
            "UPDATE kanban_notify_subs "
            "SET last_event_id = ?, updated_at = ? "
            "WHERE task_id = ? "
            "  AND platform = 'webui' "
            "  AND chat_id = ? "
            "  AND last_event_id < ?"
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
            "  AND last_event_id < ?"
        )
        params = [
            int(new_cursor),
            task_id,
            chat_id,
            int(new_cursor),
        ]
    if profile_column == "notifier_profile" and profile_value is not None:
        sql += " AND notifier_profile = ?"
        params.append(profile_value)
    elif profile_column == "profile" and profile_value is not None:
        sql += " AND profile = ?"
        params.append(profile_value)
    cur = conn.execute(sql, params)
    return int(getattr(cur, "rowcount", 0) or 0)


def _advance_cursor(
    *,
    board: str | None,
    task_id: str,
    chat_id: str,
    new_cursor: int,
    profile_column: str | None,
    profile_value: str | None,
    has_updated_at: bool = True,
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
                new_cursor=new_cursor,
                profile_column=profile_column,
                profile_value=profile_value,
                has_updated_at=has_updated_at,
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


# ── Target validation (RFC §8) ────────────────────────────────────────


def _get_session_for_target(sid: str) -> Any:
    """Lazy wrapper around api.models.get_session. Import is deferred so
    test doubles that don't need real session load can override the symbol
    via monkeypatch without forcing an api.models import.

    Resolution order:
      1. Module-level ``get_session`` attribute (test doubles monkeypatch here).
      2. ``api.models.get_session`` import (production).
      3. Raise ``KeyError`` so the caller treats the chat_id as missing.
    """
    fn = globals().get("get_session")
    if fn is None:
        try:
            from api.models import get_session as _real_get_session  # type: ignore
        except Exception as exc:
            # Distinguish a missing dependency (KeyError path: "session is
            # genuinely absent") from an unexpected import failure (raise
            # the original so the caller logs WARNING, not DEBUG).
            raise RuntimeError(
                f"api.models.get_session import failed: {exc!r}"
            ) from exc
        # Cache the resolution for subsequent calls (still mutable via
        # monkeypatch.setattr(mod, "get_session", ...) for tests).
        globals()["get_session"] = _real_get_session
        fn = _real_get_session
    return fn(sid)


def _validate_target(
    chat_id: str,
    sub_profile: str | None,
) -> tuple[str, str | None]:
    """Return ``(status, resolved_profile)``.

    ``status`` is one of:
      * ``"ok"`` — session exists; ``resolved_profile`` is the persisted
        session profile (which the prompt may use for display).
      * ``"missing"`` — chat_id has no session record (KeyError from
        ``get_session``); quarantine the candidate, log a warning WITHOUT
        prompt content, advance cursor.
      * ``"mismatch"`` — positive subscription/profile disagreement; fail
        closed, log error WITHOUT prompt content, advance cursor.
      * ``"transient"`` — ``get_session`` raised a non-KeyError exception
        (H4). The candidate is NOT consumed: cursors stay untouched so the
        next iteration can re-read the events, and a bounded per-chat
        backoff is applied so a persistent I/O error doesn't burn CPU.

    Note (B): the ``profile_column`` parameter is intentionally absent.
    The session's persisted profile is the canonical answer to "does this
    chat match this subscription?". The subscription row's profile COLUMN
    name (``notifier_profile`` vs legacy ``profile``) is already chosen
    per-candidate in the candidate-rows scan and applied independently
    during the cursor UPDATE; we never need to feed it back into session
    validation.
    """
    try:
        session = _get_session_for_target(chat_id)
    except KeyError:
        logger.warning(
            "kanban notification target session %r is missing; "
            "quarantining terminal event without waking any session",
            chat_id,
        )
        return "missing", None
    except Exception:
        # H4: a transient get_session exception (SESSIONS table I/O error,
        # full disk, locked DB, in-progress migration, etc.) is NOT the
        # same as a genuinely absent session. We log WARNING so the
        # operator sees it, but do NOT consume/quarantine — the events
        # stay readable for the next iteration after the backoff expires.
        logger.warning(
            "kanban notification get_session transient failure for %r; "
            "deferring until backoff expires",
            chat_id,
            exc_info=True,
        )
        return "transient", None
    try:
        resolved_profile = getattr(session, "profile", None)
    except Exception:
        resolved_profile = None

    if sub_profile is None or str(sub_profile).strip() == "":
        # Legacy row — no profile discriminator. Trust the resolved session's
        # persisted profile (RFC §8 step 5).
        return "ok", resolved_profile

    # Positive check: subscription profile must match the session's profile,
    # using the alias-aware matcher (RFC §8 step 3: 'default' and root alias
    # must compare identically to the rest of WebUI).
    if not _profiles_match(sub_profile, resolved_profile):
        logger.error(
            "kanban notification profile mismatch for chat_id=%r "
            "(subscription_profile=%r session_profile=%r); "
            "quarantining terminal event without waking any session",
            chat_id,
            sub_profile,
            resolved_profile,
        )
        return "mismatch", resolved_profile
    return "ok", resolved_profile


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


def _normalize_field(text: Any, *, limit: int = _FIELD_TRUNCATE_CHARS) -> str:
    """Make untrusted DB text safe to embed in a single-line prompt.

    The function is applied to EVERY untrusted value that ends up in the
    wake prompt — free-text task fields (``title``/``summary``/``result``/
    ``block_reason``) AND structural identifiers (``task_id``/``board``/
    ``status``) that get wrapped in Markdown backticks — so a single
    sanitizer controls every prompt character regardless of which
    Kanban row it came from.

    Defences applied in order:

    * Strip every Unicode control / format / bidi-isolate character
      (``unicodedata.category(ch)[0] in {"C", ""}``). This removes C0/C1
      controls, DEL, U+200E/F (LRM/RLM), U+202A–U+202E (bidi embedding /
      override / isolate), U+2066–U+2069 (bidi isolates), tag chars, and
      variation selectors so a malicious author cannot hide instructions
      from human readers while exposing them to the model. Cf chars are
      always rendered as a single space.
    * Replace ASCII brackets ``[`` and ``]`` with their fullwidth forms
      ``〔``/``〕`` so the text cannot close or reopen the
      ``[IMPORTANT: ...]`` envelope or smuggle bracketed pseudo-
      instructions. Markdown backticks are also replaced with the
      modifier letter grave accent U+02CB so a malicious ``task_id``
      containing a backtick cannot break out of the backtick-delimited
      run used for the ``Board`` / ``task_id`` / ``status`` fields.
    * Collapse whitespace and truncate to ``limit`` with a visible
      ``…(truncated)`` marker so a single oversized field cannot blow
      past the global prompt budget.
    """
    if text is None:
        return ""
    s = str(text)
    out = []
    for ch in s:
        cp = ord(ch)
        # C0 controls, DEL, Unicode line/paragraph separators.
        if cp < 0x20 or cp == 0x7F or cp in (0x2028, 0x2029):
            out.append(" ")
            continue
        # Every other Unicode control / format / private-use / surrogate /
        # unassigned character — including bidi LRM/RLM, embedding /
        # override / isolate controls, tag chars, and variation selectors.
        cat = unicodedata.category(ch)
        if cat and cat[0] == "C":
            out.append(" ")
            continue
        out.append(ch)
    s = "".join(out)
    # Escape structural delimiters (brackets + backticks) so the untrusted
    # text cannot impersonate the server header, close/reopen the
    # ``[IMPORTANT: ...]`` envelope, or break out of the backtick-delimited
    # ``Board`` / ``task_id`` / ``status`` Markdown runs.
    s = s.replace("[", "〔").replace("]", "〕").replace("`", "ˋ")
    # Collapse runs of whitespace from the control-strip step.
    s = " ".join(s.split())
    if len(s) > limit:
        s = s[:limit] + "…(truncated)"
    return s


def _format_entry(
    board: str | None, task_id: str, event_id: int, task_row: dict | None
) -> str:
    """Format one bulleted entry. ``task_row`` may be ``None`` when the
    authoritative task read failed; the entry still references board/task/
    event so the agent can recover via kanban_show.

    Every value interpolated into the prompt — including the
    backtick-delimited ``board``/``task_id``/``status`` identifiers — is
    passed through ``_normalize_field`` so a malicious task_id cannot
    break out of the backtick-delimited run via an embedded `` ` ``,
    newline, or bidi override.
    """
    title = _normalize_field((task_row or {}).get("title") or "(untitled)")
    status = _normalize_field((task_row or {}).get("status") or "")
    summary = _normalize_field((task_row or {}).get("summary") or "")
    result = _normalize_field((task_row or {}).get("result") or "")
    reason = _normalize_field((task_row or {}).get("block_reason") or "")
    board_label = _normalize_field(board or "(unknown)")
    task_id_norm = _normalize_field(task_id or "")
    parts = [
        f"- Board `{board_label}` · `{task_id_norm}` · {title} · status `{status}`"
    ]
    if summary:
        parts.append(f"  Summary: {summary}")
    if result:
        parts.append(f"  Result: {result}")
    if reason:
        parts.append(f"  Blocker: {reason}")
    parts.append(f"  Delivery event: {event_id}")
    return "\n".join(parts)


def _build_prompt(
    entries: list[dict], *, total_pending: int | None = None
) -> tuple[str, int]:
    """Assemble the wake prompt with header + bounded body.

    Returns ``(prompt, selected_count)`` where ``selected_count`` is
    the EXACT number of entries from ``entries`` that are actually
    represented in the prompt body (counting BOTH the 20-entry cap
    AND the char-budget truncation, whichever hits first). Entries
    past ``selected_count`` stay readable for the next iteration —
    the caller advances cursors ONLY for the entries in the
    represented prefix.

    ``total_pending`` may exceed ``len(entries)`` because callers bound
    task-metadata reads to the first 20 candidates. The overflow annotation
    uses that total, so it reports both unread candidates and char-budget
    omissions accurately.
    The annotation line is reserved against the 12_000-char budget
    BEFORE construction so the final prompt length is always
    <= ``_MAX_PROMPT_CHARS`` without relying on a runtime assert.
    """
    pending_count = max(len(entries), int(total_pending or 0))
    selected: list[str] = []
    header = _PROMPT_HEADER + "\n\n" + _PROMPT_INTRO + "\n\n"
    # Fixed-cost budget: header + footer + 2 trailing newlines + the
    # overflow annotation line (if any). Reserve ALL of this upfront
    # so the body never overflows the budget.
    overflow_annotation = ""
    if pending_count > 0:
        # We will only show the annotation when we truncate, but its
        # worst-case length is bounded by ``pending_count``. Reserve the
        # worst case so the budget arithmetic stays conservative.
        worst_case_overflow = (
            f"- …({pending_count} additional update(s) pending, "
            "delivered on the next wake turn)"
        )
        fixed_cost = (
            len(header)
            + len(_PROMPT_FOOTER)
            + 2  # trailing "\n\n"
            + len(worst_case_overflow)
            + 1  # +1 newline before annotation
        )
    else:
        fixed_cost = len(header) + len(_PROMPT_FOOTER) + 2
    body_chars = fixed_cost
    for entry in entries:
        if len(selected) >= _MAX_TASKS_PER_TURN:
            break
        line = _format_entry(
            entry.get("board"),
            entry.get("task_id", ""),
            int(entry.get("event_id") or 0),
            entry.get("task"),
        )
        # Reserve one newline between entries.
        line_with_sep = line + "\n"
        if (body_chars + len(line_with_sep)) > _MAX_PROMPT_CHARS:
            # Even one more entry won't fit — leave the rest for the
            # next serialized wake turn (RFC §9 leave overflow
            # unconsumed).
            break
        selected.append(line)
        body_chars += len(line_with_sep)
    truncated = pending_count - len(selected)
    if truncated > 0 and selected:
        overflow_annotation = (
            f"- …({truncated} additional update(s) pending, "
            "delivered on the next wake turn)"
        )
    body = "\n".join(selected)
    if body:
        body += "\n"
    if overflow_annotation:
        body += overflow_annotation + "\n"
    body += "\n"
    prompt = header + body + _PROMPT_FOOTER
    # The reservation math guarantees the final length is within budget.
    # Fail closed if a future formatter change violates that invariant.
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise ValueError(
            f"_build_prompt exceeded budget: {len(prompt)} > {_MAX_PROMPT_CHARS}"
        )
    return prompt, len(selected)


# ── Dispatch (RFC §10) ────────────────────────────────────────────────


def _dispatch(chat_id: str, prompt: str) -> dict:
    """Call routes.start_session_turn. Lazy-imported so test doubles can
    monkeypatch the module-level ``start_session_turn`` symbol before the
    watcher runs. Resolution order:
      1. Module-level ``start_session_turn`` attribute (test doubles).
      2. ``api.routes.start_session_turn`` import (production).
      3. Return ``{"_status": 500, ...}`` so the dispatch state machine
         treats the failure like any other transient 5xx.
    """
    fn = globals().get("start_session_turn")
    if fn is None:
        try:
            from api.routes import start_session_turn as _real  # type: ignore
        except Exception:
            logger.warning(
                "kanban notification start_session_turn import failed; "
                "this WebUI build cannot dispatch wakeups"
            )
            return {"_status": 500, "error": "start_session_turn unavailable"}
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


# ── Single iteration (RFC §10 / §11 / §9) ─────────────────────────────


def _read_task(conn, task_id: str) -> dict | None:
    """Read the authoritative task fields the prompt needs. Missing
    optional columns are omitted; raw worker logs and secrets are never
    included."""
    try:
        row = conn.execute(
            "SELECT status, summary, result, block_reason, title "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except Exception:
        # Schema may not have every column (legacy builds). Try the lean
        # fallback that only reads title + status.
        try:
            row = conn.execute(
                "SELECT status, title FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                status = row["status"]
                title = row["title"]
            except (KeyError, TypeError):
                status, title = row[0], row[1]
            return {"status": status, "title": title}
        except Exception:
            logger.debug("lean task read also failed for %s", task_id, exc_info=True)
            return None
    if row is None:
        return None
    out: dict[str, Any] = {}
    try:
        out["status"] = row["status"]
    except (KeyError, TypeError):
        try:
            out["status"] = row[0]
        except Exception:
            pass
    for col, key in (
        ("summary", "summary"),
        ("result", "result"),
        ("block_reason", "block_reason"),
        ("title", "title"),
    ):
        try:
            out[key] = row[col]
        except (KeyError, TypeError):
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
    boards = state.get("boards") or []
    schema_by_board = state.setdefault("schema_by_board", {})
    valid_boards: list[str] = []
    invalid_boards: list[str] = []
    for board in boards:
        live = _inspect_subs_columns(board)
        schema_by_board[board] = live
        if live.get("required_ok", False):
            valid_boards.append(board)
        else:
            missing = [c for c in _REQUIRED_COLUMNS if c not in live.get("columns", [])]
            invalid_boards.append(board)
            logger.warning(
                "kanban_notify_subs on board %r is missing required columns %s; "
                "skipping this board until the schema is restored",
                board,
                missing,
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

    def _advance_cursor_conn(
        board: str | None,
        task_id: str,
        chat_id: str,
        new_cursor: int,
        profile_column: str | None,
        profile_value: str | None,
        conn: Any | None,
    ) -> bool:
        """Cursor advance that reuses ``conn`` when available. Falls back
        to opening a per-call connection (the original behaviour) when
        the caller has no cached connection (e.g. the watcher's startup
        bootstrap). ``has_updated_at`` is resolved from the per-board
        schema so a legacy ``kanban_notify_subs`` without that column
        does not crash the iteration (Fix 3)."""
        has_updated_at = _has_updated_at(board)
        if conn is None:
            return _advance_cursor(
                board=board,
                task_id=task_id,
                chat_id=chat_id,
                new_cursor=new_cursor,
                profile_column=profile_column,
                profile_value=profile_value,
                has_updated_at=has_updated_at,
            )
        try:
            rowcount = _update_cursor_row(
                conn,
                task_id=task_id,
                chat_id=chat_id,
                new_cursor=new_cursor,
                profile_column=profile_column,
                profile_value=profile_value,
                has_updated_at=has_updated_at,
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
    candidates: list[dict] = []
    for board in boards:
        conn = _get_board_conn(board)
        try:
            candidates.extend(_candidate_rows(board, state, conn=conn))
        except Exception:
            logger.debug("candidate scan failed for board=%s", board, exc_info=True)

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
                _validate_target,
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
    _validate_target,
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
    target_status, resolved_profile = _validate_chat_target(chat_id, group)
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
            new_cursor=int(cand.get("event_id") or 0),
            profile_column=cand.get("profile_column"),
            profile_value=cand.get("profile"),
            conn=_get_board_conn(cand.get("board")),
        )
    if target_status in ("missing", "mismatch"):
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
        _advance_cursor_conn(
            board=cand.get("board"),
            task_id=str(cand.get("task_id") or ""),
            chat_id=str(cand.get("chat_id") or ""),
            new_cursor=int(cand.get("event_id") or 0),
            profile_column=cand.get("profile_column"),
            profile_value=cand.get("profile"),
            conn=_get_board_conn(cand.get("board")),
        )

    if not dispatchable:
        return

    # Build terminal entries with task metadata. Bound metadata
    # reads to AT MOST _MAX_TASKS_PER_TURN tasks (the cap we are
    # about to evaluate) so a flood of pending terminals can't pin
    # SQLite reads against the budget.
    selected = dispatchable[:_MAX_TASKS_PER_TURN]
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
    resp = _dispatch(chat_id, prompt)

    # A3: strict acceptance — real integer status in 100..399 AND a
    # non-empty stream_id. A 201 with stream id is accepted; a
    # 2xx/3xx without a stream id is NOT accepted (we don't know
    # whether the agent actually started).
    accepted, reason = _is_dispatch_accepted(resp)
    if accepted:
        chat_backoff_state.pop(chat_id, None)
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
        for sub_key, max_delivered in selected_max_by_sub.items():
            next_undelivered_terminal = None
            for t in dispatchable:
                tkey = (
                    t.get("board"),
                    t.get("task_id"),
                    t.get("chat_id"),
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
                ceiling = min(ceiling, next_undelivered_terminal - 1)
            _advance_cursor_conn(
                board=sub_key[0],
                task_id=str(sub_key[1] or ""),
                chat_id=str(sub_key[2] or ""),
                new_cursor=ceiling,
                profile_column=_find_profile_column_for_board(state, sub_key[0]),
                profile_value=sub_key[3],
                conn=_get_board_conn(sub_key[0]),
            )
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
    status_code = int(resp.get("_status") or 0)
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


def _validate_chat_target(
    chat_id: str,
    group: list[dict],
) -> tuple[str, str | None]:
    """Resolve the session for ``chat_id`` once, returning one of
    ``("ok", profile)``, ``("missing", None)``, or ``("transient", None)``.

    ``group`` is unused here; it is included so callers can pass the
    chat group context. Per-candidate profile validation is performed
    separately by ``_classify_candidate_target``.
    """
    try:
        session = _get_session_for_target(chat_id)
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
    if chat_status == "transient":
        # Caller (the chat loop) defers the entire chat before we
        # get here, so this branch is defensive only.
        return "ok"
    sub_profile = cand.get("profile")
    if sub_profile is None or str(sub_profile).strip() == "":
        # Legacy row with no profile column value. RFC §8 step 5:
        # trust the resolved session's persisted profile.
        return "ok"
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


def _is_dispatch_accepted(resp: dict) -> tuple[bool, str]:
    """A3 / Fix 2: a dispatch response is accepted iff BOTH:

      * ``_status`` is a real integer HTTP-like status in
        ``100..399`` (informational, success, or redirection). Any
        missing key, zero, negative, non-integer (bool, str, float,
        None), or value ``>= 400`` is REJECTED — we will not advance
        the cursor when we cannot tell whether the agent actually
        started. This avoids the bug where a missing ``_status`` (which
        previously fell back to ``0`` and was accepted as long as
        ``stream_id`` was present) silently accepted a half-formed
        response.
      * ``stream_id`` is a non-empty string (same as before).

    Production returns ``200`` and the test fixture returns ``200`` or
    ``201``; both pass.
    """
    raw = resp.get("_status")
    if raw is None:
        return False, "missing _status"
    # Reject bool (``True`` would otherwise parse as 1) and any
    # non-int subtype.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return False, f"non-integer _status={raw!r}"
    status_code = raw
    if status_code < 100 or status_code >= 400:
        return False, f"status={status_code}"
    stream_id = resp.get("stream_id")
    if not isinstance(stream_id, str) or not stream_id.strip():
        return False, f"missing stream_id (status={status_code})"
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
    global _WATCHER_STARTED_AT
    _WATCHER_STARTED_AT = time.time()
    backoff = _BACKOFF_INITIAL_SECONDS
    state: dict | None = None
    last_board_refresh = 0.0
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
        state = _initialize_baseline_state(boards)
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
                            boards = _discover_boards()
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
                    try:
                        boards = _discover_boards()
                    except Exception:
                        logger.debug("board refresh failed", exc_info=True)
                    else:
                        if state is not None:
                            state["boards"] = boards
                            baseline_map = state.setdefault("baseline", {})
                            schema_by_board = state.setdefault("schema_by_board", {})
                            for board in boards:
                                if board not in baseline_map:
                                    baseline_map[board] = 0
                                schema_by_board.setdefault(
                                    board, _inspect_subs_columns(board)
                                )
                    last_board_refresh = now
                if state is not None:
                    _run_one_iteration(state)
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
