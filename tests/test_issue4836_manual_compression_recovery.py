"""Regression tests for #4836 — manual /compress undone by reconciliation/recovery."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import time
import types

import api.models as models
from api.models import Session, reconciled_state_db_messages_for_session
from api.routes import _handle_session_compress
from api.session_recovery import inspect_session_recovery_status, recover_all_sessions_on_startup


class _FakeHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class _FakeCompressor:
    def compress(self, messages, current_tokens=None, focus_topic=None):
        if len(messages) >= 2:
            return [messages[0], messages[-1]]
        return list(messages)


class _FakeAgent:
    last_instance = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.context_compressor = _FakeCompressor()
        _FakeAgent.last_instance = self


def _install_fake_compression_runtime(monkeypatch, agent_cls):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    import api.config as _cfg

    fake_runtime_provider = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_provider.resolve_runtime_provider = lambda requested=None: {
        "api_key": "fake-key",
        "provider": requested or "openai",
        "base_url": "https://api.openai.com/v1",
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__path__ = []
    fake_hermes_cli.runtime_provider = fake_runtime_provider
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_provider)
    import hermes_cli.runtime_provider as _rtp

    monkeypatch.setattr(
        _cfg,
        "resolve_model_provider",
        lambda model: ("openai/gpt-5.4-mini", "openai", "https://api.openai.com/v1"),
    )
    monkeypatch.setattr(
        _cfg,
        "_get_session_agent_lock",
        lambda sid: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        _rtp,
        "resolve_runtime_provider",
        lambda requested=None: {
            "api_key": "fake-key",
            "provider": requested or "openai",
            "base_url": "https://api.openai.com/v1",
        },
    )


def _msg(role, content, ts):
    return {"role": role, "content": content, "timestamp": ts, "_ts": ts}


def test_manual_compress_persists_truncation_boundary(monkeypatch, cleanup_test_sessions, tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    original_messages = [
        _msg("user", "one", 1.0),
        _msg("assistant", "two", 2.0),
        _msg("user", "three", 3.0),
        _msg("assistant", "four", 4.0),
    ]
    sid = f"issue4836_{time.time_ns()}"
    cleanup_test_sessions.append(sid)
    session = Session(
        session_id=sid,
        title="Untitled",
        workspace=str(tmp_path),
        model="openai/gpt-5.4-mini",
        messages=original_messages,
    )
    session.save(touch_updated_at=False)

    _install_fake_compression_runtime(monkeypatch, _FakeAgent)
    handler = _FakeHandler()
    _handle_session_compress(handler, {"session_id": sid})

    assert handler.status == 200
    loaded = Session.load(sid)
    assert loaded.compression_anchor_mode == "manual"
    assert loaded.truncation_watermark is not None
    assert loaded.truncation_boundary == loaded.truncation_watermark
    assert loaded.last_prompt_tokens is not None
    assert len(loaded.context_messages) == 2
    assert loaded.messages == original_messages


def test_manual_compress_blocks_state_db_replay(monkeypatch, cleanup_test_sessions, tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    sidecar = [
        _msg("user", "one", 1.0),
        _msg("assistant", "two", 2.0),
        _msg("user", "three", 3.0),
        _msg("assistant", "four", 4.0),
    ]
    state_db = [
        _msg("user", "one", 1.0),
        _msg("assistant", "two", 2.0),
        _msg("user", "three", 3.0),
        _msg("assistant", "four", 4.0),
        _msg("assistant", "", 4.1),  # reasoning-only row that used to replay
    ]
    session = Session(
        session_id=f"issue4836_reconcile_{time.time_ns()}",
        title="Untitled",
        workspace=str(tmp_path),
        model="openai/gpt-5.4-mini",
        messages=sidecar,
        context_messages=sidecar,
    )
    session.save(touch_updated_at=False)
    cleanup_test_sessions.append(session.session_id)

    _install_fake_compression_runtime(monkeypatch, _FakeAgent)
    handler = _FakeHandler()
    _handle_session_compress(handler, {"session_id": session.session_id})
    assert handler.status == 200

    loaded = Session.load(session.session_id)
    merged = reconciled_state_db_messages_for_session(
        loaded,
        prefer_context=True,
        state_messages=state_db,
    )
    assert len(merged) == len(loaded.context_messages)


def test_startup_recovery_skips_intentional_manual_compress(monkeypatch, tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    sid = "issue4836_recovery"
    live_path = session_dir / f"{sid}.json"
    bak_path = session_dir / f"{sid}.json.bak"

    live = {
        "session_id": sid,
        "title": "Untitled",
        "workspace": str(tmp_path),
        "model": "openai/gpt-5.4-mini",
        "messages": [
            _msg("user", "one", 1.0),
            _msg("assistant", "two", 2.0),
        ],
        "context_messages": [
            _msg("user", "one", 1.0),
        ],
        "compression_anchor_summary": "Compressed: 2 -> 1 messages",
        "compression_anchor_message_key": {"role": "assistant", "ts": 2.0, "text": "two", "attachments": 0},
        "compression_anchor_mode": "manual",
        "truncation_watermark": 1.0,
        "truncation_boundary": 1.0,
        "message_count": 2,
    }
    bak = dict(live)
    bak["messages"] = live["messages"] + [
        _msg("user", "three", 3.0),
        _msg("assistant", "four", 4.0),
    ] * 150
    # The .bak is the PRE-compression backup: its model-facing context_messages
    # is still the large UNCOMPRESSED context (the shrink hasn't been applied to
    # it). Restoring it would undo the intentional shrink → no_action.
    bak["context_messages"] = bak["messages"]
    live_path.write_text(json.dumps(live), encoding="utf-8")
    bak_path.write_text(json.dumps(bak), encoding="utf-8")

    status = inspect_session_recovery_status(live_path)
    assert status["recommend"] == "no_action"
    assert status.get("intentional_compress_shrink") is True

    result = recover_all_sessions_on_startup(session_dir)
    assert result["restored"] == 0
    restored = json.loads(live_path.read_text(encoding="utf-8"))
    assert len(restored["messages"]) == 2


def test_startup_recovery_still_fires_for_post_compress_real_loss(monkeypatch, tmp_path):
    """A manually-compressed session that LATER loses its messages array must
    still be recovered from a backup written AFTER the compression (#4836
    must not permanently disable #1558 crash-recovery for compressed sessions).
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    sid = "issue4836_post_compress_loss"
    live_path = session_dir / f"{sid}.json"
    bak_path = session_dir / f"{sid}.json.bak"

    # Live sidecar: manually compressed AND then truncated by a genuine loss
    # (messages array shrank to 1 entry).
    live = {
        "session_id": sid,
        "title": "Untitled",
        "workspace": str(tmp_path),
        "model": "openai/gpt-5.4-mini",
        "messages": [_msg("user", "one", 1.0)],
        "context_messages": [_msg("user", "one", 1.0)],
        "compression_anchor_summary": "Compressed earlier",
        "compression_anchor_message_key": {"role": "assistant", "ts": 2.0, "text": "two", "attachments": 0},
        "compression_anchor_mode": "manual",
        "truncation_watermark": 1.0,
        "truncation_boundary": 1.0,
        "message_count": 1,
    }
    # Backup: a healthy post-compression snapshot (more messages, written AFTER
    # the compress). Its context_messages is the SAME already-compressed context
    # as live (the compression persists across saves), so it post-dates the
    # compression and must be restored when the live file later loses data.
    bak = dict(live)
    bak["messages"] = [
        _msg("user", "one", 1.0),
        _msg("assistant", "two", 2.0),
        _msg("user", "three", 3.0),
        _msg("assistant", "four", 4.0),
    ]
    bak["context_messages"] = [_msg("user", "one", 1.0)]  # same compressed context as live
    live_path.write_text(json.dumps(live), encoding="utf-8")
    bak_path.write_text(json.dumps(bak), encoding="utf-8")

    status = inspect_session_recovery_status(live_path)
    assert status["recommend"] == "restore", (
        "post-compression real loss must still be recoverable even though the "
        "session was manually compressed"
    )

    result = recover_all_sessions_on_startup(session_dir)
    assert result["restored"] == 1
    restored = json.loads(live_path.read_text(encoding="utf-8"))
    assert len(restored["messages"]) == 4
    assert len(restored["context_messages"]) == 1


def test_startup_recovery_fires_when_loss_shrinks_both_messages_and_context(monkeypatch, tmp_path):
    """A post-compression loss that clobbers BOTH messages AND context_messages
    below the healthy backup's context must STILL be recovered.

    Opus gate edge: a pure length heuristic (bak_ctx_len > live_ctx_len) would
    misread the healthy backup's larger context as 'pre-compression' and wrongly
    suppress recovery — permanent silent data loss. The compaction-marker
    discriminator fixes this: the healthy post-compression backup carries the
    `[context compaction…]` marker, so it is recognized as post-compression and
    recovered regardless of relative context lengths.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    sid = "issue4836_both_shrunk"
    live_path = session_dir / f"{sid}.json"
    bak_path = session_dir / f"{sid}.json.bak"

    marker = {"role": "user", "content": "[context compaction] summary of earlier turns", "timestamp": 1.0}
    # Live: a genuine loss clobbered messages (130->2) AND context (50->1 marker only).
    live = {
        "session_id": sid,
        "title": "Untitled",
        "workspace": str(tmp_path),
        "model": "openai/gpt-5.4-mini",
        "messages": [_msg("user", "one", 1.0), _msg("assistant", "two", 2.0)],
        "context_messages": [marker],
        "compression_anchor_summary": "Compressed earlier",
        "compression_anchor_message_key": {"role": "assistant", "ts": 2.0, "text": "two", "attachments": 0},
        "compression_anchor_mode": "manual",
        "truncation_watermark": 1.0,
        "truncation_boundary": 1.0,
        "message_count": 2,
    }
    # Healthy post-compression backup: MORE messages AND a LARGER compressed
    # context (still carries the compaction marker) — must be recovered.
    bak = dict(live)
    bak["messages"] = [_msg("user", f"m{i}", float(i)) for i in range(130)]
    bak["context_messages"] = [marker] + [_msg("assistant", f"c{i}", float(i)) for i in range(49)]
    live_path.write_text(json.dumps(live), encoding="utf-8")
    bak_path.write_text(json.dumps(bak), encoding="utf-8")

    status = inspect_session_recovery_status(live_path)
    assert status["recommend"] == "restore", (
        "a loss shrinking both messages and context must still recover from a "
        "marked (post-compression) backup — the marker, not length, decides"
    )

    result = recover_all_sessions_on_startup(session_dir)
    assert result["restored"] == 1
    restored = json.loads(live_path.read_text(encoding="utf-8"))
    assert len(restored["messages"]) == 130
