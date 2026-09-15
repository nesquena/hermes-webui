"""Tests for POST /api/learn and the /learn slash command.

Backend: real HTTP POSTs via TEST_BASE (gated by requires_agent_modules).
Frontend: behavioral tests that execute the REAL static/commands.js in node's
vm with mocked browser globals (following tests/test_cli_only_slash_commands.py
and tests/test_composer_capture_clear_race.py). No source-substring asserts:
each test invokes the live dispatcher/handler and checks what it DID.
"""
import json
import shutil
import subprocess
import tempfile
import textwrap
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import TEST_BASE, requires_agent_modules

ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _extract_js_function(source: str, name: str) -> str:
    """Slice a self-contained top-level `function name(...) {...}` from REAL JS source.

    String/comment-aware brace matching, so the returned text is the actual
    shipped implementation — not a copy. Raises (failing the test) when the
    function is absent, which is the pre-fix behavior for new helpers.
    """
    marker = "function %s(" % name
    start = source.find(marker)
    if start < 0:
        raise AssertionError("real JS function %s not found in source" % name)
    i = source.find("{", start)
    depth = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    break
                i += 1
        elif ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                i += 1
            continue
        elif ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise AssertionError("unbalanced braces extracting real JS function %s" % name)


# ── Backend: POST /api/learn route ────────────────────────────────────────────


