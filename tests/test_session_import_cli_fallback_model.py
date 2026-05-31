"""Regression test for #1386: CLI session import must not crash when the
session is missing from `get_cli_sessions()` metadata at the time of import.

Before the fix, `_handle_session_import_cli` only assigned `model` inside
the `for cs in get_cli_sessions(): if cs["session_id"] == sid` loop. If
the session existed in the messages store but had no metadata row (or had
been pruned after `get_cli_session_messages()` was called), `model` was
unbound and `import_cli_session(sid, title, msgs, model, ...)` raised
`UnboundLocalError`.

The fix initializes `model = "unknown"` before the loop so the import
proceeds with a sensible default rather than crashing.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _extract_handler(name: str) -> str:
    """Return the source of the handler function `name` from api/routes.py."""
    marker = f"def {name}("
    idx = ROUTES_PY.find(marker)
    assert idx != -1, f"{name} not found in api/routes.py"
    # Walk forward until a top-level `def ` (col 0) appears.
    next_def = ROUTES_PY.find("\ndef ", idx + len(marker))
    return ROUTES_PY[idx : next_def if next_def != -1 else len(ROUTES_PY)]


def test_import_cli_initializes_model_before_metadata_loop():
    """The fallback `model = 'unknown'` must be set BEFORE the
    `for cs in get_cli_sessions()` loop so that a metadata-less session
    cannot leave `model` unbound."""
    handler = _extract_handler("_handle_session_import_cli")
    init_idx = handler.find('model = "unknown"')
    if init_idx == -1:
        # Allow single quotes too.
        init_idx = handler.find("model = 'unknown'")
    assert init_idx != -1, (
        "Expected `model = \"unknown\"` initialization in "
        "_handle_session_import_cli before the metadata loop. Without it, "
        "import crashes when the session has messages but no metadata row."
    )
    loop_idx = handler.find("for cs in get_cli_sessions()")
    assert loop_idx != -1, "Expected `for cs in get_cli_sessions()` loop"
    assert init_idx < loop_idx, (
        "`model` must be initialized BEFORE the `for cs in get_cli_sessions()` "
        "loop, otherwise a session without a metadata row leaves `model` "
        "unbound and `import_cli_session(..., model, ...)` raises "
        "UnboundLocalError."
    )


def test_import_cli_passes_model_to_import_helper():
    """Sanity: the handler still passes the resolved model down to
    `import_cli_session` — the regression test would not catch a refactor
    that drops the argument entirely."""
    handler = _extract_handler("_handle_session_import_cli")
    assert "import_cli_session(" in handler
    # The model variable should appear as a positional or keyword arg in
    # the import_cli_session call.
    call_idx = handler.find("import_cli_session(")
    call_block = handler[call_idx : call_idx + 400]
    assert "model" in call_block, (
        "import_cli_session() call should still receive the `model` argument."
    )


def test_session_import_cli_refresh_matches_messages_despite_timestamp_type_differences(monkeypatch):
    """Refreshing an imported session should still extend when timestamps differ only by type.

    Existing WebUI messages can use integer timestamps while CLI refresh returns
    floating-point timestamps for the same turns. This test verifies the handler
    accepts that as semantic equality and replaces with the longer, fresher tail.
    """
    import api.routes as routes

    session_id = "ts_type_diff_001"

    class FakeSession:
        def __init__(self):
            self.messages = [
                {"role": "user", "content": "hello", "timestamp": 1710000000},
                {"role": "assistant", "content": "working", "timestamp": 1710000001},
            ]
            self.source_tag = "weixin"
            self.raw_source = "weixin"
            self.session_source = "messaging"
            self.source_label = "WeChat"
            self.parent_session_id = None

        def compact(self):
            return {"session_id": session_id, "title": "Imported"}

        def save(self, touch_updated_at=False):
            save_calls.append(touch_updated_at)

    save_calls = []
    existing = FakeSession()
    fresh = [
        {"role": "user", "content": "hello", "timestamp": 1710000000.0},
        {"role": "assistant", "content": "working", "timestamp": 1710000001.0},
        {"role": "assistant", "content": "next", "timestamp": 1710000002.0},
    ]

    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, sid: existing if sid == session_id else None))
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "bad", lambda _handler, msg, status=400: {"ok": False, "error": msg, "status": status})
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda sid: fresh if sid == session_id else [])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None: [{"session_id": session_id, "source_tag": "weixin", "raw_source": "weixin", "session_source": "messaging", "source_label": "WeChat"}])

    response = routes._handle_session_import_cli(object(), {"session_id": session_id})

    assert response["imported"] is False
    assert response["session"]["messages"] == fresh
    assert existing.messages == fresh
    assert save_calls == [False]


def test_session_import_cli_refresh_rejects_prefix_if_non_timing_content_diverges(monkeypatch):
    """Only true prefixes should be treated as unchanged history during refresh.

    If the refreshed message body diverges, we should keep the existing in-memory
    transcript instead of replacing it with potentially older content.
    """
    import api.routes as routes

    session_id = "ts_type_diverge_001"

    class FakeSession:
        def __init__(self):
            self.messages = [
                {"role": "user", "content": "old-prefix", "timestamp": 1710000000},
                {"role": "assistant", "content": "from local", "timestamp": 1710000001},
            ]
            self.source_tag = "telegram"
            self.raw_source = "telegram"
            self.session_source = "messaging"
            self.source_label = "Telegram"
            self.is_cli_session = True
            self.parent_session_id = None

        def compact(self):
            return {"session_id": session_id, "title": "Imported"}

        def save(self, touch_updated_at=False):
            save_calls.append(touch_updated_at)

    save_calls = []
    existing = FakeSession()
    fresh = [
        {"role": "user", "content": "different-prefix", "timestamp": 1710000000.0},
        {"role": "assistant", "content": "from cli", "timestamp": 1710000001.0},
        {"role": "assistant", "content": "next", "timestamp": 1710000002.0},
    ]

    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, sid: existing if sid == session_id else None))
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "bad", lambda _handler, msg, status=400: {"ok": False, "error": msg, "status": status})
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda sid: fresh if sid == session_id else [])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None: [{"session_id": session_id, "source_tag": "telegram", "raw_source": "telegram", "session_source": "messaging", "source_label": "Telegram"}])

    response = routes._handle_session_import_cli(object(), {"session_id": session_id})

    assert response["imported"] is False
    assert response["session"]["messages"] == existing.messages
    assert existing.messages[0]["content"] == "old-prefix"
    assert save_calls == []


def test_session_import_cli_preserves_parent_metadata_on_existing_import(monkeypatch):
    """Refreshing an already-imported CLI session must persist lineage metadata."""
    import api.routes as routes

    session_id = "existing_parent_lineage_001"
    parent_id = "root_parent_lineage_001"

    class FakeSession:
        def __init__(self):
            self.messages = [{"role": "user", "content": "hello", "timestamp": 1.0}]
            self.source_tag = "telegram"
            self.raw_source = "telegram"
            self.session_source = "messaging"
            self.source_label = "Telegram"
            self.parent_session_id = None
            self.is_cli_session = True

        def compact(self):
            return {"session_id": session_id, "title": "Imported", "parent_session_id": self.parent_session_id}

        def save(self, touch_updated_at=False):
            save_calls.append(touch_updated_at)

    save_calls = []
    existing = FakeSession()

    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, sid: existing if sid == session_id else None))
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda sid: existing.messages if sid == session_id else [])
    monkeypatch.setattr(
        routes,
        "get_cli_sessions",
        lambda source_filter=None: [{
            "session_id": session_id,
            "source_tag": "telegram",
            "raw_source": "telegram",
            "session_source": "messaging",
            "source_label": "Telegram",
            "parent_session_id": parent_id,
        }],
    )

    response = routes._handle_session_import_cli(object(), {"session_id": session_id})

    assert response["imported"] is False
    assert existing.parent_session_id == parent_id
    assert response["session"]["parent_session_id"] == parent_id
    assert save_calls == [False]


def test_read_only_import_payload_includes_parent_session_id(monkeypatch):
    """Read-only CLI/session imports should also expose lineage in the payload."""
    import api.routes as routes

    session_id = "readonly_parent_lineage_001"
    parent_id = "readonly_root_lineage_001"
    messages = [{"role": "user", "content": "hello", "timestamp": 1.0}]

    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, sid: None))
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "bad", lambda _handler, msg, status=400: {"ok": False, "error": msg, "status": status})
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda sid: messages if sid == session_id else [])
    monkeypatch.setattr(
        routes,
        "get_cli_sessions",
        lambda source_filter=None: [{
            "session_id": session_id,
            "title": "Read-only child",
            "model": "test-model",
            "created_at": 1.0,
            "updated_at": 2.0,
            "source_tag": "discord",
            "raw_source": "discord",
            "session_source": "messaging",
            "source_label": "Discord",
            "parent_session_id": parent_id,
            "read_only": True,
        }],
    )

    response = routes._handle_session_import_cli(object(), {"session_id": session_id})

    assert response["imported"] is False
    assert response["session"]["parent_session_id"] == parent_id
    assert response["session"]["messages"] == messages


def test_merge_cli_sidebar_metadata_keeps_larger_sidecar_message_count():
    """Sidebar metadata merge should not shrink repaired aggregate sidecar counts."""
    import api.routes as routes

    merged = routes._merge_cli_sidebar_metadata(
        {"session_id": "sid", "message_count": 535, "title": "Recovered"},
        {"session_id": "sid", "message_count": 407, "source_tag": "discord"},
    )

    assert merged["message_count"] == 535


def test_webui_state_projection_dedupes_by_lineage_root():
    """WebUI-origin state.db projections should not be additive non-WebUI rows."""
    import api.routes as routes

    represented = {"root_sid"}
    state_projection = {
        "session_id": "tip_sid",
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "_lineage_root_id": "root_sid",
        "_lineage_tip_id": "tip_sid",
    }

    assert routes._is_duplicate_webui_state_projection(state_projection, represented) is True


def test_external_state_projection_not_deduped_by_webui_source_guard():
    """The WebUI-source guard must not hide real external conversations."""
    import api.routes as routes

    represented = {"root_sid"}
    external_projection = {
        "session_id": "tip_sid",
        "source_tag": "telegram",
        "raw_source": "telegram",
        "session_source": "messaging",
        "_lineage_root_id": "root_sid",
        "_lineage_tip_id": "tip_sid",
    }

    assert routes._is_duplicate_webui_state_projection(external_projection, represented) is False


def test_sessions_endpoint_suppresses_duplicate_webui_state_projection(monkeypatch):
    """The /api/sessions merge should not add WebUI state.db lineage duplicates."""
    import api.profiles as profiles
    import api.routes as routes

    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _sessions: False)
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": True})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    webui_row = {
        "session_id": "visible_tip",
        "title": "Long Conversation",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "_lineage_root_id": "root_sid",
        "_lineage_tip_id": "visible_tip",
    }
    duplicate_webui_projection = {
        "session_id": "state_projection_tip",
        "title": "Long Conversation",
        "profile": "default",
        "updated_at": 30,
        "last_message_at": 30,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "_lineage_root_id": "root_sid",
        "_lineage_tip_id": "state_projection_tip",
    }
    external_projection = {
        "session_id": "telegram_tip",
        "title": "External Thread",
        "profile": "default",
        "updated_at": 10,
        "last_message_at": 10,
        "source_tag": "telegram",
        "raw_source": "telegram",
        "session_source": "messaging",
        "_lineage_root_id": "root_sid",
        "_lineage_tip_id": "telegram_tip",
    }

    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [webui_row])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None: [duplicate_webui_projection, external_projection])

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("http://example.com/api/sessions"))

    assert handler.status == 200
    session_ids = [row["session_id"] for row in handler.json_body()["sessions"]]
    assert "visible_tip" in session_ids
    assert "state_projection_tip" not in session_ids
    assert "telegram_tip" in session_ids


def test_messaging_session_loader_prefers_repaired_sidecar_transcript():
    """Pin the /api/session invariant that repaired sidecars keep authoritative order."""
    handler = _extract_handler("handle_get")
    old = "if is_messaging_session and cli_messages:\n                    _all_msgs = cli_messages"
    assert old not in handler
    assert "_all_msgs = _merged_session_messages_for_display(s, cli_messages)" in handler
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    assert "sidecar_messages = _webui_sidecar_lineage_messages_for_display(session)" in src
    assert "return prefix + merged_sidecar + suffix" in src



def test_messaging_merge_applies_truncation_watermark_before_sidecar_advances():
    import types
    import api.routes as routes

    session = types.SimpleNamespace(
        messages=[
            {"role": "user", "content": "kept", "timestamp": 10.0},
            {"role": "assistant", "content": "kept answer", "timestamp": 20.0},
        ],
        truncation_watermark=20.0,
    )
    cli_messages = [
        {"role": "user", "content": "kept", "timestamp": 10.0},
        {"role": "assistant", "content": "kept answer", "timestamp": 20.0},
        {"role": "user", "content": "stale after truncation", "timestamp": 30.0},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m["content"] for m in merged] == ["kept", "kept answer"]


def test_messaging_merge_preserves_timestamp_less_cli_rows_at_prefix():
    import types
    import api.routes as routes

    session = types.SimpleNamespace(
        messages=[{"role": "assistant", "content": "sidecar", "timestamp": 10.0}],
        truncation_watermark=None,
    )
    cli_messages = [
        {"role": "user", "content": "legacy no timestamp"},
        {"role": "assistant", "content": "sidecar", "timestamp": 10.0},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m["content"] for m in merged] == ["legacy no timestamp", "sidecar"]


def test_messaging_merge_places_same_timestamp_cli_extras_after_sidecar_row():
    import types
    import api.routes as routes

    session = types.SimpleNamespace(
        messages=[{"id": "sidecar-a", "role": "assistant", "content": "sidecar", "timestamp": 10.0}],
        truncation_watermark=None,
    )
    cli_messages = [
        {"id": "sidecar-a", "role": "assistant", "content": "sidecar", "timestamp": 10.0},
        {"id": "cli-extra", "role": "assistant", "content": "cli extra", "timestamp": 10.0},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m["content"] for m in merged] == ["sidecar", "cli extra"]

def test_messaging_merge_preserves_same_visible_suffix_turn_with_distinct_timestamp():
    import types
    import api.routes as routes

    session = types.SimpleNamespace(
        messages=[
            {"role": "user", "content": "hello", "timestamp": 100.0},
            {"role": "assistant", "content": "hi", "timestamp": 101.0},
            {"role": "user", "content": "alpha", "timestamp": 102.0},
            {"role": "assistant", "content": "r", "timestamp": 103.0},
        ],
        truncation_watermark=None,
    )
    cli_messages = list(session.messages) + [
        {"role": "user", "content": "alpha", "timestamp": 200.0},
        {"role": "assistant", "content": "two", "timestamp": 201.0},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [(m["role"], m["content"], m.get("timestamp")) for m in merged] == [
        ("user", "hello", 100.0),
        ("assistant", "hi", 101.0),
        ("user", "alpha", 102.0),
        ("assistant", "r", 103.0),
        ("user", "alpha", 200.0),
        ("assistant", "two", 201.0),
    ]


def test_messaging_merge_preserves_timestamp_less_cli_row_without_poisoning_later_repeat():
    import types
    import api.routes as routes

    session = types.SimpleNamespace(
        messages=[{"role": "assistant", "content": "sidecar", "timestamp": 100.0}],
        truncation_watermark=None,
    )
    cli_messages = [
        {"role": "user", "content": "alpha"},
        {"role": "assistant", "content": "sidecar", "timestamp": 100.0},
        {"role": "user", "content": "alpha", "timestamp": 200.0},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [(m["role"], m["content"], m.get("timestamp")) for m in merged] == [
        ("user", "alpha", None),
        ("assistant", "sidecar", 100.0),
        ("user", "alpha", 200.0),
    ]


def test_messaging_merge_dedupes_timestamp_less_cli_row_when_stable_id_matches_sidecar():
    import types
    import api.routes as routes

    session = types.SimpleNamespace(
        messages=[{"message_id": "m1", "role": "user", "content": "alpha", "timestamp": 100.0}],
        truncation_watermark=None,
    )
    cli_messages = [{"message_id": "m1", "role": "user", "content": "alpha"}]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [(m.get("message_id"), m["content"]) for m in merged] == [("m1", "alpha")]


def test_messaging_merge_honors_message_id_alias_for_distinct_repeated_suffix_turn():
    import types
    import api.routes as routes

    session = types.SimpleNamespace(
        messages=[{"message_id": "s1", "role": "user", "content": "alpha", "timestamp": 100.0}],
        truncation_watermark=None,
    )
    cli_messages = [
        {"message_id": "s1", "role": "user", "content": "alpha", "timestamp": 100.0},
        {"message_id": "c2", "role": "user", "content": "alpha", "timestamp": 200.0},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [(m.get("message_id"), m["content"], m.get("timestamp")) for m in merged] == [
        ("s1", "alpha", 100.0),
        ("c2", "alpha", 200.0),
    ]

def test_state_db_session_messages_preserve_message_id(monkeypatch, tmp_path):
    import sqlite3
    import api.models as models

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            ("msg-1", "sid-1", "user", "hello", 1.0),
        )

    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    messages = models.get_state_db_session_messages("sid-1")

    assert messages == [{"id": "msg-1", "role": "user", "content": "hello", "timestamp": 1.0}]
