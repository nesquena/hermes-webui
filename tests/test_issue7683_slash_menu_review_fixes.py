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


def _run_send(command: str, script_body: str = "await send();", *, commands_api_error: bool = False, drop_dispatch_helper: bool = False, registry_override: list | None = None) -> dict:
    """Run the REAL send() from messages.js in a VM with the real COMMANDS
    table, a synthetic /api/commands registry, and instrumented api() that
    records every call path so the test can tell WHICH branch ran.

    ``commands_api_error`` makes the /api/commands registry fetch throw (a
    transient metadata failure). ``drop_dispatch_helper`` removes the real
    _isWebuiDispatchableAgentCommand helper from the shared realm so send()
    exercises the fallback non-dispatchability check. ``registry_override``
    replaces the synthetic registry payload for one run.
    """
    helper_drop_line = (
        'vm.runInContext("_isWebuiDispatchableAgentCommand = undefined;", ctx);'
        if drop_dispatch_helper
        else ""
    )
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
            if (path === '/api/commands' && {json.dumps(commands_api_error)}) throw new Error('registry unavailable');
            if (path === '/api/commands') return {{ commands: {json.dumps(registry_override if registry_override is not None else _REGISTRY_PAYLOAD)} }};
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
        {helper_drop_line}
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


def test_unknown_command_with_available_metadata_falls_through_to_chat():
    """#7683 finding A (Greptile P1: \"Unknown commands throw\"): a genuinely
    unknown slash token, with metadata loaded OK and no match, must keep its
    intended behaviour -- fall through to the normal agent round-trip
    (/api/chat/start) -- and must NOT throw on the dispatchability guard (the
    old ternary-precedence bug dereferenced _agentCmd.category while
    _agentCmd was null)."""
    out = _run_send(
        "/does-not-exist-xyz",
        """
        await send();
        return {};
        """,
    )
    assert "/api/chat/start" in out["apiCalls"], (
        "a genuinely unknown command must fall through to the normal chat "
        f"round-trip, got api calls: {out['apiCalls']} (regression on the "
        "ternary-precedence null dereference, #7683)."
    )


def test_metadata_unavailable_fails_closed_without_chat_round_trip():
    """#7683 finding B: when the /api/commands registry fetch fails
    (available:false), a known CLI-only command like /agents must NOT leak to
    /api/chat/start -- send() fails closed locally with a retryable
    metadata-unavailable message instead."""
    out = _run_send(
        "/agents",
        """
        await send();
        return {};
        """,
        commands_api_error=True,
    )
    assistant = [m for m in out["messages"] if m["role"] == "assistant"]
    assert any("temporarily unavailable" in m["content"] for m in assistant), (
        "a registry failure must fail closed with a retryable unavailable "
        f"message, got: {assistant!r}"
    )
    assert "/api/chat/start" not in out["apiCalls"], (
        "metadata-unavailable must fail closed -- /api/chat/start must not be "
        f"called, got api calls: {out['apiCalls']}"
    )