@requires_agent_modules
def test_learn_endpoint_builds_prompt_from_request():
    payload = json.dumps({"request": "the release checklist workflow"}).encode()
    req = urllib.request.Request(
        f"{TEST_BASE}/api/learn", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    assert body["prompt"].startswith("[/learn]")
    assert "the release checklist workflow" in body["prompt"]


@requires_agent_modules
def test_learn_endpoint_empty_request_learns_from_conversation():
    payload = json.dumps({"request": ""}).encode()
    req = urllib.request.Request(
        f"{TEST_BASE}/api/learn", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    assert "this conversation" in body["prompt"]


# ── Frontend: behavioral harness (real commands.js in node vm) ─────────────────


def _node() -> str:
    exe = shutil.which("node")
    if not exe:  # pragma: no cover
        pytest.skip("node not available")
        raise RuntimeError("unreachable")
    return exe


def _run_learn_harness(api_js, test_js, composer_initial=""):
    """Load the REAL commands.js with mocked globals, run test_js, return its result.

    Mock browser state: S (session sid-1), $('msg') composer element backed by
    __state.composer, send()/showToast()/api() record into __sendCount/__sendOpts/
    __toasts/__calls. api_js is harness-scope JS (may close over S/state); test_js
    runs INSIDE the vm and must read results via the __-prefixed ctx globals.
    Dispatch ordering mirrors the real slash branch (static/messages.js send()):
    the handler is invoked WITHOUT await and the composer is cleared
    synchronously right after, so tests fire entry.fn(...) then set
    __state.composer='' before awaiting. Any non-empty composer observed after
    the api await is therefore a user draft typed mid-request. api mocks yield
    one microtask (await Promise.resolve()) before resolving so the
    synchronous dispatcher clear lands before the in-flight request settles —
    matching the real round-trip order.
    """
    node = _node()
    harness = (
        "const vm = require('vm');\n"
        "const __calls = [];\n"
        "const __toasts = [];\n"
        "const __sendCount = {n: 0};\n"
        "const __sendOpts = [];\n"
        "const __state = {composer: " + json.dumps(composer_initial) + "};\n"
        "const S = {session: {session_id: 'sid-1'}, pendingFiles: []};\n"
        "const composerEl = {};\n"
        "Object.defineProperty(composerEl, 'value', {"
        "get(){return __state.composer;}, set(v){__state.composer = v;}, configurable: true});\n"
        "async function __api(" + "path, opts" + ") {\n" + api_js + "\n}\n"
        "const ctx = {\n"
        "  console,\n"
        "  localStorage: {getItem(){return null;}, setItem(){}, removeItem(){}},\n"
        "  t: (key) => key,\n"
        "  S,\n"
        "  $: (id) => (id === 'msg' ? composerEl : null),\n"
        "  autoResize(){},\n"
        "  showToast(msg){ __toasts.push(String(msg)); },\n"
        "  send: async (opts) => { __sendCount.n++; __sendOpts.push(opts || null); },\n"
        "  api: __api,\n"
        "  __calls, __toasts, __sendCount, __sendOpts, __state,\n"
        "};\n"
        "vm.createContext(ctx);\n"
        "vm.runInContext(" + json.dumps(COMMANDS_JS) + ", ctx);\n"
        "(async () => {\n"
        "  const result = await vm.runInContext(\"(async () => { " + test_js + " })()\", ctx);\n"
        "  process.stdout.write(JSON.stringify(result));\n"
        "})().catch(err => { console.error(err && err.stack || err); process.exit(1); });\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(harness)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run([node, str(script_path)], check=True, capture_output=True, text=True, timeout=60)
    finally:
        script_path.unlink(missing_ok=True)
    return json.loads(proc.stdout)


HAPPY_API_JS = (
    "__calls.push({path, body: JSON.parse(opts.body)});\n"
    "await Promise.resolve();\n"
    "return {prompt: '[/learn] generated prompt'};"
)

# Fire the handler exactly like the real slash branch: no await, then the
# synchronous dispatcher clear (static/messages.js runs _cmd.fn(...) then
# sets $('msg').value='' without awaiting).
DISPATCH_JS = (
    "const entry = COMMANDS.find(c => c.name === 'learn');"
    " const p = entry.fn('my request');"
    " __state.composer = '';"
    " await p;"
)


def test_learn_dispatched_through_command_table():
    """/learn must resolve through the live dispatcher to cmdLearn (noEcho)."""
    result = _run_learn_harness(
        HAPPY_API_JS,
        "const entry = COMMANDS.find(c => c.name === 'learn');"
        " const parsed = parseCommand('/learn my request');"
        " return {found: !!entry, noEcho: !!(entry && entry.noEcho),"
        "  fnIsCmdLearn: !!(entry && entry.fn === cmdLearn),"
        "  handlerAlias: HANDLERS.learn === cmdLearn,"
        "  parsedName: parsed && parsed.name, parsedArgs: parsed && parsed.args};",
    )
    assert result == {
        "found": True,
        "noEcho": True,
        "fnIsCmdLearn": True,
        "handlerAlias": True,
        "parsedName": "learn",
        "parsedArgs": "my request",
    }


def test_cmd_learn_sends_prompt_through_chat_pipeline():
    """Happy path: POSTs the request, then submits prompt-as-payload with
    the /learn invocation as the display text (payload/display separation)."""
    result = _run_learn_harness(
        HAPPY_API_JS,
        DISPATCH_JS +
        " return {composer: __state.composer, sendCalls: __sendCount.n,"
        "  sendOpts: __sendOpts, toasts: __toasts, apiCalls: __calls};",
    )
    assert result["apiCalls"] == [{"path": "/api/learn", "body": {"request": "my request"}}]
    assert result["composer"] == "[/learn] generated prompt"
    assert result["sendCalls"] == 1
    assert result["sendOpts"] == [{"displayText": "/learn my request"}]
    assert result["sendOpts"][0]["displayText"] != result["composer"], (
        "the transcript row must show the /learn invocation, not the prompt"
    )
    assert result["toasts"] == []


def test_cmd_learn_preserves_draft_typed_during_request():
    """Race guard (P1): a draft typed while /api/learn is pending must survive.

    Fails on the pre-fix handler, which unconditionally overwrote the composer
    and submitted the prompt, silently discarding the user's draft.
    """
    result = _run_learn_harness(
        "__calls.push({path});"
        " await Promise.resolve();"
        " __state.composer = 'user draft typed during request';"
        " return {prompt: '[/learn] generated prompt'};",
        DISPATCH_JS +
        " return {composer: __state.composer, sendCalls: __sendCount.n,"
        "  sendOpts: __sendOpts, toasts: __toasts};",
    )
    assert result["composer"] == "user draft typed during request"
    assert result["sendCalls"] == 0
    assert result["sendOpts"] == []
    assert any("learn_composer_busy" in toast for toast in result["toasts"]), (
        "expected a composer-busy toast preserving the draft"
    )


def test_cmd_learn_aborts_when_session_changes_during_request():
    """The prompt must not be submitted into a different session than the caller's."""
    result = _run_learn_harness(
        "__calls.push({path});"
        " await Promise.resolve();"
        " S.session.session_id = 'sid-2';"
        " return {prompt: '[/learn] generated prompt'};",
        DISPATCH_JS +
        " return {composer: __state.composer, sendCalls: __sendCount.n,"
        "  sendOpts: __sendOpts, toasts: __toasts};",
    )
    assert result["composer"] == ""
    assert result["sendCalls"] == 0
    assert result["sendOpts"] == []
    assert any("learn_session_changed" in toast for toast in result["toasts"]), (
        "a session switch must abort with the session-changed toast, not the composer-busy one"
    )


def test_cmd_learn_empty_prompt_shows_toast_without_sending():
    result = _run_learn_harness(
        "return {};",
        "await cmdLearn('my request');"
        " return {composer: __state.composer, sendCalls: __sendCount.n, toasts: __toasts};",
    )
    assert result["sendCalls"] == 0
    assert any("learn_no_prompt" in toast for toast in result["toasts"])


def test_cmd_learn_without_session_does_not_call_api():
    result = _run_learn_harness(
        HAPPY_API_JS,
        "S.session = null; await cmdLearn('my request');"
        " return {sendCalls: __sendCount.n, toasts: __toasts, apiCalls: __calls};",
    )
    assert result["apiCalls"] == []
    assert result["sendCalls"] == 0
    assert any("no_active_session" in toast for toast in result["toasts"])


def test_learn_i18n_keys_resolve_in_en_locale():
    """The /learn strings must resolve from the live LOCALES runtime object."""
    node = _node()
    script = textwrap.dedent(
        """
        const vm = require('vm');
        // i18n.js runs loadLocale() at the top level, which touches
        // localStorage + document.documentElement — stub both.
        const ctx = {
          console,
          localStorage: {getItem(){return null;}, setItem(){}, removeItem(){}},
          document: {documentElement: {}, querySelectorAll(){return [];}},
        };
        vm.createContext(ctx);
        vm.runInContext(%s, ctx);
        const out = vm.runInContext("({cmd_learn: LOCALES.en.cmd_learn, learn_failed: LOCALES.en.learn_failed, learn_no_prompt: LOCALES.en.learn_no_prompt, learn_composer_busy: LOCALES.en.learn_composer_busy, learn_session_changed: LOCALES.en.learn_session_changed, resolved: t('learn_composer_busy')})", ctx);
        process.stdout.write(JSON.stringify(out));
        """
    ) % json.dumps(I18N_JS)
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run([node, str(script_path)], check=True, capture_output=True, text=True, timeout=60)
    finally:
        script_path.unlink(missing_ok=True)
    result = json.loads(proc.stdout)
    assert result["cmd_learn"], "LOCALES.en.cmd_learn must be non-empty"
    assert result["learn_failed"], "LOCALES.en.learn_failed must be non-empty"
    assert result["learn_no_prompt"], "LOCALES.en.learn_no_prompt must be non-empty"
    assert result["learn_composer_busy"], "LOCALES.en.learn_composer_busy must be non-empty"
    assert result["learn_session_changed"], "LOCALES.en.learn_session_changed must be non-empty"
    assert result["resolved"] == result["learn_composer_busy"], (
        "t('learn_composer_busy') must resolve through the live locale fallback chain"
    )


# ── Frontend: queued-turn display separation (real queue fns in node vm) ──────
#
# Greptile P2: a /learn turn queued while busy must keep its payload/display
# separation — the queued-message state must preserve the display override and
# the drain must pass it back into send(). Sibling paths in the same class:
# the /api/chat/start active-stream conflict retry (REAL _conflictRetryQueueEntry
# from static/messages.js) and the queue-chip surfaces (REAL
# _queuedEntryDisplayText + _applyQueuedEntryEdit from static/ui.js: chips show
# the invocation, and an in-place chip edit drops the stale override so the
# edited turn drains as user-authored plain text). Full send()/setBusy() cannot load
# in node vm (DOM-heavy files), so these tests execute the REAL extracted
# functions (_withDisplayOverride from static/messages.js, queueSessionMessage
# + shiftQueuedSessionMessage from static/ui.js) with stubbed storage, and run
# the /learn busy round-trip end-to-end through the REAL cmdLearn. The
# send-queue shim mirrors the concurrent/busy branch of the real send()
# (queue the composer payload via _withDisplayOverride on the hoisted option)
# and the drain snippet mirrors the real setBusy() drain (shift → composer →
# send through next.displayText); pinned line refs live in each docstring so
# drift is detectable by review.


def _run_queue_harness(test_js):
    """Run test_js with the REAL queue-state functions and stubbed storage.

    Stubs mirror the storage contract: _persistSessionQueueStorage JSON-rounds
    the queue (as the real sessionStorage/localStorage persist does), so a
    surviving displayText proves it is a plain persisted string, not a live
    reference.
    """
    node = _node()
    prelude = (
        "const __persisted = {};\n"
        "const SESSION_QUEUES = {};\n"
        "function _getSessionQueue(sid, create){\n"
        "  if(!SESSION_QUEUES[sid]){\n"
        "    if(!create) return [];\n"
        "    SESSION_QUEUES[sid] = [];\n"
        "    const raw = __persisted[sid];\n"
        "    if(raw){ try{ SESSION_QUEUES[sid] = JSON.parse(raw); }catch(_){} }\n"
        "  }\n"
        "  return SESSION_QUEUES[sid];\n"
        "}\n"
        "function _persistSessionQueueStorage(sid, q){ __persisted[sid] = JSON.stringify(q); }\n"
        "function _clearPersistedSessionQueue(sid){ delete __persisted[sid]; }\n"
        + _extract_js_function(MESSAGES_JS, "_withDisplayOverride") + "\n"
        + _extract_js_function(MESSAGES_JS, "_conflictRetryQueueEntry") + "\n"
        + _extract_js_function(UI_JS, "queueSessionMessage") + "\n"
        + _extract_js_function(UI_JS, "shiftQueuedSessionMessage") + "\n"
        + _extract_js_function(UI_JS, "_queuedEntryDisplayText") + "\n"
        + _extract_js_function(UI_JS, "_applyQueuedEntryEdit") + "\n"
    )
    harness = (
        "const vm = require('vm');\n"
        "const ctx = {console};\n"
        "vm.createContext(ctx);\n"
        "vm.runInContext(" + json.dumps(prelude) + ", ctx);\n"
        "const result = vm.runInContext(\"(function(){ " + test_js + " })()\", ctx);\n"
        "process.stdout.write(JSON.stringify(result));\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(harness)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run([node, str(script_path)], check=True, capture_output=True, text=True, timeout=60)
    finally:
        script_path.unlink(missing_ok=True)
    return json.loads(proc.stdout)


def test_with_display_override_merges_only_nonblank():
    """The REAL _withDisplayOverride keeps the override out unless non-blank."""
    result = _run_queue_harness(
        "const base = {text: 'GEN', files: [], model: 'm', model_provider: 'p', profile: 'd'};"
        " return {"
        "  merged: _withDisplayOverride(base, '/learn my request'),"
        "  blank: _withDisplayOverride(base, '   '),"
        "  missing: _withDisplayOverride(base),"
        "  nullPayload: _withDisplayOverride(null, '/learn x')};"
    )
    assert result["merged"] == {
        "text": "GEN", "files": [], "model": "m", "model_provider": "p",
        "profile": "d", "displayText": "/learn my request",
    }
    for key in ("blank", "missing"):
        assert result[key] == {
            "text": "GEN", "files": [], "model": "m", "model_provider": "p", "profile": "d",
        }, "a blank/missing override must leave the payload unchanged"
    assert result["nullPayload"] is None


def test_queue_state_preserves_display_override():
    """The REAL queue fns must carry displayText through enqueue→persist→shift."""
    result = _run_queue_harness(
        "const n0 = queueSessionMessage('sid-1',"
        " _withDisplayOverride({text: '[/learn] generated prompt', files: [],"
        "  model: 'm', model_provider: 'p', profile: 'default'}, '/learn my request'));"
        " const persisted = JSON.parse(__persisted['sid-1']);"
        " const next = shiftQueuedSessionMessage('sid-1');"
        " return {queued: n0, persistedEntry: persisted[0], drained: next,"
        "  queueGone: !('sid-1' in SESSION_QUEUES),"
        "  persistedCleared: !('sid-1' in __persisted)};"
    )
    assert result["queued"] == 1
    assert result["persistedEntry"]["text"] == "[/learn] generated prompt"
    assert result["persistedEntry"]["displayText"] == "/learn my request", (
        "the display override must survive the JSON persist round-trip"
    )
    assert result["drained"]["text"] == "[/learn] generated prompt"
    assert result["drained"]["displayText"] == "/learn my request"
    assert result["queueGone"] and result["persistedCleared"]


def test_conflict_retry_entry_keeps_display_override():
    """The REAL conflict-retry constructor preserves the payload/display split.

    Covers static/messages.js send()'s /api/chat/start active-stream catch:
    the retried turn must queue with the same one-shot override as the
    busy-queue branches, so the drained turn shows the /learn invocation
    instead of the generated prompt. Blank/missing override yields the exact
    pre-fix payload shape. Fails pre-fix (helper absent → extraction raises)."""
    result = _run_queue_harness(
        "const model = {model: 'm', model_provider: 'p'};"
        " const entry = _conflictRetryQueueEntry('[/learn] generated prompt',"
        "  model, 'default', '/learn my request');"
        " const plain = _conflictRetryQueueEntry('hello', model, 'default');"
        " const blank = _conflictRetryQueueEntry('[/learn] generated prompt',"
        "  model, 'default', '   ');"
        " queueSessionMessage('sid-1', entry);"
        " const persisted = JSON.parse(__persisted['sid-1']);"
        " const next = shiftQueuedSessionMessage('sid-1');"
        " return {entry, plain, blank,"
        "  persistedEntry: persisted[0], drained: next};"
    )
    assert result["entry"] == {
        "text": "[/learn] generated prompt", "files": [],
        "model": "m", "model_provider": "p", "profile": "default",
        "displayText": "/learn my request",
    }
    assert result["plain"] == {
        "text": "hello", "files": [],
        "model": "m", "model_provider": "p", "profile": "default",
    }, "a missing override must yield the exact pre-fix payload shape"
    assert "displayText" not in result["blank"], (
        "a blank override must not be stored on the queued entry"
    )
    assert result["persistedEntry"]["displayText"] == "/learn my request", (
        "the retried override must survive the JSON persist round-trip"
    )
    assert result["drained"]["text"] == "[/learn] generated prompt"
    assert result["drained"]["displayText"] == "/learn my request"


def test_queued_entry_display_text_prefers_override():
    """The REAL chip-label helper shows the invocation, not the prompt.

    Covers static/ui.js _renderQueueChips: a queued /learn turn must chip as
    its /learn invocation (what the drain will render), while plain and
    legacy-shaped entries chip as their payload. Fails pre-fix (absent)."""
    result = _run_queue_harness(
        "return {"
        " learn: _queuedEntryDisplayText({text: '[/learn] generated prompt',"
        "  displayText: '/learn my request'}),"
        " plain: _queuedEntryDisplayText({text: 'hello'}),"
        " blank: _queuedEntryDisplayText({text: '[/learn] generated prompt',"
        "  displayText: '  '}),"
        " legacy: _queuedEntryDisplayText({message: 'old shape'}),"
        " empty: _queuedEntryDisplayText(null)};"
    )
    assert result == {
        "learn": "/learn my request",
        "plain": "hello",
        "blank": "[/learn] generated prompt",
        "legacy": "old shape",
        "empty": "",
    }


def test_apply_queued_entry_edit_drops_stale_override():
    """An in-place chip edit re-targets the payload and drops the override.

    Covers the static/ui.js chip onblur save through the REAL helper: the
    edited turn is user-authored, so display==payload like any plain queued
    message and the drain cannot show a stale invocation for edited content.
    Fails pre-fix (absent)."""
    result = _run_queue_harness(
        "const edited = _applyQueuedEntryEdit("
        " {text: '[/learn] generated prompt', displayText: '/learn my request',"
        "  files: [], model: 'm', model_provider: 'p', profile: 'd',"
        "  _queued_at: 1}, 'user-edited text');"
        " const plain = _applyQueuedEntryEdit({text: 'a', model: 'm'}, 'b');"
        " return {edited, plain,"
        "  nullEntry: _applyQueuedEntryEdit(null, 'x')};"
    )
    assert result["edited"]["text"] == "user-edited text"
    assert "displayText" not in result["edited"], (
        "a stale invocation must not survive an edit of the queued payload"
    )
    assert result["edited"]["model"] == "m"
    assert result["edited"]["_queued_at"] == 1
    assert result["plain"] == {"text": "b", "model": "m"}
    assert result["nullEntry"] is None


def _run_learn_queue_harness(api_js, test_js):
    """REAL cmdLearn + REAL queue fns, with a send-shim for the busy branch.

    The shim mirrors static/messages.js send(): while __busy it queues the
    composer payload via the REAL _withDisplayOverride/queueSessionMessage;
    otherwise it records the submitted {payload, display} turn. The drain
    snippet in test_js mirrors static/ui.js setBusy(): shift the entry,
    restore the composer, send through next.displayText. The REAL extracted
    functions are evaluated once in a prelude vm context and shared by
    reference with both the main ctx and the Node-side send closure (a vm
    function keeps its own globals, and the storage stubs operate on the
    shared __persisted/SESSION_QUEUES objects).
    """
    node = _node()
    stubs = (
        "const __persisted = {};\n"
        "const SESSION_QUEUES = {};\n"
        "function _getSessionQueue(sid, create){\n"
        "  if(!SESSION_QUEUES[sid]){\n"
        "    if(!create) return [];\n"
        "    SESSION_QUEUES[sid] = [];\n"
        "    const raw = __persisted[sid];\n"
        "    if(raw){ try{ SESSION_QUEUES[sid] = JSON.parse(raw); }catch(_){} }\n"
        "  }\n"
        "  return SESSION_QUEUES[sid];\n"
        "}\n"
        "function _persistSessionQueueStorage(sid, q){ __persisted[sid] = JSON.stringify(q); }\n"
        "function _clearPersistedSessionQueue(sid){ delete __persisted[sid]; }\n"
    )
    real_fns = (
        _extract_js_function(MESSAGES_JS, "_withDisplayOverride") + "\n"
        + _extract_js_function(UI_JS, "queueSessionMessage") + "\n"
        + _extract_js_function(UI_JS, "shiftQueuedSessionMessage") + "\n"
    )
    harness = (
        "const vm = require('vm');\n"
        + stubs +
        "const __prelude = {console, __persisted, SESSION_QUEUES,"
        " _getSessionQueue, _persistSessionQueueStorage, _clearPersistedSessionQueue};\n"
        "vm.createContext(__prelude);\n"
        "vm.runInContext(" + json.dumps(real_fns) + ", __prelude);\n"
        "const queueSessionMessage = __prelude.queueSessionMessage;\n"
        "const shiftQueuedSessionMessage = __prelude.shiftQueuedSessionMessage;\n"
        "const _withDisplayOverride = __prelude._withDisplayOverride;\n"
        "const __calls = [];\n"
        "const __toasts = [];\n"
        "const __sendCount = {n: 0};\n"
        "const __sendOpts = [];\n"
        "const __sent = [];\n"
        "const __busy = {v: false};\n"
        "const __state = {composer: ''};\n"
        "const S = {session: {session_id: 'sid-1'}, pendingFiles: []};\n"
        "const composerEl = {};\n"
        "Object.defineProperty(composerEl, 'value', {"
        "get(){return __state.composer;}, set(v){__state.composer = v;}, configurable: true});\n"
        "async function __api(" + "path, opts" + ") {\n" + api_js + "\n}\n"
        "const ctx = {\n"
        "  console,\n"
        "  localStorage: {getItem(){return null;}, setItem(){}, removeItem(){}},\n"
        "  t: (key) => key,\n"
        "  S,\n"
        "  $: (id) => (id === 'msg' ? composerEl : null),\n"
        "  autoResize(){},\n"
        "  showToast(msg){ __toasts.push(String(msg)); },\n"
        "  send: async (opts) => {\n"
        "    __sendCount.n++; __sendOpts.push(opts || null);\n"
        "    if (__busy.v) {\n"
        "      queueSessionMessage('sid-1', _withDisplayOverride({text: __state.composer,"
        " files: [], model: 'm', model_provider: 'p', profile: 'default'}, opts && opts.displayText));\n"
        "      return;\n"
        "    }\n"
        "    __sent.push({payload: __state.composer, display: (opts && opts.displayText) || null});\n"
        "  },\n"
        "  api: __api,\n"
        "  queueSessionMessage, shiftQueuedSessionMessage, _withDisplayOverride,\n"
        "  __persisted, SESSION_QUEUES,\n"
        "  __calls, __toasts, __sendCount, __sendOpts, __sent, __busy, __state,\n"
        "};\n"
        "vm.createContext(ctx);\n"
        "vm.runInContext(" + json.dumps(COMMANDS_JS) + ", ctx);\n"
        "(async () => {\n"
        "  const result = await vm.runInContext(\"(async () => { " + test_js + " })()\", ctx);\n"
        "  process.stdout.write(JSON.stringify(result));\n"
        "})().catch(err => { console.error(err && err.stack || err); process.exit(1); });\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(harness)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run([node, str(script_path)], check=True, capture_output=True, text=True, timeout=60)
    finally:
        script_path.unlink(missing_ok=True)
    return json.loads(proc.stdout)


# Drain snippet mirroring the real setBusy() drain: shift the queued entry,
# restore the composer from its wire text, and re-send through its display
# override (plain queued text re-sends unchanged).
DRAIN_JS = (
    "const next = shiftQueuedSessionMessage('sid-1');"
    " __state.composer = (next && next.text) || '';"
    " __busy.v = false;"
    " await send((next && typeof next.displayText === 'string' && next.displayText.trim())"
    "  ? {displayText: next.displayText} : undefined);"
)


def test_learn_queued_while_busy_keeps_invocation_display():
    """Busy round-trip: the drained turn sends the prompt but shows /learn.

    Fails pre-fix: without _withDisplayOverride the queued entry carries no
    displayText (extraction itself fails), so the drained turn would display
    the internal generated prompt in the transcript.
    """
    result = _run_learn_queue_harness(
        HAPPY_API_JS,
        "__busy.v = true;"
        + DISPATCH_JS +
        DRAIN_JS +
        " return {sent: __sent, sendOpts: __sendOpts, toasts: __toasts};",
    )
    assert result["toasts"] == []
    assert result["sendOpts"] == [
        {"displayText": "/learn my request"},
        {"displayText": "/learn my request"},
    ], "both the queued submit and the drained re-send must carry the invocation"
    assert result["sent"] == [
        {"payload": "[/learn] generated prompt", "display": "/learn my request"}
    ]
    assert result["sent"][0]["payload"] != result["sent"][0]["display"]
