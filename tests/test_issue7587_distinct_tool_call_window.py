"""PR #8672 review P2: distinct tool calls across different-second rows.

#7587 gave the visible/content keys an occurrence discriminator so two
independent same-content turns stop collapsing into one identity.  The
in-window tool-call exception inside
``merge_session_messages_append_only`` compares the CONTENT key of an
incoming state.db tool-call row against the keys the sidecar registered;
that comparison has to stay content-only.  A sidecar tool row and the
durable row for a DIFFERENT tool call in the same turn carry identical
(empty) content but legitimately different second-level clocks, so once
the content key became occurrence-sensitive the membership test failed,
the incoming row fell out of the in-window rejection branch, and the
distinct tool call was silently dropped.

These tests deliberately use DIFFERENT seconds for the two tool rows
(``tests/test_merge_key_tool_calls.py`` pins the same-second shape, which
is why it stayed green), and they walk the three paths the review
reproduced: the bare merge, a real SQLite projection feeding that merge,
the display reconciliation (``/api/session``) and the context
reconciliation (the ``prefer_context=True`` function the streaming worker
builds the agent history from).  Same-second and pre-window controls pin
the behaviour that must NOT change.

The round-2 P2 regression added the EMPTY-ANCHOR shape to every path:
journal recovery stores the recovered tool cards on the session against
an EMPTY assistant anchor that carries NO inline tool_calls, so the
content-only membership set behind the in-window exception must be seeded
from every row -- excluding such anchors from the set dropped the
incoming tool call.  The fix derives the occurrence-free membership key
from the already-computed content key (occurrence slot cleared) instead
of narrowing the set or adding a second normalisation pass.
"""

import json
import sqlite3
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.requires_agent_modules


def _assistant_tool_call(tc_id: str, fn_name: str, timestamp) -> dict:
    """Assistant tool-call-only row: empty content, one tool invocation."""
    return {
        "role": "assistant",
        "content": "",
        "timestamp": timestamp,
        "tool_calls": [
            {"id": tc_id, "function": {"name": fn_name, "arguments": "{}"}},
        ],
    }


def _final_answer(timestamp) -> dict:
    return {"role": "assistant", "content": "final answer", "timestamp": timestamp}


def _empty_anchor(timestamp) -> dict:
    """Journal-recovery anchor: empty assistant content, no inline tool_calls.

    Journal recovery deliberately stores the recovered tool cards on the
    session against such an anchor instead of inline tool_calls, so the
    in-window distinct-tool-call exception must still be able to match it
    (#8672 review P2).
    """
    return {"role": "assistant", "content": "", "timestamp": timestamp}


def _tool_call_ids(messages) -> list:
    return [
        tc.get("id")
        for m in messages
        for tc in (m.get("tool_calls") or [])
    ]


# ── SQLite / session fixtures (mirrors the reconciliation test modules) ──────


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


def _make_state_db(path: Path, sid: str, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL, message_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, "webui", "8672 Tool Calls", "test-model", 1000.0, len(rows)),
    )
    for row in rows:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, tool_call_id, tool_calls, tool_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                row["role"],
                row["content"],
                row.get("timestamp", 1000.0),
                row.get("tool_call_id"),
                row.get("tool_calls"),
                row.get("tool_name"),
            ),
        )
    conn.commit()
    conn.close()


def _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages, context_messages=None):
    import api.config as config
    import api.models as models
    import api.routes as routes
    import api.profiles as profiles

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
        title="8672 Tool Calls",
        workspace=str(tmp_path),
        model="test-model",
        messages=sidecar_messages,
        created_at=1000.0,
        updated_at=1001.0,
    )
    if context_messages is not None:
        session.context_messages = list(context_messages)
    session.save(touch_updated_at=False)
    return session


# ── 1. The bare merge (the review's minimal repro) ───────────────────────────


