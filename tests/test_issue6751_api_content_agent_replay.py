"""Regression coverage for #6751 api_content replay preservation.

The fixture models the durable Agent ``messages`` rows that carry a clean
visible ``content`` value plus the exact provider-facing ``api_content``
sidecar.  The two user rows intentionally render the same text while keeping
different timestamps and sidecars so a content-only reconciliation can be
observed if it crosses the rows.
"""

import sqlite3
import sys
import json
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.requires_agent_modules


def _write_state_db(path, sid):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            api_content TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO messages (session_id, role, content, timestamp, api_content)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (sid, "user", "same visible text", 100.0, "[Workspace::v1: /one]\\nsame visible text"),
            (sid, "user", "same visible text", 200.0, "[Workspace::v1: /two]\\nsame visible text"),
        ],
    )
    conn.commit()
    conn.close()


def test_issue6751_state_db_sidecars_survive_reconcile_and_internal_agent_projection(
    monkeypatch, tmp_path
):
    """Distinct same-text rows retain their own sidecar through every boundary."""
    import api.models as models
    from api.streaming import _sanitize_messages_for_agent

    sid = "issue6751-replay"
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, sid)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    state_messages = models.get_state_db_session_messages(sid)
    assert [message["api_content"] for message in state_messages] == [
        "[Workspace::v1: /one]\\nsame visible text",
        "[Workspace::v1: /two]\\nsame visible text",
    ]
    assert [message["_state_db_row_id"] for message in state_messages] == [1, 2]

    sidecar_messages = [
        {"role": "user", "content": "same visible text", "timestamp": 100.0},
        {"role": "user", "content": "same visible text", "timestamp": 200.0},
    ]
    merged = models.merge_session_messages_append_only(sidecar_messages, state_messages)
    assert [message.get("api_content") for message in merged] == [
        "[Workspace::v1: /one]\\nsame visible text",
        "[Workspace::v1: /two]\\nsame visible text",
    ]

    internal_history = _sanitize_messages_for_agent(merged)
    assert [message.get("api_content") for message in internal_history] == [
        "[Workspace::v1: /one]\\nsame visible text",
        "[Workspace::v1: /two]\\nsame visible text",
    ]


def test_issue6751_reconciliation_prioritizes_row_id_and_preserves_existing_sidecar():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 200.0,
            "_state_db_row_id": 2,
            "api_content": "already-authoritative",
        },
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 100.0,
            "_state_db_row_id": 1,
        },
    ]
    state = [
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 100.0,
            "_state_db_row_id": 1,
            "api_content": "[Workspace::v1: /one]\\nsame visible text",
        },
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 200.0,
            "_state_db_row_id": 2,
            "api_content": "[Workspace::v1: /two]\\nsame visible text",
        },
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert [message["api_content"] for message in merged] == [
        "already-authoritative",
        "[Workspace::v1: /one]\\nsame visible text",
        "[Workspace::v1: /two]\\nsame visible text",
    ]


def test_issue6751_reconciliation_consumes_each_state_source_once_on_timestamp_collision():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 100.0,
            "_state_db_row_id": 1,
        },
        {"role": "user", "content": "same visible text", "timestamp": 100.0},
    ]
    state = [
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 100.0,
            "_state_db_row_id": 1,
            "api_content": "wire-one",
        },
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 100.0,
            "api_content": "wire-two",
        },
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert [message.get("api_content") for message in merged] == ["wire-one", "wire-two"]


