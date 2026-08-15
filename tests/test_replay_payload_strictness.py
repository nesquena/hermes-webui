import copy
import contextlib
import json
from types import SimpleNamespace

import pytest


class _IntSubclass(int):
    pass


class _StrSubclass(str):
    pass


def _empty_assistant(
    message_id: object = "assistant-a", *, finish_reason="stop", reasoning="same"
):
    return {
        "role": "assistant",
        "content": "",
        "id": message_id,
        "finish_reason": finish_reason,
        "reasoning": reasoning,
        "timestamp": 123,
    }


def _structured_assistant(image_url):
    return {
        "role": "assistant",
        "content": [{"type": "image_url", "image_url": image_url}],
        "finish_reason": "incomplete",
    }


def _session_payload(tmp_path, sid, *, messages, context_messages=None, **extra):
    payload = {
        "session_id": sid,
        "title": "strict replay repair",
        "workspace": str(tmp_path),
        "model": "test-model",
        "created_at": 100.0,
        "updated_at": 200.0,
        "messages": messages,
        "tool_calls": [],
    }
    if context_messages is not None:
        payload["context_messages"] = context_messages
    payload.update(extra)
    return payload


def _patch_store(monkeypatch, models, session_dir):
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")


def test_canonical_digest_is_type_faithful():
    from api.models import _canonical_message_digest

    tuple_payload = {"role": "assistant", "content": "", "meta": (1, 2)}
    list_payload = {"role": "assistant", "content": "", "meta": [1, 2]}
    int_key_payload = {"role": "assistant", "content": "", "meta": {1: "v"}}
    str_key_payload = {"role": "assistant", "content": "", "meta": {"1": "v"}}

    assert _canonical_message_digest(tuple_payload) is None
    assert _canonical_message_digest(int_key_payload) is None
    assert _canonical_message_digest(list_payload) is not None
    assert _canonical_message_digest(str_key_payload) is not None


def test_incomplete_reducer_requires_identical_full_payload():
    from api.models import _collapse_duplicate_incomplete_message_ids

    first = _empty_assistant(1701, finish_reason="incomplete", reasoning="alpha")
    distinct = _empty_assistant(1701, finish_reason="incomplete", reasoning="beta")
    duplicate = copy.deepcopy(first)

    collapsed, changed = _collapse_duplicate_incomplete_message_ids(
        [first, distinct, duplicate]
    )

    assert changed is True
    assert collapsed == [first, distinct]


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "text", "text": {}}],
        [{"type": "text", "text": 0}],
        [{"type": "text", "text": []}],
        [{"type": "text"}],
        [{"type": "text", "text": "", "content": "hidden alternative"}],
        [{"type": "text", "text": "", "image_url": "file:///A.png"}],
    ],
)
def test_empty_text_schema_rejects_ambiguous_blocks(content):
    from api.models import _is_admissible_empty_text_content

    assert _is_admissible_empty_text_content(content) is False


def test_replay_fallback_preserves_distinct_nontext_assistants():
    from api.streaming import _deduplicate_context_messages, _message_replay_key

    first = {
        "role": "assistant",
        "content": [{"type": "image_url", "image_url": "file:///A.png"}],
        "finish_reason": "incomplete",
        "id": 1,
    }
    second = {
        "role": "assistant",
        "content": [{"type": "image_url", "image_url": "file:///B.png"}],
        "finish_reason": "incomplete",
        "id": 2,
    }

    assert _message_replay_key(first) is None
    assert _message_replay_key(second) is None
    assert _deduplicate_context_messages([first, second]) == [first, second]


def test_replay_prefix_does_not_strip_noncomparable_structured_assistants():
    from api.streaming import _message_replay_key, _strip_replayed_prefix

    first = _structured_assistant("file:///A.png")
    second = _structured_assistant("file:///B.png")
    candidate_tail = {"role": "assistant", "content": "keep the tail"}

    assert _message_replay_key(first) is None
    assert _message_replay_key(second) is None
    assert _strip_replayed_prefix([first], [second, candidate_tail]) == [
        second,
        candidate_tail,
    ]


