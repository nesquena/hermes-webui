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
import collections
import json
import sqlite3
import threading
import time

import pytest

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


def _make_agent_state_db(path, *, with_display_columns=True):
    """State.db for the Agent sidebar projection: sessions + messages."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER,
            started_at REAL, source TEXT, parent_session_id TEXT,
            ended_at REAL, end_reason TEXT, session_source TEXT
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
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, timestamp REAL, tool_calls TEXT, active INTEGER
            {display_cols}
        )
        """.format(display_cols=display_cols)
    )
    conn.commit()
    conn.close()


def _reset_failure_caches(monkeypatch):
    """Isolate the revision-gated backfill caches per test."""
    monkeypatch.setattr(
        models, "_SESSION_FAILURE_SUMMARY_CACHE", collections.OrderedDict()
    )
    monkeypatch.setattr(models, "_SESSION_FAILURE_FINGERPRINT_CACHE", {})


@pytest.fixture
def fake_agent_home(tmp_path, monkeypatch):
    """Point profile resolvers at a temporary HERMES_HOME + projects.json."""
    home = tmp_path / "hermes"
    home.mkdir()

    import api.config as cfg
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: None)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name == "default")

    projects_file = tmp_path / "projects.json"
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    projects_file.write_text("[]", encoding="utf-8")

    models.clear_cli_sessions_cache()
    yield home
    models.clear_cli_sessions_cache()


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


# ── the reducer requires a genuine successful settlement (#8603 r1) ────────

def test_summary_survives_the_webui_error_settlement_row():
    """The streaming error-settlement path appends an assistant ``_error=True``
    row and saves; that row must not read as recovery."""
    summary = models._session_failure_summary([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing", "retryable": False}},
        {"role": "assistant", "content": "**Error:** provider said no",
         "_error": True},
    ])
    assert summary is not None
    assert summary["layer"] == "billing"
    assert summary["retryable"] is False


def test_summary_survives_a_partial_row():
    summary = models._session_failure_summary([
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "upstream", "retryable": True}},
        {"role": "assistant", "content": "partial text", "_partial": True},
    ])
    assert summary is not None
    assert summary["layer"] == "upstream"


def test_summary_survives_a_reasoning_row():
    summary = models._session_failure_summary([
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing"}},
        {"role": "assistant", "content": "", "reasoning": "thinking..."},
    ])
    assert summary is not None
    assert summary["layer"] == "billing"


def test_summary_survives_user_and_tool_activity_after_a_failure():
    """A real failure → new user → tool-call/result sequence must NOT clear
    before an actual answer lands."""
    summary = models._session_failure_summary([
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing"}},
        {"role": "user", "content": "try again"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "tool output", "tool_name": "bash"},
    ])
    assert summary is not None
    assert summary["kind"] == "failed_turn"
    assert summary["layer"] == "billing"


def test_summary_clears_only_after_a_genuine_settled_answer():
    summary = models._session_failure_summary([
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing"}},
        {"role": "user", "content": "try again"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "tool output", "tool_name": "bash"},
        {"role": "assistant", "content": "done"},
    ])
    assert summary is None


def test_summary_does_not_clear_on_a_non_failure_boundary_kind():
    summary = models._session_failure_summary([
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing"}},
        {"role": "assistant", "display_kind": "boundary_row",
         "display_metadata": {"layer": "other"}},
    ])
    assert summary is not None
    assert summary["layer"] == "billing"


# ── both display_metadata ingresses converge on one sanitized shape ────────

def test_summary_normalizes_the_agents_nested_metadata():
    """The Agent's result messages carry ``{error, error_surface:{...}}``
    (direct ingress, no projector); the summary must match the projected
    flattened shape exactly."""
    flattened = models._session_failure_summary([
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing", "code": "billing",
                              "retryable": False}},
    ])
    nested = models._session_failure_summary([
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {
             "error": "HTTP 402: out of credits at https://secret.example",
             "error_surface": {"layer": "billing", "code": "billing",
                               "retryable": False, "provider": "custom"},
         }},
    ])
    assert nested == flattened
    assert "secret.example" not in json.dumps(nested)


def test_summary_keeps_retryability_unknown_when_absent():
    """Tri-state retryability: an absent producer flag must not collapse to
    the terminal false."""
    summary = models._session_failure_summary([
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "upstream"}},
    ])
    assert summary == {"kind": "failed_turn", "layer": "upstream"}
    assert "retryable" not in summary


def test_summary_treats_a_non_boolean_retryable_as_unknown():
    summary = models._session_failure_summary([
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "upstream", "retryable": "maybe"}},
    ])
    assert summary == {"kind": "failed_turn", "layer": "upstream"}
    assert "retryable" not in summary


# ── the duplicate merge converges the ingresses ────────────────────────────

def test_merge_replaces_a_nested_target_with_the_normalized_surface():
    """A nonempty nested target must not keep its raw payload: the duplicate
    merge adopts the normalized shape, and the raw provider error text never
    rides on in the transcript."""
    target = {"role": "assistant", "display_kind": "failed_turn",
              "display_metadata": {
                  "error": "HTTP 402 at https://secret.example",
                  "error_surface": {"layer": "billing", "code": "billing",
                                    "retryable": False}}}
    source = {"role": "assistant", "display_kind": "failed_turn",
              "display_metadata": {"layer": "billing", "code": "billing",
                                   "retryable": False}}
    models._merge_session_display_metadata(target, source)
    assert target["display_metadata"] == {
        "layer": "billing", "code": "billing", "retryable": False,
    }
    assert "secret.example" not in json.dumps(target)


def test_merge_adopts_the_normalized_surface_over_a_useless_nested_target():
    target = {"role": "assistant", "display_kind": "failed_turn",
              "display_metadata": {"error": "boom"}}
    source = {"role": "assistant", "display_kind": "failed_turn",
              "display_metadata": {"layer": "upstream", "retryable": True}}
    models._merge_session_display_metadata(target, source)
    assert target["display_metadata"] == {"layer": "upstream", "retryable": True}
    assert "boom" not in json.dumps(target)


def test_merge_keeps_non_failure_metadata_presence_based():
    """Boundary kinds other than failed_turn keep the generic merge — their
    metadata must never be dropped."""
    target = {"role": "assistant", "display_kind": "steer"}
    source = {"role": "assistant", "display_kind": "steer",
              "display_metadata": {"anything": "kept"}}
    models._merge_session_display_metadata(target, source)
    assert target["display_metadata"] == {"anything": "kept"}


# ── save() publishes the summary to resident object + index (both directions)

def _point_session_dir_at(monkeypatch, session_dir):
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", collections.OrderedDict())


def test_save_publishes_summary_to_resident_and_index(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)

    session = models.Session(session_id="sess-save", messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing", "code": "billing",
                              "retryable": False}},
    ])
    expected = {"kind": "failed_turn", "layer": "billing", "code": "billing",
                "retryable": False}
    session.save()
    # Resident object: compact() must read the just-derived value, not the
    # pre-save one.
    assert session.failure_summary == expected
    assert session.compact()["failure_summary"] == expected
    # Sidecar JSON prefix.
    on_disk = json.loads(
        (session_dir / "sess-save.json").read_text(encoding="utf-8")
    )
    assert on_disk["failure_summary"] == expected
    # Sidebar index projection (second save exercises the targeted patch
    # path; the first rebuilt the whole index).
    session.save()
    index = json.loads(
        (session_dir / "_index.json").read_text(encoding="utf-8")
    )
    assert index[0]["session_id"] == "sess-save"
    assert index[0]["failure_summary"] == expected


def test_save_clears_summary_on_recovery(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)

    session = models.Session(session_id="sess-recover", messages=[
        {"role": "assistant", "display_kind": "failed_turn",
         "display_metadata": {"layer": "billing", "retryable": False}},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "recovered"},
    ])
    # A stale marker the resident object still carries from before recovery.
    session.failure_summary = {"kind": "failed_turn", "layer": "billing",
                               "retryable": False}
    session.save()
    assert session.failure_summary is None
    assert "failure_summary" not in session.compact()
    on_disk = json.loads(
        (session_dir / "sess-recover.json").read_text(encoding="utf-8")
    )
    assert on_disk["failure_summary"] is None
    session.save()
    index = json.loads(
        (session_dir / "_index.json").read_text(encoding="utf-8")
    )
    assert index[0]["session_id"] == "sess-recover"
    assert "failure_summary" not in index[0]


# ── cold acquisition / backfill (#8603 r2) ─────────────────────────────────

