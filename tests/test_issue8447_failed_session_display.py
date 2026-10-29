"""Regression tests — a session whose last turn failed at the provider must
not look like a healthy idle session (#8447).

The Agent stamps ``display_kind='failed_turn'`` on the boundary row of a turn
that failed at the provider and carries a failure surface in
``display_metadata``.  Before this change the WebUI dropped both columns on
every canonical read, so a dead session was byte-identical in shape to a
healthy one and the sidebar rendered it as an ordinary idle row.

These pin the whole path:

* both SQL readers (full and bounded tail) select and decode the columns;
* the shared projector sanitizes ``display_metadata`` — the raw provider
  error text never leaves the reader;
* duplicate-row reconciliation preserves the display fields;
* the derived per-session summary is persisted in the metadata prefix, so the
  sidebar reads it without a transcript scan;
* a later successful terminal turn clears the state, while interim
  assistant/tool activity does not;
* missing or malformed metadata yields a generic state, never a guessed
  verdict.

They also pin the negative: the raw provider error text must never reach the
sidebar row, the session summary, or a provider request payload.
"""
import json
import sqlite3

import api.models as models
from api.route_session_list_cache import _SIDEBAR_SESSION_RESPONSE_FIELDS


# ── helpers ────────────────────────────────────────────────────────────────

def _make_state_db(path, *, with_display_columns=True):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT,
            source TEXT, session_source TEXT, title TEXT,
            message_count INTEGER, started_at REAL, ended_at REAL
        )
        """
    )
    display_cols = (
        ", display_kind TEXT, display_metadata TEXT"
        if with_display_columns
        else ""
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            timestamp REAL, tool_calls TEXT, tool_name TEXT, active INTEGER
            {display_cols}
        )
        """.format(display_cols=display_cols)
    )
    conn.commit()
    conn.close()


def _point_models_at(monkeypatch, db):
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "_strict_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "_agent_state_db_path", lambda **kwargs: db)


def _insert_failed_turn(conn, *, session_id="sess-1", row_id=1, ts=1000.0,
                        layer="billing", code="billing", retryable=False,
                        raw_error="HTTP 402: You're out of credits. Add more at https://secret-provider.example/billing",
                        metadata=None):
    """Insert a ``failed_turn`` boundary row the way the Agent stamps it."""
    if metadata is None:
        surface = {"layer": layer, "code": code, "retryable": retryable,
                   "provider": "custom", "model": "qwen3.8-max"}
        metadata = {"error": raw_error, "error_surface": surface}
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp, "
        "display_kind, display_metadata) VALUES (?,?,?,?,?,?,?)",
        (row_id, session_id, "assistant",
         "Your request was not processed. Send it again if you still want to.",
         ts, "failed_turn", json.dumps(metadata)),
    )


def _insert_row(conn, *, row_id, role, content, ts, display_kind=None,
                display_metadata=None, tool_calls=None):
    columns = ["id", "session_id", "role", "content", "timestamp", "tool_calls"]
    values = [row_id, "sess-1", role, content, ts, tool_calls]
    available = {
        str(r[1]) for r in conn.execute("PRAGMA table_info(messages)").fetchall()
    }
    if "display_kind" in available:
        columns.append("display_kind")
        values.append(display_kind)
    if "display_metadata" in available:
        columns.append("display_metadata")
        values.append(display_metadata)
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO messages ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )


# ── the projector sanitizes display_metadata ───────────────────────────────

def test_projector_sanitizes_display_metadata_and_drops_raw_error():
    row = {
        "role": "assistant",
        "content": "Your request was not processed.",
        "timestamp": 1000.0,
        "display_kind": "failed_turn",
        "display_metadata": json.dumps({
            "error": "HTTP 402: out of credits at https://secret.example/x",
            "error_surface": {"layer": "billing", "code": "billing",
                              "retryable": False, "provider": "custom",
                              "model": "qwen3.8-max"},
        }),
    }
    out = models._project_state_db_message(
        row, set(row.keys()), [], ["display_kind", "display_metadata"],
    )
    assert out["display_kind"] == "failed_turn"
    assert out["display_metadata"] == {
        "layer": "billing", "code": "billing", "retryable": False,
    }
    # The raw provider error text must not survive anywhere in the row.
    assert "secret.example" not in json.dumps(out)
    assert "out of credits" not in json.dumps(out)