def test_replayed_context_block_requires_all_comparison_keys_to_be_conclusive():
    from api.streaming import _message_replay_key, _strip_replayed_context_items

    first = _structured_assistant("file:///A.png")
    second = _structured_assistant("file:///B.png")
    shared_tail = [
        {"role": "user", "content": "shared user row"},
        {"role": "assistant", "content": "shared assistant row"},
    ]
    existing = [first, *shared_tail]
    candidates = [second, *copy.deepcopy(shared_tail)]

    assert len(existing) == len(candidates) == 3
    assert _message_replay_key(first) is None
    assert _message_replay_key(second) is None
    assert _strip_replayed_context_items(existing, candidates) == candidates


def test_messages_prefix_rejects_noncomparable_structured_assistants():
    from api.streaming import _message_replay_key, _messages_have_prefix

    first = _structured_assistant("file:///A.png")
    second = _structured_assistant("file:///B.png")

    assert _message_replay_key(first) is None
    assert _message_replay_key(second) is None
    assert not _messages_have_prefix([first], [second], key_fn=_message_replay_key)


def test_default_prefix_accepts_only_exact_structured_payload():
    from api.streaming import _messages_have_prefix

    first = _structured_assistant("file:///A.png")
    identical = copy.deepcopy(first)
    different = _structured_assistant("file:///B.png")

    assert _messages_have_prefix([identical], [first]) is False
    assert _messages_have_prefix(
        [identical], [first], allow_exact_payload=True
    ) is True
    assert _messages_have_prefix(
        [different], [first], allow_exact_payload=True
    ) is False


def test_exact_prefix_mode_rejects_lossy_structured_identity_matches():
    from api.streaming import (
        _message_replay_key,
        _messages_have_prefix,
    )

    first = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "same visible text"},
            {"type": "image_url", "image_url": "file:///A.png", "detail": True},
        ],
    }
    different_image = copy.deepcopy(first)
    different_image["content"][1]["image_url"] = "file:///B.png"
    different_scalar_type = copy.deepcopy(first)
    different_scalar_type["content"][1]["detail"] = 1

    assert _message_replay_key(first) is None
    assert _message_replay_key(different_image) is None
    assert not _messages_have_prefix(
        [copy.deepcopy(first)], [first], key_fn=_message_replay_key
    )
    assert not _messages_have_prefix(
        [different_image], [first], allow_exact_payload=True
    )
    assert not _messages_have_prefix(
        [different_scalar_type], [first], allow_exact_payload=True
    )


def test_exact_prefix_mode_rejects_container_subclasses():
    from api.streaming import _message_replay_key, _messages_have_prefix

    class MessageDict(dict):
        pass

    first = {"role": "assistant", "content": "same visible text"}
    subclassed = MessageDict(copy.deepcopy(first))

    assert _message_replay_key(subclassed) is None
    assert not _messages_have_prefix(
        [subclassed], [first], key_fn=_message_replay_key
    )
    assert not _messages_have_prefix(
        [subclassed], [first], allow_exact_payload=True
    )


def test_exact_prefix_mode_requires_identical_nonempty_attachments():
    from api.streaming import _message_replay_key, _messages_have_prefix

    first = {
        "role": "user",
        "content": "same visible text",
        "attachments": [{"name": "A.pdf"}],
    }
    identical = copy.deepcopy(first)
    different = copy.deepcopy(first)
    different["attachments"][0]["name"] = "B.pdf"

    assert _message_replay_key(first) is None
    assert not _messages_have_prefix(
        [identical], [first], key_fn=_message_replay_key
    )
    assert not _messages_have_prefix([identical], [first])
    assert _messages_have_prefix(
        [identical], [first], allow_exact_payload=True
    )
    assert not _messages_have_prefix(
        [different], [first], allow_exact_payload=True
    )


