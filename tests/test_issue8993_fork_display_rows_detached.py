"""A fork's display rows must not alias the source session's row dicts.

GET /api/session memoizes a full-transcript summary (todo state, last
timestamp, visible count) next to the merged rows of an inactive session. The
branch route used to hand the child the SOURCE's display-row dicts. When the
child's turn settled, the missing-clock stamp then wrote timestamps into those
shared dicts, changing the source's rows in memory without touching anything
its cache key covers (sidecar stat, row count, last-row timestamp, truncation
markers, SQLite revision). The source's next GET served the memoized summary
for rows that no longer matched it.

Real branch handler, chat-start preparation, settlement, missing-clock stamp,
sidecar saves, SQLite reads/revision and GET handler. The only synthetic input
is the agent's returned message list. All data here is synthetic.
"""

import copy
import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import urlparse

import api.config as config
import api.models as models
import api.routes as routes
import api.streaming as streaming

SID = "20261118_120000_forksource"
QUERY = "messages=1&resolve_model=0&msg_limit=30"

# The todo tool result carries no clock: legitimate for older transcripts.
SOURCE_ROWS = [
    {"role": "user", "content": "question", "timestamp": 10.0},
    {"role": "assistant", "content": "", "timestamp": 11.0,
     "tool_calls": [{"id": "todo", "function": {"name": "todo", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "todo",
     "content": '{"todos":[{"id":"1","content":"latest task","status":"pending"}]}'},
    {"role": "assistant", "content": "answer", "timestamp": 14.0},
]


def _seed_state_db(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
                     "model TEXT, started_at REAL, message_count INTEGER)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                     "content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT)")
        conn.execute("INSERT INTO sessions VALUES (?, 'webui', 'source', 'test-model', 10, 4)", (SID,))
        for index, row in enumerate(SOURCE_ROWS, 1):
            conn.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (index, SID, row["role"], row["content"], row.get("timestamp"), row.get("tool_call_id"),
                 json.dumps(row["tool_calls"]) if row.get("tool_calls") else None),
            )


def test_fork_settlement_cannot_restamp_the_inactive_source_rows(monkeypatch, tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    for module in (config, models, routes):
        monkeypatch.setattr(module, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "_write_session_index", lambda **kwargs: None)
    db_path = tmp_path / "state.db"
    _seed_state_db(db_path)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    assert routes._state_db_session_signature(SID)[0] == "target-session-revision-v2"

    source = models.Session(session_id=SID, messages=copy.deepcopy(SOURCE_ROWS),
                            context_messages=copy.deepcopy(SOURCE_ROWS),
                            model="test-model", context_length=100)
    source.save(skip_index=True)
    captured = {}

    def capture(_handler, value, **kwargs):
        captured.clear()
        captured.update(value)
        return value

    monkeypatch.setattr(routes, "get_session", lambda value, **kwargs: source)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *a, **k: {})
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **k: None)
    monkeypatch.setattr(routes, "j", capture)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *a, **k: False)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": SID})
    hits = []
    real_cached = routes._display_merge_cached_messages

    def cached_spy(*args, **kwargs):
        out = real_cached(*args, **kwargs)
        hits.append(out is not None)
        return out

    monkeypatch.setattr(routes, "_display_merge_cached_messages", cached_spy)

    def get_source():
        routes._handle_session_get(SimpleNamespace(), urlparse(f"/api/session?session_id={SID}&{QUERY}"))
        return copy.deepcopy(captured["session"])

    routes.handle_post(SimpleNamespace(), urlparse("/api/session/branch"))
    child = routes.SESSIONS[captured["session_id"]]
    try:
        # Warm the source's display-merge cache after the branch route ran.
        with routes._display_merge_cache_lock:
            routes._display_merge_cache.clear()
        get_source()
        assert SID in routes._display_merge_cache
        old_key = routes._display_merge_cache[SID]["key"]
        source_rows_before = copy.deepcopy(source.messages)

        # The child's turn: real preparation, settlement and missing-clock stamp.
        returned = copy.deepcopy(child.context_messages) + [
            {"role": "user", "content": "follow-up", "timestamp": 90.0},
            {"role": "assistant", "content": "child answer", "timestamp": 91.0},
        ]
        routes._prepare_chat_start_session_for_stream(
            child, msg="follow-up", attachments=[], workspace=child.workspace,
            model="test-model", model_provider=None, stream_id="child-run",
            started_at=90.0, source="fork")
        identity = streaming._active_turn_authority(child, "child-run", "follow-up")
        streaming._settle_result_messages(
            child, list(child.messages), list(child.context_messages), returned,
            "follow-up", "fork", identity)
        streaming._stamp_missing_message_timestamps(child.messages, now=100.0)
        child.active_stream_id = None
        child.pending_user_message = None
        child.pending_started_at = None
        child.pending_user_source = None
        child.save(skip_index=True)

        # Nothing the source's cache key covers has changed.
        assert not source.active_stream_id and not source.pending_user_message
        assert routes._display_merge_cache_key(source, source.messages, None) == old_key

        hits.clear()
        served = get_source()
        assert hits and hits[-1] is True, "the source GET did not use its cached merge"
        with routes._display_merge_cache_lock:
            routes._display_merge_cache.clear()
        fresh = get_source()

        assert (served.get("todo_state"), served.get("last_message_at")) == (
            fresh.get("todo_state"), fresh.get("last_message_at")
        ), "the source's cached summary no longer matches its rows"
        assert served == fresh
        assert source.messages == source_rows_before, "the child's settlement edited the source's rows"
        assert not any(c is s for c in child.messages for s in source.messages)
    finally:
        routes.clear_session_writeback_owner_if_owned(child.session_id, "child-run")
        with routes.LOCK:
            routes.SESSIONS.pop(child.session_id, None)
        with routes._display_merge_cache_lock:
            routes._display_merge_cache.clear()
