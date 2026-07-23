"""Regression coverage for stitched full-transcript loading across session segments."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import api.models as models
import api.routes as routes



def test_session_endpoint_merges_sidecar_and_lineage_messages_for_cli_sessions(monkeypatch):
    class DummySession:
        def __init__(self):
            self.messages = [{"role": "assistant", "content": "sidecar tail", "timestamp": 10.0}]
            self.tool_calls = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.model = "openai/gpt-5"
            self.session_id = "tip"

        def compact(self):
            return {"session_id": "tip", "title": "Tip", "model": "openai/gpt-5"}

    captured = {}

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: DummySession())
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda s: None)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {"session_source": "messaging"})
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda s: True)
    monkeypatch.setattr(
        routes,
        "get_cli_session_messages",
        lambda sid: [
            {"role": "user", "content": "root user", "timestamp": 1.0},
            {"role": "assistant", "content": "tip assistant", "timestamp": 2.0},
        ],
    )
    monkeypatch.setattr(routes, "_resolve_effective_session_model_for_display", lambda s: getattr(s, "model", None))
    monkeypatch.setattr(routes, "_resolve_effective_session_model_provider_for_display", lambda s: None)
    monkeypatch.setattr(routes, "_merge_cli_sidebar_metadata", lambda raw, meta: raw)
    monkeypatch.setattr(routes, "redact_session_data", lambda raw: raw)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200: captured.setdefault("payload", payload))

    class Handler:
        pass

    class Parsed:
        path = "/api/session"
        query = "session_id=tip"

    routes.handle_get(Handler(), Parsed())

    session = captured["payload"]["session"]
    assert [m["content"] for m in session["messages"]] == [
        "root user",
        "tip assistant",
        "sidecar tail",
    ]


def test_session_endpoint_preserves_distinct_messages_with_different_ids(monkeypatch):
    class DummySession:
        def __init__(self):
            self.messages = [
                {
                    "id": "sidecar-retry",
                    "role": "user",
                    "content": "retry the same request",
                    "timestamp": 2.0,
                }
            ]
            self.tool_calls = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.model = "openai/gpt-5"
            self.session_id = "tip"

        def compact(self):
            return {"session_id": "tip", "title": "Tip", "model": "openai/gpt-5"}

    captured = {}

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: DummySession())
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda s: None)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {"session_source": "messaging"})
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda s: True)
    monkeypatch.setattr(
        routes,
        "get_cli_session_messages",
        lambda sid: [
            {"role": "user", "content": "root user", "timestamp": 1.0},
            {
                "id": "cli-retry",
                "role": "user",
                "content": "retry the same request",
                "timestamp": 2.0,
            },
        ],
    )
    monkeypatch.setattr(routes, "_resolve_effective_session_model_for_display", lambda s: getattr(s, "model", None))
    monkeypatch.setattr(routes, "_resolve_effective_session_model_provider_for_display", lambda s: None)
    monkeypatch.setattr(routes, "_merge_cli_sidebar_metadata", lambda raw, meta: raw)
    monkeypatch.setattr(routes, "redact_session_data", lambda raw: raw)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200: captured.setdefault("payload", payload))

    class Handler:
        pass

    class Parsed:
        path = "/api/session"
        query = "session_id=tip"

    routes.handle_get(Handler(), Parsed())

    session = captured["payload"]["session"]
    retry_messages = [m for m in session["messages"] if m.get("content") == "retry the same request"]
    assert [m.get("id") for m in retry_messages] == ["cli-retry", "sidecar-retry"]



def test_cli_continuation_session_opens_nonempty(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            parent_session_id TEXT,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            name TEXT,
            reasoning TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sessions (id, source, parent_session_id, started_at, ended_at, end_reason)
        VALUES
            ('parent-session', 'telegram', NULL, 100.0, 200.0, 'cli_close'),
            ('child-session', 'telegram', 'parent-session', 201.0, NULL, NULL)
        """
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, role, content, timestamp)
        VALUES
            ('parent-session', 'user', 'parent turn', '2026-05-14 10:00:01'),
            ('child-session', 'assistant', 'child reply', '2026-05-14 10:01:01')
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(models, '_active_state_db_path', lambda: db_path)

    messages = models.get_cli_session_messages('child-session')

    assert [message['content'] for message in messages] == ['parent turn', 'child reply']


