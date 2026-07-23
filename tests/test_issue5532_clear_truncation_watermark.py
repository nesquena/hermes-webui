"""Regression tests for #5532 — /api/session/clear P0 data loss.

Root cause: the ``/api/session/clear`` handler wiped ``s.messages`` and
``s.tool_calls`` but NEVER set ``truncation_watermark``. The append-only
state.db merge (``merge_session_messages_append_only`` via
``reconciled_state_db_messages_for_session``) treats an unset / None watermark
as "keep everything and just dedup", so the next ``/api/session`` read
resurrected every cleared turn from state.db:

  * history reappeared after clear + refresh, and
  * because ``context_messages`` also survived the clear, a continued turn still
    carried the full pre-clear context into the model.

The sibling ``/api/session/truncate`` handler does NOT have this bug: it calls
``truncate_session_at_keep(session, keep)`` which sets
``truncation_watermark = truncation_boundary = _truncation_watermark_for(kept)``.

Fix: route ``/clear`` through the SAME helper with ``keep=0``. A full clear is a
truncate that keeps zero messages, so the watermark becomes
``_truncation_watermark_for([]) == 0.0`` — the #2914 "truncate-to-empty"
sentinel that blocks ALL state.db replay — and ``context_messages`` is emptied
in lockstep. The merge contract is not forked; ``/clear`` and ``/truncate`` now
set the marker identically.

These tests fail on origin/master (watermark stays None → merge resurrects the
cleared transcript) and pass with the fix.
"""
from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace


def _msg(role: str, content: str, ts: float, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "timestamp": ts}


def _seed_session_dir(monkeypatch, tmp_path):
    """Point the session store at an isolated tmp dir (mirror #2914 harness)."""
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return models


def _call_clear(monkeypatch, session_id):
    """Invoke the real POST /api/session/clear route and return its payload."""
    import api.routes as routes

    body = {"session_id": session_id}
    body_bytes = json.dumps(body).encode()
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    # Eviction runs provider I/O; stub it so the test stays hermetic.
    monkeypatch.setattr("api.config._evict_session_agent", lambda _sid: None)

    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status

    monkeypatch.setattr(routes, "j", fake_j)

    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body_bytes))},
        rfile=BytesIO(body_bytes),
    )
    routes.handle_post(handler, SimpleNamespace(path="/api/session/clear"))
    return captured


def _four_turn_messages():
    return [
        _msg("user", "first", 1.0, "u1"),
        _msg("assistant", "reply first", 2.0, "a1"),
        _msg("user", "second", 3.0, "u2"),
        _msg("assistant", "reply second", 4.0, "a2"),
    ]


def test_clear_endpoint_sets_truncation_watermark(monkeypatch, tmp_path):
    """POST /api/session/clear must set truncation_watermark (0.0 sentinel).

    This is the fail-without-fix assertion: on origin/master the watermark
    stays None because the handler never set it.
    """
    _seed_session_dir(monkeypatch, tmp_path)
    from api.models import Session

    session = Session(
        session_id="issue5532clear",
        messages=_four_turn_messages(),
        context_messages=_four_turn_messages(),
    )
    session.save()

    captured = _call_clear(monkeypatch, "issue5532clear")
    assert captured["payload"].get("ok") is True

    loaded = Session.load("issue5532clear")
    assert loaded is not None
    # Full clear = truncate-to-empty: watermark is the #2914 sentinel 0.0
    # (== _truncation_watermark_for([]) == the last KEPT message timestamp for
    # keep=0). On master this is None and the assertion fails.
    assert loaded.truncation_watermark == 0.0
    assert loaded.truncation_boundary == 0.0
    assert loaded.messages == []


def test_clear_empties_context_messages(monkeypatch, tmp_path):
    """Second-half fix (#5532): context_messages must also be cleared so a
    continued turn does not carry pre-clear context into the model. On master
    the /clear handler left context_messages untouched."""
    _seed_session_dir(monkeypatch, tmp_path)
    from api.models import Session

    session = Session(
        session_id="issue5532ctx",
        messages=_four_turn_messages(),
        context_messages=_four_turn_messages(),
    )
    session.save()

    _call_clear(monkeypatch, "issue5532ctx")

    loaded = Session.load("issue5532ctx")
    assert loaded is not None
    assert loaded.context_messages == []


