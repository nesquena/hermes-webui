"""#7050 — one session, one display coordinate for ``message_count``.

A foreign/CLI session (TUI source, claimed into the WebUI) has its transcript
spread over a WebUI sidecar plus the Agent's ``state.db``. ``GET /api/session``
used to answer with two different counts for the SAME session:

* ``messages=0`` (the frontend's 30-second external-refresh poll) took the cheap
  ``COUNT(*)`` summary, which lives in state.db's RAW row domain -- it counts
  archived rows (``active = 0``), replayed transport rows and tool rows that the
  display merge folds away;
* ``messages=1`` returned ``len(_all_msgs)``, the MERGED display transcript.

The poll compares the two, always sees a change and force-reloads the entire
transcript every 30s (titlebar count flickering, unscrollable conversation).

These tests drive the real endpoint and assert that every response mode reports
the same count/timestamp for the same synthetic session, without changing the
payload a metadata poll ships (still no transcript).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


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


def _tool_calls(idx):
    return json.dumps([{
        "id": f"call_{idx}",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    }])


def _foreign_state_rows(*, archived_pairs: int, active_turns: int, duplicate_active: bool):
    """Build a CLI-shaped transcript: archived history + active turns + replay row.

    ``archived_pairs`` rows are compacted history (``active = 0``): state.db keeps
    them, the display reader does not read them. ``duplicate_active`` appends a
    byte-identical replay of an active row (same role/content/timestamp), the
    shape a retried CLI transport write leaves behind.
    """
    rows = []
    ts = 1000.0
    for idx in range(archived_pairs):
        rows.append({"role": "user", "content": f"archived q{idx}", "timestamp": ts, "active": 0})
        ts += 1.0
        rows.append({
            "role": "assistant", "content": f"archived a{idx}", "timestamp": ts,
            "tool_calls": _tool_calls(f"old_{idx}"), "active": 0,
        })
        ts += 1.0
    active_start = ts
    for idx in range(active_turns):
        rows.append({"role": "user", "content": f"q{idx}", "timestamp": ts})
        ts += 1.0
        rows.append({
            "role": "assistant", "content": f"a{idx}", "timestamp": ts,
            "tool_calls": _tool_calls(idx),
        })
        ts += 1.0
        rows.append({
            "role": "tool", "content": f"result {idx}", "timestamp": ts,
            "tool_call_id": f"call_{idx}", "tool_name": "read_file",
        })
        ts += 0.5
    if duplicate_active:
        rows.append({
            "role": "user", "content": "q0", "timestamp": active_start,
        })
    return rows, ts


def _make_state_db(path: Path, sid: str, rows, *, source: str = "cli"):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
            "model TEXT, started_at REAL, message_count INTEGER)"
        )
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, "
            "tool_name TEXT, active INTEGER)"
        )
        conn.execute(
            "INSERT INTO sessions (id, source, title, model, started_at, message_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, source, "Foreign CLI", "test-model", 1000.0, len(rows)),
        )
        for row in rows:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, tool_call_id, "
                "tool_calls, tool_name, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid, row["role"], row["content"], row["timestamp"],
                    row.get("tool_call_id"), row.get("tool_calls"), row.get("tool_name"),
                    row.get("active", 1),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _install_session(monkeypatch, tmp_path, sid, sidecar_messages, *, source="cli"):
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
    # The suite may run under a named profile; the fixture session is profile-less.
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default", raising=False)
    monkeypatch.setattr(
        routes,
        "_lookup_cli_session_metadata",
        lambda session_id: {
            "session_id": session_id,
            "source": source,
            "session_source": source,
            "source_tag": source,
            "raw_source": source,
            "source_label": source.upper(),
        },
    )
    session_dir.mkdir(parents=True, exist_ok=True)

    session = models.Session(
        session_id=sid,
        title="Foreign CLI",
        workspace=str(tmp_path),
        model="test-model",
        messages=sidecar_messages,
        created_at=1000.0,
        updated_at=1001.0,
        is_cli_session=(source != "webui"),
        session_source=source,
        source_tag=source,
        raw_source=source,
        source_label=source.upper(),
    )
    session.save(touch_updated_at=False)
    return session


def _load(sid, query):
    import api.routes as routes

    handler = _GetHandler(f"/api/session?session_id={sid}{query}")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200, (handler.status, handler.wfile.getvalue()[:400])
    return handler.response_json["session"]


def _solution(sid):
    """Every response mode for one session, as the frontend sees them."""
    metadata = _load(sid, "&messages=0&resolve_model=0")
    limited = _load(sid, "&messages=1&msg_limit=30&expand_renderable=1&resolve_model=0")
    full = _load(sid, "&messages=1&resolve_model=0")
    return metadata, limited, full


def test_foreign_cli_metadata_count_matches_message_load(monkeypatch, tmp_path):
    """A freshly claimed CLI session: the poll count must be the display count."""
    sid = "cli7050_claimed"
    rows, _ = _foreign_state_rows(archived_pairs=4, active_turns=4, duplicate_active=True)
    _install_session(monkeypatch, tmp_path, sid, [])
    _make_state_db(tmp_path / "state.db", sid, rows)

    metadata, limited, full = _solution(sid)

    # The raw row domain really is bigger: that is what made the poll loop.
    assert len(rows) > 13
    assert metadata["message_count"] == full["message_count"] == limited["message_count"]
    assert metadata["message_count"] == 12
    assert metadata["last_message_at"] == full["last_message_at"] == limited["last_message_at"]
    # A metadata poll still ships no transcript at all.
    assert metadata["messages"] == []
    assert metadata["_messages_truncated"] is False


def test_foreign_cli_materialized_sidecar_metadata_matches_message_load(monkeypatch, tmp_path):
    """A CLI session already claimed once: sidecar rows + state.db archive rows."""
    sid = "cli7050_materialized"
    rows, _ = _foreign_state_rows(archived_pairs=3, active_turns=4, duplicate_active=False)
    sidecar_rows = [row for row in rows if row.get("active", 1) == 1]
    _install_session(monkeypatch, tmp_path, sid, sidecar_rows)
    _make_state_db(tmp_path / "state.db", sid, rows)

    metadata, limited, full = _solution(sid)

    assert len(rows) > len(sidecar_rows)
    assert metadata["message_count"] == full["message_count"] == limited["message_count"]
    assert metadata["message_count"] == len(sidecar_rows)
    assert metadata["last_message_at"] == full["last_message_at"] == limited["last_message_at"]
    assert metadata["messages"] == []


def test_webui_native_metadata_poll_keeps_the_cheap_growth_path(monkeypatch, tmp_path):
    """Control: WebUI-native polling keeps its raw growth signal (#2605/#7882).

    Not-in-scope by design: native sessions are never probed by the 30s
    external-refresh poll, so their metadata answer stays on the cheap
    COUNT(*)-based growth path instead of paying for a display merge.
    """
    sid = "webui7050_native"
    sidecar_rows = [
        {"role": "user", "content": "q0", "timestamp": 1000.0},
        {"role": "assistant", "content": "a0", "timestamp": 1001.0},
    ]
    grown_rows = sidecar_rows + [
        {"role": "user", "content": "q1", "timestamp": 1002.0},
        {"role": "assistant", "content": "a1", "timestamp": 1003.0},
        # archived history: keeps the raw growth count above the display count,
        # so a metadata answer routed through the display merge would show 2.
        {"role": "user", "content": "archived q", "timestamp": 998.0, "active": 0},
        {"role": "assistant", "content": "archived a", "timestamp": 999.0, "active": 0},
    ]
    _install_session(monkeypatch, tmp_path, sid, sidecar_rows, source="webui")
    _make_state_db(tmp_path / "state.db", sid, grown_rows, source="webui")

    metadata = _load(sid, "&messages=0&resolve_model=0")

    assert metadata["message_count"] == 6
    full = _load(sid, "&messages=1&resolve_model=0")
    assert full["message_count"] == 4


def test_foreign_cli_warm_resident_reflects_separately_saved_sidecar(monkeypatch, tmp_path):
    """A warm resident in memory must not hide a newer sidecar saved separately."""
    import api.models as models

    sid = "cli7050_warm_resident"
    sidecar_messages = [
        {"role": "user", "content": "q0", "timestamp": 1000.0},
        {"role": "assistant", "content": "a0", "timestamp": 1001.0},
    ]
    _install_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, sidecar_messages)

    resident = models.get_session(sid)
    assert resident is not None
    assert len(resident.messages) == 2

    separate = models.Session.load(sid)
    separate.messages.extend([
        {"role": "user", "content": "q1", "timestamp": 1002.0},
        {"role": "assistant", "content": "a1", "timestamp": 1003.0},
    ])
    separate.save(touch_updated_at=False)

    metadata, limited, full = _solution(sid)

    assert metadata["message_count"] == full["message_count"] == limited["message_count"] == 4
    assert metadata["last_message_at"] == full["last_message_at"] == limited["last_message_at"]
    assert metadata["messages"] == []


def test_foreign_cli_persisted_sidecar_metadata_miss_stays_in_display_coordinates(monkeypatch, tmp_path):
    """Foreign session with CLI metadata lookup miss ({}) must stay in display coordinates."""
    import api.routes as routes

    sid = "cli7050_meta_miss"
    rows, _ = _foreign_state_rows(archived_pairs=2, active_turns=1, duplicate_active=False)
    sidecar_rows = [row for row in rows if row.get("active", 1) == 1]
    assert len(rows) == 7
    assert len(sidecar_rows) == 3

    _install_session(monkeypatch, tmp_path, sid, sidecar_rows)
    _make_state_db(tmp_path / "state.db", sid, rows)

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda session_id: {})

    metadata = _load(sid, "&messages=0&resolve_model=0")
    full = _load(sid, "&messages=1&resolve_model=0")

    assert metadata["message_count"] == full["message_count"] == 3
    assert metadata["last_message_at"] == full["last_message_at"]
    assert metadata["message_count"] != 7


def test_foreign_cli_cold_persisted_pending_metadata_hydrates_saved_transcript(monkeypatch, tmp_path):
    """Cold persisted pending metadata stub must hydrate saved sidecar transcript.

    4 sidecar rows + persisted pending_user_message + state.db at the first 2 rows,
    no resident => metadata, limited and full all report message_count == 4 and the
    same last_message_at; metadata still ships messages == [].
    """
    import api.models as models

    sid = "cli7050_cold_pending"
    sidecar_rows = [
        {"role": "user", "content": "q0", "timestamp": 1000.0},
        {"role": "assistant", "content": "a0", "timestamp": 1001.0},
        {"role": "user", "content": "q1", "timestamp": 1002.0},
        {"role": "assistant", "content": "a1", "timestamp": 1003.0},
    ]
    state_rows = sidecar_rows[:2]

    session = _install_session(monkeypatch, tmp_path, sid, sidecar_rows)
    session.pending_user_message = "persisted pending prompt"
    session.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", sid, state_rows)

    # Ensure no resident in memory
    models.SESSIONS.clear()

    metadata, limited, full = _solution(sid)

    assert metadata["message_count"] == full["message_count"] == limited["message_count"] == 4
    assert metadata["last_message_at"] == full["last_message_at"] == limited["last_message_at"]
    assert metadata["messages"] == []
    assert metadata["_messages_truncated"] is False


def test_foreign_cli_idle_cold_session_without_pending_control_a(monkeypatch, tmp_path):
    """Control A: idle cold session (no pending field) reports 4/4/4 unchanged."""
    import api.models as models

    sid = "cli7050_cold_idle_control_a"
    sidecar_rows = [
        {"role": "user", "content": "q0", "timestamp": 1000.0},
        {"role": "assistant", "content": "a0", "timestamp": 1001.0},
        {"role": "user", "content": "q1", "timestamp": 1002.0},
        {"role": "assistant", "content": "a1", "timestamp": 1003.0},
    ]
    state_rows = sidecar_rows[:2]

    _install_session(monkeypatch, tmp_path, sid, sidecar_rows)
    _make_state_db(tmp_path / "state.db", sid, state_rows)

    models.SESSIONS.clear()

    metadata, limited, full = _solution(sid)

    assert metadata["message_count"] == full["message_count"] == limited["message_count"] == 4
    assert metadata["last_message_at"] == full["last_message_at"] == limited["last_message_at"]
    assert metadata["messages"] == []
    assert metadata["_messages_truncated"] is False


def test_foreign_cli_live_unsaved_resident_with_pending_answers_from_memory(monkeypatch, tmp_path):
    """Control B: live resident with unsaved messages + pending answers from memory.

    A warm resident in memory carrying unsaved messages and an in-flight
    pending_user_message must NOT regress to a disk snapshot.
    """
    import api.models as models

    sid = "cli7050_live_resident_pending_control_b"
    disk_rows = [
        {"role": "user", "content": "q0", "timestamp": 1000.0},
        {"role": "assistant", "content": "a0", "timestamp": 1001.0},
    ]
    _install_session(monkeypatch, tmp_path, sid, disk_rows)
    _make_state_db(tmp_path / "state.db", sid, disk_rows)

    # Warm resident into memory
    resident = models.get_session(sid)
    assert resident is not None
    assert len(resident.messages) == 2
    assert getattr(resident, "_loaded_metadata_only", False) is False

    # Add unsaved in-memory messages + pending_user_message without saving to disk
    resident.messages.extend([
        {"role": "user", "content": "q1", "timestamp": 1002.0},
        {"role": "assistant", "content": "a1", "timestamp": 1003.0},
    ])
    resident.pending_user_message = "live in-flight prompt"

    metadata, limited, full = _solution(sid)

    assert metadata["message_count"] == full["message_count"] == limited["message_count"] == 4
    assert metadata["last_message_at"] == full["last_message_at"] == limited["last_message_at"]
    assert metadata["messages"] == []


def test_foreign_cli_bounded_load_reconciles_immediate_parent_lineage(monkeypatch, tmp_path):
    """D1 (P1): foreign continuation must reconcile immediate parent across all modes.

    Immediate parent sidecar holds parent-only history and is not a snapshot.
    Child continuation carries parent_session_id to it + child rows in sidecar
    and in state.db. Metadata (0), bounded (30), and full (1) loads must all
    agree on message_count (4) and last_message_at (1003.0); metadata still
    ships messages == [] and _messages_truncated is False.
    """
    parent_sid = "cli7050_p1_parent"
    parent_rows = [
        {"role": "user", "content": "archived parent q", "timestamp": 1000.0},
        {"role": "assistant", "content": "archived parent a", "timestamp": 1001.0},
    ]
    parent = _install_session(monkeypatch, tmp_path, parent_sid, parent_rows)
    assert not getattr(parent, "pre_compression_snapshot", False)

    child_sid = "cli7050_p1_child"
    child_rows = [
        {"role": "user", "content": "q0", "timestamp": 1002.0},
        {"role": "assistant", "content": "a0", "timestamp": 1003.0},
    ]
    child = _install_session(monkeypatch, tmp_path, child_sid, child_rows)
    child.parent_session_id = parent_sid
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, child_rows)

    metadata, limited, full = _solution(child_sid)

    assert metadata["message_count"] == full["message_count"] == limited["message_count"] == 4
    assert metadata["last_message_at"] == full["last_message_at"] == limited["last_message_at"] == 1003.0
    assert metadata["messages"] == []
    assert metadata["_messages_truncated"] is False
    assert len(limited["messages"]) == 4
    assert len(full["messages"]) == 4


def test_foreign_cli_child_without_parent_lineage_control(monkeypatch, tmp_path):
    """D2 (control): child with no parent_session_id reports 2 / 2 / 2."""
    child_sid = "cli7050_no_parent_control"
    child_rows = [
        {"role": "user", "content": "q0", "timestamp": 1002.0},
        {"role": "assistant", "content": "a0", "timestamp": 1003.0},
    ]
    _install_session(monkeypatch, tmp_path, child_sid, child_rows)
    _make_state_db(tmp_path / "state.db", child_sid, child_rows)

    metadata, limited, full = _solution(child_sid)

    assert metadata["message_count"] == full["message_count"] == limited["message_count"] == 2
    assert metadata["last_message_at"] == full["last_message_at"] == limited["last_message_at"] == 1003.0
    assert metadata["messages"] == []
    assert metadata["_messages_truncated"] is False
    assert len(limited["messages"]) == 2
    assert len(full["messages"]) == 2


def test_foreign_cli_memoized_bounded_merge_invalidates_on_parent_growth(monkeypatch, tmp_path):
    """D3: warm memoized bounded merge reflects parent sidecar growth."""
    import api.models as models

    parent_sid = "cli7050_cache_parent"
    parent_rows = [
        {"role": "user", "content": "archived parent q", "timestamp": 1000.0},
        {"role": "assistant", "content": "archived parent a", "timestamp": 1001.0},
    ]
    _install_session(monkeypatch, tmp_path, parent_sid, parent_rows)

    child_sid = "cli7050_cache_child"
    child_rows = [
        {"role": "user", "content": "q0", "timestamp": 1002.0},
        {"role": "assistant", "content": "a0", "timestamp": 1003.0},
    ]
    child = _install_session(monkeypatch, tmp_path, child_sid, child_rows)
    child.parent_session_id = parent_sid
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, child_rows)

    limited_query = "&messages=1&msg_limit=30&expand_renderable=1&resolve_model=0"
    lim1 = _load(child_sid, limited_query)
    assert lim1["message_count"] == 4
    assert len(lim1["messages"]) == 4

    parent = models.Session.load(parent_sid)
    parent.messages.extend([
        {"role": "user", "content": "parent q2", "timestamp": 1004.0},
        {"role": "assistant", "content": "parent a2", "timestamp": 1005.0},
    ])
    parent.save(touch_updated_at=False)

    lim2 = _load(child_sid, limited_query)
    assert lim2["message_count"] == 6
    assert len(lim2["messages"]) == 6
    contents = [m["content"] for m in lim2["messages"]]
    assert "parent q2" in contents
    assert "parent a2" in contents


