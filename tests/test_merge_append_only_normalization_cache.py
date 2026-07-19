from collections import defaultdict

import api.models as models


class _CountingList(list):
    def __init__(self, *args, str_counter):
        super().__init__(*args)
        self._str_counter = str_counter

    def __str__(self):
        self._str_counter["count"] += 1
        return super().__str__()


class _ExplodingList(list):
    def __init__(self, *args, str_counter):
        super().__init__(*args)
        self._str_counter = str_counter

    def __str__(self):
        self._str_counter["count"] += 1
        raise RuntimeError("content __str__ must not run")


def _install_key_wrappers(monkeypatch):
    call_counts: dict[str, dict[int, int]] = {
        "merge": defaultdict(int),
        "dedup": defaultdict(int),
        "content": defaultdict(int),
        "visible": defaultdict(int),
    }

    def _install(name):
        original = getattr(models, f"_session_message_{name}_key")

        def _wrapped(message, *args, **kwargs):
            if isinstance(message, dict):
                call_counts[name][id(message)] += 1
            return original(message, *args, **kwargs)

        monkeypatch.setattr(models, f"_session_message_{name}_key", _wrapped)

    for name in call_counts:
        _install(name)

    return call_counts


def test_merge_append_only_caches_canonical_keys_and_preserves_identity(monkeypatch):
    """Verify exact key-call fanout and output identity for shared row objects."""
    str_calls = {"count": 0}
    repeated_user_content = _CountingList(["hello", "world"], str_counter=str_calls)
    repeated_assistant_content = _CountingList(["reply", "with", "spacing"], str_counter=str_calls)

    repeated_user = {
        "role": "user",
        "content": repeated_user_content,
        "timestamp": 1000,
    }
    repeated_assistant = {
        "role": "assistant",
        "content": repeated_assistant_content,
        "timestamp": 1001,
    }
    sidecar_messages = [repeated_user, repeated_user, repeated_user, repeated_assistant]
    state_messages = [repeated_assistant, repeated_user]

    call_counts = _install_key_wrappers(monkeypatch)
    merged = models.merge_session_messages_append_only(sidecar_messages, state_messages)

    assert [id(msg) for msg in merged] == [
        id(repeated_user),
        id(repeated_user),
        id(repeated_user),
        id(repeated_assistant),
    ]
    assert merged == [repeated_user, repeated_user, repeated_user, repeated_assistant]

    assert str_calls["count"] == 2
    assert dict(call_counts["merge"]) == {
        id(repeated_user): 1,
        id(repeated_assistant): 1,
    }
    assert len(dict(call_counts["dedup"])) == 2
    assert len(dict(call_counts["content"])) == 2
    assert len(dict(call_counts["visible"])) == 2
    assert set(dict(call_counts["dedup"]).values()) == {1}
    assert set(dict(call_counts["content"]).values()) == {1}
    assert set(dict(call_counts["visible"]).values()) == {1}


def test_merge_append_only_helpers_do_not_see_mutated_content(monkeypatch):
    original_content = _CountingList(["streamed", "content"], str_counter={"count": 0})
    sidecar_messages = [
        {
            "role": "user",
            "content": original_content,
            "timestamp": 1000,
        }
    ]
    state_messages = [
        {
            "role": "assistant",
            "content": "ack",
            "timestamp": 1001,
        }
    ]

    observed = {"helpers_invoked": False}
    call_counts: dict[str, dict[int, int]] = {
        "merge": defaultdict(int),
        "dedup": defaultdict(int),
        "content": defaultdict(int),
        "visible": defaultdict(int),
    }

    def _install(name, *, assert_content_content=False):
        original = getattr(models, f"_session_message_{name}_key")

        def _wrapped(message, *args, **kwargs):
            if isinstance(message, dict):
                call_counts[name][id(message)] += 1
            if assert_content_content and isinstance(message, dict):
                # Verify the aliased sidecar dict still owns the original object
                # during helper execution.
                assert sidecar_messages[0]["content"] is original_content
                observed["helpers_invoked"] = True
            return original(message, *args, **kwargs)

        monkeypatch.setattr(models, f"_session_message_{name}_key", _wrapped)

    _install("merge")
    _install("dedup")
    _install("visible")
    _install("content", assert_content_content=True)

    models.merge_session_messages_append_only(sidecar_messages, state_messages)

    assert observed["helpers_invoked"] is True
    assert len(dict(call_counts["content"])) == 2
    assert set(dict(call_counts["content"]).values()) == {1}


def test_merge_append_only_state_only_stable_id_does_not_stringify_content(monkeypatch):
    """State-only merge keeps stable-ID rows on the helper fast path."""
    str_calls = {"count": 0}
    message = {
        "id": "stable-id",
        "role": "assistant",
        "content": _ExplodingList(["danger", "zone"], str_counter=str_calls),
        "timestamp": 2000,
    }

    call_counts = _install_key_wrappers(monkeypatch)
    merged = models.merge_session_messages_append_only([], [message])

    assert merged == [message]
    assert merged[0] is message
    assert str_calls["count"] == 0
    assert dict(call_counts["merge"]) == {}
    assert dict(call_counts["dedup"]) == {
        id(message): 1,
    }
    assert dict(call_counts["content"]) == {}
    assert dict(call_counts["visible"]) == {}


def test_merge_append_only_distinct_equal_dicts_remain_distinct_inputs(monkeypatch):
    """Two equal dict rows still pass through as separate inputs."""
    msg_1 = {
        "role": "user",
        "content": "same",
        "timestamp": 1000,
    }
    msg_2 = {
        "role": "user",
        "content": "same",
        "timestamp": 1000,
    }

    call_counts = _install_key_wrappers(monkeypatch)
    merged = models.merge_session_messages_append_only([], [msg_1, msg_2])

    assert merged == [msg_1]
    assert merged[0] is msg_1
    assert dict(call_counts["dedup"]) == {
        id(msg_1): 1,
        id(msg_2): 1,
    }
    assert dict(call_counts["merge"]) == {}


def test_merge_append_only_recomputes_after_mutation(monkeypatch):
    """Per-call key caches reset; mutation between calls changes work input."""
    str_calls = {"count": 0}
    state_message = {
        "role": "assistant",
        "content": _CountingList(["first"], str_counter=str_calls),
        "timestamp": 3000,
    }

    call_counts = _install_key_wrappers(monkeypatch)
    first = models.merge_session_messages_append_only([], [state_message])
    state_message["content"].append("second")
    second = models.merge_session_messages_append_only([], [state_message])

    assert first == [state_message]
    assert second == [state_message]
    assert first is not second
    assert str_calls["count"] == 2
    assert dict(call_counts["dedup"]) == {
        id(state_message): 2,
    }
    assert dict(call_counts["merge"]) == {}
    assert dict(call_counts["content"]) == {}
    assert dict(call_counts["visible"]) == {}
