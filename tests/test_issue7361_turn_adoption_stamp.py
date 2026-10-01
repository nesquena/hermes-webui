"""Turn-adoption provenance stamping in `_settle_current_turn_boundary` (#7361).

The Agent returns the pending user row as part of ``result["messages"]``.
`_settle_current_turn_boundary`` adopts that row (the "checkpoint branch")
and is supposed to carry the turn's provenance onto it exactly like
``_materialize_active_turn_user`` (the "insert branch") does.

The stamp used to sit *inside* `if isinstance(checkpoint, dict):` — and the
eager checkpoint dict only exists when the session pre-persisted the row.
Under the default **deferred** save mode there is no checkpoint row when the
turn settles, so the whole stamp (plus the ``_fork_child_turn`` marker that
derives from the same source) silently fell through for every
``process_wakeup`` turn: the row persisted without ``_source``, and the
frontend — which gates the collapsed wakeup card on that stamp — rendered
the raw multi-kilobyte ``[IMPORTANT: Background process …]`` envelope as a
plain user bubble. Regression window: every transcript saved since the
turn-ownership settlement change in ``exp-v0.52.155``.

These tests execute the real settlement helper (no source-text greps, per
the #7649 lesson) across the exact state shapes the issue measured.
"""
from __future__ import annotations

from api.streaming import _settle_current_turn_boundary


def _wakeup_body():
    return (
        "[IMPORTANT: Background process proc_42 completed (exit_code=0).\n"
        "Command: npm run build\n"
        "Output:\nBuild finished in 12s]"
    )


def _wakeup_identity():
    """Identity for a process_wakeup turn with NO eager checkpoint dict —
    the deferred-save default the issue reproduces under."""
    return {
        "token": "stream-1:1781024055",
        "text": _wakeup_body(),
        "timestamp": 1781024055.0,
        "source": "process_wakeup",
        "current_turn_user_idx": 0,
        "checkpoint": None,
        "turn_id": "turn-1",
        "agent_turn_boundary_resolved": True,
    }


def _fork_identity():
    return {
        "token": "stream-2:1781024055",
        "text": "regenerate please",
        "timestamp": 1781024055.0,
        "source": "fork",
        "session_id": "fork-child-sid",
        "current_turn_user_idx": 0,
        "checkpoint": None,
        "turn_id": "turn-2",
        "agent_turn_boundary_resolved": True,
    }


def _adopted_user_row(identity):
    """The Agent-returned user row the adoption path must stamp."""
    return [{"role": "user", "content": identity["text"],
             "timestamp": identity["timestamp"]}]


def test_wakeup_adoption_stamps_source_without_eager_checkpoint():
    """The issue's exact reproduction: deferred save mode → no checkpoint
    dict → the adopted row must still carry _source=process_wakeup."""
    identity = _wakeup_identity()
    result = _settle_current_turn_boundary(
        [], _adopted_user_row(identity), identity, identity["text"], "webui")

    assert len(result) == 1
    row = result[0]
    assert row["_source"] == "process_wakeup", (
        "adopted wakeup row lost _source — the collapsed wakeup card needs it")
    assert row["_active_turn_token"] == identity["token"]


def test_wakeup_adoption_stamps_display_meta_without_eager_checkpoint():
    """_wakeup_meta rides on the same stamp: the card prefers it over the
    client parser, so losing it degrades even correctly-stamped rows."""
    identity = _wakeup_identity()
    result = _settle_current_turn_boundary(
        [], _adopted_user_row(identity), identity, identity["text"], "webui")

    row = result[0]
    assert row.get("_wakeup_meta"), (
        f"expected display meta on the adopted wakeup row, got {row.get('_wakeup_meta')!r}")


def test_fork_adoption_keeps_fork_child_turn_marker():
    """Sibling effect from the issue's notes: fork turns lost their
    _fork_child_turn marker on the same path — regeneration authorization
    for forked sessions depends on it."""
    identity = _fork_identity()
    result = _settle_current_turn_boundary(
        [], _adopted_user_row(identity), identity, identity["text"], "webui")

    row = result[0]
    assert row["_source"] == "fork"
    assert row.get("_fork_child_turn") == "fork-child-sid", (
        "fork adoption must stamp the child-session marker like the "
        "materialize path does")


def test_webui_adoption_stays_unmarked():
    """The default-source contract: webui turns keep _source omitted."""
    identity = {
        "token": "stream-3:1781024055",
        "text": "hello there",
        "timestamp": 1781024055.0,
        "source": "webui",
        "current_turn_user_idx": 0,
        "checkpoint": None,
        "turn_id": "turn-3",
        "agent_turn_boundary_resolved": True,
    }
    result = _settle_current_turn_boundary(
        [], _adopted_user_row(identity), identity, identity["text"], "webui")

    row = result[0]
    assert "_source" not in row, "webui turns must not gain a _source stamp"
    assert row["_active_turn_token"] == identity["token"]


def test_eager_checkpoint_branch_still_merges_checkpoint_fields():
    """Regression guard for the rest of the checkpoint branch: when the
    eager checkpoint dict DOES exist, its id/timestamp/attachments still
    merge into the adopted row, and the stamp now runs on top."""
    identity = _wakeup_identity()
    identity["checkpoint"] = {
        "role": "user",
        "content": identity["text"],
        "id": "msg-xyz",
        "timestamp": 1781024000.0,
        "attachments": [{"name": "a.png", "path": "/tmp/a.png"}],
    }
    # The adopted row carries no id/attachments of its own (Agent returned a
    # bare user row); the merge fills them from the eager checkpoint. The
    # merge never overwrites a value the row already has.
    agent_row = [{"role": "user", "content": identity["text"],
                  "timestamp": identity["timestamp"]}]
    result = _settle_current_turn_boundary(
        [], agent_row, identity, identity["text"], "webui")

    row = result[0]
    assert row["id"] == "msg-xyz", "checkpoint id merge regressed"
    assert row["timestamp"] == identity["timestamp"], (
        "checkpoint merge must not overwrite an existing row timestamp")
    assert row["attachments"] == [{"name": "a.png", "path": "/tmp/a.png"}]
    assert row["_source"] == "process_wakeup", (
        "stamp must hold on the checkpoint-carrying path too")


def test_source_falls_back_to_call_argument_without_identity_source():
    """The function's `source` parameter is the fallback when the identity
    carries none — the stamp must still resolve it (previously it was
    evaluated inside the checkpoint gate but with the same fallback)."""
    identity = {
        "token": "stream-4:1781024055",
        "text": _wakeup_body(),
        "timestamp": 1781024055.0,
        "current_turn_user_idx": 0,
        "checkpoint": None,
        "turn_id": "turn-4",
        "agent_turn_boundary_resolved": True,
    }
    result = _settle_current_turn_boundary(
        [], _adopted_user_row(identity), identity, identity["text"],
        "process_wakeup")

    row = result[0]
    assert row["_source"] == "process_wakeup"


def test_adoption_preserves_existing_source_on_agent_row():
    """If the Agent-returned row already carries a _source stamp (e.g. from
    an earlier recovery pass), the settlement must not downgrade it to the
    webui default."""
    identity = _wakeup_identity()
    rows = _adopted_user_row(identity)
    rows[0]["_source"] = "process_wakeup"
    result = _settle_current_turn_boundary(
        [], rows, identity, identity["text"], "webui")

    assert result[0]["_source"] == "process_wakeup"
