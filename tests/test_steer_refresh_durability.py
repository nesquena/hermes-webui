"""Regression coverage for durable mid-run Steer timeline events."""
from __future__ import annotations

import json
import queue
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api import run_journal

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _handler():
    handler = MagicMock()
    handler.wfile = MagicMock()
    handler.headers = MagicMock()
    handler.headers.get = MagicMock(return_value="")
    return handler


def _response(handler):
    raw = handler.wfile.write.call_args_list[-1][0][0]
    return json.loads(raw.decode("utf-8"))


def test_accepted_steer_is_journaled_and_broadcast_with_one_event_identity(monkeypatch):
    from api import streaming
    from api.config import (
        SESSION_AGENT_CACHE,
        SESSION_AGENT_CACHE_LOCK,
        STREAMS,
        STREAMS_LOCK,
    )

    sid = "steer_refresh_sid"
    stream_id = "steer_refresh_run"
    agent = MagicMock()
    agent.steer.return_value = True
    stream = queue.Queue()
    session = MagicMock(active_stream_id=stream_id)

    with SESSION_AGENT_CACHE_LOCK:
        old_cache = dict(SESSION_AGENT_CACHE)
        SESSION_AGENT_CACHE.clear()
        SESSION_AGENT_CACHE[sid] = (agent, "sig")
    with STREAMS_LOCK:
        old_streams = dict(STREAMS)
        STREAMS.clear()
        STREAMS[stream_id] = stream

    journal_event = {
        "event_id": f"{stream_id}:7",
        "seq": 7,
        "run_id": stream_id,
        "session_id": sid,
        "created_at": 123.0,
    }
    captured = {}

    def accept_and_append(_writer, event_name, payload, accept):
        captured["event_name"] = event_name
        captured["payload"] = payload
        assert accept() is True
        return True, journal_event, None, None

    try:
        with patch.object(streaming, "get_session", return_value=session), patch.object(
            streaming.RunJournalWriter,
            "accept_and_append_if_nonterminal",
            autospec=True,
            side_effect=accept_and_append,
        ) as transaction:
            handler = _handler()
            streaming._handle_chat_steer(handler, {"session_id": sid, "text": "keep this steer"})
    finally:
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE.clear()
            SESSION_AGENT_CACHE.update(old_cache)
        with STREAMS_LOCK:
            STREAMS.clear()
            STREAMS.update(old_streams)

    transaction.assert_called_once()
    assert captured["event_name"] == "steer_delivered"
    assert captured["payload"]["text"] == "keep this steer"
    assert captured["payload"]["status"] == "delivered"

    event_name, payload, event_id = stream.get_nowait()
    assert event_name == "steer_delivered"
    assert payload["text"] == "keep this steer"
    assert payload["status"] == "delivered"
    assert event_id == f"{stream_id}:7"
    body = _response(handler)
    assert body["accepted"] is True
    assert body == {"accepted": True, "fallback": None, "stream_id": stream_id}


def test_run_journal_snapshot_rebuilds_delivered_steer_row(monkeypatch):
    from api import routes

    sid = "snapshot_steer_sid"
    stream_id = "snapshot_steer_run"
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": sid,
            "run_id": stream_id,
            "last_seq": 3,
            "last_event_id": f"{stream_id}:3",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _sid, _run: {
            "events": [
                {
                    "event": "steer_delivered",
                    "type": "steer_delivered",
                    "event_id": f"{stream_id}:3",
                    "seq": 3,
                    "run_id": stream_id,
                    "session_id": sid,
                    "created_at": 123.0,
                    "payload": {
                        "session_id": sid,
                        "stream_id": stream_id,
                        "text": "persist across refresh",
                        "status": "delivered",
                    },
                }
            ]
        },
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    assert snapshot is not None
    rows = snapshot["anchor_activity_scene"]["activity_rows"]
    steer_rows = [row for row in rows if row.get("source_event_type") == "steer_delivered"]
    assert len(steer_rows) == 1
    assert steer_rows[0]["role"] == "control"
    assert steer_rows[0]["kind"] == "control_boundary"
    assert steer_rows[0]["text"] == "persist across refresh"
    assert steer_rows[0]["event_id"] == f"{stream_id}:3"

    # The session endpoint sends the compact transport projection, not the raw
    # recovery snapshot.  Identity must survive that final consumer too or a
    # refresh cannot dedupe the replayed row against the live SSE event.
    compact = routes._runtime_journal_snapshot_for_session_payload(snapshot)
    compact_rows = compact["anchor_activity_scene"]["activity_rows"]
    compact_steer = [
        row for row in compact_rows
        if row.get("source_event_type") == "steer_delivered"
    ]
    assert len(compact_steer) == 1
    assert compact_steer[0]["event_id"] == f"{stream_id}:3"
    assert compact_steer[0]["run_id"] == stream_id
    assert compact_steer[0]["stream_id"] == stream_id
    assert compact_steer[0]["seq"] == 3


