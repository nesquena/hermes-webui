"""
Unit tests for message timestamp formatting, regex matching, workspace prefix stripping,
and turn identity deduplication under api/streaming.py.
"""
from api.streaming import (
    _TIMESTAMP_PREFIX_RE,
    _time_context_prefix,
    _strip_workspace_prefix,
    _strip_workspace_prefixes_for_compare,
    _message_identity,
    _drop_checkpointed_current_user_from_context,
    _build_native_multimodal_message,
    _fallback_title_from_exchange,
    _looks_like_current_user_turn,
)


def test_time_context_prefix_format_and_match():
    ts = 1721412020.0
    prefix = _time_context_prefix(ts)
    assert prefix.startswith("[Time: ")
    assert prefix.endswith("]\n")
    assert _TIMESTAMP_PREFIX_RE.match(prefix) is not None


def test_timestamp_prefix_re_variants():
    valid_prefixes = [
        "[Time: 2026-07-19T18:00:20+00:00]\n",
        "[Time: 2026-07-19T18:00:20+0000]\n",
        "[Time: 2026-07-19T18:00:20-03:00]\n",
        "[Time: 2026-07-19T18:00:20-0500]\n",
        "[Time: 2026-07-19T18:00:20Z]\n",
        "[Time:  2026-07-19T18:00:20+00:00]\n",
    ]
    for p in valid_prefixes:
        assert _TIMESTAMP_PREFIX_RE.match(p) is not None, f"Failed to match: {p}"

    invalid_prefixes = [
        "[Time: 2026-07-19]\n",
        "Time: 2026-07-19T18:00:20Z\n",
        "[Time: not-a-date]\n",
        "[Time: 2026/07/19 18:00:20]\n",
    ]
    for p in invalid_prefixes:
        assert _TIMESTAMP_PREFIX_RE.match(p) is None, f"Unexpected match: {p}"


def test_strip_workspace_prefix_with_timestamp():
    ts_prefix = "[Time: 2026-07-19T18:00:20+00:00]\n"
    ws_prefix = "[Workspace::v1:/tmp/test]\n"
    user_prompt = "Explain quantum computing."

    full_message = f"{ts_prefix}{ws_prefix}{user_prompt}"
    stripped = _strip_workspace_prefix(full_message)
    assert stripped == user_prompt

    # Just timestamp without workspace prefix
    only_ts = f"{ts_prefix}{user_prompt}"
    assert _strip_workspace_prefix(only_ts) == user_prompt

    # For compare helper
    assert _strip_workspace_prefixes_for_compare(full_message) == user_prompt


def test_message_identity_with_timestamp_dedup():
    ts_prefix = "[Time: 2026-07-19T18:00:20+00:00]\n"
    ws_prefix = "[Workspace::v1:/home/user]\n"
    prompt = "Hello world!"

    raw_user_msg = {"role": "user", "content": prompt}
    prefixed_user_msg = {"role": "user", "content": f"{ts_prefix}{ws_prefix}{prompt}"}

    assert _message_identity(raw_user_msg) == _message_identity(prefixed_user_msg)


def test_drop_checkpointed_current_user_from_context():
    prompt = "How does this function work?"
    ts_prefix = "[Time: 2026-07-19T18:00:20+00:00]\n"
    ws_prefix = "[Workspace::v1:/workspace]\n"

    history = [
        {"role": "user", "content": "Previous prompt"},
        {"role": "assistant", "content": "Previous answer"},
        {"role": "user", "content": f"{ts_prefix}{ws_prefix}{prompt}"},
    ]

    pruned = _drop_checkpointed_current_user_from_context(history, prompt)
    assert len(pruned) == 2
    assert pruned[-1]["role"] == "assistant"


def test_looks_like_current_user_turn():
    prompt = "Check disk space"
    ts_prefix = "[Time: 2026-07-19T18:00:20+00:00]\n"
    ws_prefix = "[Workspace::v1:/workspace]\n"

    msg = {"role": "user", "content": f"{ts_prefix}{ws_prefix}{prompt}"}
    assert _looks_like_current_user_turn(msg, prompt) is True


