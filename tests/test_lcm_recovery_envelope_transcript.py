import json

from api.compression_anchor import visible_messages_for_anchor
from api.models import Session
from api.streaming import (
    _compression_anchor_message_key,
    _compression_summary_from_messages,
    _merge_display_messages_after_agent_result,
    _sanitize_messages_for_agent,
    _settle_result_messages,
)


RECENT_ENVELOPE = (
    "[Recent Summary (d3, node 9)]\n"
    "PRIVATE_RECENT_RECOVERY_BODY\n"
    "[Expand for details: retrieve node 9]"
)
OBJECTIVE_ENVELOPE = (
    "[Current user objective preserved from compacted history]\n"
    "PRIVATE_OBJECTIVE_RECOVERY_BODY"
)
COMBINED_ENVELOPE = f"{OBJECTIVE_ENVELOPE}\n\n---\n\n{RECENT_ENVELOPE}"
DEPTH1_ENVELOPE = (
    "[Depth-1 Summary (d1, node 4)]\n"
    "PRIVATE_DEPTH1_RECOVERY_BODY"
)
ARC_ENVELOPE = (
    "[Session Arc Summary (d1, node 39)]\n"
    "PRIVATE_ARC_RECOVERY_BODY"
)


def _session(tmp_path, monkeypatch, messages, context_messages):
    import api.models as models

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    return Session(
        session_id="lcm-envelope",
        workspace=str(tmp_path),
        messages=messages,
        context_messages=context_messages,
    )


