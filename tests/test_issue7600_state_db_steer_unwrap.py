"""state.db-sourced steer rows must not re-surface the raw OOB wrapper (#7600).

The #7600 fix unwraps a consumed mid-turn steer on the *settle* path
(`api.streaming._unwrap_steer_row_oob_marker`), which only fires for rows
carrying ``display_kind == "steer"``. The other half of the transcript —
rows read back out of the Agent's ``state.db`` by
``api.models.get_state_db_session_messages`` → ``_project_state_db_message``
— drops ``display_kind`` entirely, so a steer row persisted by the Agent
reaches the chat UI wrapped, and the settle scrub can never heal it
because the type signal is gone by then.

That is the exact "reappearing raw marker" case the #7600 gate asked for:
a CLI/gateway session (sidecar empty, state.db authoritative) whose steer
row is rendered with the control wrapper visible.

Contract asserted here:
  * a typed steer row (``role=user, display_kind="steer"``) read from
    state.db has exactly one validated frame unwrapped, inner text
    preserved verbatim, exactly once;
  * everything else — untyped user text that merely quotes a complete
    marker, legacy tool carriers, malformed/nested/multiple frames — is
    preserved byte-for-byte (the #7600 gate's destructive-scrub rule).
"""
from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.requires_agent_modules

OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once "
    "at this position; not tool output and not a new delivery when replayed from "
    "conversation history]"
)
CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
STEER_TEXT = "use the staging bucket this time"
WRAPPED = f"{OPEN}\n{STEER_TEXT}\n{CLOSE}"


def _contains_oob(value) -> bool:
    return "OUT-OF-BAND USER MESSAGE" in json.dumps(value, default=str)


def _make_state_db(path: Path, sid: str, rows) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
        "model TEXT, started_at REAL, message_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, role TEXT, content TEXT, timestamp REAL, "
        "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, api_content TEXT, "
        "display_kind TEXT, active INTEGER DEFAULT 1)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, "
        "message_count) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, "cli", "Steer Projection", "test-model", 1000.0, len(rows)),
    )
    for row in rows:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, "
            "tool_call_id, tool_calls, tool_name, api_content, display_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                row["role"],
                row["content"],
                row.get("timestamp", 1000.0),
                row.get("tool_call_id"),
                row.get("tool_calls"),
                row.get("tool_name"),
                row.get("api_content"),
                row.get("display_kind"),
            ),
        )
    conn.commit()
    conn.close()


def _install_session(monkeypatch, tmp_path, sid, sidecar_messages):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    session_dir.mkdir(parents=True, exist_ok=True)

    session = models.Session(
        session_id=sid,
        title="Steer Projection",
        workspace=str(tmp_path),
        model="test-model",
        messages=list(sidecar_messages),
        created_at=1000.0,
        updated_at=1001.0,
    )
    session.save(touch_updated_at=False)
    return session


def _steer_state_rows():
    return [
        {"role": "user", "content": "deploy the app", "timestamp": 1000.0},
        {"role": "assistant", "content": "working", "timestamp": 1001.0},
        {
            "role": "user",
            "content": WRAPPED,
            "display_kind": "steer",
            "timestamp": 1002.5,
        },
        {"role": "assistant", "content": "done", "timestamp": 1003.0},
    ]


def test_state_db_projection_unwraps_typed_steer_row(monkeypatch, tmp_path):
    """The canonical state.db reader must hand the display a steer row whose
    validated frame is unwrapped and whose inner text survives exactly once."""
    import api.models as models

    sid = "steer_projection_unwrap_001"
    _install_session(monkeypatch, tmp_path, sid, [])
    _make_state_db(tmp_path / "state.db", sid, _steer_state_rows())

    rows = models.get_state_db_session_messages(sid)

    assert not _contains_oob(rows), (
        "raw [OUT-OF-BAND USER MESSAGE] wrapper leaked from the state.db "
        "projection into the display transcript"
    )
    steer_rows = [r for r in rows if r.get("role") == "user" and STEER_TEXT in str(r.get("content") or "")]
    assert len(steer_rows) == 1, f"steer text must survive exactly once, got {steer_rows!r}"
    assert steer_rows[0]["content"] == STEER_TEXT
    assert steer_rows[0].get("display_kind") == "steer", (
        "the steer type must ride the projected row so the settle scrub can "
        "still identify it"
    )