def test_issue6751_reconciliation_fails_closed_when_row_id_roles_disagree():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 100.0,
            "_state_db_row_id": 1,
        }
    ]
    state = [
        {
            "role": "assistant",
            "content": "same visible text",
            "timestamp": 100.0,
            "_state_db_row_id": 1,
            "api_content": "assistant-wire",
        }
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert "api_content" not in merged[0]


def test_issue6751_duplicate_durable_row_ids_fail_closed():
    """A row id shared by multiple sidecar targets must never guess a target."""
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "user", "content": "same", "_state_db_row_id": 7},
        {"role": "user", "content": "same", "_state_db_row_id": 7},
    ]
    state = [
        {
            "role": "user",
            "content": "same",
            "_state_db_row_id": 7,
            "api_content": "wire",
        }
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert [message.get("api_content") for message in merged[:2]] == [None, None]


def test_issue6751_conflicting_row_id_aliases_fail_closed():
    """Contradictory private aliases are invalid provenance, not a fallback hint."""
    from api.models import merge_session_messages_append_only

    sidecar = [
        {
            "role": "assistant",
            "content": "answer",
            "_state_db_row_id": 7,
            "_db_row_id": 8,
        }
    ]
    state = [
        {
            "role": "assistant",
            "content": "answer",
            "_state_db_row_id": 7,
            "api_content": "wire",
        }
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert "api_content" not in merged[0]


def test_issue6751_incompatible_visible_content_fails_closed():
    """A matching durable id cannot override an incompatible visible turn."""
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "user", "content": "visible one", "_state_db_row_id": 7}
    ]
    state = [
        {
            "role": "user",
            "content": "visible two",
            "_state_db_row_id": 7,
            "api_content": "wire-two",
        }
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert "api_content" not in merged[0]


def test_issue6751_ambiguous_metadata_free_sequence_fails_closed():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "assistant", "content": "same answer"},
        {"role": "assistant", "content": "same answer"},
    ]
    state = [
        {"role": "assistant", "content": "same answer", "api_content": "wire-one"},
        {"role": "assistant", "content": "same answer", "api_content": "wire-two"},
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert merged[:2] == sidecar
    assert [message.get("api_content") for message in merged] == [
        None,
        None,
        "wire-one",
        "wire-two",
    ]


def test_issue6751_partial_metadata_bucket_fails_closed_instead_of_zipping():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "assistant", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    state = [
        {"role": "assistant", "content": "first", "timestamp": 10.0, "api_content": "wire-first"},
        {"role": "assistant", "content": "second", "api_content": "wire-second"},
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert merged[:2] == sidecar
    assert [message.get("api_content") for message in merged] == [
        None,
        None,
        "wire-first",
        "wire-second",
    ]


def test_issue6751_state_db_reader_preserves_shape_without_api_content(monkeypatch, tmp_path):
    import api.models as models

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    conn.execute(
        "INSERT INTO messages VALUES (7, ?, ?, ?, ?)",
        ("shape-session", "user", "ordinary", 1.0),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    assert models.get_state_db_session_messages("shape-session") == [
        {"role": "user", "content": "ordinary", "timestamp": 1.0}
    ]


def test_issue6751_public_message_projection_strips_internal_replay_fields(monkeypatch):
    import api.helpers as helpers
    import api.config as config

    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": False})
    safe = helpers.redact_session_data(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "visible",
                    "api_content": "wire",
                    "_state_db_row_id": 7,
                    "_db_row_id": 8,
                    "state_db_row_id": 9,
                }
            ]
        }
    )

    assert safe["messages"] == [{"role": "user", "content": "visible"}]


def test_issue6751_public_session_projection_strips_context_aliases(monkeypatch):
    import api.config as config
    from api.helpers import public_session_projection

    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": False})
    safe = public_session_projection(
        {
            "messages": [{"role": "user", "content": "visible", "api_content": "wire"}],
            "context_messages": [
                {
                    "role": "assistant",
                    "content": "answer",
                    "api_content": "provider bytes",
                    "_state_db_row_id": 7,
                    "_db_row_id": 8,
                    "state_db_row_id": 9,
                }
            ],
            "runtime_journal_snapshot": {
                "messages": [{"role": "assistant", "api_content": "nested"}]
            },
        }
    )

    assert safe["messages"] == [{"role": "user", "content": "visible"}]
    assert safe["context_messages"] == [{"role": "assistant", "content": "answer"}]
    assert safe["runtime_journal_snapshot"]["messages"] == [{"role": "assistant"}]


def test_issue6751_public_projection_preserves_non_message_alias_keys(monkeypatch):
    import api.config as config
    from api.helpers import public_session_projection

    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": False})
    safe = public_session_projection(
        {
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "api_content": "user-json-field",
                        "_db_row_id": 41,
                    },
                }
            ],
            "tool_calls": [
                {
                    "name": "tool",
                    "api_content": "tool-payload-field",
                    "_db_row_id": 42,
                }
            ],
        }
    )

    assert safe["messages"][0]["content"] == {
        "api_content": "user-json-field",
        "_db_row_id": 41,
    }
    assert "api_content" not in safe["tool_calls"][0]
    assert "_db_row_id" not in safe["tool_calls"][0]


