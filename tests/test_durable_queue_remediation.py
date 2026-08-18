"""Discriminating regressions for the durable-queue remediation contract."""

from __future__ import annotations

import copy
import io
import json
import re
import shutil
import subprocess
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import models, routes
from api.models import Session

from tests.test_server_queue_durability import (
    _JSONHandler,
    _disable_enqueue_drain,
    _install_start_stubs,
    _new_session,
    _payload,
    _queue_action,
    _setup,
    _uploaded_file,
)


ROOT = Path(__file__).resolve().parents[1]


def test_local_settlement_preserves_full_attachment_metadata():
    source = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    start = source.index("# Preserve the accepted attachment metadata")
    end = source.index("# Persist reasoning trace", start)
    block = source[start:end]

    assert "copy.deepcopy(list(attachments))" in block
    assert "_attachment_name" not in block


def _mark_pending_compression_transfer(child, parent) -> None:
    child.parent_session_id = parent.session_id
    child.queue_transfer = {
        "version": 1,
        "state": "pending",
        "parent_session_id": parent.session_id,
        "child_session_id": child.session_id,
        "item_ids": [
            str(item.get("id"))
            for item in (child.queue or [])
            if isinstance(item, dict) and str(item.get("id") or "")
        ],
        "clear_generation": child.clear_generation,
    }


def _js_function(source: str, name: str) -> str:
    for marker in (f"async function {name}(", f"function {name}("):
        start = source.find(marker)
        if start >= 0:
            break
    else:
        raise AssertionError(f"{name} not found")
    parens = 0
    brace = -1
    for index in range(source.find("(", start), len(source)):
        char = source[index]
        if char == "(":
            parens += 1
        elif char == ")":
            parens -= 1
        elif char == "{" and parens == 0:
            brace = index
            break
    if brace < 0:
        raise AssertionError(f"body for {name} not found")
    depth = 1
    pos = brace + 1
    while depth:
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos]


def _busy_send_node_result(
    messages_source: str,
    commands_source: str,
    texts: list[str],
    *,
    record_all_handlers: bool = False,
    files: list[dict] | None = None,
    report_remaining_files: bool = False,
):
    command_start = commands_source.index("const COMMANDS=[")
    command_end = commands_source.index("];", command_start) + 2
    command_registry = commands_source[command_start:command_end]
    handler_names = sorted(set(re.findall(r"fn:([A-Za-z_$][\w$]*)", command_registry)))
    handler_stubs = []
    for name in handler_names:
        if name == "cmdModel":
            handler_stubs.append(
                "function cmdModel(args){handled.push({name:'model',args:String(args||'')});return true;}"
            )
        elif name == "cmdClear":
            handler_stubs.append(
                "function cmdClear(args){handled.push({name:'clear',args:String(args||'')});return true;}"
            )
        elif record_all_handlers:
            handler_stubs.append(
                f"function {name}(args){{handled.push({{name:'{name[3:].lower()}',args:String(args||'')}});return true;}}"
            )
        else:
            handler_stubs.append(f"function {name}(){{return true;}}")
    helpers = []
    for name in (
        "_slashCommandMatch",
        "_echoSlashUserMessage",
        "_finishSlashCommand",
        "_runBuiltinSlashCommand",
        "_slashPromptIntent",
        "_prepareSlashTurn",
        "_queuePayloadForSlashTurn",
    ):
        marker = f"async function {name}(" if f"async function {name}(" in messages_source else f"function {name}("
        if marker in messages_source:
            helpers.append(_js_function(messages_source, name))
    script = f"""
    function t(key){{return key;}}
    const handled=[];
    const queued=[];
    const input={{value:''}};
    const S={{busy:true,pendingFiles:{json.dumps(files if files is not None else [])},session:{{session_id:'sid',model:'model',model_provider:'provider'}},activeStreamId:'stream',messages:[],toolCalls:[]}};
    const reportRemainingFiles={str(report_remaining_files).lower()};
    const window={{_defaultMessageMode:'queue'}};
    const document={{querySelector:()=>null}};
    function $(id){{return id==='msg'?input:null;}}
    function parseCommand(text){{if(!text.startsWith('/'))return null;const parts=text.slice(1).split(/\\s+/);return {{name:parts[0].toLowerCase(),args:parts.slice(1).join(' ').trim()}};}}
    function queueSessionMessage(sid,payload){{queued.push({{sid,payload}});return Promise.resolve({{accepted:true}});}}
    function getAgentCommandMetadata(){{return Promise.resolve(null);}}
    function getBundleCommandMetadata(){{return Promise.resolve(null);}}
    function api(url){{return Promise.resolve(String(url).includes('moa/resolve')?{{ok:true}}:{{}});}}
    function _composerTextWithPendingSelections(){{return input.value;}}
    function _flushSelectionBlocksToComposer(){{}}
    function _chatPayloadModelState(){{return {{model:'model',model_provider:'provider'}};}}
    function _clearStaleBusyStateBeforeSend(){{}}
    function isCompressionUiRunning(){{return false;}}
    function _dismissHandoffHint(){{}}
    function setComposerStatus(){{}}
    function setStatus(){{}}
    function updateSendBtn(){{}}
    function autoResize(){{}}
    function hideCmdDropdown(){{}}
    function showToast(){{}}
    function _clearComposerAfterQueuedSelectionSend(){{}}
    function renderMessages(){{}}
    function clearLiveToolCards(){{}}
    function ensureLiveWorklogShell(){{}}
    function appendThinking(){{}}
    function syncTopbar(){{}}
    function renderSessionList(){{}}
    let _sendInProgress=false;
    let _sendInProgressSid=null;
    const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();
    {chr(10).join(handler_stubs)}
    {command_registry}
    {''.join(helpers)}
    {_js_function(messages_source, 'send')}
    async function run(text){{
      input.value=text;
      handled.length=0;
      queued.length=0;
      await send();
      const result={{handled:[...handled],queued:queued.map(entry=>entry.payload)}};
      if(reportRemainingFiles) result.remainingFiles=S.pendingFiles.map(file=>({{...file}}));
      return result;
    }}
    Promise.resolve().then(async()=>{{
      const results=[];
      for(const text of {json.dumps(texts)}) results.push(await run(text));
      console.log(JSON.stringify(results));
    }});
    """
    return _run_node(script)


def _run_node(script: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    args = [node, "-"] if len(script) > 100_000 else [node, "-e", script]
    result = subprocess.run(
        args,
        input=script if args[-1] == "-" else None,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_claimed_head_uses_its_typed_intent_and_direct_tail_keeps_its_own(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    head = {
        "id": "head-intent",
        "text": "/moa head prompt",
        "display_text": "head display",
        "files": [{"name": "head.txt", "path": "/head.txt"}],
        "model": "head-model",
        "model_provider": "head-provider",
        "source": "webui_queue",
        "workspace": str(tmp_path / "head-workspace"),
        "moa_config": {"members": ["head"]},
        "goal_related": False,
        "goal_state": {"goal_id": "head-goal"},
        "recovery": {"clear": False},
        "dispatch": {"backend": "local", "queue_item_id": "head-intent"},
        "intent": {
            "version": 1,
            "raw_text": "/moa head prompt",
            "display_text": "head display",
            "agent_text": "head prompt",
            "attachments": [{"name": "head.txt", "path": "/head.txt"}],
            "model": "head-model",
            "model_provider": "head-provider",
            "workspace": str(tmp_path / "head-workspace"),
            "source": "webui_queue",
            "command": {"name": "moa", "args": "head prompt"},
            "moa_config": {"members": ["head"]},
            "goal_related": False,
            "goal_state": {"goal_id": "head-goal"},
            "recovery": {"clear": False},
            "dispatch": {"backend": "local", "queue_item_id": "head-intent"},
        },
    }
    session = _new_session("queue-typed-intent", tmp_path, queue=[head])
    worker = _install_start_stubs(monkeypatch)

    response = routes._start_chat_stream_for_session(
        session,
        msg="direct tail",
        attachments=[{"name": "tail.txt", "path": "/tail.txt"}],
        workspace=str(tmp_path / "tail-workspace"),
        model="tail-model",
        model_provider="tail-provider",
        source="webui",
        display_text="direct tail",
        goal_related=True,
        moa_config={"members": ["wrong"]},
        clear_recovery=True,
        external_runtime_owned=False,
    )

    assert response["queue_item_id"] == "head-intent"
    assert worker.starts[0][2]["goal_related"] is False
    assert worker.starts[0][2]["moa_config"] == {"members": ["head"]}
    assert worker.starts[0][1][3] == str(tmp_path / "head-workspace")
    persisted = models.Session.load(session.session_id)
    tail = persisted.queue[0]
    assert tail["intent"]["model"] == "tail-model"
    assert tail["intent"]["model_provider"] == "tail-provider"
    assert tail["intent"]["workspace"] == str(tmp_path / "tail-workspace")
    assert tail["intent"]["attachments"] == [{"name": "tail.txt", "path": "/tail.txt"}]
    assert tail["intent"]["goal_related"] is True


def test_slash_command_is_normalized_once_when_enqueued_and_drained(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    real_drain = routes.drain_queued_session_turn
    _disable_enqueue_drain(monkeypatch)
    session = _new_session("queue-command-intent", tmp_path)
    status, accepted = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "/moa inspect the report",
            "model": "queued-model",
            "model_provider": "queued-provider",
        }
    )
    assert status == 200
    item = accepted["item"]
    assert item["intent"]["raw_text"] == "/moa inspect the report"
    assert item["intent"]["command"]["name"] == "moa"
    assert item["intent"]["agent_text"] == "inspect the report"

    worker = _install_start_stubs(monkeypatch)
    monkeypatch.setattr(routes, "drain_queued_session_turn", real_drain)
    response = routes.drain_queued_session_turn(session.session_id)
    assert response["stream_id"]
    assert not worker.starts[0][1][1].lstrip().startswith("/")
    assert worker.starts[0][1][1] == "inspect the report"


def test_typed_bundle_turn_is_recomputed_before_queue_admission(monkeypatch, tmp_path):
    from api import commands

    _setup(monkeypatch, tmp_path)
    real_drain = routes.drain_queued_session_turn
    _disable_enqueue_drain(monkeypatch)
    monkeypatch.setattr(
        commands,
        "resolve_bundle_command",
        lambda _raw: {"name": "research", "message": "resolved report invocation"},
    )
    session = _new_session("queue-bundle-intent", tmp_path)
    raw_text = "/research inspect the report"
    typed_intent = {
        "version": 1,
        "raw_text": raw_text,
        "display_text": raw_text,
        "agent_text": "untrusted client text",
        "attachments": [],
        "command": {
            "name": "research",
            "args": "inspect the report",
            "dispatch": "prompt_transform",
            "transform": "bundle",
        },
    }

    status, accepted = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "resolved report invocation",
            "display_text": raw_text,
            "intent": typed_intent,
        }
    )
    assert status == 200
    assert accepted["item"]["intent"]["raw_text"] == raw_text
    assert accepted["item"]["intent"]["agent_text"] == "resolved report invocation"

    worker = _install_start_stubs(monkeypatch)
    monkeypatch.setattr(routes, "drain_queued_session_turn", real_drain)
    response = routes.drain_queued_session_turn(session.session_id)
    assert response["stream_id"]
    assert worker.starts[0][1][1] == "resolved report invocation"


