"""#8034 — the Gateway runs history must not carry rows the legacy path filters.

``_run_gateway_runs_api_streaming`` builds ``/v1/runs`` ``conversation_history``
from ``session.context_messages``. The legacy in-process path sends the same
rows through ``_sanitize_messages_for_api``, which skips persisted error
markers and partial rows with no visible content. The Gateway builder did
neither, so a provider-error row reached the Gateway as an assistant turn and a
reasoning-only or tool-only cancellation reached it as empty assistant content.

The rows below have the shapes the app writes: the error marker of
``api/gateway_chat.py`` and ``api/streaming.py`` (``_error: True``), the cancel
marker (``_error: True`` with ``provider_details``), and the partial built by
``_build_partial_assistant_message`` (``_partial: True``, content possibly empty,
``reasoning`` / ``_partial_tool_calls`` beside it).
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

USER = {"role": "user", "content": "first question", "timestamp": 1}
ANSWER = {"role": "assistant", "content": "first answer", "timestamp": 2}
ERROR_ROW = {
    "role": "assistant",
    "content": "**Provider error:** upstream returned 500\n\n*Try again in a moment.*",
    "timestamp": 3,
    "_error": True,
    "provider_details": "HTTP 500",
}
CANCEL_ROW = {
    "role": "assistant",
    "content": "Task cancelled.",
    "_error": True,
    "provider_details": "Task cancelled.",
    "provider_details_label": "Cancellation details",
    "timestamp": 4,
}
EMPTY_PARTIAL_REASONING = {
    "role": "assistant",
    "content": "",
    "_partial": True,
    "timestamp": 5,
    "reasoning": "the model was still thinking",
}
EMPTY_PARTIAL_TOOLS = {
    "role": "assistant",
    "content": "",
    "_partial": True,
    "timestamp": 6,
    "_partial_tool_calls": [{"name": "terminal", "args": {"command": "ls"}}],
}
BLANK_PARTIAL = {"role": "assistant", "content": " \n\t", "_partial": True, "timestamp": 7}
TEXT_PARTIAL = {
    "role": "assistant",
    "content": "Python is a high-level",
    "_partial": True,
    "timestamp": 8,
}
FOLLOW_UP = {"role": "user", "content": "second question", "timestamp": 9}


class _JsonResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, _limit=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class _SseResponse:
    def __iter__(self):
        return iter([b'data: {"event":"run.completed","output":"ok","usage":{}}\n', b"\n"])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _gateway_history(context_messages, *, tag: str) -> list[dict]:
    """The ``conversation_history`` the runs-API bridge posts for this session."""
    from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_runs_api_streaming

    stream_id = f"sid-8034-{tag}"
    bodies = []

    def fake_urlopen(req, *, timeout=None):
        if req.full_url.endswith("/v1/runs"):
            bodies.append(json.loads(req.data.decode("utf-8")))
            return _JsonResponse({"run_id": f"run-8034-{tag}"})
        return _SseResponse()

    STREAM_PARTIAL_TEXT[stream_id] = ""
    STREAM_REASONING_TEXT[stream_id] = ""
    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _run_gateway_runs_api_streaming(
                session_id=f"sess-8034-{tag}",
                msg_text="next turn",
                model="test-model",
                workspace="/tmp",
                stream_id=stream_id,
                base_url="http://gw:8642",
                api_key="secret",
                prefill_messages=[],
                body_extras={},
                put_gateway_event=lambda event, data: None,
                cancel_event=threading.Event(),
                session=SimpleNamespace(context_messages=context_messages, profile=None),
            )
    finally:
        STREAM_PARTIAL_TEXT.pop(stream_id, None)
        STREAM_REASONING_TEXT.pop(stream_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)
    assert len(bodies) == 1
    return bodies[0].get("conversation_history", [])


def _pairs(history) -> list[tuple[str, object]]:
    return [(row["role"], row["content"]) for row in history]


@pytest.mark.parametrize("row", [ERROR_ROW, CANCEL_ROW], ids=["provider-error", "cancel-marker"])
def test_an_error_row_is_not_sent_to_the_gateway(row):
    history = _gateway_history([USER, ANSWER, row, FOLLOW_UP], tag="error")

    assert _pairs(history) == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
    ]


@pytest.mark.parametrize(
    "row",
    [EMPTY_PARTIAL_REASONING, EMPTY_PARTIAL_TOOLS, BLANK_PARTIAL],
    ids=["reasoning-only", "tool-only", "whitespace"],
)
def test_an_empty_partial_is_not_sent_to_the_gateway(row):
    history = _gateway_history([USER, row, FOLLOW_UP], tag="empty-partial")

    assert _pairs(history) == [("user", "first question"), ("user", "second question")]
    assert all(str(entry["content"]).strip() for entry in history)


def test_a_partial_with_text_is_still_sent():
    """The model continues from the cut-off point (#893)."""
    history = _gateway_history([USER, TEXT_PARTIAL, FOLLOW_UP], tag="text-partial")

    assert _pairs(history) == [
        ("user", "first question"),
        ("assistant", "Python is a high-level"),
        ("user", "second question"),
    ]


def test_ordinary_rows_are_sent_as_before():
    """The control: nothing here is an error marker or a partial."""
    history = _gateway_history([USER, ANSWER, FOLLOW_UP], tag="control")

    assert _pairs(history) == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
    ]


def test_error_rows_and_partials_fare_the_same_on_both_backends():
    """One history of ordinary rows, error markers and partials: the user and
    assistant turns the Gateway is sent are the ones the legacy sanitizer
    keeps. Parity for these rows only; reasoning-only and ``_recovered`` rows,
    which the sanitizer also drops, are not in it."""
    from api.streaming import _sanitize_messages_for_api

    rows = [
        USER,
        ANSWER,
        ERROR_ROW,
        EMPTY_PARTIAL_REASONING,
        TEXT_PARTIAL,
        CANCEL_ROW,
        EMPTY_PARTIAL_TOOLS,
        BLANK_PARTIAL,
        FOLLOW_UP,
    ]

    legacy = [
        (row["role"], row["content"])
        for row in _sanitize_messages_for_api(rows)
        if row.get("role") in {"user", "assistant"}
    ]

    assert _pairs(_gateway_history(rows, tag="parity")) == legacy
    assert legacy == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("assistant", "Python is a high-level"),
        ("user", "second question"),
    ]


def test_the_filter_leaves_every_other_row_alone():
    """Empty content alone is not the test: a completed tool-call turn has
    none, and the legacy path keeps it."""
    from api.streaming import _is_non_replayable_history_row, _sanitize_messages_for_api

    tool_call_turn = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
        ],
    }
    tool_result = {"role": "tool", "tool_call_id": "call_1", "content": "ok"}

    assert not _is_non_replayable_history_row(tool_call_turn)
    assert not _is_non_replayable_history_row({"role": "user", "content": ""})
    assert not _is_non_replayable_history_row(USER)
    assert not _is_non_replayable_history_row(TEXT_PARTIAL)
    assert not _is_non_replayable_history_row("not a row")
    assert [row["role"] for row in _sanitize_messages_for_api([USER, tool_call_turn, tool_result])] == [
        "user",
        "assistant",
        "tool",
    ]
