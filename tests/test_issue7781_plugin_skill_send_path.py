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
const queueEntries = [];
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
  _listeners: {},
  addEventListener(type, fn){ (this._listeners[type] = this._listeners[type] || []).push(fn); },
  __fire(type){ (this._listeners[type] || []).forEach((fn) => fn({ preventDefault(){}, stopPropagation(){} })); },
  removeEventListener(){},
  appendChild(child){ this.children.push(child); return child; }, setAttribute(){}, removeAttribute(){},
  focus(){}, blur(){},
  scrollTop: 0, scrollHeight: 0, offsetHeight: 0, offsetWidth: 0,
  innerHTML: '', textContent: '', children: [],
  remove(){}, closest(){ return null; },
  querySelector(){ return null; }, querySelectorAll(){ return []; },
  getAttribute(){ return null; }, hasAttribute(){ return false; },
  parentElement: null, parentNode: null,
});

const elementCache = {};
const createdElements = [];
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
  createElement(tag){ const el = elementStub(); el.tagName = String(tag || '').toUpperCase(); createdElements.push(el); return el; },
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
  // #7781 (P1, greptile 2026-11-26): the draft persistence the restore helper
  // calls. Records every save so a test can see WHICH chat the command was
  // handed back to.
  // #7781 (P1, greptile 2026-11-26): the restore helper consults the per-sid
  // draft revision + last saved payload signature that sessions.js keeps, to
  // tell a NEWER user-typed draft apart from the draft this send captured.
  // Mirror that bookkeeping here: a NON-EMPTY payload that differs from the
  // last one saved for this sid bumps the revision; empty payloads never do
  // (same rule as sessions.js's _bumpComposerDraftRevision).
  _draftSaves: [],
  _switchLog: [],
  _draftRevBySid: new Map(),
  _composerDraftLastPayloadBySid: new Map(),
  _composerDraftRevision(sid) { return ctx._draftRevBySid.get(sid) || 0; },
  _saveComposerDraftNow(sid, text, files) {
    const normText = String(text || '');
    const normFiles = (files || []).filter(Boolean);
    const sig = JSON.stringify({ text: normText, files: normFiles });
    if (normText || normFiles.length) {
      if (ctx._composerDraftLastPayloadBySid.get(sid) !== sig) {
        ctx._composerDraftLastPayloadBySid.set(sid, sig);
        ctx._draftRevBySid.set(sid, (ctx._draftRevBySid.get(sid) || 0) + 1);
      }
    } else {
      ctx._composerDraftLastPayloadBySid.set(sid, sig);
    }
    ctx._draftSaves.push({ sid, text, files: (files || []) });
    return Promise.resolve();
  },
  __draftSaves() { return ctx._draftSaves; },
  __resetDraftSaves() { ctx._draftSaves.length = 0; },
  __switchLog() { return ctx._switchLog; },
  __resetSwitchLog() { ctx._switchLog.length = 0; },
  removeThinking(){},
  history: { replaceState(){} },
  S: { session: { session_id: 'sess-1' }, pendingFiles: [], messages: [], toolCalls: [] },
  INFLIGHT: {},
  queueSessionMessage(sid, payload) { queueEntries.push({ sid, payload }); },
  updateQueueBadge(){},
  _clearComposerAfterQueuedSelectionSend(){},
  _clearQueueCardDisplay(){},
  _queueRenderKeys: {},
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
    if (path === '/api/commands') {
      // #7781 (P1, greptile 2026-11-26): park the command-metadata fetch so a
      // chat switch can happen BEFORE the idle resolver clears the composer.
      if (deferredFetch.target === 'commands') {
        return new Promise((resolve, reject) => {
          deferredFetch.resolve = () => resolve({ commands: [] });
          deferredFetch.reject = reject;
        });
      }
      return { commands: [] };
    }
    if (path === '/api/commands/bundles') return { bundles: [] };
    if (path.indexOf('/api/skills/content?name=') === 0) {
      const _skillUrl = new URL(path, 'http://localhost/');
      const name = _skillUrl.searchParams.get('name') || '';
      if (deferredFetch.target === 'content') {
        return new Promise((resolve, reject) => {
          deferredFetch.resolve = () => resolve({ content: skillContentByName[name] || '' });
          deferredFetch.reject = reject;
        });
      }
      return { content: skillContentByName[name] || '' };
    }
    if (path === '/api/chat/steer') {
      return { accepted: true, fallback: null, stream_id: 'run-steer-1' };
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
  __created() { return createdElements; },
  __resetCreated() { createdElements.length = 0; },
  _refreshSessionSkillUsageForOwner(sid) { apiCalls.push({ path: '__skill_usage__', sid }); },
  __apiCalls() { return apiCalls; },
  __resetQueue() { queueEntries.length = 0; },
  __queueEntries() { return queueEntries; },
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
  __switchSession(sid, draft, opts) {
    // Mirror what loadSession() does on a switch: persist the composer and
    // staged files of the chat being left to ITS draft FIRST, then show the
    // other chat's draft. #7781 P1: a later restore of a captured command
    // must compare against what this save left behind.
    const fromSid = ctx.S.session && ctx.S.session.session_id;
    const departingText = getEl('msg').value;
    const departingFiles = (ctx.S.pendingFiles || []).filter(Boolean);
    ctx._switchLog.push({ fromSid, savedText: departingText, toSid: sid, draft });
    if (fromSid && (!opts || opts.persist !== false)) {
      ctx._saveComposerDraftNow(fromSid, departingText, departingFiles);
      ctx.S.pendingFiles = [];
    }
    ctx.S.session = { session_id: sid };
    ctx.S.messages = [];
    getEl('msg').value = draft;
  },
  __messages() { return ctx.S.messages; },
};
vm.createContext(ctx);
vm.runInContext(__COMMANDS_JS__, ctx);
vm.runInContext(__MESSAGES_JS__, ctx);