def test_issue6751_provider_projection_still_strips_api_content_by_default():
    from api.streaming import _sanitize_messages_for_api

    sanitized = _sanitize_messages_for_api(
        [{
            "role": "user",
            "content": "same visible text",
            "api_content": "[Workspace::v1: /tmp]\\nsame visible text",
        }]
    )

    assert sanitized == [{"role": "user", "content": "same visible text"}]


def test_issue6751_sync_chat_agent_receives_original_api_content_bytes(monkeypatch, tmp_path):
    """The production sync route must hand the Agent the original wire text."""
    import api.config as config
    import api.models as models
    import api.routes as routes

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "get_session", models.get_session)
    monkeypatch.setattr(routes, "title_from", models.title_from)
    monkeypatch.setattr(config, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: tmp_path)
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])

    original_wire = "[Workspace::v1: /original-workspace]\nturn-two request"
    context = [
        {
            "role": "user",
            "content": "turn-two request",
            "timestamp": 2.0,
            "api_content": original_wire,
        },
        {"role": "assistant", "content": "turn-two answer", "timestamp": 3.0},
    ]
    session = models.Session(
        session_id="issue6751_sync_agent",
        workspace=str(tmp_path),
        messages=list(context),
        context_messages=list(context),
        model="test-model",
        model_provider="test-provider",
    )
    session.save(touch_updated_at=False)

    captured = {}

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **kwargs):
            captured["history"] = list(kwargs.get("conversation_history") or [])
            return {
                "messages": captured["history"]
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "ok"},
                ],
                "final_response": "ok",
                "completed": True,
            }

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))

    class Handler:
        def __init__(self):
            self.headers = {}
            self.status = None
            self.wfile = SimpleNamespace(write=lambda _body: None)

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            return None

        def end_headers(self):
            return None

    handler = Handler()
    routes._handle_chat_sync(
        handler,
        {
            "session_id": session.session_id,
            "message": "follow-up",
            "workspace": str(tmp_path),
        },
    )

    assert handler.status == 200
    assert [message.get("api_content") for message in captured["history"] if message.get("role") == "user"] == [
        original_wire
    ]


def test_issue6751_json_import_strips_internal_aliases_before_persistence(monkeypatch, tmp_path):
    """Caller-supplied provider bytes never become durable imported context."""
    from collections import OrderedDict

    import api.config as config
    import api.models as models
    import api.routes as routes

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    sessions = OrderedDict()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: tmp_path)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": False})
    captured = {}

    def fake_json(handler, payload, status=200, **_kwargs):
        captured["payload"] = payload
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "j", fake_json)
    routes._handle_session_import(
        None,
        {
            "title": "imported",
            "workspace": str(tmp_path),
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "visible": "visible",
                        "api_content": "user-json-field",
                        "_db_row_id": 41,
                    },
                    "api_content": "caller provider bytes",
                    "_state_db_row_id": 1,
                    "_db_row_id": 2,
                    "state_db_row_id": 3,
                }
            ],
            "context_messages": [
                {"role": "assistant", "content": "answer", "api_content": "hidden"}
            ],
            "tool_calls": [
                {
                    "name": "tool",
                    "api_content": "nested hidden",
                    "_db_row_id": 3,
                    "arguments": {
                        "api_content": "tool-argument-field",
                        "_db_row_id": 42,
                    },
                }
            ],
        },
    )

    assert captured["status"] == 200
    imported = next(iter(sessions.values()))
    assert imported.messages == [
        {
            "role": "user",
            "content": {
                "visible": "visible",
                "api_content": "user-json-field",
                "_db_row_id": 41,
            },
        }
    ]
    assert imported.context_messages == []
    assert imported.tool_calls == [
        {
            "name": "tool",
            "arguments": {
                "api_content": "tool-argument-field",
                "_db_row_id": 42,
            },
        }
    ]
    persisted = json.loads(imported.path.read_text(encoding="utf-8"))
    aliases = ("api_content", "_state_db_row_id", "_db_row_id", "state_db_row_id")
    assert all(alias not in persisted["messages"][0] for alias in aliases)
    assert all(alias not in persisted["tool_calls"][0] for alias in aliases)
    assert persisted["messages"][0]["content"]["api_content"] == "user-json-field"
    assert persisted["messages"][0]["content"]["_db_row_id"] == 41
    assert persisted["tool_calls"][0]["arguments"]["api_content"] == "tool-argument-field"
    assert persisted["tool_calls"][0]["arguments"]["_db_row_id"] == 42


