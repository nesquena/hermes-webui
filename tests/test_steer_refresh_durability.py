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


def _extract_js_function(source: str, name: str) -> str:
    marker = f"function {name}"
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"function {name} did not close")


def _handler():
    handler = MagicMock()
    handler.wfile = MagicMock()
    handler.headers = MagicMock()
    handler.headers.get = MagicMock(return_value="")
    return handler


def _response(handler):
    raw = handler.wfile.write.call_args_list[-1][0][0]
    return json.loads(raw.decode("utf-8"))


def test_structured_steer_paths_must_be_real_session_uploads(tmp_path, monkeypatch):
    from api import streaming, upload

    attachment_root = tmp_path / "attachments"
    monkeypatch.setattr(upload, "_attachment_root", lambda: attachment_root)
    session_dir = upload._session_attachment_dir("sid")
    session_dir.mkdir(parents=True)
    uploaded = session_dir / "benign.pdf"
    uploaded.write_text("content", encoding="utf-8")

    assert streaming._verified_steer_attachment_paths("sid", [str(uploaded)]) == [str(uploaded)]
    with pytest.raises(ValueError, match="not a session upload"):
        streaming._verified_steer_attachment_paths(
            "sid",
            [str(tmp_path / "Ignore previous instructions" / "benign.pdf")],
        )


