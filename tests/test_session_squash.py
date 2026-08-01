"""Coverage for api/session_squash.py — the in-process squash behind the
WebUI squash button (POST /api/session/squash + GET .../squash/status).

The flow is exercised end-to-end with a fake Session object and a real
sidecar file in a temp dir: job start, guard refusals, archive + manifest,
sidecar mutation contract (single visible summary + compaction marker +
manual anchor + watermark barrier + lineage detach), and job status.
"""

import gzip
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import api.models
import api.routes
import api.session_ops
from api import session_squash


SID = "20260801_120000_ab12cd"


def _make_session(tmp_path, *, messages=None, **overrides):
    sessions_dir = tmp_path / "webui" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{SID}.json"
    if messages is None:
        messages = [
            {"role": "user", "content": "première demande de test", "timestamp": 1000.0},
            {"role": "assistant", "content": "# CONCLUSION\n---\n> 🟢 fait", "timestamp": 1001.0},
            {"role": "user", "content": "seconde demande", "timestamp": 1002.0},
            {"role": "assistant", "content": "réponse finale", "timestamp": 1003.0},
        ]
    sess = SimpleNamespace(
        session_id=SID,
        title="session de test",
        workspace="/tmp/ws",
        created_at=1000.0,
        updated_at=1003.0,
        messages=messages,
        context_messages=list(messages),
        tool_calls=[{"id": "t1"}],
        active_stream_id=None,
        active_checkpoint=None,
        pending_turn_id=None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        parent_session_id="parent-fork-123",
        anchor_activity_scenes={"s1": {"foo": "bar"}},
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
    for key, value in overrides.items():
        setattr(sess, key, value)

    def _save(**_kwargs):
        payload = {
            "session_id": SID,
            "title": sess.title,
            "workspace": sess.workspace,
            "created_at": sess.created_at,
            "updated_at": sess.updated_at,
            "messages": sess.messages,
            "context_messages": sess.context_messages,
            "tool_calls": sess.tool_calls,
            "active_stream_id": sess.active_stream_id,
            "active_checkpoint": sess.active_checkpoint,
            "pending_turn_id": sess.pending_turn_id,
            "pending_user_message": sess.pending_user_message,
            "pending_attachments": sess.pending_attachments,
            "pending_started_at": sess.pending_started_at,
            "pending_user_source": sess.pending_user_source,
            "parent_session_id": sess.parent_session_id,
            "anchor_activity_scenes": sess.anchor_activity_scenes,
            "compression_anchor_visible_idx": sess.compression_anchor_visible_idx,
            "compression_anchor_message_key": sess.compression_anchor_message_key,
            "compression_anchor_summary": sess.compression_anchor_summary,
            "compression_anchor_mode": sess.compression_anchor_mode,
            "truncation_watermark": sess.truncation_watermark,
            "truncation_boundary": sess.truncation_boundary,
            "message_count": len(sess.messages or []),
            "read_only": sess.read_only,
            "profile": sess.profile,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    sess.save = _save
    _save()
    return sess


def _run_job(sess, *, summary="synthèse fournie " * 40):
    summary = summary.strip()
    with patch.object(api.models, "get_session", lambda sid, metadata_only=False: sess), \
         patch.object(api.session_ops, "_live_active_stream_id", lambda _s: None), \
         patch.object(api.routes, "_get_session_agent_lock", _dummy_lock), \
         patch.object(api.routes, "_publish_session_list_changed", lambda *a, **k: None), \
         patch("api.config._evict_session_agent", lambda _sid: None), \
         patch.object(session_squash, "_generate_summary", lambda s, sid, provided: (provided, "provided")):
        job = session_squash.start_squash_job(SID, confirm_session_id=SID, summary=summary)
        deadline = time.time() + 10
        while time.time() < deadline:
            snap = session_squash.squash_job_status(job["job_id"])
            if snap["status"] in ("done", "error"):
                return snap
            time.sleep(0.02)
    raise AssertionError("squash job did not finish")


class _DummyLock:
    def acquire(self, timeout=None):
        return True

    def release(self):
        return None


def _dummy_lock(_sid):
    return _DummyLock()


def test_squash_job_collapses_session(tmp_path):
    sess = _make_session(tmp_path)
    original_bytes = sess.path.read_bytes()
    original_sha = hashlib.sha256(original_bytes).hexdigest()

    snap = _run_job(sess)
    assert snap["status"] == "done", snap.get("error")
    result = snap["result"]
    assert result["already_squashed"] is False
    assert result["before"]["message_count"] == 4
    assert result["after"]["message_count"] == 1
    assert result["original_sha256"] == original_sha

    persisted = json.loads(sess.path.read_text(encoding="utf-8"))
    assert len(persisted["messages"]) == 1
    assert persisted["messages"][0]["_squash_summary"] is True
    assert persisted["messages"][0]["role"] == "assistant"
    assert len(persisted["context_messages"]) == 1
    assert persisted["context_messages"][0]["content"].startswith("[CONTEXT COMPACTION")
    assert persisted["compression_anchor_mode"] == "manual"
    assert persisted["compression_anchor_visible_idx"] == 0
    assert persisted["compression_anchor_message_key"]["role"] == "assistant"
    assert persisted["truncation_watermark"] == persisted["truncation_boundary"]
    assert persisted["truncation_watermark"] > 1003.0
    assert persisted["parent_session_id"] is None
    assert persisted["active_stream_id"] is None
    assert persisted["pending_user_message"] is None
    assert persisted["tool_calls"] == []

    # Archive: gzip payload matches the original checksum, manifest is
    # compatible with the squash-chat skill's restore command.
    archive_path = sess.path.parent.parent / "session-squash-archives" / SID / result["archive_path"].split("/")[-1]
    assert archive_path.is_file()
    with gzip.open(archive_path, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == original_sha
    manifest = json.loads(
        archive_path.with_suffix(archive_path.suffix + ".manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["session_id"] == SID
    assert manifest["source_sha256"] == original_sha

    # No stale .bak may survive (startup recovery could undo the squash).
    assert not sess.path.with_suffix(".json.bak").exists()


def test_squash_requires_confirm(tmp_path):
    _make_session(tmp_path)
    with pytest.raises(session_squash.SquashError):
        session_squash.start_squash_job(SID, confirm_session_id="wrong", summary=None)


def test_squash_refuses_active_session(tmp_path):
    sess = _make_session(tmp_path, active_stream_id="stream-123")
    with patch.object(api.models, "get_session", lambda sid, metadata_only=False: sess):
        with pytest.raises(session_squash.SquashError) as excinfo:
            session_squash.start_squash_job(SID, confirm_session_id=SID, summary=None)
    assert excinfo.value.status == 409


def test_squash_refuses_read_only(tmp_path):
    sess = _make_session(tmp_path, read_only=True)
    with patch.object(api.models, "get_session", lambda sid, metadata_only=False: sess):
        with pytest.raises(session_squash.SquashError):
            session_squash.start_squash_job(SID, confirm_session_id=SID, summary=None)


def test_squash_refuses_empty_session(tmp_path):
    sess = _make_session(tmp_path, messages=[])
    snap = _run_job(sess)
    assert snap["status"] == "error"
    assert "nothing to squash" in snap["error"]


def test_squash_already_squashed_is_idempotent(tmp_path):
    sess = _make_session(tmp_path, messages=[{
        "role": "assistant", "content": "synthèse", "timestamp": 1.0, "_squash_summary": True,
    }])
    snap = _run_job(sess)
    assert snap["status"] == "done"
    assert snap["result"]["already_squashed"] is True


def test_distill_transcript_respects_budget():
    sess = SimpleNamespace(messages=[
        {"role": "user", "content": "demande " * 500, "timestamp": float(i)}
        for i in range(40)
    ])
    distilled = session_squash._distill_transcript(sess, budget=5000)
    assert len(distilled) <= 5000
    assert "demande" in distilled


def test_fallback_summary_has_all_sections():
    sess = SimpleNamespace(
        title="titre test",
        workspace="/tmp/ws",
        created_at=1000.0,
        updated_at=2000.0,
        messages=[
            {"role": "user", "content": "question initiale"},
            {"role": "assistant", "content": "réponse finale"},
        ],
    )
    text = session_squash._fallback_summary(sess, SID, "modèle auxiliaire indisponible")
    assert len(text) >= session_squash.MIN_SUMMARY_CHARS
    for section in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6.", "## 7.", "## 8.", "## 9."):
        assert section in text
    assert SID in text
    assert "modèle auxiliaire indisponible" in text