def test_run_journal_snapshot_keeps_steer_between_later_activity(monkeypatch):
    from api import routes

    sid = "snapshot_steer_order_sid"
    stream_id = "snapshot_steer_order_run"
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": sid,
            "run_id": stream_id,
            "last_seq": 7,
            "last_event_id": f"{stream_id}:7",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _sid, _run: {
            "events": [
                {"event": "token", "event_id": f"{stream_id}:1", "seq": 1,
                 "run_id": stream_id, "session_id": sid, "payload": {"text": "before"}},
                {"event": "reasoning", "event_id": f"{stream_id}:2", "seq": 2,
                 "run_id": stream_id, "session_id": sid, "payload": {"text": "earlier thought"}},
                {"event": "steer_delivered", "event_id": f"{stream_id}:3", "seq": 3,
                 "run_id": stream_id, "session_id": sid,
                 "payload": {"text": "steer here", "status": "delivered"}},
                {"event": "tool", "event_id": f"{stream_id}:4", "seq": 4,
                 "run_id": stream_id, "session_id": sid,
                 "payload": {"name": "terminal", "id": "call-1", "args": {"command": "pwd"}}},
                {"event": "tool_complete", "event_id": f"{stream_id}:5", "seq": 5,
                 "run_id": stream_id, "session_id": sid,
                 "payload": {"name": "terminal", "id": "call-1", "preview": "ok"}},
                {"event": "reasoning", "event_id": f"{stream_id}:6", "seq": 6,
                 "run_id": stream_id, "session_id": sid, "payload": {"text": "later thought"}},
                {"event": "token", "event_id": f"{stream_id}:7", "seq": 7,
                 "run_id": stream_id, "session_id": sid, "payload": {"text": "after"}},
            ]
        },
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    rows = snapshot["anchor_activity_scene"]["activity_rows"]
    visible = [(row["role"], row.get("source_event_type"), row.get("text")) for row in rows]
    assert visible == [
        ("prose", "token", "before"),
        ("thinking", "reasoning", "earlier thought"),
        ("control", "steer_delivered", "steer here"),
        ("tool", "tool_complete", "ok"),
        ("thinking", "reasoning", "later thought"),
        ("prose", "token", "after"),
    ]
    assert [row["order_index"] for row in rows] == list(range(len(rows)))
    assert [row["seq"] for row in rows] == [1, 2, 3, 4, 6, 7]