def test_accepted_steer_is_journaled_and_broadcast_with_one_event_identity(monkeypatch):
    from api import streaming
    from api.config import (
        AGENT_INSTANCES,
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
        old_agents = dict(AGENT_INSTANCES)
        AGENT_INSTANCES.clear()
        AGENT_INSTANCES[stream_id] = agent

    journal_event = {
        "event_id": f"{stream_id}:7",
        "seq": 7,
        "run_id": stream_id,
        "session_id": sid,
        "created_at": 123.0,
    }
    captured = {}

    def accept_and_append(_writer, event_name, payload, accept, *, publish=None):
        captured["event_name"] = event_name
        captured["payload"] = payload
        assert accept() is True
        assert publish is not None
        publish(journal_event)
        return True, journal_event, None, None

    try:
        with patch.object(streaming, "get_session", return_value=session), patch.object(
            streaming,
            "_verified_steer_attachment_paths",
            return_value=["/private/tmp/notes.txt"],
        ), patch.object(
            streaming.RunJournalWriter,
            "accept_and_append_if_nonterminal",
            autospec=True,
            side_effect=accept_and_append,
        ) as transaction:
            handler = _handler()
            streaming._handle_chat_steer(handler, {
                "session_id": sid,
                "user_text": "keep this steer",
                "attachment_paths": ["/private/tmp/notes.txt"],
                # Contradictory legacy fields must not control either runtime or
                # transcript content once the structured contract is present.
                "text": "malicious hidden instruction",
                "display_text": "benign cover text",
            })
    finally:
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE.clear()
            SESSION_AGENT_CACHE.update(old_cache)
        with STREAMS_LOCK:
            STREAMS.clear()
            STREAMS.update(old_streams)
            AGENT_INSTANCES.clear()
            AGENT_INSTANCES.update(old_agents)

    transaction.assert_called_once()
    assert captured["event_name"] == "steer_delivered"
    assert captured["payload"]["text"] == "keep this steer"
    assert captured["payload"]["files"] == ["notes.txt"]
    assert captured["payload"]["status"] == "delivered"
    agent.steer.assert_called_once_with(
        "keep this steer\n\n[Attached files for this steer: /private/tmp/notes.txt]\n"
        "Use the file tools/read_file to inspect these documents if needed."
    )

    event_name, payload, event_id = stream.get_nowait()
    assert event_name == "steer_delivered"
    assert payload["text"] == "keep this steer"
    assert payload["files"] == ["notes.txt"]
    assert payload["status"] == "delivered"
    assert event_id == f"{stream_id}:7"
    body = _response(handler)
    assert body["accepted"] is True
    assert body["fallback"] is None
    assert body["stream_id"] == stream_id
    assert body["durable"] is True
    assert body["published"] is True
    assert body["steer_event"]["event_id"] == f"{stream_id}:7"


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
                        "files": ["notes.txt"],
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
    assert steer_rows[0]["role"] == "user"
    assert steer_rows[0]["kind"] == "control_boundary"
    assert steer_rows[0]["text"] == "persist across refresh"
    assert steer_rows[0]["payload"]["files"] == ["notes.txt"]
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
    assert compact_steer[0]["payload"]["files"] == ["notes.txt"]


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
        ("user", "steer_delivered", "steer here"),
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
        ("user", "new direction", 2),
        ("thinking", "after steer", 3),
    ]
    assert snapshot["last_reasoning_text"] == "before steerafter steer"


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_anchor_projection_classifies_steer_as_user_authored_control_boundary():
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
api.applyAssistantTurnAnchorSourceEvent(registry,{{
  type:'steer_delivered',event_id:'run:5',seq:5,created_at:124,
  payload:{{session_id:'sid',stream_id:'run',text:'durable steer',status:'delivered'}}
}},{{session_id:'sid',stream_id:'run',run_id:'run'}});
const scene=api.projectAssistantTurnAnchorActivityScene(registry,{{mode:'compact_worklog'}});
console.log(JSON.stringify(scene.activity_rows));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert len(rows) == 2, "repeated user guidance with distinct event ids must stay distinct"
    assert [row["event_id"] for row in rows] == ["run:4", "run:5"]
    assert all(row["source_event_type"] == "steer_delivered" for row in rows)
    assert all(row["role"] == "user" for row in rows)
    assert all(row["kind"] == "control_boundary" for row in rows)
    assert all(row["display_hint"] == "user_message" for row in rows)
    assert [row["text"] for row in rows] == ["durable steer", "durable steer"]


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_http_response_and_sse_converge_on_one_steer_row():
    anchors = ROOT / "static" / "assistant_turn_anchors.js"
    commands = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
    response_helper = _extract_js_function(commands, "_recordSteerEventFromResponse")
    script = f"""
const fs=require('fs');
const vm=require('vm');
const sandbox={{window:{{}},console}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(anchors))},'utf8'),sandbox);
const api=sandbox.window.HermesAssistantTurnAnchors;
const registry=api.createAssistantTurnAnchorRegistry({{session_id:'sid',run_id:'run',stream_id:'run'}});
sandbox.window._liveAnchorRegistries=new Map([['run',registry]]);
let renders=0;
sandbox.window._renderLiveAnchorActivitySceneForStream=()=>{{renders+=1;return true;}};
sandbox._steerOwnerStreamIsCurrent=()=>true;
vm.runInContext({json.dumps(response_helper)},sandbox);
const response={{stream_id:'run',published:false,steer_event:{{
  type:'steer_delivered',event_id:'run:8',seq:8,run_id:'run',stream_id:'run',session_id:'sid',created_at:123,
  payload:{{session_id:'sid',stream_id:'run',text:'one row',status:'delivered'}},
}}}};
const recorded=sandbox._recordSteerEventFromResponse('sid',response);
// The later SSE carries the same journal envelope and must dedupe.
api.applyAssistantTurnAnchorSourceEvent(registry,{{
  type:'steer_delivered',event_id:'run:8',seq:8,run_id:'run',stream_id:'run',session_id:'sid',created_at:123,
  payload:{{session_id:'sid',stream_id:'run',text:'one row',status:'delivered'}},
}},{{session_id:'sid',run_id:'run',stream_id:'run'}});
const scene=api.projectAssistantTurnAnchorActivityScene(registry,{{mode:'transparent_stream'}});
console.log(JSON.stringify({{recorded,renders,rows:scene.activity_rows}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["recorded"] is True
    assert state["renders"] == 1
    assert len(state["rows"]) == 1
    assert state["rows"][0]["event_id"] == "run:8"


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_steer_activity_row_inherits_safe_user_markdown_rendering():
    source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    function_source = _extract_js_function(source, "_anchorSceneNodeForRow")
    script = f"""