def test_distinct_tool_calls_at_different_seconds_both_survive_merge():
    """P2 repro: a different-second state.db tool row must not be dropped."""
    from api.models import merge_session_messages_append_only

    sidecar = [
        _assistant_tool_call("call_1", "read_file", 1000.0),
        _final_answer(1002.0),
    ]
    state = [
        _assistant_tool_call("call_2", "terminal", 1001.0),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert _tool_call_ids(merged) == ["call_1", "call_2"], (
        "the incoming distinct tool call was dropped: "
        f"{[m.get('content') for m in merged]}"
    )
    assert merged[-1]["content"] == "final answer"


def test_distinct_tool_calls_after_empty_anchor_both_survive_merge():
    """P2 repro (reviewer's minimal clock schedule): empty-anchor sidecar.

    The matching sidecar row carries NO inline tool_calls -- journal
    recovery anchors the recovered tool cards on the session against an
    empty assistant row.  The incoming durable tool call must still find
    its content-equivalent anchor and survive the merge.
    """
    from api.models import merge_session_messages_append_only

    sidecar = [
        _empty_anchor(1000.0),
        _final_answer(1002.0),
    ]
    state = [
        _assistant_tool_call("call_recovered", "terminal", 1001.0),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert _tool_call_ids(merged) == ["call_recovered"], (
        "the incoming distinct tool call was dropped: "
        f"{[m.get('content') for m in merged]}"
    )
    assert merged[-1]["content"] == "final answer"


def test_same_second_distinct_tool_calls_after_empty_anchor_survive():
    """Control: same-second + empty anchor also keeps the tool call."""
    from api.models import merge_session_messages_append_only

    sidecar = [
        _empty_anchor(1000),
        _final_answer(1001),
    ]
    state = [
        _assistant_tool_call("call_recovered", "terminal", 1000),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert _tool_call_ids(merged) == ["call_recovered"]
    assert merged[-1]["content"] == "final answer"


def test_pre_window_tool_call_after_empty_anchor_is_still_dropped():
    """Control: pre-window + empty anchor stays excluded (master behaviour)."""
    from api.models import merge_session_messages_append_only

    sidecar = [
        _empty_anchor(1000.0),
        _final_answer(1002.0),
    ]
    state = [
        _assistant_tool_call("call_recovered", "terminal", 999.0),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert merged == sidecar
    assert _tool_call_ids(merged) == []


def test_same_second_distinct_tool_calls_still_both_survive_merge():
    """Control: the same-second shape (old tests) must stay green."""
    from api.models import merge_session_messages_append_only

    sidecar = [
        _assistant_tool_call("call_1", "read_file", 1000),
        _final_answer(1001),
    ]
    state = [
        _assistant_tool_call("call_2", "terminal", 1000),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert _tool_call_ids(merged) == ["call_1", "call_2"]
    assert merged[-1]["content"] == "final answer"


def test_pre_window_distinct_tool_call_row_is_still_dropped():
    """Control: a pre-window state.db tool row stays excluded."""
    from api.models import merge_session_messages_append_only

    sidecar = [
        _assistant_tool_call("call_1", "read_file", 1000.0),
        _final_answer(1002.0),
    ]
    state = [
        _assistant_tool_call("call_2", "terminal", 999.0),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert merged == sidecar
    assert _tool_call_ids(merged) == ["call_1"]


def test_repeated_user_turns_at_different_seconds_still_both_survive():
    """Guard: the carve-out is tool-call-only; #7587 user behaviour holds.

    Two identical user turns one second apart must still BOTH survive the
    in-window branch -- widening the content-only equivalence back to user
    rows would re-introduce the #7587 collapse this PR fixes.  The sidecar
    opens on an unrelated assistant row so the positional replay probe
    cannot vouch for the state.db row (the same reason the #7587 module
    keeps its repeated-turn transcripts misaligned).
    """
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
        {"role": "user", "content": "same text", "timestamp": 1000.0},
        _final_answer(1002.0),
    ]
    state = [
        {"role": "user", "content": "same text", "timestamp": 1001.0},
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    repeated = [
        m for m in merged if m.get("role") == "user" and m.get("content") == "same text"
    ]
    assert len(repeated) == 2, (
        "a repeated user turn was suppressed again: "
        f"{[m.get('content') for m in merged]}"
    )


# ── 2. Real SQLite projection → merge ────────────────────────────────────────


def test_distinct_tool_calls_survive_real_sqlite_projection_then_merge(monkeypatch, tmp_path):
    """The durable row must survive the real state.db projection, then merge."""
    import api.models as models
    from api.models import merge_session_messages_append_only

    sid = "webui_8672_projection"
    db_path = tmp_path / "state.db"
    _make_state_db(
        db_path,
        sid,
        [
            {
                "role": "assistant",
                "content": "",
                "timestamp": 1001.0,
                "tool_calls": json.dumps(
                    [{"id": "call_2", "function": {"name": "terminal", "arguments": "{}"}}]
                ),
            },
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path, raising=False)

    projected = models.get_state_db_session_messages(sid)
    # Prove the projection itself carries the durable tool-call row.
    assert _tool_call_ids(projected) == ["call_2"]

    sidecar = [
        _assistant_tool_call("call_1", "read_file", 1000.0),
        _final_answer(1002.0),
    ]

    merged = merge_session_messages_append_only(sidecar, projected)

    assert _tool_call_ids(merged) == ["call_1", "call_2"], (
        "the projected distinct tool call was dropped: "
        f"{[m.get('content') for m in merged]}"
    )
    assert merged[-1]["content"] == "final answer"


def test_distinct_tool_calls_after_empty_anchor_survive_real_sqlite_projection_then_merge(monkeypatch, tmp_path):
    """The durable row must survive the real state.db projection, then merge.

    Same empty-anchor shape as the P2 repro, walked through the real
    SQLite projection so the failure cannot be an artefact of hand-built
    state rows.
    """
    import api.models as models
    from api.models import merge_session_messages_append_only

    sid = "webui_8672_projection_anchor"
    db_path = tmp_path / "state.db"
    _make_state_db(
        db_path,
        sid,
        [
            {
                "role": "assistant",
                "content": "",
                "timestamp": 1001.0,
                "tool_calls": json.dumps(
                    [{"id": "call_recovered", "function": {"name": "terminal", "arguments": "{}"}}]
                ),
            },
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path, raising=False)

    projected = models.get_state_db_session_messages(sid)
    assert _tool_call_ids(projected) == ["call_recovered"]

    sidecar = [
        _empty_anchor(1000.0),
        _final_answer(1002.0),
    ]

    merged = merge_session_messages_append_only(sidecar, projected)

    assert _tool_call_ids(merged) == ["call_recovered"], (
        "the projected distinct tool call was dropped: "
        f"{[m.get('content') for m in merged]}"
    )
    assert merged[-1]["content"] == "final answer"


# ── 3. Display reconciliation (/api/session) ─────────────────────────────────


def test_display_reconciliation_keeps_distinct_tool_calls(monkeypatch, tmp_path):
    """GET /api/session must show both tool calls and the final answer."""
    import api.routes as routes

    sid = "webui_8672_display"
    sidecar_messages = [
        _assistant_tool_call("call_1", "read_file", 1000.0),
        _final_answer(1002.0),
    ]
    _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "role": "assistant",
                "content": "",
                "timestamp": 1001.0,
                "tool_calls": json.dumps(
                    [{"id": "call_2", "function": {"name": "terminal", "arguments": "{}"}}]
                ),
            },
        ],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]
    assert _tool_call_ids(messages) == ["call_1", "call_2"], (
        "display reconciliation dropped the distinct tool call: "
        f"{[m.get('content') for m in messages]}"
    )
    assert messages[-1]["content"] == "final answer"
    assert handler.response_json["session"]["message_count"] == 3


def test_display_reconciliation_keeps_tool_calls_after_empty_anchor(monkeypatch, tmp_path):
    """GET /api/session must keep the recovered tool call after an empty anchor."""
    import api.routes as routes

    sid = "webui_8672_display_anchor"
    sidecar_messages = [
        _empty_anchor(1000.0),
        _final_answer(1002.0),
    ]
    _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "role": "assistant",
                "content": "",
                "timestamp": 1001.0,
                "tool_calls": json.dumps(
                    [{"id": "call_recovered", "function": {"name": "terminal", "arguments": "{}"}}]
                ),
            },
        ],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]
    assert _tool_call_ids(messages) == ["call_recovered"], (
        "display reconciliation dropped the recovered tool call: "
        f"{[m.get('content') for m in messages]}"
    )
    assert messages[-1]["content"] == "final answer"
    assert handler.response_json["session"]["message_count"] == 3


# ── 4. Context reconciliation (next-turn model context) ─────────────────────


def test_context_reconciliation_keeps_distinct_tool_calls(monkeypatch, tmp_path):
    """The next-turn context reconciliation must keep both tool calls.

    ``reconciled_state_db_messages_for_session(..., prefer_context=True)``
    is the exact function the streaming worker calls to build the agent's
    conversation_history.  The assertion deliberately stops at that
    reconciliation boundary: the downstream agent projection strips tool
    calls that have no tool-role answer -- a pre-existing contract that
    drops the sidecar's own call too on unpatched master, so only the
    reconciliation output can show this regression.
    """
    import api.models as models

    sid = "webui_8672_context"
    sidecar_messages = [
        _assistant_tool_call("call_1", "read_file", 1000.0),
        _final_answer(1002.0),
    ]
    session = _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        sidecar_messages,
        context_messages=sidecar_messages,
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "role": "assistant",
                "content": "",
                "timestamp": 1001.0,
                "tool_calls": json.dumps(
                    [{"id": "call_2", "function": {"name": "terminal", "arguments": "{}"}}]
                ),
            },
        ],
    )

    reconciled = models.reconciled_state_db_messages_for_session(
        session, prefer_context=True
    )

    assert _tool_call_ids(reconciled) == ["call_1", "call_2"], (
        "context reconciliation dropped the distinct tool call: "
        f"{[m.get('content') for m in reconciled]}"
    )
    assert reconciled[-1]["content"] == "final answer"


def test_context_reconciliation_keeps_tool_calls_after_empty_anchor(monkeypatch, tmp_path):
    """The next-turn context reconciliation must keep the recovered tool call.

    ``reconciled_state_db_messages_for_session(..., prefer_context=True)``
    is the exact function the streaming worker calls to build the agent's
    conversation_history; the empty anchor is the shape journal recovery
    persists.
    """
    import api.models as models

    sid = "webui_8672_context_anchor"
    sidecar_messages = [
        _empty_anchor(1000.0),
        _final_answer(1002.0),
    ]
    session = _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        sidecar_messages,
        context_messages=sidecar_messages,
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "role": "assistant",
                "content": "",
                "timestamp": 1001.0,
                "tool_calls": json.dumps(
                    [{"id": "call_recovered", "function": {"name": "terminal", "arguments": "{}"}}]
                ),
            },
        ],
    )

    reconciled = models.reconciled_state_db_messages_for_session(
        session, prefer_context=True
    )

    assert _tool_call_ids(reconciled) == ["call_recovered"], (
        "context reconciliation dropped the recovered tool call: "
        f"{[m.get('content') for m in reconciled]}"
    )
    assert reconciled[-1]["content"] == "final answer"
