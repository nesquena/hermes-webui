"""#8195: a Desktop-created session whose in-place compaction ran twice loses the
real user message in WebUI.

Reporter shape (issue #8195), session ``20261012_115121_7e083f``:

    id order  user(orig, active=0/compacted=1)
              -> marker1 (active=0/compacted=1)
              -> user copy, same text+timestamp (active=0/compacted=1)
              -> marker2 (active)
              -> assistant (active)

Compaction #2 archived every active row and re-inserted its own transcript
without re-carrying the user row compaction #1 replayed, so NO active copy of
the user message survives.  Desktop renders it (hermes-state's display
projection reads ``active = 1 OR compacted = 1``, one row per ``display_order``
group); WebUI's display path read only ``active = 1`` and therefore dropped it.

These tests pin the parity contract: the display path surfaces
compaction-archived display history, while the model-context reader and the
settled-state reconciliation keep excluding it.
"""

from __future__ import annotations

import sqlite3

import api.models as models
import api.routes as routes
from api.models import Session, get_state_db_session_messages, reconciled_state_db_messages_for_session

SID = "20261012_115121_7e083f"
USER_MSG = "最近發生什麼？"
MARKER_1 = "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below."
MARKER_2 = "[CONTEXT COMPACTION — REFERENCE ONLY] Later turns were compacted into the summary below."

# (id implicit, role, content, timestamp, active, compacted, display_order)
SHAPE = [
    ("user", USER_MSG, 100.0, 0, 1, 10),          # original row, archived by compaction #1
    ("user", MARKER_1, 200.0, 0, 1, 20),          # compaction #1 marker, archived by compaction #2
    ("user", USER_MSG, 100.0, 0, 1, 10),          # compaction #1 replay, archived by compaction #2
    ("user", MARKER_2, 300.0, 1, 0, 30),          # compaction #2 marker (live)
    ("assistant", "final answer", 305.0, 1, 0, 40),  # live reply
]


def _make_state_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT,
            started_at REAL, message_count INTEGER, cwd TEXT, ended_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL,
            active INTEGER DEFAULT 1,
            compacted INTEGER DEFAULT 0,
            display_order INTEGER,
            display_identity TEXT,
            display_metadata TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count, cwd) VALUES (?,?,?,?,?,?,?)",
        (SID, "desktop", "Desktop chat", "m", 90.0, len(SHAPE), "/home/u/proj"),
    )
    for index, (role, content, timestamp, active, compacted, display_order) in enumerate(SHAPE, start=1):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active, compacted, display_order, display_identity) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (SID, role, content, timestamp, active, compacted, display_order, f"identity-{display_order}"),
        )
    conn.commit()
    conn.close()


def test_display_reader_surfaces_compaction_archived_rows(tmp_path, monkeypatch):
    """Display reads include compaction-archived history, de-duplicated per display_order group."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    messages = get_state_db_session_messages(SID, include_compacted=True)

    # Both rows of display_order group 10 are the same logical user message; the
    # group must project once (the newest row wins), exactly like hermes-state's
    # display read.  marker1 was archived by compaction #2 and must come back.
    assert [m["content"] for m in messages] == [MARKER_1, USER_MSG, MARKER_2, "final answer"]


def test_session_endpoint_renders_user_row_lost_by_second_compaction(tmp_path, monkeypatch):
    """GET /api/session renders the user message for the reporter's exact shape (#8195)."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db)

    session = Session(session_id=SID, messages=[], tool_calls=[])
    session.session_source = "desktop"
    captured = {}
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda s: None)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda s: False)
    monkeypatch.setattr(routes, "_resolve_effective_session_model_for_display", lambda s: None)
    monkeypatch.setattr(routes, "_resolve_effective_session_model_provider_for_display", lambda s: None)
    monkeypatch.setattr(routes, "_merge_cli_sidebar_metadata", lambda raw, meta: raw)
    monkeypatch.setattr(routes, "redact_session_data", lambda raw: raw)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200: captured.setdefault("payload", payload))

    class Handler:
        pass

    class Parsed:
        path = "/api/session"
        query = f"session_id={SID}&messages=1"

    routes.handle_get(Handler(), Parsed())

    contents = [m.get("content") for m in captured["payload"]["session"]["messages"]]
    assert USER_MSG in contents, contents
    assert contents.count(USER_MSG) == 1, contents


