"""Tests for the wakeup ``_source`` backstop and completion-row dedup.

Wakeup deliveries with trusted internal provenance but missing ``_source``
must still be stamped so the never-render UI contract and dedup keep working.
Text shape alone is not authority: a human may paste the same envelope.
"""

from api.models import _normalize_wakeup_rows_for_display
from api.process_event_utils import (
    is_wakeup_user_text,
    stamp_message_source,
    stamp_wakeup_source_if_untagged,
    wakeup_event_key,
)

RAW_COMPLETION = (
    "[IMPORTANT: Background process proc_5b9fcce4cbff completed (exit_code=1).\n"
    "Command: exec yarn build --outDir /tmp/build --emptyOutDir\n"
    "Output:\n"
    "yarn run v1.22.22\n"
    "error Couldn't find a package.json file in \"/a0\"\n"
    "]"
)
PREFIXED_COMPLETION = (
    "[Workspace::v1: /workspace/project/.worktrees/hermes-70ce773a]\n"
    + RAW_COMPLETION
)
WRAPPED_COMPLETION = (
    "[INTERNAL BACKGROUND EVENT — this is internal orchestration input, "
    "not a user message.\nUse it to resume the current task.\n"
    "Technical event follows:\n" + RAW_COMPLETION + "]"
)
WATCH_MATCH = (
    '[IMPORTANT: Background process proc_aaaabbbbcccc matched watch pattern "DONE".\n'
    "Command: make test\n"
    "Matched output:\nDONE\n]"
)
ASYNC_DELEGATION = (
    "[ASYNC DELEGATION BATCH COMPLETE — deleg_c60b0f8e]\n"
    "A background fan-out of 1 subagent(s) has finished.\n\n"
    "--- ✓ TASK 1/1 ---\nTOKEN_OK"
)


def test_is_wakeup_user_text_shapes():
    assert is_wakeup_user_text(RAW_COMPLETION)
    assert is_wakeup_user_text(PREFIXED_COMPLETION)
    assert is_wakeup_user_text(WRAPPED_COMPLETION)
    assert is_wakeup_user_text(WATCH_MATCH)


def test_is_wakeup_user_text_rejects_human_text():
    assert not is_wakeup_user_text("Bonjour, peux-tu vérifier le build ?")
    assert not is_wakeup_user_text("")
    assert not is_wakeup_user_text(None)
    # Mentioning a completion mid-paragraph is not the pinned envelope.
    assert not is_wakeup_user_text("voici le log: " + RAW_COMPLETION)


def test_wakeup_event_key_completion_shapes_agree():
    key = wakeup_event_key(RAW_COMPLETION)
    assert key is not None
    assert key[:3] == ("completion", "proc_5b9fcce4cbff", "1")
    assert wakeup_event_key(PREFIXED_COMPLETION) == key
    assert wakeup_event_key(WRAPPED_COMPLETION) == key


def test_wakeup_event_key_watch_and_human_are_none():
    assert wakeup_event_key(WATCH_MATCH) is None
    assert wakeup_event_key("hello") is None


def test_backstop_stamps_untagged_wakeup_row():
    msg = {"role": "user", "content": RAW_COMPLETION, "_user_originated": False}
    assert stamp_wakeup_source_if_untagged(msg)
    assert msg["_source"] == "process_wakeup"
    assert msg["_wakeup_meta"]["task_id"] == "proc_5b9fcce4cbff"
    assert msg["_wakeup_meta"]["exit_code"] == 1


def test_backstop_leaves_human_and_stamped_rows_untouched():
    human = {"role": "user", "content": "peux-tu relancer le build ?"}
    assert not stamp_wakeup_source_if_untagged(human)
    assert "_source" not in human

    pasted = {"role": "user", "content": RAW_COMPLETION}
    assert not stamp_wakeup_source_if_untagged(pasted)
    assert "_source" not in pasted

    stamped = {"role": "user", "content": RAW_COMPLETION, "_source": "telegram"}
    assert not stamp_wakeup_source_if_untagged(stamped)
    assert stamped["_source"] == "telegram"

    assistant = {"role": "assistant", "content": RAW_COMPLETION}
    assert not stamp_wakeup_source_if_untagged(assistant)


