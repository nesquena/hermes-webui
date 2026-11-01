"""#8717: slash-command regressions from #7683 (exp-v0.52.611).

1. A CLI-only command whose metadata arrives after the user switched chats
   (or edited the composer) must not rewrite or persist another chat's
   composer and saved draft.
2. With the command registry unavailable, text whose first token cannot be a
   command (an absolute path) is sent as ordinary text, as on v0.52.544. A
   command-shaped token still fails closed, and keeps its text in the
   composer so "try again" is one keypress.

Drives the REAL send() from static/messages.js and the real COMMANDS table
from static/commands.js in a node VM. /api/commands can be held open so the
test can switch sessions or edit the composer while send() awaits metadata.
"""

import json
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)

_REGISTRY = [
    {"name": "plan", "description": "Write a plan", "category": "Session", "aliases": [], "cli_only": True, "args_hint": "<prompt>"},
    {"name": "agents", "description": "Manage background agents", "category": "Session", "aliases": ["tasks"], "cli_only": False},
]


def _run(command: str, script_body: str, *, registry_error: bool = False, start_session: bool = True) -> dict:
    session = "{ session_id: 'sid-1', title: 'A' }" if start_session else "null"
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const msgInput = {{
          value: {json.dumps(command)}, style: {{}}, scrollHeight: 0,
          addEventListener(){{}}, removeEventListener(){{}},
          focus(){{}}, blur(){{}}, setSelectionRange(){{}},
        }};
        const genericEl = {{
          addEventListener(){{}}, removeEventListener(){{}},
          classList: {{ add(){{}}, remove(){{}} }},
          style: {{}}, dataset: {{}}, value: '', textContent: '', innerHTML: '',
        }};
        const apiCalls = [];
        let releaseCommands = null;
        const commandsGate = new Promise(resolve => {{ releaseCommands = resolve; }});
        const ctx = {{
          console,
          window: {{ addEventListener(){{}}, requestAnimationFrame(cb){{ return 1; }} }},
          document: {{
            addEventListener(){{}},
            getElementById(id){{ return id === 'msg' ? msgInput : genericEl; }},
            querySelector(){{ return null; }},
          }},
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: key => key,
          S: {{
            busy: false,
            session: {session},
            pendingFiles: [], messages: [], activeProfile: 'default',
            activeStreamId: null, toolCalls: [],
          }},
          INFLIGHT: {{}}, _pendingSelections: [],
          _sendInProgress: false, _sendInProgressSid: null,
          _composerTextWithPendingSelections(){{ return msgInput.value; }},
          _flushSelectionBlocksToComposer(){{}},
          _dismissHandoffHint(){{}},
          _clearStaleBusyStateBeforeSend(){{ return false; }},
          _clearComposerAfterQueuedSelectionSend(){{}},
          _chatPayloadModelState(){{ return {{ model: '', model_provider: '' }}; }},
          queueSessionMessage(){{}}, updateQueueBadge(){{}}, renderTray(){{}},
          showToast(){{}}, renderMessages(){{}}, renderSessionList(){{}},
          autoResize(){{}}, hideCmdDropdown(){{}}, syncTopbar(){{}},
          setBusy(){{}}, setComposerStatus(){{}}, setStatus(){{}},
          updateSendBtn(){{}}, clearOptimisticSessionStreaming(){{}},
          clearLiveToolCards(){{}},
          appendThinking(){{}}, markInflight(){{}}, saveInflightState(){{}},
          startApprovalPolling(){{}}, startClarifyPolling(){{}},
          _fetchYoloState(){{}}, attachLiveStream(){{}},
          newSession: async () => {{ ctx.S.session = {{ session_id: 'sid-new', title: 'New' }}; }},
          $: id => id === 'msg' ? msgInput : genericEl,
          EventSource: function(){{ this.addEventListener=()=>{{}}; this.close=()=>{{}}; }},
          URL: function(){{ this.href=''; }},
          location: {{ href: 'http://localhost/', protocol: 'http:', host: 'localhost' }},
          setInterval: () => 0, clearInterval: () => 0,
          setTimeout: () => 0, clearTimeout: () => 0,
          renderSessionListFromCache(){{}},
          api: async (path, options) => {{
            apiCalls.push(path);
            if (path === '/api/commands') {{
              await commandsGate;
              if ({json.dumps(registry_error)}) throw new Error('registry unavailable');
              return {{ commands: {json.dumps(_REGISTRY)} }};
            }}
            if (path === '/api/commands/bundles') return {{ bundles: [] }};
            if (path === '/api/skills') return {{ skills: [] }};
            if (path === '/api/chat/start') return {{ stream_id: 's1' }};
            if (path === '/api/extensions/status') return {{ enabled: false, extensions: [] }};
            throw new Error('unexpected api path: ' + path);
          }},
        }};
        ctx.window.window = ctx.window;
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        vm.runInContext({json.dumps(MESSAGES_JS)}, ctx);
        ctx.__msgInput = msgInput;
        ctx.__release = () => releaseCommands();
        vm.runInContext(`
          globalThis.__saves = [];
          _handleComposerDraftPostFailure = () => {{}};
          _saveComposerDraftNow = (sid, text, files) => {{ __saves.push({{ sid, text }}); return Promise.resolve(); }};
        `, ctx);
        (async () => {{
          const result = await vm.runInContext(`(async () => {{
            const tick = async () => {{ for (let i = 0; i < 20; i++) await Promise.resolve(); }};
            {script_body}
          }})()`, ctx);
          process.stdout.write(JSON.stringify({{
            result,
            apiCalls,
            saves: ctx.__saves,
            messages: ctx.S.messages.map(m => ({{ role: m.role, content: String(m.content) }})),
            composer: msgInput.value,
            sid: ctx.S.session && ctx.S.session.session_id,
          }}));
        }})().catch(err => {{
          console.error(err && err.stack || err);
          process.exit(1);
        }});
        """
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        path = Path(handle.name)
    try:
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    finally:
        path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"node VM failed: {proc.stderr}")
    return json.loads(proc.stdout)


_SWITCH_DURING_METADATA = """
  const p = send();
  await tick();
  S.session = { session_id: 'sid-2', title: 'B' };
  __msgInput.value = 'B unrelated new draft';
  __release();
  await p; await tick();
  return {};