def test_authoritative_prefix_rejects_different_durable_ids():
    from api.streaming import _result_has_authoritative_full_history_prefix

    previous = [{"role": "user", "content": "old prompt", "id": "old-user"}]
    result = [
        {"role": "user", "content": "old prompt", "id": "different-user"},
        {"role": "assistant", "content": "new answer"},
    ]

    assert not _result_has_authoritative_full_history_prefix(
        result,
        previous,
        {
            "text": "current prompt",
            "current_turn_user_idx": len(previous),
            "turn_id": "turn:durable-id-control",
        },
        "current prompt",
    )


def _settle_structured_result(previous, result):
    from api.streaming import _settle_result_messages

    prompt = "continue the active turn"
    session = SimpleNamespace(
        session_id="strict-structured-prefix",
        messages=copy.deepcopy(previous),
        context_messages=copy.deepcopy(previous),
        truncation_watermark=None,
    )
    _settle_result_messages(
        session,
        copy.deepcopy(previous),
        copy.deepcopy(previous),
        copy.deepcopy(result),
        prompt,
        "webui",
        {
            "token": "stream:strict-structured-prefix",
            "text": prompt,
            "timestamp": 200.0,
            "source": "webui",
            "attachments": [],
            "current_turn_user_idx": len(previous),
            "turn_id": "turn:strict-structured-prefix",
        },
    )
    return session


def test_settle_preserves_exact_structured_assistant_delta():
    previous = [_structured_assistant("file:///A.png")]
    previous[0]["id"] = "structured-assistant-a"
    repeated_delta = copy.deepcopy(previous[0])
    answer = {"role": "assistant", "content": "new answer"}

    session = _settle_structured_result(previous, [repeated_delta, answer])

    assert len(session.messages) == len(session.context_messages) == 4
    for projection in (session.messages, session.context_messages):
        assert sum(isinstance(row.get("content"), list) for row in projection) == 2
        assert sum(row.get("role") == "user" for row in projection) == 1
        assert projection[-1]["content"] == "new answer"


def test_settle_strips_exact_structured_prefix_from_full_history():
    previous = [_structured_assistant("file:///A.png")]
    previous[0]["id"] = "structured-assistant-a"
    current_user = {"role": "user", "content": "continue the active turn"}
    answer = {"role": "assistant", "content": "new answer"}

    session = _settle_structured_result(previous, [*previous, current_user, answer])

    assert len(session.messages) == len(session.context_messages) == 3
    for projection in (session.messages, session.context_messages):
        assert sum(isinstance(row.get("content"), list) for row in projection) == 1
        assert [row.get("role") for row in projection] == [
            "assistant",
            "user",
            "assistant",
        ]
        assert projection[-1]["content"] == "new answer"


def test_settle_strips_exact_structured_history_with_out_of_band_current_user():
    previous = [
        {"role": "user", "content": "old prompt", "id": "old-user"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "old answer", "annotations": ["durable"]}
            ],
            "id": "old-assistant",
            "reasoning": "durable reasoning",
            "api_content": "durable provider payload",
        },
    ]
    answer = {"role": "assistant", "content": "new answer", "id": "new-assistant"}

    session = _settle_structured_result(previous, [*copy.deepcopy(previous), answer])

    for projection in (session.messages, session.context_messages):
        assert len(projection) == 4
        assert projection[:2] == previous
        assert [row.get("role") for row in projection] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert projection[2]["content"] == "continue the active turn"
        assert projection[3] == answer


def test_settle_preserves_idless_exact_prefix_authority_after_id_assignment():
    previous = [
        {"role": "user", "content": "old prompt"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "old answer", "annotations": ["durable"]}
            ],
            "reasoning": "durable reasoning",
        },
    ]
    answer = {"role": "assistant", "content": "new answer"}

    session = _settle_structured_result(previous, [*copy.deepcopy(previous), answer])

    for projection in (session.messages, session.context_messages):
        assert [row.get("role") for row in projection] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert [row.get("content") for row in projection].count("old prompt") == 1
        assert sum(
            isinstance(row.get("content"), list)
            and row["content"][0].get("text") == "old answer"
            for row in projection
        ) == 1
        assert projection[-1]["content"] == "new answer"
        assert type(projection[-1].get("id")) is int