def test_clear_then_read_does_not_resurrect_state_db_messages(monkeypatch, tmp_path):
    """After /clear, a subsequent /api/session read (the append-only state.db
    merge) must return EMPTY — the cleared turns must NOT resurrect from
    state.db.

    On origin/master the watermark is None, so the merge keeps every state.db
    row and the transcript reappears (the P0 data-loss symptom).
    """
    _seed_session_dir(monkeypatch, tmp_path)
    from api.models import Session, reconciled_state_db_messages_for_session

    session = Session(
        session_id="issue5532resurrect",
        messages=_four_turn_messages(),
        context_messages=_four_turn_messages(),
    )
    session.save()

    _call_clear(monkeypatch, "issue5532resurrect")
    cleared = Session.load("issue5532resurrect")
    assert cleared is not None

    # state.db still holds the full pre-clear transcript (append-only backing).
    state_db = [
        _msg("user", "first", 1.0, "state-u1"),
        _msg("assistant", "reply first", 2.0, "state-a1"),
        _msg("user", "second", 3.0, "state-u2"),
        _msg("assistant", "reply second", 4.0, "state-a2"),
    ]
    merged = reconciled_state_db_messages_for_session(
        cleared, state_messages=state_db
    )
    assert merged == [], (
        "cleared session must not resurrect state.db messages; "
        f"got {[m.get('content') for m in merged]}"
    )


def test_clear_matches_truncate_to_empty_marker(monkeypatch, tmp_path):
    """/clear and /truncate(keep=0) must set the SAME watermark/boundary marker
    so the merge contract is not forked. Guards against a future divergence
    where /clear grows its own bespoke watermark logic."""
    _seed_session_dir(monkeypatch, tmp_path)
    from api.models import Session
    from api.session_ops import truncate_session_at_keep

    # Reference: what truncate-to-empty produces on the same transcript.
    ref = Session(
        session_id="issue5532ref",
        messages=_four_turn_messages(),
        context_messages=_four_turn_messages(),
    )
    truncate_session_at_keep(ref, 0)

    # Actual: what /clear produces.
    session = Session(
        session_id="issue5532match",
        messages=_four_turn_messages(),
        context_messages=_four_turn_messages(),
    )
    session.save()
    _call_clear(monkeypatch, "issue5532match")
    loaded = Session.load("issue5532match")

    assert loaded is not None
    assert loaded.truncation_watermark == ref.truncation_watermark == 0.0
    assert loaded.truncation_boundary == ref.truncation_boundary == 0.0
    assert loaded.messages == ref.messages == []
    assert loaded.context_messages == ref.context_messages == []


def test_clear_detaches_compression_snapshot_parent(monkeypatch, tmp_path):
    """Codex gate (#5532): clearing a COMPRESSED-CONTINUATION child must not let
    the pre-clear transcript resurrect via the compression-snapshot parent.

    A compressed continuation keeps its archived transcript in a parent sidecar
    marked pre_compression_snapshot; _webui_sidecar_lineage_messages_for_display()
    stitches that parent back for display, merging the child with
    truncation_watermark=None — so the 0.0 sentinel on the CHILD does NOT stop
    the parent from resurrecting the cleared history on refresh. The fix detaches
    the lineage (parent_session_id + compression anchor fields) on clear.

    Fail-without-fix: on the pre-fix handler the child keeps parent_session_id,
    so the display stitch re-adds the parent's archived messages after clear.
    """
    _seed_session_dir(monkeypatch, tmp_path)
    from api.models import Session
    import api.routes as routes

    parent = Session(
        session_id="issue5532parent",
        messages=_four_turn_messages(),
        pre_compression_snapshot=True,
    )
    parent.save()

    child = Session(
        session_id="issue5532child",
        messages=[
            _msg("user", "post-compression turn", 5.0, "u3"),
            _msg("assistant", "post-compression reply", 6.0, "a3"),
        ],
        context_messages=[],
        parent_session_id="issue5532parent",
    )
    child.save()

    # Sanity: before clear, the display stitch DOES surface the parent snapshot.
    pre = routes._webui_sidecar_lineage_messages_for_display(Session.load("issue5532child"))
    assert any(m.get("content") == "first" for m in pre), (
        "precondition: compressed continuation should stitch the parent snapshot"
    )

    _call_clear(monkeypatch, "issue5532child")

    loaded = Session.load("issue5532child")
    assert loaded is not None
    assert loaded.messages == []
    # Lineage detached so the stitch can't resurrect the parent transcript.
    assert getattr(loaded, "parent_session_id", None) in (None, "")
    assert getattr(loaded, "compression_anchor_visible_idx", None) is None
    assert getattr(loaded, "compression_anchor_message_key", None) is None
    assert getattr(loaded, "compression_anchor_summary", None) is None
    # The load-time display path must return EMPTY — no resurrected parent rows.
    display = routes._webui_sidecar_lineage_messages_for_display(loaded)
    assert display == [], f"cleared compressed continuation must not resurrect parent; got {display}"