def _write_legacy_sidecar(session_dir, sid, messages):
    """A sidecar written before the summary derivation existed: no
    ``failure_summary`` key at all."""
    payload = {
        "session_id": sid,
        "title": "Legacy",
        "created_at": 1.0,
        "updated_at": 2.0,
        "message_count": len(messages),
        "messages": messages,
    }
    (session_dir / f"{sid}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_cold_full_load_acquires_summary_from_state_db(tmp_path, monkeypatch):
    """A session whose sidecar has no summary key at all must surface the
    already-settled failure through a cold full load — without a WebUI save."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_failed_turn(conn, session_id="sess-1", row_id=2, ts=1000.0)
    _insert_row(conn, row_id=1, role="user", content="old", ts=900.0)
    conn.commit()
    conn.close()
    _point_models_at(monkeypatch, db)

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    _write_legacy_sidecar(session_dir, "sess-1", [
        {"role": "user", "content": "old", "timestamp": 900.0},
    ])

    session = models.Session.load("sess-1")
    assert session.failure_summary == {
        "kind": "failed_turn", "layer": "billing", "code": "billing",
        "retryable": False,
    }
    # The failed_turn row lives in state.db; the sidecar must NOT have been
    # rewritten by the load.
    assert "failure_summary" not in json.loads(
        (session_dir / "sess-1.json").read_text(encoding="utf-8")
    )


def test_cold_metadata_only_load_acquires_summary_without_saving_stub(
    tmp_path, monkeypatch
):
    db = tmp_path / "state.db"
    _make_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_failed_turn(conn, session_id="sess-1", row_id=1, ts=1000.0)
    conn.commit()
    conn.close()
    _point_models_at(monkeypatch, db)

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    _write_legacy_sidecar(session_dir, "sess-1", [
        {"role": "user", "content": "old", "timestamp": 900.0},
    ])
    before = (session_dir / "sess-1.json").stat()

    session = models.Session.load_metadata_only("sess-1")
    assert session._loaded_metadata_only is True
    assert session.failure_summary == {
        "kind": "failed_turn", "layer": "billing", "code": "billing",
        "retryable": False,
    }
    # The sidecar was not rewritten into a stub by the acquisition.
    after = (session_dir / "sess-1.json").stat()
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
    assert "message_count" in json.loads(
        (session_dir / "sess-1.json").read_text(encoding="utf-8")
    )


def test_agent_owned_list_rows_carry_the_summary(fake_agent_home, monkeypatch):
    """The direct Agent session-list dictionaries must carry the summary —
    they never pass through a WebUI save."""
    db = fake_agent_home / "state.db"
    _make_agent_state_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO sessions (id, title, model, message_count, started_at, "
        "source) VALUES ('agent-sess', 'Agent run', 'gpt-x', 2, 1.0, 'cli')"
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES ('agent-sess', 'user', 'hi', 1.0)"
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, "
        "display_kind, display_metadata) VALUES "
        "('agent-sess', 'assistant', 'Your request was not processed.', 2.0, "
        "'failed_turn', ?)",
        (json.dumps({
            "error": "HTTP 402 out of credits at https://secret.example",
            "error_surface": {"layer": "billing", "code": "billing",
                              "retryable": False},
        }),),
    )
    conn.commit()
    conn.close()
    _reset_failure_caches(monkeypatch)

    sessions = models._load_cli_sessions_uncached(
        fake_agent_home, db, "default"
    )
    assert [s["session_id"] for s in sessions] == ["agent-sess"]
    assert sessions[0]["failure_summary"] == {
        "kind": "failed_turn", "layer": "billing", "code": "billing",
        "retryable": False,
    }
    payload = json.dumps(sessions)
    assert "secret.example" not in payload


def test_list_row_summary_clears_after_external_agent_advancement(
    fake_agent_home, monkeypatch
):
    """Append a genuine successful turn straight into state.db (the Agent
    advancing the session, no WebUI save) → the next list build drops the
    failure marker."""
    db = fake_agent_home / "state.db"
    _make_agent_state_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO sessions (id, title, model, message_count, started_at, "
        "source) VALUES ('agent-sess', 'Agent run', 'gpt-x', 2, 1.0, 'cli')"
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES ('agent-sess', 'user', 'hi', 1.0)"
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, "
        "display_kind, display_metadata) VALUES "
        "('agent-sess', 'assistant', 'Your request was not processed.', 2.0, "
        "'failed_turn', ?)",
        (json.dumps({"error_surface": {"layer": "billing", "retryable": False}}),),
    )
    conn.commit()
    conn.close()
    _reset_failure_caches(monkeypatch)
    # The fingerprint memo would otherwise serve the pre-insert fingerprint
    # for its TTL; force a re-probe so this test observes the Agent's
    # advancement immediately (production tolerates the TTL's freshness lag).
    monkeypatch.setattr(models, "_SESSION_FAILURE_FINGERPRINT_TTL", 0.0)

    first = models._load_cli_sessions_uncached(fake_agent_home, db, "default")
    assert first[0]["failure_summary"]["kind"] == "failed_turn"

    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES ('agent-sess', 'assistant', 'recovered', 3.0)"
    )
    conn.commit()
    conn.close()

    second = models._load_cli_sessions_uncached(fake_agent_home, db, "default")
    assert "failure_summary" not in second[0]


def test_backfill_skips_the_tail_read_when_state_db_is_unchanged(
    tmp_path, monkeypatch
):
    """Revision gate: a repeating poll on an unchanged database issues no
    second tail read at all."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_failed_turn(conn, session_id="sess-1", row_id=1, ts=1000.0)
    conn.commit()
    conn.close()
    _reset_failure_caches(monkeypatch)

    reads = []
    real_open = models.open_state_db_readonly

    def counting_open(db_path, *args, **kwargs):
        reads.append(str(db_path))
        return real_open(db_path, *args, **kwargs)

    monkeypatch.setattr(models, "open_state_db_readonly", counting_open)

    expected = {"kind": "failed_turn", "layer": "billing", "code": "billing",
                "retryable": False}
    first = models._session_failure_summary_from_state_db("sess-1", db)
    second = models._session_failure_summary_from_state_db("sess-1", db)
    assert first == expected
    assert second == expected
    # The first call runs the bounded tail read; the second is answered by
    # the fingerprint/revision gate (the only other connection is the
    # fingerprint probe, which opens its own sqlite3 connection).
    assert len(reads) == 1, reads


def test_backfill_is_bounded_and_never_guesses_beyond_the_window(
    tmp_path, monkeypatch
):
    """The acquisition is a bounded tail read: once every row in the widened
    window is interim activity, it reports no summary instead of scanning the
    whole transcript."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_failed_turn(conn, session_id="sess-1", row_id=1, ts=1000.0)
    for index in range(models._SESSION_FAILURE_TAIL_WINDOW_WIDE + 8):
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp, "
            "tool_calls, active) VALUES (?,?,?,?,?,?,1)",
            (100 + index, "sess-1", "assistant", "", 2000.0 + index,
             json.dumps([{"id": "t%d" % index}])),
        )
    conn.commit()
    conn.close()
    _reset_failure_caches(monkeypatch)

    # The failure sits beyond BOTH bounded windows; the answer must be "no
    # marker", never a guessed verdict.
    assert models._session_failure_summary_from_state_db("sess-1", db) is None


def _make_many_session_state_db(db, *, count=12):
    """``count`` sessions, every third one ending in a failed turn."""
    conn = sqlite3.connect(str(db))
    failed = set()
    for index in range(count):
        sid = "sess-%02d" % index
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) "
            "VALUES (?,?,?,?,?)",
            (index * 2 + 1, sid, "user", "hi", 1000.0 + index),
        )
        if index % 3 == 0:
            failed.add(sid)
            _insert_failed_turn(
                conn, session_id=sid, row_id=index * 2 + 2, ts=1001.0 + index
            )
    conn.commit()
    conn.close()
    return failed


def _counting_state_db_open(monkeypatch):
    """Count every read-only state.db connection the module opens."""
    reads = []
    real_open = models.open_state_db_readonly

    def counting_open(db_path, *args, **kwargs):
        reads.append(str(db_path))
        return real_open(db_path, *args, **kwargs)

    monkeypatch.setattr(models, "open_state_db_readonly", counting_open)
    return reads


def test_list_backfill_reads_every_session_on_one_state_db_open(
    tmp_path, monkeypatch
):
    """The list-level backfill must answer N rows from ONE state.db open.

    Answering a row at a time opened state.db once per session, so a sidebar
    poll over N sessions paid N connections (and N writer-lock waits).
    """
    db = tmp_path / "state.db"
    _make_state_db(db)
    failed = _make_many_session_state_db(db)
    _reset_failure_caches(monkeypatch)
    reads = _counting_state_db_open(monkeypatch)

    rows = [{"session_id": "sess-%02d" % index} for index in range(12)]
    # A row that already carries a summary is left exactly as it is.
    rows.append({"session_id": "sess-99", "failure_summary": {"kind": "kept"}})
    rows.append({"session_id": ""})

    models._backfill_cli_session_failure_summaries(rows, db)

    assert len(reads) == 1, reads
    assert {row["session_id"] for row in rows[:12]
            if "failure_summary" in row} == failed
    assert rows[-2]["failure_summary"] == {"kind": "kept"}
    assert "failure_summary" not in rows[-1]
    marked = rows[0]
    assert marked["failure_summary"] == {
        "kind": "failed_turn", "layer": "billing", "code": "billing",
        "retryable": False,
    }


def test_list_backfill_does_not_wait_out_a_writer_lock(tmp_path, monkeypatch):
    """A briefly-held writer lock costs one fast-failed read, not one 5s
    busy-wait per row (the sidebar poll must never stall on a lock)."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    _make_many_session_state_db(db)
    _reset_failure_caches(monkeypatch)
    reads = _counting_state_db_open(monkeypatch)

    lock = sqlite3.connect(str(db), isolation_level=None)
    lock.execute("BEGIN EXCLUSIVE")
    rows = [{"session_id": "sess-%02d" % index} for index in range(12)]
    try:
        started = time.monotonic()
        models._backfill_cli_session_failure_summaries(rows, db)
        elapsed = time.monotonic() - started
    finally:
        lock.rollback()
        lock.close()

    # One open, answered fast: the batch never re-opens per row.
    assert len(reads) == 1, reads
    assert elapsed < 10.0, elapsed
    # An unreadable database is "unknown", never "known zero": no row invents
    # a verdict, and none of them silently claims a healthy session.
    assert all("failure_summary" not in row for row in rows)


# ── #8603 review round 3 ───────────────────────────────────────────────────
#
# Round 2 of this PR was reviewed against a real server, a real Agent
# SessionDB and a master comparison. Seven findings came back; each one below
# names the finding it closes and drives the producer's real shapes (a locked
# state.db, a production-shaped messages table with its (session_id, id)
# index, pre-#8447 sidecars and index rows, an imported CLI duplicate, the
# Agent's interim row forms, a fail → fail sequence).

import threading  # noqa: E402  (test-module local import, grouped with the r3 tests)


def _make_production_shaped_state_db(path):
    """A state.db shaped like the installed Agent's.

    The bounded tail read is only cheap because ``(session_id, id)`` is
    indexed, and the settled-success discriminator only sees the interim
    markers that are actually columns, so a regression for either has to run
    against the real schema rather than the reduced one above.
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            finish_reason TEXT,
            reasoning TEXT,
            codex_message_items TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            display_kind TEXT,
            display_metadata TEXT
        )
        """
    )
    conn.execute("CREATE INDEX idx_messages_session_id ON messages(session_id, id)")
    conn.commit()
    conn.close()


