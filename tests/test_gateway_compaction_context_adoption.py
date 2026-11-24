"""The Gateway runs path keeps the context the Agent compacted mid-turn.

``/v1/runs`` uses a caller-supplied ``conversation_history`` as-is. When the
Agent compacts the conversation in place during a turn, its committed result
lives only in ``state.db``; the WebUI success writeback kept appending to its
own pre-compaction ``context_messages``, so every later turn re-sent the whole
history, was rejected for size, and was compacted again.

Each test drives the real ``_run_gateway_chat_streaming`` against the fake
``/v1/runs`` gateway of ``test_gateway_cancel_reconciliation`` and a real
``state.db`` holding the rows an in-place compaction leaves active.
"""
from __future__ import annotations

import os
import sqlite3
import time
from unittest.mock import patch

import pytest

import api.config as cfg
import api.models as models
from api.models import get_session
from tests.test_gateway_cancel_reconciliation import (
    CAPTURED_BODIES,
    GATEWAY_STORE,
    fake_urlopen,
    history_rows,
    make_session,
)

SUMMARY = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below.\n"
    "The user asked two questions and both were answered."
)
OLD_CONTEXT = [
    {"role": "user", "content": "old question one", "timestamp": 10.0},
    {"role": "assistant", "content": "old answer one", "timestamp": 11.0},
    {"role": "user", "content": "old question two", "timestamp": 12.0},
    {"role": "assistant", "content": "old answer two", "timestamp": 13.0},
]


@pytest.fixture(autouse=True)
def _clean_gateway_state():
    del CAPTURED_BODIES[:]
    GATEWAY_STORE.clear()
    yield
    with cfg.STREAMS_LOCK:
        for key in ("STREAMS", "CANCEL_FLAGS", "STREAM_PARTIAL_TEXT", "STREAM_REASONING_TEXT",
                    "STREAM_LIVE_TOOL_CALLS", "STREAM_LAST_EVENT_ID"):
            getattr(cfg, key).clear()
    cfg.ACTIVE_RUNS.clear()