def test_claimed_session_keeps_archived_user_prompt_in_front_of_sidecar(tmp_path, monkeypatch):
    """A sidecar materialized from the post-compaction active rows must not hide
    the archived user prompt (#8195).

    ``_claim_or_synthesize_cli_session`` snapshots the ACTIVE transcript, so the
    sidecar starts at the second compaction marker. The append-only merge then
    refused to place any older state.db row before the sidecar start, which is
    the exact moment the user message disappeared from WebUI.
    """
    db = tmp_path / "state.db"
    _make_state_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db)

    session = Session(
        session_id=SID,
        messages=[
            {"role": "user", "content": MARKER_2, "timestamp": 300.0},
            {"role": "assistant", "content": "final answer", "timestamp": 305.0},
        ],
        tool_calls=[],
    )
    session.session_source = "desktop"
    captured = {}
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda s: None)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda s: False)
    monkeypatch.setattr(routes, "_resolve_effective_session_model_for_display", lambda s: None)
    monkeypatch.setattr(routes, "_resolve_effective_session_model_provider_for_display", lambda s: None)
    monkeypatch.setattr(routes, "_merge_cli_sidebar_metadata", lambda raw, meta: raw)
    monkeypatch.setattr(routes, "redact_session_data", lambda raw: raw)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200: captured.setdefault("payload", payload))

    class Handler:
        pass

    class Parsed:
        path = "/api/session"
        query = f"session_id={SID}&messages=1"

    routes.handle_get(Handler(), Parsed())

    contents = [m.get("content") for m in captured["payload"]["session"]["messages"]]
    assert contents.count(USER_MSG) == 1, contents
    assert contents.index(USER_MSG) < contents.index(MARKER_2), contents
    assert contents[-1] == "final answer", contents


def test_get_without_webui_sidecar_serves_archived_user_prompt(tmp_path, monkeypatch):
    """First-ever GET of a Desktop session (no WebUI sidecar, KeyError branch).

    ``_claim_or_synthesize_cli_session`` builds ``synth.messages`` from
    ``get_cli_session_messages()``, which is the active-only read. For the
    reporter's shape that stub is the WHOLE response, so the user message never
    reaches the renderer even after the display-projection reader exists.
    """
    db = tmp_path / "state.db"
    _make_state_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db)

    captured = {}

    def _no_sidecar(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(routes, "get_session", _no_sidecar)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_profile_agnostic_foreign_session", lambda meta: False)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda profile, handler: True)
    monkeypatch.setattr(routes, "_merge_cli_sidebar_metadata", lambda raw, meta: raw)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200: captured.setdefault("payload", payload))

    class Handler:
        pass

    class Parsed:
        path = "/api/session"
        query = f"session_id={SID}&messages=1"

    routes.handle_get(Handler(), Parsed())

    payload = captured.get("payload") or {}
    contents = [m.get("content") for m in (payload.get("session") or {}).get("messages", [])]
    assert USER_MSG in contents, contents
    assert contents.count(USER_MSG) == 1, contents
    assert contents.index(USER_MSG) < contents.index(MARKER_2), contents


def test_model_context_reader_still_excludes_compaction_archive(tmp_path, monkeypatch):
    """The default (model-context) read must NOT resurrect archived rows."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    messages = get_state_db_session_messages(SID)

    assert [m["content"] for m in messages] == [MARKER_2, "final answer"]


def test_reconciled_context_does_not_gain_archived_rows(tmp_path, monkeypatch):
    """Settled-state reconciliation stays on the active set — no compression re-trigger."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    session = Session(
        session_id=SID,
        messages=[{"role": "user", "content": "live sidecar tail", "timestamp": 400.0}],
    )

    reconciled = reconciled_state_db_messages_for_session(session)

    contents = [m["content"] for m in reconciled]
    assert MARKER_1 not in contents, contents
    assert USER_MSG not in contents, contents
    assert [m["content"] for m in get_state_db_session_messages(SID)] == [MARKER_2, "final answer"]