def test_settle_preserves_payload_distinct_rejected_history_prefix():
    previous = [
        {"role": "user", "content": "old prompt", "id": "old-user"},
        {
            "role": "assistant",
            "content": "same answer",
            "id": "old-assistant",
            "reasoning": "old reasoning",
            "model": "old-model",
            "request_id": "old-request",
        },
    ]
    distinct_history = {
        "role": "assistant",
        "content": "same answer",
        "id": "distinct-assistant",
        "reasoning": "distinct reasoning",
        "model": "distinct-model",
        "request_id": "distinct-request",
    }
    final = {
        "role": "assistant",
        "content": "final answer",
        "id": "final-assistant",
    }

    session = _settle_structured_result(
        previous,
        [copy.deepcopy(previous[0]), distinct_history, final],
    )

    for projection in (session.messages, session.context_messages):
        assert [row.get("role") for row in projection] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "assistant",
        ]
        assert [
            row.get("id")
            for row in projection
            if row.get("role") == "assistant"
        ] == ["old-assistant", "distinct-assistant", "final-assistant"]
        assert projection[1] == previous[1]
        assert projection[2]["content"] == "continue the active turn"
        assert projection[2]["_active_turn_token"] == (
            "stream:strict-structured-prefix"
        )
        assert type(projection[2].get("id")) is int
        assert projection[3] == distinct_history
    assert session.messages[2]["id"] == session.context_messages[2]["id"]


def test_sync_chat_uses_strict_turn_provenance_for_rejected_history_prefix(
    monkeypatch,
    tmp_path,
):
    """The synchronous route must not re-enable visible-only prefix deletion."""
    from api import config, routes

    previous = [
        {"role": "user", "content": "old prompt", "id": "old-user"},
        {
            "role": "assistant",
            "content": "same answer",
            "id": "old-assistant",
            "reasoning": "old reasoning",
            "model": "old-model",
            "request_id": "old-request",
        },
    ]
    distinct_history = {
        "role": "assistant",
        "content": "same answer",
        "id": "distinct-assistant",
        "reasoning": "distinct reasoning",
        "model": "distinct-model",
        "request_id": "distinct-request",
    }
    prompt = "continue the active turn"
    final = {
        "role": "assistant",
        "content": "final answer",
        "id": "final-assistant",
    }
    result = {
        "messages": [
            copy.deepcopy(previous[0]),
            copy.deepcopy(distinct_history),
            {"role": "user", "content": prompt},
            copy.deepcopy(final),
        ],
        "current_turn_user_idx": len(previous),
        "turn_id": "turn:sync-strict-prefix",
        "final_response": "final answer",
        "completed": True,
    }

    class _Session:
        session_id = "sync-strict-prefix"
        workspace = str(tmp_path)
        model = "test-model"
        model_provider = "test-provider"
        profile = "default"
        pending_user_source = "webui"
        title = "Already titled"
        input_tokens = 0
        output_tokens = 0
        estimated_cost = 0.0
        cache_read_tokens = 0
        cache_write_tokens = 0
        truncation_watermark = None
        messages = copy.deepcopy(previous)
        context_messages = copy.deepcopy(previous)

        def save(self):
            return None

        def compact(self):
            return {
                "session_id": self.session_id,
                "title": self.title,
                "message_count": len(self.messages),
            }

    class _Agent:
        def __init__(self, **_kwargs):
            self._persist_user_message_idx = len(previous)
            self._current_turn_id = "turn:sync-strict-prefix"

        def run_conversation(self, **_kwargs):
            return copy.deepcopy(result)

    session = _Session()
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: value)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: contextlib.nullcontext())
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_args: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider"),
    )
    monkeypatch.setattr(routes, "require_ai_agent_class", lambda: _Agent)
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "title_from", lambda _messages, fallback: fallback)
    monkeypatch.setattr(routes, "public_session_projection", lambda payload: payload)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200: payload)
    monkeypatch.setattr(
        config,
        "resolve_model_provider",
        lambda _model: ("test-model", "test-provider", None),
    )

    routes._handle_chat_sync(
        object(),
        {
            "session_id": session.session_id,
            "message": prompt,
            "workspace": str(tmp_path),
        },
    )

    for projection in (session.messages, session.context_messages):
        assert [row.get("role") for row in projection] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "assistant",
        ]
        assert [
            row.get("id")
            for row in projection
            if row.get("role") == "assistant"
        ] == ["old-assistant", "distinct-assistant", "final-assistant"]
        assert projection[2]["content"] == prompt
        assert type(projection[2].get("id")) is int
        assert projection[3] == distinct_history
    assert session.messages[2]["id"] == session.context_messages[2]["id"]


