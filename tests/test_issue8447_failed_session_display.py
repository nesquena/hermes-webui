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
