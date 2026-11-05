"""#7882 cap follow-up: the eager-checkpoint fact must travel the REAL producer path.

The sidebar overlay's capped-out branch cannot re-walk the transcript, so the
only trustworthy evidence that the CURRENT pending hidden wakeup already has its
(excluded) sidecar row is a fact written next to that row. An earlier revision
read it from ``session['messages']``, a key no real sidebar row ever carries: the
overlay pops it and ``_strip_sidebar_heavy_metadata`` drops it from the index
snapshot, and no producer (``Session.compact()`` or the saved sidecar index)
emits it. Those tests supplied a transcript-shaped dict directly and bypassed
the failing ingress.

These tests drive the production ingress end to end and mock none of it:

    routes._checkpoint_user_message_for_eager_session_save   (the producer)
      -> Session.save                       (sidecar + _index.json)
      -> all_sessions(sidebar_metadata_only=True)
      -> _apply_sidebar_state_db_override_metadata            (the overlay)
      -> GET /api/sessions via routes.handle_get              (the public poll)

with a REAL sqlite state.db, four pinned fillers consuming the top-N reconcile
window, and the target row capped out — the schedule the maintainer replayed.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import OrderedDict
from io import BytesIO
from urllib.parse import urlparse

import pytest

import api.models as models

CAP = 2
PINNED_FILLERS = 4
TARGET = "CHECKPOINTED"


# --- the public GET handler -------------------------------------------------------


class _GetHandler:
    """Minimal BaseHTTPRequestHandler stand-in (as used by the sibling suites)."""

    def __init__(self, path):
        self.path = path
        self.headers = {}
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.wfile = BytesIO()
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def log_message(self, *args, **kwargs):
        pass

    @property
    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


# --- store + state.db fixtures ----------------------------------------------------


def _install_store(monkeypatch, tmp_path):
    """Point every layer of the real read/write path at a throwaway store."""
    import api.config as config
    import api.profiles as profiles
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    index_file = session_dir / "_index.json"
    for module in (config, models):
        monkeypatch.setattr(module, "STATE_DIR", tmp_path, raising=False)
        monkeypatch.setattr(module, "SESSION_DIR", session_dir, raising=False)
        monkeypatch.setattr(module, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
    return session_dir


def _state_db(tmp_path, rows_by_sid):
    """Create state.db with a ``sessions`` row + message rows per session."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
        "model TEXT, started_at REAL, message_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, "
        "tool_name TEXT)"
    )
    for sid, rows in rows_by_sid.items():
        conn.execute(
            "INSERT INTO sessions (id, source, title, model, started_at, message_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, "webui", "Session", "test-model", 1000.0, len(rows)),
        )
        for row in rows:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (sid, row["role"], row.get("content", ""), float(row["timestamp"])),
            )
    conn.commit()
    conn.close()
    return path


def _state_db_message_count(tmp_path, sid):
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        row = conn.execute(
            "SELECT message_count FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def _append_state_db_rows(tmp_path, sid, rows):
    """Simulate the installed Agent core appending rows straight to state.db."""
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        for row in rows:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (sid, row["role"], row.get("content", ""), float(row["timestamp"])),
            )
        conn.execute(
            "UPDATE sessions SET message_count = "
            "(SELECT COUNT(*) FROM messages WHERE session_id = ?) WHERE id = ?",
            (sid, sid),
        )
        conn.commit()
    finally:
        conn.close()


def _new_session(sid, *, messages, created_at, updated_at, title=None, pinned=False):
    return models.Session(
        session_id=sid,
        title=title or f"Session {sid}",
        workspace="/tmp",
        model="test-model",
        messages=messages,
        created_at=created_at,
        updated_at=updated_at,
        pinned=pinned,
    )


def _seed_store(monkeypatch, tmp_path, *, now):
    """Four pinned fillers own the reconcile window; the target is capped out.

    Every row is written by the real ``Session.save`` (sidecar + index) and has
    matching rows in a real state.db, so the overlay reads what production reads.
    """
    _install_store(monkeypatch, tmp_path)
    fillers = []
    state_rows = {}
    for index in range(PINNED_FILLERS):
        sid = f"fmt{index}"
        rows = [
            {"role": "user", "content": f"filler {index} q", "timestamp": now - 900 - index},
            {"role": "assistant", "content": f"filler {index} a", "timestamp": now - 899 - index},
        ]
        session = _new_session(
            sid, messages=rows, created_at=now - 900 - index,
            updated_at=now - 899 - index, pinned=True,
        )
        session.save(touch_updated_at=False)
        fillers.append(session)
        state_rows[sid] = rows
    return fillers, state_rows