def test_helper_absent_fallback_blocks_non_dispatchable_without_throwing():
    """#7683 finding C: when the _isWebuiDispatchableAgentCommand helper is
    absent from the realm, the parenthesised fallback check must still block a
    non-dispatchable registry command (/agents) through the CLI-only
    explainer -- without throwing on _agentCmd.category."""
    out = _run_send(
        "/agents",
        """
        await send();
        return {};
        """,
        drop_dispatch_helper=True,
    )
    assistant = [m for m in out["messages"] if m["role"] == "assistant"]
    assert any("`/agents` is a Hermes CLI-only command" in m["content"] for m in assistant), (
        "the helper-absent fallback must explain /agents as CLI-only, "
        f"got: {assistant!r}"
    )
    assert "/api/chat/start" not in out["apiCalls"], (
        "/agents must not fall through to the model as plain text when the "
        "dispatch helper is absent."
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


# ---------------------------------------------------------------------------
# 2026-10-08 re-gate: the three findings the first round of tests missed
# ---------------------------------------------------------------------------


def _assistant_texts(result: dict) -> list[str]:
    return [m["content"] for m in result.get("messages", []) if m.get("role") == "assistant"]


def _failed_closed(result: dict) -> bool:
    """True when send() answered with the fail-closed 'unavailable' message."""
    return any(
        "temporarily unavailable" in text for text in _assistant_texts(result)
    )


def test_registry_unavailable_still_dispatches_the_backend_exec_family():
    """[CORE] with /api/commands down, the backend-exec family must still run.

    The fail-closed guard used to run BEFORE the dispatch branches, so a
    transient registry failure blocked `/reload-skills`, `/reload_skills`,
    `/reload_mcp`, `/codex_runtime` and `/credits` — commands master dispatches
    through `/api/commands/exec` without needing registry metadata.
    """
    for name in ("/reload-skills", "/reload_skills", "/reload_mcp", "/codex_runtime", "/credits"):
        result = _run_send(name, commands_api_error=True)
        assert not _failed_closed(result), (
            f"{name} was blocked by the fail-closed guard while the registry "
            f"was unavailable; master executes it. Got: {result}"
        )
        assert "/api/commands/exec" in result.get("apiCalls", []), (
            f"{name} never reached the backend-exec transport. Calls: "
            f"{result.get('apiCalls')}"
        )


def test_registry_unavailable_still_resolves_native_moa():
    """[CORE] `/moa` is a native branch that never consults the registry.

    Master reaches `/api/commands/moa/resolve` with `/api/commands` returning
    503, so the guard must exempt it explicitly.
    """
    result = _run_send("/moa explain quantum", commands_api_error=True)
    assert not _failed_closed(result), (
        f"/moa was blocked while the registry was unavailable; master resolves "
        f"it natively. Got: {result}"
    )
    assert "/api/commands/moa/resolve" in result.get("apiCalls", []), (
        f"/moa never reached its native resolver. Calls: {result.get('apiCalls')}"
    )


def test_registry_unavailable_fails_closed_for_an_unknown_command():
    """The guard must still fail closed where master does: an unknown token.

    This is the case the guard exists for — without it the token would leak to
    `/api/chat/start` as plain text.
    """
    result = _run_send("/does-not-exist", commands_api_error=True)
    assert _failed_closed(result), (
        f"an unknown command with the registry down must fail closed. Got: {result}"
    )
    assert "/api/chat/start" not in result.get("apiCalls", []), (
        "the unknown token leaked to /api/chat/start instead of failing closed"
    )


def test_registry_unavailable_fails_closed_for_a_non_dispatchable_command():
    """`/agents` is a real registry command that WebUI cannot dispatch."""
    result = _run_send(
        "/agents",
        commands_api_error=True,
        registry_override='[{"name":"agents","category":"Session","cli_only":false}]',
    )
    assert _failed_closed(result), (
        f"/agents with the registry down must fail closed. Got: {result}"
    )
    assert "/api/chat/start" not in result.get("apiCalls", [])


def test_available_registry_still_fails_closed_for_an_unknown_command():
    """The healthy path is unchanged: an unknown command still goes to chat."""
    result = _run_send("/does-not-exist")
    assert not _failed_closed(result)
    assert "/api/chat/start" in result.get("apiCalls", []), (
        f"an unknown command on the healthy path must fall through to chat. "
        f"Calls: {result.get('apiCalls')}"
    )


def test_failed_metadata_retry_does_not_refresh_the_dropdown():
    """[CORE] a FAILED registry fetch must not reopen a dismissed dropdown.

    `ensureSkillCommandsLoadedForAutocomplete()` runs on every composer input
    event, and its callback used to call `refreshSlashCommandDropdown()`
    unconditionally — including when the fetch threw. Reproduced in Chromium:
    after a registry failure, type `/new`, press Enter, release the retry with a
    503, press Enter again; master creates a chat, the old head left the session
    unchanged because the late refresh swallowed the Enter.
    """
    script = textwrap.dedent(
        """
        const COMMANDS_JS_SRC = __COMMANDS_JS__;
        const vm = require('vm');
        globalThis.refreshes = 0;
        globalThis.fetches = 0;
        const ctx = {
          console,
          Date, Math, JSON, Object, Array, String, Number, Boolean, RegExp,
          Error, Promise, setTimeout, clearTimeout,
          t: key => key,
          document: {
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: () => ({
              style: {}, dataset: {}, classList: { add() {}, remove() {} },
              appendChild() {}, addEventListener() {}, removeEventListener() {},
              setAttribute() {}, remove() {},
            }),
            addEventListener() {}, removeEventListener() {},
            body: { appendChild() {}, classList: { add() {}, remove() {} } },
          },
          window: {},
          localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
          location: { href: 'http://localhost:8787/', search: '' },
          navigator: { userAgent: 'node' },
          fetch: () => Promise.reject(new Error('registry unavailable')),
        };
        ctx.globalThis = ctx;
        ctx.window = ctx;
        vm.createContext(ctx);
        vm.runInContext(COMMANDS_JS_SRC, ctx);
        vm.runInContext(
            "refreshSlashCommandDropdown = function(){ globalThis.refreshes++; };", ctx);
        // One keystroke's worth of autocomplete preload, with the registry down.
        vm.runInContext(
            "ensureSkillCommandsLoadedForAutocomplete();", ctx);
        setTimeout(() => {
          console.log(JSON.stringify({ refreshes: globalThis.refreshes }));
        }, 50);
        """
    )
    script = script.replace("__COMMANDS_JS__", json.dumps(COMMANDS_JS))
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(script)
        path = Path(fh.name)
    try:
        proc = subprocess.run(
            ["node", str(path)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        path.unlink(missing_ok=True)
    assert payload["refreshes"] == 0, (
        "a failed /api/commands fetch refreshed the dropdown anyway, which "
        "reopens a dropdown the user dismissed and can swallow their Enter"
    )


def test_agent_metadata_retry_is_rate_limited_per_keystroke():
    """[CORE] a failing registry must not be refetched on every keystroke."""
    script = textwrap.dedent(
        """
        const COMMANDS_JS_SRC = __COMMANDS_JS__;
        const vm = require('vm');
        globalThis.fetches = 0;
        const ctx = {
          console,
          Date, Math, JSON, Object, Array, String, Number, Boolean, RegExp,
          Error, Promise, setTimeout, clearTimeout,
          t: key => key,
          document: {
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: () => ({
              style: {}, dataset: {}, classList: { add() {}, remove() {} },
              appendChild() {}, addEventListener() {}, removeEventListener() {},
              setAttribute() {}, remove() {},
            }),
            addEventListener() {}, removeEventListener() {},
            body: { appendChild() {}, classList: { add() {}, remove() {} } },
          },
          window: {},
          localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
          location: { href: 'http://localhost:8787/', search: '' },
          navigator: { userAgent: 'node' },
          t: key => key,
          $: () => null,
          refreshSlashCommandDropdown() {},
          api: (path) => {
            if (String(path).indexOf('/api/commands') === 0) globalThis.fetches++;
            return Promise.reject(new Error('down'));
          },
        };
        ctx.globalThis = ctx;
        ctx.window = ctx;
        vm.createContext(ctx);
        vm.runInContext(COMMANDS_JS_SRC, ctx);
        // Ten keystrokes in a row, as fast as a user can type.
        for (let i = 0; i < 10; i++) {
          vm.runInContext("ensureSkillCommandsLoadedForAutocomplete();", ctx);
        }
        setTimeout(() => {
          console.log(JSON.stringify({ fetches: globalThis.fetches }));
        }, 50);
        """
    )
    script = script.replace("__COMMANDS_JS__", json.dumps(COMMANDS_JS))
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(script)
        path = Path(fh.name)
    try:
        proc = subprocess.run(
            ["node", str(path)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        path.unlink(missing_ok=True)
    # Ten keystrokes must not produce ten fetches. Two is the honest floor:
    # loadBundleCommands() also awaits loadAgentCommandMetadata() on master (it
    # needs the registry to build bundle entries), so one keystroke legitimately
    # costs at most one fetch per loader. What the cooldown removes is the
    # per-keystroke refetch after a failure, which is what the reviewer counted
    # (3 fetches vs master's 1 in the degraded probe).
    assert payload["fetches"] <= 2, (
        f"10 keystrokes triggered {payload['fetches']} registry fetches; a "
        f"failing endpoint must be cooled down, not polled per keystroke"
    )


def test_mixed_arg_hint_is_not_double_wrapped():
    """[SHOULD-FIX] `<file> [options]` must not become `<<file> [options]>`."""
    script = textwrap.dedent(
        """
        const vm = require('vm');
        const ctx = { console, String, RegExp };
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        vm.runInContext(NORMALIZE_SRC, ctx);
        const cases = {
          'file': '<file>',
          '[options]': '[options]',
          '<file>': '<file>',
          '<file> [options]': '<file> [options]',
          '[a] [b]': '[a] [b]',
          '<a> <b>': '<a> <b>',
        };
        const out = {};
        for (const [input, expected] of Object.entries(cases)) {
          out[input] = vm.runInContext('normalizeArgHint', ctx)(input);
        }
        console.log(JSON.stringify(out));
        """
    )
    import re as _re

    m = _re.search(
        r"function normalizeArgHint\(hint\)\{.*?\n\}", COMMANDS_JS, _re.S
    )
    assert m, "normalizeArgHint not found in static/commands.js"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(script.replace("NORMALIZE_SRC", json.dumps(m.group(0))))
        path = Path(fh.name)
    try:
        proc = subprocess.run(
            ["node", str(path)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        path.unlink(missing_ok=True)
    assert payload["<file> [options]"] == "<file> [options]", (
        f"a mixed hint was double-wrapped: {payload['<file> [options]']!r}"
    )
    assert payload["file"] == "<file>"
    assert payload["[options]"] == "[options]"


# ── Round-2 finding 1: an enabled plain skill survives a registry outage ────


def test_enabled_skill_dispatches_when_registry_is_unavailable():
    """#7683 round-2 (CORE, static/messages.js:1705): with /api/commands
    returning 503, selecting an enabled plain skill from autocomplete must
    still submit /api/chat/start — the skill's metadata comes from
    /api/skills, so a registry outage says nothing about it. Master submits;
    the pre-fix head answered "metadata unavailable" and never sent."""
    out = _run_send(
        "/deep-research",
        """
        // The harness realm has no loadSkillCommands; provide the real
        // shape (/api/skills list). /deep-research is deliberately absent
        // from the synthetic registry payload, so only the skill lookup
        // can identify it.
        globalThis.loadSkillCommands = async () => [
          { name: 'deep-research', desc: 'Run a deep research pass' },
        ];
        await send();
        return {};
        """,
        commands_api_error=True,
    )
    assert "/api/chat/start" in out["apiCalls"], (
        "an enabled plain skill must still reach /api/chat/start when the "
        f"command registry is unavailable, got api calls: {out['apiCalls']}"
    )
    assistant = [m for m in out["messages"] if m["role"] == "assistant"]
    assert not any("temporarily unavailable" in m["content"] for m in assistant), (
        f"the skill must not be answered with metadata-unavailable: {assistant!r}"
    )


def test_unknown_command_still_fails_closed_when_registry_is_unavailable():
    """#7683 round-2 (control): the skill exemption must not open the leak the
    guard closes — a command that is NOT a skill must still fail closed with
    the retryable unavailable message and no chat round trip."""
    out = _run_send(
        "/not-a-real-command",
        """
        globalThis.loadSkillCommands = async () => [
          { name: 'deep-research', desc: 'Run a deep research pass' },
        ];
        await send();
        return {};
        """,
        commands_api_error=True,
    )
    assert "/api/chat/start" not in out["apiCalls"], (
        "a non-skill command must still fail closed while the registry is "
        f"unavailable, got api calls: {out['apiCalls']}"
    )
    assistant = [m for m in out["messages"] if m["role"] == "assistant"]
    assert any("temporarily unavailable" in m["content"] for m in assistant), (
        f"expected the retryable unavailable message, got: {assistant!r}"
    )


# ── Round-2 finding 2: bundle rows gate on the BUNDLE cache's readiness ─────


def test_bundle_rows_survive_a_registry_failure():
    """#7683 round-2 (SILENT, static/commands.js:286): a /api/commands failure
    leaves _agentCommandCacheReady=false while _bundleCommandCache is already
    populated. Bundle autocomplete rows must gate on the BUNDLE cache's
    readiness, or successfully loaded bundles vanish from the dropdown
    (Chromium: /release-bundle present on master, gone on the old head)."""
    import re as _re

    # Load the REAL commands.js wholesale (same shared-realm pattern as
    # _run_send) so every helper getMatchingCommands closes over is present,
    # then drive only the cache-state flags.
    script = textwrap.dedent(
        """
        const vm = require('vm');
        const ctx = {
          console, String, RegExp, Array, Object, Set, Map,
          window: { addEventListener() {}, requestAnimationFrame(cb) { return 1; } },
          document: {
            addEventListener() {},
            getElementById() { return null; },
            querySelector() { return null; },
          },
          localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
          t: key => key,
          setTimeout: () => 0, clearTimeout: () => 0,
          setInterval: () => 0, clearInterval: () => 0,
        };
        ctx.globalThis = ctx;
        ctx.window.window = ctx.window;
        vm.createContext(ctx);
        vm.runInContext(COMMANDS_JS, ctx);
        // Registry failed: agent cache not ready. Bundles loaded fine.
        vm.runInContext(
          `_agentCommandCacheReady = false;
           _bundleCommandCacheReady = true;
           _bundleCommandCache = [{ name: 'release-bundle', desc: 'Ship it', arg: '' }];
           _skillCommandCache = [];`,
          ctx
        );
        const out = vm.runInContext(`getMatchingCommands('release')`, ctx);
        console.log(JSON.stringify(out.map(m => m.name)));
        """
    ).replace("COMMANDS_JS", json.dumps(COMMANDS_JS))
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(script)
        path = Path(fh.name)
    try:
        proc = subprocess.run(
            ["node", str(path)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        names = json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        path.unlink(missing_ok=True)
    assert "release-bundle" in names, (
        "loaded bundles must stay in autocomplete when only the command "
        f"registry failed, got matches: {names!r}"
    )
