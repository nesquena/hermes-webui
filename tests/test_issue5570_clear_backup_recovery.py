from __future__ import annotations

import json

from api.session_recovery import (
    inspect_session_recovery_status,
    recover_all_sessions_on_startup,
    recover_session,
)


def _msg(role: str, content: str, ts: float, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "timestamp": ts}


def _write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def _clear_sentinel(sid: str) -> dict:
    return {
        "session_id": sid,
        "messages": [],
        "context_messages": [],
        "truncation_watermark": 0.0,
        "truncation_boundary": 0.0,
        "active_stream_id": None,
        "pending_user_message": None,
        "pending_attachments": [],
        "pending_started_at": None,
        "pending_user_source": None,
        "clear_generation": f"clear-{sid}",
    }


def _stale_pre_clear_backup(sid: str) -> dict:
    return {
        "session_id": sid,
        "messages": [
            _msg("user", "pre-clear prompt", 1.0, "u1"),
            _msg("assistant", "pre-clear reply", 2.0, "a1"),
        ],
        "context_messages": [
            _msg("user", "pre-clear prompt", 1.0, "cu1"),
            _msg("assistant", "pre-clear reply", 2.0, "ca1"),
        ],
        "truncation_watermark": None,
        "truncation_boundary": None,
    }


def _recoverable_post_clear_backup(sid: str) -> dict:
    backup = _clear_sentinel(sid)
    backup["messages"] = [
        _msg("user", "post-clear prompt", 10.0, "u10"),
        _msg("assistant", "post-clear reply", 11.0, "a11"),
    ]
    backup["context_messages"] = list(backup["messages"])
    return backup


def _normal_loss_fixture(sid: str) -> tuple[dict, dict]:
    live = {
        "session_id": sid,
        "messages": [_msg("user", "live prompt", 1.0, "u1")],
        "context_messages": [_msg("user", "live prompt", 1.0, "cu1")],
    }
    backup = {
        "session_id": sid,
        "messages": [
            _msg("user", "live prompt", 1.0, "u1"),
            _msg("assistant", "live reply", 2.0, "a1"),
            _msg("user", "extra prompt", 3.0, "u2"),
        ],
        "context_messages": [
            _msg("user", "live prompt", 1.0, "cu1"),
            _msg("assistant", "live reply", 2.0, "ca1"),
            _msg("user", "extra prompt", 3.0, "cu2"),
        ],
    }
    return live, backup


def test_stale_pre_clear_backup_inspect_decision(tmp_path):
    sid = "issue5570_clear"
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")

    _write_json(live_path, _clear_sentinel(sid))
    _write_json(bak_path, _stale_pre_clear_backup(sid))

    status = inspect_session_recovery_status(live_path)

    assert status["recommend"] == "no_action"
    assert status["intentional_clear_truncate"] is True


def test_clear_sentinel_backup_is_not_restored(tmp_path):
    sid = "issue5570_clear_prompt_contract"
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")

    _write_json(live_path, _clear_sentinel(sid))
    _write_json(bak_path, _stale_pre_clear_backup(sid))

    status = inspect_session_recovery_status(live_path)
    result = recover_session(live_path)
    restored_live = json.loads(live_path.read_text(encoding="utf-8"))

    assert status["recommend"] == "no_action"
    assert status["intentional_clear_truncate"] is True
    assert result["restored"] is False
    assert restored_live["messages"] == []
    assert restored_live["truncation_watermark"] == 0.0


def test_stale_pre_clear_backup_manual_recovery_leaves_clear_sidecar(tmp_path):
    sid = "issue5570_clear_manual"
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")

    _write_json(live_path, _clear_sentinel(sid))
    _write_json(bak_path, _stale_pre_clear_backup(sid))

    result = recover_session(live_path)

    assert result["restored"] is False
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"] == []
    assert json.loads(live_path.read_text(encoding="utf-8"))["truncation_watermark"] == 0.0


def test_stale_pre_clear_backup_startup_recovery_skips_restore(tmp_path):
    sid = "issue5570_clear_startup"
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")

    _write_json(live_path, _clear_sentinel(sid))
    _write_json(bak_path, _stale_pre_clear_backup(sid))

    result = recover_all_sessions_on_startup(tmp_path)

    assert result["restored"] == 0
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"] == []


def test_same_generation_clear_backup_still_restores(tmp_path):
    sid = "issue5570_clear_marker_pair"
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")

    _write_json(live_path, _clear_sentinel(sid))
    _write_json(bak_path, _recoverable_post_clear_backup(sid))

    status = inspect_session_recovery_status(live_path)
    result = recover_session(live_path)

    assert status["recommend"] == "restore"
    assert result["restored"] is True
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"][0]["content"] == "post-clear prompt"