def _public_poll(sid):
    """One real GET /api/sessions; returns the public row for *sid*."""
    import api.routes as routes

    routes._clear_session_list_cache()
    handler = _GetHandler("/api/sessions?sidebar_source=webui")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200, f"GET /api/sessions returned {handler.status}"
    rows = [
        row for row in handler.response_json["sessions"]
        if row.get("session_id") == sid
    ]
    assert rows, f"{sid} is missing from the public /api/sessions payload"
    return rows[0]


def _eager_checkpoint(session, *, text, started_at):
    """Run the REAL eager-checkpoint producer (routes.py)."""
    import api.routes as routes

    session.pending_user_message = text
    session.pending_user_source = "delegation_wakeup"
    session.pending_started_at = started_at
    session.active_stream_id = f"stream-{session.session_id}"
    routes._checkpoint_user_message_for_eager_session_save(
        session, text, None, started_at, source="delegation_wakeup",
    )


# --- the production finding: the eager checkpoint discounted twice -----------------


def test_eager_checkpoint_reports_visible_three_on_the_public_poll(monkeypatch, tmp_path):
    """Writer -> saved index -> payload builder -> GET must report 3, not 2.

    Schedule (the maintainer's replay): two visible rows are saved and also live
    in state.db; the eager checkpoint then materialises the hidden wakeup row in
    the sidecar (raw 3 / visible 2); the installed Agent appends the plain user
    row and the visible assistant reply to state.db (raw/actual 4). The
    authoritative reconciled visible count is 3.
    """
    now = time.time()
    _seed_store(monkeypatch, tmp_path, now=now)
    target_messages = [
        {"role": "user", "content": "hi", "timestamp": now - 60},
        {"role": "assistant", "content": "hello", "timestamp": now - 59},
    ]
    session = _new_session(
        TARGET, messages=target_messages, created_at=now - 60, updated_at=now - 59,
        title="Checkpointed",
    )
    session.save(touch_updated_at=False)
    _state_db(tmp_path, {
        "fmt0": [
            {"role": "user", "content": "filler 0 q", "timestamp": now - 900},
            {"role": "assistant", "content": "filler 0 a", "timestamp": now - 899},
        ],
        TARGET: list(target_messages),
    })
    for index in range(1, PINNED_FILLERS):
        _append_state_db_rows(tmp_path, f"fmt{index}", [])

    # Eager checkpoint: the hidden wakeup row lands in the sidecar before the
    # stream starts, and the save publishes sidecar + index together.
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", str(CAP))
    _eager_checkpoint(session, text="handoff", started_at=now)
    session.save()

    sidecar = json.loads((models.SESSION_DIR / f"{TARGET}.json").read_text(encoding="utf-8"))
    assert sidecar["message_count"] == 3, "the checkpoint row must be in the sidecar"
    assert sidecar["visible_message_count"] == 2, "and excluded from the visible count"
    index_row = next(
        row for row in json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
        if row["session_id"] == TARGET
    )
    assert index_row["message_count"] == 3
    assert index_row["visible_message_count"] == 2

    # The installed Agent appends the plain user row + the visible reply.
    _append_state_db_rows(tmp_path, TARGET, [
        {"role": "user", "content": "handoff", "timestamp": now + 1},
        {"role": "assistant", "content": "done", "timestamp": now + 2},
    ])
    assert _state_db_message_count(tmp_path, TARGET) == 4

    first = _public_poll(TARGET)
    second = _public_poll(TARGET)

    # Cost oracle: the walk now runs for the grown row itself (its visible
    # total has no honest cheap source), and the memo keeps the second poll
    # free once state.db stops moving.
    reconciled = []
    original_reconcile = models._reconciled_visible_count_for_sidebar

    def _counting_reconcile(session_id, **bounds):
        reconciled.append(str(session_id))
        return original_reconcile(session_id, **bounds)

    monkeypatch.setattr(models, "_reconciled_visible_count_for_sidebar", _counting_reconcile)
    _public_poll(TARGET)
    assert TARGET in reconciled, (
        "the grown row's visible total is walked, not delta-arithmetic'd"
    )
    assert reconciled == [TARGET], (
        f"only the grown row needs a walk, saw {reconciled}"
    )

    for label, row in (("first", first), ("second", second)):
        assert row["message_count"] == 4, (
            f"{label} poll: state.db raw growth stays authoritative"
        )
        assert row["visible_message_count"] == 3, (
            f"{label} poll: the hidden row the sidecar already excluded must not be "
            "discounted a second time (got "
            f"{row['visible_message_count']})"
        )


