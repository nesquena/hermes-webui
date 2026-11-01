"""Slash-prefixed composer text is agent input unless WebUI interception is opted in."""

import io
import json
from pathlib import Path
import subprocess
import tempfile
import textwrap
from types import SimpleNamespace

import pytest

import api.routes as routes


ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _run_send(message: str, *, intercept: bool = False) -> dict:
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const chatStarts = [];
        let newSessionCalls = 0;
        const msgInput = {{
          value: {json.dumps(message)},
          style: {{}},
          scrollHeight: 0,
          selectionStart: {len(message)},
          selectionEnd: {len(message)},
          addEventListener(){{}}, removeEventListener(){{}}, focus(){{}}, blur(){{}},
          setSelectionRange(){{}},
        }};
        const genericEl = {{
          addEventListener(){{}}, removeEventListener(){{}},
          classList: {{ add(){{}}, remove(){{}}, contains(){{return false;}} }},
          style: {{}}, dataset: {{}}, value: '', textContent: '', innerHTML: '',
          hidden:false, setAttribute(){{}}, removeAttribute(){{}}, getAttribute(){{return null;}},
          querySelector(){{return null;}}, querySelectorAll(){{return []; }},
        }};
        const ctx = {{
          console,
          window: {{
            __HERMES_CONFIG__: {{interceptSlashCommands: {json.dumps(intercept)}}},
            addEventListener(){{}}, requestAnimationFrame(){{return 1;}},
            location: {{pathname:'/',search:''}},
          }},
          _testMessage: {json.dumps(message)},
          history: {{replaceState(){{}}}},
          document: {{
            addEventListener(){{}},
            getElementById(id){{ return id === 'msg' ? msgInput : genericEl; }},
            querySelector(){{ return null; }},
          }},
          localStorage: {{getItem(){{return null;}},setItem(){{}},removeItem(){{}}}},
          setInterval(){{return 1;}}, clearInterval(){{}}, setTimeout(fn){{fn();return 1;}}, clearTimeout(){{}},
          t: key => key,
          S: {{
            busy:false,
            session:{{session_id:'sid-1',title:'Existing chat',workspace:'/tmp/workspace',model:'test-model'}},
            pendingFiles:[], messages:[], activeProfile:'default', activeStreamId:null,
          }},
          INFLIGHT: {{}},
          _pendingSelections: [], _sendInProgress:false, _sendInProgressSid:null,
          _composerTextWithPendingSelections(){{return msgInput.value;}},
          _flushSelectionBlocksToComposer(){{}}, _dismissHandoffHint(){{}},
          _clearStaleBusyStateBeforeSend(){{return false;}},
          _chatPayloadModelState(){{return {{model:'test-model',model_provider:null}};}},
          _clearComposerAfterQueuedSelectionSend(){{}}, queueSessionMessage(){{}},
          updateQueueBadge(){{}}, renderTray(){{}}, showToast(){{}}, renderMessages(){{}},
          renderSessionList(){{}}, renderSessionListFromCache(){{}}, autoResize(){{}},
          syncTopbar(){{}}, setBusy(value){{ctx.S.busy=value;}}, setComposerStatus(){{}},
          setStatus(){{}}, updateSendBtn(){{}}, clearOptimisticSessionStreaming(){{}},
          clearLiveToolCards(){{}}, appendThinking(){{}}, removeThinking(){{}},
          startApprovalPolling(){{}}, stopApprovalPolling(){{}},
          startClarifyPolling(){{}}, stopClarifyPolling(){{}}, _fetchYoloState(){{}},
          hideApprovalCard(){{}}, hideClarifyCard(){{}},
          uploadPendingFiles: async () => [],
          _clearComposerDraft: async () => {{}},
          _restoreComposerDraftAfterFailedSend(){{}},
          _recoverCompressedSend: async () => false,
          applySessionTitleUpdate(){{}}, upsertActiveSessionForLocalTurn(){{}},
          newSession: async () => {{newSessionCalls+=1;ctx.S.session={{session_id:'sid-2',title:'Untitled',workspace:'/tmp/workspace',model:'test-model'}};}},
          _runOptionalPreStartUiStep(_name, fn){{fn();}},
          $: id => id === 'msg' ? msgInput : genericEl,
          api: async (path, options={{}}) => {{
            if(path==='/api/chat/start'){{
              chatStarts.push(JSON.parse(options.body));
              const error=new Error('stop after payload capture');
              error.status=404;
              throw error;
            }}
            if(path==='/api/commands') return {{commands:[]}};
            if(path==='/api/commands/bundles') return {{bundles:[]}};
            if(path==='/api/skills') return {{skills:[]}};
            throw new Error('unexpected api path: '+path);
          }},
        }};
        ctx.window.window=ctx.window;
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)},ctx);
        vm.runInContext({json.dumps(MESSAGES_JS)},ctx);
        vm.runInContext(`
          startApprovalPolling=()=>{{}}; stopApprovalPolling=()=>{{}};
          startClarifyPolling=()=>{{}}; stopClarifyPolling=()=>{{}};
          hideApprovalCard=()=>{{}}; hideClarifyCard=()=>{{}}; removeThinking=()=>{{}};
          clearOptimisticSessionStreaming=()=>{{}}; renderMessages=()=>{{}};
          renderSessionList=()=>{{}}; setBusy=value=>{{S.busy=value;}}; setComposerStatus=()=>{{}};
        `,ctx);
        (async()=>{{
          const menuMatches=await vm.runInContext('getSlashAutocompleteMatches(_testMessage)',ctx);
          await vm.runInContext('send()',ctx);
          process.stdout.write(JSON.stringify({{chatStarts,messages:ctx.S.messages,menuMatches,newSessionCalls}}));
        }})().catch(error=>{{console.error(error&&error.stack||error);process.exit(1);}});
        """
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run(["node", str(script_path)], capture_output=True, text=True)
    finally:
        script_path.unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize(
    "message",
    [
        "/help",
        "/status",
        "/model",
        "/new",
        "/agents",
        "/custom-command argument1",
        "/custom-command  argument1 /nested  ",
        "Bonjour /help /status",
        "ordinary message",
        "https://example.com/test / path/to/file",
        "```bash\nprintf '/help\\n'\n```",
    ],
)
def test_default_composer_send_forwards_text_unchanged(message: str) -> None:
    result = _run_send(message)

    assert [request["message"] for request in result["chatStarts"]] == [message]
    assert result["menuMatches"] == []
    assert result["newSessionCalls"] == 0


def test_legacy_webui_slash_interception_remains_explicitly_opt_in() -> None:
    result = _run_send("/new", intercept=True)

    assert result["chatStarts"] == []
    assert [match["name"] for match in result["menuMatches"]] == ["new"]
    assert result["newSessionCalls"] == 1


def test_app_shell_defaults_slash_interception_off() -> None:
    from api.config import load_settings

    assert load_settings()["intercept_slash_commands"] is False


class _JSONHandler:
    def __init__(self) -> None:
        self.status = None
        self.wfile = io.BytesIO()

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, _key: str, _value: str) -> None:
        pass

    def end_headers(self) -> None:
        pass


def test_chat_start_passes_slash_text_unchanged_to_runtime(monkeypatch, tmp_path) -> None:
    message = "/custom-command  argument1 /nested  "
    session = SimpleNamespace(
        session_id="slash-pass-through",
        title="Existing chat",
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        profile="default",
        messages=[],
        context_messages=[],
        pending_user_message=None,
    )
    captured = {}

    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        lambda *_args, **_kwargs: str(tmp_path),
    )
    monkeypatch.setattr(
        routes,
        "_read_profile_model_config",
        lambda *_args, **_kwargs: (None, "test-model", {}),
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", None, "test-model"),
    )

    def _start_run(_session, **kwargs):
        captured.update(kwargs)
        return {"stream_id": "stream-slash", "session_id": session.session_id}

    monkeypatch.setattr(routes, "_start_run", _start_run)

    handler = _JSONHandler()
    routes._handle_chat_start(
        handler,
        {"session_id": session.session_id, "message": message},
    )

    assert handler.status == 200
    assert captured["msg"] == message
