"""Regression coverage for large-session fuzzy duplicate matching."""

from __future__ import annotations

import pytest


def _visible_keys(count: int) -> set[tuple]:
    return {("assistant", f"message {idx}", "") for idx in range(count)}


def test_large_visible_set_keeps_exact_duplicate_matching():
    import api.models as models

    keys = _visible_keys(1001)
    exact = ("assistant", "message 1000", "")

    assert models._matching_visible_duplicate(exact, keys) == exact


def test_large_visible_set_skips_fuzzy_duplicate_scan(monkeypatch):
    import api.models as models

    keys = _visible_keys(1001)
    fuzzy_only = ("assistant", "message 1000 with suffix", "")

    def unexpected_lookup(_keys):
        raise AssertionError("large visible sets must not build the fuzzy lookup")

    monkeypatch.setattr(models, "_build_visible_duplicate_lookup", unexpected_lookup)

    assert models._matching_visible_duplicate(fuzzy_only, keys) is None


def test_threshold_sized_visible_set_preserves_fuzzy_recovery():
    import api.models as models

    keys = _visible_keys(1000)
    fuzzy_only = ("assistant", "message 999 with suffix", "")

    assert models._matching_visible_duplicate(fuzzy_only, keys) in keys


@pytest.mark.parametrize(
    "workspace_prefix",
    ("[Workspace::v1: /tmp/project]", "[Workspace: /tmp/project]"),
)
def test_large_merge_dedupes_workspace_prefixed_middle_replay(workspace_prefix):
    import api.models as models

    sidecar = [
        {
            "role": "user" if idx % 2 == 0 else "assistant",
            "content": f"message {idx}",
            "timestamp": float(idx),
        }
        for idx in range(1000)
    ]
    sidecar.insert(
        500,
        {"role": "user", "content": "canonical prompt", "timestamp": 499.5},
    )
    sidecar.append(
        {"role": "assistant", "content": "final assistant", "timestamp": 1001.0}
    )
    state_replay = [
        {
            "role": "user",
            "content": f"{workspace_prefix}\ncanonical prompt",
            "timestamp": 2000.0,
        }
    ]

    merged = models.merge_session_messages_append_only(sidecar, state_replay)

    assert len(merged) == len(sidecar)
    assert merged[-1]["role"] == "assistant"
    assert merged[-1]["content"] == "final assistant"


def test_large_merge_preserves_literal_workspace_prefix_as_distinct_turn():
    import api.models as models

    trailing_text = "same trailing text"
    literal_sidecar_text = f"[Workspace: user typed this literally]\n{trailing_text}"
    sidecar = [
        {
            "role": "user" if idx % 2 == 0 else "assistant",
            "content": f"message {idx}",
            "timestamp": float(idx),
        }
        for idx in range(1000)
    ]
    sidecar.insert(
        500,
        {"role": "user", "content": literal_sidecar_text, "timestamp": 499.5},
    )
    sidecar.append(
        {"role": "assistant", "content": "stale assistant tail", "timestamp": 1001.0}
    )
    distinct_state_turn = {
        "role": "user",
        "content": f"[Workspace::v1: /tmp/actual]\n{trailing_text}",
        "timestamp": 2000.0,
    }

    merged = models.merge_session_messages_append_only(
        sidecar,
        [distinct_state_turn],
    )

    assert len(merged) == len(sidecar) + 1
    assert any(message.get("content") == literal_sidecar_text for message in merged)
    assert merged[-1] is distinct_state_turn


def test_small_visible_set_preserves_fuzzy_recovery():
    import api.models as models

    keys = {("assistant", "canonical answer", "")}
    fuzzy_only = ("assistant", "canonical answer with suffix", "")

    assert models._matching_visible_duplicate(fuzzy_only, keys) in keys


def test_visible_duplicate_lookup_caches_existing_key_parses(monkeypatch):
    import api.models as models

    rich = [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]
    existing = models._session_message_visible_key({"role": "user", "content": rich})
    lookup = models._build_visible_duplicate_lookup({existing})
    candidate = models._session_message_visible_key(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,BB=="}},
            ],
        }
    )
    loads = 0
    real_loads = models.json.loads

    def counted_loads(*args, **kwargs):
        nonlocal loads
        loads += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(models.json, "loads", counted_loads)
    for _ in range(3):
        assert models._matching_visible_duplicate(candidate, {existing}, lookup) is None

    assert loads == 4  # candidate once per probe, existing key once for the shared lookup


def test_large_mixed_shape_reconciliation_preserves_image_identity():
    import api.models as models

    def rich(image, text):
        return [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image}},
        ]

    image_a = "data:image/png;base64," + "A" * 210_000
    image_b = "data:image/png;base64," + "B" * 210_000
    bare_text = "describe this image"
    prefixed_text = "[Workspace::v1: /tmp/project]\n" + bare_text

    merged = models.merge_session_messages_append_only(
        [{"role": "user", "content": bare_text, "timestamp": 1000.0}],
        [{
            "role": "user",
            "content": rich(image_a, prefixed_text),
            "timestamp": 2000.0,
        }],
    )
    users = [message for message in merged if message.get("role") == "user"]
    assert len(users) == 2
    assert any(message["content"] == bare_text for message in users)
    assert any(message["content"] == rich(image_a, prefixed_text) for message in users)

    structured = models.merge_session_messages_append_only(
        [{"role": "user", "content": rich(image_a, bare_text), "timestamp": 1000.0}],
        [{
            "role": "user",
            "content": rich(image_b, prefixed_text),
            "timestamp": 2000.0,
        }],
    )
    assert len([message for message in structured if message.get("role") == "user"]) == 2
