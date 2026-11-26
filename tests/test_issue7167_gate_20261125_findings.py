"""Gate-certification regressions (#7167, 2026-11-25 RED).

Two regressions introduced by the journal-recovery ownership integration:

**Blocker 1 — current user-turn retention.**  With `messages` empty, the repair
syncs the core transcript and then asks whether that core transcript already
holds the pending turn. Text equality plus a matching journal-text substring in
the old assistant tail was accepted as proof, so an older same-text row (no
token, no timestamp) was treated as the current turn: the pending prompt was
never materialized and its attachments/token were backfilled onto the
historical row. The current turn is a distinct turn and must survive; ownership
now requires positive identity (matching token or exact positive timestamp) and
is not inferred from text.

**Blocker 2 — tool-result association.**  With no `tid` and no `args`, the
completion fallback walked the card pool in journal start order (FIFO), while
the legacy producer settles its live cards NEWEST-FIRST: each result then
attached to the other command's card. The fallback now consumes an already-done
card first (round-10 contract), then prefers a freshly materialized card
(round-9 contract), and within one class consumes the newest start (the
canonical live-settlement order).

Both tests pass on frozen master `d4a80d7e7` and fail on the pre-fix
integration (`c6c37d0b9` rebased onto it).
"""

from __future__ import annotations

import json

import pytest

import api.models as models
import api.profiles as profiles
from api.models import Session
from api.run_journal import append_run_event


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME + the WebUI state dir so journal writes sandbox."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(models, "SESSION_DIR", home / "sessions")
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", home / "sessions" / "_index.json")
    return home


def test_gate_20261125_current_user_turn_retained_beside_historical_row(hermes_home):
    sid = "gate_20261125_retention"
    stream_id = "stream-gate-20261125-retention"
    core_path = hermes_home / "sessions" / f"session_{sid}.json"
    core_path.write_text(json.dumps({
        "messages": [
            {"role": "user", "content": "Same prompt"},
            {"role": "assistant", "content": "Done."},
        ]
    }), encoding="utf-8")
    append_run_event(sid, stream_id, "token", {"text": "Done."})

    session = Session(
        session_id=sid,
        title="gate 2026-11-25 retention",
        messages=[],
        pending_user_message="Same prompt",
        pending_started_at=500.5,
        pending_attachments=[{"name": "shot.png", "path": "/tmp/shot.png"}],
        active_stream_id=stream_id,
    )
    models._apply_core_sync_or_error_marker(
        session, core_path, stream_id_for_recheck=stream_id, require_stream_dead=False
    )
    users = [m for m in session.messages if isinstance(m, dict) and m.get("role") == "user"]
    assert len(users) == 2, (
        "the distinct current user turn must be retained beside the historical "
        f"same-text row, got {[(u.get('timestamp'), u.get('content')) for u in users]}"
    )
    historical, current = users
    assert not historical.get("attachments"), (
        "the historical row must not receive the current turn's attachments"
    )
    assert not historical.get("_active_turn_token"), (
        "the historical row must not receive the current turn's token"
    )
    assert current.get("attachments") == [{"name": "shot.png", "path": "/tmp/shot.png"}]
    assert current.get("_active_turn_token") == f"{stream_id}:{500.5:.17g}"

    # Persisted WebUI loader readback: the same boundary must survive the real
    # save path and a cold Session.load(), not just the in-memory repair pass
    # (gate certification 2026-11-25: "including a persisted WebUI loader
    # readback").
    session.save()
    models.SESSIONS.pop(sid, None)
    loaded = Session.load(sid)
    assert loaded is not None
    loaded_users = [
        m for m in loaded.messages if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert len(loaded_users) == 2, (
        "the persisted loader readback must keep the distinct current user turn "
        f"beside the historical row, got "
        f"{[(u.get('timestamp'), u.get('content')) for u in loaded_users]}"
    )
    loaded_historical, loaded_current = loaded_users
    assert not loaded_historical.get("attachments"), (
        "the persisted historical row must not carry the current turn's attachments"
    )
    assert not loaded_historical.get("_active_turn_token"), (
        "the persisted historical row must not carry the current turn's token"
    )
    assert loaded_current.get("attachments") == [
        {"name": "shot.png", "path": "/tmp/shot.png"}
    ]
    assert loaded_current.get("_active_turn_token") == f"{stream_id}:{500.5:.17g}"


def test_gate_20261125_idless_completion_matches_newest_first_settlement(hermes_home):
    sid = "gate_20261125_completion_order"
    stream_id = "stream-gate-20261125-completion"
    append_run_event(sid, stream_id, "tool", {"name": "terminal", "preview": "cmd A"})
    append_run_event(sid, stream_id, "tool", {"name": "terminal", "preview": "cmd B"})
    # The legacy producer settles newest-first: B completes before A.
    append_run_event(sid, stream_id, "tool_complete", {"name": "terminal", "preview": "result B"})
    append_run_event(sid, stream_id, "tool_complete", {"name": "terminal", "preview": "result A"})

    session = Session(
        session_id=sid,
        title="gate 2026-11-25 completion order",
        messages=[
            {"role": "user", "content": "go", "timestamp": 900.5},
            {
                "role": "assistant",
                "content": "",
                "timestamp": 900.6,
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": stream_id,
            },
        ],
    )
    models._append_journaled_partial_output(session, stream_id, dedupe_existing=True)
    cards = [
        (c.get("preview") or c.get("snippet"), c.get("done"))
        for c in session.tool_calls
    ]
    assert len(cards) == 2, cards
    assert cards[0][0] == "result A", (
        f"start A must receive A's result under newest-first settlement, got {cards}"
    )
    assert cards[1][0] == "result B", (
        f"start B must receive B's result under newest-first settlement, got {cards}"
    )
    assert all(done for _preview, done in cards), cards

    # Persisted WebUI loader readback: the association must survive the real
    # save path and a cold Session.load(), including the card anchors that the
    # saved sidecar rebuilds from.
    session.save()
    models.SESSIONS.pop(sid, None)
    loaded = Session.load(sid)
    assert loaded is not None
    loaded_cards = [
        (c.get("preview") or c.get("snippet"), c.get("done"))
        for c in (loaded.tool_calls or [])
    ]
    assert len(loaded_cards) == 2, loaded_cards
    assert loaded_cards[0][0] == "result A", (
        "the persisted loader readback must keep A's result on start A, got "
        f"{loaded_cards}"
    )
    assert loaded_cards[1][0] == "result B", (
        "the persisted loader readback must keep B's result on start B, got "
        f"{loaded_cards}"
    )
    assert all(done for _preview, done in loaded_cards), loaded_cards
