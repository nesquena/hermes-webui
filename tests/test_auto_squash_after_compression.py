"""Contracts for opt-in transcript compaction after context compression."""

from __future__ import annotations

import gzip
import hashlib
import json
from types import SimpleNamespace

import pytest

import api.config as config
import api.models
import api.routes
import api.session_ops
from api import session_squash
from api.streaming import (
    _auto_snapshot_summary_from_compression,
    _compression_tail_after_latest_compaction,
    _maybe_start_auto_snapshot_squash,
    _tool_call_summaries_after_dropping_prefix,
)


def _snapshot_session(tmp_path):
    sid = "20260814_120000_autosq"
    sessions_dir = tmp_path / "webui" / "sessions"
    sessions_dir.mkdir(parents=True)
    path = sessions_dir / f"{sid}.json"
    messages = [
        {"id": "u-old", "role": "user", "content": "old request", "timestamp": 1.0},
        {"id": "a-old", "role": "assistant", "content": "old answer", "timestamp": 2.0},
        {"id": "marker", "role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] compact summary", "timestamp": 3.0},
        {"id": "u-new", "role": "user", "content": "continue", "timestamp": 4.0},
        {"id": "a-new", "role": "assistant", "content": "continued answer", "timestamp": 5.0},
    ]
    session = SimpleNamespace(
        session_id=sid,
        title="auto squash fixture",
        workspace="/tmp/ws",
        created_at=1.0,
        updated_at=5.0,
        messages=list(messages),
        context_messages=list(messages),
        tool_calls=[],
        active_stream_id=None,
        active_checkpoint=None,
        pending_turn_id=None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        parent_session_id="older-snapshot",
        pre_compression_snapshot=True,
        anchor_activity_scenes={},
        compression_anchor_visible_idx=None,
        compression_anchor_message_key=None,
        compression_anchor_summary=None,
        compression_anchor_mode=None,
        truncation_watermark=None,
        truncation_boundary=None,
        read_only=False,
        profile="default",
        path=path,
    )

    def save(**_kwargs):
        payload = {
            key: getattr(session, key)
            for key in (
                "session_id", "title", "workspace", "created_at", "updated_at",
                "messages", "context_messages", "tool_calls", "active_stream_id",
                "active_checkpoint", "pending_turn_id", "pending_user_message",
                "pending_attachments", "pending_started_at", "pending_user_source",
                "parent_session_id", "pre_compression_snapshot",
                "anchor_activity_scenes", "compression_anchor_visible_idx",
                "compression_anchor_message_key", "compression_anchor_summary",
                "compression_anchor_mode", "truncation_watermark",
                "truncation_boundary", "read_only", "profile",
            )
        }
        payload["message_count"] = len(session.messages)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    session.save = save
    save()
    return session