def test_unprepared_bundle_is_rejected_before_enqueue_and_edit_save(monkeypatch, tmp_path):
    from api import commands

    _setup(monkeypatch, tmp_path)
    _disable_enqueue_drain(monkeypatch)
    monkeypatch.setattr(commands, "list_command_bundles", lambda: [{"name": "research"}])
    session = _new_session("unprepared-bundle-queue", tmp_path)

    status, response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "/research inspect",
        }
    )
    assert 400 <= status < 500, response
    assert Session.load(session.session_id).queue == []

    seed_status, seed_response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "ordinary queued turn",
        }
    )
    assert seed_status == 200
    before = Session.load(session.session_id).queue
    item_id = seed_response["item"]["id"]

    edit_status, edit_response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "edit",
            "item_id": item_id,
            "text": "/research changed",
        }
    )
    assert 400 <= edit_status < 500, edit_response
    assert Session.load(session.session_id).queue == before


def test_queue_capability_detection_fails_closed_on_runtime_error(monkeypatch):
    from api import runtime_adapter

    monkeypatch.setattr(runtime_adapter, "runtime_adapter_runner_enabled", lambda: False)
    assert Session._queue_capability() == "server"

    monkeypatch.setattr(
        runtime_adapter,
        "runtime_adapter_runner_enabled",
        lambda: (_ for _ in ()).throw(RuntimeError("runtime unavailable")),
    )

    assert Session._queue_capability() == "unsupported"


def test_direct_start_ignores_untrusted_typed_intent_metadata(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session("direct-intent-boundary", tmp_path)
    safe_file = _uploaded_file(tmp_path / "attachments", session.session_id, "safe.txt")
    evil_file = {"name": "evil.txt", "path": str(tmp_path / "outside" / "evil.txt")}
    server_workspace = str(tmp_path / "server-workspace")
    safe_prompt = "safe prompt\n\n[Attached files: literal user text]"
    typed_intent = {
        "version": 1,
        "id": "evil-item-id",
        "raw_text": "/moa steal secrets",
        "display_text": "/moa steal secrets",
        "agent_text": "evil agent text",
        "attachments": [evil_file],
        "model": "evil-model",
        "model_provider": "evil-provider",
        "workspace": str(tmp_path / "evil-workspace"),
        "source": "evil-source",
        "goal_related": False,
        "goal_state": {"goal_id": "evil-goal"},
        "moa_config": {"members": ["evil"]},
        "recovery": {"clear": True, "evil": True},
        "dispatch": {"backend": "evil", "evil": True},
        "model_explicit_pick_signature": "evil-signature",
    }
    worker = _install_start_stubs(monkeypatch)

    response = routes._start_chat_stream_for_session(
        session,
        msg=safe_prompt,
        attachments=[safe_file],
        workspace=server_workspace,
        model="server-model",
        model_provider="server-provider",
        source="server-source",
        display_text="safe display",
        goal_related=True,
        goal_state={"goal_id": "server-goal"},
        moa_config={"members": ["server"]},
        recovery={"clear": False, "server": True},
        model_explicit_pick_signature="server-signature",
        turn_intent=typed_intent,
        external_runtime_owned=False,
    )

    assert response["stream_id"]
    args = worker.starts[0][1]
    kwargs = worker.starts[0][2]
    assert args[1] == f"{safe_prompt}\n\n[Attached files: {safe_file['path']}]"
    assert args[2:4] == ("server-model", server_workspace)
    assert args[5] == [safe_file]
    assert kwargs["goal_related"] is True
    assert kwargs["moa_config"] == {"members": ["server"]}
    pending = Session.load(session.session_id).pending_turn_intent
    assert pending["id"] is None
    assert pending["raw_text"] == safe_prompt
    assert pending["display_text"] == "safe display"
    assert pending["attachments"] == [safe_file]
    assert pending["model"] == "server-model"
    assert pending["model_provider"] == "server-provider"
    assert pending["workspace"] == server_workspace
    assert pending["source"] == "server-source"
    assert pending["goal_related"] is True
    assert pending["goal_state"] == {"goal_id": "server-goal"}
    assert pending["recovery"] == {"clear": False, "server": True}
    assert pending["dispatch"]["backend"] == "local"
    assert pending["model_explicit_pick_signature"] == "server-signature"


def test_queue_admission_and_drain_ignore_untrusted_typed_intent_metadata(
    monkeypatch, tmp_path
):
    from api import commands

    _setup(monkeypatch, tmp_path)
    real_drain = routes.drain_queued_session_turn
    _disable_enqueue_drain(monkeypatch)
    monkeypatch.setattr(commands, "resolve_moa_config", lambda: {"members": ["server"]})
    session = _new_session("queue-intent-boundary", tmp_path)
    safe_file = _uploaded_file(tmp_path / "attachments", session.session_id, "safe.txt")
    typed_intent = {
        "version": 1,
        "id": "evil-item-id",
        "raw_text": "/model evil-model",
        "display_text": "/model evil-model",
        "agent_text": "evil prompt",
        "attachments": [{"name": "evil.txt", "path": str(tmp_path / "evil.txt")}],
        "model": "evil-model",
        "model_provider": "evil-provider",
        "workspace": str(tmp_path / "evil-workspace"),
        "source": "evil-source",
        "goal_related": True,
        "goal_state": {"goal_id": "evil-goal"},
        "moa_config": {"members": ["evil"]},
        "recovery": {"clear": True, "evil": True},
        "dispatch": {"backend": "evil", "evil": True},
        "model_explicit_pick_signature": "evil-signature",
    }
    status, accepted = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "safe queue prompt",
            "files": [safe_file],
            "model": "server-model",
            "model_provider": "server-provider",
            "goal_related": False,
            "goal_state": {"goal_id": "server-goal"},
            "moa_config": {"members": ["caller-must-not-persist"]},
            "recovery": {"clear": False, "server": True},
            "intent": typed_intent,
        }
    )

    assert status == 200
    item = accepted["item"]
    assert item["id"] != "evil-item-id"
    assert item["intent"]["id"] == item["id"]
    assert item["intent"]["raw_text"] == "safe queue prompt"
    assert item["intent"]["attachments"] == [safe_file]
    assert item["intent"]["model"] == "server-model"
    assert item["intent"]["model_provider"] == "server-provider"
    assert item["intent"]["workspace"] == session.workspace
    assert item["intent"]["source"] == "webui"
    assert item["intent"]["goal_related"] is False
    assert item["intent"]["goal_state"] == {"goal_id": "server-goal"}
    assert item["intent"]["moa_config"] == {"members": ["server"]}
    assert item["intent"]["recovery"] == {"clear": False, "server": True}
    assert item["intent"]["dispatch"] == {"backend": "local"}
    assert item["intent"]["model_explicit_pick_signature"] is None

    worker = _install_start_stubs(monkeypatch)
    monkeypatch.setattr(routes, "drain_queued_session_turn", real_drain)
    response = real_drain(session.session_id)
    assert response["stream_id"]
    assert worker.starts[0][1][1].startswith("safe queue prompt")
    assert worker.starts[0][1][5] == [safe_file]
    assert worker.starts[0][1][2:4] == ("server-model", session.workspace)


def test_queue_admission_persists_validated_captured_workspace(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _disable_enqueue_drain(monkeypatch)
    captured = tmp_path / "captured-workspace"
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: Path(value))
    session = _new_session("queue-captured-workspace", tmp_path)

    status, _response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "run in captured workspace",
            "workspace": str(captured),
        }
    )

    assert status == 200
    persisted = Session.load(session.session_id)
    assert persisted is not None
    assert persisted.queue[0]["workspace"] == str(captured)
    assert persisted.queue[0]["intent"]["workspace"] == str(captured)