def test_eager_checkpoint_matches_the_cap_disabled_control(monkeypatch, tmp_path):
    """The cap changes cost, not the public visible count."""
    now = time.time()
    _seed_store(monkeypatch, tmp_path, now=now)
    target_messages = [
        {"role": "user", "content": "hi", "timestamp": now - 60},
        {"role": "assistant", "content": "hello", "timestamp": now - 59},
    ]
    session = _new_session(
        TARGET, messages=target_messages, created_at=now - 60, updated_at=now - 59,
    )
    session.save(touch_updated_at=False)
    _state_db(tmp_path, {
        "fmt0": [{"role": "user", "content": "filler 0", "timestamp": now - 900}],
        TARGET: list(target_messages),
    })
    for index in range(1, PINNED_FILLERS):
        _append_state_db_rows(tmp_path, f"fmt{index}", [])

    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    _eager_checkpoint(session, text="handoff", started_at=now)
    session.save()
    _append_state_db_rows(tmp_path, TARGET, [
        {"role": "user", "content": "handoff", "timestamp": now + 1},
        {"role": "assistant", "content": "done", "timestamp": now + 2},
    ])

    control = _public_poll(TARGET)
    assert control["visible_message_count"] == 3, (
        "cap-disabled control: the reconciled walk reports the authoritative 3"
    )


def test_deferred_hidden_turn_is_still_discounted(monkeypatch, tmp_path):
    """No eager checkpoint: the hidden row lives only in state.db and stays owed."""
    now = time.time()
    _seed_store(monkeypatch, tmp_path, now=now)
    target_messages = [
        {"role": "user", "content": "hi", "timestamp": now - 60},
        {"role": "assistant", "content": "hello", "timestamp": now - 59},
    ]
    session = _new_session(TARGET, messages=target_messages, created_at=now - 60, updated_at=now - 59)
    session.save(touch_updated_at=False)
    _state_db(tmp_path, {TARGET: list(target_messages)})

    # Deferred producer: pending fields exist, the row does NOT.
    session.pending_user_message = "handoff"
    session.pending_user_source = "delegation_wakeup"
    session.pending_started_at = now
    session.active_stream_id = f"stream-{TARGET}"
    session.save(touch_updated_at=False)
    _append_state_db_rows(tmp_path, TARGET, [
        {"role": "user", "content": "handoff", "timestamp": now + 1},
        {"role": "assistant", "content": "done", "timestamp": now + 2},
    ])

    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", str(CAP))
    row = _public_poll(TARGET)

    assert row["message_count"] == 4
    assert row["visible_message_count"] == 3, (
        "a deferred hidden row is absent from the sidecar, so its visible-count "
        "discount is still owed (and the reply still counts)"
    )


def _deferred_target(monkeypatch, tmp_path):
    """Sidecar without the wakeup row + a state.db that already has it.

    This is the shape the re-gate called out: the eager checkpoint did NOT
    materialise the row in the sidecar (so ``pending_hidden_materialized`` is
    False), yet the reconciled walk can still stamp the state.db row as hidden
    and exclude it. The discount is paid once by the walk -- subtracting it
    again counts 2 for a transcript that renders 3.
    """
    now = time.time()
    _seed_store(monkeypatch, tmp_path, now=now)
    target_messages = [
        {"role": "user", "content": "hi", "timestamp": now - 60},
        {"role": "assistant", "content": "hello", "timestamp": now - 59},
    ]
    session = _new_session(TARGET, messages=target_messages, created_at=now - 60, updated_at=now - 59)
    session.save(touch_updated_at=False)
    _state_db(tmp_path, {TARGET: list(target_messages)})

    session.pending_user_message = "handoff"
    session.pending_user_source = "delegation_wakeup"
    session.pending_started_at = now
    session.active_stream_id = f"stream-{TARGET}"
    session.save(touch_updated_at=False)
    _append_state_db_rows(tmp_path, TARGET, [
        {"role": "user", "content": "handoff", "timestamp": now},
        {"role": "assistant", "content": "done", "timestamp": now + 2},
    ])
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", str(CAP))


def _poll_with_walk(monkeypatch, walk_result):
    monkeypatch.setattr(
        models,
        "_reconciled_visible_count_for_sidebar",
        lambda sid, **_bounds: walk_result,
    )
    return _public_poll(TARGET)