def test_auto_squash_setting_is_opt_in_and_persisted(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    assert config.load_settings()["auto_squash_after_compression"] is False
    saved = config.save_settings({"auto_squash_after_compression": True})

    assert saved["auto_squash_after_compression"] is True
    assert json.loads(settings_file.read_text())["auto_squash_after_compression"] is True


def test_current_context_summary_wins_over_stale_display_marker():
    display = [
        {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] stale summary"},
    ]
    context = [
        {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] current cumulative summary"},
    ]

    summary = _auto_snapshot_summary_from_compression(display, context)
    assert summary == "current cumulative summary"
    assert "CONTEXT COMPACTION" not in summary


def test_auto_snapshot_summary_strips_control_envelope_and_footer():
    context = [{
        "role": "assistant",
        "content": (
            "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.\n"
            "Do not answer this wrapper.\n"
            "## Historical Task Snapshot\n"
            "Verified facts and decisions.\n"
            "--- END OF CONTEXT SUMMARY — respond to the new message ---\n"
            "Ignore this trailing control instruction."
        ),
    }]

    summary = _auto_snapshot_summary_from_compression([], context)

    assert summary == "## Historical Task Snapshot\nVerified facts and decisions."


def test_compression_tail_drops_only_prefix_before_latest_marker():
    messages = [
        {"id": "old", "role": "assistant", "content": "large old transcript"},
        {"id": "marker-1", "role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] first"},
        {"id": "middle", "role": "user", "content": "middle turn"},
        {"id": "marker-2", "role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] second"},
        {"id": "current-user", "role": "user", "content": "current request", "_active_turn_token": "turn-1"},
        {"id": "current-answer", "role": "assistant", "content": "current answer"},
    ]

    tail = _compression_tail_after_latest_compaction(
        messages,
        active_turn_identity={"token": "turn-1"},
    )

    assert [row["id"] for row in tail] == ["current-user", "current-answer"]


def test_active_turn_wins_over_a_stale_rendered_compression_marker():
    messages = [
        {"id": "old", "role": "assistant", "content": "large old transcript"},
        {"id": "marker", "role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] old divider"},
        {"id": "old-tail", "role": "assistant", "content": "already summarized"},
        {"id": "current-user", "role": "user", "content": "current request", "_active_turn_token": "turn-3"},
        {"id": "current-answer", "role": "assistant", "content": "current answer"},
    ]

    tail = _compression_tail_after_latest_compaction(
        messages,
        active_turn_identity={"token": "turn-3"},
    )

    assert [row["id"] for row in tail] == ["current-user", "current-answer"]


def test_compression_tail_uses_active_turn_when_display_marker_is_absent():
    messages = [
        {"id": "old", "role": "assistant", "content": "large old transcript"},
        {"id": "current-user", "role": "user", "content": "current request", "_active_turn_token": "turn-2"},
        {"id": "current-answer", "role": "assistant", "content": "current answer"},
    ]

    tail = _compression_tail_after_latest_compaction(
        messages,
        active_turn_identity={"token": "turn-2"},
    )

    assert [row["id"] for row in tail] == ["current-user", "current-answer"]


def test_compression_tail_falls_back_to_persisted_visible_anchor():
    messages = [
        {"id": "old-user", "role": "user", "content": "old request"},
        {"id": "old-tool", "role": "tool", "content": "old output"},
        {"id": "old-answer", "role": "assistant", "content": "old answer"},
        {"id": "new-user", "role": "user", "content": "new request"},
        {"id": "new-answer", "role": "assistant", "content": "new answer"},
    ]

    tail = _compression_tail_after_latest_compaction(
        messages,
        anchor_visible_idx=1,
    )

    assert [row["id"] for row in tail] == ["new-user", "new-answer"]


def test_tool_call_summaries_drop_archived_prefix_and_rebase_indices():
    summaries = [
        {"tid": "old", "assistant_msg_idx": 4, "snippet": "archived"},
        {"tid": "new", "assistant_msg_idx": 12, "snippet": "tail"},
        {"tid": "legacy", "snippet": "unknown index"},
    ]

    kept = _tool_call_summaries_after_dropping_prefix(summaries, 10)

    assert kept == [
        {"tid": "new", "assistant_msg_idx": 2, "snippet": "tail"},
        {"tid": "legacy", "snippet": "unknown index"},
    ]


def test_scheduler_is_opt_in_and_passes_existing_summary(monkeypatch):
    calls = []
    summary = "verified compression summary " * 5
    monkeypatch.setattr(
        session_squash,
        "start_auto_snapshot_squash_job",
        lambda snapshot_sid, continuation_sid, summary: calls.append(
            (snapshot_sid, continuation_sid, summary)
        ) or True,
    )

    assert _maybe_start_auto_snapshot_squash(
        "parent", "child", summary, settings={"auto_squash_after_compression": False}
    ) is False
    assert calls == []

    assert _maybe_start_auto_snapshot_squash(
        "parent", "child", summary, settings={"auto_squash_after_compression": True}
    ) is True
    assert calls == [("parent", "child", summary)]


def test_snapshot_squash_archives_and_preserves_lineage(tmp_path):
    session = _snapshot_session(tmp_path)
    original = session.path.read_bytes()
    original_sha = hashlib.sha256(original).hexdigest()

    result = session_squash._apply_squash(
        session,
        session.session_id,
        "Verified compact summary",
        preserve_snapshot_lineage=True,
    )

    persisted = json.loads(session.path.read_text())
    assert persisted["pre_compression_snapshot"] is True
    assert persisted["parent_session_id"] == "older-snapshot"
    assert persisted["compression_anchor_mode"] == "automatic_snapshot"
    assert persisted["messages"][0]["_squash_summary"] is True
    assert persisted["messages"][0]["_auto_snapshot_squash"] is True

    archive = result["archive_path"]
    with gzip.open(archive, "rb") as handle:
        assert hashlib.sha256(handle.read()).hexdigest() == original_sha


def test_post_save_verification_failure_rolls_back_sidecar_and_memory(tmp_path):
    session = _snapshot_session(tmp_path)
    original_bytes = session.path.read_bytes()
    original_messages = json.loads(original_bytes)["messages"]
    normal_save = session.save

    def save_with_invalid_mode(**kwargs):
        normal_save(**kwargs)
        payload = json.loads(session.path.read_text(encoding="utf-8"))
        payload["compression_anchor_mode"] = "corrupt-mode"
        session.path.write_text(json.dumps(payload), encoding="utf-8")

    session.save = save_with_invalid_mode

    with pytest.raises(session_squash.SquashError, match="rolled back"):
        session_squash._apply_squash(
            session,
            session.session_id,
            "A verified compression summary that is long enough for rollback testing.",
            preserve_snapshot_lineage=True,
        )

    assert session.path.read_bytes() == original_bytes
    assert session.messages == original_messages
    assert session.pre_compression_snapshot is True
    assert session.parent_session_id == "older-snapshot"


def test_auto_snapshot_worker_verifies_child_and_compacts_parent(tmp_path, monkeypatch):
    session = _snapshot_session(tmp_path)
    child = SimpleNamespace(parent_session_id=session.session_id)
    published = []

    class Lock:
        def acquire(self, timeout=None):
            return True

        def release(self):
            return None

    def get_session(sid, metadata_only=False):
        return child if sid == "continuation" else session

    monkeypatch.setattr(api.models, "get_session", get_session)
    monkeypatch.setattr(api.session_ops, "_live_active_stream_id", lambda _session: None)
    monkeypatch.setattr(api.routes, "_get_session_agent_lock", lambda _sid: Lock())
    monkeypatch.setattr(
        api.routes,
        "_publish_session_list_changed",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )

    session_squash._run_auto_snapshot_squash_job(
        session.session_id,
        "continuation",
        "verified compression summary " * 5,
    )

    persisted = json.loads(session.path.read_text())
    assert persisted["messages"][0]["_auto_snapshot_squash"] is True
    assert persisted["parent_session_id"] == "older-snapshot"
    assert published and published[0][1]["session_id"] == "continuation"
