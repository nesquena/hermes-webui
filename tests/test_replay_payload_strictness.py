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