def test_successful_walk_does_not_discount_a_deferred_wakeup_twice(monkeypatch, tmp_path):
    """Re-gate finding 1: the walk already excluded the row, so do not subtract again."""
    _deferred_target(monkeypatch, tmp_path)
    row = _poll_with_walk(
        monkeypatch, models._ReconciledSidebarVisible(3, True)
    )

    assert row["visible_message_count"] == 3, (
        "a successful walk that dropped the hidden row already paid the "
        "discount; subtracting it here reported 2 for a 3-row transcript"
    )


def test_successful_walk_still_discounts_an_unstamped_deferred_row(monkeypatch, tmp_path):
    """Control: when the walk could NOT identify the row as hidden, the discount is owed."""
    _deferred_target(monkeypatch, tmp_path)
    row = _poll_with_walk(
        monkeypatch, models._ReconciledSidebarVisible(3, False)
    )

    assert row["visible_message_count"] == 2, (
        "an unstamped deferred row is still owed its discount on the walk path"
    )


def test_walk_reports_whether_it_excluded_a_hidden_row(monkeypatch, tmp_path):
    """The flag is what lets the caller stop guessing: it comes from the merge."""
    _deferred_target(monkeypatch, tmp_path)
    result = models._reconciled_visible_count_for_sidebar(TARGET)

    assert result is not None
    assert isinstance(result, models._ReconciledSidebarVisible)
    assert result.hidden_excluded is True, (
        "the walk merged a state.db row and identified it as hidden, so it must "
        "report the exclusion instead of leaving the caller to subtract again"
    )


def test_fact_from_another_turn_does_not_vouch(monkeypatch, tmp_path):
    """A checkpoint fact recorded for a PREVIOUS pending turn must not excuse a new one."""
    now = time.time()
    _seed_store(monkeypatch, tmp_path, now=now)
    target_messages = [
        {"role": "user", "content": "hi", "timestamp": now - 60},
        {"role": "assistant", "content": "hello", "timestamp": now - 59},
    ]
    session = _new_session(TARGET, messages=target_messages, created_at=now - 60, updated_at=now - 59)
    session.save(touch_updated_at=False)
    _state_db(tmp_path, {TARGET: list(target_messages)})

    # A NEW pending turn starts with a DIFFERENT start timestamp: whatever fact
    # the previous turn left behind names a different row.
    _eager_checkpoint(session, text="handoff", started_at=now - 30)
    session.save(touch_updated_at=False)
    session.pending_started_at = now
    session.save(touch_updated_at=False)
    _append_state_db_rows(tmp_path, TARGET, [
        {"role": "assistant", "content": "done", "timestamp": now + 2},
    ])

    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", str(CAP))
    row = _public_poll(TARGET)

    assert row["visible_message_count"] == 2, (
        "a fact bound to another turn cannot vouch for the current pending row"
    )


# --- the other real-path regressions ----------------------------------------------


def test_first_append_row_is_public_through_the_writer_and_handler(monkeypatch, tmp_path):
    """The original first-append case on the real ingress, capped out."""
    now = time.time()
    _seed_store(monkeypatch, tmp_path, now=now)
    session = _new_session(
        TARGET, messages=[], created_at=now - 60, updated_at=now - 60,
        title="Named conversation",
    )
    session.save(touch_updated_at=False)
    _state_db(tmp_path, {TARGET: []})

    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", str(CAP))
    # First user turn arrives through state.db (CLI/Telegram continuation).
    _append_state_db_rows(tmp_path, TARGET, [
        {"role": "user", "content": "first", "timestamp": now + 1},
    ])

    first = _public_poll(TARGET)
    second = _public_poll(TARGET)
    for row in (first, second):
        assert row["message_count"] == 1
        assert row["visible_message_count"] == 1, (
            "the cheap raw growth must make a first-append row publicly visible "
            "even when fillers own the reconcile window"
        )


def _public_poll_row_or_none(sid):
    """Like ``_public_poll`` but tolerates the row being hidden from the sidebar."""
    import api.routes as routes

    routes._clear_session_list_cache()
    handler = _GetHandler("/api/sessions?sidebar_source=webui")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200, f"GET /api/sessions returned {handler.status}"
    for row in handler.response_json["sessions"]:
        if row.get("session_id") == sid:
            return row
    return None


def _sync_state_db_session_count(tmp_path, sid, count):
    """Keep the state.db sessions row in step with the truncated transcript.

    The real writer decrements this row when the transcript shrinks; the message
    ROWS stay behind, which is the "raw growth" the overlay reads. Leaving the
    counter at its pre-truncation value makes the row's current count equal the
    raw count, so the timestamp guard has nothing to protect and the regression
    is invisible to the fixture.
    """
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        conn.execute(
            "UPDATE sessions SET message_count = ? WHERE id = ?", (count, sid),
        )
        conn.commit()
    finally:
        conn.close()