def test_normal_larger_backup_recovery_still_restores(tmp_path):
    sid = "issue5570_normal_loss"
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")

    live, backup = _normal_loss_fixture(sid)
    _write_json(live_path, live)
    _write_json(bak_path, backup)

    status = inspect_session_recovery_status(live_path)
    result = recover_session(live_path)

    assert status["recommend"] == "restore"
    assert result["restored"] is True
    assert len(json.loads(live_path.read_text(encoding="utf-8"))["messages"]) == 3


def test_empty_non_sentinel_live_session_still_recovers(tmp_path):
    sid = "issue5570_empty_non_sentinel"
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")

    live = {
        "session_id": sid,
        "messages": [],
        "context_messages": [],
        "truncation_watermark": None,
        "truncation_boundary": None,
        "active_stream_id": None,
        "pending_user_message": None,
        "pending_attachments": [],
        "pending_started_at": None,
        "pending_user_source": None,
    }
    backup = {
        "session_id": sid,
        "messages": [
            _msg("user", "replay prompt", 4.0, "u4"),
            _msg("assistant", "replay reply", 5.0, "a5"),
        ],
        "context_messages": [
            _msg("user", "replay prompt", 4.0, "cu4"),
            _msg("assistant", "replay reply", 5.0, "ca5"),
        ],
    }
    _write_json(live_path, live)
    _write_json(bak_path, backup)

    status = inspect_session_recovery_status(live_path)
    result = recover_session(live_path)

    assert status["recommend"] == "restore"
    assert result["restored"] is True
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"][0]["content"] == "replay prompt"


def test_clear_shaped_live_without_clear_generation_still_restores(tmp_path):
    sid = "issue5570_shape_without_marker"
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")
    live = _clear_sentinel(sid)
    live.pop("clear_generation")
    _write_json(live_path, live)
    _write_json(bak_path, _stale_pre_clear_backup(sid))

    status = inspect_session_recovery_status(live_path)
    result = recover_session(live_path)

    assert status["recommend"] == "restore"
    assert result["restored"] is True
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"][0]["content"] == "pre-clear prompt"


def test_empty_live_without_clear_watermark_still_restores(tmp_path):
    sid = "issue5570_empty_without_watermark"
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")

    _write_json(live_path, {
        **_clear_sentinel(sid),
        "truncation_watermark": None,
        "truncation_boundary": None,
    })
    _write_json(bak_path, _stale_pre_clear_backup(sid))

    result = recover_session(live_path)

    assert result["restored"] is True
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"][0]["content"] == "pre-clear prompt"


def test_active_or_pending_clear_like_sidecar_still_restores(tmp_path):
    for field, value in (
        ("active_stream_id", "stream-1"),
        ("pending_user_message", "pending prompt"),
    ):
        sid = f"issue5570_{field}"
        live_path = tmp_path / f"{sid}.json"
        bak_path = live_path.with_suffix(".json.bak")
        live = _clear_sentinel(sid)
        live[field] = value
        _write_json(live_path, live)
        _write_json(bak_path, _stale_pre_clear_backup(sid))

        result = recover_session(live_path)

        assert result["restored"] is True
        restored = json.loads(live_path.read_text(encoding="utf-8"))
        assert restored["messages"][0]["content"] == "pre-clear prompt"


def test_incomplete_clear_like_sidecar_missing_pending_fields_still_restores(tmp_path):
    sid = "issue5570_missing_pending_fields"
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")
    live = _clear_sentinel(sid)
    live.pop("active_stream_id")
    live.pop("pending_user_message")
    _write_json(live_path, live)
    _write_json(bak_path, _stale_pre_clear_backup(sid))

    status = inspect_session_recovery_status(live_path)
    result = recover_session(live_path)

    assert status["recommend"] == "restore"
    assert result["restored"] is True
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"][0]["content"] == "pre-clear prompt"


def test_malformed_live_sidecar_still_recovers_larger_backup(tmp_path):
    sid = "issue5570_malformed_live"
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")
    live_path.write_text("{not json", encoding="utf-8")
    _write_json(bak_path, _stale_pre_clear_backup(sid))

    status = inspect_session_recovery_status(live_path)
    result = recover_session(live_path)

    assert status["recommend"] == "restore"
    assert result["restored"] is True
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"][0]["content"] == "pre-clear prompt"