def _insert_message(conn, *, sid, role, content, ts, tool_calls=None,
                    display_kind=None, display_metadata=None, finish_reason=None,
                    codex_message_items=None, reasoning=None):
    columns = ["session_id", "role", "content", "timestamp"]
    values = [sid, role, content, ts]
    for key, value in (
        ("tool_calls", tool_calls),
        ("display_kind", display_kind),
        ("display_metadata", display_metadata),
        ("finish_reason", finish_reason),
        ("codex_message_items", codex_message_items),
        ("reasoning", reasoning),
    ):
        if value is not None:
            columns.append(key)
            values.append(value)
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO messages ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )


def _failed_turn_metadata(layer="billing", code="billing", retryable=False):
    return json.dumps({
        "error": "HTTP 402: out of credits at https://secret-provider.example/billing",
        "error_surface": {"layer": layer, "code": code, "retryable": retryable,
                          "provider": "custom", "model": "qwen3.8-max"},
    })


class _RecordingCursor:
    """Cursor wrapper that records every statement the reader issues."""

    def __init__(self, real, sink):
        self._real = real
        self._sink = sink

    def execute(self, sql, parameters=()):
        self._sink.append((sql, tuple(parameters or ())))
        return self._real.execute(sql, parameters or ())

    def __getattr__(self, name):
        return getattr(self._real, name)


