"""Reproduction tests for data-loss cases found in PR review.

Empty-sidecar recovery resurrects deleted pre-edit turns when editing
an older message with ≥2 later turns.

Same-second recovery can silently drop the legitimate post-edit
assistant reply.

Both are regression tests — they should FAIL against the current code
(commit 297c88fed7) and PASS after the fix.
"""
from __future__ import annotations

import api.models as models
import api.webui_session_db as webui_db


def _msg(role: str, content: str, ts: float) -> dict:
    return {"role": role, "content": content, "timestamp": ts}


# ─── Resurrected deleted turns ───────────────────────────────────────────────


def test_core_a_empty_sidecar_resurrects_deleted_turns():
    """Editing an older message with ≥2 later turns resurrects
    the deleted suffix on empty-sidecar recovery."""
    state = [
        _msg("user", "original prompt", 100.0),
        _msg("assistant", "original reply", 101.0),
        _msg("user", "deleted question 1", 200.0),
        _msg("assistant", "deleted answer 1", 201.0),
        _msg("user", "deleted question 2", 300.0),
        _msg("assistant", "deleted answer 2", 302.0),
        _msg("user", "edited prompt", 400.0),
        _msg("assistant", "post-edit reply", 401.0),
    ]

    merged = models.merge_session_messages_append_only(
        [],  # empty sidecar (cold reload)
        state,
        truncation_watermark=400.0,
        truncation_boundary=101.0,
    )

    contents = [m["content"] for m in merged]

    assert "original prompt" in contents
    assert "original reply" in contents
    assert "edited prompt" in contents
    assert "post-edit reply" in contents

    assert "deleted question 1" not in contents, (
        f"Deleted turn @200 resurrected! Contents: {contents}"
    )
    assert "deleted answer 1" not in contents, (
        f"Deleted turn @201 resurrected! Contents: {contents}"
    )
    assert "deleted question 2" not in contents, (
        f"Deleted turn @300 resurrected! Contents: {contents}"
    )
    assert "deleted answer 2" not in contents, (
        f"Deleted turn @302 resurrected! Contents: {contents}"
    )

    expected = [
        "original prompt", "original reply",
        "edited prompt", "post-edit reply",
    ]
    assert contents == expected, (
        f"Expected {expected}, got {contents}"
    )


def test_core_a_single_deleted_turn_still_works():
    """Sanity check: backward-scan handles one deleted turn correctly."""
    state = [
        _msg("user", "first msg", 50.0),
        _msg("assistant", "first reply", 51.0),
        _msg("user", "original pre-edit", 100.0),
        _msg("assistant", "original reply", 101.0),
        _msg("user", "edited/new turn", 200.0),
        _msg("assistant", "post-edit reply", 201.0),
    ]

    merged = models.merge_session_messages_append_only(
        [], state, truncation_watermark=200.0
    )

    contents = [m["content"] for m in merged]
    expected = [
        "first msg", "first reply", "edited/new turn", "post-edit reply"
    ]
    assert contents == expected, (
        f"Sanity: expected {expected}, got {contents}"
    )


# ─── Same-second assistant reply dropped ──────────────────────────────────────


def test_core_b_same_second_assistant_reply_dropped():
    """Same-second equality guard drops legitimate post-edit
    assistant reply when it shares the timestamp with the edited user message."""
    T = 100.0
    sidecar = [_msg("user", "edited prompt", T)]
    state = [
        _msg("user", "edited prompt", T),
        _msg("assistant", "post-edit reply", T),
    ]

    merged = models.merge_session_messages_append_only(
        sidecar, state, truncation_watermark=T
    )

    contents = [m["content"] for m in merged]

    assert "edited prompt" in contents, (
        f"Edited user message missing! Contents: {contents}"
    )
    assert "post-edit reply" in contents, (
        f"Same-second assistant reply was DROPPED! Contents: {contents}"
    )


def test_core_b_same_second_replaced_user_still_filtered():
    """Sanity check: same-second guard still filters the replaced
    pre-edit user message (same timestamp, different content)."""
    T = 100.0
    sidecar = [_msg("user", "edited prompt", T)]
    state = [
        _msg("user", "original pre-edit prompt", T),
        _msg("user", "edited prompt", T),
        _msg("assistant", "post-edit reply", T),
    ]

    merged = models.merge_session_messages_append_only(
        sidecar, state, truncation_watermark=T
    )

    contents = [m["content"] for m in merged]

    assert "original pre-edit prompt" not in contents, (
        f"Sanity: replaced user message leaked! Contents: {contents}"
    )
    assert "edited prompt" in contents
    assert "post-edit reply" in contents


