"""Behavioural tests for the two #7683 review findings.

The static-source assertions in test_issue7675_slash_menu_polish.py prove the
right expressions exist in the source; these tests EXECUTE the real code paths
and assert the observable behaviour the reviewer asked for:

1. ``/moa <prompt>`` must reach its native MoA handler (the
   ``/api/commands/moa/resolve`` round-trip that arms ``_pendingMoaConfig``),
   NOT the CLI-only explainer, while a genuinely hidden registry command
   (``/agents``) still routes through the explainer.
2. ``cmdHelp()`` must render a single-bracketed arg hint (``<model_name>``),
   never doubled brackets (``<<model_name>>``).

Both are driven through a Node VM against the REAL static/commands.js +
static/messages.js in one shared realm, exactly as static/index.html loads
them (consecutive non-module classic scripts).
"""
from __future__ import annotations

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


# Registry payload mirrors the real hermes_cli registry for the commands under
# test: moa is category Session (NOT cli_only) and intentionally absent from
# messages.js's _AGENT_COMMANDS_RUN_ON_WEBUI because it has its own native
# branch in send(). /agents is a hidden registry command (neither
# backend-exec, plugin, nor WebUI-native).
_REGISTRY_PAYLOAD = [
    {
        "name": "moa",
        "description": "Mixture of Agents",
        "category": "Session",
        "aliases": [],
        "cli_only": False,
        "gateway_only": False,
        "args_hint": "<prompt>",
    },
    {
        "name": "agents",
        "description": "Manage background agents",
        "category": "Session",
        "aliases": ["tasks"],
        "cli_only": False,
        "gateway_only": False,
    },
    {
        "name": "reload-skills",
        "description": "Re-scan installed skills",
        "category": "Tools",
        "aliases": ["reload_skills"],
        "cli_only": False,
        "gateway_only": False,
    },
]