def test_truncate_four_to_two_on_the_public_poll(monkeypatch, tmp_path):
    """Truncation repair on the real ingress: state.db must not re-credit the suffix.

    Master's contract: the sidecar's truncated count wins while the removed
    suffix's state.db rows carry an older timestamp. Dropping the timestamp
    guard made this report 4 raw / 4 visible, resurrecting deleted messages in
    the sidebar (#8400 re-gate).
    """
    now = time.time()
    _seed_store(monkeypatch, tmp_path, now=now)
    four = [
        {"role": "user", "content": "q1", "timestamp": now - 60},
        {"role": "assistant", "content": "a1", "timestamp": now - 59},
        {"role": "user", "content": "q2", "timestamp": now - 58},
        {"role": "assistant", "content": "a2", "timestamp": now - 57},
    ]
    session = _new_session(TARGET, messages=four, created_at=now - 60, updated_at=now - 57)
    session.save(touch_updated_at=False)
    _state_db(tmp_path, {TARGET: list(four)})

    # Truncate four to two, exactly as the edit/rollback path does.
    session.messages = four[:2]
    session.truncation_boundary = float(now - 57.5)
    session.save(touch_updated_at=False)
    _sync_state_db_session_count(tmp_path, TARGET, 2)

    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", str(CAP))
    first = _public_poll(TARGET)
    second = _public_poll(TARGET)

    for row in (first, second):
        assert row["message_count"] == 2, (
            "the truncated sidecar owns the count: an older state.db suffix "
            "must not be re-credited"
        )
        assert row["visible_message_count"] == 2, (
            "append-only state.db rows must not re-credit a suffix the sidecar truncated"
        )


def test_truncate_four_to_zero_hides_the_row_on_the_public_poll(monkeypatch, tmp_path):
    """Master's contract for a fully emptied transcript: the row disappears."""
    now = time.time()
    _seed_store(monkeypatch, tmp_path, now=now)
    four = [
        {"role": "user", "content": "q1", "timestamp": now - 60},
        {"role": "assistant", "content": "a1", "timestamp": now - 59},
        {"role": "user", "content": "q2", "timestamp": now - 58},
        {"role": "assistant", "content": "a2", "timestamp": now - 57},
    ]
    session = _new_session(TARGET, messages=four, created_at=now - 60, updated_at=now - 57)
    session.save(touch_updated_at=False)
    _state_db(tmp_path, {TARGET: list(four)})

    session.messages = []
    session.truncation_boundary = float(now - 56.5)
    session.save(touch_updated_at=False)
    _sync_state_db_session_count(tmp_path, TARGET, 0)

    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", str(CAP))
    assert _public_poll_row_or_none(TARGET) is None, (
        "an emptied transcript must not be resurrected by the removed suffix "
        "still sitting in state.db"
    )


# --- the fact itself must live where a real row can read it -----------------------


def test_writer_persists_the_fact_in_the_metadata_prefix_and_index(monkeypatch, tmp_path):
    now = time.time()
    _install_store(monkeypatch, tmp_path)
    session = _new_session(
        TARGET,
        messages=[
            {"role": "user", "content": "hi", "timestamp": now - 60},
            {"role": "assistant", "content": "hello", "timestamp": now - 59},
        ],
        created_at=now - 60, updated_at=now - 59,
    )
    session.save(touch_updated_at=False)
    _eager_checkpoint(session, text="handoff", started_at=now)
    session.save()

    raw = (models.SESSION_DIR / f"{TARGET}.json").read_text(encoding="utf-8")
    prefix = raw[: raw.index('"messages"')]
    assert "_pending_hidden_wakeup_at" in prefix, (
        "the fact must sit inside the metadata head a capped-out row reads"
    )
    index_row = next(
        row for row in json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
        if row["session_id"] == TARGET
    )
    assert index_row.get("_pending_hidden_wakeup_at") == pytest.approx(now, rel=0, abs=1e-6)
    assert index_row.get("pending_user_source") == "delegation_wakeup"
    stripped = models._strip_sidebar_heavy_metadata(dict(index_row))
    assert "_pending_hidden_wakeup_at" in stripped, (
        "the sidebar payload must not drop the only evidence the capped-out "
        "branch can read"
    )
    assert "messages" not in stripped


