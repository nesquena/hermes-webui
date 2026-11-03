"""#7781 (messages.js): picking a plugin skill from the slash picker must load
the skill into that turn.

The #7770 picker offers plugin-registered skills as qualified
``/namespace:skill`` entries. Those names have no COMMANDS entry and no bundle
entry, so ``send()`` used to forward the literal text as a chat message and the
skill body never reached the agent (maintainer finding #2, CORE).

This suite drives the REAL ``send()`` (static/messages.js) — with
static/commands.js loaded into the same node ``vm`` — and mocks
``/api/skills`` / ``/api/skills/content``. It pins what lands in the
``/api/chat/start`` POST body:

* with a matching plugin row: the skill body + the typed instruction after the
  command + the ``[USER OVERRIDE]`` directive ride on the dispatched message,
  while the user message echoed into ``S.messages`` (and the composer restore)
  keeps the ORIGINAL qualified command text;
* with no matching plugin row: the qualified command falls through unchanged —
  the dispatched message is the literal text and no skill content is fetched.

The harness stops at the dispatch point: the mocked ``/api/chat/start`` records
the pending POST body and then throws so ``send()`` unwinds through its own
error path instead of wiring the real SSE stream.

The suite needs neither the agent module nor a live server (same harness style
as test_issue7509_disabled_skills_slash_picker.py), so it deliberately carries
no ``requires_agent_modules`` marker.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")

PLUGIN_ROW = {
    "name": "demo:skill",
    "description": "A plugin-registered skill",
    "category": "plugin",
    "plugin": "demo",
    "source": "plugin",
    "disabled": False,
}

# The plugin manager also accepts underscore-qualified names. The picker
# must insert this exact registered spelling (PR #7781 re-gate finding 1),
# and send() resolves qualified names verbatim against the registry.
UNDERSCORE_ROW = {
    "name": "reviewdemo:under_score",
    "description": "An underscore-qualified plugin skill",
    "category": "plugin",
    "plugin": "reviewdemo",
    "source": "plugin",
    "disabled": False,
}

DIRECTORY_ROW = {
    "name": "local-skill",
    "description": "A directory-installed skill",
    "category": None,
    "disabled": False,
}

# The api stub below is deliberately permissive: every unexpected path returns
# {} so a stray send()-path dependency (draft persists, approval polls, ...)
# degrades instead of crashing the harness. Only the paths the assertions care
# about are load-bearing.
_HARNESS = """\
const vm = require('vm');

const apiCalls = [];
let skillsPayload = [];
const skillContentByName = {};
// #7781 should-fix knob: make the /api/skills listing fetch reject.
let skillsListingShouldFail = false;
// #7781 [CORE] knobs: park a fetch on a promise the test settles only AFTER
// switching the viewed session — the cross-chat race the finding describes.
const deferredFetch = { target: null, resolve: null, reject: null };

const elementStub = () => ({
  value: '',
  style: { removeProperty(){}, setProperty(){} },
  dataset: {},
  classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
  addEventListener(){}, removeEventListener(){},
  appendChild(){}, setAttribute(){}, removeAttribute(){},
  focus(){}, blur(){},
  scrollTop: 0, scrollHeight: 0, offsetHeight: 0, offsetWidth: 0,
  innerHTML: '', textContent: '', children: [],
  remove(){}, closest(){ return null; },
  querySelector(){ return null; }, querySelectorAll(){ return []; },
  getAttribute(){ return null; }, hasAttribute(){ return false; },
  parentElement: null, parentNode: null,
});

const elementCache = {};
const getEl = (id) => {
  if (!elementCache[id]) elementCache[id] = elementStub();
  return elementCache[id];
};

const documentStub = {
  baseURI: 'http://localhost/',
  hidden: false,
  visibilityState: 'visible',
  addEventListener(){}, removeEventListener(){},
  getElementById: getEl,
  querySelector(){ return null; },
  querySelectorAll(){ return []; },
  createElement(){ return elementStub(); },
  body: elementStub(),
  documentElement: elementStub(),
};