def test_server_recomputes_moa_and_bundle_transforms_from_display_text(
    monkeypatch, tmp_path
):
    from api import commands

    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(commands, "resolve_moa_config", lambda: {"members": ["server-moa"]})
    monkeypatch.setattr(
        commands,
        "resolve_bundle_command",
        lambda _raw: {"name": "research", "message": "server bundle invocation"},
    )
    worker = _install_start_stubs(monkeypatch)

    moa_session = _new_session("server-moa-transform", tmp_path)
    moa_response = routes._start_run(
        moa_session,
        msg="inspect",
        attachments=[],
        workspace=str(tmp_path),
        model="server-model",
        model_provider="server-provider",
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
        display_text="/moa inspect",
        turn_intent={
            "version": 1,
            "command": {
                "name": "moa",
                "args": "inspect",
                "dispatch": "prompt_transform",
                "transform": "moa",
            },
            "agent_text": "evil moa text",
        },
        gateway_chat_enabled=False,
    )
    bundle_session = _new_session("server-bundle-transform", tmp_path)
    bundle_response = routes._start_run(
        bundle_session,
        msg="server bundle invocation",
        attachments=[],
        workspace=str(tmp_path),
        model="server-model",
        model_provider="server-provider",
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
        display_text="/research inspect",
        turn_intent={
            "version": 1,
            "command": {
                "name": "research",
                "args": "inspect",
                "dispatch": "prompt_transform",
                "transform": "bundle",
            },
            "agent_text": "evil bundle text",
        },
        gateway_chat_enabled=False,
    )

    assert moa_response["stream_id"]
    assert bundle_response["stream_id"]
    assert worker.starts[0][1][1] == "inspect"
    assert worker.starts[0][2]["moa_config"] == {"members": ["server-moa"]}
    assert worker.starts[1][1][1] == "server bundle invocation"
    assert Session.load(moa_session.session_id).pending_turn_intent["display_text"] == "/moa inspect"
    assert Session.load(bundle_session.session_id).pending_turn_intent["display_text"] == "/research inspect"


@pytest.mark.parametrize(
    ("display_text", "primary_text", "resolver"),
    [
        ("/moa inspect", "inspect", "moa"),
        ("/research inspect", "server bundle invocation", "bundle"),
    ],
)
def test_display_transform_without_versioned_intent_is_rejected(
    monkeypatch, tmp_path, display_text, primary_text, resolver
):
    from api import commands

    _setup(monkeypatch, tmp_path)
    if resolver == "moa":
        monkeypatch.setattr(commands, "resolve_moa_config", lambda: {"members": ["server"]})
    else:
        monkeypatch.setattr(
            commands,
            "list_command_bundles",
            lambda: [{"name": "research"}],
        )
        monkeypatch.setattr(
            commands,
            "resolve_bundle_command",
            lambda _raw: {"name": "research", "message": "server bundle invocation"},
        )
    session = _new_session(f"display-only-{resolver}", tmp_path)
    worker = _install_start_stubs(monkeypatch)

    response = routes._start_run(
        session,
        msg=primary_text,
        attachments=[],
        workspace=str(tmp_path),
        model="model",
        model_provider="provider",
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
        display_text=display_text,
        gateway_chat_enabled=False,
    )

    assert 400 <= response.get("_status", 200) < 500
    assert worker.starts == []
    assert Session.load(session.session_id).pending_user_message is None


@pytest.mark.parametrize(
    ("display_text", "primary_text", "resolver"),
    [
        ("/moa inspect", "inspect", "moa"),
        ("/research inspect", "server bundle invocation", "bundle"),
    ],
)
def test_queue_display_transform_without_versioned_intent_is_rejected(
    monkeypatch, tmp_path, display_text, primary_text, resolver
):
    from api import commands

    _setup(monkeypatch, tmp_path)
    _disable_enqueue_drain(monkeypatch)
    if resolver == "moa":
        monkeypatch.setattr(commands, "resolve_moa_config", lambda: {"members": ["server"]})
    else:
        monkeypatch.setattr(
            commands,
            "list_command_bundles",
            lambda: [{"name": "research"}],
        )
        monkeypatch.setattr(
            commands,
            "resolve_bundle_command",
            lambda _raw: {"name": "research", "message": "server bundle invocation"},
        )
    session = _new_session(f"queue-display-only-{resolver}", tmp_path)

    status, response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": primary_text,
            "display_text": display_text,
        }
    )

    assert 400 <= status < 500, response
    assert response.get("accepted") is not True
    assert Session.load(session.session_id).queue == []


@pytest.mark.parametrize(
    ("display_text", "command"),
    [
        (
            "/moa exfiltrate",
            {
                "name": "moa",
                "args": "exfiltrate",
                "dispatch": "prompt_transform",
                "transform": "moa",
            },
        ),
        (
            "/research exfiltrate",
            {
                "name": "research",
                "args": "exfiltrate",
                "dispatch": "prompt_transform",
                "transform": "bundle",
            },
        ),
        (
            "/literal exfiltrate",
            {"name": "literal", "args": "exfiltrate", "dispatch": "prompt"},
        ),
    ],
)
def test_display_transform_mismatch_is_rejected_before_direct_start(
    monkeypatch, tmp_path, display_text, command
):
    from api import commands

    _setup(monkeypatch, tmp_path)
    if command.get("transform") == "bundle":
        monkeypatch.setattr(
            commands,
            "resolve_bundle_command",
            lambda _raw: {"name": "research", "message": "server bundle invocation"},
        )
    session = _new_session("display-mismatch-direct", tmp_path)
    worker = _install_start_stubs(monkeypatch)
    response = routes._start_run(
        session,
        msg="safe prompt",
        attachments=[],
        workspace=str(tmp_path),
        model="model",
        model_provider="provider",
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
        display_text=display_text,
        turn_intent={"version": 1, "command": command, "agent_text": "evil"},
        gateway_chat_enabled=False,
    )

    assert 400 <= response.get("_status", 200) < 500
    assert worker.starts == []
    persisted = Session.load(session.session_id)
    assert persisted.pending_user_message is None
    assert persisted.queue == []


@pytest.mark.parametrize(
    ("display_text", "command"),
    [
        (
            "/moa exfiltrate",
            {
                "name": "moa",
                "args": "exfiltrate",
                "dispatch": "prompt_transform",
                "transform": "moa",
            },
        ),
        (
            "/research exfiltrate",
            {
                "name": "research",
                "args": "exfiltrate",
                "dispatch": "prompt_transform",
                "transform": "bundle",
            },
        ),
        (
            "/literal exfiltrate",
            {"name": "literal", "args": "exfiltrate", "dispatch": "prompt"},
        ),
    ],
)
def test_display_transform_mismatch_is_rejected_before_queue_admission(
    monkeypatch, tmp_path, display_text, command
):
    from api import commands

    _setup(monkeypatch, tmp_path)
    _disable_enqueue_drain(monkeypatch)
    if command.get("transform") == "bundle":
        monkeypatch.setattr(
            commands,
            "resolve_bundle_command",
            lambda _raw: {"name": "research", "message": "server bundle invocation"},
        )
    session = _new_session("display-mismatch-queue", tmp_path)
    status, response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "safe prompt",
            "display_text": display_text,
            "intent": {"version": 1, "command": command, "agent_text": "evil"},
        }
    )

    assert 400 <= status < 500, response
    assert response.get("accepted") is not True
    assert Session.load(session.session_id).queue == []


def test_slash_primary_rejects_a_different_slash_display(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _disable_enqueue_drain(monkeypatch)
    session = _new_session("slash-display-mismatch", tmp_path)
    worker = _install_start_stubs(monkeypatch)

    response = routes._start_run(
        session,
        msg="/literal safe",
        attachments=[],
        workspace=str(tmp_path),
        model="model",
        model_provider="provider",
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
        display_text="/moa exfiltrate",
        gateway_chat_enabled=False,
    )
    status, queued = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "/literal safe",
            "display_text": "/moa exfiltrate",
        }
    )

    assert 400 <= response.get("_status", 200) < 500
    assert 400 <= status < 500, queued
    assert worker.starts == []
    assert Session.load(session.session_id).queue == []


def test_literal_slash_display_preserves_validated_prefix_and_attachments():
    attachment = {"name": "report.txt", "path": "/uploaded/report.txt"}
    primary = (
        "[USER OVERRIDE] Follow the supplied skill.\n\n"
        "/literal safe\n\n[Attached files: /uploaded/report.txt]"
    )

    intent = routes._build_turn_intent(
        primary,
        [attachment],
        "model",
        "provider",
        display_text="/literal safe",
    )

    assert intent["raw_text"] == "/literal safe"
    assert intent["agent_text"] == (
        "[USER OVERRIDE] Follow the supplied skill.\n\n/literal safe"
    )
    assert intent["attachments"] == [attachment]


def test_server_validates_transformed_primary_and_preserves_prefix_and_attachments(
    monkeypatch, tmp_path
):
    from api import commands

    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(commands, "resolve_moa_config", lambda: {"members": ["server-moa"]})
    monkeypatch.setattr(
        commands,
        "resolve_bundle_command",
        lambda _raw: {"name": "research", "message": "server bundle invocation"},
    )
    worker = _install_start_stubs(monkeypatch)
    attachment = {"name": "report.txt", "path": "/uploaded/report.txt"}
    moa_session = _new_session("display-equivalent-moa", tmp_path)
    moa_primary = "inspect\n\n[Attached files: /uploaded/report.txt]"
    moa_response = routes._start_run(
        moa_session,
        msg=moa_primary,
        attachments=[attachment],
        workspace=str(tmp_path),
        model="model",
        model_provider="provider",
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
        display_text="/moa inspect",
        turn_intent={"version": 1, "command": {"name": "moa", "transform": "moa"}},
        gateway_chat_enabled=False,
    )
    bundle_session = _new_session("display-equivalent-bundle", tmp_path)
    bundle_primary = "[FORCED SKILL CONTEXT: research]\nUse the skill.\n[/FORCED SKILL CONTEXT]\n\nserver bundle invocation\n\n[Attached files: /uploaded/report.txt]"
    bundle_response = routes._start_run(
        bundle_session,
        msg=bundle_primary,
        attachments=[attachment],
        workspace=str(tmp_path),
        model="model",
        model_provider="provider",
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
        display_text="/research inspect",
        turn_intent={
            "version": 1,
            "command": {"name": "research", "transform": "bundle"},
        },
        gateway_chat_enabled=False,
    )

    assert moa_response["stream_id"]
    assert bundle_response["stream_id"]
    assert worker.starts[0][1][1] == moa_primary
    assert worker.starts[1][1][1] == bundle_primary
    assert worker.starts[0][1][5] == [attachment]
    assert worker.starts[1][1][5] == [attachment]
    assert Session.load(moa_session.session_id).pending_turn_intent["display_text"] == "/moa inspect"
    assert Session.load(bundle_session.session_id).pending_turn_intent["display_text"] == "/research inspect"