def test_issue6751_json_import_nested_tool_calls_are_removed_at_agent_boundary(
    monkeypatch, tmp_path
):
    """Import, reload, and the sync Agent boundary must not pass nested aliases."""
    from collections import OrderedDict

    import api.config as config
    import api.models as models
    import api.routes as routes

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    sessions = OrderedDict()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: tmp_path)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": False})
    monkeypatch.setattr(config, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])
    captured_response = {}

    def fake_json(handler, payload, status=200, **_kwargs):
        captured_response["payload"] = payload
        captured_response["status"] = status
        return True

    _real_routes_json = routes.j
    monkeypatch.setattr(routes, "j", fake_json)
    routes._handle_session_import(
        None,
        {
            "title": "nested-import",
            "workspace": str(tmp_path),
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "answer",
                            "api_content": "ordinary content field",
                        }
                    ],
                    "_state_db_row_id": 1,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "api_content": "nested provider bytes",
                            "_db_row_id": 2,
                            "args": {
                                "messages": [
                                    {"api_content": "import-business-payload"}
                                ]
                            },
                            "function": {
                                "name": "lookup",
                                "arguments": '{"api_content":"ordinary argument field"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "content": "lookup result",
                },
            ],
            "tool_calls": [
                {
                    "id": "call-1",
                    "api_content": "session provider bytes",
                    "state_db_row_id": 3,
                    "arguments": {"_db_row_id": 4},
                }
            ],
        },
    )

    assert captured_response["status"] == 200
    imported = next(iter(sessions.values()))
    # Reload from the persisted sidecar to cover the import -> disk -> load path.
    reloaded = models.Session.load(imported.session_id)
    assert reloaded is not None
    nested = reloaded.messages[0]
    aliases = ("api_content", "_state_db_row_id", "_db_row_id", "state_db_row_id")
    assert all(alias not in nested for alias in aliases)
    assert all(alias not in nested["tool_calls"][0] for alias in aliases)
    assert "api_content" not in nested["content"][0]
    assert nested["tool_calls"][0]["function"]["arguments"] == (
        '{"api_content":"ordinary argument field"}'
    )
    assert nested["tool_calls"][0]["args"]["messages"] == [
        {"api_content": "import-business-payload"}
    ]
    assert reloaded.tool_calls == [{"id": "call-1", "arguments": {"_db_row_id": 4}}]

    # Restore the real response writer for the production sync route below.
    monkeypatch.setattr(routes, "j", _real_routes_json)

    captured_agent = {}

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **kwargs):
            captured_agent["history"] = list(kwargs.get("conversation_history") or [])
            return {
                "messages": captured_agent["history"]
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "ok"},
                ],
                "final_response": "ok",
                "completed": True,
            }

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    monkeypatch.setattr(routes, "get_session", lambda _sid: reloaded)

    class Handler:
        def __init__(self):
            self.headers = {}
            self.status = None
            self.wfile = SimpleNamespace(write=lambda _body: None)

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            return None

        def end_headers(self):
            return None

    handler = Handler()
    routes._handle_chat_sync(
        handler,
        {
            "session_id": reloaded.session_id,
            "message": "follow-up",
            "workspace": str(tmp_path),
        },
    )

    assert handler.status == 200
    agent_history_row = next(
        message for message in captured_agent["history"] if message.get("role") == "assistant"
    )
    assert all(alias not in agent_history_row for alias in aliases)
    assert all(alias not in agent_history_row["tool_calls"][0] for alias in aliases)