def test_run_journal_snapshot_splits_reasoning_across_steer_boundary(monkeypatch):
    from api import routes

    sid = "snapshot_reasoning_split_sid"
    stream_id = "snapshot_reasoning_split_run"
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": sid,
            "run_id": stream_id,
            "last_seq": 3,
            "last_event_id": f"{stream_id}:3",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _sid, _run: {
            "events": [
                {"event": "reasoning", "event_id": f"{stream_id}:1", "seq": 1,
                 "run_id": stream_id, "session_id": sid,
                 "payload": {"text": "before steer"}},
                {"event": "steer_delivered", "event_id": f"{stream_id}:2", "seq": 2,
                 "run_id": stream_id, "session_id": sid,
                 "payload": {"text": "new direction", "status": "delivered"}},
                {"event": "reasoning", "event_id": f"{stream_id}:3", "seq": 3,
                 "run_id": stream_id, "session_id": sid,
                 "payload": {"text": "after steer"}},
            ]
        },
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    rows = snapshot["anchor_activity_scene"]["activity_rows"]
    assert [(row["role"], row["text"], row["seq"]) for row in rows] == [
        ("thinking", "before steer", 1),
        ("control", "new direction", 2),
        ("thinking", "after steer", 3),
    ]
    assert snapshot["last_reasoning_text"] == "before steerafter steer"


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_anchor_projection_classifies_steer_as_durable_control_boundary():
    anchors = ROOT / "static" / "assistant_turn_anchors.js"
    script = f"""
const fs=require('fs');
const vm=require('vm');
const sandbox={{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(anchors))},'utf8'),sandbox);
const api=sandbox.window.HermesAssistantTurnAnchors;
const registry=api.createAssistantTurnAnchorRegistry({{session_id:'sid',stream_id:'run',run_id:'run'}});
api.applyAssistantTurnAnchorSourceEvent(registry,{{
  type:'steer_delivered',event_id:'run:4',seq:4,created_at:123,
  payload:{{session_id:'sid',stream_id:'run',text:'durable steer',status:'delivered'}}
}},{{session_id:'sid',stream_id:'run',run_id:'run'}});
const scene=api.projectAssistantTurnAnchorActivityScene(registry,{{mode:'compact_worklog'}});
console.log(JSON.stringify(scene.activity_rows));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["source_event_type"] == "steer_delivered"
    assert rows[0]["role"] == "control"
    assert rows[0]["kind"] == "control_boundary"
    assert rows[0]["text"] == "durable steer"


def test_frontend_consumes_steer_sse_and_settlement_keeps_control_rows():
    source = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    assert "source.addEventListener('steer_delivered'" in source
    assert "_applyToAnchor('steer_delivered',d,e)" in source

    start = source.index("function _anchorSceneHasWorklogWorthyRows")
    end = source.index("\n  function ", start + 20)
    body = source[start:end]
    assert "role==='control'" in body


def test_terminal_journal_prevents_runtime_acceptance(tmp_path):
    writer = run_journal.RunJournalWriter(
        "steer_terminal_sid",
        "steer_terminal_run",
        session_dir=tmp_path,
    )
    run_journal.append_run_event(
        "steer_terminal_sid",
        "steer_terminal_run",
        "done",
        {"session": {}},
        session_dir=tmp_path,
    )
    called = []

    accepted, event, reason, error = writer.accept_and_append_if_nonterminal(
        "steer_delivered",
        {"text": "too late"},
        lambda: called.append(True) or True,
    )

    assert (accepted, event, reason, error) == (False, None, "terminal", None)
    assert called == []


def test_accept_and_delivery_append_precede_concurrent_terminal(tmp_path):
    import threading

    writer = run_journal.RunJournalWriter(
        "steer_order_sid",
        "steer_order_run",
        session_dir=tmp_path,
    )
    started = threading.Event()
    finished = threading.Event()

    def append_terminal():
        started.set()
        run_journal.append_run_event(
            "steer_order_sid",
            "steer_order_run",
            "done",
            {"session": {}},
            session_dir=tmp_path,
        )
        finished.set()

    def accept():
        threading.Thread(target=append_terminal).start()
        assert started.wait(timeout=10)
        assert not finished.wait(timeout=0.1)
        return True

    accepted, event, reason, error = writer.accept_and_append_if_nonterminal(
        "steer_delivered",
        {"text": "in time"},
        accept,
    )
    assert accepted is True and event is not None and reason is None and error is None
    assert finished.wait(timeout=10)
    journal = run_journal.read_run_events(
        "steer_order_sid",
        "steer_order_run",
        session_dir=tmp_path,
    )
    assert [item["event"] for item in journal["events"]] == ["steer_delivered", "done"]