def test_display_backfill_preserves_payload_distinct_same_visible_assistant():
    from api.streaming import _merge_display_messages_after_agent_result

    user = {"role": "user", "content": "old prompt", "id": "old-user"}
    visible = {
        "role": "assistant",
        "content": "same answer",
        "id": "assistant-a",
        "reasoning": "first reasoning",
    }
    context_only = {
        "role": "assistant",
        "content": "same answer",
        "id": "assistant-b",
        "reasoning": "second reasoning",
    }
    current = {"role": "user", "content": "next prompt", "id": "current-user"}
    final = {"role": "assistant", "content": "done", "id": "assistant-final"}
    previous_display = [user, visible]
    previous_context = [user, visible, context_only]

    merged = _merge_display_messages_after_agent_result(
        copy.deepcopy(previous_display),
        copy.deepcopy(previous_context),
        copy.deepcopy(previous_context + [current, final]),
        "next prompt",
        verification_nudge_provenance={
            "active_turn_identity": {
                "token": "stream:backfill",
                "text": "next prompt",
                "current_turn_user_idx": len(previous_context),
                "turn_id": "turn:backfill",
            }
        },
    )

    assert [row.get("id") for row in merged] == [
        "old-user",
        "assistant-a",
        "assistant-b",
        "current-user",
        "assistant-final",
    ]
    assert merged[1] == visible
    assert merged[2] == context_only


def test_display_merge_collapses_only_exact_durable_empty_replay():
    from api.streaming import _merge_display_messages_after_agent_result

    prompt = "Continue the active turn."
    token = "stream:active-turn"
    active_user = {
        "role": "user",
        "content": prompt,
        "_active_turn_token": token,
    }
    replay = _empty_assistant("durable-empty")
    previous = [active_user, replay]
    provenance = {
        "active_turn_identity": {
            "token": token,
            "text": prompt,
            "current_turn_user_idx": 0,
            "turn_id": "turn-active",
        }
    }

    merged = _merge_display_messages_after_agent_result(
        previous,
        previous,
        previous + [copy.deepcopy(replay)],
        prompt,
        verification_nudge_provenance=provenance,
    )
    assert merged == previous

    distinct = copy.deepcopy(replay)
    distinct["reasoning"] = "different"
    merged_distinct = _merge_display_messages_after_agent_result(
        previous,
        previous,
        previous + [distinct],
        prompt,
        verification_nudge_provenance=provenance,
    )
    assert merged_distinct == previous + [distinct]

    synthetic_only = _merge_display_messages_after_agent_result(
        previous + [copy.deepcopy(replay)],
        previous + [copy.deepcopy(replay)],
        [
            {
                "role": "user",
                "content": "[System: verify the workspace]",
                "_verification_stop_synthetic": True,
            }
        ],
        prompt,
        verification_nudge_provenance=provenance,
    )
    assert synthetic_only == previous