def _write_agent_rows(session_id, rows):
    """Replace the session's state.db rows; each row is (role, content, timestamp, active)."""
    db_path = models._active_state_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL, "
            "active INTEGER NOT NULL DEFAULT 1)"
        )
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, ?, ?, ?, ?)",
            [(session_id, role, content, ts, active) for role, content, ts, active in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _start_turn(session, stream_id, prompt):
    session.active_stream_id = stream_id
    session.pending_user_message = prompt
    session.pending_started_at = time.time()
    session.pending_attachments = []
    session.pending_user_source = "webui"
    session.save()
    return session.pending_started_at


def _run_turn(session_id, prompt, stream_id):
    import api.gateway_chat as gateway_chat

    with cfg.STREAMS_LOCK:
        cfg.STREAMS[stream_id] = cfg.create_stream_channel()
    env = {"HERMES_WEBUI_CHAT_BACKEND": "gateway", "HERMES_WEBUI_GATEWAY_USE_RUNS_API": "1"}
    with patch.dict(os.environ, env), \
            patch.object(gateway_chat, "gateway_supports_approval", lambda *a, **k: True), \
            patch.object(gateway_chat.urllib.request, "urlopen", side_effect=fake_urlopen):
        gateway_chat._run_gateway_chat_streaming(session_id, prompt, "test-model", "/tmp", stream_id, [])


def _texts(rows):
    return [row["content"] for row in rows]


def _compacted_rows(started, prompt, *, summary_at=None):
    """What an in-place compaction leaves in state.db: archived old rows, then the live set."""
    summary_at = started + 1 if summary_at is None else summary_at
    return [
        ("user", "old question one", 10.0, 0),
        ("assistant", "old answer one", 11.0, 0),
        ("user", "old question two", 12.0, 0),
        ("assistant", "old answer two", 13.0, 0),
        ("user", SUMMARY, summary_at, 1),
        ("assistant", "old answer two", summary_at, 1),
        ("user", prompt, summary_at, 1),
        ("assistant", "turn answer", summary_at + 1, 1),
    ]


def test_context_follows_the_agents_in_place_compaction():
    session = make_session("compaction-adopt", OLD_CONTEXT)
    started = _start_turn(session, "stream-adopt-1", "new question")
    _write_agent_rows(session.session_id, _compacted_rows(started, "new question"))

    _run_turn(session.session_id, "new question", "stream-adopt-1")

    saved = get_session(session.session_id)
    assert _texts(saved.context_messages) == [SUMMARY, "old answer two", "new question", "turn answer"]
    # The visible transcript keeps every turn and never shows the summary.
    assert _texts(saved.messages) == [
        "old question one", "old answer one", "old question two", "old answer two",
        "new question", "turn answer",
    ]


def test_the_next_turn_sends_the_compacted_history_not_the_old_one():
    session = make_session("compaction-next", OLD_CONTEXT)
    started = _start_turn(session, "stream-next-1", "new question")
    _write_agent_rows(session.session_id, _compacted_rows(started, "new question"))
    _run_turn(session.session_id, "new question", "stream-next-1")

    session = get_session(session.session_id)
    _start_turn(session, "stream-next-2", "follow up")
    _run_turn(session.session_id, "follow up", "stream-next-2")

    assert history_rows(CAPTURED_BODIES[-1]) == [
        ("user", SUMMARY),
        ("assistant", "old answer two"),
        ("user", "new question"),
        ("assistant", "turn answer"),
    ]
    saved = get_session(session.session_id)
    # Adopted rows are model context only: no summary and no second copy of a kept turn.
    shown = _texts(saved.messages)
    assert SUMMARY not in shown
    assert shown.count("old answer two") == 1
    assert shown[:6] == [
        "old question one", "old answer one", "old question two", "old answer two",
        "new question", "turn answer",
    ]
    assert "follow up" in shown


def test_a_prompt_the_agent_folded_into_the_previous_user_row_is_split_off():
    session = make_session("compaction-folded", OLD_CONTEXT)
    started = _start_turn(session, "stream-fold-1", "new question")
    _write_agent_rows(session.session_id, [
        ("user", SUMMARY, started + 1, 1),
        ("assistant", "old answer two", started + 1, 1),
        ("user", "a delegated result\n\nnew question", started + 1, 1),
        ("assistant", "turn answer", started + 2, 1),
    ])

    _run_turn(session.session_id, "new question", "stream-fold-1")

    saved = get_session(session.session_id)
    assert _texts(saved.context_messages) == [
        SUMMARY, "old answer two", "a delegated result", "new question", "turn answer",
    ]


@pytest.mark.parametrize(
    "rows_for",
    [
        # A compaction from before this turn: the WebUI context already reflects it, or the
        # user changed it since (truncate, undo), so the stored rows must not replace it.
        lambda started: _compacted_rows(started, "new question", summary_at=started - 60),
        # No compaction at all.
        lambda started: [
            ("user", "old question one", 10.0, 1),
            ("assistant", "old answer one", 11.0, 1),
            ("user", "new question", started + 1, 1),
            ("assistant", "turn answer", started + 2, 1),
        ],
        # A compaction, but this turn's prompt is not in the stored rows.
        lambda started: [
            ("user", SUMMARY, started + 1, 1),
            ("assistant", "old answer two", started + 1, 1),
        ],
        # The compacted rows are archived, not active.
        lambda started: [(role, text, ts, 0) for role, text, ts, _ in _compacted_rows(started, "new question")],
    ],
    ids=["older-compaction", "no-compaction", "prompt-missing", "archived-only"],
)
def test_the_context_is_left_alone_without_a_compaction_from_this_turn(rows_for):
    session = make_session("compaction-none", OLD_CONTEXT)
    started = _start_turn(session, "stream-none-1", "new question")
    _write_agent_rows(session.session_id, rows_for(started))

    _run_turn(session.session_id, "new question", "stream-none-1")

    saved = get_session(session.session_id)
    assert _texts(saved.context_messages) == [
        "old question one", "old answer one", "old question two", "old answer two",
        "new question", "turn answer",
    ]


def test_the_context_is_left_alone_when_state_db_is_missing():
    session = make_session("compaction-nodb", OLD_CONTEXT)
    _start_turn(session, "stream-nodb-1", "new question")

    _run_turn(session.session_id, "new question", "stream-nodb-1")

    saved = get_session(session.session_id)
    assert _texts(saved.context_messages) == [
        "old question one", "old answer one", "old question two", "old answer two",
        "new question", "turn answer",
    ]


# Native producer shapes: the real Agent compressor assembles the compacted set and the
# store's messages.compact (SQLite archive_and_compact) commits it, as the Agent does mid-turn.
NATIVE_SUMMARY = "[CONTEXT COMPACTION — REFERENCE ONLY] The user asked q0 and q1; both were answered."
NATIVE_OLD = [
    {"role": "user", "content": "q0"}, {"role": "assistant", "content": "a0"},
    {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
    {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"},
]


def _native_compaction(session_id, history, prompt, compress_start, compress_end, prompt_content=None):
    """Commit what the Agent writes when it compacts *history* + *prompt* mid-turn."""
    from agent.context_compressor import ContextCompressor, _HandoffScan
    from persistence.conversation.messages import NewMessage
    from persistence.conversation.sessions import NewSession
    from persistence_assembly import open_persistence, profile_scope

    messages = [dict(row) for row in history] + [{"role": "user", "content": prompt_content or prompt}]
    with patch("agent.context_compressor.get_model_context_length", return_value=1_000_000):
        compressor = ContextCompressor(model="test/model", quiet_mode=True)
    scan = _HandoffScan(
        turns_to_summarize=messages[compress_start:compress_end], summary_indices=set(),
        tail_start=compress_end, previous_summary_before=None, has_user_turn_before=None,
    )
    compacted = compressor._assemble_compressed(messages, compress_start, compress_end, scan, NATIVE_SUMMARY)
    db_path = models._active_state_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open_persistence(profile_scope(home=db_path.parent), eager=True) as store:
        store.sessions.create(NewSession(session_id=session_id, source="api_server"))
        for row in messages:
            store.messages.append(session_id, NewMessage.from_live(row))
        store.messages.compact(session_id, [NewMessage.from_live(row) for row in compacted])
        store.messages.append(session_id, NewMessage.from_live({"role": "assistant", "content": "turn answer"}))
    return compacted


def _run_native_turns(sid, history, compress_start, compress_end, prompt_content=None):
    pytest.importorskip("agent.context_compressor", reason="hermes-agent modules not importable")
    pytest.importorskip("persistence_assembly", reason="hermes-agent persistence not importable")
    session = make_session(sid, [row for row in history if row["role"] != "system"])
    _start_turn(session, f"{sid}-1", "live ask")
    compacted = _native_compaction(session.session_id, history, "live ask", compress_start, compress_end, prompt_content)
    _run_turn(session.session_id, "live ask", f"{sid}-1")
    session = get_session(session.session_id)
    _start_turn(session, f"{sid}-2", "follow up")
    _run_turn(session.session_id, "follow up", f"{sid}-2")
    return compacted, get_session(session.session_id)


@pytest.mark.requires_agent_modules
def test_a_summary_merged_into_a_retained_user_row_never_reaches_the_display():
    # Head [q0, a0] ends in an assistant, so the summary folds into the retained user q2.
    compacted, saved = _run_native_turns("native-merged", NATIVE_OLD, 2, 4)
    carrier = compacted[2]["content"]
    assert NATIVE_SUMMARY in carrier and carrier.startswith("[PRIOR CONTEXT")

    assert history_rows(CAPTURED_BODIES[-1]) == [
        ("user", "q0"), ("assistant", "a0"), ("user", carrier), ("assistant", "a2"),
        ("user", "live ask"), ("assistant", "turn answer"),
    ]
    # The fake Gateway repeats "turn answer", so the display merge keeps one copy.
    assert _texts(saved.messages) == ["q0", "a0", "q1", "a1", "q2", "a2", "live ask", "turn answer", "follow up"]


@pytest.mark.requires_agent_modules
def test_a_summary_carrier_holding_the_live_ask_is_adopted_without_the_old_history():
    # A system-only head: the summary leads the retained current user row, then the live ask.
    history = [{"role": "system", "content": "sys"}] + NATIVE_OLD[:4]
    compacted, saved = _run_native_turns("native-carrier", history, 1, 5)
    carrier = compacted[-1]["content"]
    assert carrier.startswith(NATIVE_SUMMARY) and carrier.endswith("live ask")
    summary_part = carrier[: -len("live ask")].rstrip()

    sent = history_rows(CAPTURED_BODIES[-1])
    assert ("user", "q0") not in sent and ("user", "q1") not in sent
    assert [text for _, text in sent if text != compacted[0]["content"]] == [
        summary_part, "live ask", "turn answer",
    ]
    assert _texts(saved.messages) == ["q0", "a0", "q1", "a1", "live ask", "turn answer", "follow up"]


IMAGE_PART = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}


@pytest.mark.requires_agent_modules
@pytest.mark.parametrize("structured", [False, True], ids=["plain-string", "text-plus-image"])
def test_a_structured_summary_carrier_is_split_from_the_live_ask(structured):
    history = [{"role": "system", "content": "sys"}] + NATIVE_OLD[:4]
    prompt_content = [{"type": "text", "text": "live ask"}, IMAGE_PART] if structured else None
    compacted, saved = _run_native_turns(f"native-split-{structured}", history, 1, 5, prompt_content)
    carrier = compacted[-1]["content"]
    if structured:
        # The compressor prepends the summary as its own text part ahead of the ask and the image.
        assert [part.get("type") for part in carrier] == ["text", "text", "image_url"]
        summary_part = carrier[0]["text"].rstrip()
    else:
        summary_part = carrier[: -len("live ask")].rstrip()
    # Adopted rows are plain text: the Gateway's history parser reads str(content).
    expected_carrier = summary_part
    assert summary_part.startswith(NATIVE_SUMMARY)

    sent = [row for row in CAPTURED_BODIES[-1]["conversation_history"] if row.get("role") != "system"]
    contents = [row.get("content") for row in sent]
    assert not {"q0", "a0", "q1", "a1"} & {c for c in contents if isinstance(c, str)}
    assert contents == [expected_carrier, "live ask", "turn answer"]
    assert _texts(saved.messages) == ["q0", "a0", "q1", "a1", "live ask", "turn answer", "follow up"]


def _received_history(body):
    """What the Gateway's /v1/runs admission parser makes of a request body's history."""
    pytest.importorskip("aiohttp", reason="the Gateway's HTTP runtime is not installed")
    runs = pytest.importorskip("gateway.platforms.api_server_runs", reason="hermes-agent gateway not importable")
    history, _, _, error = runs._resolve_conversation_history(
        None, body, body.get("input"), _openai_error=lambda *a, **k: ("error", a),
    )
    assert error is None
    return history


# Native persistence -> WebUI settlement/adoption -> next request -> the receiving parser.
# The request body alone does not prove meaning: the receiver reads each row as str(content).
@pytest.mark.requires_agent_modules
@pytest.mark.parametrize(
    "case",
    ["merged-carrier", "summary-carrier", "text-plus-image-carrier"],
)
def test_the_receiving_gateway_reads_adopted_history_as_the_compacted_text(case):
    pytest.importorskip("aiohttp", reason="the Gateway's HTTP runtime is not installed")
    pytest.importorskip("gateway.platforms.api_server_runs", reason="hermes-agent gateway not importable")
    sid = f"receiver-{case}"
    if case == "merged-carrier":
        compacted, _ = _run_native_turns(sid, NATIVE_OLD, 2, 4)
        summary_text = compacted[2]["content"]
        expected = [("user", "q0"), ("assistant", "a0"), ("user", summary_text), ("assistant", "a2")]
    else:
        history = [{"role": "system", "content": "sys"}] + NATIVE_OLD[:4]
        prompt_content = [{"type": "text", "text": "live ask"}, IMAGE_PART] if case.startswith("text-plus") else None
        compacted, _ = _run_native_turns(sid, history, 1, 5, prompt_content)
        carrier = compacted[-1]["content"]
        summary_text = (carrier[0]["text"] if isinstance(carrier, list) else carrier[: -len("live ask")]).rstrip()
        expected = [("user", summary_text)]
    expected += [("user", "live ask"), ("assistant", "turn answer")]

    received = _received_history(CAPTURED_BODIES[-1])
    pairs = [(row["role"], row["content"]) for row in received if row["role"] != "system"]
    assert pairs == expected
    for _, content in pairs:
        # No stringified part list (Python repr) and no inline media reach the model.
        assert not content.startswith("[{") and "image_url" not in content and "base64" not in content
    assert NATIVE_SUMMARY in summary_text


# Adoption -> display edit -> next send: the adopted rows no longer align row-for-row with
# the display, so a display selection must not be read as a count of compacted rows.
def _adopted_session(sid):
    session = make_session(sid, OLD_CONTEXT)
    started = _start_turn(session, f"{sid}-1", "new question")
    _write_agent_rows(session.session_id, _compacted_rows(started, "new question"))
    _run_turn(session.session_id, "new question", f"{sid}-1")
    return get_session(session.session_id)


def _plain_session(sid):
    session = make_session(sid, OLD_CONTEXT)
    _start_turn(session, f"{sid}-1", "new question")
    _run_turn(session.session_id, "new question", f"{sid}-1")
    return get_session(session.session_id)


def _post(path, body):
    import json
    from types import SimpleNamespace

    import api.routes as routes
    from tests.test_session_truncate_keep_count_validation import _JSONHandler

    handler = _JSONHandler(json.dumps(body).encode())
    with patch.object(routes, "_check_csrf", lambda handler: True):
        routes.handle_post(handler, SimpleNamespace(path=path))
    return handler.status, handler.payload()


def _send_next(sid, prompt="next ask"):
    session = get_session(sid)
    _start_turn(session, f"{sid}-next", prompt)
    _run_turn(sid, prompt, f"{sid}-next")
    return history_rows(CAPTURED_BODIES[-1])


ORIGINAL_ROWS = [
    ("user", "old question one"), ("assistant", "old answer one"),
    ("user", "old question two"), ("assistant", "old answer two"),
]
MAKERS = pytest.mark.parametrize("make", ["compacted", "no-compaction"])


def _make(kind, sid):
    return (_adopted_session if kind == "compacted" else _plain_session)(f"{sid}-{kind}")


@MAKERS
@pytest.mark.parametrize("keep", [0, 2, 4])
def test_truncate_after_adoption_sends_the_selected_display_prefix(make, keep):
    sid = _make(make, f"adopt-trunc-{keep}").session_id
    status, payload = _post("/api/session/truncate", {"session_id": sid, "keep_count": keep})
    assert status == 200 and payload["ok"] is True
    assert _texts(get_session(sid).messages) == [text for _, text in ORIGINAL_ROWS[:keep]]

    # keep=0 is a deliberate reset: the Gateway gets the empty-history sentinel, never stored turns.
    expected = ORIGINAL_ROWS[:keep] or [("user", "No earlier conversation turns.")]
    assert _send_next(sid) == expected


@MAKERS
def test_truncate_keeping_the_adopted_turn_keeps_the_compacted_context(make):
    sid = _make(make, "adopt-keep").session_id
    status, _ = _post("/api/session/truncate", {"session_id": sid, "keep_count": 6})
    assert status == 200
    sent = _send_next(sid)
    if make == "compacted":
        assert sent == [("user", SUMMARY), ("assistant", "old answer two"),
                        ("user", "new question"), ("assistant", "turn answer")]
    else:
        assert sent == ORIGINAL_ROWS + [("user", "new question"), ("assistant", "turn answer")]


@MAKERS
def test_undo_twice_after_adoption_sends_the_remaining_display_prefix(make):
    from api.session_ops import undo_last

    sid = _make(make, "adopt-undo").session_id
    undo_last(sid)
    assert _texts(get_session(sid).messages) == [text for _, text in ORIGINAL_ROWS]
    undo_last(sid)
    assert _texts(get_session(sid).messages) == [text for _, text in ORIGINAL_ROWS[:2]]
    assert _send_next(sid) == ORIGINAL_ROWS[:2]


@MAKERS
def test_retry_after_adoption_resends_from_the_display_prefix(make):
    from api.session_ops import retry_last

    sid = _make(make, "adopt-retry").session_id
    retry_last(sid)
    # Retrying the compacted turn itself selects exactly the rows the summary stands for.
    assert _send_next(sid, "new question") == ORIGINAL_ROWS


@pytest.mark.parametrize("keep", [2, 4])
def test_fork_after_adoption_carries_the_selected_display_prefix(keep):
    from api.session_ops import truncate_context_for_display_keep

    saved = _adopted_session(f"adopt-fork-{keep}")
    forked = truncate_context_for_display_keep(saved.context_messages, saved.messages, keep)
    assert [(row["role"], row["content"]) for row in forked] == ORIGINAL_ROWS[:keep]


# Source-lead discriminators (gate 7022665452).
def test_a_prompt_that_merely_looks_like_a_marker_is_not_a_compaction():
    marker_prompt = "[CONTEXT COMPACTION — REFERENCE ONLY] is this header text special?"
    session = make_session("lead-marker", OLD_CONTEXT)
    started = _start_turn(session, "lead-marker-1", marker_prompt)
    _write_agent_rows(session.session_id, [
        ("user", "old question one", 10.0, 1), ("assistant", "old answer one", 11.0, 1),
        ("user", "old question two", 12.0, 1), ("assistant", "old answer two", 13.0, 1),
        ("user", marker_prompt, started + 1, 1), ("assistant", "turn answer", started + 2, 1),
    ])
    _run_turn(session.session_id, marker_prompt, "lead-marker-1")
    saved = get_session(session.session_id)
    assert not any(row.get("_gateway_compaction_adopted") for row in saved.context_messages)


def test_a_repeated_prompt_cuts_at_the_live_ask_not_an_earlier_copy():
    session = make_session("lead-same-text", OLD_CONTEXT)
    started = _start_turn(session, "lead-same-1", "continue")
    _write_agent_rows(session.session_id, [
        ("user", SUMMARY, started + 1, 1), ("assistant", "ok", started + 1, 1),
        ("user", "continue", started + 1, 1), ("assistant", "first continuation", started + 1, 1),
        ("user", "continue", started + 1, 1), ("assistant", "turn answer", started + 2, 1),
    ])
    _run_turn(session.session_id, "continue", "lead-same-1")
    assert _texts(get_session(session.session_id).context_messages) == [
        SUMMARY, "ok", "continue", "first continuation", "continue", "turn answer",
    ]


PREFILL = [{"role": "user", "content": "recall: notes"}, {"role": "assistant", "content": "noted"}]


def test_prefill_rows_the_compaction_kept_are_not_carried_into_the_next_send():
    import api.streaming as streaming

    loaded = {"status": "loaded", "source": "file", "messages": [dict(r) for r in PREFILL],
              "message_count": 2}
    with patch.object(streaming, "_load_webui_prefill_context", lambda cfg=None: dict(loaded)):
        session = make_session("lead-prefill", OLD_CONTEXT)
        started = _start_turn(session, "lead-prefill-1", "new question")
        # The Agent compacts the list it was given: history + prefill + prompt.
        _write_agent_rows(session.session_id, [
            ("user", SUMMARY, started + 1, 1), ("assistant", "old answer two", started + 1, 1),
            ("user", "recall: notes", started + 1, 1), ("assistant", "noted", started + 1, 1),
            ("user", "new question", started + 1, 1), ("assistant", "turn answer", started + 2, 1),
        ])
        _run_turn(session.session_id, "new question", "lead-prefill-1")
        _send_next(session.session_id, "follow up")
    sent = history_rows(CAPTURED_BODIES[-1])
    assert sent.count(("user", "recall: notes")) == 1
    assert sent == [
        ("user", SUMMARY), ("assistant", "old answer two"),
        ("user", "new question"), ("assistant", "turn answer"),
        ("user", "recall: notes"), ("assistant", "noted"),
    ]


def test_a_turn_without_compaction_adds_no_full_history_read():
    import api.models as models_mod

    calls = []
    real = models_mod.get_state_db_session_messages

    def spy(*args, **kwargs):
        calls.append(kwargs.get("since_timestamp"))
        return real(*args, **kwargs)

    session = make_session("lead-read", OLD_CONTEXT)
    started = _start_turn(session, "lead-read-1", "new question")
    _write_agent_rows(session.session_id, [
        ("user", "old question one", 10.0, 1), ("assistant", "old answer one", 11.0, 1),
        ("user", "new question", started + 1, 1), ("assistant", "turn answer", started + 2, 1),
    ])
    with patch.object(models_mod, "get_state_db_session_messages", spy):
        _run_turn(session.session_id, "new question", "lead-read-1")
    # Base already does exactly one full read per turn (pre-existing); adoption adds
    # only a since-turn-start probe and no second full-history read.
    assert calls.count(None) == 1
    assert [c for c in calls if c is not None] == [started]
