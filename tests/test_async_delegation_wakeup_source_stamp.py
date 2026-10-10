"""Wakeup provenance must survive the Agent-echoed current-turn user row.

Root cause for the "[ASYNC DELEGATION BATCH COMPLETE — …] renders as a raw user
bubble" report: in the default ``deferred`` session-save mode there is no eager
checkpoint row, so ``_settle_current_turn_boundary`` takes the
``_mark_active_turn_checkpoint`` branch on the row the Agent returned for the
current turn (api/streaming.py). That helper stamped ``_active_turn_token``
only, never routing through ``stamp_message_source``, so the persisted turn kept
the token but lost ``_source``/``_wakeup_meta`` — and the client gates the
collapsed process-wakeup card on ``_source === 'process_wakeup'``.

The class is "mark an existing row as this turn's user row", not the async
delegation envelope: every non-``webui`` turn source (background-process
wakeups, forks) rides the same branch.
"""

import types

from api import streaming


BATCH_BODY = (
    "[ASYNC DELEGATION BATCH COMPLETE — deleg_7062a9f8]\n"
    "A background fan-out of 1 subagent(s) you dispatched earlier has finished.\n"
    "\n"
    "Role: leaf   Model: ?   Total duration: 398.05s\n"
    "\n"
    "--- ✗ TASK 1/1: Read-only audit  (status=interrupted, api_calls=9, 396.83s) ---\n"
    "Partial output:\n"
    "Operation interrupted: waiting for model response."
)
COMPLETION_BODY = (
    "[IMPORTANT: Background process bg-1 completed (exit_code=0).\n"
    "Command: python worker.py\n"
    "Output:\n"
    "done]"
)
TOKEN = "7740f0b6e9ee4f3ea31dca09e8882d2a:1788025846.8926563"
STARTED_AT = 1788025846.8926563


def _identity(body, source, *, session_id="sess-1", checkpoint=None):
    """The turn identity a resolved Agent boundary produces mid-run."""
    return {
        "session_id": session_id,
        "token": TOKEN,
        "text": body,
        "timestamp": STARTED_AT,
        "source": source,
        "attachments": [],
        "checkpoint": checkpoint,
        "current_turn_user_idx": 2,
        "turn_id": "turn-wakeup",
        "agent_turn_boundary_resolved": True,
        "agent_turn_boundary_source": "result",
    }


def _settle(body, source, *, session_id="sess-1", checkpoint=None):
    """Run one full settle where the Agent echoed the current user row.

    ``api_content``/``id``/``_db_persisted`` mirror the Agent-side row shape the
    reported session persisted for the wakeup turn.
    """
    previous_display = [
        {"role": "user", "content": "earlier question", "timestamp": 1788025651.0, "id": 34},
        {"role": "assistant", "content": "earlier answer", "timestamp": 1788025844.85, "id": 35},
    ]
    previous_context = [dict(row) for row in previous_display]
    result_messages = [
        dict(previous_display[0]),
        dict(previous_display[1]),
        {
            "role": "user",
            "content": body,
            "api_content": "[Workspace::v1: /tmp/ws]\n" + body,
            "timestamp": STARTED_AT,
            "_db_persisted": True,
        },
        {"role": "assistant", "content": "Acknowledged.", "timestamp": STARTED_AT + 5},
    ]
    session = types.SimpleNamespace(
        session_id=session_id,
        messages=list(previous_display),
        context_messages=list(previous_context),
        truncation_watermark=None,
        pending_user_message=body,
        pending_started_at=STARTED_AT,
        pending_user_source=source,
        pending_attachments=[],
    )
    streaming._settle_result_messages(
        session,
        previous_display,
        previous_context,
        result_messages,
        body,
        source,
        _identity(body, source, session_id=session_id, checkpoint=checkpoint),
    )
    return session


def _current_turn_row(session):
    rows = [
        row
        for row in session.messages
        if isinstance(row, dict) and row.get("_active_turn_token") == TOKEN
    ]
    assert len(rows) == 1, session.messages
    return rows[0]


def test_agent_echoed_async_delegation_turn_keeps_process_wakeup_source():
    session = _settle(BATCH_BODY, "process_wakeup")

    row = _current_turn_row(session)
    assert row["role"] == "user"
    assert row["content"] == BATCH_BODY
    # The Agent-side row identity is the one that survived the merge.
    assert row.get("api_content")
    assert row["_source"] == "process_wakeup"


def test_agent_echoed_background_completion_turn_keeps_wakeup_meta():
    """Sibling on the same branch: the structured completion grammar must still
    get its display metadata, not just the ``_source`` stamp."""
    session = _settle(COMPLETION_BODY, "process_wakeup")

    row = _current_turn_row(session)
    assert row["_source"] == "process_wakeup"
    assert row["_wakeup_meta"] == {
        "type": "completion",
        "task_id": "bg-1",
        "command": "python worker.py",
        "exit_code": 0,
    }


def test_agent_echoed_fork_turn_keeps_fork_child_marker():
    """Sibling on the same branch: ``fork`` provenance drives regeneration
    ownership (api/session_ops._selected_regeneration_turn_owned)."""
    session = _settle("forked prompt", "fork", session_id="child-sess")

    row = _current_turn_row(session)
    assert row["_source"] == "fork"
    assert row["_fork_child_turn"] == "child-sess"


def test_agent_echoed_webui_turn_still_omits_the_default_source():
    """``webui`` turns keep the "_source omitted for the default source"
    contract — the stamp helper must not start writing it here."""
    session = _settle("a normal typed prompt", "webui")

    row = _current_turn_row(session)
    assert "_source" not in row
    assert "_wakeup_meta" not in row


def test_eager_checkpoint_turn_is_unaffected():
    """With an eager checkpoint the materialize branch already stamped; the
    marking branch must not regress it."""
    checkpoint = {
        "role": "user",
        "content": BATCH_BODY,
        "timestamp": STARTED_AT,
        "_active_turn_token": TOKEN,
        "_source": "process_wakeup",
    }
    session = _settle(BATCH_BODY, "process_wakeup", checkpoint=checkpoint)

    row = _current_turn_row(session)
    assert row["_source"] == "process_wakeup"