def test_issue6751_json_import_rejects_non_list_session_tool_calls(monkeypatch, tmp_path):
    """Session-level tool_calls are a list schema, not an arbitrary JSON value."""
    from collections import OrderedDict

    import api.config as config
    import api.helpers as helpers
    import api.models as models
    import api.routes as routes

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    sessions = OrderedDict()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: tmp_path)
    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": False})
    captured = {}

    def fake_json(handler, payload, status=200, **_kwargs):
        captured["payload"] = payload
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "j", fake_json)
    monkeypatch.setattr(helpers, "j", fake_json)
    routes._handle_session_import(
        None,
        {
            "title": "invalid-tool-calls",
            "workspace": str(tmp_path),
            "messages": [],
            "tool_calls": {"api_content": "must-not-be-a-session-list"},
        },
    )

    assert captured["status"] == 400
    assert sessions == OrderedDict()


def test_issue6751_context_dedup_keeps_distinct_api_content_histories():
    from api.streaming import _deduplicate_context_messages

    history = [
        {"role": "user", "content": "same visible", "api_content": "wire-one"},
        {"role": "user", "content": "same visible", "api_content": "wire-two"},
    ]

    deduped = _deduplicate_context_messages(history)

    assert [message["api_content"] for message in deduped] == ["wire-one", "wire-two"]


def test_issue6751_context_dedup_ignores_malformed_api_content():
    from api.streaming import _deduplicate_context_messages

    history = [
        {"role": "user", "content": "same visible", "api_content": {"bad": True}},
        {"role": "user", "content": "same visible"},
    ]

    assert _deduplicate_context_messages(history) == history[:1]


def test_issue6751_source_only_duplicate_durable_id_does_not_fallback_attach():
    from api.models import merge_session_messages_append_only

    sidecar = [{"role": "user", "content": "same", "timestamp": 100.0}]
    state = [
        {
            "role": "user",
            "content": "same",
            "timestamp": 100.0,
            "_state_db_row_id": 7,
            "api_content": "wire-one",
        },
        {
            "role": "user",
            "content": "same",
            "timestamp": 200.0,
            "_state_db_row_id": 7,
            "api_content": "wire-two",
        },
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert merged[0].get("api_content") is None


def test_issue6751_ephemeral_terminal_sse_projects_agent_messages(monkeypatch):
    import api.config as config
    from api.streaming import _ephemeral_session_payload

    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": False})
    payload = _ephemeral_session_payload(
        "ephemeral-sid",
        [
            {
                "role": "assistant",
                "content": "visible",
                "api_content": "raw provider bytes",
                "_state_db_row_id": 7,
                "_db_row_id": 8,
                "state_db_row_id": 9,
            }
        ],
    )

    assert payload == {
        "session_id": "ephemeral-sid",
        "messages": [{"role": "assistant", "content": "visible"}],
    }


def test_issue6751_schema_scrubber_preserves_tool_argument_business_payload(monkeypatch):
    """Only authoritative message/tool schemas are scrubbed; args are opaque."""
    import api.config as config
    from api.helpers import public_session_projection

    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": False})
    payload = public_session_projection(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "visible",
                            "api_content": "content-part-internal",
                        }
                    ],
                    "api_content": "message-wire",
                    "_state_db_row_id": 7,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "api_content": "tool-call-internal",
                            "function": {
                                "name": "lookup",
                                "api_content": "function-internal",
                                "arguments": '{"api_content":"opaque-argument"}',
                            },
                            "args": {
                                "messages": [
                                    {
                                        "api_content": "business-payload",
                                        "_db_row_id": 41,
                                    }
                                ]
                            },
                        }
                    ],
                }
            ],
            "runtime_journal_snapshot": {
                "messages": [
                    {
                        "role": "assistant",
                        "content": "snapshot-visible",
                        "api_content": "snapshot-wire",
                    }
                ],
                "tool_calls": [
                    {
                        "name": "lookup",
                        "args": {
                            "messages": [
                                {
                                    "api_content": "snapshot-business-payload",
                                }
                            ]
                        },
                    }
                ],
            },
        }
    )

    message = payload["messages"][0]
    assert "api_content" not in message
    assert "_state_db_row_id" not in message
    assert "api_content" not in message["content"][0]
    tool_call = message["tool_calls"][0]
    assert "api_content" not in tool_call
    assert "api_content" not in tool_call["function"]
    assert tool_call["function"]["arguments"] == '{"api_content":"opaque-argument"}'
    assert tool_call["args"]["messages"][0] == {
        "api_content": "business-payload",
        "_db_row_id": 41,
    }
    assert payload["runtime_journal_snapshot"]["messages"] == [
        {"role": "assistant", "content": "snapshot-visible"}
    ]
    assert payload["runtime_journal_snapshot"]["tool_calls"][0]["args"]["messages"][0] == {
        "api_content": "snapshot-business-payload"
    }