def test_busy_send_uses_command_planner_for_browser_and_prompt_transform_commands():
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    commands = (ROOT / "static/commands.js").read_text(encoding="utf-8")
    result = _busy_send_node_result(
        messages,
        commands,
        ["/model gpt-5", "/clear", "/moa inspect"],
    )
    assert result[0] == {"handled": [{"name": "model", "args": "gpt-5"}], "queued": []}
    assert result[1] == {"handled": [{"name": "clear", "args": ""}], "queued": []}
    assert result[2]["handled"] == []
    assert len(result[2]["queued"]) == 1
    queued = result[2]["queued"][0]
    assert queued["text"] == "inspect"
    assert queued["display_text"] == "/moa inspect"
    assert queued["intent"]["command"]["name"] == "moa"
    assert queued["intent"]["agent_text"] == "inspect"


def test_busy_slash_planner_preserves_files_for_local_and_prompt_transform_turns():
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    commands = (ROOT / "static/commands.js").read_text(encoding="utf-8")
    files = [{"name": "report.txt", "path": "reports/report.txt"}]
    result = _busy_send_node_result(
        messages,
        commands,
        ["/clear", "/moa inspect"],
        files=files,
        report_remaining_files=True,
    )
    assert result[0]["handled"] == [{"name": "clear", "args": ""}]
    assert result[0]["queued"] == []
    assert result[0]["remainingFiles"] == files

    queued = result[1]["queued"][0]
    assert queued["text"] == "inspect"
    assert queued["display_text"] == "/moa inspect"
    assert queued["files"] == files
    assert queued["intent"]["attachments"] == files