def test_stamp_message_source_falls_back_to_backstop():
    msg = {"role": "user", "content": PREFIXED_COMPLETION, "_user_originated": False}
    stamp_message_source(msg, None)
    assert msg["_source"] == "process_wakeup"

    msg2 = {"role": "user", "content": "hello"}
    stamp_message_source(msg2, "webui")
    assert "_source" not in msg2

    msg3 = {"role": "user", "content": RAW_COMPLETION}
    stamp_message_source(msg3, "webui", active_turn_token="s1:1.0")
    assert "_source" not in msg3
    assert "_display_kind" not in msg3
    assert msg3["_active_turn_token"] == "s1:1.0"


def test_normalize_collapses_eager_and_merged_twins():
    eager = {"role": "user", "content": RAW_COMPLETION, "_active_turn_token": "t:1", "_user_originated": False}
    merged = {"role": "user", "content": PREFIXED_COMPLETION, "_user_originated": False}
    reply = {"role": "assistant", "content": "[[SILENT]]"}
    out = _normalize_wakeup_rows_for_display([eager, merged, reply])
    assert len(out) == 2
    assert out[0]["_source"] == "process_wakeup"
    assert out[0]["_active_turn_token"] == "t:1"  # first occurrence kept
    assert out[1] is reply


def test_normalize_keeps_distinct_processes_and_watch_rows():
    other = RAW_COMPLETION.replace("proc_5b9fcce4cbff", "proc_000011112222")
    rows = [
        {"role": "user", "content": RAW_COMPLETION, "_user_originated": False},
        {"role": "user", "content": other, "_user_originated": False},
        {"role": "user", "content": WATCH_MATCH, "_user_originated": False},
        {"role": "user", "content": WATCH_MATCH, "_user_originated": False},
    ]
    out = _normalize_wakeup_rows_for_display(rows)
    # Two completions (distinct sids) + both watch rows survive.
    assert len(out) == 4
    assert all(m["_source"] == "process_wakeup" for m in out)


def test_normalize_keeps_divergent_payloads_for_same_process_identity():
    partial = RAW_COMPLETION.replace("error Couldn't", "partial output: Couldn't")
    final = RAW_COMPLETION.replace("error Couldn't", "final output: Couldn't")
    out = _normalize_wakeup_rows_for_display([
        {"role": "user", "content": partial, "_user_originated": False},
        {"role": "user", "content": final, "_user_originated": False},
    ])
    assert [row["content"] for row in out] == [partial, final]


def test_normalize_stamps_previously_stamped_rows_into_dedup():
    first = {
        "role": "user",
        "content": RAW_COMPLETION,
        "_source": "process_wakeup",
    }
    second = {"role": "user", "content": PREFIXED_COMPLETION, "_user_originated": False}
    out = _normalize_wakeup_rows_for_display([first, second])
    assert len(out) == 1
    assert out[0] is first


def test_normalize_passes_through_empty_and_non_list():
    assert _normalize_wakeup_rows_for_display([]) == []
    assert _normalize_wakeup_rows_for_display(None) is None


def test_async_delegation_shape_key_and_backstop_metadata():
    assert is_wakeup_user_text(ASYNC_DELEGATION)
    key = wakeup_event_key(ASYNC_DELEGATION)
    assert key is not None
    assert key[:2] == (
        "async_delegation",
        "deleg_c60b0f8e",
    )
    msg = {"role": "user", "content": ASYNC_DELEGATION, "_user_originated": False}
    assert stamp_wakeup_source_if_untagged(msg)
    assert msg["_source"] == "async_delegation"
    assert msg["_display_kind"] == "internal_event"
    assert msg["_user_originated"] is False
    assert msg["_event_id"] == "async_delegation:deleg_c60b0f8e:terminal"
    assert msg["_workflow_id"] == "delegation:deleg_c60b0f8e"


def test_normalize_keeps_untrusted_state_row_separate_from_internal_twin():
    state_row = {
        "role": "user",
        "content": ASYNC_DELEGATION,
        "_db_persisted": True,
        "id": 5,
    }
    sidecar_row = {
        "role": "user",
        "content": ASYNC_DELEGATION,
        "_source": "async_delegation",
        "_event_id": "async_delegation:deleg_c60b0f8e:terminal",
    }
    out = _normalize_wakeup_rows_for_display([state_row, sidecar_row])
    assert len(out) == 2
    assert out[0] is state_row
    assert "_source" not in out[0]
    assert out[1] is sidecar_row
    assert out[1]["_source"] == "async_delegation"