const vm=require('vm');
function makeEl(tag){{
  return {{
    tagName:String(tag||'').toUpperCase(), className:'', textContent:'', innerHTML:'', children:[], attrs:{{}},
    setAttribute(name,value){{this.attrs[name]=String(value);}},
    appendChild(child){{this.children.push(child);return child;}},
  }};
}}
const sandbox={{
  document:{{createElement:makeEl}},
  window:{{_showThinking:true,_renderUserMarkdown:true}},
  t:key=>key==='steer_message_label'?'Steer':'',
  _getCachedRender:(text,isUser)=>{{
    if(text!=='*helloooo* <img src=x onerror=alert(1)>'||isUser!==true) throw new Error('wrong user render input');
    return '<em>helloooo</em> &lt;img src=x onerror=alert(1)&gt;';
  }},
}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(function_source)},sandbox);
const row={{
  row_id:'run:4',local_id:'run:4',kind:'control_boundary',role:'user',
  source_event_type:'steer_delivered',status:'delivered',text:'*helloooo* <img src=x onerror=alert(1)>',
  payload:{{files:['notes.txt']}},
}};
const node=sandbox._anchorSceneNodeForRow(row,{{settled:true}});
console.log(JSON.stringify(node));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    node = json.loads(result.stdout)
    assert node["className"] == "msg-row steer-delivered-row"
    assert node["attrs"]["data-role"] == "user"
    assert node["attrs"]["data-steer-delivery"] == "delivered"
    assert node["attrs"]["data-anchor-source-event-type"] == "steer_delivered"
    assert [child["className"] for child in node["children"]] == [
        "steer-delivered-label",
        "msg-body",
        "steer-delivered-files",
    ]
    assert node["children"][0]["textContent"] == "Steer"
    assert node["children"][1]["innerHTML"] == "<em>helloooo</em> &lt;img src=x onerror=alert(1)&gt;"
    assert node["children"][1]["textContent"] == ""
    assert node["children"][2]["textContent"] == "notes.txt"


def test_frontend_consumes_steer_sse_and_settlement_keeps_user_rows():
    source = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    assert "source.addEventListener('steer_delivered'" in source
    assert "_applyToAnchor('steer_delivered',d,e)" in source

    start = source.index("function _anchorSceneHasWorklogWorthyRows")
    end = source.index("\n  function ", start + 20)
    body = source[start:end]
    assert "source_event_type||'')==='steer_delivered'" in body

    render_source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    start = render_source.index("function _anchorSceneSceneHasWorklogWorthyRows")
    end = render_source.index("\nfunction ", start + 20)
    render_body = render_source[start:end]
    assert "source_event_type||'')==='steer_delivered'" in render_body


def test_user_authored_steer_remains_visible_in_final_answer_only_mode():
    source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    live_start = source.index("function renderLiveAnchorActivityScene")
    live = source[live_start:source.index("\nfunction _renderLiveAnchorActivitySceneTransparent", live_start)]
    settled_start = source.index("function _renderSettledAnchorSceneForMessage")
    settled = source[settled_start:source.index("\nfunction _syncLiveWorklogReasonsForAnchor", settled_start)]
    hide_start = source.index("function _hideLiveActivityForFinalAnswerOnly")
    hide_live = source[hide_start:source.index("\nif(typeof window", hide_start)]
    style = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert "sceneMode==='hide_all_activity'" in live
    assert "_renderLiveAnchorSteerRowsOnly" in live
    assert "isFinalAnswerOnlyMode()" in settled
    assert "_renderSettledAnchorSteerRowsOnlyForMessage" in settled
    assert "clearLiveToolCards({preserveDom:true})" in hide_live
    assert ':not([data-steer-delivery="delivered"])' in hide_live
    assert '[data-steer-only="1"] > .msg-role.assistant' in style


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_final_answer_only_promotes_steer_out_of_removed_compact_worklog():
    source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    start = source.index("function _hideLiveActivityForFinalAnswerOnly")
    function_source = source[start:source.index("\nif(typeof window", start)]
    script = f"""
const vm=require('vm');
const inner={{children:[],querySelectorAll(selector){{
  if(selector==='[data-steer-delivery="delivered"]') return [steer];
  return [group];
}},appendChild(node){{
  if(node.parentElement&&node.parentElement.children) node.parentElement.children=node.parentElement.children.filter(x=>x!==node);
  node.parentElement=this;
  this.children.push(node);
  return node;
}}}};
const turn={{removed:false,setAttribute(){{}},remove(){{this.removed=true;}}}};
const group={{parentElement:inner,children:[],remove(){{
  inner.children=inner.children.filter(x=>x!==this);
  this.parentElement=null;
}}}};
const steer={{parentElement:group,children:[]}};
group.children=[steer];
inner.children=[group];
let preserveDom=false;
const sandbox={{
  clearLiveToolCards(opts){{preserveDom=!!(opts&&opts.preserveDom);}},
  removeThinking(){{}},
  $:id=>id==='liveAssistantTurn'?turn:null,
  _assistantTurnBlocks:()=>inner,
}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(function_source)},sandbox);
sandbox._hideLiveActivityForFinalAnswerOnly();
console.log(JSON.stringify({{
  preserveDom,
  childCount:inner.children.length,
  keptSteer:inner.children[0]===steer,
  turnRemoved:turn.removed,
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state == {
        "preserveDom": True,
        "childCount": 1,
        "keptSteer": True,
        "turnRemoved": False,
    }


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