def test_writer_omits_the_fact_without_an_eager_checkpoint(monkeypatch, tmp_path):
    now = time.time()
    _install_store(monkeypatch, tmp_path)
    session = _new_session(
        TARGET,
        messages=[{"role": "user", "content": "hi", "timestamp": now - 60}],
        created_at=now - 60, updated_at=now - 59,
    )
    session.pending_user_message = "handoff"
    session.pending_user_source = "delegation_wakeup"
    session.pending_started_at = now
    session.save()

    index_row = next(
        row for row in json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
        if row["session_id"] == TARGET
    )
    assert "_pending_hidden_wakeup_at" not in index_row
    assert models._pending_hidden_row_materialized(index_row) is False


def test_writer_drops_the_fact_once_the_row_is_gone(monkeypatch, tmp_path):
    """Fail closed: no row in the transcript means no evidence."""
    now = time.time()
    _install_store(monkeypatch, tmp_path)
    session = _new_session(
        TARGET,
        messages=[{"role": "user", "content": "hi", "timestamp": now - 60}],
        created_at=now - 60, updated_at=now - 59,
    )
    _eager_checkpoint(session, text="handoff", started_at=now)
    session.messages.pop()  # the checkpointed row disappears
    session.save()

    index_row = next(
        row for row in json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
        if row["session_id"] == TARGET
    )
    assert "_pending_hidden_wakeup_at" not in index_row


def test_reload_keeps_the_fact_only_while_it_names_the_pending_turn(monkeypatch, tmp_path):
    """The fact rides the metadata prefix, so a reload must keep it -- but only
    while the pending turn it names is still the pending turn."""
    now = time.time()
    _install_store(monkeypatch, tmp_path)
    session = _new_session(
        TARGET,
        messages=[
            {"role": "user", "content": "hi", "timestamp": now - 60},
            {"role": "assistant", "content": "hello", "timestamp": now - 59},
        ],
        created_at=now - 60, updated_at=now - 59,
    )
    _eager_checkpoint(session, text="handoff", started_at=now)
    session.save()

    reloaded = models._load_session_from_path(models.SESSION_DIR / f"{TARGET}.json")
    assert reloaded is not None
    assert reloaded._pending_hidden_wakeup_at == pytest.approx(now, rel=0, abs=1e-6), (
        "an index rebuild reloads the sidecar; it must not lose the checkpoint fact"
    )
    assert reloaded.compact().get("_pending_hidden_wakeup_at") == pytest.approx(
        now, rel=0, abs=1e-6
    )

    # A different pending turn (or none at all) must not inherit the fact.
    reloaded.pending_started_at = now + 5
    assert reloaded.compact().get("_pending_hidden_wakeup_at") is None
    reloaded.pending_user_source = None
    reloaded.pending_started_at = None
    assert reloaded.compact().get("_pending_hidden_wakeup_at") is None


def test_a_reload_drops_the_fact_once_the_hidden_row_is_gone(
    tmp_path, monkeypatch,
):
    """A stale fact must not survive a reload that replaced the hidden row.

    The fact excuses a discount, so it may only live while the row it names is
    really in the transcript. ``Session.compact()`` re-emits it and an index
    rebuild can persist that compact payload, so validating on load without
    looking at the transcript lets a replaced row keep the excuse forever --
    the sidebar then undercounts by one. Loading must check the row.
    """
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)

    payload = {
        "session_id": "reload",
        "title": "Reload",
        "workspace": str(tmp_path),
        "model": "test-model",
        "message_count": 2,
        "pending_user_message": "Do the task.",
        "pending_started_at": 500.0,
        "pending_user_source": "delegation_wakeup",
        "_pending_hidden_wakeup_at": 500.0,
        # The hidden delegation-wakeup row is NOT here: it was replaced.
        "messages": [
            {"role": "user", "content": "Do the task.", "timestamp": 500.0},
            {"role": "assistant", "content": "Done.", "timestamp": 501.0},
        ],
    }

    session = models.Session(**payload)

    assert session._pending_hidden_wakeup_at is None, (
        "a reload whose transcript no longer holds the named hidden row must "
        "drop the fact; keeping it excuses a discount that is still owed"
    )


# --- re-gate findings 1 and 2: every ingress keeps the current-turn fact --------


def _eager_continued_target(monkeypatch, tmp_path, *, now, seed_fillers=True):
    """Seed fillers plus a target whose eager turn the installed Agent continued.

    Returns after the Agent appended the plain user row and the visible reply to
    state.db (authoritative visible 3, raw 4).
    """
    if seed_fillers:
        _seed_store(monkeypatch, tmp_path, now=now)
    else:
        _install_store(monkeypatch, tmp_path)
    target_messages = [
        {"role": "user", "content": "hi", "timestamp": now - 60},
        {"role": "assistant", "content": "hello", "timestamp": now - 59},
    ]
    session = _new_session(
        TARGET, messages=target_messages, created_at=now - 60, updated_at=now - 59,
        title="Checkpointed",
    )
    session.save(touch_updated_at=False)
    _state_db(tmp_path, {TARGET: list(target_messages)})

    _eager_checkpoint(session, text="handoff", started_at=now)
    session.save()
    return session