const windowStub = {
  addEventListener(){}, removeEventListener(){},
  location: { href: 'http://localhost/', origin: 'http://localhost' },
  navigator: { userAgent: 'node' },
  innerWidth: 800, innerHeight: 600,
  scrollTo(){},
  matchMedia(){ return { matches: false, addEventListener(){} }; },
  localStorage: { getItem(){ return null; }, setItem(){}, removeItem(){} },
};

const ctx = {
  console,
  document: documentStub,
  window: windowStub,
  location: windowStub.location,
  navigator: windowStub.navigator,
  localStorage: windowStub.localStorage,
  EventSource: class { constructor(){ this.readyState = 0; } close(){} addEventListener(){} },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  crypto: { randomUUID: () => 'uuid-' + Math.random() },
  requestAnimationFrame: (cb) => setTimeout(cb, 0),
  cancelAnimationFrame: (id) => clearTimeout(id),
  setTimeout, clearTimeout, setInterval, clearInterval,
  URL, URLSearchParams, TextEncoder, TextDecoder,
  getComputedStyle: () => ({ minHeight: '0px', lineHeight: '0px', paddingTop: '0px', paddingBottom: '0px', borderTopWidth: '0px', borderBottomWidth: '0px' }),
  t: (k) => k,
  $: (id) => documentStub.getElementById(id),
  showToast(){},
  newSession: async () => {},
  renderMessages(){},
  setBusy(){},
  setComposerStatus(){},
  uploadPendingFiles: async () => [],
  clearLiveToolCards(){},
  appendThinking(){},
  saveInflightState(){},
  renderTray(){},
  updateSendBtn(){},
  ensureLiveWorklogShell(){},
  upsertActiveSessionForLocalTurn(){},
  renderSessionListFromCache(){},
  _clearComposerDraft: async () => {},
  removeThinking(){},
  history: { replaceState(){} },
  S: { session: { session_id: 'sess-1' }, pendingFiles: [], messages: [], toolCalls: [] },
  INFLIGHT: {},
  api: async (path, opts) => {
    apiCalls.push({ path, opts: opts || null });
    if (path === '/api/skills') {
      if (skillsListingShouldFail) throw new Error('skills listing unavailable');
      if (deferredFetch.target === 'listing') {
        return new Promise((resolve, reject) => {
          deferredFetch.resolve = () => resolve({ skills: skillsPayload });
          deferredFetch.reject = reject;
        });
      }
      return { skills: skillsPayload };
    }
    if (path === '/api/commands') return { commands: [] };
    if (path === '/api/commands/bundles') return { bundles: [] };
    if (path.indexOf('/api/skills/content?name=') === 0) {
      const name = decodeURIComponent(path.slice('/api/skills/content?name='.length));
      if (deferredFetch.target === 'content') {
        return new Promise((resolve, reject) => {
          deferredFetch.resolve = () => resolve({ content: skillContentByName[name] || '' });
          deferredFetch.reject = reject;
        });
      }
      return { content: skillContentByName[name] || '' };
    }
    if (path === '/api/chat/start') {
      // Dispatch point reached and recorded (startCall in the runner reads the
      // captured body). Stop here instead of wiring the real SSE stream.
      throw new Error('stubbed /api/chat/start dispatch');
    }
    return {};
  },
  // Test-only knobs, same idiom as the other node-vm harnesses.
  __setSkills(next) { skillsPayload = next; },
  __setSkillContent(name, content) { skillContentByName[name] = content; },
  __clearSkillContent() { for (const k in skillContentByName) delete skillContentByName[k]; },
  __resetApiCalls() { apiCalls.length = 0; },
  __apiCalls() { return apiCalls; },
  __setComposer(text) { getEl('msg').value = text; },
  __composer() { const el = elementCache['msg']; return el ? el.value : null; },
  __failSkillsListing(v) { skillsListingShouldFail = !!v; },
  __deferFetch(target) { deferredFetch.target = target; deferredFetch.resolve = null; deferredFetch.reject = null; },
  __settleFetch(mode, message) {
    const settle = deferredFetch.resolve;
    const fail = deferredFetch.reject;
    deferredFetch.target = null; deferredFetch.resolve = null; deferredFetch.reject = null;
    if (mode === 'error' && fail) fail(new Error(message || 'deferred fetch failed'));
    else if (settle) settle();
  },
  // Simulate the user opening another chat (with its own composer draft)
  // while a send is parked on an await. S lives inside the vm context, so
  // the outer-scope knob must reach it through the context object.
  __switchSession(sid, draft) { ctx.S.session = { session_id: sid }; ctx.S.messages = []; getEl('msg').value = draft; },
  __messages() { return ctx.S.messages; },
};
vm.createContext(ctx);
vm.runInContext(__COMMANDS_JS__, ctx);
vm.runInContext(__MESSAGES_JS__, ctx);