@pytest.mark.parametrize(
    ("first", "distinct"),
    [
        (
            {"role": "assistant", "content": "same", "id": "assistant-a"},
            {"role": "assistant", "content": "same", "id": "assistant-b"},
        ),
        (
            {
                "role": "assistant",
                "content": "same",
                "attachments": [{"name": "A.pdf"}],
            },
            {
                "role": "assistant",
                "content": "same",
                "attachments": [{"name": "B.pdf"}],
            },
        ),
        (
            {"role": "assistant", "content": "same", "reasoning": "alpha"},
            {"role": "assistant", "content": "same", "reasoning": "beta"},
        ),
        (
            {"role": "assistant", "content": "same", "model": "model-a"},
            {"role": "assistant", "content": "same", "model": "model-b"},
        ),
        (
            {"role": "assistant", "content": "same", "request_id": "request-a"},
            {"role": "assistant", "content": "same", "request_id": "request-b"},
        ),
        (
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "same", "annotations": ["A"]}
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "same", "annotations": ["B"]}
                ],
            },
        ),
    ],
)
def test_display_merge_preserves_distinct_structured_nonempty_assistants(
    first,
    distinct,
):
    from api.streaming import _merge_display_messages_after_agent_result

    prompt = "Continue the active turn."

    merged = _merge_display_messages_after_agent_result(
        [],
        [],
        [first, distinct],
        prompt,
    )

    assistants = [row for row in merged if row.get("role") == "assistant"]
    assert assistants == [first, distinct]


def test_display_merge_collapses_exact_nonempty_assistant_payload():
    from api.streaming import _merge_display_messages_after_agent_result

    first = {
        "role": "assistant",
        "content": "same",
        "id": "assistant-a",
        "reasoning": "same reasoning",
        "attachments": [{"name": "A.pdf"}],
    }

    merged = _merge_display_messages_after_agent_result(
        [],
        [],
        [first, copy.deepcopy(first)],
        "Continue the active turn.",
    )

    assistants = [row for row in merged if row.get("role") == "assistant"]
    assert assistants == [first]


def test_settle_keeps_payload_distinct_assistants_in_both_projections():
    first = {
        "role": "assistant",
        "content": "same",
        "id": "assistant-a",
        "reasoning": "first reasoning",
        "annotations": [{"source": "A"}],
    }
    distinct = {
        **copy.deepcopy(first),
        "id": "assistant-b",
        "reasoning": "second reasoning",
        "annotations": [{"source": "B"}],
    }

    session = _settle_structured_result([], [first, distinct])

    for projection in (session.messages, session.context_messages):
        assert [row.get("role") for row in projection] == [
            "user",
            "assistant",
            "assistant",
        ]
        assert [
            row.get("id")
            for row in projection
            if row.get("role") == "assistant"
        ] == ["assistant-a", "assistant-b"]


def test_settle_collapses_exact_assistant_payload_in_both_projections():
    first = {
        "role": "assistant",
        "content": "same",
        "id": "assistant-a",
        "reasoning": "same reasoning",
        "attachments": [{"name": "A.pdf"}],
    }

    session = _settle_structured_result([], [first, copy.deepcopy(first)])

    for projection in (session.messages, session.context_messages):
        assistants = [row for row in projection if row.get("role") == "assistant"]
        assert assistants == [first]


def test_settle_collapses_idless_exact_assistant_before_stable_id_assignment():
    first = {
        "role": "assistant",
        "content": "same",
        "reasoning": "same reasoning",
        "attachments": [{"name": "A.pdf"}],
    }

    session = _settle_structured_result([], [first, copy.deepcopy(first)])

    for projection in (session.messages, session.context_messages):
        assistants = [row for row in projection if row.get("role") == "assistant"]
        assert len(assistants) == 1
        assert assistants[0]["content"] == "same"
        assert type(assistants[0].get("id")) is int