"""

_PLAIN = """
  const p = send();
  await tick();
  __release();
  await p; await tick();
  return {};
"""


@pytest.mark.parametrize("command", ["/plan A migration", "/agents"])
def test_switching_chats_during_metadata_leaves_the_other_chats_draft_alone(command):
    """#8717 (1): chat B's composer and saved draft are never written with the
    result of a command sent from chat A."""
    out = _run(command, _SWITCH_DURING_METADATA)
    assert out["sid"] == "sid-2"
    assert out["composer"] == "B unrelated new draft", out
    assert not [s for s in out["saves"] if s["sid"] == "sid-2"], (
        f"{command} sent from chat A persisted a draft for chat B: {out['saves']!r}"
    )


def test_editing_the_composer_during_metadata_keeps_the_edit():
    """#8717 (1): an edit made while metadata loads is the user's newer intent;
    the explainer must not replace it or persist over it."""
    body = """
      const p = send();
      await tick();
      __msgInput.value = 'actually, something else';
      __release();
      await p; await tick();
      return {};
    """
    out = _run("/plan A migration", body)
    assert out["composer"] == "actually, something else", out
    assert out["saves"] == [], out["saves"]


def test_unchanged_session_still_gets_the_reworded_prompt_and_draft():
    """The #7683 behaviour is kept when nothing changed during the wait."""
    out = _run("/plan A migration", _PLAIN)
    want = "Without executing anything yet, write an implementation plan for: A migration"
    assert out["composer"] == want, out
    assert out["saves"] == [{"sid": "sid-1", "text": want}], out["saves"]


def test_trailing_space_bare_command_still_clears_and_persists():
    """`/plan ` (picked from the dropdown, then sent) is trimmed to `/plan`;
    the ownership check must compare trimmed text."""
    out = _run("/plan ", _PLAIN)
    assert out["composer"] == "", out
    assert out["saves"] == [{"sid": "sid-1", "text": ""}], out["saves"]


def test_no_session_at_send_time_still_rewrites_the_new_session():
    """With no open session, send() creates one after the metadata wait; the
    composer still holds the command, so the explainer applies to it."""
    out = _run("/plan A migration", _PLAIN, start_session=False)
    want = "Without executing anything yet, write an implementation plan for: A migration"
    assert out["sid"] == "sid-new"
    assert out["composer"] == want, out
    assert out["saves"] == [{"sid": "sid-new", "text": want}], out["saves"]


@pytest.mark.parametrize(
    "text",
    [
        "/tmp/example.js explain",
        "/Users/me/notes/todo.md summarize this",
        "/home/user/project/app.py why does this fail?",
        "/etc/nginx/nginx.conf",
    ],
)
def test_registry_outage_still_sends_text_starting_with_a_path(text):
    """#8717 (2): a first token that cannot be a command name is ordinary text.
    v0.52.544 sent it with the registry down; .611 blocked it."""
    out = _run(text, _PLAIN, registry_error=True)
    assert "/api/chat/start" in out["apiCalls"], out["apiCalls"]
    assistant = [m["content"] for m in out["messages"] if m["role"] == "assistant"]
    assert not any("temporarily unavailable" in m for m in assistant), assistant


@pytest.mark.parametrize("command", ["/agents", "/does-not-exist-xyz now", "/plan A migration"])
def test_registry_outage_still_fails_closed_for_command_shaped_names_and_keeps_the_text(command):
    """The #7683 fail-closed guard still applies to command-shaped names, and
    the text stays in the composer so retrying is one keypress."""
    out = _run(command, _PLAIN, registry_error=True)
    assert "/api/chat/start" not in out["apiCalls"], out["apiCalls"]
    assistant = [m["content"] for m in out["messages"] if m["role"] == "assistant"]
    assert any("temporarily unavailable" in m for m in assistant), assistant
    assert out["composer"] == command, out