class _VmStepCountingConnection:
    """Read-only state.db wrapper counting SQLite VM instructions.

    ``set_progress_handler(handler, 1)`` invokes the handler once per VM
    instruction, which is the same cost metric the reviewer measured (2.9M
    steps for the row-numbering batch form on a 500 × 1000 store).
    """

    def __init__(self, real):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "vm_steps", 0)
        object.__setattr__(self, "statements", [])
        real.set_progress_handler(self._tick, 1)

    def _tick(self):
        self.vm_steps += 1
        return 0

    def cursor(self):
        return _RecordingCursor(self._real.cursor(), self.statements)

    def close(self):
        self._real.set_progress_handler(None, 1)
        self._real.close()

    def __setattr__(self, name, value):
        # Forward connection configuration (row_factory, ...) to the real
        # connection: the reader sets row_factory on what open() returned.
        if name in ("_real", "vm_steps", "statements"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _counting_state_db_connections(monkeypatch):
    """Count VM steps and record statements for every state.db the module opens."""
    opened = []
    real_open = models.open_state_db_readonly

    def counting_open(db_path, *args, **kwargs):
        conn = _VmStepCountingConnection(real_open(db_path, *args, **kwargs))
        opened.append(conn)
        return conn

    monkeypatch.setattr(models, "open_state_db_readonly", counting_open)
    return opened


# ── finding 1: the index lock must never wait on state.db ───────────────────

def test_index_rebuild_loads_sessions_outside_the_index_lock(tmp_path, monkeypatch):
    """A full index rebuild loads sidecars OUTSIDE ``_INDEX_WRITE_LOCK``.

    A cold ``Session.load`` opens the Agent's state.db (the failure-summary
    acquisition). With the load inside the index lock, a writer holding
    state.db parked the rebuild — and every other index writer, including an
    unrelated session's save() — for SQLite's 5s busy timeout.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    _write_legacy_sidecar(session_dir, "sess-1", [
        {"role": "user", "content": "hi", "timestamp": 1.0},
    ])

    entered = threading.Event()
    release = threading.Event()
    real_loader = models._load_session_from_path

    def blocking_loader(path):
        entered.set()
        assert release.wait(10), "loader was never released"
        return real_loader(path)

    monkeypatch.setattr(models, "_load_session_from_path", blocking_loader)

    errors = []

    def rebuild():
        try:
            models._write_session_index(updates=None)
        except Exception as exc:  # pragma: no cover - surfaced via errors
            errors.append(exc)

    worker = threading.Thread(target=rebuild, daemon=True)
    worker.start()
    try:
        assert entered.wait(10), "rebuild never reached a session load"
        # The load is in flight: an unrelated index writer must not be blocked
        # behind it.
        assert models._INDEX_WRITE_LOCK.acquire(timeout=2), (
            "session load ran while _INDEX_WRITE_LOCK was held"
        )
        models._INDEX_WRITE_LOCK.release()
    finally:
        release.set()
        worker.join(timeout=10)
    assert not errors, errors
    index = json.loads((session_dir / "_index.json").read_text(encoding="utf-8"))
    assert [e["session_id"] for e in index] == ["sess-1"]


def test_cold_load_does_not_wait_out_a_writer_lock(tmp_path, monkeypatch):
    """A writer holding state.db costs a fast-failed read, not a 5s wait.

    The two single-session readers (tail read and revision probe) inherited
    sqlite3's 5s default busy timeout; the batched list reader already used
    50ms. Both now share that budget, so a cold load — and the index rebuild
    that runs it — cannot be parked behind a writer.
    """
    db = tmp_path / "state.db"
    _make_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_failed_turn(conn, session_id="sess-1", row_id=1, ts=1000.0)
    conn.commit()
    conn.close()

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    _write_legacy_sidecar(session_dir, "sess-1", [
        {"role": "user", "content": "hi", "timestamp": 1.0},
    ])
    _point_models_at(monkeypatch, db)
    _reset_failure_caches(monkeypatch)

    writer = sqlite3.connect(str(db), isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        session = models.Session.load("sess-1")
        elapsed = time.monotonic() - started
    finally:
        writer.rollback()
        writer.close()

    assert session is not None
    # An unreadable database is "unknown", never "known healthy": the load
    # keeps the sidecar's own (absent) state instead of inventing a verdict.
    assert session.failure_summary is None
    assert elapsed < 2.0, elapsed


# ── finding 2: the batch read touches tails, not whole transcripts ──────────

def _row_numbered_tail_steps(cur, sids, limit):
    """The row-numbering batch form this PR replaces (measured baseline)."""
    placeholders = ",".join("?" for _ in sids)
    cur.execute(
        "SELECT session_id, role, content, display_kind, display_metadata, "
        "tool_calls, id FROM ("
        "SELECT session_id, role, content, display_kind, display_metadata, "
        "tool_calls, id, "
        "ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY id DESC) "
        "AS _failure_tail_rank "
        f"FROM messages WHERE session_id IN ({placeholders})"
        f") WHERE _failure_tail_rank <= ? "
        "ORDER BY session_id, _failure_tail_rank",
        (*sids, limit),
    )
    return len(cur.fetchall())


def test_batch_tail_read_uses_indexed_reverse_reads_and_only_widens_undecided(
    tmp_path, monkeypatch
):
    """The list backfill reads a bounded tail per session, not every row.

    Cost assertions (the finding is a cost finding, so an output assertion
    alone would not catch a regression):

    * no statement may number rows with ``ROW_NUMBER()``;
    * every tail statement is a bounded ``ORDER BY <id> DESC LIMIT ?`` read,
      served by the ``(session_id, id)`` index (verified through
      ``EXPLAIN QUERY PLAN``);
    * the VM-step cost must stay an order of magnitude below the
      row-numbering form, measured on the same database in the same test;
    * only tails the narrow window could not decide are widened.
    """
    sessions = 60
    messages = 400
    db = tmp_path / "state.db"
    _make_production_shaped_state_db(db)
    conn = sqlite3.connect(str(db))
    failed = set()
    undecided = set()
    for index in range(sessions):
        sid = "sess-%03d" % index
        for m in range(messages):
            _insert_message(
                conn, sid=sid,
                role="user" if m % 2 == 0 else "assistant",
                content="message %d" % m, ts=float(m),
            )
        if index % 5 == 0:
            # A settled failure at the tail: decided by the narrow window.
            failed.add(sid)
            _insert_message(
                conn, sid=sid, role="assistant",
                content="Your request was not processed.", ts=float(messages),
                display_kind="failed_turn",
                display_metadata=_failed_turn_metadata(),
            )
        elif index % 7 == 0:
            # 30 interim tool rows AFTER the failure: only the wide window can
            # decide this one, so it — and only it — may be widened.
            undecided.add(sid)
            _insert_message(
                conn, sid=sid, role="assistant",
                content="Your request was not processed.", ts=2.0,
                display_kind="failed_turn",
                display_metadata=_failed_turn_metadata(
                    layer="upstream", code="rate_limit", retryable=True,
                ),
            )
            for m in range(30):
                _insert_message(
                    conn, sid=sid, role="assistant", content="", ts=10.0 + m,
                    tool_calls=json.dumps([{"id": "t%d" % m}]),
                )
    conn.commit()
    conn.close()
    _reset_failure_caches(monkeypatch)

    sids = ["sess-%03d" % index for index in range(sessions)]
    opened = _counting_state_db_connections(monkeypatch)

    widen_calls = []
    real_batch = models._state_db_failure_batch_tails

    def recording_batch(cur, cols, active_clause, order_col, chunk, limit, **kwargs):
        widen_calls.append((list(chunk), limit))
        return real_batch(cur, cols, active_clause, order_col, chunk, limit, **kwargs)

    monkeypatch.setattr(models, "_state_db_failure_batch_tails", recording_batch)

    summaries = models._session_failure_summaries_from_state_db(sids, db)

    # Correctness first: every failure is found, and no healthy row invents one.
    for sid in failed:
        assert summaries.get(sid) == {
            "kind": "failed_turn", "layer": "billing", "code": "billing",
            "retryable": False,
        }, sid
    for sid in undecided:
        assert summaries.get(sid) == {
            "kind": "failed_turn", "layer": "upstream", "code": "rate_limit",
            "retryable": True,
        }, sid
    for index in range(sessions):
        sid = "sess-%03d" % index
        if sid not in failed and sid not in undecided:
            assert summaries.get(sid) is None, sid

    # One state.db open for the whole page.
    assert len(opened) == 1, opened
    statements = [sql for conn_proxy in opened for sql, _params in conn_proxy.statements]
    assert not any("ROW_NUMBER" in sql for sql in statements), statements
    tail_statements = [
        sql for sql in statements
        if "FROM messages" in sql and "WHERE session_id = ?" in sql
    ]
    assert len(tail_statements) >= len(sids), len(tail_statements)
    for sql in tail_statements:
        assert "ORDER BY id DESC" in sql and "LIMIT ?" in sql, sql

    # Cost: an order of magnitude below the row-numbering form on the same
    # database, and bounded per session.
    baseline_steps = 0
    for conn_proxy in opened:
        baseline_steps += conn_proxy.vm_steps
    probe = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        baseline_conn = _VmStepCountingConnection(probe)
        try:
            cur = baseline_conn.cursor()
            _row_numbered_tail_steps(
                cur, sids, models._SESSION_FAILURE_TAIL_WINDOW
            )
        finally:
            row_numbered_steps = baseline_conn.vm_steps
    finally:
        probe.close()
    assert row_numbered_steps > 0
    assert baseline_steps * 10 < row_numbered_steps, (
        baseline_steps, row_numbered_steps,
    )
    assert baseline_steps < 3000 * len(sids), (baseline_steps, len(sids))

    # Widening discipline: exactly the undecided tails are re-read wide, and
    # no settled tail is.
    widened = {
        sid for chunk, limit in widen_calls
        if limit == models._SESSION_FAILURE_TAIL_WINDOW_WIDE
        for sid in chunk
    }
    assert widened == undecided, sorted(widened ^ undecided)
    narrow_calls = [
        chunk for chunk, limit in widen_calls
        if limit == models._SESSION_FAILURE_TAIL_WINDOW
    ]
    assert sorted(sid for chunk in narrow_calls for sid in chunk) == sorted(sids)

    # The narrow read is served by the (session_id, id) index, not a scan.
    plan_conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        plan = plan_conn.execute(
            "EXPLAIN QUERY PLAN SELECT role, content FROM messages "
            "WHERE session_id = ? AND (active IS NULL OR active != 0) "
            "ORDER BY id DESC LIMIT ?",
            ("sess-000", models._SESSION_FAILURE_TAIL_WINDOW),
        ).fetchall()
    finally:
        plan_conn.close()
    plan_text = " ".join(str(row[-1]) for row in plan)
    assert "USING INDEX idx_messages_session_id" in plan_text, plan_text
    assert "SCAN" not in plan_text, plan_text


# ── finding 3: a successful retry outside the WebUI clears the marker ───────

def _write_imported_sidecar(session_dir, sid, *, summary):
    """A sidecar as ``/api/session/import_cli`` leaves it: summary derived."""
    payload = {
        "session_id": sid,
        "title": "Imported CLI run",
        "created_at": 1.0,
        "updated_at": 2.0,
        "message_count": 2,
        "is_cli_session": True,
        "source_tag": "cli",
        "messages": [{"role": "user", "content": "hi", "timestamp": 1.0}],
        "failure_summary": summary,
        "failure_summary_revision": 2,
    }
    (session_dir / f"{sid}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_imported_cli_row_summary_clears_after_external_success(
    tmp_path, monkeypatch
):
    """The reviewer's R3 repro, end to end.

    Import a CLI session whose last turn failed, let the Agent append an
    ordinary successful answer straight into state.db, then read the session
    three ways: the list row, a cold open, and a save. Every one of them used
    to keep the stale marker, because a sidecar that already carried a
    ``failure_summary`` was never checked against the database again.
    """
    db = tmp_path / "state.db"
    _make_production_shaped_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_message(conn, sid="cli-sess", role="user", content="hi", ts=1.0)
    _insert_message(
        conn, sid="cli-sess", role="assistant",
        content="Your request was not processed.", ts=2.0,
        display_kind="failed_turn", display_metadata=_failed_turn_metadata(),
    )
    conn.commit()
    conn.close()

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    _point_models_at(monkeypatch, db)
    _reset_failure_caches(monkeypatch)
    stale = {"kind": "failed_turn", "layer": "billing", "code": "billing",
             "retryable": False}
    _write_imported_sidecar(session_dir, "cli-sess", summary=stale)

    # The Agent advances the session: a plain successful answer, no WebUI save.
    conn = sqlite3.connect(str(db))
    _insert_message(conn, sid="cli-sess", role="assistant",
                    content="recovered", ts=3.0)
    conn.commit()
    conn.close()
    # Force a re-probe so the test observes the advancement immediately
    # (production tolerates the fingerprint memo's freshness lag).
    monkeypatch.setattr(models, "_SESSION_FAILURE_FINGERPRINT_TTL", 0.0)
    _reset_failure_caches(monkeypatch)

    # 1) the sidebar row.
    rows = [{"session_id": "cli-sess", "failure_summary": dict(stale)}]
    models._backfill_cli_session_failure_summaries(rows, db)
    assert "failure_summary" not in rows[0], rows[0]

    # 2) a cold open of the session.
    session = models.Session.load("cli-sess")
    assert session is not None
    assert session.failure_summary is None
    # ... and the acquisition still never rewrites the sidecar.
    assert "failure_summary" in json.loads(
        (session_dir / "cli-sess.json").read_text(encoding="utf-8")
    )

    # 3) the next save persists the recovery, so a restart sees it too.
    session.messages = session.messages or []
    session.save()
    on_disk = json.loads(
        (session_dir / "cli-sess.json").read_text(encoding="utf-8")
    )
    assert on_disk["failure_summary"] is None
    # The healthy verdict is stamped with the database revision it was read
    # at, so a later read can tell "checked at 3, healthy" from "never asked".
    assert on_disk["failure_summary_revision"] == 3


def test_unanswerable_state_db_keeps_the_sidecar_summary(tmp_path, monkeypatch):
    """"Healthy" and "unavailable" are different answers.

    A database that cannot speak for the session (no rows for it here) must
    not be read as a recovery: the sidecar keeps its own summary, so a
    transiently unreadable state.db can neither invent nor erase a verdict.
    """
    db = tmp_path / "state.db"
    _make_production_shaped_state_db(db)
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    _point_models_at(monkeypatch, db)
    _reset_failure_caches(monkeypatch)
    stale = {"kind": "failed_turn", "layer": "billing", "code": "billing",
             "retryable": False}
    _write_imported_sidecar(session_dir, "cli-sess", summary=stale)

    session = models.Session.load("cli-sess")
    assert session.failure_summary == stale
    rows = [{"session_id": "cli-sess", "failure_summary": dict(stale)}]
    models._backfill_cli_session_failure_summaries(rows, db)
    assert rows[0]["failure_summary"] == stale
    assert "_failure_summary_answered" not in rows[0]


# ── finding 4: pre-#8447 index rows and imported duplicates ─────────────────

def test_pre_pr_index_rows_are_marked_and_cleared_from_state_db(
    tmp_path, monkeypatch
):
    """An index row written before #8447 must still surface the failure.

    Such a row carries ``last_message_at`` (so the existing backfill never
    reloads it) and no ``failure_summary`` (and a healthy row never gains the
    field), so the index alone cannot tell "never derived" from "derived,
    healthy". The pre-PR reader also dropped display_kind/display_metadata,
    so the sidecar transcript no longer holds the failure either — state.db is
    the only surviving source, and the sidebar must ask it.
    """
    db = tmp_path / "state.db"
    _make_production_shaped_state_db(db)
    conn = sqlite3.connect(str(db))
    for index in range(6):
        sid = "legacy-%d" % index
        _insert_message(conn, sid=sid, role="user", content="hi", ts=1.0)
        if index % 2 == 0:
            _insert_message(
                conn, sid=sid, role="assistant",
                content="Your request was not processed.", ts=2.0,
                display_kind="failed_turn",
                display_metadata=_failed_turn_metadata(),
            )
        else:
            _insert_message(conn, sid=sid, role="assistant",
                            content="all good", ts=2.0)
    conn.commit()
    conn.close()

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    _point_models_at(monkeypatch, db)
    _reset_failure_caches(monkeypatch)
    # Pre-#8447 sidecars: no failure_summary key at all.
    for index in range(6):
        _write_legacy_sidecar(session_dir, "legacy-%d" % index, [
            {"role": "user", "content": "hi", "timestamp": 1.0},
        ])
    # A pre-#8447 index row: complete enough that nothing reloads it.
    index_rows = []
    for index in range(6):
        index_rows.append({
            "session_id": "legacy-%d" % index,
            "title": "Legacy %d" % index,
            "message_count": 2,
            "updated_at": 2.0,
            "last_message_at": 2.0,
            "created_at": 1.0,
        })
    (session_dir / "_index.json").write_text(
        json.dumps(index_rows), encoding="utf-8"
    )

    first = models.all_sessions()
    marked = {
        row["session_id"]: row.get("failure_summary")
        for row in first
    }
    for index in range(6):
        sid = "legacy-%d" % index
        if index % 2 == 0:
            assert marked.get(sid) == {
                "kind": "failed_turn", "layer": "billing", "code": "billing",
                "retryable": False,
            }, (sid, marked.get(sid))
        else:
            assert "failure_summary" not in next(
                row for row in first if row["session_id"] == sid
            ), sid

    # The Agent recovers one of them: the next list build must clear it.
    conn = sqlite3.connect(str(db))
    _insert_message(conn, sid="legacy-0", role="assistant",
                    content="recovered", ts=3.0)
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_SESSION_FAILURE_FINGERPRINT_TTL", 0.0)
    _reset_failure_caches(monkeypatch)

    second = models.all_sessions()
    recovered = next(row for row in second if row["session_id"] == "legacy-0")
    assert "failure_summary" not in recovered, recovered


def test_merge_carries_the_agent_failure_summary_in_both_directions(
    tmp_path, monkeypatch
):
    """Both CLI→WebUI merge paths must carry the failure state.

    The imported duplicate is suppressed in favour of the WebUI row, so
    dropping the Agent row's summary there loses the marker forever (R2). The
    clear direction matters as much as the set direction: a WebUI row whose
    marker the Agent has since recovered must not keep vouching for it.
    """
    import api.routes as routes

    stale = {"kind": "failed_turn", "layer": "billing", "code": "billing",
             "retryable": False}
    fresh = {"kind": "failed_turn", "layer": "upstream", "code": "rate_limit",
             "retryable": True}

    # _merge_cli_sidebar_metadata: the messaging-session path.
    merged = routes._merge_cli_sidebar_metadata(
        {"session_id": "s", "title": "Imported"},
        {"session_id": "s", "failure_summary": dict(fresh),
         "_failure_summary_answered": True},
    )
    assert merged["failure_summary"] == fresh

    # ... and the clear direction: an answered-healthy Agent row.
    merged = routes._merge_cli_sidebar_metadata(
        {"session_id": "s", "title": "Imported", "failure_summary": dict(stale)},
        {"session_id": "s", "_failure_summary_answered": True},
    )
    assert "failure_summary" not in merged

    # ... and an Agent row that could not be answered leaves the row alone.
    merged = routes._merge_cli_sidebar_metadata(
        {"session_id": "s", "title": "Imported", "failure_summary": dict(stale)},
        {"session_id": "s"},
    )
    assert merged["failure_summary"] == stale

    # The four-field copy path, driven through the real list builder with the
    # two sources stubbed the way the models layer would have refreshed them.
    webui_row = {
        "session_id": "dup", "title": "Imported CLI run",
        "message_count": 2, "updated_at": 3.0, "last_message_at": 3.0,
        "created_at": 1.0, "profile": "default",
    }
    agent_row = {
        "session_id": "dup", "title": "Imported CLI run",
        "message_count": 2, "updated_at": 3.0, "last_message_at": 3.0,
        "created_at": 1.0, "profile": "default",
        "source_tag": "cli", "raw_source": "cli", "session_source": "cli",
        "source_label": "CLI", "is_cli_session": True,
        "failure_summary": dict(fresh), "_failure_summary_answered": True,
    }
    monkeypatch.setattr(routes, "all_sessions",
                        lambda diag=None, **kwargs: [dict(webui_row)])
    monkeypatch.setattr(routes, "get_cli_sessions",
                        lambda source_filter=None, all_profiles=False: [dict(agent_row)])
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows",
                        lambda _rows: False)
    monkeypatch.setattr(routes, "_prune_orphaned_webui_zero_message_sessions",
                        lambda rows, **_kwargs: rows)
    payload = routes._build_session_list_cache_payload(
        active_profile="default", all_profiles=False, show_cli_sessions=True,
        show_previous_messaging_sessions=False, show_cron_sessions=False,
    )
    rows = routes._session_list_payload_to_response(payload)["sessions"]
    dup = next(row for row in rows if row["session_id"] == "dup")
    assert dup["failure_summary"] == fresh
    assert "_failure_summary_answered" not in dup

    # Healthy Agent answer → the imported duplicate loses its stale marker.
    healthy_agent = dict(agent_row)
    healthy_agent.pop("failure_summary")
    monkeypatch.setattr(
        routes, "get_cli_sessions",
        lambda source_filter=None, all_profiles=False: [dict(healthy_agent)],
    )
    stale_webui = dict(webui_row)
    stale_webui["failure_summary"] = dict(stale)
    monkeypatch.setattr(routes, "all_sessions",
                        lambda diag=None, **kwargs: [dict(stale_webui)])
    payload = routes._build_session_list_cache_payload(
        active_profile="default", all_profiles=False, show_cli_sessions=True,
        show_previous_messaging_sessions=False, show_cron_sessions=False,
    )
    rows = routes._session_list_payload_to_response(payload)["sessions"]
    dup = next(row for row in rows if row["session_id"] == "dup")
    assert "failure_summary" not in dup, dup


# ── finding 5: interim replies are not recovery ─────────────────────────────

_FAILED_TURN_ROW = {
    "role": "assistant",
    "display_kind": "failed_turn",
    "display_metadata": {"error_surface": {"layer": "billing",
                                           "retryable": False}},
}


@pytest.mark.parametrize("interim", [
    # A content part list that carries reasoning only.
    {"role": "assistant", "content": [
        {"type": "reasoning", "text": "weighing the retry"},
    ]},
    {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "weighing the retry"},
    ]},
    # A Codex commentary row.
    {"role": "assistant", "content": "",
     "codex_message_items": [{"type": "message", "text": "commentary"}]},
    # A provisional verification round.
    {"role": "assistant", "content": "checking the diff",
     "finish_reason": "verification_required"},
    # An unfinished turn.
    {"role": "assistant", "content": "half an answer",
     "finish_reason": "incomplete"},
])
def test_summary_survives_the_four_interim_forms(interim):
    """Each interim form used to read as a settled answer and clear the state."""
    messages = [dict(_FAILED_TURN_ROW), dict(interim)]
    assert models._session_failure_summary(messages) == {
        "kind": "failed_turn", "layer": "billing", "retryable": False,
    }, interim


def test_summary_still_clears_on_a_real_answer():
    """The control group: a genuine answer still clears, and so does an
    ``_error`` settlement row's absence of any claim."""
    assert models._session_failure_summary([
        dict(_FAILED_TURN_ROW),
        {"role": "assistant", "content": "here is the answer"},
    ]) is None
    # A reasoning-only part list followed by the real answer still clears.
    assert models._session_failure_summary([
        dict(_FAILED_TURN_ROW),
        {"role": "assistant", "content": [
            {"type": "reasoning", "text": "thinking"},
        ]},
        {"role": "assistant", "content": "here is the answer"},
    ]) is None
    # ... and the interim row alone, with no failure behind it, is not one.
    assert models._session_failure_summary([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "reasoning", "text": "thinking"},
        ]},
    ]) is None