def test_clear_preserves_ordinary_fork_parent_link(monkeypatch, tmp_path):
    """#5532 Codex gate: clearing a genuine FORK child must NOT drop its
    parent_session_id — only compression-snapshot parents are detached.

    /api/session/branch stamps session_source="fork" and a parent_session_id so
    the child nests under its parent and shows the "Forked from" indicator
    (sessions.js:5720/5964/7105). The clear fix detaches the lineage ONLY when the
    parent is a pre_compression_snapshot; an ordinary fork parent must survive.

    Fail-without-fix: the unconditional parent detach dropped every parent link,
    silently un-nesting forked sessions.
    """
    _seed_session_dir(monkeypatch, tmp_path)
    from api.models import Session

    parent = Session(
        session_id="issue5532forkparent",
        messages=_four_turn_messages(),
        # NOT a compression snapshot — an ordinary fork source.
    )
    parent.save()

    child = Session(
        session_id="issue5532forkchild",
        messages=[
            _msg("user", "forked turn", 5.0, "u3"),
            _msg("assistant", "forked reply", 6.0, "a3"),
        ],
        context_messages=[],
        parent_session_id="issue5532forkparent",
    )
    child.session_source = "fork"
    child.save()

    _call_clear(monkeypatch, "issue5532forkchild")

    loaded = Session.load("issue5532forkchild")
    assert loaded is not None
    assert loaded.messages == []
    # The fork parent link MUST survive so the child still nests + shows "Forked from".
    assert getattr(loaded, "parent_session_id", None) == "issue5532forkparent", (
        "clearing an ordinary fork child must preserve its parent_session_id"
    )


def test_clear_survives_startup_recovery(monkeypatch, tmp_path):
    """Codex gate #2 (#5532): a cleared session must stay cleared across a WebUI
    restart. s.save() writes a pre-clear .json.bak (messages shrank to []), and
    recover_all_sessions_on_startup restores any session whose .bak has MORE
    messages than the live file — ignoring the live truncation_watermark==0.0.
    Without dropping the stale .bak on clear, startup recovery resurrects the
    cleared transcript (with a None watermark).

    Fail-without-fix: on the pre-fix handler the .bak survives, recovery restores
    it, and the reloaded session has messages again + watermark None.
    """
    models = _seed_session_dir(monkeypatch, tmp_path)
    from api.models import Session
    from api import session_recovery

    session = Session(
        session_id="issue5532recover",
        messages=_four_turn_messages(),
        context_messages=_four_turn_messages(),
    )
    session.save()
    # Second save shrinking to nothing is what /clear does; but drive the REAL
    # route so the .bak-drop path is exercised end to end.
    _call_clear(monkeypatch, "issue5532recover")

    session_dir = models.SESSION_DIR
    live_path = session_dir / "issue5532recover.json"
    # The stale pre-clear backup must be gone (so recovery can't undo the clear).
    assert not live_path.with_suffix(".json.bak").exists(), (
        "clear must drop the pre-clear .json.bak so startup recovery can't resurrect it"
    )

    result = session_recovery.recover_all_sessions_on_startup(session_dir)
    assert result["restored"] == 0, f"startup recovery must not restore a cleared session; got {result}"

    loaded = Session.load("issue5532recover")
    assert loaded is not None
    assert loaded.messages == []
    assert (loaded.context_messages or []) == []
    assert loaded.truncation_watermark == 0.0