def test_webui_continuation_session_opens_with_snapshot_parent_messages(monkeypatch):
    """Opening a WebUI compression child should expose the archived parent transcript."""
    parent = SimpleNamespace(
        session_id="parent-webui",
        parent_session_id=None,
        pre_compression_snapshot=True,
        truncation_watermark=None,
        messages=[
            {"role": "user", "content": "make the LLM settings table", "timestamp": 1.0},
            {"role": "assistant", "content": "LLM Settings Table", "timestamp": 2.0},
        ],
    )
    child = SimpleNamespace(
        session_id="child-webui",
        parent_session_id="parent-webui",
        pre_compression_snapshot=False,
        truncation_watermark=None,
        messages=[
            {"role": "user", "content": "continue after compression", "timestamp": 3.0},
            {"role": "assistant", "content": "child reply", "timestamp": 4.0},
        ],
        tool_calls=[],
        active_stream_id=None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        context_length=0,
        threshold_tokens=0,
        last_prompt_tokens=0,
        model="openai/gpt-5",
        profile="default",
    )
    child.compact = lambda: {"session_id": "child-webui", "title": "Child", "model": "openai/gpt-5"}

    captured = {}
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: child)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda s: None)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda s: False)
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda sid, profile=None, since_timestamp=None, include_inactive=False, limit=None: [])
    monkeypatch.setattr(routes.Session, "load", lambda sid: parent if sid == "parent-webui" else None)
    monkeypatch.setattr(routes, "_resolve_effective_session_model_for_display", lambda s: getattr(s, "model", None))
    monkeypatch.setattr(routes, "_resolve_effective_session_model_provider_for_display", lambda s: None)
    monkeypatch.setattr(routes, "_merge_cli_sidebar_metadata", lambda raw, meta: raw)
    monkeypatch.setattr(routes, "redact_session_data", lambda raw: raw)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200: captured.setdefault("payload", payload))

    class Handler:
        pass

    class Parsed:
        path = "/api/session"
        query = "session_id=child-webui"

    routes.handle_get(Handler(), Parsed())

    contents = [m["content"] for m in captured["payload"]["session"]["messages"]]
    assert contents == [
        "make the LLM settings table",
        "LLM Settings Table",
        "continue after compression",
        "child reply",
    ]


def test_webui_fork_session_does_not_stitch_non_snapshot_parent(monkeypatch):
    """A normal fork's parent_session_id is provenance, not a transcript stitch request."""
    parent = SimpleNamespace(
        session_id="parent-fork",
        parent_session_id=None,
        pre_compression_snapshot=False,
        truncation_watermark=None,
        messages=[{"role": "user", "content": "parent should stay separate", "timestamp": 1.0}],
    )
    child = SimpleNamespace(
        session_id="child-fork",
        parent_session_id="parent-fork",
        pre_compression_snapshot=False,
        truncation_watermark=None,
        messages=[{"role": "user", "content": "fork child only", "timestamp": 2.0}],
    )

    monkeypatch.setattr(routes.Session, "load", lambda sid: parent if sid == "parent-fork" else None)

    assert [m["content"] for m in routes._webui_sidecar_lineage_messages_for_display(child)] == [
        "fork child only",
    ]


def test_webui_fork_session_does_not_stitch_snapshot_parent(monkeypatch):
    """A fork child must stay isolated even if its parent later becomes a snapshot."""
    parent = SimpleNamespace(
        session_id="parent-snapshot-fork",
        parent_session_id=None,
        session_source="webui",
        pre_compression_snapshot=True,
        truncation_watermark=None,
        messages=[{"role": "user", "content": "parent should stay separate", "timestamp": 1.0}],
    )
    child = SimpleNamespace(
        session_id="child-snapshot-fork",
        parent_session_id="parent-snapshot-fork",
        session_source="fork",
        pre_compression_snapshot=False,
        truncation_watermark=None,
        messages=[{"role": "user", "content": "fork child only", "timestamp": 2.0}],
    )

    monkeypatch.setattr(routes.Session, "load", lambda sid: parent if sid == "parent-snapshot-fork" else None)

    assert [m["content"] for m in routes._webui_sidecar_lineage_messages_for_display(child)] == [
        "fork child only",
    ]


def test_webui_compressed_fork_stitches_fork_snapshots_only(monkeypatch):
    original_parent = SimpleNamespace(
        session_id="original-fork-parent",
        parent_session_id=None,
        session_source="webui",
        pre_compression_snapshot=True,
        truncation_watermark=None,
        messages=[{"role": "user", "content": "original parent should stay separate", "timestamp": 0.0}],
    )
    first_snapshot = SimpleNamespace(
        session_id="fork-compression-snapshot-1",
        parent_session_id="original-fork-parent",
        session_source="fork",
        pre_compression_snapshot=True,
        truncation_watermark=None,
        messages=[{"role": "user", "content": "before first fork compression", "timestamp": 1.0}],
    )
    second_snapshot = SimpleNamespace(
        session_id="fork-compression-snapshot-2",
        parent_session_id="fork-compression-snapshot-1",
        session_source="fork",
        pre_compression_snapshot=True,
        truncation_watermark=None,
        messages=[{"role": "assistant", "content": "before second fork compression", "timestamp": 2.0}],
    )
    child = SimpleNamespace(
        session_id="fork-compression-child",
        parent_session_id="fork-compression-snapshot-2",
        session_source="fork",
        pre_compression_snapshot=False,
        truncation_watermark=None,
        messages=[{"role": "assistant", "content": "after fork compression", "timestamp": 3.0}],
    )
    by_id = {
        "original-fork-parent": original_parent,
        "fork-compression-snapshot-1": first_snapshot,
        "fork-compression-snapshot-2": second_snapshot,
    }

    monkeypatch.setattr(routes.Session, "load", lambda sid: by_id.get(sid))

    assert [m["content"] for m in routes._webui_sidecar_lineage_messages_for_display(child)] == [
        "before first fork compression",
        "before second fork compression",
        "after fork compression",
    ]


