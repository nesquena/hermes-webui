"""Gate-remediation regression tests for nesquena/hermes-webui#7032.

The 2026-08-16 gate certification reproduced one root cause with two
manifestations on head 661a8010: the display projection became
payload-strict while the model-context replay dedup path kept comparing
rows whose persisted copies carry the request-local ``_active_turn_token``
(stripped from the history the Agent replays back by
``_sanitize_messages_for_agent``).  A normal repeated-prompt turn then
either

(a) duplicates the historical user row in model context, or
(b) drops the current exchange from model context (display/context
    divergence).

Both scenarios below run the real streaming caller sequence
(``_settle_result_messages``) against a turn-1 persisted state whose user
row carries ``_active_turn_token`` exactly as production persists it.
"""

import copy
from types import SimpleNamespace

PROMPT = "run the report again"


def _persisted_turn_one(*, with_api_content):
    """Turn-1 state as the streaming writeback persists it."""
    user = {
        "role": "user",
        "content": PROMPT,
        "id": 1,
        "timestamp": 100.0,
        "_active_turn_token": "direct-stream:100",
    }
    if with_api_content:
        user["api_content"] = "[Workspace] " + PROMPT
    assistant = {
        "role": "assistant",
        "content": "the report says A",
        "id": 2,
        "timestamp": 101.0,
    }
    return [user, assistant]


def _settle_repeated_prompt_turn(previous_context, result_messages, *, authoritative):
    from api.streaming import _settle_result_messages

    previous_display = copy.deepcopy(previous_context)
    session = SimpleNamespace(
        session_id="gate-7032-regression",
        messages=copy.deepcopy(previous_display),
        context_messages=copy.deepcopy(previous_context),
        truncation_watermark=None,
    )
    identity = {
        "token": "direct-stream:200",
        "text": PROMPT,
        "timestamp": 200.0,
        "source": "webui",
        "attachments": [],
        "current_turn_user_idx": len(previous_context) if authoritative else None,
        "turn_id": "turn:2" if authoritative else "",
    }
    _settle_result_messages(
        session,
        copy.deepcopy(previous_display),
        copy.deepcopy(previous_context),
        result_messages,
        PROMPT,
        "webui",
        identity,
    )
    return session


def _user_rows(messages):
    return [
        message
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ]


def _assistant_answers(messages):
    return [
        message.get("content")
        for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]


def test_replayed_history_with_persisted_turn_token_is_not_duplicated():
    """(a) The historical user row must not be duplicated in model context.

    The Agent replays the sanitized history (no ``_active_turn_token``),
    echoes the repeated current prompt, and answers.  The persisted user
    row and its replayed copy are the same durable turn; treating them as
    payload-distinct reinserts history into the model context.
    """
    from api.streaming import _sanitize_messages_for_agent

    previous_context = _persisted_turn_one(with_api_content=True)
    replayed_history = _sanitize_messages_for_agent(previous_context)
    result_messages = copy.deepcopy(replayed_history) + [
        {"role": "user", "content": PROMPT},
        {"role": "assistant", "content": "the report now says B"},
    ]

    session = _settle_repeated_prompt_turn(
        previous_context,
        result_messages,
        authoritative=True,
    )

    context_users = _user_rows(session.context_messages)
    assert len(context_users) == 2, (
        f"historical user row duplicated in model context: {session.context_messages}"
    )
    assert _assistant_answers(session.context_messages) == [
        "the report says A",
        "the report now says B",
    ]
    assert len(_user_rows(session.messages)) == 2
    assert _assistant_answers(session.messages) == [
        "the report says A",
        "the report now says B",
    ]


def test_replayed_history_with_persisted_turn_token_legacy_authority():
    """(a) Same protection when an older Agent omits turn authority."""
    from api.streaming import _sanitize_messages_for_agent

    previous_context = _persisted_turn_one(with_api_content=True)
    replayed_history = _sanitize_messages_for_agent(previous_context)
    result_messages = copy.deepcopy(replayed_history) + [
        {"role": "user", "content": PROMPT},
        {"role": "assistant", "content": "the report now says B"},
    ]

    session = _settle_repeated_prompt_turn(
        previous_context,
        result_messages,
        authoritative=False,
    )

    context_users = _user_rows(session.context_messages)
    assert len(context_users) == 2, (
        f"historical user row duplicated in model context: {session.context_messages}"
    )
    assert _assistant_answers(session.context_messages) == [
        "the report says A",
        "the report now says B",
    ]


def test_current_answer_survives_full_history_replay_without_user_echo():
    """(b) The current answer must reach model context exactly once.

    The Agent replays the sanitized history and appends only the new
    assistant answer (the current user turn is supplied out of band).
    Model context and display must agree on the assistant answers; the
    current answer must not be dropped and the historical answer must not
    be duplicated.
    """
    from api.streaming import _sanitize_messages_for_agent

    previous_context = _persisted_turn_one(with_api_content=False)
    replayed_history = _sanitize_messages_for_agent(previous_context)
    result_messages = copy.deepcopy(replayed_history) + [
        {"role": "assistant", "content": "the report now says B"},
    ]

    session = _settle_repeated_prompt_turn(
        previous_context,
        result_messages,
        authoritative=False,
    )

    context_answers = _assistant_answers(session.context_messages)
    display_answers = _assistant_answers(session.messages)
    assert "the report now says B" in context_answers, (
        f"current answer dropped from model context: {session.context_messages}"
    )
    assert context_answers == ["the report says A", "the report now says B"]
    assert display_answers == context_answers, (
        "display/context divergence: "
        f"display={display_answers} context={context_answers}"
    )