def test_projector_drops_display_metadata_without_a_usable_surface():
    row = {
        "role": "assistant",
        "content": "x",
        "timestamp": 1000.0,
        "display_kind": "failed_turn",
        "display_metadata": json.dumps({"error": "boom"}),
    }
    out = models._project_state_db_message(
        row, set(row.keys()), [], ["display_kind", "display_metadata"],
    )
    assert out["display_kind"] == "failed_turn"
    assert "display_metadata" not in out
    assert "boom" not in json.dumps(out)


def test_projector_survives_malformed_display_metadata():
    row = {
        "role": "assistant",
        "content": "x",
        "timestamp": 1000.0,
        "display_kind": "failed_turn",
        "display_metadata": "{not json",
    }
    out = models._project_state_db_message(
        row, set(row.keys()), [], ["display_kind", "display_metadata"],
    )
    assert out["display_kind"] == "failed_turn"
    assert "display_metadata" not in out


def test_projector_keeps_a_non_boolean_retryable_out():
    row = {
        "role": "assistant",
        "content": "x",
        "timestamp": 1000.0,
        "display_kind": "failed_turn",
        "display_metadata": json.dumps({
            "error_surface": {"layer": "billing", "retryable": "maybe"},
        }),
    }
    out = models._project_state_db_message(
        row, set(row.keys()), [], ["display_kind", "display_metadata"],
    )
    assert out["display_metadata"] == {"layer": "billing"}


# ── both SQL readers carry the columns ─────────────────────────────────────

