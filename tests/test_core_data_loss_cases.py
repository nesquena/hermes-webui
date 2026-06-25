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
    """Sanity check: an advanced watermark with the original cutoff persisted
    as truncation_boundary keeps the prefix + post-edit tail and drops the one
    deleted turn."""
    state = [
        _msg("user", "first msg", 50.0),
        _msg("assistant", "first reply", 51.0),
        _msg("user", "original pre-edit", 100.0),
        _msg("assistant", "original reply", 101.0),
        _msg("user", "edited/new turn", 200.0),
        _msg("assistant", "post-edit reply", 201.0),
    ]

    # Original cutoff kept through the first reply (@51); a new turn was then
    # committed (@200), advancing the watermark. boundary (51) < watermark (200).
    merged = models.merge_session_messages_append_only(
        [], state, truncation_watermark=200.0, truncation_boundary=51.0
    )

    contents = [m["content"] for m in merged]
    expected = [
        "first msg", "first reply", "edited/new turn", "post-edit reply"
    ]
    assert contents == expected, (
        f"Sanity: expected {expected}, got {contents}"
    )


def test_core_a_not_advanced_watermark_equals_boundary_does_not_resurrect():
    """Just-truncated state (boundary == watermark, no new turn committed yet):
    everything above the watermark is the deleted suffix and must NOT be kept.

    Reachable via crash/cold-load metadata-vs-sidecar divergence — the exact
    scenario the empty-sidecar branch serves. Opus gate found that the prior
    code swept the suffix into `at_or_after` here, resurrecting deleted turns
    (the same data-loss class this fix exists to kill)."""
    state = [
        _msg("user", "u1", 50.0),
        _msg("assistant", "a1", 51.0),
        _msg("user", "deleted-u2", 100.0),
        _msg("assistant", "deleted-a2", 101.0),
        _msg("user", "deleted-u3", 150.0),
        _msg("assistant", "deleted-a3", 151.0),
    ]

    merged = models.merge_session_messages_append_only(
        [], state, truncation_watermark=51.0, truncation_boundary=51.0
    )

    contents = [m["content"] for m in merged]
    assert contents == ["u1", "a1"], (
        f"watermark==boundary must keep only ts<=watermark, got {contents}"
    )


def test_core_a_legacy_none_boundary_frozen_watermark_does_not_resurrect():
    """Legacy session (truncation_boundary is None) with a positive watermark:
    in the pre-#4767 model a persisted positive watermark always meant
    'frozen at cutoff' (committing a turn cleared it to None), so the safe
    behavior is the conservative ts<=watermark filter — no resurrection, and
    the legitimately-kept first turn survives."""
    state = [
        _msg("user", "u1", 50.0),
        _msg("assistant", "a1", 51.0),
        _msg("user", "deleted-u2", 100.0),
        _msg("assistant", "deleted-a2", 101.0),
        _msg("user", "deleted-u3", 150.0),
        _msg("assistant", "deleted-a3", 151.0),
    ]

    merged = models.merge_session_messages_append_only(
        [], state, truncation_watermark=51.0  # boundary defaults to None
    )

    contents = [m["content"] for m in merged]
    assert contents == ["u1", "a1"], (
        f"legacy None-boundary frozen watermark must keep only ts<=watermark "
        f"(no resurrection, keep u1), got {contents}"
    )


def test_advanced_sidecar_at_watermark_keeps_state_only_post_edit_reply():
    """Non-empty sidecar whose newest row EQUALS the watermark (the post-edit
    user turn is checkpointed but its assistant reply exists only in state.db)
    must keep the state-only post-edit reply when the session is genuinely
    advanced (truncation_boundary < watermark).

    Opus/Codex gate found that the `sidecar_advanced_past_watermark` guard only
    treated `max_sidecar_timestamp > watermark` as advanced, so a reply at
    ts > watermark was dropped whenever the sidecar tail merely EQUALLED the
    watermark — silently losing the legitimate post-edit assistant reply on
    restore/full reload. The boundary < watermark signal now also marks the
    session advanced."""
    # boundary @51 (original cutoff), watermark advanced to @200 (new user turn),
    # sidecar holds the prefix + the committed edited user @200 (== watermark),
    # state.db additionally holds the post-edit assistant reply @201 (state-only).
    sidecar = [
        _msg("user", "first", 50.0),
        _msg("assistant", "first reply", 51.0),
        _msg("user", "edited prompt", 200.0),
    ]
    state = [
        _msg("user", "first", 50.0),
        _msg("assistant", "first reply", 51.0),
        _msg("user", "edited prompt", 200.0),
        _msg("assistant", "post-edit reply", 201.0),
    ]

    merged = models.merge_session_messages_append_only(
        sidecar, state, truncation_watermark=200.0, truncation_boundary=51.0
    )

    contents = [m["content"] for m in merged]
    assert "post-edit reply" in contents, (
        f"State-only post-edit assistant reply was DROPPED! Contents: {contents}"
    )
    assert contents == ["first", "first reply", "edited prompt", "post-edit reply"], (
        f"Expected full advanced transcript, got {contents}"
    )