def _run_send(command: str, script_body: str = "") -> dict:
    """Run the REAL send() from messages.js in a VM with the real COMMANDS
    table, a synthetic /api/commands registry, and instrumented api() that
    records every call path so the test can tell WHICH branch ran.
    """
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
            session: {{ session_id: 'sid-1', title: 'New Chat' }},
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
          newSession: async () => {{}},
          $: id => id === 'msg' ? msgInput : genericEl,
          EventSource: function(){{ this.addEventListener=()=>{{}}; this.close=()=>{{}}; }},
          URL: function(){{ this.href=''; }},
          location: {{ href: 'http://localhost/', protocol: 'http:', host: 'localhost' }},
          setInterval: () => 0, clearInterval: () => 0,
          setTimeout: () => 0, clearTimeout: () => 0,
          renderSessionListFromCache(){{}},
          api: async (path, options) => {{
            apiCalls.push(path);
            if (path === '/api/commands') return {{ commands: {json.dumps(_REGISTRY_PAYLOAD)} }};
            if (path === '/api/commands/moa/resolve') {{
              return {{ usage: '/moa <prompt>', default_preset: 'p', preset: 'p' }};
            }}
            if (path === '/api/commands/exec') {{
              return {{ output: 'exec-ok' }};
            }}
            if (path === '/api/chat/start') return {{ stream_id: 's1' }};
            if (path === '/api/extensions/status') return {{ enabled: false, extensions: [] }};
            throw new Error('unexpected api path: ' + path);
          }},
        }};
        ctx.window.window = ctx.window;
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        vm.runInContext({json.dumps(MESSAGES_JS)}, ctx);
        (async () => {{
          const result = await vm.runInContext(`(async () => {{
            {script_body}
          }})()`, ctx);
          process.stdout.write(JSON.stringify({{
            result,
            apiCalls,
            messages: ctx.S.messages.map(m => ({{ role: m.role, content: String(m.content) }})),
            remainingInput: msgInput.value,
          }}));
        }})().catch(err => {{
          console.error(err && err.stack || err);
          process.exit(1);
        }});
        """
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=30)
    finally:
        script_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"node VM failed: {proc.stderr}")
    return json.loads(proc.stdout)


# ── Finding 1: /moa must reach its native handler, not the explainer ────────


def test_moa_reaches_native_handler_not_the_cli_only_explainer():
    """#7683 finding 1 (CORE): with normal registry metadata loaded (moa is
    category Session, NOT cli_only), `/moa <prompt>` must reach the native
    MoA handler -- observable as the /api/commands/moa/resolve round-trip --
    instead of being answered by the CLI-only explainer.
    """
    out = _run_send(
        "/moa explain quantum",
        """
        await send();
        return { pendingMoaConfig: String(typeof _pendingMoaConfig) };
        """,
    )
    # The native handler resolves MoA config server-side before rewriting text.
    assert "/api/commands/moa/resolve" in out["apiCalls"], (
        f"/moa never reached its native handler: api calls were {out['apiCalls']}. "
        "It was likely intercepted by the hidden-command explainer (#7683)."
    )
    # It must NOT be answered with the CLI-only explainer text.
    assistant = [m for m in out["messages"] if m["role"] == "assistant"]
    assert not any("CLI-only command" in m["content"] for m in assistant), (
        f"/moa was answered by the CLI-only explainer: {assistant!r} (#7683)."
    )
    # And it must NOT be sent down the generic backend-exec transport.
    assert "/api/commands/exec" not in out["apiCalls"], (
        "/moa must not be routed through the generic backend-exec transport -- "
        "it has its own native handler (#7683)."
    )


def test_hidden_agents_command_still_routes_through_explainer():
    """#7683: the fix must not open the leak it was closing -- a genuinely
    hidden registry command (/agents) must still be explained as CLI-only
    instead of falling through to the model as plain text.
    """
    out = _run_send(
        "/agents",
        """
        await send();
        return {};
        """,
    )
    assistant = [m for m in out["messages"] if m["role"] == "assistant"]
    assert any("`/agents` is a Hermes CLI-only command" in m["content"] for m in assistant), (
        f"/agents must be answered by the CLI-only explainer, got: {assistant!r}"
    )
    assert "/api/chat/start" not in out["apiCalls"], (
        "/agents must not fall through to the model as plain text."
    )


def test_moa_without_args_still_shows_native_usage():
    """#7683: bare `/moa` must keep hitting the native usage branch (which
    asks /api/commands/moa/resolve for its usage string), not the explainer.
    """
    out = _run_send(
        "/moa",
        """
        await send();
        return {};
        """,
    )
    assert "/api/commands/moa/resolve" in out["apiCalls"]
    assistant = [m for m in out["messages"] if m["role"] == "assistant"]
    assert assistant and assistant[0]["content"] == "/moa <prompt>", (
        f"bare /moa must show the native usage string, got {assistant!r}"
    )


def test_backend_exec_command_still_dispatches_normally():
    """#7683: the predicate change must not disturb the backend-exec family
    (/reload-skills) -- it still goes through /api/commands/exec.
    """
    out = _run_send(
        "/reload-skills",
        """
        await send();
        return {};
        """,
    )
    assert "/api/commands/exec" in out["apiCalls"], (
        f"/reload-skills must still dispatch via the backend-exec transport: "
        f"{out['apiCalls']}"
    )


# ── Finding 2: /help must render single-bracketed arg hints ─────────────────


def _run_cmd_help() -> str:
    """Execute the REAL cmdHelp() from commands.js and return the rendered
    assistant message content."""
    out = _run_send("", "cmdHelp();")
    assistant = [m for m in out["messages"] if m["role"] == "assistant"]
    assert assistant, "cmdHelp pushed no assistant message"
    return assistant[0]["content"]


def test_help_renders_single_bracketed_model_arg_hint():
    """#7683 finding 2 (SILENT): the COMMANDS table already stores bracketed
    hints, so /help must render `<model_name>` -- never `<<model_name>>`.
    """
    content = _run_cmd_help()
    assert "/model <model_name>" in content, (
        f"/help must render the model row with a single-bracketed arg hint. "
        f"Got:\n{content}"
    )
    assert "<<model_name>>" not in content, (
        f"/help doubled the angle brackets around the arg hint (#7683). Got:\n{content}"
    )


def test_help_renders_no_doubled_brackets_anywhere():
    """#7683 finding 2: the regression is not specific to /model -- no row may
    render `<<...>>` for a required arg or `[[...]]` for an optional one.
    """
    content = _run_cmd_help()
    for line in content.splitlines():
        assert "<<" not in line, f"doubled angle brackets in /help line: {line!r}"
        assert "[[" not in line, f"doubled square brackets in /help line: {line!r}"


def test_help_renders_optional_args_with_single_square_brackets():
    """#7683: optional args keep their single `[optional]` rendering."""
    content = _run_cmd_help()
    assert "/compress [focus topic]" in content, (
        f"/help must render optional args as `[focus topic]`. Got:\n{content}"
    )