@pytest.mark.parametrize("interim", [
    {"role": "assistant", "content": "",
     "codex_message_items": json.dumps([{"type": "message", "text": "commentary"}])},
    {"role": "assistant", "content": "checking the diff",
     "finish_reason": "verification_required"},
    {"role": "assistant", "content": "half an answer",
     "finish_reason": "incomplete"},
])
def test_state_db_tail_projector_carries_the_interim_discriminators(
    tmp_path, monkeypatch, interim
):
    """The database tail projector must not drop the interim markers.

    The sidecar path sees ``codex_message_items`` / ``finish_reason`` on the
    row; the bounded tail read projects its own message shape, and a
    discriminator lost there turns a Codex commentary row or a
    ``verification_required`` round into a settled success — clearing a
    failure that is still open.
    """
    db = tmp_path / "state.db"
    _make_production_shaped_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_message(conn, sid="sess-1", role="user", content="hi", ts=1.0)
    _insert_message(
        conn, sid="sess-1", role="assistant",
        content="Your request was not processed.", ts=2.0,
        display_kind="failed_turn", display_metadata=_failed_turn_metadata(),
    )
    _insert_message(conn, sid="sess-1", ts=3.0, **interim)
    conn.commit()
    conn.close()
    _reset_failure_caches(monkeypatch)

    summary, _revision, answered = (
        models._session_failure_state_from_state_db("sess-1", db)
    )
    assert summary == {
        "kind": "failed_turn", "layer": "billing", "code": "billing",
        "retryable": False,
    }, interim
    assert answered is True