def test_advanced_does_not_resurrect_stale_post_watermark_row_before_checkpoint():
    """The boundary<watermark advanced signal must NOT resurrect a deleted
    suffix row with ts > watermark that appears in state.db BEFORE the edited
    checkpoint row.

    Codex final-gate found that making `sidecar_advanced_past_watermark`
    globally true (whenever boundary < watermark) let a stale ts>watermark row
    bypass the skip before state replay reached the sidecar's edited checkpoint,
    resurrecting deleted turns. The bypass is now gated on the checkpoint having
    been consumed (state_replay_idx >= len(sidecar_visible_sequence))."""
    # sidecar ends at the edited user @200 (== watermark); state.db lists a
    # deleted future suffix (@300/@301) BEFORE the appended edited turn.
    sidecar = [
        _msg("user", "first", 50.0),
        _msg("assistant", "first reply", 51.0),
        _msg("user", "edited prompt", 200.0),
    ]
    state = [
        _msg("user", "first", 50.0),
        _msg("assistant", "first reply", 51.0),
        _msg("user", "deleted future prompt", 300.0),
        _msg("assistant", "deleted future answer", 301.0),
        _msg("user", "edited prompt", 200.0),
    ]

    merged = models.merge_session_messages_append_only(
        sidecar, state, truncation_watermark=200.0, truncation_boundary=51.0
    )

    contents = [m["content"] for m in merged]
    assert "deleted future prompt" not in contents, (
        f"Stale post-watermark row resurrected before checkpoint! {contents}"
    )
    assert "deleted future answer" not in contents, (
        f"Stale post-watermark row resurrected before checkpoint! {contents}"
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


# ─── Route-level CORE-A: default /api/session full reload must not resurrect ──


def test_core_a_route_full_session_load_does_not_resurrect_deleted_turns(tmp_path):
    """The default GET /api/session full reload (no msg_limit) must pass the
    persisted truncation_boundary through to the append-only merge.

    Regression guard for the call-site gap: merge_session_messages_append_only
    was fixed to honor truncation_boundary, but the default full-load call site
    (api/routes.py handle_get '/api/session') still passed only
    truncation_watermark, so a normal full reload fell back to the old
    'drop one turn pair' heuristic and resurrected deleted suffix turns when an
    older message was edited leaving >=2 later turns. This drives the real
    endpoint end-to-end so the boundary must reach the merge.
    """
    import json
    from io import BytesIO
    from urllib.parse import urlparse

    import api.routes as routes
    from api.models import Session

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

    class _Handler:
        def __init__(self):
            self.status = None
            self.response_headers = []
            self.wfile = BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, k, v):
            self.response_headers.append((k, v))

        def end_headers(self):
            pass

        def payload(self):
            return json.loads(self.wfile.getvalue().decode() or "{}")

    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    orig_dir, orig_index = models.SESSION_DIR, models.SESSION_INDEX_FILE
    models.SESSION_DIR = sess_dir
    models.SESSION_INDEX_FILE = sess_dir / "_index.json"
    models.SESSIONS.clear()

    saved = {
        "get_state_db_session_messages": getattr(routes, "get_state_db_session_messages", None),
        "_session_visible_to_active_profile": getattr(routes, "_session_visible_to_active_profile", None),
        "_clear_stale_stream_state": getattr(routes, "_clear_stale_stream_state", None),
        "_resolve_effective_session_model_for_display": getattr(routes, "_resolve_effective_session_model_for_display", None),
        "_resolve_effective_session_model_provider_for_display": getattr(routes, "_resolve_effective_session_model_provider_for_display", None),
    }
    try:
        s = Session(
            session_id="corea_route",
            messages=[],
            truncation_watermark=400.0,
            truncation_boundary=101.0,
        )
        s.save()
        routes.get_state_db_session_messages = lambda sid, profile=None: list(state)
        routes._session_visible_to_active_profile = lambda profile, handler: True
        routes._clear_stale_stream_state = lambda s: None
        routes._resolve_effective_session_model_for_display = lambda s: getattr(s, "model", None)
        routes._resolve_effective_session_model_provider_for_display = lambda s: getattr(s, "model_provider", None)

        h = _Handler()
        routes.handle_get(
            h, urlparse("/api/session?session_id=corea_route&messages=1&resolve_model=0")
        )
        contents = [
            m.get("content")
            for m in h.payload().get("session", {}).get("messages", [])
        ]

        assert h.status == 200, f"unexpected status {h.status}"
        assert "deleted question 1" not in contents, (
            f"Deleted turn resurrected via /api/session full reload: {contents}"
        )
        assert "deleted answer 1" not in contents, (
            f"Deleted turn resurrected via /api/session full reload: {contents}"
        )
        assert "edited prompt" in contents and "post-edit reply" in contents, (
            f"Post-edit turn missing from full reload: {contents}"
        )
    finally:
        for name, val in saved.items():
            if val is not None:
                setattr(routes, name, val)
        models.SESSION_DIR, models.SESSION_INDEX_FILE = orig_dir, orig_index
        models.SESSIONS.clear()