def test_fallback_title_from_exchange_strips_timestamp():
    ts_prefix = "[Time: 2026-07-19T18:00:20+00:00]\n"
    ws_prefix = "[Workspace::v1:/workspace]\n"
    user_text = f"{ts_prefix}{ws_prefix}Write a snake game in Python"
    assistant_text = "Here is a simple snake game using curses."

    title = _fallback_title_from_exchange(user_text, assistant_text)
    assert title is not None
    assert "Time:" not in title
    assert "[time" not in title.lower()
    assert "snake" in title.lower()


def test_build_native_multimodal_message_timestamps():
    cfg_enabled = {"gateway": {"message_timestamps": {"enabled": True}}}
    cfg_disabled = {"gateway": {"message_timestamps": {"enabled": False}}}

    workspace_ctx = "[Workspace::v1:/ws]\n"
    msg_text = "Hello!"
    now = 1721412020.0

    # Enabled
    res_enabled = _build_native_multimodal_message(
        workspace_ctx, msg_text, [], "/ws", cfg=cfg_enabled, turn_started_at=now
    )
    assert res_enabled.startswith("[Time: ")
    assert "[Workspace::v1:/ws]\nHello!" in res_enabled

    # Disabled
    res_disabled = _build_native_multimodal_message(
        workspace_ctx, msg_text, [], "/ws", cfg=cfg_disabled, turn_started_at=now
    )
    assert not res_disabled.startswith("[Time: ")
    assert res_disabled == workspace_ctx + msg_text

    # None turn_started_at
    res_no_ts = _build_native_multimodal_message(
        workspace_ctx, msg_text, [], "/ws", cfg=cfg_enabled, turn_started_at=None
    )
    assert res_no_ts == workspace_ctx + msg_text


def test_build_native_multimodal_message_malformed_config():
    workspace_ctx = "[Workspace::v1:/ws]\n"
    msg_text = "Hello!"
    now = 1721412020.0

    malformed_configs = [
        None,
        {},
        {"gateway": None},
        {"gateway": "invalid"},
        {"gateway": {"message_timestamps": None}},
        {"gateway": {"message_timestamps": "true"}},
        {"gateway": {"message_timestamps": {"enabled": None}}},
        {"gateway": {"message_timestamps": {"enabled": 0}}},
        [],
        "config_string",
    ]
    for bad_cfg in malformed_configs:
        res = _build_native_multimodal_message(
            workspace_ctx, msg_text, [], "/ws", cfg=bad_cfg, turn_started_at=now
        )
        assert not res.startswith("[Time: ")
        assert res == workspace_ctx + msg_text


def test_build_native_multimodal_message_text_mode_with_timestamps():
    cfg_text_mode = {
        "gateway": {"message_timestamps": {"enabled": True}},
        "agent": {"image_input_mode": "text"},
    }
    workspace_ctx = "[Workspace::v1:/ws]\n"
    msg_text = "What is in this photo?"
    now = 1721412020.0

    # With attachments and text mode
    res = _build_native_multimodal_message(
        workspace_ctx, msg_text, ["dummy.png"], "/ws", cfg=cfg_text_mode, turn_started_at=now
    )
    assert isinstance(res, str)
    assert res.startswith("[Time: ")
    assert "[Workspace::v1:/ws]\nWhat is in this photo?" in res


def test_time_context_prefix_timezone_and_roundtrip(monkeypatch):
    # Test that prefix roundtrips through stripping regardless of local timezone/DST
    ts = 1721412020.0  # July 2026 (DST in northern hemisphere)
    winter_ts = 1705687220.0  # January 2024 (Standard time in northern hemisphere)

    for test_ts in (ts, winter_ts):
        prefix = _time_context_prefix(test_ts)
        assert _TIMESTAMP_PREFIX_RE.match(prefix) is not None
        body = "Test body"
        combined = f"{prefix}[Workspace::v1:/dir]\n{body}"
        assert _strip_workspace_prefix(combined) == body