def test_core_b_same_second_empty_sidecar_assistant_reply():
    """Same-second assistant reply in empty-sidecar
    recovery path with truncation_boundary — must survive."""
    T = 100.0
    state = [
        _msg("user", "first msg", 50.0),
        _msg("assistant", "first reply", 51.0),
        _msg("user", "edited prompt", T),
        _msg("assistant", "post-edit reply", T),
    ]

    merged = models.merge_session_messages_append_only(
        [], state, truncation_watermark=T,
        truncation_boundary=51.0,
    )

    contents = [m["content"] for m in merged]

    assert "first msg" in contents
    assert "first reply" in contents
    assert "edited prompt" in contents
    assert "post-edit reply" in contents, (
        f"Same-second assistant reply dropped! Contents: {contents}"
    )


# ─── Persistence: save/load round-trip ────────────────────────────────────────


def test_truncation_boundary_survives_save_load(monkeypatch, tmp_path):
    """truncation_boundary must be persisted to JSON and restored on load."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    sid = "boundary_save_load"
    session = models.Session(
        session_id=sid,
        messages=[_msg("user", "hello", 100.0)],
        truncation_watermark=200.0,
        truncation_boundary=101.0,
    )
    session.save()

    with models.LOCK:
        models.SESSIONS.pop(sid, None)

    loaded = models.Session.load(sid)
    assert loaded.truncation_boundary == 101.0, (
        f"truncation_boundary lost after save/load: got {loaded.truncation_boundary}"
    )
    assert loaded.truncation_watermark == 200.0


def test_truncation_boundary_none_survives_save_load(monkeypatch, tmp_path):
    """When truncation_boundary is None, it must survive save/load as None."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    sid = "boundary_none_save"
    session = models.Session(
        session_id=sid,
        messages=[_msg("user", "hello", 100.0)],
        truncation_watermark=None,
        truncation_boundary=None,
    )
    session.save()

    with models.LOCK:
        models.SESSIONS.pop(sid, None)

    loaded = models.Session.load(sid)
    assert getattr(loaded, "truncation_boundary", None) is None


# ─── reconciled_state_db_messages_for_session passes boundary ─────────────────


def test_reconciled_passes_truncation_boundary(monkeypatch, tmp_path):
    """reconciled_state_db_messages_for_session must pass truncation_boundary
    to merge_session_messages_append_only so empty-sidecar recovery uses it."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    sid = "reconciled_boundary"
    session = models.Session(
        session_id=sid,
        messages=[],  # empty sidecar (crash recovery)
        truncation_watermark=200.0,
        truncation_boundary=51.0,
    )
    session.save()

    state_db = [
        _msg("user", "kept", 50.0),
        _msg("assistant", "kept reply", 51.0),
        _msg("user", "deleted 1", 100.0),
        _msg("assistant", "deleted answer 1", 101.0),
        _msg("user", "deleted 2", 150.0),
        _msg("assistant", "deleted answer 2", 151.0),
        _msg("user", "new turn", 200.0),
        _msg("assistant", "new reply", 201.0),
    ]

    monkeypatch.setattr(
        models,
        "get_state_db_session_messages",
        lambda sid: state_db,
    )

    reconciled = models.reconciled_state_db_messages_for_session(session)
    contents = [m["content"] for m in reconciled]

    assert "deleted 1" not in contents, (
        f"reconciled leaked deleted turn: {contents}"
    )
    assert "deleted 2" not in contents, (
        f"reconciled leaked deleted turn: {contents}"
    )
    assert "kept" in contents
    assert "kept reply" in contents
    assert "new turn" in contents
    assert "new reply" in contents


# ─── webui_session_db _METADATA_FIELDS includes boundary ──────────────────────


def test_webui_session_db_metadata_fields_includes_boundary():
    """webui_session_db._METADATA_FIELDS must include truncation_boundary
    so it's recognized as a metadata field (not leaked into extra)."""
    assert "truncation_boundary" in webui_db._METADATA_FIELDS, (
        "webui_session_db._METADATA_FIELDS missing truncation_boundary"
    )