def test_issue6751_agent_schema_scrubber_keeps_message_sidecar_only():
    from api.streaming import _sanitize_messages_for_agent

    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "visible", "api_content": "part-internal"}
            ],
            "api_content": "message-wire",
            "tool_calls": [
                {
                    "id": "call-1",
                    "api_content": "tool-call-internal",
                    "function": {
                        "name": "lookup",
                        "api_content": "function-internal",
                        "arguments": '{"api_content":"opaque-argument"}',
                    },
                    "args": {"messages": [{"api_content": "business-payload"}]},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]

    sanitized = _sanitize_messages_for_agent(messages)

    assistant = sanitized[0]
    assert assistant["api_content"] == "message-wire"
    assert "api_content" not in assistant["content"][0]
    tool_call = assistant["tool_calls"][0]
    assert "api_content" not in tool_call
    assert "api_content" not in tool_call["function"]
    assert tool_call["function"]["arguments"] == '{"api_content":"opaque-argument"}'
    assert tool_call["args"]["messages"] == [{"api_content": "business-payload"}]


def test_issue6751_session_arc_fuzzy_replay_requires_equal_api_content():
    from api.streaming import _looks_like_replayed_session_arc_summary

    summary_a = {
        "role": "user",
        "content": "[Session Arc Summary]\n" + "same recovered context\n" * 260,
        "api_content": "wire-one",
    }
    summary_b = dict(summary_a, api_content="wire-two")

    assert not _looks_like_replayed_session_arc_summary(summary_a, summary_b)
    assert _looks_like_replayed_session_arc_summary(summary_a, dict(summary_a))


@pytest.mark.parametrize("bad_row_id", ["invalid-row-id", "nan", "inf", float("nan"), float("inf")])
def test_issue6751_invalid_row_id_rejects_metadata_free_fallback(bad_row_id):
    from api.models import merge_session_messages_append_only

    sidecar = [{"role": "user", "content": "same"}]
    state = [
        {
            "role": "user",
            "content": "same",
            "_state_db_row_id": bad_row_id,
            "api_content": "wire",
        }
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert merged[0].get("api_content") is None


@pytest.mark.parametrize("bad_timestamp", ["invalid-time", "nan", "inf", float("nan"), float("inf")])
def test_issue6751_invalid_timestamp_rejects_metadata_free_fallback(bad_timestamp):
    from api.models import merge_session_messages_append_only

    sidecar = [{"role": "user", "content": "same"}]
    state = [
        {
            "role": "user",
            "content": "same",
            "timestamp": bad_timestamp,
            "api_content": "wire",
        }
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert merged[0].get("api_content") is None


def test_issue6751_distinct_state_sidecars_are_not_deduplicated():
    from api.models import merge_session_messages_append_only

    merged = merge_session_messages_append_only(
        [],
        [
            {"role": "user", "content": "same", "api_content": "wire-one"},
            {"role": "user", "content": "same", "api_content": "wire-two"},
        ],
    )

    assert [message["api_content"] for message in merged] == ["wire-one", "wire-two"]


def test_issue6751_same_row_id_different_sidecars_remain_distinct():
    """A durable row id cannot override distinct provider payload identity."""
    from api.models import merge_session_messages_append_only

    sidecar = [
        {
            "role": "user",
            "content": "same visible",
            "timestamp": 100.0,
            "_state_db_row_id": 7,
            "api_content": "wire-one",
        }
    ]
    state = [
        {
            "role": "user",
            "content": "same visible",
            "timestamp": 100.0,
            "_state_db_row_id": 7,
            "api_content": "wire-two",
        }
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert [message.get("api_content") for message in merged] == [
        "wire-one",
        "wire-two",
    ]