def test_server_queue_rejects_browser_only_commands_before_acceptance(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _disable_enqueue_drain(monkeypatch)
    session = _new_session("queue-browser-command", tmp_path)

    for text in ("/model gpt-5", "/clear"):
        status, response = _queue_action(
            {"session_id": session.session_id, "action": "enqueue", "text": text}
        )
        assert 400 <= status < 500, response
        assert response.get("accepted") is not True
        assert Session.load(session.session_id).queue == []


def test_server_queue_rejects_moa_transform_on_gateway_before_acceptance(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _disable_enqueue_drain(monkeypatch)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _config: True)
    session = _new_session("queue-moa-gateway", tmp_path)

    status, response = _queue_action(
        {"session_id": session.session_id, "action": "enqueue", "text": "/moa inspect"}
    )

    assert 400 <= status < 500, response
    assert response.get("accepted") is not True
    assert Session.load(session.session_id).queue == []


def test_unknown_slash_prompt_keeps_literal_text_when_drained(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    real_drain = routes.drain_queued_session_turn
    _disable_enqueue_drain(monkeypatch)
    session = _new_session("queue-literal-slash", tmp_path)
    raw_text = "/literal prompt that starts with slash"

    status, accepted = _queue_action(
        {"session_id": session.session_id, "action": "enqueue", "text": raw_text}
    )
    assert status == 200
    assert accepted["item"]["intent"]["raw_text"] == raw_text
    assert accepted["item"]["intent"]["agent_text"] == raw_text

    worker = _install_start_stubs(monkeypatch)
    monkeypatch.setattr(routes, "drain_queued_session_turn", real_drain)
    response = routes.drain_queued_session_turn(session.session_id)
    assert response["stream_id"]
    assert worker.starts[0][1][1] == raw_text


def test_compression_snapshot_keeps_parent_queue_until_child_save(tmp_path, monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "SESSION_DIR", tmp_path)
    (tmp_path / "old_session.json").write_text(
        json.dumps(
            {
                "messages": [],
                "queue": [{"id": "accepted", "text": "accepted", "files": []}],
            }
        ),
        encoding="utf-8",
    )
    saves = []

    class FakeSession:
        session_id = "new_session"
        parent_session_id = None
        pre_compression_snapshot = False
        pinned = True
        active_stream_id = "live"
        pending_user_message = "current"
        pending_attachments = []
        pending_started_at = 1.0
        pending_user_source = "webui"
        pending_queue_item = None
        messages = [{"role": "user", "content": "current"}]
        queue = [{"id": "accepted", "text": "accepted", "files": []}]

        def save(self, *, touch_updated_at=True, skip_index=False, backup_on_shrink=True):
            saves.append(self.session_id)
            payload = {
                "session_id": self.session_id,
                "parent_session_id": self.parent_session_id,
                "pre_compression_snapshot": self.pre_compression_snapshot,
                "pinned": self.pinned,
                "active_stream_id": self.active_stream_id,
                "pending_user_message": self.pending_user_message,
                "pending_attachments": self.pending_attachments,
                "pending_started_at": self.pending_started_at,
                "pending_user_source": self.pending_user_source,
                "pending_queue_item": self.pending_queue_item,
                "messages": self.messages,
                "queue": self.queue,
            }
            (tmp_path / f"{self.session_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    session = FakeSession()
    streaming._preserve_pre_compression_snapshot(session, "old_session")

    assert saves == ["old_session"]
    assert not (tmp_path / "new_session.json").exists()
    parent = json.loads((tmp_path / "old_session.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in parent["queue"]] == ["accepted"]
    assert [item["id"] for item in session.queue] == ["accepted"]


def test_compression_recovery_child_save_failure_keeps_parent_and_does_not_drain(
    monkeypatch, tmp_path
):
    _setup(monkeypatch, tmp_path)
    parent = _new_session(
        "recovery-save-parent",
        tmp_path,
        queue=[{"id": "accepted", "text": "accepted", "files": []}],
    )
    parent.pre_compression_snapshot = True
    parent.save()
    child = _new_session(
        "recovery-save-child",
        tmp_path,
        queue=[{"id": "child", "text": "child", "files": []}],
    )
    _mark_pending_compression_transfer(child, parent)
    child.save()

    original_save = Session.save

    def fail_child_save(self, *args, **kwargs):
        if self.session_id == child.session_id:
            raise OSError("child ownership save failed")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(Session, "save", fail_child_save)
    starts = []
    monkeypatch.setattr(
        routes,
        "_start_run",
        lambda *args, **kwargs: starts.append((args, kwargs)) or {"stream_id": "must-not-start"},
    )

    result = routes.drain_queued_session_turn(child.session_id)

    assert result["_status"] == 409
    assert starts == []
    assert [item["id"] for item in Session.load(parent.session_id).queue] == ["accepted"]


def test_compression_recovery_parent_clear_failure_keeps_child_owner_and_drains_once(
    monkeypatch, tmp_path
):
    _setup(monkeypatch, tmp_path)
    parent = _new_session(
        "recovery-parent-clear-parent",
        tmp_path,
        queue=[{"id": "parent", "text": "parent", "files": []}],
    )
    child = _new_session(
        "recovery-parent-clear-child",
        tmp_path,
        queue=[
            {"id": "parent", "text": "parent", "files": []},
            {"id": "child", "text": "child", "files": []},
        ],
    )
    _mark_pending_compression_transfer(child, parent)
    child.save()
    original_save = Session.save
    child_save_count = 0

    def fail_parent_save(self, *args, **kwargs):
        nonlocal child_save_count
        if self.session_id == child.session_id:
            child_save_count += 1
        if self.session_id == parent.session_id:
            raise OSError("parent clear save failed")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(Session, "save", fail_parent_save)
    recovered = routes._recover_compression_queue_transfer(
        parent.session_id, child.session_id
    )
    assert recovered is not None
    assert recovered.queue_transfer["state"] == "owned"
    assert [item["id"] for item in Session.load(child.session_id).queue] == [
        "parent",
        "child",
    ]

    starts = []
    monkeypatch.setattr(
        routes,
        "_start_run",
        lambda *args, **kwargs: starts.append((args, kwargs)) or {"stream_id": "child-stream"},
    )
    result = routes.drain_queued_session_turn(child.session_id)

    assert result["stream_id"] == "child-stream"
    assert len(starts) == 1
    assert starts[0][1]["queue_item_id"] == "parent"
    assert child_save_count == 1


def test_compression_recovery_clear_generation_discards_stale_parent_queue(
    monkeypatch, tmp_path
):
    _setup(monkeypatch, tmp_path)
    parent = _new_session(
        "recovery-generation-parent",
        tmp_path,
        queue=[{"id": "stale", "text": "stale", "files": []}],
    )
    parent.clear_generation = "old-generation"
    parent.save()
    child = _new_session(
        "recovery-generation-child",
        tmp_path,
        queue=[{"id": "fresh", "text": "fresh", "files": []}],
    )
    child.clear_generation = "new-generation"
    _mark_pending_compression_transfer(child, parent)
    child.save()

    recovered = routes._recover_compression_queue_transfer(
        parent.session_id, child.session_id
    )

    assert recovered is not None
    assert [item["id"] for item in recovered.queue] == ["fresh"]
    assert Session.load(parent.session_id).queue == []
    assert Session.load(parent.session_id).queue_transfer is None


def test_compression_restart_after_child_save_before_parent_clear_uses_lineage_once(
    monkeypatch, tmp_path
):
    _setup(monkeypatch, tmp_path)
    parent = _new_session(
        "restart-transfer-parent",
        tmp_path,
        queue=[
            {"id": "parent", "text": "parent", "files": []},
            {"id": "shared", "text": "parent copy", "files": []},
        ],
    )
    parent.pre_compression_snapshot = True
    parent.save()
    child = _new_session(
        "restart-transfer-child",
        tmp_path,
        queue=[
            {"id": "parent", "text": "parent", "files": []},
            {"id": "shared", "text": "child copy", "files": []},
            {"id": "child", "text": "child", "files": []},
        ],
    )
    child.parent_session_id = parent.session_id
    child.queue_transfer = {
        "version": 1,
        "state": "owned",
        "parent_session_id": parent.session_id,
        "child_session_id": child.session_id,
        "item_ids": ["parent", "shared", "child"],
        "clear_generation": child.clear_generation,
    }
    child.save()
    models.SESSIONS.clear()
    routes.SESSIONS.clear()

    original_save = Session.save

    def fail_restart_child_save(self, *args, **kwargs):
        if self.session_id == child.session_id:
            raise AssertionError("restart recovery must not rewrite durable child")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(Session, "save", fail_restart_child_save)
    snapshot = Session.load(parent.session_id)
    continuation = routes._pre_compression_continuation_session_id(snapshot)

    assert continuation == child.session_id
    recovered_child = Session.load(child.session_id)
    assert [item["id"] for item in recovered_child.queue] == [
        "parent",
        "shared",
        "child",
    ]
    assert next(item for item in recovered_child.queue if item["id"] == "shared")["text"] == "child copy"
    assert Session.load(parent.session_id).queue == []


def test_interrupted_compression_transfer_dedupes_surviving_sidecars_by_id(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    parent = _new_session(
        "compression-parent",
        tmp_path,
        queue=[
            {"id": "a", "text": "a", "files": []},
            {"id": "shared", "text": "parent", "files": []},
        ],
    )
    parent.pre_compression_snapshot = True
    parent.save()
    child = _new_session(
        "compression-child",
        tmp_path,
        queue=[
            {"id": "shared", "text": "child", "files": []},
            {"id": "c", "text": "c", "files": []},
        ],
    )
    _mark_pending_compression_transfer(child, parent)
    child.save()

    routes._recover_compression_queue_transfer(parent.session_id, child.session_id)

    recovered = Session.load(child.session_id)
    assert [item["id"] for item in recovered.queue] == ["a", "shared", "c"]
    assert next(item for item in recovered.queue if item["id"] == "shared")["text"] == "child"
    assert Session.load(parent.session_id).queue == []


def test_ordinary_branch_lineage_does_not_authorize_queue_transfer(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    parent = _new_session(
        "ordinary-branch-parent",
        tmp_path,
        queue=[{"id": "parent-only", "text": "parent", "files": []}],
    )
    child = _new_session(
        "ordinary-branch-child",
        tmp_path,
        queue=[{"id": "child-only", "text": "child", "files": []}],
    )
    child.parent_session_id = parent.session_id
    child.save()

    recovered = routes._recover_compression_queue_transfer(
        parent.session_id, child.session_id
    )

    persisted_parent = Session.load(parent.session_id)
    persisted_child = Session.load(child.session_id)
    assert recovered is not None
    assert persisted_parent is not None
    assert persisted_child is not None
    assert [item["id"] for item in recovered.queue] == ["child-only"]
    assert [item["id"] for item in persisted_parent.queue] == ["parent-only"]
    assert [item["id"] for item in persisted_child.queue] == ["child-only"]


@pytest.mark.parametrize("held_side", ("parent", "child"))
def test_compression_recovery_locks_both_sidecars_before_loading(
    monkeypatch, tmp_path, held_side
):
    _setup(monkeypatch, tmp_path)
    from api import config

    parent = _new_session(
        "compression-recovery-lock-parent",
        tmp_path,
        queue=[{"id": "parent-item", "text": "parent", "files": []}],
    )
    child = _new_session(
        "compression-recovery-lock-child",
        tmp_path,
        queue=[{"id": "child-item", "text": "child", "files": []}],
    )
    _mark_pending_compression_transfer(child, parent)
    child.save()

    parent_sid = parent.session_id
    child_sid = child.session_id
    held_sid = parent_sid if held_side == "parent" else child_sid
    held_lock = config._get_session_agent_lock(held_sid)
    held_lock.acquire()
    requested = []
    requests_ready = threading.Event()
    loads_started = threading.Event()
    result = {}
    original_get_lock = routes._get_session_agent_lock
    original_load = models.Session.load
    expected_requests = 1 if held_sid == min(parent_sid, child_sid) else 2

    def tracked_get_lock(sid):
        lock = original_get_lock(sid)
        requested.append(str(sid))
        if len(requested) >= expected_requests:
            requests_ready.set()
        return lock

    def tracked_load(cls, sid):
        loads_started.set()
        return original_load(sid)

    monkeypatch.setattr(routes, "_get_session_agent_lock", tracked_get_lock)
    monkeypatch.setattr(models.Session, "load", classmethod(tracked_load))

    def recover():
        try:
            result["session"] = routes._recover_compression_queue_transfer(
                parent_sid, child_sid
            )
        except BaseException as exc:  # surface worker failures after join
            result["error"] = exc

    worker = threading.Thread(target=recover, daemon=True)
    try:
        worker.start()
        assert requests_ready.wait(timeout=2)
        assert not loads_started.is_set()

        mutation = {
            "id": f"{held_side}-mutation",
            "text": "written while recovery waited",
            "files": [],
        }
        owner = parent if held_side == "parent" else child
        owner.queue.append(mutation)
        owner.save()
    finally:
        held_lock.release()
        worker.join(timeout=2)
        with config.SESSION_AGENT_LOCKS_LOCK:
            config.SESSION_AGENT_LOCKS.pop(parent_sid, None)
            config.SESSION_AGENT_LOCKS.pop(child_sid, None)

    assert not worker.is_alive()
    if "error" in result:
        raise result["error"]
    recovered = Session.load(child_sid)
    assert mutation["id"] in {item["id"] for item in recovered.queue}


def test_compression_recovery_acquires_an_aliased_lock_once(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    parent = _new_session(
        "compression-recovery-alias-parent",
        tmp_path,
        queue=[{"id": "parent-item", "text": "parent", "files": []}],
    )
    child = _new_session(
        "compression-recovery-alias-child",
        tmp_path,
        queue=[{"id": "child-item", "text": "child", "files": []}],
    )

    class CountingLock:
        def __init__(self):
            self._lock = threading.Lock()
            self.acquire_count = 0
            self.release_count = 0

        def acquire(self):
            self.acquire_count += 1
            return self._lock.acquire()

        def release(self):
            self.release_count += 1
            self._lock.release()

    aliased_lock = CountingLock()
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: aliased_lock)

    routes._recover_compression_queue_transfer(parent.session_id, child.session_id)

    assert aliased_lock.acquire_count == 1
    assert aliased_lock.release_count == 1


def test_compression_alias_happens_before_child_snapshot_exposure():
    source = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    rotation = source.index("s.session_id = new_sid")
    alias = source.index(
        "_alias_session_agent_lock(old_sid, new_sid, _agent_lock)", rotation
    )
    snapshot = source.index("_preserve_pre_compression_snapshot(s, old_sid)", rotation)

    assert rotation < alias < snapshot
    assert source.count("_alias_session_agent_lock(old_sid, new_sid, _agent_lock)") == 1


def test_clear_persists_empty_queue_and_new_generation(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session(
        "clear-queue-atomic",
        tmp_path,
        queue=[{"id": "wipe-me", "text": "old work", "files": []}],
    )
    session.active_stream_id = "old-stream"
    session.pending_user_message = "active"
    session.pending_queue_item = {"id": "claimed", "text": "active", "files": []}
    session.save()
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    body = json.dumps({"session_id": session.session_id}).encode()
    handler = _JSONHandler()
    handler.headers["Content-Length"] = str(len(body))
    handler.rfile = io.BytesIO(body)
    routes.handle_post(handler, SimpleNamespace(path="/api/session/clear", query=""))

    assert handler.status == 200
    assert _payload(handler)["session"]["queue"] == []
    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    assert persisted["queue"] == []
    assert persisted["clear_generation"]
    assert persisted["pending_queue_item"] is None

    stale = routes._start_chat_stream_for_session(
        session,
        msg="old teardown turn",
        attachments=[],
        workspace=str(tmp_path),
        model="model",
        model_provider="provider",
        external_runtime_owned=False,
        queue_generation="before-clear",
    )
    assert stale["_status"] == 409
    assert stale["reason"] == "cleared"

    _install_start_stubs(monkeypatch)
    fresh = routes._start_chat_stream_for_session(
        session,
        msg="fresh turn",
        attachments=[],
        workspace=str(tmp_path),
        model="model",
        model_provider="provider",
        external_runtime_owned=False,
        queue_generation=persisted["clear_generation"],
    )
    assert fresh["stream_id"]


def test_active_clear_fences_stale_teardown_from_restoring_old_queue(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from api import streaming

    session = _new_session(
        "clear-stale-teardown",
        tmp_path,
        queue=[{"id": "tail", "text": "tail", "files": []}],
    )
    session.messages = [{"role": "user", "content": "prior turn"}]
    session.active_stream_id = "old-stream"
    session.pending_user_message = "claimed"
    session.pending_queue_item = {"id": "claimed", "text": "claimed", "files": []}
    session.pending_turn_intent = {"clear_generation": session.clear_generation}
    session.save()
    stale_worker_session = Session.load(session.session_id)

    cleared = Session.load(session.session_id)
    cleared.queue = []
    cleared.active_stream_id = None
    cleared.pending_user_message = None
    cleared.pending_queue_item = None
    cleared.pending_turn_intent = None
    cleared.pending_queue_outcome = {"state": "cleared", "item_id": None}
    cleared.clear_generation = "after-clear"
    cleared.save()

    streaming._finalize_cancelled_turn(stale_worker_session, stream_id="old-stream")

    persisted = Session.load(session.session_id)
    assert persisted.queue == []
    assert persisted.clear_generation == "after-clear"
    assert persisted.pending_queue_outcome == {"state": "cleared", "item_id": None}


@pytest.mark.parametrize("backend", ["local", "gateway"])
def test_cancel_before_worker_start_runs_one_shared_post_teardown_drain(monkeypatch, backend):
    if backend == "local":
        from api import streaming

        monkeypatch.setattr(streaming, "STREAMS", {})
        monkeypatch.setattr(streaming, "unregister_stream_owner", lambda *_args: None)
        monkeypatch.setattr(streaming, "clear_session_writeback_owner_if_owned", lambda *_args: None)
        drained = []
        monkeypatch.setattr(streaming, "_drain_queued_session_turn_after_teardown", lambda sid: drained.append(sid))
        streaming._run_agent_streaming("sid", "msg", "model", ".", "stream")
    else:
        from api import gateway_chat

        monkeypatch.setattr(gateway_chat, "STREAMS", {})
        monkeypatch.setattr(gateway_chat, "_finish_gateway_run_starting", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(gateway_chat, "_clear_gateway_run_starting", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(gateway_chat, "unregister_stream_owner", lambda *_args: None)
        monkeypatch.setattr(gateway_chat, "clear_session_writeback_owner_if_owned", lambda *_args: None)
        drained = []
        monkeypatch.setattr(gateway_chat, "_drain_queued_session_turn_after_teardown", lambda sid: drained.append(sid))
        gateway_chat._run_gateway_chat_streaming("sid", "msg", "model", ".", "stream")
    assert drained == ["sid"]


def test_local_cancel_consumes_claimed_item_and_drains_tail(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from api import streaming

    session = _new_session("cancel-restore-local", tmp_path, queue=[{"id": "tail", "text": "tail", "files": []}])
    session.active_stream_id = "stream"
    session.pending_user_message = "claimed"
    session.pending_queue_item = {"id": "claimed", "text": "claimed", "files": []}
    session.pending_turn_intent = {"clear_generation": session.clear_generation}
    streaming._persist_cancelled_turn(session)
    session.save()

    persisted = Session.load(session.session_id)
    assert [item["id"] for item in persisted.queue] == ["tail"]
    assert persisted.pending_queue_outcome["state"] == "cancelled"

    worker = _install_start_stubs(monkeypatch)
    response = streaming._drain_queued_session_turn_after_teardown(session.session_id)
    assert response["stream_id"]
    assert len(worker.starts) == 1
    assert worker.starts[0][1][1] == "tail"


def test_gateway_terminal_error_consumes_claimed_item_with_durable_outcome(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from api import gateway_chat

    session = _new_session("cancel-restore-gateway", tmp_path, queue=[{"id": "tail", "text": "tail", "files": []}])
    session.active_stream_id = "gateway-stream"
    session.pending_user_message = "claimed"
    session.pending_queue_item = {"id": "claimed", "text": "claimed", "files": []}
    session.save()

    result = gateway_chat._settle_gateway_terminal_error(
        session.session_id,
        "gateway-stream",
        str(tmp_path),
        "model",
        "provider",
        RuntimeError("cancelled"),
    )
    assert result["terminal_session_persisted"] is True
    persisted = Session.load(session.session_id)
    assert [item["id"] for item in persisted.queue] == ["tail"]
    assert persisted.pending_queue_outcome["state"] == "error"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            "empty",
            {
                "label": "Gateway returned no response",
                "type": "gateway_empty_response",
                "message": "Gateway returned no assistant message for this turn.",
            },
        ),
        (
            "http",
            {
                "label": "Gateway request failed",
                "type": "gateway_http_error",
                "message": "Gateway returned HTTP 503.",
            },
        ),
        (
            "unexpected",
            {
                "label": "Gateway request failed",
                "type": "gateway_error",
                "message": "unexpected gateway exception",
            },
        ),
    ],
)
def test_gateway_terminal_failures_durably_settle_claimed_queue_once(
    monkeypatch, tmp_path, failure, expected
):
    _setup(monkeypatch, tmp_path)
    drain_queued_turn = routes.drain_queued_session_turn
    _disable_enqueue_drain(monkeypatch)
    from api import config, gateway_chat, streaming

    session = _new_session("gateway-terminal-lifecycle", tmp_path)
    attachment = _uploaded_file(tmp_path / "attachments", session.session_id, "queued.txt")
    status, head = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "Visible queued turn",
            "display_text": "Visible queued turn",
            "files": [attachment],
            "model": "gpt-4o",
            "model_provider": "openai",
        }
    )
    assert status == 200
    status, tail = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "tail",
            "model": "gpt-4o",
            "model_provider": "openai",
        }
    )
    assert status == 200
    monkeypatch.setattr(routes, "drain_queued_session_turn", drain_queued_turn)

    class Channel:
        def __init__(self):
            self.events = []

        def put_nowait(self, event):
            self.events.append(event)

    channel = Channel()
    worker = _install_start_stubs(monkeypatch)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: channel)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _config: True)
    monkeypatch.setattr(gateway_chat, "RunJournalWriter", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "register_active_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "update_active_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "unregister_active_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "unregister_stream_owner", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "clear_session_writeback_owner_if_owned", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "_cleanup_gateway_pending_mirror", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "_drain_queued_session_turn_after_teardown", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "gateway_run_id_pending", lambda *_args: False)
    monkeypatch.setattr(gateway_chat, "_mark_gateway_run_starting", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "_finish_gateway_run_starting", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "_clear_gateway_run_starting", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *_args: False)
    monkeypatch.setattr(gateway_chat, "gateway_approval_unavailable_reason", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "_gateway_use_runs_api_enabled", lambda *_args: False)
    monkeypatch.setattr(gateway_chat, "_gateway_base_url", lambda *_args: "http://gateway.local")
    monkeypatch.setattr(gateway_chat, "_gateway_api_key", lambda *_args: "")
    monkeypatch.setattr(gateway_chat, "_gateway_read_timeout_secs", lambda *_args: 1)
    monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_main_model_request_overrides", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(gateway_chat, "clear_process_wakeup_pause", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda *_args: [])
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda *_args: [])
    monkeypatch.setattr(streaming, "_normalize_prefill_messages_before_user_turn", lambda value: value)
    monkeypatch.setattr(streaming, "_public_prefill_context_status", lambda *_args: {})
    monkeypatch.setattr(streaming, "_webui_ephemeral_system_prompt", lambda *_args, **_kwargs: "system")

    if failure == "empty":
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                yield b"data: [DONE]\n\n"

        urlopen = lambda *_args, **_kwargs: Response()
    elif failure == "http":
        def urlopen(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                "http://gateway.local/v1/chat/completions",
                503,
                "Service Unavailable",
                hdrs={},
                fp=io.BytesIO(b"gateway down"),
            )
    else:
        def urlopen(*_args, **_kwargs):
            raise RuntimeError("unexpected gateway exception")

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", urlopen)
    start = routes.drain_queued_session_turn(session.session_id)
    assert start["queue_item_id"] == head["item"]["id"]
    stream_id = start["stream_id"]
    settlements = []
    original_settle = routes._settle_claimed_queue_item

    def settle(current, *, outcome, stream_id=None):
        settlements.append((outcome, stream_id))
        return original_settle(current, outcome=outcome, stream_id=stream_id)

    monkeypatch.setattr(routes, "_settle_claimed_queue_item", settle)
    target, args, kwargs = worker.starts[0]
    target(*args, **kwargs)

    errors = [data for event, data in channel.events if event == "apperror"]
    assert len(errors) == 1
    assert {key: errors[-1][key] for key in expected} == expected
    saved = Session.load(session.session_id)
    user = saved.messages[-2]
    assistant = saved.messages[-1]
    assert user["content"] == "Visible queued turn"
    assert user["attachments"] == start["attachments"]
    assert assistant["role"] == "assistant"
    assert assistant["_error"] is True
    assert saved.active_stream_id is None
    assert saved.pending_user_message is None
    assert saved.pending_attachments == []
    assert saved.pending_queue_item is None
    assert saved.pending_turn_intent is None
    assert saved.pending_queue_outcome == {
        "state": "error",
        "item_id": head["item"]["id"],
        "stream_id": stream_id,
    }
    assert [entry["id"] for entry in saved.queue] == [tail["item"]["id"]]
    assert settlements == [("error", stream_id)]


@pytest.mark.parametrize("kind", ["moa", "bundle", "literal"])
def test_queue_combine_guard_preserves_transformed_items_and_allows_literals(
    monkeypatch, tmp_path, kind
):
    _setup(monkeypatch, tmp_path)
    _disable_enqueue_drain(monkeypatch)
    session = _new_session(f"combine-{kind}", tmp_path)

    if kind == "moa":
        from api import commands

        monkeypatch.setattr(commands, "resolve_moa_config", lambda *_args: {"preset": "default"})
        first_body = {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "inspect",
            "display_text": "/moa inspect",
            "intent": {
                "version": 1,
                "command": {
                    "name": "moa",
                    "args": "inspect",
                    "raw": "/moa inspect",
                    "dispatch": "server_transform",
                    "transform": "moa",
                },
            },
            "model": "gpt-4o",
            "model_provider": "openai",
        }
    elif kind == "bundle":
        from api import commands

        monkeypatch.setattr(commands, "list_commands", lambda: [])
        monkeypatch.setattr(commands, "list_command_bundles", lambda: [{"name": "research"}])
        monkeypatch.setattr(
            commands,
            "resolve_bundle_command",
            lambda _raw: {"name": "research", "message": "bundle invocation"},
        )
        first_body = {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "bundle invocation",
            "display_text": "/research query",
            "intent": {
                "version": 1,
                "command": {
                    "name": "research",
                    "args": "query",
                    "raw": "/research query",
                    "dispatch": "transform_required",
                    "transform": "bundle",
                },
            },
            "model": "gpt-4o",
            "model_provider": "openai",
        }
    else:
        first_body = {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "one",
            "model": "gpt-4o",
            "model_provider": "openai",
        }

    status, first = _queue_action(first_body)
    assert status == 200
    status, single_combine = _queue_action(
        {"session_id": session.session_id, "action": "combine"}
    )
    assert status == 200
    assert [entry["id"] for entry in single_combine["queue"]] == [first["item"]["id"]]
    status, second = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "two",
            "model": "gpt-4o",
            "model_provider": "openai",
        }
    )
    assert status == 200
    before_queue = copy.deepcopy(Session.load(session.session_id).queue)
    before_bytes = session.path.read_bytes()

    status, response = _queue_action(
        {"session_id": session.session_id, "action": "combine"}
    )

    if kind == "literal":
        assert status == 200
        assert response["queue"][0]["text"] == "one\n\ntwo"
        assert response["queue"][0]["files"] == []
        assert Session.load(session.session_id).queue == response["queue"]
    else:
        assert status == 409
        assert response == {
            "error": "cannot combine transformed queue items",
            "error_code": "queue_command_preprocess",
        }
        assert session.queue == before_queue
        assert session.path.read_bytes() == before_bytes
        assert Session.load(session.session_id).queue == before_queue
        assert [entry["id"] for entry in before_queue] == [
            first["item"]["id"],
            second["item"]["id"],
        ]


def test_gateway_late_same_stream_cancel_corrects_completed_queue_settlement(
    monkeypatch, tmp_path
):
    _setup(monkeypatch, tmp_path)
    from api import config, gateway_chat, streaming

    session = _new_session(
        "gateway-late-cancel",
        tmp_path,
        queue=[{"id": "tail", "text": "tail", "files": []}],
    )
    session.active_stream_id = "gateway-stream"
    session.pending_user_message = "claimed"
    session.pending_attachments = []
    session.pending_queue_item = {"id": "claimed", "text": "claimed", "files": []}
    session.pending_turn_intent = {"clear_generation": session.clear_generation}
    session.save()

    class Queue:
        def __init__(self):
            self.events = []

        def put_nowait(self, event):
            self.events.append(event)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"answer"}}]}\n'
            yield b'data: [DONE]\n'

    queue = Queue()
    lock = threading.RLock()
    outcomes = []
    original_settle = routes._settle_claimed_queue_item

    def settle(current, *, outcome, stream_id=None):
        outcomes.append((outcome, stream_id))
        result = original_settle(current, outcome=outcome, stream_id=stream_id)
        if outcome == "completed":
            gateway_chat.CANCEL_FLAGS[stream_id].set()
        return result

    monkeypatch.setattr(routes, "_settle_claimed_queue_item", settle)
    monkeypatch.setattr(gateway_chat, "STREAMS", {"gateway-stream": queue})
    monkeypatch.setattr(gateway_chat, "get_session", lambda _sid: session)
    monkeypatch.setattr(gateway_chat, "_get_session_agent_lock", lambda _sid: lock)
    monkeypatch.setattr(gateway_chat, "RunJournalWriter", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "register_active_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "update_active_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "unregister_active_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "unregister_stream_owner", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "clear_session_writeback_owner_if_owned", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "_cleanup_gateway_pending_mirror", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "_drain_queued_session_turn_after_teardown", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "gateway_run_id_pending", lambda *_args: False)
    monkeypatch.setattr(gateway_chat, "_clear_gateway_run_starting", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *_args: False)
    monkeypatch.setattr(gateway_chat, "gateway_approval_unavailable_reason", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "_gateway_use_runs_api_enabled", lambda *_args: False)
    monkeypatch.setattr(gateway_chat, "_gateway_base_url", lambda *_args: "http://gateway")
    monkeypatch.setattr(gateway_chat, "_gateway_api_key", lambda *_args: "")
    monkeypatch.setattr(gateway_chat, "_gateway_read_timeout_secs", lambda *_args: 1)
    monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_main_model_request_overrides", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(gateway_chat, "clear_process_wakeup_pause", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda *_args: [])
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda *_args: [])
    monkeypatch.setattr(streaming, "_normalize_prefill_messages_before_user_turn", lambda value: value)
    monkeypatch.setattr(streaming, "_public_prefill_context_status", lambda *_args: {})
    monkeypatch.setattr(streaming, "_webui_ephemeral_system_prompt", lambda *_args, **_kwargs: "system")

    gateway_chat._run_gateway_chat_streaming(
        session.session_id,
        "claimed",
        "model",
        str(tmp_path),
        "gateway-stream",
        [],
        model_provider="provider",
    )

    persisted = Session.load(session.session_id)
    assert outcomes == [("completed", "gateway-stream"), ("cancelled", "gateway-stream")]
    assert persisted.pending_queue_outcome["state"] == "cancelled"
    assert [item["id"] for item in persisted.queue] == ["tail"]


