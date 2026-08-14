import copy
import json

import pytest


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

    assert _messages_have_prefix([identical], [first]) is True
    assert _messages_have_prefix([different], [first]) is False


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