def test_lcm_recovery_envelopes_stay_context_only_after_settle_and_save(
    tmp_path, monkeypatch
):
    historical_prompt = {
        "role": "user",
        "content": (
            "[Current user objective preserved from compacted history]\n"
            "Please explain this literal heading from an earlier turn."
        ),
        "id": "historical-headed-prompt",
        "timestamp": 10,
    }
    previous = [
        {"role": "user", "content": "earlier prompt"},
        {"role": "assistant", "content": "earlier answer"},
        historical_prompt,
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] prior compacted history",
        },
    ]
    prompt = "continue with the current task"
    token = "stream-1:12"
    current_user = {
        "role": "user",
        "content": prompt,
        "_active_turn_token": token,
    }
    assistant_answer = (
        "[Recent Summary (d3, node 9)]\n"
        "This is a real assistant answer quoting the recovery heading."
    )
    combined_envelope = {"role": "assistant", "content": COMBINED_ENVELOPE}
    objective_envelope = {
        "role": "user",
        "content": OBJECTIVE_ENVELOPE,
        "_active_turn_token": "unrelated-token",
    }
    depth1_envelope = {"role": "assistant", "content": DEPTH1_ENVELOPE}
    quoted_gap_row = {
        "role": "user",
        "content": (
            "An earlier user quoted this heading:\n"
            "[Depth-1 Summary (d1, node 4)]\n"
            "That text was part of the discussion."
        ),
    }
    result = previous + [
        combined_envelope,
        objective_envelope,
        depth1_envelope,
        quoted_gap_row,
        current_user,
        {"role": "assistant", "content": assistant_answer},
    ]
    session = _session(tmp_path, monkeypatch, list(previous), list(previous))
    identity = {
        "token": token,
        "text": prompt,
        "current_turn_user_idx": len(previous) + 4,
        "turn_id": "turn-1",
        "agent_turn_boundary_resolved": True,
    }

    _settle_result_messages(
        session, list(previous), list(previous), result, prompt, "webui", identity
    )

    display_text = [row.get("content") for row in session.messages]
    context_text = [row.get("content") for row in session.context_messages]
    assert COMBINED_ENVELOPE not in display_text
    assert OBJECTIVE_ENVELOPE not in display_text
    assert DEPTH1_ENVELOPE not in display_text
    assert historical_prompt["content"] in display_text
    assert quoted_gap_row["content"] in display_text
    assert COMBINED_ENVELOPE in context_text
    assert OBJECTIVE_ENVELOPE in context_text
    assert DEPTH1_ENVELOPE in context_text
    assert assistant_answer in display_text
    flagged_context_text = {
        row.get("content")
        for row in session.context_messages
        if row.get("_lcm_recovery_envelope") is True
    }
    assert flagged_context_text == {
        COMBINED_ENVELOPE,
        OBJECTIVE_ENVELOPE,
        DEPTH1_ENVELOPE,
    }
    assert not next(
        row for row in session.context_messages
        if row.get("content") == historical_prompt["content"]
    ).get("_lcm_recovery_envelope")
    assert not next(
        row for row in session.context_messages
        if row.get("content") == quoted_gap_row["content"]
    ).get("_lcm_recovery_envelope")

    # LCM can replace the prior model context, putting its recovery envelope
    # before the current turn. The merge must keep that context private too.
    non_prefix_session = _session(tmp_path, monkeypatch, list(previous), list(previous))
    non_prefix_result = [
        combined_envelope,
        objective_envelope,
        current_user,
        {"role": "assistant", "content": assistant_answer},
    ]
    _settle_result_messages(
        non_prefix_session,
        list(previous),
        list(previous),
        non_prefix_result,
        prompt,
        "webui",
        {**identity, "current_turn_user_idx": 2},
    )
    non_prefix_display = [row.get("content") for row in non_prefix_session.messages]
    non_prefix_context = [row.get("content") for row in non_prefix_session.context_messages]
    assert COMBINED_ENVELOPE not in non_prefix_display
    assert OBJECTIVE_ENVELOPE not in non_prefix_display
    assert historical_prompt["content"] in non_prefix_display
    assert assistant_answer in non_prefix_display
    assert COMBINED_ENVELOPE in non_prefix_context
    assert OBJECTIVE_ENVELOPE in non_prefix_context
    assert {
        row.get("content") for row in non_prefix_session.context_messages
        if row.get("_lcm_recovery_envelope") is True
    } == {COMBINED_ENVELOPE, OBJECTIVE_ENVELOPE}
    non_prefix_previous_display = list(non_prefix_session.messages)
    non_prefix_previous_context = list(non_prefix_session.context_messages)
    _settle_result_messages(
        non_prefix_session,
        non_prefix_previous_display,
        non_prefix_previous_context,
        _sanitize_messages_for_agent(non_prefix_previous_context) + [
            {"role": "user", "content": "next turn", "_active_turn_token": "non-prefix-next"},
            {"role": "assistant", "content": "Next answer."},
        ],
        "next turn",
        "webui",
        {
            "token": "non-prefix-next",
            "text": "next turn",
            "current_turn_user_idx": len(non_prefix_previous_context),
            "agent_turn_boundary_resolved": True,
        },
    )
    assert COMBINED_ENVELOPE not in [row.get("content") for row in non_prefix_session.messages]
    assert OBJECTIVE_ENVELOPE not in [row.get("content") for row in non_prefix_session.messages]
    assert "Next answer." in [row.get("content") for row in non_prefix_session.messages]

    # Sync chat writes context before calling the shared merge (rather than
    # _settle_result_messages). Its shared rows need the same private marker.
    sync_previous = [{"role": "user", "content": "before"},
                     {"role": "assistant", "content": "before answer"}]
    sync_result = sync_previous + [
        {"role": "assistant", "content": ARC_ENVELOPE},
        {"role": "assistant", "content": DEPTH1_ENVELOPE},
        {"role": "user", "content": "sync turn", "_active_turn_token": "sync-1"},
        {"role": "assistant", "content": "sync answer"},
    ]
    sync_context = list(sync_result)
    sync_display = _merge_display_messages_after_agent_result(
        sync_previous, sync_previous, list(sync_result), "sync turn",
        verification_nudge_provenance={"active_turn_identity": {
            "token": "sync-1", "text": "sync turn", "current_turn_user_idx": 4,
            "agent_turn_boundary_resolved": True,
        }},
    )
    assert ARC_ENVELOPE not in [row.get("content") for row in sync_display]
    assert DEPTH1_ENVELOPE not in [row.get("content") for row in sync_display]
    assert sync_context[2].get("_lcm_recovery_envelope") is True
    assert sync_context[3].get("_lcm_recovery_envelope") is True
    assert _compression_summary_from_messages(sync_context) is None
    sync_session = _session(tmp_path, monkeypatch, sync_display, sync_context)
    sync_session.compression_anchor_summary = _compression_summary_from_messages(sync_context)
    assert "PRIVATE_ARC_RECOVERY_BODY" not in json.dumps({
        "compression_anchor_summary": sync_session.compact()["compression_anchor_summary"]
    })
    sync_next = _merge_display_messages_after_agent_result(
        sync_display, sync_context,
        _sanitize_messages_for_agent(sync_context) + [
            {"role": "user", "content": "sync next", "_active_turn_token": "sync-2"},
            {"role": "assistant", "content": "sync next answer"},
        ], "sync next",
        verification_nudge_provenance={"active_turn_identity": {
            "token": "sync-2", "text": "sync next",
            "current_turn_user_idx": len(sync_context),
            "agent_turn_boundary_resolved": True,
        }},
    )
    assert ARC_ENVELOPE not in [row.get("content") for row in sync_next]
    assert DEPTH1_ENVELOPE not in [row.get("content") for row in sync_next]
    assert "sync next answer" in [row.get("content") for row in sync_next]

    # The private marker must survive disk reload, while the Agent projection
    # strips it and retains the recovery content as model context.
    session.save(touch_updated_at=False)
    session = Session.load(session.session_id)
    assert session is not None
    assert {
        row.get("content")
        for row in session.context_messages
        if row.get("_lcm_recovery_envelope") is True
    } == flagged_context_text
    provider_context = _sanitize_messages_for_agent(session.context_messages)
    assert all("_lcm_recovery_envelope" not in row for row in provider_context)
    assert {row.get("content") for row in provider_context} >= flagged_context_text

    # On the next turn, previous_context contains context-only LCM envelopes.
    # Backfill must not reinsert them into the visible transcript, and settlement
    # must carry their provenance forward even though the Agent projection drops it.
    second_previous_display = list(session.messages)
    second_previous_context = list(session.context_messages)
    second_prompt = "continue after the recovered context"
    second_token = "stream-1:13"
    second_result = _sanitize_messages_for_agent(second_previous_context) + [
        {
            "role": "user",
            "content": second_prompt,
            "_active_turn_token": second_token,
        },
        {"role": "assistant", "content": "Second turn answer."},
    ]
    _settle_result_messages(
        session,
        second_previous_display,
        second_previous_context,
        second_result,
        second_prompt,
        "webui",
        {
            "token": second_token,
            "text": second_prompt,
            "current_turn_user_idx": len(second_previous_context),
            "turn_id": "turn-3",
            "agent_turn_boundary_resolved": True,
        },
    )
    display_text = [row.get("content") for row in session.messages]
    assert COMBINED_ENVELOPE not in display_text
    assert OBJECTIVE_ENVELOPE not in display_text
    assert DEPTH1_ENVELOPE not in display_text
    assert quoted_gap_row["content"] in display_text
    assert "Second turn answer." in display_text
    assert {
        row.get("content")
        for row in session.context_messages
        if row.get("_lcm_recovery_envelope") is True
    } == flagged_context_text

    visible = visible_messages_for_anchor(session.messages, auto_compression=True)
    for envelope in (COMBINED_ENVELOPE, OBJECTIVE_ENVELOPE, DEPTH1_ENVELOPE):
        assert envelope not in [row.get("content") for row in visible]
    anchor_idx = next(
        idx for idx, row in enumerate(visible)
        if row.get("content") == historical_prompt["content"]
    )
    session.compression_anchor_visible_idx = anchor_idx
    anchor_key = _compression_anchor_message_key(visible[anchor_idx])
    anchor_summary = (
        _compression_summary_from_messages(session.messages)
        or _compression_summary_from_messages(session.context_messages)
    )
    session.compression_anchor_message_key = anchor_key
    session.compression_anchor_summary = anchor_summary

    session.save(touch_updated_at=False)
    session = Session.load(session.session_id)
    persisted = json.loads(session.path.read_text())
    persisted_display = [row.get("content") for row in persisted["messages"]]
    persisted_context = persisted["context_messages"]
    assert COMBINED_ENVELOPE not in persisted_display
    assert OBJECTIVE_ENVELOPE not in persisted_display
    assert DEPTH1_ENVELOPE not in persisted_display
    assert COMBINED_ENVELOPE in [row.get("content") for row in persisted_context]
    assert OBJECTIVE_ENVELOPE in [row.get("content") for row in persisted_context]
    assert DEPTH1_ENVELOPE in [row.get("content") for row in persisted_context]
    assert assistant_answer in persisted_display
    assert "Second turn answer." in persisted_display
    assert {
        row.get("content")
        for row in persisted_context
        if row.get("_lcm_recovery_envelope") is True
    } == flagged_context_text

    public = session.compact()
    assert public["compression_anchor_visible_idx"] == anchor_idx
    assert public["compression_anchor_message_key"] == anchor_key
    assert public["compression_anchor_message_key"]["text"] == (
        _compression_anchor_message_key(historical_prompt)["text"]
    )
    assert public["compression_anchor_summary"] == anchor_summary
    metadata_json = json.dumps({
        key: public[key] for key in (
            "compression_anchor_visible_idx",
            "compression_anchor_message_key",
            "compression_anchor_summary",
        )
    })
    for private_body in (
        "PRIVATE_RECENT_RECOVERY_BODY",
        "PRIVATE_OBJECTIVE_RECOVERY_BODY",
        "PRIVATE_DEPTH1_RECOVERY_BODY",
    ):
        assert private_body not in metadata_json
    assert "_lcm_recovery_envelope" not in metadata_json
    assert {
        row.get("content")
        for row in _sanitize_messages_for_agent(session.context_messages)
    } >= flagged_context_text
    assert all(
        "_lcm_recovery_envelope" not in row
        for row in _sanitize_messages_for_agent(session.context_messages)
    )


def test_owned_user_prompt_starting_with_lcm_heading_survives(tmp_path, monkeypatch):
    previous = [
        {"role": "user", "content": "earlier prompt"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    prompt = (
        "[Current user objective preserved from compacted history]\n"
        "Please explain this heading as literal user text."
    )
    token = "stream-2:13"
    current_user = {
        "role": "user",
        "content": prompt,
        "_active_turn_token": token,
    }
    result = previous + [
        {"role": "user", "content": OBJECTIVE_ENVELOPE},
        current_user,
        {"role": "assistant", "content": "I will treat that as your prompt."},
    ]
    session = _session(tmp_path, monkeypatch, list(previous), list(previous))
    identity = {
        "token": token,
        "text": prompt,
        "current_turn_user_idx": len(previous) + 1,
        "turn_id": "turn-2",
        "agent_turn_boundary_resolved": True,
    }

    _settle_result_messages(
        session, list(previous), list(previous), result, prompt, "webui", identity
    )

    assert prompt in [row.get("content") for row in session.messages]
    assert OBJECTIVE_ENVELOPE not in [row.get("content") for row in session.messages]