def _continue_target_in_state_db(tmp_path, now):
    """The installed Agent appends the pending user row and the visible reply."""
    _append_state_db_rows(tmp_path, TARGET, [
        {"role": "user", "content": "handoff", "timestamp": now + 1},
        {"role": "assistant", "content": "done", "timestamp": now + 2},
    ])
    assert _state_db_message_count(tmp_path, TARGET) == 4


def test_metadata_stub_ingress_keeps_the_current_turn_fact(monkeypatch, tmp_path):
    """``load_metadata_only`` skips the transcript on purpose, not by accident.

    The capped-out row is rebuilt from a metadata stub; an empty ``messages``
    list there is no evidence that the checkpointed row vanished, so the fact
    must survive and the poll must report the authoritative 3.
    """
    now = time.time()
    _eager_continued_target(monkeypatch, tmp_path, now=now)
    _continue_target_in_state_db(tmp_path, now)

    stub = models.Session.load_metadata_only(TARGET)
    assert stub is not None
    assert stub.messages == []
    assert stub._pending_hidden_wakeup_at == pytest.approx(now, rel=0, abs=1e-6), (
        "a transcript-less ingress must not read the absent transcript as a lost row"
    )

    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", str(CAP))
    first = _public_poll(TARGET)
    second = _public_poll(TARGET)
    for label, row in (("first", first), ("second", second)):
        assert row["visible_message_count"] == 3, (
            f"{label} poll after a metadata-stub ingress reported "
            f"{row['visible_message_count']}, expected 3"
        )


def test_missing_index_ingress_keeps_the_current_turn_fact(monkeypatch, tmp_path):
    """Missing-index recovery goes through ``load_metadata_only`` as well."""
    now = time.time()
    _eager_continued_target(monkeypatch, tmp_path, now=now)
    _continue_target_in_state_db(tmp_path, now)

    entries = json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
    kept = [row for row in entries if row.get("session_id") != TARGET]
    assert len(kept) == len(entries) - 1
    models.SESSION_INDEX_FILE.write_text(json.dumps(kept), encoding="utf-8")

    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", str(CAP))
    first = _public_poll(TARGET)
    second = _public_poll(TARGET)
    for label, row in (("first", first), ("second", second)):
        assert row["visible_message_count"] == 3, (
            f"{label} poll after a missing-index recovery reported "
            f"{row['visible_message_count']}, expected 3"
        )