def test_settle_preserves_idless_repeated_answer_across_current_turn_boundary():
    previous = [
        {"role": "user", "content": "Say it once."},
        {"role": "assistant", "content": "same"},
    ]
    result = [*copy.deepcopy(previous), {"role": "assistant", "content": "same"}]

    session = _settle_structured_result(previous, result)

    for projection in (session.messages, session.context_messages):
        assert [row.get("role") for row in projection] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert [
            row.get("content")
            for row in projection
            if row.get("role") == "assistant"
        ] == ["same", "same"]
        assistant_ids = [
            row.get("id")
            for row in projection
            if row.get("role") == "assistant"
        ]
        assert type(assistant_ids[-1]) is int
        if assistant_ids[0] is not None:
            assert assistant_ids[0] != assistant_ids[-1]


@pytest.mark.parametrize(
    "bad_idx",
    [True, 1.0, 1.5, "1", _IntSubclass(1), None, -1],
)
def test_active_turn_authority_rejects_lossy_index_types(bad_idx):
    from api.streaming import (
        _active_turn_boundary_is_valid,
        _resolve_active_turn_authority,
    )

    resolved = _resolve_active_turn_authority(
        {"current_turn_user_idx": None, "turn_id": ""},
        result={"current_turn_user_idx": bad_idx, "turn_id": "turn-1"},
    )

    assert resolved["current_turn_user_idx"] is None
    assert _active_turn_boundary_is_valid(resolved) is False


@pytest.mark.parametrize(
    "bad_turn_id",
    [None, "", "   ", 1, ["turn-1"], {"turn": "1"}, _StrSubclass("turn-1")],
)
def test_active_turn_authority_rejects_lossy_turn_id_types(bad_turn_id):
    from api.streaming import (
        _active_turn_boundary_is_valid,
        _resolve_active_turn_authority,
    )

    resolved = _resolve_active_turn_authority(
        {"current_turn_user_idx": None, "turn_id": ""},
        result={"current_turn_user_idx": 1, "turn_id": bad_turn_id},
    )

    assert resolved["current_turn_user_idx"] is None
    assert resolved["turn_id"] == ""
    assert _active_turn_boundary_is_valid(resolved) is False


def test_active_turn_authority_does_not_mix_fields_across_attempts():
    from api.streaming import (
        _active_turn_boundary_is_valid,
        _resolve_active_turn_authority,
    )

    resolved = _resolve_active_turn_authority(
        {
            "token": "stream:attempt",
            "text": "prompt",
            "current_turn_user_idx": 1,
            "turn_id": "turn:first-attempt",
        },
        result={"current_turn_user_idx": 2, "turn_id": []},
        agent=SimpleNamespace(
            _persist_user_message_idx=None,
            _current_turn_id="turn:second-attempt",
        ),
    )

    assert resolved["current_turn_user_idx"] is None
    assert resolved["turn_id"] == ""
    assert _active_turn_boundary_is_valid(resolved) is False


def test_active_turn_authority_accepts_complete_result_pair_atomically():
    from api.streaming import _resolve_active_turn_authority

    resolved = _resolve_active_turn_authority(
        {"current_turn_user_idx": 1, "turn_id": "turn:first-attempt"},
        result={"current_turn_user_idx": 2, "turn_id": "turn:result"},
        agent=SimpleNamespace(
            _persist_user_message_idx=3,
            _current_turn_id="turn:agent",
        ),
    )

    assert resolved["current_turn_user_idx"] == 2
    assert resolved["turn_id"] == "turn:result"


def test_active_turn_authority_falls_back_to_complete_agent_pair():
    from api.streaming import _resolve_active_turn_authority

    resolved = _resolve_active_turn_authority(
        {"current_turn_user_idx": 1, "turn_id": "turn:first-attempt"},
        result={"current_turn_user_idx": 2, "turn_id": []},
        agent=SimpleNamespace(
            _persist_user_message_idx=3,
            _current_turn_id="turn:agent",
        ),
    )

    assert resolved["current_turn_user_idx"] == 3
    assert resolved["turn_id"] == "turn:agent"