def test_full_reader_returns_the_failure_row(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_failed_turn(conn)
    conn.commit()
    conn.close()
    _point_models_at(monkeypatch, db)

    msgs = models.get_state_db_session_messages("sess-1")
    assert len(msgs) == 1, msgs
    assert msgs[0]["display_kind"] == "failed_turn"
    assert msgs[0]["display_metadata"]["layer"] == "billing"
    assert msgs[0]["display_metadata"]["retryable"] is False
    assert "secret-provider.example" not in json.dumps(msgs)


def test_bounded_tail_reader_returns_the_failure_row(tmp_path, monkeypatch):
    """The bounded tail reader is a separate SELECT and a separate column
    list — it must not silently drop the display columns."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_failed_turn(conn, ts=1000.0)
    conn.commit()
    conn.close()
    _point_models_at(monkeypatch, db)

    # A floor that includes the failure row.
    msgs = models.get_state_db_session_messages(
        "sess-1", since_timestamp=999.0,
    )
    assert any(m.get("display_kind") == "failed_turn" for m in msgs), msgs


def test_older_schema_without_display_columns_still_reads(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db, with_display_columns=False)
    conn = sqlite3.connect(str(db))
    _insert_row(conn, row_id=1, role="user", content="hi", ts=1000.0)
    conn.commit()
    conn.close()
    _point_models_at(monkeypatch, db)

    msgs = models.get_state_db_session_messages("sess-1")
    assert len(msgs) == 1, msgs
    assert "display_kind" not in msgs[0]
    assert "display_metadata" not in msgs[0]


# ── duplicate reconciliation preserves the display fields ──────────────────

def test_merge_preserves_display_fields_across_duplicate_rows():
    target = {"role": "assistant", "content": "x"}
    source = {
        "role": "assistant",
        "content": "x",
        "display_kind": "failed_turn",
        "display_metadata": {"layer": "billing", "retryable": False},
    }
    models._merge_session_display_metadata(target, source)
    assert target["display_kind"] == "failed_turn"
    assert target["display_metadata"] == {"layer": "billing", "retryable": False}


def test_merge_keeps_an_existing_display_kind():
    target = {"display_kind": "failed_turn", "display_metadata": {"layer": "billing"}}
    source = {"display_kind": "failed_turn", "display_metadata": {"layer": "other"}}
    models._merge_session_display_metadata(target, source)
    assert target["display_metadata"] == {"layer": "billing"}


# ── the derived per-session summary ────────────────────────────────────────

def test_summary_reports_a_failed_turn():
    summary = models._session_failure_summary([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "nope", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing", "code": "billing",
                              "retryable": False}},
    ])
    assert summary == {"kind": "failed_turn", "layer": "billing",
                       "code": "billing", "retryable": False}


def test_summary_is_generic_when_metadata_is_missing():
    summary = models._session_failure_summary([
        {"role": "assistant", "content": "nope", "display_kind": "failed_turn"},
    ])
    assert summary == {"kind": "failed_turn", "detail": "unknown"}
    # Never a guessed billing verdict.
    assert "billing" not in json.dumps(summary)


def test_summary_is_none_for_a_healthy_session():
    assert models._session_failure_summary([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]) is None


def test_summary_clears_after_a_successful_terminal_turn():
    summary = models._session_failure_summary([
        {"role": "assistant", "content": "nope", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing"}},
        {"role": "assistant", "content": "recovered"},
    ])
    assert summary is None


def test_summary_survives_interim_tool_activity():
    """Tool activity is not proof of recovery: a failed turn can be followed
    by more tool calls before another failure."""
    summary = models._session_failure_summary([
        {"role": "assistant", "content": "nope", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing"}},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "tool output", "tool_name": "bash"},
    ])
    assert summary is not None
    assert summary["kind"] == "failed_turn"


def test_summary_does_not_clear_on_the_next_failures_own_assistant_row():
    """A failed turn is itself an assistant row, so clearing on 'any later
    assistant row' would self-clear on the next failure."""
    summary = models._session_failure_summary([
        {"role": "assistant", "content": "nope", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing"}},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "nope again", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing", "retryable": False}},
    ])
    assert summary is not None
    assert summary["kind"] == "failed_turn"


def test_summary_distinguishes_retryable_from_non_retryable():
    retryable = models._session_failure_summary([
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "upstream", "retryable": True}},
    ])
    terminal = models._session_failure_summary([
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing", "retryable": False}},
    ])
    assert retryable is not None
    assert terminal is not None
    assert retryable["retryable"] is True
    assert terminal["retryable"] is False


def test_summary_ignores_a_non_failure_display_kind():
    assert models._session_failure_summary([
        {"role": "assistant", "display_kind": "boundary_row",
         "display_metadata": {"layer": "billing"}},
    ]) is None


def test_summary_never_carries_the_raw_error_text():
    summary = models._session_failure_summary([
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {
             "error": "HTTP 402: out of credits at https://secret.example",
             "error_surface": {"layer": "billing", "retryable": False},
         }},
    ])
    assert "secret.example" not in json.dumps(summary)
    assert "out of credits" not in json.dumps(summary)


# ── the sidebar carries the summary ────────────────────────────────────────

def test_sidebar_allowlist_carries_the_failure_summary():
    assert "failure_summary" in _SIDEBAR_SESSION_RESPONSE_FIELDS


def test_compact_emits_the_summary_only_when_the_session_failed():
    session = models.Session(session_id="sess-1")
    session.messages = [
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing", "retryable": False}},
    ]
    session.failure_summary = models._session_failure_summary(session.messages)
    compact = session.compact()
    assert compact["failure_summary"] == {
        "kind": "failed_turn", "layer": "billing", "retryable": False,
    }

    healthy = models.Session(session_id="sess-2")
    healthy.messages = [{"role": "assistant", "content": "ok"}]
    healthy.failure_summary = models._session_failure_summary(healthy.messages)
    assert "failure_summary" not in healthy.compact()
