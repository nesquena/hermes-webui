"""Gate-certification regression (#7167, 2026-11-18 RED finding 1).

The legacy tool-progress producer emits ``args`` but no ``tid`` and settles its
live cards **newest-first**. Replaying such a journal:

    start A(args=A), start B(args=B), complete B(args=B), complete A(args=A)

the journal's ``complete`` order is not the ``start`` order, so matching an
id-less completion to the first unconsumed same-name card (FIFO) attaches each
command's result to the other command's card. Ownership must come from the
command arguments when they are present; FIFO is only a fallback for journals
that carry no arguments at all.
"""

from __future__ import annotations

import pytest

import api.models as models
import api.profiles as profiles
from api.models import Session
from api.run_journal import RunJournalWriter, append_run_event


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME + the WebUI state dir so journal writes sandbox."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(models, "SESSION_DIR", home / "sessions")
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", home / "sessions" / "_index.json"
    )
    return home


def test_idless_legacy_completions_match_by_command_arguments_not_fifo(hermes_home):
    sid = "gate_legacy_completion_ownership"
    stream_id = "stream-gate-legacy-ownership"

    append_run_event(
        sid, stream_id, "tool",
        {"name": "terminal", "args": {"cmd": "echo A"}, "preview": "echo A"},
    )
    append_run_event(
        sid, stream_id, "tool",
        {"name": "terminal", "args": {"cmd": "echo B"}, "preview": "echo B"},
    )
    # Legacy producer settles newest-first: B's completion is journaled first.
    append_run_event(
        sid, stream_id, "tool_complete",
        {"name": "terminal", "args": {"cmd": "echo B"}, "preview": "result B"},
    )
    append_run_event(
        sid, stream_id, "tool_complete",
        {"name": "terminal", "args": {"cmd": "echo A"}, "preview": "result A"},
    )

    session = Session(
        session_id=sid,
        title="gate legacy completion ownership",
        messages=[
            {"role": "user", "content": "go", "timestamp": 900.5},
            {
                "role": "assistant",
                "content": "",
                "timestamp": 900.6,
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": stream_id,
            },
        ],
    )

    from api.models import _append_journaled_partial_output

    _appended, accounted = _append_journaled_partial_output(
        session, stream_id, dedupe_existing=True
    )
    assert accounted is True, "the journal's tool output must be accounted for"

    cards = session.tool_calls
    assert len(cards) == 2, [c.get("args") for c in cards]
    by_cmd = {}
    for card in cards:
        cmd = str((card.get("args") or {}).get("cmd") or "")
        by_cmd[cmd] = card
    assert set(by_cmd) == {"echo A", "echo B"}, by_cmd
    assert by_cmd["echo A"]["snippet"] == "result A", by_cmd["echo A"]
    assert by_cmd["echo B"]["snippet"] == "result B", by_cmd["echo B"]
    assert by_cmd["echo A"]["done"] is True
    assert by_cmd["echo B"]["done"] is True


@pytest.mark.parametrize(
    ("saved_start", "saved_done"),
    [(2, False), (1, True), (2, True)],
    ids=["newest-reused-unfinished", "older-reused-done", "newest-reused-done"],
)
def test_tagged_idless_completion_matches_live_settlement_after_reload(
    hermes_home, saved_start, saved_done,
):
    """The legacy producer's event type survives the journal and fixes the order."""
    sid = "gate_marked_legacy_tool_settlement"
    stream_id = "stream-gate-marked-legacy"
    writer = RunJournalWriter(sid, stream_id)
    for label in ("A", "B"):
        writer.append_sse_event("tool", {
            "event_type": "tool.started", "name": "terminal",
            "args": {}, "preview": f"start {label}",
        })
    for label in ("B", "A"):
        writer.append_sse_event("tool_complete", {
            "event_type": "tool.completed", "name": "terminal", "args": {},
            "preview": f"result {label}", "duration": 1.0 if label == "A" else 2.0,
            "is_error": label == "B",
        })

    session = Session(
        session_id=sid,
        title="legacy tool settlement",
        messages=[
            {"role": "user", "content": "Run both tools", "timestamp": 900.5},
            {"role": "assistant", "content": "", "timestamp": 900.6,
             "_recovered_from_run_journal": True, "_recovered_stream_id": stream_id},
        ],
    )
    saved_label = "A" if saved_start == 1 else "B"
    saved_preview = f"persisted {saved_label}" if saved_done else f"start {saved_label}"
    session.tool_calls = [{
        "name": "terminal", "preview": saved_preview, "snippet": saved_preview,
        "tid": "", "assistant_msg_idx": 1, "done": saved_done,
        "_recovered_from_run_journal": True, "_recovered_stream_id": stream_id,
        "_recovered_event_id": f"{stream_id}:{saved_start}",
    }]
    if saved_done:
        session.tool_calls[0].update(duration=5.0, is_error=False)

    expected = {
        f"{stream_id}:1": ("result A", 1.0, False),
        f"{stream_id}:2": ("result B", 2.0, True),
    }
    if saved_done:
        expected[f"{stream_id}:{saved_start}"] = (saved_preview, 5.0, False)

    # Exercise both recovery and its retry against a cold-loaded sidecar.
    for _ in range(2):
        models._append_journaled_partial_output(session, stream_id, dedupe_existing=True)
        session.save()
        models.SESSIONS.pop(sid, None)
        session = Session.load(sid)
        assert session is not None
        assert len(session.tool_calls) == 2
        assert all(card["done"] for card in session.tool_calls)
        assert {
            card["_recovered_event_id"]: (card["preview"], card["duration"], card["is_error"])
            for card in session.tool_calls
        } == expected