(async () => {
  try {
    const result = await vm.runInContext(`(async () => {
      const run = async (composerText, skills, contentMap) => {
        __setComposer(composerText);
        __setSkills(skills);
        __clearSkillContent();
        for (const k in (contentMap || {})) __setSkillContent(k, contentMap[k]);
        __resetApiCalls();
        S.messages = [];
        INFLIGHT = {};
        await send(composerText);
        const startCall = __apiCalls().find((c) => c && c.path === '/api/chat/start');
        return {
          startBody: startCall ? JSON.parse(startCall.opts.body) : null,
          userMessage: S.messages.find((m) => m && m.role === 'user') || null,
          skillsFetches: __apiCalls().filter((c) => c && c.path === '/api/skills').length,
          contentFetches: __apiCalls()
            .filter((c) => c && c.path.indexOf('/api/skills/content?name=') === 0)
            .map((c) => c.path),
          composerAfter: __composer(),
        };
      };
      // #7781 [CORE] runner: park the content (or listing) fetch, switch the
      // viewed session mid-flight with a draft typed there, THEN settle the
      // fetch. Whatever send() does next must belong to the chat that issued
      // the command only.
      const runDeferredSwitch = async (composerText, skills, contentMap, opts) => {
        __setComposer(composerText);
        __setSkills(skills);
        __clearSkillContent();
        for (const k in (contentMap || {})) __setSkillContent(k, contentMap[k]);
        __resetApiCalls();
        __failSkillsListing(false);
        S.session = { session_id: opts.ownerSid };
        S.messages = [];
        INFLIGHT = {};
        __deferFetch(opts.defer);
        let thrown = null;
        const sendPromise = send(composerText).catch((e) => { thrown = String((e && e.message) || e); });
        // Wait until send() has actually reached the deferred fetch.
        for (let i = 0; i < 200; i++) {
          const reached = __apiCalls().some((c) => c && (opts.defer === 'listing'
            ? c.path === '/api/skills'
            : c.path.indexOf('/api/skills/content?name=') === 0));
          if (reached) break;
          await new Promise((r) => setTimeout(r, 5));
        }
        // The user opens another chat and types a draft there.
        __switchSession(opts.switchToSid, opts.draft);
        __settleFetch(opts.mode, opts.errorMessage);
        await sendPromise;
        const startCall = __apiCalls().find((c) => c && c.path === '/api/chat/start');
        return {
          startBody: startCall ? JSON.parse(startCall.opts.body) : null,
          messages: S.messages,
          composerAfter: __composer(),
          contentFetches: __apiCalls()
            .filter((c) => c && c.path.indexOf('/api/skills/content?name=') === 0)
            .map((c) => c.path),
          thrown,
        };
      };
      // #7781 should-fix runner: the /api/skills listing fetch rejects with
      // NO session switch. Master forwards the literal text; the buggy single
      // try answered "Plugin skill error" and swallowed the command.
      const runListingFailure = async (composerText, skills) => {
        __setComposer(composerText);
        __setSkills(skills);
        __clearSkillContent();
        __resetApiCalls();
        __failSkillsListing(true);
        S.session = { session_id: 'sess-1' };
        S.messages = [];
        INFLIGHT = {};
        let thrown = null;
        await send(composerText).catch((e) => { thrown = String((e && e.message) || e); });
        __failSkillsListing(false);
        const startCall = __apiCalls().find((c) => c && c.path === '/api/chat/start');
        return {
          startBody: startCall ? JSON.parse(startCall.opts.body) : null,
          messages: S.messages,
          composerAfter: __composer(),
          contentFetches: __apiCalls()
            .filter((c) => c && c.path.indexOf('/api/skills/content?name=') === 0)
            .map((c) => c.path),
          thrown,
        };
      };
      const _positive = await run('/demo:skill do X', [__PLUGIN_ROW__], { 'demo:skill': 'DEMO SKILL BODY' });
      const _negative = await run('/demo:skill', [__DIRECTORY_ROW__], {});
      // PR #7781 re-gate finding 1 controls: canonical underscore spelling
      // must dispatch with the skill body; the ordinary hyphen spelling and
      // an unknown qualified command must fall through unchanged (no fetch,
      // no directive) because neither is a registered identity.
      const _underscore = await run('/reviewdemo:under_score do X', [__UNDERSCORE_ROW__], { 'reviewdemo:under_score': 'UNDERSCORE SKILL BODY' });
      const _hyphen = await run('/reviewdemo:under-score do X', [__UNDERSCORE_ROW__], {});
      const _unknown = await run('/reviewdemo:missing do X', [__UNDERSCORE_ROW__], {});
      // Full picker-entry -> send() chain: the entry the picker would build
      // for the underscore row supplies the composer text inserted on pick,
      // and that exact text must resolve back to the registered skill.
      const _pickedEntry = _buildSkillCommandEntry(__UNDERSCORE_ROW__);
      const _entryName = _pickedEntry && _pickedEntry.name;
      const _picked = await run('/' + _entryName + ' do Y', [__UNDERSCORE_ROW__], { 'reviewdemo:under_score': 'UNDERSCORE SKILL BODY' });
      // #7781 re-gate CORE: the cross-chat race. A fetch parks the send, the
      // user switches to another chat (with a draft typed there), and only
      // then does the fetch settle. success + error paths for the content
      // fetch, plus the listing-fetch variant (the should-fix fall-through
      // must honour the owner guard too).
      const _switchSuccess = await runDeferredSwitch('/demo:skill do X', [__PLUGIN_ROW__], { 'demo:skill': 'DEMO SKILL BODY' }, { defer: 'content', ownerSid: 'sess-A', switchToSid: 'sess-B', draft: 'chat B draft', mode: 'success' });
      const _switchError = await runDeferredSwitch('/demo:skill do X', [__PLUGIN_ROW__], { 'demo:skill': 'DEMO SKILL BODY' }, { defer: 'content', ownerSid: 'sess-A', switchToSid: 'sess-B', draft: 'chat B draft', mode: 'error', errorMessage: 'content fetch failed' });
      const _switchListingFail = await runDeferredSwitch('/demo:skill do X', [__PLUGIN_ROW__], { 'demo:skill': 'DEMO SKILL BODY' }, { defer: 'listing', ownerSid: 'sess-A', switchToSid: 'sess-B', draft: 'chat B draft', mode: 'error', errorMessage: 'skills listing unavailable' });
      const _listingFailure = await runListingFailure('/demo:skill do X', [__PLUGIN_ROW__]);
      return {
        positive: _positive,
        negative: _negative,
        underscore: _underscore,
        hyphen: _hyphen,
        unknown: _unknown,
        picked: { entryName: _entryName, run: _picked },
        switchSuccess: _switchSuccess,
        switchError: _switchError,
        switchListingFail: _switchListingFail,
        listingFailure: _listingFailure,
      };
    })()`, ctx);
    process.stdout.write(JSON.stringify(result), () => process.exit(0));
  } catch (err) {
    console.error(err && err.stack || err);
    process.exit(1);
  }
})();
"""


def _run_send_harness() -> dict:
    script = (
        _HARNESS.replace("__COMMANDS_JS__", json.dumps(COMMANDS_JS, ensure_ascii=False))
        .replace("__MESSAGES_JS__", json.dumps(MESSAGES_JS, ensure_ascii=False))
        .replace("__PLUGIN_ROW__", json.dumps(PLUGIN_ROW))
        .replace("__UNDERSCORE_ROW__", json.dumps(UNDERSCORE_ROW))
        .replace("__DIRECTORY_ROW__", json.dumps(DIRECTORY_ROW))
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=120)
    finally:
        script_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"node harness failed (exit {proc.returncode}):\n{proc.stderr}\n{proc.stdout}"
        )
    return json.loads(proc.stdout)


def test_plugin_skill_command_loads_skill_into_dispatched_message():
    """send('/demo:skill do X') with a matching plugin row must attach the skill
    body to the dispatched message: the /api/chat/start body carries the
    [USER OVERRIDE] directive, the [FORCED SKILL CONTEXT] block with the body,
    AND the typed instruction 'do X' — while the user message echo keeps the
    original '/demo:skill do X' text (maintainer finding #2)."""
    result = _run_send_harness()["positive"]
    assert result["startBody"] is not None, result
    message = result["startBody"]["message"]
    assert "DEMO SKILL BODY" in message, result
    assert "do X" in message, result
    assert "[USER OVERRIDE]" in message, result
    assert "You MUST follow the skill 'demo:skill' content provided below" in message, result
    assert "[FORCED SKILL CONTEXT: demo:skill]" in message, result
    # The echo/display path must keep the ORIGINAL qualified command text: the
    # user bubble is the override display text, and the failed-start restore put
    # the same original back into the composer.
    assert result["userMessage"] is not None and result["userMessage"]["content"] == "/demo:skill do X", result
    assert result["composerAfter"] == "/demo:skill do X", result
    # The skill body must be fetched exactly once, for the canonical name.
    assert result["contentFetches"] == ["/api/skills/content?name=demo%3Askill"], result


def test_qualified_command_without_plugin_row_falls_through_unchanged():
    """send('/demo:skill') with NO matching plugin row must not crash and must
    forward the literal text as before: the /api/chat/start body is exactly the
    original text, no skill content is fetched, and no directive is attached."""
    result = _run_send_harness()["negative"]
    assert result["startBody"] is not None, result
    assert result["startBody"]["message"] == "/demo:skill", result
    assert "DEMO SKILL BODY" not in result["startBody"]["message"], result
    assert result["contentFetches"] == [], result
    assert result["userMessage"] is not None and result["userMessage"]["content"] == "/demo:skill", result


def test_canonical_underscore_qualified_name_dispatches_with_skill_body():
    """Control (canonical underscore): send('/reviewdemo:under_score do X')
    with the underscore-qualified row registered must attach the skill body —
    the picker's inserted text resolves because it matches the registry name
    verbatim (PR #7781 re-gate finding 1)."""
    result = _run_send_harness()["underscore"]
    assert result["startBody"] is not None, result
    message = result["startBody"]["message"]
    assert "UNDERSCORE SKILL BODY" in message, result
    assert "do X" in message, result
    assert "[USER OVERRIDE]" in message, result
    assert "You MUST follow the skill 'reviewdemo:under_score' content provided below" in message, result
    # The content fetch must target the canonical underscore identity.
    assert result["contentFetches"] == ["/api/skills/content?name=reviewdemo%3Aunder_score"], result


def test_ordinary_hyphen_spelling_falls_through_unchanged():
    """Control (ordinary hyphen): send('/reviewdemo:under-score do X') must NOT
    resolve to the registered 'reviewdemo:under_score' — a hyphen spelling is
    not a registry identity. It falls through as literal chat: no fetch, no
    directive (PR #7781 re-gate finding 1)."""
    result = _run_send_harness()["hyphen"]
    assert result["startBody"] is not None, result
    assert result["startBody"]["message"] == "/reviewdemo:under-score do X", result
    assert "UNDERSCORE SKILL BODY" not in result["startBody"]["message"], result
    assert result["contentFetches"] == [], result
    assert result["userMessage"] is not None and result["userMessage"]["content"] == "/reviewdemo:under-score do X", result


def test_unknown_qualified_command_falls_through_unchanged():
    """Control (unknown command): send('/reviewdemo:missing do X') with no
    matching row must forward the literal text and fetch nothing — the plugin
    lookup must never rewrite unknown qualified commands."""
    result = _run_send_harness()["unknown"]
    assert result["startBody"] is not None, result
    assert result["startBody"]["message"] == "/reviewdemo:missing do X", result
    assert result["contentFetches"] == [], result
    assert result["userMessage"] is not None and result["userMessage"]["content"] == "/reviewdemo:missing do X", result


def test_picker_entry_for_underscore_name_feeds_send_verbatim():
    """Full picker-entry -> send() chain: _buildSkillCommandEntry must emit
    the registered underscore identity as the entry name (the composer text a
    pick inserts is '/<entry.name>'), and sending that exact text must resolve
    back to the plugin skill and attach its body (PR #7781 re-gate finding 1)."""
    result = _run_send_harness()["picked"]
    assert result["entryName"] == "reviewdemo:under_score", result
    run_result = result["run"]
    assert run_result["startBody"] is not None, result
    message = run_result["startBody"]["message"]
    assert "UNDERSCORE SKILL BODY" in message, result
    assert "do Y" in message, result
    assert "[USER OVERRIDE]" in message, result
    assert run_result["contentFetches"] == ["/api/skills/content?name=reviewdemo%3Aunder_score"], result
    # The user-message echo keeps the ORIGINAL composer text (with underscore).
    assert run_result["userMessage"] is not None and run_result["userMessage"]["content"] == "/reviewdemo:under_score do Y", result


# ── #7781 re-gate: the cross-chat send race (CORE) ───────────────────────
#
# send() resolves a qualified plugin command across two awaits
# (/api/skills, then /api/skills/content). While the second fetch is in
# flight the user is free to open another chat and type a draft there.
# The buggy head read S.session AFTER the awaits, so it finished the send
# in the newly opened chat: the forced-skill directive POSTed into that
# chat and its composer draft was wiped (success path), or the
# "Plugin skill error" bubble was written into it (error path).
#
# The fix reuses the #8718 _slashOwner snapshot: after every await, if the
# viewed session is no longer the chat that issued the command, send()
# stops without touching the composer or the transcript.


def test_deferred_content_session_switch_success_path_stops_send():
    """/demo:skill do X sent in chat A; the /api/skills/content response is
    held; the user opens chat B and types a draft; the response is released.
    The send must stop: no /api/chat/start POST at all, chat B's transcript
    untouched, chat B's draft intact (maintainer CORE finding)."""
    result = _run_send_harness()["switchSuccess"]
    # The race window was genuinely exercised: the content fetch happened.
    assert result["contentFetches"] == ["/api/skills/content?name=demo%3Askill"], result
    # The send stopped before dispatch — nothing POSTed anywhere.
    assert result["startBody"] is None, result
    # Chat B's transcript was not touched (no user bubble, no directive).
    assert result["messages"] == [], result
    # Chat B's composer draft survived.
    assert result["composerAfter"] == "chat B draft", result
    assert result["thrown"] is None, result


def test_deferred_content_session_switch_error_path_stops_send():
    """Same race on the error path: the /api/skills/content fetch rejects
    after the user switched to chat B. The "Plugin skill error" bubble must
    NOT be written into chat B and its draft must NOT be cleared."""
    result = _run_send_harness()["switchError"]
    assert result["contentFetches"] == ["/api/skills/content?name=demo%3Askill"], result
    assert result["startBody"] is None, result
    assert result["messages"] == [], result
    assert result["composerAfter"] == "chat B draft", result
    assert result["thrown"] is None, result


def test_deferred_listing_session_switch_stops_send():
    """The listing fetch itself is the deferred one and it REJECTS after the
    switch. The should-fix fall-through (forward the literal text) must also
    honour the owner guard — otherwise chat A's literal command text would
    be forwarded into chat B and wipe its draft."""
    result = _run_send_harness()["switchListingFail"]
    assert result["startBody"] is None, result
    assert result["messages"] == [], result
    assert result["composerAfter"] == "chat B draft", result
    assert result["thrown"] is None, result


def test_listing_failure_forwards_literal_text_like_master():
    """should-fix: with NO session switch, a failing /api/skills listing
    fetch must fall through to master's behaviour — the literal command text
    is forwarded as the dispatched message. The previous single try wrapped
    the listing fetch too, so any /ns:cmd answered "Plugin skill error" and
    sent nothing."""
    result = _run_send_harness()["listingFailure"]
    assert result["startBody"] is not None, result
    assert result["startBody"]["message"] == "/demo:skill do X", result
    # No skill content may be fetched when the listing never loaded.
    assert result["contentFetches"] == [], result
    # No "Plugin skill error" bubble may answer for the failed listing.
    assert not any(
        "Plugin skill error" in str(m.get("content") or "")
        for m in result["messages"]
    ), result