def test_stale_index_fact_does_not_vouch_for_a_newer_deferred_turn(monkeypatch, tmp_path):
    """An older eager checkpoint's fact must not carry through the index row.

    The sidecar is written by an eager checkpoint, then the turn settles and a
    NEWER deferred hidden turn starts. The stale index row still carries the old
    fact and the old ``pending_started_at``; refreshing from the current sidecar
    must take both from the sidecar, so the stale fact cannot excuse the new
    turn's discount.

    No filler here: a pinned filler would consume the reconcile window itself
    and turn this into the uncapped case, so the target has to be capped out on
    its own.
    """
    now = time.time()
    _eager_continued_target(monkeypatch, tmp_path, now=now, seed_fillers=False)

    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", str(CAP))

    stale_index = next(
        row for row in json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
        if row["session_id"] == TARGET
    )
    assert stale_index["_pending_hidden_wakeup_at"] == pytest.approx(
        now, rel=0, abs=1e-6
    ), "the eager save must publish the fact into the index row"
    assert stale_index["message_count"] == 3, (
        "fixture precondition: the sidecar holds two visible rows + the eager "
        "wakeup row, so the continuation below must not be appended twice"
    )

    # The turn settles, then a NEWER deferred hidden turn starts: no transcript
    # row, same text, different start timestamp. Patch the canonical sidecar on
    # disk (the writer only persists the fact while it names a row) and leave the
    # OLD index bytes in place -- the stale-index recovery scene.
    sidecar_path = models.SESSION_DIR / f"{TARGET}.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    from api.models import is_hidden_transcript_row as _hidden

    sidecar.pop("_pending_hidden_wakeup_at", None)
    sidecar["pending_user_message"] = "handoff"
    sidecar["pending_user_source"] = "delegation_wakeup"
    sidecar["pending_started_at"] = now + 5
    # What the writer publishes for this transcript: the visible count excludes
    # hidden rows (the same predicate ``save`` uses) and only rows that exist,
    # so the removed eager wakeup row no longer absorbs a discount.
    sidecar["visible_message_count"] = sum(
        1 for m in sidecar.get("messages") or [] if not _hidden(m)
    )
    assert sidecar["visible_message_count"] == 2, (
        "fixture precondition: the eager wakeup row is the only hidden row, so "
        "removing it cannot raise the visible total"
    )
    # The newer deferred turn's row is appended by the Agent below and is hidden
    # by provenance, so the authoritative total is sidecar-visible + the reply.
    sidecar["visible_message_count"] = 3
    sidecar["updated_at"] = now + 5
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    _append_state_db_rows(tmp_path, TARGET, [
        {"role": "user", "content": "handoff", "timestamp": now + 6},
        {"role": "assistant", "content": "done", "timestamp": now + 7},
    ])

    _seen = {}
    _original_overlay = models._apply_sidebar_state_db_override_metadata

    def _capturing_overlay(sessions, metadata, count_session_ids=None):
        for _s in sessions:
            if _s.get('session_id') == TARGET:
                _seen.update(_s)
        return _original_overlay(sessions, metadata, count_session_ids)

    monkeypatch.setattr(
        models, '_apply_sidebar_state_db_override_metadata', _capturing_overlay
    )
    first = _public_poll(TARGET)
    second = _public_poll(TARGET)
    for label, row in (("first", first), ("second", second)):
        # Authoritative visible total after the continuation is 3: the deferred
        # hidden row keeps its discount (no sidecar row holds it) and the visible
        # reply counts. A stale fact would excuse the discount and read 2.
        assert row["visible_message_count"] == 3, (
            f"{label} poll: a fact stamped by the previous eager turn must not "
            f"vouch for the newer deferred hidden turn (got "
            f"{row['visible_message_count']}, expected 3)"
        )

    # The overlay must take turn identity and fact from the CURRENT sidecar, not
    # from the stale index row: a stale fact travelling with the stale pending
    # timestamp would agree with itself and vouch for the wrong turn. The
    # sidecar's deferred turn started at ``now + 5``; that is the identity the
    # overlay has to see.
    assert _seen.get('pending_started_at') == pytest.approx(now + 5, rel=0, abs=1e-6), (
        "the overlay kept the stale index pending_started_at instead of the "
        "current sidecar's"
    )
    assert not _seen.get('_pending_hidden_wakeup_at'), (
        "the overlay kept the stale index checkpoint fact"
    )

    # Control: with the transcript walk allowed on every row (cap disabled) the
    # same store reports the same total, i.e. the capped branch is not the thing
    # that moves the number.
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    control = _public_poll(TARGET)
    assert control["visible_message_count"] == 3


# --- the constructor must see the boundary it is storing (re-gate should-fix) ------


def test_constructor_drops_the_fact_when_a_truncation_boundary_is_persisted():
    """A full load must validate the fact against the boundary it stores.

    The validator reads ``session.truncation_boundary``, so it cannot run before
    the constructor assigns that attribute: it used to, which made the dead
    ``transcript_truncated`` argument look meaningful while it was always false. The
    behaviour pinned here is the one ``save`` already enforced -- a truncated
    sidecar never emits the fact -- so the persisted payload is unchanged; only
    the in-memory value on a full load now agrees with it.
    """
    now = time.time()
    transcript = [
        {"role": "user", "content": "hi", "timestamp": now - 60},
        {"role": "assistant", "content": "hello", "timestamp": now - 59},
        {
            "role": "user",
            "content": "handoff",
            "_source": "delegation_wakeup",
            "timestamp": now,
        },
    ]

    def build(boundary):
        return models.Session(
            session_id=TARGET,
            title="Session",
            workspace="/tmp",
            model="test-model",
            messages=[dict(message) for message in transcript],
            created_at=now - 60,
            updated_at=now - 59,
            pending_user_message="handoff",
            pending_user_source="delegation_wakeup",
            pending_started_at=now,
            truncation_boundary=boundary,
            _pending_hidden_wakeup_at=now,
        )

    kept = build(None)
    assert kept._pending_hidden_wakeup_at == pytest.approx(now, rel=0, abs=1e-6), (
        "a full load without a truncation boundary must keep the fact"
    )
    dropped = build(float(now - 57.5))
    assert dropped._pending_hidden_wakeup_at is None, (
        "a full load of a truncated sidecar kept a fact whose row may be gone"
    )