(async () => {
  try {
    const result = await vm.runInContext(`(async () => {
      const run = async (composerText, skills, contentMap) => {
        S.session = { session_id: 'sess-1' };
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
          usageRefreshes: __apiCalls()
            .filter((c) => c && c.path === '__skill_usage__')
            .map((c) => c.sid),
        };
      };
      // #7781 [CORE] runner: park the content (or listing) fetch, switch the
      // viewed session mid-flight with a draft typed there, THEN settle the
      // fetch. Whatever send() does next must belong to the chat that issued
      // the command only.
      const runDeferredSwitch = async (composerText, skills, contentMap, opts) => {
        __resetDraftSaves();
        __resetSwitchLog();
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
            : opts.defer === 'commands'
              ? c.path === '/api/commands'
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
          draftSaves: __draftSaves().slice(),
          switchLog: __switchLog().slice(),
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
      // #7781 [CORE] 2026-11-23: send() takes its busy/default-steer branch
      // BEFORE the idle-path plugin resolver, so a picked '/ns:skill args'
      // used to steer only the literal command. This runner drives that exact
      // branch with a live stream and records the /api/chat/steer payload.
      const runBusy = async (composerText, skills, contentMap, opts) => {
        opts = opts || {};
        __setComposer(composerText);
        __setSkills(skills);
        __clearSkillContent();
        for (const k in (contentMap || {})) __setSkillContent(k, contentMap[k]);
        __resetApiCalls();
        __resetQueue();
        __failSkillsListing(false);
        S.session = { session_id: opts.sessionId || 'sess-1' };
        S.messages = [];
        INFLIGHT = {};
        S.busy = true;
        S.activeStreamId = 'run-steer-1';
        window._defaultMessageMode = opts.mode || 'steer';
        let thrown = null;
        await send(opts.sendOptions || undefined).catch((e) => { thrown = String((e && e.message) || e); });
        const steerCalls = __apiCalls().filter((c) => c && c.path === '/api/chat/steer');
        const steerCall = steerCalls[steerCalls.length - 1];
        const out = {
          steerBody: steerCall ? JSON.parse(steerCall.opts.body) : null,
          steerCalls: steerCalls.length,
          contentFetches: __apiCalls()
            .filter((c) => c && c.path.indexOf('/api/skills/content?name=') === 0)
            .map((c) => c.path),
          skillsFetches: __apiCalls().filter((c) => c && c.path === '/api/skills').length,
          queueEntries: __queueEntries().map((e) => ({ sid: e.sid, text: e.payload && e.payload.text })),
          composerAfter: __composer(),
          usageRefreshes: __apiCalls()
            .filter((c) => c && c.path === '__skill_usage__')
            .map((c) => c.sid),
          thrown,
        };
        S.busy = false;
        S.activeStreamId = null;
        window._defaultMessageMode = 'steer';
        return out;
      };
      // Owner fence for the busy path: the skill fetches park the send, the
      // user opens another chat with a draft, then the fetch settles. Nothing
      // may be steered and the other chat's composer must survive.
      const runBusyDeferredSwitch = async (composerText, skills, contentMap, opts) => {
        __resetDraftSaves();
        __resetSwitchLog();
        __setComposer(composerText);
        __setSkills(skills);
        __clearSkillContent();
        for (const k in (contentMap || {})) __setSkillContent(k, contentMap[k]);
        __resetApiCalls();
        __resetQueue();
        __failSkillsListing(false);
        S.session = { session_id: opts.ownerSid };
        S.messages = [];
        INFLIGHT = {};
        S.busy = true;
        S.activeStreamId = 'run-steer-1';
        window._defaultMessageMode = 'steer';
        __deferFetch(opts.defer || 'content');
        let thrown = null;
        const sendPromise = send(composerText).catch((e) => { thrown = String((e && e.message) || e); });
        for (let i = 0; i < 200; i++) {
          const reached = __apiCalls().some((c) => c && (opts.defer === 'listing'
            ? c.path === '/api/skills'
            : c.path.indexOf('/api/skills/content?name=') === 0));
          if (reached) break;
          await new Promise((r) => setTimeout(r, 5));
        }
        __switchSession(opts.switchToSid, opts.draft);
        __settleFetch('success');
        await sendPromise;
        const out = {
          steerCalls: __apiCalls().filter((c) => c && c.path === '/api/chat/steer').length,
          queueEntries: __queueEntries().map((e) => ({ sid: e.sid, text: e.payload && e.payload.text })),
          composerAfter: __composer(),
          draftSaves: __draftSaves().slice(),
          switchLog: __switchLog().slice(),
          thrown,
        };
        S.busy = false;
        S.activeStreamId = null;
        window._defaultMessageMode = 'steer';
        return out;
      };
      // #7781 (P1 ①, greptile 2026-11-26): while the skill fetch is parked,
      // the user (still viewing the issuing chat) retypes a NEWER draft into
      // the composer the send cleared, then opens another chat. loadSession()
      // persists that newer text for the issuing chat; the restore must NOT
      // overwrite it with the captured command.
      const runDeferredSwitchNewerDraft = async (composerText, skills, contentMap, opts) => {
        __resetDraftSaves();
        __resetSwitchLog();
        __setComposer(composerText);
        __setSkills(skills);
        __clearSkillContent();
        for (const k in (contentMap || {})) __setSkillContent(k, contentMap[k]);
        __resetApiCalls();
        __failSkillsListing(false);
        S.session = { session_id: opts.ownerSid };
        S.messages = [];
        INFLIGHT = {};
        __deferFetch(opts.defer || 'content');
        let thrown = null;
        const sendPromise = send(composerText).catch((e) => { thrown = String((e && e.message) || e); });
        for (let i = 0; i < 200; i++) {
          const reached = __apiCalls().some((c) => c && (opts.defer === 'listing'
            ? c.path === '/api/skills'
            : c.path.indexOf('/api/skills/content?name=') === 0));
          if (reached) break;
          await new Promise((r) => setTimeout(r, 5));
        }
        // The user retypes a newer draft in the issuing chat, then opens
        // another chat — the switch saves THAT newer draft first.
        __setComposer(opts.newerDraft);
        __switchSession(opts.switchToSid, opts.draft);
        __settleFetch('success');
        await sendPromise;
        return {
          composerAfter: __composer(),
          messages: S.messages,
          draftSaves: __draftSaves().slice(),
          switchLog: __switchLog().slice(),
          contentFetches: __apiCalls()
            .filter((c) => c && c.path.indexOf('/api/skills/content?name=') === 0)
            .map((c) => c.path),
          thrown,
        };
      };
      // #7781 (P1 ②, greptile 2026-11-26): the issuing chat has staged
      // attachments when a busy skill send clears the composer; opening another
      // chat saves those files with the departing draft. Restoring the command
      // must carry that captured file list instead of writing files: [].
      const runBusyDeferredSwitchStagedFiles = async (composerText, skills, contentMap, opts) => {
        __resetDraftSaves();
        __resetSwitchLog();
        __setComposer(composerText);
        __setSkills(skills);
        __clearSkillContent();
        for (const k in (contentMap || {})) __setSkillContent(k, contentMap[k]);
        __resetApiCalls();
        __resetQueue();
        __failSkillsListing(false);
        S.session = { session_id: opts.ownerSid };
        S.messages = [];
        INFLIGHT = {};
        S.busy = true;
        S.activeStreamId = 'run-steer-1';
        S.pendingFiles = (opts.pendingFiles || []).map((f) => Object.assign({}, f));
        window._defaultMessageMode = 'steer';
        __deferFetch(opts.defer || 'content');
        let thrown = null;
        const sendPromise = send(composerText).catch((e) => { thrown = String((e && e.message) || e); });
        for (let i = 0; i < 200; i++) {
          const reached = __apiCalls().some((c) => c && (opts.defer === 'listing'
            ? c.path === '/api/skills'
            : c.path.indexOf('/api/skills/content?name=') === 0));
          if (reached) break;
          await new Promise((r) => setTimeout(r, 5));
        }
        // The user opens another chat; the staged files stay with the issuing
        // chat's departing draft.
        __switchSession(opts.switchToSid, opts.draft);
        __settleFetch('success');
        await sendPromise;
        const out = {
          composerAfter: __composer(),
          steerCalls: __apiCalls().filter((c) => c && c.path === '/api/chat/steer').length,
          draftSaves: __draftSaves().slice(),
          switchLog: __switchLog().slice(),
          thrown,
        };
        S.busy = false;
        S.activeStreamId = null;
        S.pendingFiles = [];
        window._defaultMessageMode = 'steer';
        return out;
      };
      const runDeadRunRecovery = async () => {
        __setComposer('/reviewdemo:under_score do EXACT');
        __setSkills([__UNDERSCORE_ROW__]);
        __clearSkillContent();
        __setSkillContent('reviewdemo:under_score', 'UNDERSCORE SKILL BODY');
        __resetApiCalls();
        __resetQueue();
        __resetCreated();
        S.session = { session_id: 'sess-1' };
        S.messages = [];
        INFLIGHT = {};
        S.busy = false;
        S.activeStreamId = null;
        // The payload the busy branch assembled and handed to _trySteer.
        const payload = '[USER OVERRIDE] You MUST follow the skill '
          + "'reviewdemo:under_score' content provided below before responding"
          + ' to the next message. UNDERSCORE SKILL BODY '
          + '[FORCED SKILL CONTEXT: reviewdemo:under_score] do EXACT';
        _showSteerRecovery(payload, true, 'stream_dead', '/reviewdemo:under_score do EXACT');
        const strip = __created().find((e) => e && e.className === 'steer-recovery');
        const retry = strip && strip.children.find((c) => c && c.tagName === 'BUTTON');
        let fired = false;
        if (retry) { fired = true; retry.__fire('click'); }
        for (let i = 0; i < 200; i++) {
          if (__apiCalls().some((c) => c && c.path === '/api/chat/start')) break;
          await new Promise((r) => setTimeout(r, 5));
        }
        const startCall = __apiCalls().find((c) => c && c.path === '/api/chat/start');
        return {
          fired,
          startBody: startCall ? JSON.parse(startCall.opts.body) : null,
          composerAfter: __composer(),
        };
      };
      const _switchDuringMetadata = await runDeferredSwitch('/reviewdemo:under_score do EXACT', [__UNDERSCORE_ROW__], { 'reviewdemo:under_score': 'UNDERSCORE SKILL BODY' }, { defer: 'commands', ownerSid: 'sess-A', switchToSid: 'sess-B', draft: 'chat B draft', mode: 'success' });
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
      // #7781 [CORE] 2026-11-23 busy/default-steer branch: the picked command
      // must steer the skill directive + body (not the literal command), while
      // literalSlash / unknown / core-command / queue controls keep master
      // behaviour and a mid-fetch session switch stops before delivery.
      const _busyPicked = await runBusy('/reviewdemo:under_score do EXACT', [__UNDERSCORE_ROW__], { 'reviewdemo:under_score': 'UNDERSCORE SKILL BODY' });
      // Full picker-entry -> busy steer chain: the entry the picker builds
      // supplies the composer text, and that exact text must resolve while a
      // stream is live (the maintainer's selected-picker -> busy consumer ask).
      const _busyEntry = _buildSkillCommandEntry(__UNDERSCORE_ROW__);
      const _busyEntryName = _busyEntry && _busyEntry.name;
      const _busyPickedFromPicker = await runBusy('/' + _busyEntryName + ' do PICK', [__UNDERSCORE_ROW__], { 'reviewdemo:under_score': 'UNDERSCORE SKILL BODY' });
      const _busyLiteral = await runBusy('/reviewdemo:under_score do EXACT', [__UNDERSCORE_ROW__], { 'reviewdemo:under_score': 'UNDERSCORE SKILL BODY' }, { sendOptions: { literalSlash: true } });
      const _busyUnknown = await runBusy('/reviewdemo:missing do EXACT', [__UNDERSCORE_ROW__], {});
      const _busyCore = await runBusy('/steer hello from core', [__UNDERSCORE_ROW__], { 'reviewdemo:under_score': 'UNDERSCORE SKILL BODY' });
      const _busyQueue = await runBusy('/reviewdemo:under_score do EXACT', [__UNDERSCORE_ROW__], { 'reviewdemo:under_score': 'UNDERSCORE SKILL BODY' }, { mode: 'queue' });
      const _busySwitchContent = await runBusyDeferredSwitch('/reviewdemo:under_score do EXACT', [__UNDERSCORE_ROW__], { 'reviewdemo:under_score': 'UNDERSCORE SKILL BODY' }, { defer: 'content', ownerSid: 'sess-A', switchToSid: 'sess-B', draft: 'chat B draft' });
      const _busySwitchListing = await runBusyDeferredSwitch('/reviewdemo:under_score do EXACT', [__UNDERSCORE_ROW__], { 'reviewdemo:under_score': 'UNDERSCORE SKILL BODY' }, { defer: 'listing', ownerSid: 'sess-A', switchToSid: 'sess-B', draft: 'chat B draft' });
      // #7781 (P1 ①, greptile 2026-11-26): while the content fetch is parked,
      // the user retypes a NEWER draft in the issuing chat, then opens chat B.
      // The restore must NOT clobber that newer saved draft with the command.
      const _newerDraftContent = await runDeferredSwitchNewerDraft('/demo:skill do X', [__PLUGIN_ROW__], { 'demo:skill': 'DEMO SKILL BODY' }, { defer: 'content', ownerSid: 'sess-A', switchToSid: 'sess-B', draft: 'chat B draft', newerDraft: 'A brand-new draft typed mid-load' });
      // #7781 (P1 ②, greptile 2026-11-26): staged attachments ride the issuing
      // chat's departing draft through the switch; restore must keep them.
      const _stagedFiles = await runBusyDeferredSwitchStagedFiles('/reviewdemo:under_score do EXACT', [__UNDERSCORE_ROW__], { 'reviewdemo:under_score': 'UNDERSCORE SKILL BODY' }, { defer: 'content', ownerSid: 'sess-A', switchToSid: 'sess-B', draft: 'chat B draft', pendingFiles: [{ name: 'report.pdf', size: 1024, type: 'application/pdf' }] });
      // #7781 (P1, greptile 2026-11-26): the switch lands while the COMMAND
      // metadata is still loading — before the idle resolver clears anything.
      const _deadRun = await runDeadRunRecovery();
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
        busyPicked: _busyPicked,
        busyPickedFromPicker: { entryName: _busyEntryName, run: _busyPickedFromPicker },
        busyLiteral: _busyLiteral,
        busyUnknown: _busyUnknown,
        busyCore: _busyCore,
        busyQueue: _busyQueue,
        busySwitchContent: _busySwitchContent,
        busySwitchListing: _busySwitchListing,
        newerDraftContent: _newerDraftContent,
        stagedFiles: _stagedFiles,
        switchDuringMetadata: _switchDuringMetadata,
        deadRunRecovery: _deadRun,
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
    # The skill body must be fetched exactly once, for the canonical name,
    # carrying the owning session id so the server records the use.
    assert len(result["contentFetches"]) == 1, result
    assert result["contentFetches"][0].startswith("/api/skills/content?name=demo%3Askill"), result
    assert "session_id=sess-1" in result["contentFetches"][0], result


def test_plugin_skill_use_is_recorded_on_the_owning_session():
    """The content request carries the owning session id and the send path
    refreshes that session's usage afterwards, so a plugin skill loaded via
    ``/namespace:skill`` appears in the conversation's "Skills Used" list
    instead of being recorded only when the agent happens to call the skill
    tool (2026-11-18 review finding)."""
    result = _run_send_harness()["positive"]
    assert result["usageRefreshes"] == ["sess-1"], result


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
    assert len(result["contentFetches"]) == 1, result
    assert result["contentFetches"][0].startswith("/api/skills/content?name=reviewdemo%3Aunder_score"), result
    assert "session_id=sess-1" in result["contentFetches"][0], result


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
    assert run_result["contentFetches"] == ["/api/skills/content?name=reviewdemo%3Aunder_score&session_id=sess-1"], result
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
    assert result["contentFetches"] == ["/api/skills/content?name=demo%3Askill&session_id=sess-A"], result
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
    assert result["contentFetches"] == ["/api/skills/content?name=demo%3Askill&session_id=sess-A"], result
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


# ── #7781 review 2026-11-23: busy/default-steer delivery ────────────────────
# ``send()`` takes its busy/default-steer branch (messages.js:1681-1758) and
# returns before the idle-path plugin resolver (:1981-2038). A picked
# ``/ns:skill args`` while a stream is live used to be steered as the literal
# command, so the skill body never reached the model. These tests drive the
# real busy branch and assert the ``/api/chat/steer`` consumer payload.


def test_busy_steer_resolves_the_picker_entry_end_to_end():
    """The maintainer's selected-picker -> busy steer-consumer chain: the
    picker-built entry supplies the composer text, and the busy branch must
    resolve that exact spelling into the steered skill payload."""
    payload = _run_send_harness()["busyPickedFromPicker"]
    assert payload["entryName"] == "reviewdemo:under_score", payload
    result = payload["run"]
    assert result["steerCalls"] == 1, result
    text = result["steerBody"]["text"]
    assert "UNDERSCORE SKILL BODY" in text, result
    assert "do PICK" in text, result
    assert "[FORCED SKILL CONTEXT: reviewdemo:under_score]" in text, result


def test_busy_steer_resolves_plugin_skill_body():
    """Selecting a plugin skill while busy must steer the directive + body,
    keeping the typed instruction, and record usage on the owning session."""
    result = _run_send_harness()["busyPicked"]
    assert result["steerCalls"] == 1, result
    body = result["steerBody"]
    assert body["session_id"] == "sess-1", result
    text = body["text"]
    assert "UNDERSCORE SKILL BODY" in text, result
    assert "do EXACT" in text, result
    assert "[USER OVERRIDE]" in text, result
    assert "You MUST follow the skill 'reviewdemo:under_score'" in text, result
    assert "[FORCED SKILL CONTEXT: reviewdemo:under_score]" in text, result
    # The content endpoint is consulted with the owning session id so the
    # skill is recorded in that conversation's "Skills Used" list.
    assert result["contentFetches"] == [
        "/api/skills/content?name=reviewdemo%3Aunder_score&session_id=sess-1"
    ], result
    assert result["usageRefreshes"] == ["sess-1"], result
    assert result["composerAfter"] == "", result


def test_busy_steer_literal_slash_stays_verbatim():
    result = _run_send_harness()["busyLiteral"]
    assert result["steerCalls"] == 1, result
    assert result["steerBody"]["text"] == "/reviewdemo:under_score do EXACT", result
    assert result["contentFetches"] == [], result


def test_busy_steer_unknown_qualified_name_stays_verbatim():
    result = _run_send_harness()["busyUnknown"]
    assert result["steerCalls"] == 1, result
    assert result["steerBody"]["text"] == "/reviewdemo:missing do EXACT", result
    assert result["contentFetches"] == [], result


def test_busy_steer_keeps_busy_control_precedence():
    """`/steer` stays a core busy-control command: its argument is steered
    verbatim instead of being run through the plugin-skill resolver."""
    result = _run_send_harness()["busyCore"]
    assert result["steerCalls"] == 1, result
    assert result["steerBody"]["text"] == "hello from core", result
    assert result["contentFetches"] == [], result


def test_busy_queue_keeps_the_literal_command_for_drain():
    """Queue mode stays lossless: the queued entry carries the original
    command text, and the drain re-enters the idle send() path where the
    resolver attaches the skill body."""
    result = _run_send_harness()["busyQueue"]
    assert result["steerCalls"] == 0, result
    assert result["queueEntries"] == [
        {"sid": "sess-1", "text": "/reviewdemo:under_score do EXACT"}
    ], result
    assert result["contentFetches"] == [], result


def test_busy_steer_session_switch_stops_before_delivery():
    """The content fetch parks the send; the user opens chat B before it
    settles. Nothing may be steered and chat B's draft must survive."""
    result = _run_send_harness()["busySwitchContent"]
    assert result["steerCalls"] == 0, result
    assert result["composerAfter"] == "chat B draft", result
    assert result["thrown"] is None, result


def test_busy_steer_listing_session_switch_stops_before_delivery():
    result = _run_send_harness()["busySwitchListing"]
    assert result["steerCalls"] == 0, result
    assert result["composerAfter"] == "chat B draft", result
    assert result["thrown"] is None, result


def test_dead_run_recovery_send_keeps_the_skill_body():
    """stream_dead -> recovery Send must start the next turn with the skill
    body. ``literalSlash`` skips the plugin resolver, so restoring only the
    short command dropped the payload the steer had already assembled."""
    result = _run_send_harness()["deadRunRecovery"]
    assert result["fired"] is True, result
    body = result["startBody"]
    assert body is not None, result
    dumped = json.dumps(body, ensure_ascii=False)
    assert "UNDERSCORE SKILL BODY" in dumped, result
    assert "do EXACT" in dumped, result
    assert result["composerAfter"].startswith("[USER OVERRIDE]"), result


def test_idle_switch_during_command_metadata_keeps_the_other_chats_draft():
    """A switch that lands while /api/commands is still loading must leave the
    OTHER chat's composer intact — the resolver may not clear it (#7781 P1)."""
    result = _run_send_harness()["switchDuringMetadata"]
    assert result["composerAfter"] == "chat B draft", result
    assert result["startBody"] is None, result
    assert result["thrown"] is None, result
    # Chat A's composer was never cleared, so the switch persisted the command
    # itself: nothing to restore, nothing lost.
    assert result["switchLog"][0]["savedText"] == "/reviewdemo:under_score do EXACT", result


def test_busy_switch_hands_the_unsent_command_back_to_its_own_chat():
    """The busy clear happens up front, so a switch during the content fetch
    persisted an EMPTY draft for the issuing chat. The stopped send must put
    the command back on that chat's draft (#7781 P1)."""
    result = _run_send_harness()["busySwitchContent"]
    assert result["composerAfter"] == "chat B draft", result
    assert result["steerCalls"] == 0, result
    assert result["switchLog"][0]["savedText"] == "", result
    saves = [s for s in result["draftSaves"] if s["sid"] == "sess-A"]
    assert saves, result
    assert saves[-1]["text"] == "/reviewdemo:under_score do EXACT", result
    assert all(s["sid"] == "sess-A" for s in saves), result


def test_idle_listing_switch_hands_the_unsent_command_back_to_its_own_chat():
    """Same for the idle resolver: after the listing fetch fails, the switch
    already saved an empty draft, so the stop must restore it (#7781 P1)."""
    result = _run_send_harness()["switchListingFail"]
    assert result["composerAfter"] == "chat B draft", result
    assert result["switchLog"][0]["savedText"] == "", result
    saves = [s for s in result["draftSaves"] if s["sid"] == "sess-A"]
    assert saves, result
    assert saves[-1]["text"] == "/demo:skill do X", result


def test_busy_steer_clears_the_composer_before_the_skill_fetch():
    """The composer must be cleared BEFORE the first await, but only for the
    chat that issued the command (#7781 P1, greptile 2026-11-26): an
    unconditional clear that ran once the user had switched erased the other
    chat's draft."""
    start = MESSAGES_JS.index("let _busySteerPayload=text;")
    end = MESSAGES_JS.index("await _trySteer(_busySteerPayload")
    segment = MESSAGES_JS[start:end]
    # The issuing chat is snapshotted before anything is cleared...
    assert segment.index("const _busyOwnerSid=") < segment.index("$('msg').value=''"), segment
    # ...and the clear is fenced on that snapshot.
    assert (
        "if((S.session&&S.session.session_id||null)===_busyOwnerSid){$('msg').value='';}"
        in segment
    ), segment
    assert segment.index("$('msg').value=''") < segment.index(
        "await _loadPluginSkillForCommand"
    ), "the clear must precede the first await"
    # A send that stops after the switch hands the command back to the chat
    # that issued it — composer when still open, draft either way. #7781
    # greptile 2026-11-26: the restore call carries the owning chat's captured
    # files (②) and its send-time draft revision (①).
    assert (
        "_restoreCommandToOwnerChat(_busyOwnerSid,_busySteerRestoreText,"
        "/* ② */_failedSendFilesSnapshot,/* ① */_busyOwnerDraftRev)"
        in segment
    ), segment
    # The revision is snapshotted before the composer is cleared.
    assert segment.index("const _busyOwnerDraftRev=") < segment.index(
        "$('msg').value=''"
    ), segment


def test_idle_plugin_resolver_clears_the_composer_before_the_listing_fetch():
    start = MESSAGES_JS.index("if(_parsedCmd.name.includes(':')&&!_bundleCmd){")
    end = MESSAGES_JS.index("if(_match){", start)
    segment = MESSAGES_JS[start:end]
    # #7781 P1: the owner fence runs FIRST — a switch during the metadata waits
    # above must not have the resolver clear the other chat's composer.
    owner = "(_slashOwner&&_slashOwner.sid||null)"
    assert owner in segment, segment
    assert segment.index(owner) < segment.index("$('msg').value=''"), segment
    assert "$('msg').value='';autoResize();" in segment
    assert segment.index("$('msg').value='';autoResize();") < segment.index(
        "await api('/api/skills')"
    ), "the clear must precede the listing fetch"
    # Every owner-fence stop hands the cleared command back to its own chat,
    # carrying the captured files (②) and the send-time draft revision (①)
    # (#7781 greptile 2026-11-26).
    assert MESSAGES_JS.count(
        "_restoreCommandToOwnerChat(_slashOwner&&_slashOwner.sid,text,"
        "/* ② */_slashOwner&&_slashOwner._files,/* ① */_slashOwner&&_slashOwner._draftRev)"
    ) == 4


# ── #7781 (P1 ①, greptile 2026-11-26): newer drafts get overwritten ─────────────────
# While the skill fetch is parked, the user can type a new draft in chat A and
# switch to chat B. loadSession() saves that newer draft for A, but the
# restore overwrites it with the captured command. The restore must compare
# against the saved draft state before writing.


def test_newer_draft_typed_during_load_is_not_overwritten_by_restore():
    """The user retypes a NEWER draft into the issuing chat while the content
    fetch is parked, then opens chat B. The switch saves that newer draft for
    chat A; the stopped send's restore must NOT clobber it with the old
    command — returning to A shows the NEWER draft (greptile P1 ①)."""
    result = _run_send_harness()["newerDraftContent"]
    # The race window was genuinely exercised: the content fetch happened and
    # the send stopped before dispatch.
    assert result["contentFetches"] == [
        "/api/skills/content?name=demo%3Askill&session_id=sess-A"
    ], result
    assert result["composerAfter"] == "chat B draft", result
    assert result["messages"] == [], result
    # The issuing chat's switched-away save recorded the NEWER draft...
    assert result["switchLog"][0]["savedText"] == "A brand-new draft typed mid-load", result
    # ...and the restore did NOT overwrite it with the old command.
    saves = [s for s in result["draftSaves"] if s["sid"] == "sess-A"]
    assert saves, result
    assert saves[-1]["text"] == "A brand-new draft typed mid-load", result
    assert result["thrown"] is None, result


# ── #7781 (P1 ②, greptile 2026-11-26): saved attachments disappear ──────────────────
# The restore always saves files: []. Busy skill sends can have staged files,
# and switching chats saves those files with the departing draft. Restoring the
# command then replaces the saved file list with an empty one, losing the
# attachments from the unsent draft.


def test_restore_keeps_the_owning_chats_staged_files():
    """Staged attachments on the issuing chat's departing draft must survive
    the restore: the command is written back AND the captured file list rides
    the save instead of files: [] (greptile P1 ②)."""
    result = _run_send_harness()["stagedFiles"]
    # The switch happened with a busy steer parked on the content fetch.
    assert result["steerCalls"] == 0, result
    assert result["composerAfter"] == "chat B draft", result
    assert result["thrown"] is None, result
    saves = [s for s in result["draftSaves"] if s["sid"] == "sess-A"]
    assert saves, result
    # The LAST draft save for chat A must carry the staged file with the
    # command — files: [] would have lost the attachment.
    last = saves[-1]
    assert last["text"] == "/reviewdemo:under_score do EXACT", result
    assert len(last["files"]) == 1, result
    assert last["files"][0]["name"] == "report.pdf", result