def test_gateway_cancelled_terminal_error_does_not_overwrite_queue_as_error(
    monkeypatch, tmp_path
):
    _setup(monkeypatch, tmp_path)
    from api import gateway_chat

    session = _new_session(
        "gateway-cancelled-error",
        tmp_path,
        queue=[{"id": "tail", "text": "tail", "files": []}],
    )
    session.active_stream_id = "gateway-error-stream"
    session.pending_user_message = "claimed"
    session.pending_queue_item = {"id": "claimed", "text": "claimed", "files": []}
    session.pending_turn_intent = {"clear_generation": session.clear_generation}
    session.save()
    monkeypatch.setattr(gateway_chat, "get_session", lambda _sid: session)
    monkeypatch.setattr(gateway_chat, "_get_session_agent_lock", lambda _sid: threading.RLock())
    cancelled = threading.Event()
    cancelled.set()

    result = gateway_chat._settle_gateway_terminal_error(
        session.session_id,
        "gateway-error-stream",
        str(tmp_path),
        "model",
        "provider",
        RuntimeError("provider failed after stop"),
        cancel_event=cancelled,
    )

    assert result["cancelled"] is True
    persisted = Session.load(session.session_id)
    assert persisted.pending_queue_outcome["state"] == "cancelled"
    assert [item["id"] for item in persisted.queue] == ["tail"]


