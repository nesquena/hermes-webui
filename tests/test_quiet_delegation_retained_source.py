"""A retained Agent result row must carry the turn's _source stamp (#quiet-delegation).

The Gateway persists the wakeup prompt as its own durable user row (with
``_row_id``/``api_content``/``_db_persisted`` provenance) before the WebUI
settles the turn. In deferred session-save mode the active-turn identity
carries no checkpoint dict, so ``_settle_current_turn_boundary``'s
checkpoint-index branch retained the Agent row with only the token stamped —
the delegation_wakeup row then rendered as a visible user turn instead of
being hidden by the ``_source`` filter.
"""

from api import streaming


def _identity(token="stream-one:1", idx=0):
    return {
        "session_id": "quiet-retained",
        "token": token,
        "text": "ASYNC DELEGATION BATCH COMPLETE\nchild findings",
        "timestamp": 1.0,
        "source": "delegation_wakeup",
        "attachments": [],
        "checkpoint": None,  # deferred session-save mode: no checkpoint dict
        "current_turn_user_idx": idx,
        "agent_turn_boundary_resolved": True,
        "turn_id": "turn-1",
    }


def _agent_user_row():
    return {
        "role": "user",
        "content": "ASYNC DELEGATION BATCH COMPLETE\nchild findings",
        "api_content": "[Workspace::v1: /x]\nASYNC DELEGATION BATCH COMPLETE\nchild findings",
        "timestamp": 1.0,
        "_row_id": 203527,
        "_db_persisted": True,
    }


def test_retained_agent_row_keeps_delegation_source_without_checkpoint():
    identity = _identity()
    settled = streaming._settle_current_turn_boundary(
        [], [_agent_user_row()], identity, identity["text"], "delegation_wakeup",
    )
    user_row = settled[0]
    assert user_row["_source"] == "delegation_wakeup"
    assert user_row["_active_turn_token"] == identity["token"]
    # Durable Agent provenance must survive the stamp.
    assert user_row["_row_id"] == 203527
    assert user_row["_db_persisted"] is True


def test_retained_agent_row_merge_hides_wakeup_turn():
    identity = _identity()
    agent_rows = [
        _agent_user_row(),
        {"role": "assistant", "content": "Handled.", "timestamp": 2.0,
         "_row_id": 203528, "_db_persisted": True},
    ]
    merged = streaming._merge_display_messages_after_agent_result(
        [], [], agent_rows, identity["text"], source="delegation_wakeup",
        verification_nudge_provenance={"active_turn_identity": identity},
    )
    user_rows = [m for m in merged if m.get("role") == "user"]
    assert len(user_rows) == 1
    assert user_rows[0]["_source"] == "delegation_wakeup"


def test_webui_source_stays_omitted_on_retained_row():
    identity = _identity()
    identity["source"] = "webui"
    settled = streaming._settle_current_turn_boundary(
        [], [_agent_user_row()], identity, identity["text"], "webui",
    )
    # Default-source contract: _source is omitted, not stamped 'webui'.
    assert "_source" not in settled[0]


def test_checkpoint_dict_path_still_stamps_source():
    identity = _identity()
    identity["checkpoint"] = {"role": "user", "content": identity["text"], "timestamp": 0.5}
    settled = streaming._settle_current_turn_boundary(
        [], [_agent_user_row()], identity, identity["text"], "delegation_wakeup",
    )
    assert settled[0]["_source"] == "delegation_wakeup"