def test_partial_reducer_only_removes_identical_rows():
    from api.models import _collapse_adjacent_duplicate_partials

    first = {
        **_empty_assistant(1701, finish_reason="incomplete"),
        "_partial": True,
        "attachments": [{"name": "A.pdf"}],
    }
    different_id = {**copy.deepcopy(first), "id": 1702}
    different_attachment = copy.deepcopy(first)
    different_attachment["attachments"] = [{"name": "B.pdf"}]

    collapsed, changed = _collapse_adjacent_duplicate_partials(
        [first, different_id, different_attachment, copy.deepcopy(different_attachment)]
    )

    assert changed is True
    assert collapsed == [first, different_id, different_attachment]


def test_non_incomplete_partial_replay_key_uses_exact_payload_digest():
    from api.streaming import _message_replay_key

    first = {
        "role": "assistant",
        "content": "",
        "id": "partial-a",
        "_partial": True,
        "reasoning": "working",
        "timestamp": 123,
    }
    exact = copy.deepcopy(first)
    distinct = copy.deepcopy(first)
    distinct["reasoning"] = "different"

    assert _message_replay_key(first) == _message_replay_key(exact)
    assert _message_replay_key(first) != _message_replay_key(distinct)


def test_load_repairs_messages_and_context_with_one_pipeline(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "strict-context-parity"
    replay = _empty_assistant("assistant-a")
    payload = _session_payload(
        tmp_path,
        sid,
        messages=[replay, copy.deepcopy(replay)],
        context_messages=[copy.deepcopy(replay), copy.deepcopy(replay)],
    )
    (session_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = models.Session.load(sid)
    persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))

    assert loaded is not None
    assert len(loaded.messages) == len(loaded.context_messages) == 1
    assert len(persisted["messages"]) == len(persisted["context_messages"]) == 1


def test_load_preserves_distinct_nonempty_payloads_and_repairs_exact_replay(
    tmp_path,
    monkeypatch,
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "strict-nonempty-context-parity"
    first = {
        "role": "assistant",
        "content": "same",
        "id": "assistant-a",
        "reasoning": "first",
    }
    distinct = {
        **copy.deepcopy(first),
        "id": "assistant-b",
        "reasoning": "second",
    }
    payload = _session_payload(
        tmp_path,
        sid,
        messages=[first, distinct, copy.deepcopy(distinct)],
        context_messages=[
            copy.deepcopy(first),
            copy.deepcopy(distinct),
            copy.deepcopy(distinct),
        ],
    )
    (session_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = models.Session.load(sid)
    persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))

    expected_ids = ["assistant-a", "assistant-b"]
    assert loaded is not None
    assert [row["id"] for row in loaded.messages] == expected_ids
    assert [row["id"] for row in loaded.context_messages] == expected_ids
    assert [row["id"] for row in persisted["messages"]] == expected_ids
    assert [row["id"] for row in persisted["context_messages"]] == expected_ids


@pytest.mark.parametrize("kind", ["partial", "incomplete"])
def test_any_visible_reduction_invalidates_positional_anchor(
    tmp_path, monkeypatch, kind
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = f"strict-anchor-{kind}"
    if kind == "partial":
        replay = {**_empty_assistant(1701), "_partial": True}
    else:
        replay = _empty_assistant(1701, finish_reason="incomplete")
    payload = _session_payload(
        tmp_path,
        sid,
        messages=[
            {"role": "user", "content": "before"},
            replay,
            copy.deepcopy(replay),
            {"role": "assistant", "content": "anchor"},
        ],
        compression_anchor_visible_idx=3,
        compression_anchor_message_key="stable-key",
    )
    (session_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = models.Session.load(sid)

    assert loaded is not None
    assert loaded.compression_anchor_visible_idx is None
    assert loaded.compression_anchor_message_key == "stable-key"