def test_duplicate_terminal_settlement_does_not_reinsert_claimed_item(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session(
        "duplicate-terminal-settlement",
        tmp_path,
        queue=[{"id": "tail", "text": "tail", "files": []}],
    )
    session.active_stream_id = "stream"
    session.pending_queue_item = {"id": "claimed", "text": "claimed", "files": []}
    session.pending_turn_intent = {"clear_generation": session.clear_generation}

    first = routes._settle_claimed_queue_item(session, outcome="cancelled", stream_id="stream")
    second = routes._settle_claimed_queue_item(session, outcome="cancelled", stream_id="stream")

    assert first == "cancelled"
    assert second == "cancelled"
    assert [item["id"] for item in session.queue] == ["tail"]
    assert session.pending_queue_outcome == {
        "state": "cancelled",
        "item_id": "claimed",
        "stream_id": "stream",
    }


def test_same_stream_late_cancel_corrects_completed_queue_outcome(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session("late-cancel-outcome", tmp_path)
    completed = {"state": "completed", "item_id": "claimed", "stream_id": "stream"}
    session.pending_queue_item = None
    session.pending_queue_outcome = dict(completed)

    assert routes._settle_claimed_queue_item(
        session, outcome="cancelled", stream_id="stream"
    ) == "cancelled"
    assert session.pending_queue_outcome["state"] == "cancelled"

    session.pending_queue_outcome = dict(completed)
    assert routes._settle_claimed_queue_item(
        session, outcome="cancelled", stream_id="stale-stream"
    ) == "completed"
    assert session.pending_queue_outcome == completed


def test_worker_admission_failure_restores_claimed_item(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session(
        "admission-rollback",
        tmp_path,
        queue=[
            {"id": "claimed", "text": "claimed", "files": []},
            {"id": "tail", "text": "tail", "files": []},
        ],
    )

    def fail_thread_start():
        raise RuntimeError("worker admission failed")

    _install_start_stubs(monkeypatch, thread_start=fail_thread_start)
    with pytest.raises(RuntimeError, match="worker admission failed"):
        routes._start_chat_stream_for_session(
            session,
            msg="claimed",
            attachments=[],
            workspace=str(tmp_path),
            model="model",
            model_provider="provider",
            external_runtime_owned=False,
            claim_queue_head=True,
        )

    persisted = Session.load(session.session_id)
    assert [item["id"] for item in persisted.queue] == ["claimed", "tail"]
    assert persisted.pending_queue_item is None
    assert persisted.pending_queue_outcome is None


def test_worker_admission_failure_after_clear_does_not_restore_claimed_item(
    monkeypatch, tmp_path
):
    _setup(monkeypatch, tmp_path)
    session = _new_session(
        "admission-rollback-after-clear",
        tmp_path,
        queue=[
            {"id": "claimed", "text": "claimed", "files": []},
            {"id": "tail", "text": "tail", "files": []},
        ],
    )
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)

    def clear_then_fail():
        body = json.dumps({"session_id": session.session_id}).encode()
        handler = _JSONHandler()
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        routes.handle_post(handler, SimpleNamespace(path="/api/session/clear", query=""))
        assert handler.status == 200
        raise RuntimeError("worker admission failed after clear")

    _install_start_stubs(monkeypatch, thread_start=clear_then_fail)
    with pytest.raises(RuntimeError, match="worker admission failed after clear"):
        routes._start_chat_stream_for_session(
            session,
            msg="claimed",
            attachments=[],
            workspace=str(tmp_path),
            model="model",
            model_provider="provider",
            external_runtime_owned=False,
            claim_queue_head=True,
        )

    persisted = Session.load(session.session_id)
    assert persisted.queue == []
    assert persisted.clear_generation
    assert persisted.pending_queue_outcome == {"state": "cleared", "item_id": None}


def test_frontend_queue_identity_rejects_duplicate_text_with_different_ids():
    sessions = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
    same = _js_function(sessions, "_sameTranscriptMessage")
    attachment_identity = _js_function(sessions, "_transcriptAttachmentIdentity")
    result = _run_node(
        f"""
        function _messageComparableText(m){{return String(m.content||'').trim();}}
        function _normalizeUserTranscriptText(v){{return String(v||'').trim();}}
        {attachment_identity}
        {same}
        console.log(JSON.stringify({{
          sameId:_sameTranscriptMessage({{role:'user',content:'repeat',queue_item_id:'a',attachments:[{{name:'one'}}]}},{{role:'user',content:'repeat',queue_item_id:'a',attachments:[{{name:'two'}}]}}),
          differentId:_sameTranscriptMessage({{role:'user',content:'repeat',queue_item_id:'a'}},{{role:'user',content:'repeat',queue_item_id:'b'}}),
          legacy:_sameTranscriptMessage({{role:'user',content:'repeat'}},{{role:'user',content:'repeat'}}),
          legacyDifferentFiles:_sameTranscriptMessage({{role:'user',content:'repeat',attachments:[{{name:'one'}}]}},{{role:'user',content:'repeat',attachments:[{{name:'two'}}]}}),
          legacySameFiles:_sameTranscriptMessage({{role:'user',content:'repeat',attachments:[{{name:'one'}}]}},{{role:'user',content:'repeat',attachments:[{{name:'one'}}]}})
        }}));
        """
    )
    assert result == {
        "sameId": True,
        "differentId": False,
        "legacy": True,
        "legacyDifferentFiles": False,
        "legacySameFiles": True,
    }


def test_server_turn_started_reconciliation_updates_queue_badge_immediately():
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    reconcile = _js_function(messages, "_reconcileServerTurnStarted")
    result = _run_node(
        f"""
        const S={{session:{{session_id:'sid'}},messages:[]}};
        const badges=[];
        function hydrateSessionQueue(){{}}
        function updateQueueBadge(sid){{badges.push(sid);}}
        function _mergePendingSessionMessage(){{return false;}}
        {reconcile}
        _reconcileServerTurnStarted('sid',{{queue:[{{id:'tail'}}],queue_item:{{id:'claimed',display_text:'claimed',files:[]}}}});
        console.log(JSON.stringify(badges));
        """
    )
    assert result == ["sid"]


def test_runner_local_direct_start_remains_supported_with_unsupported_queue(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    session = _new_session(
        "runner-local-busy",
        tmp_path,
        queue=[{"id": "retained-server-item", "text": "retained", "files": []}],
    )

    class Client:
        def start_run(self, request):
            return {"run_id": "runner", "stream_id": "runner-stream", "session_id": request.session_id}

    monkeypatch.setattr(routes, "_runtime_runner_client_factory", lambda: Client())
    response = routes._start_run(
        session,
        msg="direct",
        attachments=[],
        workspace=str(tmp_path),
        model="model",
        model_provider="provider",
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
        gateway_chat_enabled=False,
    )
    assert response["stream_id"] == "runner-stream"
    assert response.get("_status", 200) != 501


def test_queue_errors_use_locale_catalog_and_toast_safe_fallback(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _disable_enqueue_drain(monkeypatch)
    session = _new_session("queue-localized-error", tmp_path)
    raw_error = "raw server mutation failure in English"

    def fail_save():
        raise RuntimeError(raw_error)

    monkeypatch.setattr(session, "save", fail_save)
    status, route_response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "clear",
        }
    )
    assert status == 500
    assert route_response["error"] == raw_error
    assert route_response["error_code"] == "queue_update_failed"

    stale_fallback = "typeof t==='function'?t('queue_failed'):'queue_failed'"
    for source, count in ((ROOT / "static/commands.js").read_text(encoding="utf-8"), 3), (
        (ROOT / "static/messages.js").read_text(encoding="utf-8"), 1
    ):
        queue_catches = [
            line for line in source.splitlines()
            if "typeof _queueErrorMessage==='function'?_queueErrorMessage(err)" in line
        ]
        assert len(queue_catches) == count
        assert all("err&&err.message" not in line and stale_fallback in line for line in queue_catches)

    i18n = (ROOT / "static/i18n.js").read_text(encoding="utf-8")
    english_catalog = i18n[:i18n.index("// Active locale")]
    translate = _js_function(i18n, "t")
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    queue_error_message = _js_function(ui, "_queueErrorMessage")
    show_toast = _js_function(ui, "showToast")
    result = _run_node(
        f"""
        {english_catalog}
        let _locale=LOCALES.es;
        {translate}
        {queue_error_message}
        const toast={{className:'',dataset:{{}},innerHTML:'',textContent:''}};
        function $(id){{return id==='toast'?toast:null;}}
        function esc(value){{return String(value??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c]));}}
        function setTimeout(){{return 1;}}
        function clearTimeout(){{}}
        function setToastDismissTimer(){{}}
        {show_toast}
        const queueKeys=['queue_failed','queue_update_failed','queue_browser_command','queue_command_preprocess',
          'queue_full','queue_item_not_found','queue_invalid_request','queue_unavailable','queue_unsupported','queue_read_only'];
        const missingCatalogKeys=Object.entries(LOCALES).flatMap(([locale,catalog])=>
          queueKeys.filter(key=>!(key in catalog)).map(key=>locale+':'+key));
        const routeResponse={json.dumps(route_response)};
        const routeError=new Error(routeResponse.error);
        routeError.body=JSON.stringify(routeResponse);
        const savedT=t;
        const values={{
          recognizedCode:_queueErrorMessage({{code:'queue_full',message:'raw queue full'}},'queue_failed'),
          recognizedBody:_queueErrorMessage({{body:JSON.stringify({{error_code:'queue_full'}}),message:'raw queue full'}},'queue_failed'),
          uncoded:_queueErrorMessage(new Error('queue is full'),'queue_failed'),
          unknown:_queueErrorMessage({{code:'server_new_code',message:'raw server English'}},'queue_update_failed'),
          unrelatedCode:_queueErrorMessage({{code:'offline_title',message:'raw offline English'}},'queue_failed'),
          catalogMissing:missingCatalogKeys
        }};
        t=(key)=>key;
        values.untranslated=_queueErrorMessage({{code:'queue_full',message:'raw server English'}},'queue_update_failed');
        t=undefined;
        values.noTranslator=_queueErrorMessage(new Error('raw server English'),'queue_failed');
        t=savedT;
        showToast(_queueErrorMessage(routeError,'queue_update_failed'),3500,'error');
        values.toastMessage=toast.dataset.toastMessage;
        values.toastHtmlHasRaw=toast.innerHTML.includes({json.dumps(raw_error)});
        console.log(JSON.stringify(values));
        """
    )
    assert result == {
        "recognizedCode": "La cola está llena",
        "recognizedBody": "La cola está llena",
        "uncoded": "Error en la cola",
        "unknown": "No se pudo actualizar la cola",
        "unrelatedCode": "Error en la cola",
        "untranslated": "queue_update_failed",
        "noTranslator": "queue_failed",
        "catalogMissing": [],
        "toastMessage": "No se pudo actualizar la cola",
        "toastHtmlHasRaw": False,
    }


def test_queue_rejections_have_stable_error_codes(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    full = _new_session(
        "queue-full-error-code",
        tmp_path,
        queue=[{"id": f"item-{index}", "text": "queued", "files": []} for index in range(100)],
    )
    status, response = _queue_action(
        {"session_id": full.session_id, "action": "enqueue", "text": "overflow"}
    )
    assert (status, response["error_code"]) == (409, "queue_full")

    session = _new_session("queue-rejection-error-codes", tmp_path)
    status, response = _queue_action(
        {"session_id": session.session_id, "action": "delete", "item_id": "missing"}
    )
    assert (status, response["error_code"]) == (404, "queue_item_not_found")

    status, response = _queue_action(
        {"session_id": "missing-session", "action": "clear"}
    )
    assert (status, response["error_code"]) == (404, "queue_unavailable")

    status, response = _queue_action(
        {"session_id": session.session_id, "action": "reorder", "item_ids": "bad"}
    )
    assert (status, response["error_code"]) == (400, "queue_invalid_request")

    status, response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "bad attachment",
            "files": [{"name": "missing.txt"}],
        }
    )
    assert (status, response["error_code"]) == (400, "queue_invalid_request")

    def reject_workspace(_path):
        raise ValueError("workspace rejected")

    monkeypatch.setattr(routes, "resolve_trusted_workspace", reject_workspace)
    status, response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "bad workspace",
            "workspace": "/not-allowed",
        }
    )
    assert (status, response["error_code"]) == (400, "queue_invalid_request")

    session.read_only = True
    session.save = lambda: None
    status, response = _queue_action(
        {"session_id": session.session_id, "action": "clear"}
    )
    assert (status, response["error_code"]) == (403, "queue_read_only")


def test_queue_initial_session_load_failure_is_typed(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session("queue-load-error-code", tmp_path)

    def fail_load(_sid):
        raise RuntimeError("queue load failed at /srv/private/queue-secret")

    monkeypatch.setattr(routes, "get_session", fail_load)
    status, response = _queue_action(
        {"session_id": session.session_id, "action": "clear"}
    )

    assert status == 500
    assert response == {
        "error": "queue load failed at <path>",
        "error_code": "queue_update_failed",
    }
