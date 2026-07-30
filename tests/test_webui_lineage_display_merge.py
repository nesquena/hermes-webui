from types import SimpleNamespace

import api.routes as routes


def _msg(role, content, ts):
    return {"role": role, "content": content, "timestamp": ts}


def test_webui_lineage_display_merge_includes_parent_only_rows(monkeypatch):
    parent = SimpleNamespace(
        session_id="parent",
        messages=[
            _msg("user", "first prompt", 1),
            _msg("assistant", "first answer", 2),
            _msg("user", "parent only prompt", 3),
            _msg("assistant", "parent only answer", 4),
        ],
    )
    tip = SimpleNamespace(
        session_id="tip",
        parent_session_id="parent",
        session_source="webui",
        messages=[
            _msg("user", "first prompt", 1),
            _msg("assistant", "first answer", 2),
            _msg("user", "tip prompt", 5),
            _msg("assistant", "tip answer", 6),
        ],
        truncation_watermark=None,
    )

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: parent)

    merged = routes._merged_webui_lineage_messages_for_display(tip, tip.messages)

    assert [m["content"] for m in merged] == [
        "first prompt",
        "first answer",
        "parent only prompt",
        "parent only answer",
        "tip prompt",
        "tip answer",
    ]


def test_webui_lineage_display_merge_preserves_duplicate_turn_duration(monkeypatch):
    parent = SimpleNamespace(
        session_id="parent",
        messages=[
            _msg("assistant", "final answer", 2000.0),
        ],
    )
    tip = SimpleNamespace(
        session_id="tip",
        parent_session_id="parent",
        session_source="webui",
        messages=[
            {
                "role": "assistant",
                "content": "final answer",
                "timestamp": 2000.0,
                "_turnDuration": 502.765,
            },
        ],
        truncation_watermark=None,
    )

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: parent)

    merged = routes._merged_webui_lineage_messages_for_display(tip, tip.messages)

    assert len(merged) == 1
    assert merged[0]["content"] == "final answer"
    assert merged[0]["_turnDuration"] == 502.765


def test_webui_lineage_display_keeps_cumulative_child_tail_without_timestamps(monkeypatch):
    parent = SimpleNamespace(
        session_id="parent",
        messages=[
            _msg("user", "first prompt", 1000.0),
            _msg("assistant", "first answer", 1001.0),
        ],
    )
    tip = SimpleNamespace(
        session_id="tip",
        parent_session_id="parent",
        session_source="webui",
        messages=[
            _msg("user", "first prompt", 1000.0),
            _msg("assistant", "first answer", 1001.0),
            {"role": "user", "content": "continue after compression"},
            {"role": "assistant", "content": "final after compression"},
        ],
        truncation_watermark=None,
    )

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: parent)

    merged = routes._merged_webui_lineage_messages_for_display(tip, tip.messages)

    assert [m["content"] for m in merged] == [
        "first prompt",
        "first answer",
        "continue after compression",
        "final after compression",
    ]
    assert merged[-1]["role"] == "assistant"


def test_webui_lineage_display_merge_skips_explicit_forks(monkeypatch):
    parent = SimpleNamespace(
        session_id="parent",
        messages=[_msg("user", "parent-only", 1)],
    )
    fork = SimpleNamespace(
        session_id="fork",
        parent_session_id="parent",
        session_source="fork",
        messages=[_msg("user", "fork starts here", 2)],
        truncation_watermark=None,
    )

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: parent)

    merged = routes._merged_webui_lineage_messages_for_display(fork, fork.messages)

    assert [m["content"] for m in merged] == ["fork starts here"]