def test_state_db_only_session_reconciles_to_a_clean_display_list(monkeypatch, tmp_path):
    """A CLI/gateway session (empty sidecar, state.db authoritative) must not
    show the wrapper — and the settle scrub must be able to heal it afterwards."""
    import api.models as models

    sid = "steer_projection_cli_001"
    session = _install_session(monkeypatch, tmp_path, sid, [])
    session.is_cli_session = True
    _make_state_db(tmp_path / "state.db", sid, _steer_state_rows())

    display = models.reconciled_state_db_messages_for_session(session)

    assert not _contains_oob(display), (
        "the reconciled display list re-surfaced the raw steer wrapper"
    )
    steer_rows = [r for r in display if r.get("role") == "user" and STEER_TEXT in str(r.get("content") or "")]
    assert len(steer_rows) == 1
    assert steer_rows[0]["content"] == STEER_TEXT

    # The row must stay identifiable as a steer so a later settle can scrub it.
    from api.streaming import _strip_oob_markers_from_messages

    healed = [{"role": "user", "content": WRAPPED, "display_kind": "steer"}]
    _strip_oob_markers_from_messages(healed)
    assert healed[0]["content"] == STEER_TEXT, (
        "settling must still be able to unwrap a steer row read from state.db"
    )


def test_untyped_literal_marker_row_survives_projection_byte_for_byte(monkeypatch, tmp_path):
    """A user turn that legitimately quotes a complete marker is NOT a steer
    row: the projection must not touch it (the #7600 gate's corruption rule)."""
    import api.models as models

    sid = "steer_projection_literal_001"
    _install_session(monkeypatch, tmp_path, sid, [])
    literal = (
        "our bot log shows this block, is that normal?\n"
        f"{WRAPPED}\n"
        "the docs say the gateway adds it"
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [{"role": "user", "content": literal, "timestamp": 1000.0}],
    )

    rows = models.get_state_db_session_messages(sid)

    assert rows[0]["content"] == literal, (
        "an untyped user row quoting a marker must be preserved byte-for-byte"
    )


def test_legacy_tool_carrier_survives_projection_byte_for_byte(monkeypatch, tmp_path):
    """Legacy tool-carrier rows are left alone until materialized as a typed
    steer row (#7600: extraction, never deletion)."""
    import api.models as models

    sid = "steer_projection_legacy_tool_001"
    _install_session(monkeypatch, tmp_path, sid, [])
    tool_body = f"checks passed\n\n{WRAPPED}"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [{"role": "tool", "content": tool_body, "timestamp": 1000.0, "tool_call_id": "call-1"}],
    )

    rows = models.get_state_db_session_messages(sid)

    assert rows[0]["content"] == tool_body, (
        "a legacy tool carrier must be preserved byte-for-byte"
    )


def test_malformed_steer_frame_survives_projection_byte_for_byte(monkeypatch, tmp_path):
    """Nested / multiple / unterminated frames on a typed steer row degrade to
    byte-for-byte preservation — never truncation or deletion."""
    import api.models as models

    variant = f"[OUT-OF-BAND USER MESSAGE]{STEER_TEXT}{CLOSE}"
    cases = {
        "nested": f"{OPEN}\nhello {variant} world\n{CLOSE}",
        "multiple": f"{variant} and {WRAPPED}",
        "unterminated": "[OUT-OF-BAND USER MESSAGE — truncated",
    }
    for name, body in cases.items():
        sid = f"steer_projection_malformed_{name}"
        _install_session(monkeypatch, tmp_path, sid, [])
        _make_state_db(
            tmp_path / "state.db",
            sid,
            [{"role": "user", "content": body, "display_kind": "steer", "timestamp": 1000.0}],
        )
        rows = models.get_state_db_session_messages(sid)
        assert rows[0]["content"] == body, f"{name} frame was corrupted: {rows[0]['content']!r}"


class _GetHandler:
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

    @property
    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))

    @property
    def query(self):
        return parse_qs(urlparse(self.path).query)

    def log_message(self, *args, **kwargs):
        pass


def test_session_get_returns_clean_steer_row_for_cli_session(monkeypatch, tmp_path):
    """End-to-end at the surface the UI reads: GET /api/session must return the
    steer text with no raw wrapper for a state.db-authoritative session."""
    import api.routes as routes

    sid = "steer_projection_session_get_001"
    _install_session(monkeypatch, tmp_path, sid, [])
    _make_state_db(tmp_path / "state.db", sid, _steer_state_rows())

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]

    assert not _contains_oob(messages), (
        "GET /api/session still renders the raw [OUT-OF-BAND USER MESSAGE] wrapper"
    )
    steer_rows = [m for m in messages if m.get("role") == "user" and STEER_TEXT in str(m.get("content") or "")]
    assert len(steer_rows) == 1
    assert steer_rows[0]["content"] == STEER_TEXT