# ── finding 6: fail → fail shows the newest failure ─────────────────────────

def test_save_takes_the_newest_failure_surface_from_state_db(tmp_path, monkeypatch):
    """A second failure must replace the first one's surface.

    The Agent settles its own boundary rows, so between the second failure and
    the sidecar write the resident transcript still ends on the FIRST
    ``failed_turn`` row: the marker kept reporting a retryable rate limit
    after a billing failure. state.db is authoritative, so the save re-reads
    it and the newest surface wins.
    """
    db = tmp_path / "state.db"
    _make_production_shaped_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_message(conn, sid="sess-1", role="user", content="first", ts=1.0)
    _insert_message(
        conn, sid="sess-1", role="assistant",
        content="Your request was not processed.", ts=2.0,
        display_kind="failed_turn",
        display_metadata=_failed_turn_metadata(
            layer="upstream", code="rate_limit", retryable=True,
        ),
    )
    conn.commit()
    conn.close()

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    _point_models_at(monkeypatch, db)
    _reset_failure_caches(monkeypatch)

    # The resident transcript as the worker last saved it: the first failure
    # is settled, the second turn is in flight, and the Agent has already
    # stamped the second boundary row into state.db.
    session = models.Session(session_id="sess-1", messages=[
        {"role": "user", "content": "first", "timestamp": 1.0},
        {"role": "assistant", "display_kind": "failed_turn", "timestamp": 2.0,
         "display_metadata": {"error_surface": {"layer": "upstream",
                                                "code": "rate_limit",
                                                "retryable": True}}},
        {"role": "assistant", "content": "**Provider error:** rate limited",
         "timestamp": 3.0, "_error": True},
        {"role": "user", "content": "try again", "timestamp": 4.0},
        {"role": "assistant", "content": "**Provider error:** out of credits",
         "timestamp": 5.0, "_error": True},
    ])
    conn = sqlite3.connect(str(db))
    _insert_message(
        conn, sid="sess-1", role="assistant",
        content="Your request was not processed.", ts=6.0,
        display_kind="failed_turn",
        display_metadata=_failed_turn_metadata(),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_SESSION_FAILURE_FINGERPRINT_TTL", 0.0)
    _reset_failure_caches(monkeypatch)

    session.save()
    assert session.failure_summary == {
        "kind": "failed_turn", "layer": "billing", "code": "billing",
        "retryable": False,
    }
    on_disk = json.loads(
        (session_dir / "sess-1.json").read_text(encoding="utf-8")
    )
    assert on_disk["failure_summary"]["layer"] == "billing"
    assert on_disk["failure_summary"]["retryable"] is False


def test_save_keeps_the_transcript_verdict_while_a_turn_is_in_flight(
    tmp_path, monkeypatch
):
    """The guard on the rule above: an in-flight request is not a recovery.

    The tail ends on a user row, so the database's healthy verdict describes
    the turn BEFORE it. Clearing there would drop the marker exactly when the
    session is failing again.
    """
    db = tmp_path / "state.db"
    _make_production_shaped_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_message(conn, sid="sess-1", role="user", content="first", ts=1.0)
    _insert_message(conn, sid="sess-1", role="assistant",
                    content="all good", ts=2.0)
    _insert_message(conn, sid="sess-1", role="user", content="again", ts=3.0)
    conn.commit()
    conn.close()

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    _point_models_at(monkeypatch, db)
    _reset_failure_caches(monkeypatch)

    session = models.Session(session_id="sess-1", messages=[
        {"role": "user", "content": "first", "timestamp": 1.0},
        {"role": "assistant", "display_kind": "failed_turn", "timestamp": 2.0,
         "display_metadata": {"error_surface": {"layer": "billing",
                                                "retryable": False}}},
        {"role": "user", "content": "again", "timestamp": 3.0},
    ])
    session.save()
    assert session.failure_summary == {
        "kind": "failed_turn", "layer": "billing", "retryable": False,
    }


# ── greptile 2026-11-08: removed turns still look failed ────────────────────

def test_save_does_not_restore_a_cleared_sessions_failure(tmp_path, monkeypatch):
    """Clearing a failed session must not resurrect its marker on save.

    Clearing removes the transcript from the WebUI sidecar and records the
    0.0 truncate-to-empty sentinel, but the Agent's append-only state.db still
    holds the failed_turn row. A database verdict read without the truncation
    window would write the removed failure straight back.
    """
    db = tmp_path / "state.db"
    _make_production_shaped_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_message(conn, sid="sess-clear", role="user", content="hi", ts=1.0)
    _insert_message(
        conn, sid="sess-clear", role="assistant",
        content="Your request was not processed.", ts=2.0,
        display_kind="failed_turn", display_metadata=_failed_turn_metadata(),
    )
    conn.commit()
    conn.close()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    _point_models_at(monkeypatch, db)
    _reset_failure_caches(monkeypatch)

    # The counterfactual: without a truncation window the old row still votes.
    unfiltered, _rev, answered = models._session_failure_state_from_state_db(
        "sess-clear", db,
    )
    assert answered is True
    assert unfiltered["kind"] == "failed_turn"

    session = models.Session(
        session_id="sess-clear",
        messages=[],
        truncation_watermark=0.0,
        truncation_boundary=0.0,
    )
    session.save()
    assert session.failure_summary is None
    on_disk = json.loads(
        (session_dir / "sess-clear.json").read_text(encoding="utf-8")
    )
    assert on_disk.get("failure_summary") is None


def test_cold_load_honors_the_sidecars_truncation_window(tmp_path, monkeypatch):
    """A truncated suffix's failure row cannot speak for the kept transcript.

    ``truncation_watermark`` (== boundary here) is the timestamp of the last
    kept message; the failed turn beyond it was removed by the truncation.
    The load must read only the kept part of state.db — otherwise the removed
    turn's marker comes back.
    """
    db = tmp_path / "state.db"
    _make_production_shaped_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_message(conn, sid="sess-tr", role="user", content="hi", ts=1.0)
    _insert_message(conn, sid="sess-tr", role="assistant", content="all good", ts=2.0)
    _insert_message(
        conn, sid="sess-tr", role="assistant",
        content="Your request was not processed.", ts=50.0,
        display_kind="failed_turn", display_metadata=_failed_turn_metadata(),
    )
    conn.commit()
    conn.close()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    _point_models_at(monkeypatch, db)
    _reset_failure_caches(monkeypatch)
    (session_dir / "sess-tr.json").write_text(json.dumps({
        "session_id": "sess-tr",
        "title": "Truncated",
        "created_at": 1.0,
        "updated_at": 2.0,
        "message_count": 2,
        "messages": [
            {"role": "user", "content": "hi", "timestamp": 1.0},
            {"role": "assistant", "content": "all good", "timestamp": 2.0},
        ],
        "truncation_watermark": 2.0,
        "truncation_boundary": 2.0,
    }), encoding="utf-8")

    # The counterfactual: unfiltered, the removed turn is the newest row.
    unfiltered, _rev, answered = models._session_failure_state_from_state_db(
        "sess-tr", db,
    )
    assert answered is True
    assert unfiltered["kind"] == "failed_turn"

    session = models.Session.load("sess-tr")
    assert session.truncation_watermark == 2.0
    assert session.failure_summary is None


def test_truncation_window_drops_the_deleted_gap_after_an_advanced_watermark(
    tmp_path, monkeypatch
):
    """boundary < watermark: the deleted (boundary, watermark) suffix is dead.

    A new turn may be committed to the sidecar before its state.db rows land,
    so the newest database row can still be a deleted-suffix failure. The read
    keeps the legitimate prefix (ts <= boundary) and the post-edit tail
    (ts >= watermark) only.
    """
    db = tmp_path / "state.db"
    _make_production_shaped_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_message(conn, sid="sess-gap", role="user", content="hi", ts=1.0)
    _insert_message(conn, sid="sess-gap", role="assistant", content="all good", ts=2.0)
    _insert_message(
        conn, sid="sess-gap", role="assistant",
        content="Your request was not processed.", ts=50.0,
        display_kind="failed_turn", display_metadata=_failed_turn_metadata(),
    )
    conn.commit()
    conn.close()
    _reset_failure_caches(monkeypatch)

    unfiltered, _rev, answered = models._session_failure_state_from_state_db(
        "sess-gap", db,
    )
    assert answered is True
    assert unfiltered["kind"] == "failed_turn"

    summary, _revision, answered = models._session_failure_state_from_state_db(
        "sess-gap", db,
        truncation_watermark=100.0,
        truncation_boundary=2.0,
    )
    # The kept prefix ends on a settled answer: an answered health clears.
    assert answered is True
    assert summary is None

    # And the post-edit tail (ts >= watermark) is legitimate again.
    conn = sqlite3.connect(str(db))
    _insert_message(
        conn, sid="sess-gap", role="assistant",
        content="Your request was not processed.", ts=100.0,
        display_kind="failed_turn", display_metadata=_failed_turn_metadata(),
    )
    conn.commit()
    conn.close()
    models._SESSION_FAILURE_FINGERPRINT_CACHE.clear()
    summary, _revision, answered = models._session_failure_state_from_state_db(
        "sess-gap", db,
        truncation_watermark=100.0,
        truncation_boundary=2.0,
    )
    assert answered is True
    assert summary["kind"] == "failed_turn"


def test_compact_and_sidebar_backfill_use_the_rows_truncation_window(
    tmp_path, monkeypatch
):
    """The index row carries its truncation window; the backfill honors it.

    ``compact()`` must emit the watermark (the index previously carried only
    the boundary), otherwise the sidebar backfill cannot tell a deleted
    suffix from a later legitimate turn and restores the removed marker.
    """
    db = tmp_path / "state.db"
    _make_production_shaped_state_db(db)
    conn = sqlite3.connect(str(db))
    _insert_message(conn, sid="sess-row", role="user", content="hi", ts=1.0)
    _insert_message(conn, sid="sess-row", role="assistant", content="all good", ts=2.0)
    _insert_message(
        conn, sid="sess-row", role="assistant",
        content="Your request was not processed.", ts=50.0,
        display_kind="failed_turn", display_metadata=_failed_turn_metadata(),
    )
    conn.commit()
    conn.close()
    _point_models_at(monkeypatch, db)
    _reset_failure_caches(monkeypatch)

    session = models.Session(
        session_id="sess-row",
        title="Truncated",
        messages=[
            {"role": "user", "content": "hi", "timestamp": 1.0},
            {"role": "assistant", "content": "all good", "timestamp": 2.0},
        ],
        truncation_watermark=2.0,
        truncation_boundary=2.0,
    )
    row = session.compact()
    assert row["truncation_watermark"] == 2.0
    assert row["truncation_boundary"] == 2.0
    row.pop("failure_summary", None)  # a pre-#8447 index row

    models._backfill_sidebar_failure_summaries([row])
    assert "failure_summary" not in row

    # Counterfactual: the same row WITHOUT its window is marked failed, which
    # proves the database really is reporting the removed turn.
    unmarked = models.Session(
        session_id="sess-row",
        title="Truncated",
        messages=[
            {"role": "user", "content": "hi", "timestamp": 1.0},
            {"role": "assistant", "content": "all good", "timestamp": 2.0},
        ],
    ).compact()
    unmarked.pop("failure_summary", None)
    assert "truncation_watermark" not in unmarked
    models._backfill_sidebar_failure_summaries([unmarked])
    assert unmarked["failure_summary"]["kind"] == "failed_turn"


# ── greptile 2026-11-08: profiles show the wrong failures ───────────────────

def test_sidebar_backfill_reads_each_profiles_own_database(tmp_path, monkeypatch):
    """All-profiles rows must be answered from their OWN profile's state.db.

    Two profiles can hold the same session id; reading only the active
    profile's database marks the foreign row with the wrong failure and can
    overwrite it with the active profile's answer. A profile whose database is
    absent is unknown — never answered from another profile.
    """
    import api.profiles as profiles

    root_home = tmp_path / "root"
    beta_home = tmp_path / "root" / "profiles" / "beta"
    root_home.mkdir(parents=True)
    beta_home.mkdir(parents=True)
    root_db = root_home / "state.db"
    beta_db = beta_home / "state.db"
    _make_production_shaped_state_db(root_db)
    _make_production_shaped_state_db(beta_db)

    conn = sqlite3.connect(str(root_db))
    _insert_message(conn, sid="shared", role="user", content="hi", ts=1.0)
    _insert_message(
        conn, sid="shared", role="assistant",
        content="Your request was not processed.", ts=2.0,
        display_kind="failed_turn",
        display_metadata=_failed_turn_metadata(
            layer="billing", code="billing", retryable=False,
        ),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(beta_db))
    _insert_message(conn, sid="shared", role="user", content="hi", ts=1.0)
    _insert_message(conn, sid="shared", role="assistant", content="all good", ts=2.0)
    _insert_message(conn, sid="beta-only", role="user", content="yo", ts=1.0)
    _insert_message(
        conn, sid="beta-only", role="assistant",
        content="Your request was not processed.", ts=2.0,
        display_kind="failed_turn",
        display_metadata=_failed_turn_metadata(
            layer="upstream", code="rate_limit", retryable=True,
        ),
    )
    conn.commit()
    conn.close()

    def fake_home(name):
        if name == "beta":
            return beta_home
        if name in (None, "", "default"):
            return root_home
        return tmp_path / "root" / "profiles" / name

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", fake_home)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name == "default")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: root_home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    _reset_failure_caches(monkeypatch)

    rows = [
        {"session_id": "shared", "profile": "default"},
        {"session_id": "shared", "profile": "beta"},
        {"session_id": "beta-only", "profile": "beta"},
        {"session_id": "gamma-only", "profile": "gamma"},
    ]
    models._backfill_sidebar_failure_summaries(rows)

    # The root row keeps the root database's billing surface.
    assert rows[0]["failure_summary"] == {
        "kind": "failed_turn", "layer": "billing", "code": "billing",
        "retryable": False,
    }
    # The beta row with the SAME session id is healthy in its own database: it
    # must not adopt the root row's marker, and an answered health clears it.
    assert "failure_summary" not in rows[1]
    assert rows[1]["_failure_summary_answered"] is True
    # A beta-only failure is invisible to the active (root) database; only the
    # beta database can mark it.
    assert rows[2]["failure_summary"]["layer"] == "upstream"
    assert rows[2]["failure_summary"]["retryable"] is True
    # A profile with no database cannot speak: the row is left untouched.
    assert "failure_summary" not in rows[3]
    assert "_failure_summary_answered" not in rows[3]


# ── greptile 2026-11-04: concurrent index writes / rebuild races ────────────

def test_index_fast_path_holds_the_write_lock(tmp_path, monkeypatch):
    """The targeted index patch runs under ``_INDEX_WRITE_LOCK``.

    Callers such as the sidebar backfill do not take the lock themselves, so
    an unlocked read-modify-write let two writers read the same old index and
    the last write drop the other's update.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    session = models.Session(
        session_id="sess-lock",
        messages=[{"role": "user", "content": "hi"}],
    )
    session.save()  # first save rebuilds the index

    observed = []
    real_patch = models._patch_session_index_in_place

    def spy(updates, session_index_file, _tmp):
        result = []

        def probe():
            acquired = models._INDEX_WRITE_LOCK.acquire(timeout=0.3)
            result.append(acquired)
            if acquired:
                models._INDEX_WRITE_LOCK.release()

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join()
        observed.append(result[0])
        return real_patch(updates, session_index_file, _tmp)

    monkeypatch.setattr(models, "_patch_session_index_in_place", spy)
    # Direct callers (all_sessions()' backfill, workspace recovery) do not take
    # the lock themselves; only _write_session_index can serialise them.
    models._write_session_index(updates=[session])
    assert observed == [False], (
        "the index fast path wrote without holding _INDEX_WRITE_LOCK"
    )


def test_rebuild_keeps_a_concurrent_writers_newer_entry(tmp_path, monkeypatch):
    """A rebuild snapshot must not clobber a row written while it was loading.

    The rebuild reads sidecars outside the index lock. If a writer publishes a
    newer entry for a row already read by the rebuild, the stale snapshot must
    not replace it; rows the snapshot alone knows are still added.
    """
    db = tmp_path / "no-state.db"
    _point_models_at(monkeypatch, db)
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _point_session_dir_at(monkeypatch, session_dir)
    _write_legacy_sidecar(session_dir, "sess-a", [
        {"role": "user", "content": "hi", "timestamp": 1.0},
    ])
    _write_legacy_sidecar(session_dir, "sess-b", [
        {"role": "user", "content": "yo", "timestamp": 1.0},
    ])
    # An index that predates the rebuild: sess-a only, with the old snapshot
    # values the rebuild will read.
    (session_dir / "_index.json").write_text(json.dumps([{
        "session_id": "sess-a",
        "title": "Old",
        "updated_at": 1.0,
        "last_message_at": 1.0,
        "message_count": 1,
    }]), encoding="utf-8")

    entered = threading.Event()
    release = threading.Event()
    real_loader = models._load_session_from_path

    def blocking_loader(path):
        if path.stem == "sess-b":
            entered.set()
            assert release.wait(10), "rebuild was never released"
        return real_loader(path)

    monkeypatch.setattr(models, "_load_session_from_path", blocking_loader)

    errors = []

    def rebuild():
        try:
            models._write_session_index(updates=None)
        except Exception as exc:  # pragma: no cover - surfaced via errors
            errors.append(exc)

    worker = threading.Thread(target=rebuild, daemon=True)
    worker.start()
    try:
        assert entered.wait(10), "rebuild never reached sess-b"
        # While sess-b loads, sess-a saves: the index now holds a newer entry
        # than the snapshot the rebuild already read.
        models._write_session_index(updates=[{
            "session_id": "sess-a",
            "title": "Renamed",
            "updated_at": 999.0,
            "last_message_at": 999.0,
            "message_count": 1,
        }])
    finally:
        release.set()
        worker.join(timeout=10)
    assert not errors, errors

    index = json.loads(
        (session_dir / "_index.json").read_text(encoding="utf-8")
    )
    by_id = {entry["session_id"]: entry for entry in index}
    assert by_id["sess-a"]["title"] == "Renamed", by_id["sess-a"]
    assert by_id["sess-a"]["updated_at"] == 999.0, by_id["sess-a"]
    # The snapshot row the concurrent write could not know about is added.
    assert by_id["sess-b"]["title"] == "Legacy", by_id["sess-b"]


# ── greptile 2026-11-04 (P2): warm refreshes reread saved history ───────────

def test_warm_refresh_reads_bounded_tails_after_an_unrelated_append(
    tmp_path, monkeypatch
):
    """An unrelated append must not re-scan every cached session's history.

    The old warm-cache revision probe used ``MAX(id) ... GROUP BY session_id``
    over the requested sessions — a full-history scan. The per-session tail
    read now doubles as the revision probe: bounded reverse reads only.
    """
    db = tmp_path / "state.db"
    _make_production_shaped_state_db(db)
    conn = sqlite3.connect(str(db))
    sids = ["sess-%02d" % index for index in range(24)]
    for index, sid in enumerate(sids):
        for message_index in range(120):
            _insert_message(
                conn, sid=sid,
                role="user" if message_index % 2 == 0 else "assistant",
                content="message %d" % message_index,
                ts=float(message_index),
            )
        if index % 3 == 0:
            _insert_message(
                conn, sid=sid, role="assistant",
                content="Your request was not processed.", ts=200.0,
                display_kind="failed_turn",
                display_metadata=_failed_turn_metadata(),
            )
    conn.commit()
    conn.close()
    _reset_failure_caches(monkeypatch)

    first = models._session_failure_summaries_from_state_db(sids, db)
    assert first["sess-00"] == {
        "kind": "failed_turn", "layer": "billing", "code": "billing",
        "retryable": False,
    }
    assert first["sess-01"] is None

    # An unrelated append elsewhere in the database moves the database-wide
    # fingerprint, so every cached entry is revalidated.
    conn = sqlite3.connect(str(db))
    _insert_message(conn, sid="sess-unrelated", role="user", content="noise", ts=1.0)
    conn.commit()
    conn.close()
    models._SESSION_FAILURE_FINGERPRINT_CACHE.clear()

    opened = _counting_state_db_connections(monkeypatch)
    second = models._session_failure_summaries_from_state_db(sids, db)

    assert second == first
    statements = [sql for proxy in opened for sql, _params in proxy.statements]
    assert not any("GROUP BY" in sql for sql in statements), statements
    assert not any("MAX(" in sql for sql in statements), statements
    tail_statements = [sql for sql in statements if "FROM messages" in sql]
    assert len(tail_statements) >= len(sids), len(tail_statements)
    for sql in tail_statements:
        assert "ORDER BY id DESC" in sql and "LIMIT ?" in sql, sql
        assert "session_id = ?" in sql, sql
    steps = sum(proxy.vm_steps for proxy in opened)
    assert steps < 3000 * len(sids), (steps, len(sids))