def test_manual_compression_recovery_behavior_is_preserved(tmp_path):
    sid = "issue5570_compression"
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")
    marker = _msg("user", "[context compaction] summary of earlier turns", 1.0, "marker")

    pre_compress_live = {
        "session_id": sid,
        "messages": [
            _msg("user", "one", 1.0, "u1"),
            _msg("assistant", "two", 2.0, "a2"),
            _msg("user", "three", 3.0, "u3"),
            _msg("assistant", "four", 4.0, "a4"),
        ],
        "context_messages": [
            _msg("user", "one", 1.0, "cu1"),
            _msg("assistant", "two", 2.0, "ca2"),
        ],
        "compression_anchor_summary": "Compressed: 4 -> 2 messages",
        "compression_anchor_message_key": {"role": "assistant", "ts": 2.0, "text": "two", "attachments": 0},
        "compression_anchor_mode": "manual",
        "truncation_watermark": 2.0,
        "truncation_boundary": 2.0,
    }
    pre_compress_backup = dict(pre_compress_live)
    pre_compress_backup["messages"] = pre_compress_live["messages"] + [
        _msg("user", "five", 5.0, "u5"),
        _msg("assistant", "six", 6.0, "a6"),
    ]
    pre_compress_backup["context_messages"] = pre_compress_backup["messages"]
    _write_json(live_path, pre_compress_live)
    _write_json(bak_path, pre_compress_backup)

    pre_status = inspect_session_recovery_status(live_path)
    pre_result = recover_session(live_path)

    assert pre_status["recommend"] == "no_action"
    assert pre_status["intentional_compress_shrink"] is True
    assert pre_result["restored"] is False

    post_backup = dict(pre_compress_live)
    post_backup["messages"] = [
        _msg("user", "one", 1.0, "u1"),
        _msg("assistant", "two", 2.0, "a2"),
        _msg("user", "three", 3.0, "u3"),
        _msg("assistant", "four", 4.0, "a4"),
    ]
    post_backup["context_messages"] = [marker]
    _write_json(live_path, {
        **pre_compress_live,
        "messages": [_msg("user", "one", 1.0, "u1")],
        "context_messages": [_msg("user", "one", 1.0, "cu1")],
    })
    _write_json(bak_path, post_backup)

    post_status = inspect_session_recovery_status(live_path)
    post_result = recover_session(live_path)

    assert post_status["recommend"] == "restore"
    assert post_result["restored"] is True


def test_post_clear_message_with_stale_pre_clear_backup_is_not_restored(tmp_path):
    """#5584 gate (Codex CORE): after a clear, the user sends one post-clear
    message and the stale pre-clear .json.bak still exists (unlink failed). The
    backup has MORE messages than the live sidecar, but the live sidecar carries
    a clear_generation the backup lacks + the clear boundary reset (watermarks
    0.0) — so recovery must NOT resurrect the pre-clear transcript on top of the
    new message."""
    sid = "s-postclear"
    live_path = tmp_path / f"{sid}.json"
    bak_path = tmp_path / f"{sid}.json.bak"
    # Live: cleared sentinel shape + one post-clear message (boundary still 0.0,
    # clear_generation intact — the clear handler's reset).
    live = _clear_sentinel(sid)
    live["messages"] = [_msg("user", "brand new post-clear question", 20.0, "u20")]
    live["context_messages"] = [_msg("user", "brand new post-clear question", 20.0, "cu20")]
    _write_json(live_path, live)
    # Stale pre-clear backup: 2 messages, no clear_generation (predates the clear).
    _write_json(bak_path, _stale_pre_clear_backup(sid))

    status = inspect_session_recovery_status(live_path)
    result = recover_session(live_path)

    assert status["bak_messages"] > status["live_messages"]  # backup is "larger"
    assert status["recommend"] == "no_action"
    assert status.get("intentional_clear_truncate") is True
    assert result["restored"] is False
    # Live sidecar must still be the post-clear content, not the resurrected pre-clear.
    reread = json.loads(live_path.read_text(encoding="utf-8"))
    assert [m["content"] for m in reread["messages"]] == ["brand new post-clear question"]


def test_post_clear_message_after_real_compaction_still_recovers(tmp_path):
    """Guard the guard: if the live sidecar's boundary has moved OFF the clear
    reset (a real compaction happened after the clear), the clear-generation
    supersede check must decline so a genuine larger backup can still restore."""
    sid = "s-postclear-compact"
    live_path = tmp_path / f"{sid}.json"
    bak_path = tmp_path / f"{sid}.json.bak"
    live = _clear_sentinel(sid)
    live["messages"] = [_msg("user", "q", 20.0, "u20")]
    live["context_messages"] = [_msg("user", "q", 20.0, "cu20")]
    # A later compaction moved the boundary off 0.0 — no longer the clear reset.
    live["truncation_watermark"] = 30.0
    live["truncation_boundary"] = 30.0
    _write_json(live_path, live)
    _write_json(bak_path, _stale_pre_clear_backup(sid))

    status = inspect_session_recovery_status(live_path)
    # Boundary moved off the clear reset -> supersede check declines -> normal
    # recovery decides (larger backup, no compress-shrink provenance -> restore).
    assert status["recommend"] == "restore"