def test_webui_merged_lineage_keeps_session_source_fork_isolated(monkeypatch):
    parent = SimpleNamespace(
        session_id="merged-parent-fork",
        messages=[{"role": "user", "content": "parent only", "timestamp": 1.0}],
    )
    fork = SimpleNamespace(
        session_id="merged-fork",
        parent_session_id="merged-parent-fork",
        session_source="fork",
        relationship_type="",
        messages=[{"role": "user", "content": "fork starts here", "timestamp": 2.0}],
        truncation_watermark=None,
    )

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: parent)

    merged = routes._merged_webui_lineage_messages_for_display(fork, fork.messages)

    assert [m["content"] for m in merged] == ["fork starts here"]


def test_webui_merged_lineage_keeps_child_relationship_isolated(monkeypatch):
    parent = SimpleNamespace(
        session_id="merged-parent-child",
        messages=[{"role": "user", "content": "parent only", "timestamp": 1.0}],
    )
    child = SimpleNamespace(
        session_id="merged-child",
        parent_session_id="merged-parent-child",
        session_source="",
        relationship_type="child_session",
        messages=[{"role": "user", "content": "child starts here", "timestamp": 2.0}],
        truncation_watermark=None,
    )

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: parent)

    merged = routes._merged_webui_lineage_messages_for_display(child, child.messages)

    assert [m["content"] for m in merged] == ["child starts here"]


def test_webui_lineage_display_keeps_child_tail_after_snapshot_watermark(monkeypatch):
    """A child sidecar watermark must not delete the child's persisted continuation tail."""
    parent = SimpleNamespace(
        session_id="parent-watermark",
        parent_session_id=None,
        pre_compression_snapshot=True,
        truncation_watermark=None,
        messages=[
            {"role": "user", "content": "parent user", "timestamp": 1000.0},
            {"role": "assistant", "content": "parent assistant", "timestamp": 1001.0},
        ],
    )
    child = SimpleNamespace(
        session_id="child-watermark",
        parent_session_id="parent-watermark",
        pre_compression_snapshot=False,
        truncation_watermark=1001.0,
        messages=[
            {"role": "user", "content": "child user", "timestamp": 1002.0},
            {"role": "assistant", "content": "child assistant final", "timestamp": 1003.0},
        ],
    )
    monkeypatch.setattr(routes.Session, "load", lambda sid: parent if sid == "parent-watermark" else None)

    contents = [m["content"] for m in routes._webui_sidecar_lineage_messages_for_display(child)]

    assert contents == [
        "parent user",
        "parent assistant",
        "child user",
        "child assistant final",
    ]


def test_webui_lineage_display_does_not_restitch_ancestor_when_child_replays_parent(monkeypatch):
    """If the child sidecar already starts with its direct parent snapshot, use it as-is."""
    grandparent = SimpleNamespace(
        session_id="grandparent-replayed",
        parent_session_id=None,
        pre_compression_snapshot=True,
        truncation_watermark=None,
        messages=[
            {"role": "user", "content": "grandparent user", "timestamp": 1000.0},
            {"role": "assistant", "content": "grandparent assistant", "timestamp": 1001.0},
        ],
    )
    parent = SimpleNamespace(
        session_id="parent-replayed",
        parent_session_id="grandparent-replayed",
        pre_compression_snapshot=True,
        truncation_watermark=1001.0,
        messages=grandparent.messages + [
            {"role": "user", "content": "parent user", "timestamp": 1002.0},
            {"role": "assistant", "content": "parent assistant", "timestamp": 1003.0},
        ],
    )
    child = SimpleNamespace(
        session_id="child-replayed",
        parent_session_id="parent-replayed",
        pre_compression_snapshot=False,
        truncation_watermark=1001.0,
        messages=parent.messages + [
            {"role": "user", "content": "followup user", "timestamp": 1004.0},
            {"role": "assistant", "content": "latest final answer", "timestamp": 1005.0},
        ],
    )
    by_id = {"parent-replayed": parent, "grandparent-replayed": grandparent}
    monkeypatch.setattr(routes.Session, "load", lambda sid: by_id.get(sid))

    contents = [m["content"] for m in routes._webui_sidecar_lineage_messages_for_display(child)]

    assert contents == [
        "grandparent user",
        "grandparent assistant",
        "parent user",
        "parent assistant",
        "followup user",
        "latest final answer",
    ]
    assert contents[-1] == "latest final answer"
