"""Regression tests for #5682 profile query boot switching."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
BOOT_JS = BOOT_JS_PATH.read_text(encoding="utf-8")
PANELS_JS = PANELS_JS_PATH.read_text(encoding="utf-8")
UI_JS = UI_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _node_prelude() -> str:
    return f"""
const sessionsSrc = {SESSIONS_JS!r};
const bootSrc = {BOOT_JS!r};
function extractFunc(src, name) {{
  const re = new RegExp('(?:async\\\\s+)?function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
function evalSession(name) {{
  globalThis[name] = (0, eval)('(' + extractFunc(sessionsSrc, name) + ')');
}}
function evalBoot(name) {{
  globalThis[name] = (0, eval)('(' + extractFunc(bootSrc, name) + ')');
}}
"""


def test_valid_profile_query_switches_before_restore_and_cleans_url():
    source = _node_prelude() + """
function applyUrl(rel) {
  const next = new URL(rel, 'https://example.test');
  window.location.href = next.href;
  window.location.pathname = next.pathname;
  window.location.search = next.search;
  window.location.hash = next.hash;
}
global.window = {
  location: {},
  history: {
    state: { from: 'test' },
    calls: [],
    replaceState(state, title, url) {
      this.calls.push({ state, title, url });
      this.state = state;
      applyUrl(url);
    }
  }
};
global.document = { baseURI: 'https://example.test/app/' };
console.warn = (...args) => { throw new Error('unexpected warn: ' + args.join(' ')); };
applyUrl('/app/?profile=vops&q=hello&prompt=hi&send=1&keep=1#frag');
global.localStorage = {
  store: { 'hermes-webui-session': 'saved-local' },
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(this.store, key) ? this.store[key] : null;
  },
  setItem(key, value) {
    this.store[key] = String(value);
  },
  removeItem(key) {
    delete this.store[key];
  }
};
evalSession('_profileQueryIntentFromLocation');
evalSession('_consumeProfileQueryParamFromLocation');
evalSession('_consumeComposerPrefillParamsFromLocation');
evalSession('_sessionUrlForSid');
evalBoot('_profileQueryBlocksSavedLocalRestore');
const intent = _profileQueryIntentFromLocation();
global.S = { activeProfile: 'default', activeProfileIsDefault: true };
const switched = [];
global.switchToProfile = async (name) => {
  switched.push(name);
  S.activeProfile = name;
  S.activeProfileIsDefault = false;
  localStorage.setItem('hermes-webui-session', 'fresh-local');
  return true;
};
(async () => {
  const savedLocalBefore = localStorage.getItem('hermes-webui-session');
  const profileSwitchProfileBefore = S.activeProfile || 'default';
  const profileSwitchIsDefaultBefore = !!S.activeProfileIsDefault;
  let profileSwitchCompleted = false;
  let profileSwitchChangedProfile = false;
  if (intent && intent.hasParam) {
    try {
      if (intent.valid) {
        if (typeof switchToProfile === 'function') {
          profileSwitchCompleted = await switchToProfile(intent.name) === true;
          if (profileSwitchCompleted) {
            profileSwitchChangedProfile = (S.activeProfile || 'default') !== profileSwitchProfileBefore || !!S.activeProfileIsDefault !== profileSwitchIsDefaultBefore;
            if (typeof _consumeProfileQueryParamFromLocation === 'function') _consumeProfileQueryParamFromLocation();
          }
        }
      } else {
        console.warn('[boot] ignored invalid profile query', intent.name);
      }
    } catch (e) {
      console.warn('[boot] profile query switch failed', e);
    }
  }
  const blocksSavedLocal = _profileQueryBlocksSavedLocalRestore(intent, null);
  if (blocksSavedLocal && profileSwitchCompleted && profileSwitchChangedProfile && localStorage.getItem('hermes-webui-session') === savedLocalBefore) localStorage.removeItem('hermes-webui-session');
  const savedLocalAfterSuppress = localStorage.getItem('hermes-webui-session');
  const savedLocalAfterReload = localStorage.getItem('hermes-webui-session');
  const keepsExplicitSession = _profileQueryBlocksSavedLocalRestore(intent, 'session-123');
  const afterProfile = window.location.pathname + window.location.search + window.location.hash;
  const promoted = _sessionUrlForSid('abc 123');
  _consumeComposerPrefillParamsFromLocation();
  const afterPrefill = window.location.pathname + window.location.search + window.location.hash;
  const profilePos = bootSrc.indexOf("const profileIntent=(typeof _profileQueryIntentFromLocation==='function')?_profileQueryIntentFromLocation():null;");
  const renderPos = bootSrc.indexOf("await renderSessionList();", profilePos);
  const savedPos = bootSrc.indexOf("const saved=urlSession||savedLocal;", profilePos);
  const loadPos = bootSrc.indexOf("await loadSession(saved, {preserveActiveInput:true});", profilePos);
  const consumePos = bootSrc.indexOf("if(typeof _consumeProfileQueryParamFromLocation==='function') _consumeProfileQueryParamFromLocation();", profilePos);
  const completedPos = bootSrc.indexOf("_profileSwitchCompleted=await switchToProfile(profileIntent.name)===true;", profilePos);
  const changedPos = bootSrc.indexOf("_profileSwitchChangedProfile=", completedPos);
  const cleanupGuardPos = bootSrc.indexOf("if(_profileQueryBlocksSavedLocal&&_profileSwitchCompleted&&_profileSwitchChangedProfile){", profilePos);
  const initialReasoningFetchPos = bootSrc.indexOf("if(typeof fetchReasoningChip==='function'&&(!_profileSwitchCompleted||!_profileSwitchChangedProfile)) fetchReasoningChip();", profilePos);
  console.log(JSON.stringify({ intent, switched, promoted, afterProfile, afterPrefill, historyCalls: window.history.calls, profilePos, renderPos, savedPos, loadPos, consumePos, completedPos, changedPos, cleanupGuardPos, initialReasoningFetchPos, savedLocalBefore, savedLocalAfterSuppress, savedLocalAfterReload, blocksSavedLocal, keepsExplicitSession }));
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""
    payload = json.loads(_run_node(source))
    assert payload["intent"] == {"hasParam": True, "valid": True, "name": "vops"}
    assert payload["switched"] == ["vops"]
    assert payload["promoted"] == "/app/session/abc%20123?keep=1#frag"
    assert payload["afterProfile"] == "/app/?q=hello&prompt=hi&send=1&keep=1#frag"
    assert payload["afterPrefill"] == "/app/?keep=1#frag"
    assert payload["historyCalls"][0]["url"] == "/app/?q=hello&prompt=hi&send=1&keep=1#frag"
    assert payload["historyCalls"][1]["url"] == "/app/?keep=1#frag"
    assert payload["profilePos"] >= 0
    assert payload["renderPos"] > payload["profilePos"]
    assert payload["savedPos"] > payload["profilePos"]
    assert payload["loadPos"] > payload["savedPos"]
    assert payload["consumePos"] > payload["profilePos"]
    assert payload["completedPos"] > payload["profilePos"]
    assert payload["changedPos"] > payload["completedPos"]
    assert payload["cleanupGuardPos"] > payload["consumePos"]
    assert payload["initialReasoningFetchPos"] > payload["completedPos"]
    assert payload["savedLocalBefore"] == "saved-local"
    assert payload["savedLocalAfterSuppress"] == "fresh-local"
    assert payload["savedLocalAfterReload"] == "fresh-local"
    assert payload["blocksSavedLocal"] is True
    assert payload["keepsExplicitSession"] is False


def test_noop_profile_query_switch_keeps_saved_local_state():
    source = _node_prelude() + """
function applyUrl(rel) {
  const next = new URL(rel, 'https://example.test');
  window.location.href = next.href;
  window.location.pathname = next.pathname;
  window.location.search = next.search;
  window.location.hash = next.hash;
}
global.window = {
  location: {},
  history: {
    state: { from: 'test' },
    calls: [],
    replaceState(state, title, url) {
      this.calls.push({ state, title, url });
      this.state = state;
      applyUrl(url);
    }
  }
};
global.document = { baseURI: 'https://example.test/app/' };
applyUrl('/app/?profile=default&q=hello&keep=1#frag');
global.S = { activeProfile: 'default', activeProfileIsDefault: true };
global.localStorage = {
  store: { 'hermes-webui-session': 'saved-local' },
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(this.store, key) ? this.store[key] : null;
  },
  removeItem(key) {
    delete this.store[key];
  }
};
evalSession('_profileQueryIntentFromLocation');
evalSession('_consumeProfileQueryParamFromLocation');
evalSession('_consumeComposerPrefillParamsFromLocation');
evalSession('_sessionUrlForSid');
evalBoot('_profileQueryBlocksSavedLocalRestore');
const intent = _profileQueryIntentFromLocation();
global.switchToProfile = async () => true;
(async () => {
  const savedLocalBefore = localStorage.getItem('hermes-webui-session');
  const profileSwitchProfileBefore = S.activeProfile || 'default';
  const profileSwitchIsDefaultBefore = !!S.activeProfileIsDefault;
  let profileSwitchCompleted = false;
  let profileSwitchChangedProfile = false;
  if (intent && intent.hasParam) {
    try {
      if (intent.valid) {
        if (typeof switchToProfile === 'function') {
          profileSwitchCompleted = await switchToProfile(intent.name) === true;
          if (profileSwitchCompleted) {
            profileSwitchChangedProfile = (S.activeProfile || 'default') !== profileSwitchProfileBefore || !!S.activeProfileIsDefault !== profileSwitchIsDefaultBefore;
            if (typeof _consumeProfileQueryParamFromLocation === 'function') _consumeProfileQueryParamFromLocation();
          }
        }
      } else {
        console.warn('[boot] ignored invalid profile query', intent.name);
      }
    } catch (e) {
      console.warn('[boot] profile query switch failed', e);
    }
  }
  const blocksSavedLocal = _profileQueryBlocksSavedLocalRestore(intent, null);
  if (blocksSavedLocal && profileSwitchCompleted && profileSwitchChangedProfile && localStorage.getItem('hermes-webui-session') === savedLocalBefore) localStorage.removeItem('hermes-webui-session');
  const cleanupGuardPos = bootSrc.indexOf("if(_profileQueryBlocksSavedLocal&&_profileSwitchCompleted&&_profileSwitchChangedProfile){", bootSrc.indexOf("const profileIntent=(typeof _profileQueryIntentFromLocation==='function')?_profileQueryIntentFromLocation():null;"));
  console.log(JSON.stringify({
    intent,
    blocksSavedLocal,
    profileSwitchCompleted,
    profileSwitchChangedProfile,
    savedLocalBefore,
    savedLocalAfter: localStorage.getItem('hermes-webui-session'),
    cleanupGuardPos,
  }));
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""
    payload = json.loads(_run_node(source))
    assert payload["intent"] == {"hasParam": True, "valid": True, "name": "default"}
    assert payload["blocksSavedLocal"] is True
    assert payload["profileSwitchCompleted"] is True
    assert payload["profileSwitchChangedProfile"] is False
    assert payload["savedLocalBefore"] == "saved-local"
    assert payload["savedLocalAfter"] == "saved-local"
    assert payload["cleanupGuardPos"] >= 0


def test_failed_profile_query_switch_keeps_saved_local_state():
    source = _node_prelude() + """
function applyUrl(rel) {
  const next = new URL(rel, 'https://example.test');
  window.location.href = next.href;
  window.location.pathname = next.pathname;
  window.location.search = next.search;
  window.location.hash = next.hash;
}
global.window = {
  location: {},
  history: {
    state: null,
    calls: [],
    replaceState(state, title, url) {
      this.calls.push({ state, title, url });
      this.state = state;
      applyUrl(url);
    }
  }
};
global.document = { baseURI: 'https://example.test/app/' };
const warns = [];
console.warn = (...args) => { warns.push(args.map(String)); };
applyUrl('/app/?profile=vops&q=hello&keep=1#frag');
global.localStorage = {
  store: { 'hermes-webui-session': 'saved-local' },
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(this.store, key) ? this.store[key] : null;
  },
  removeItem(key) {
    delete this.store[key];
  }
};
evalSession('_profileQueryIntentFromLocation');
evalSession('_consumeProfileQueryParamFromLocation');
evalSession('_consumeComposerPrefillParamsFromLocation');
evalSession('_sessionUrlForSid');
evalBoot('_profileQueryBlocksSavedLocalRestore');
const intent = _profileQueryIntentFromLocation();
global.switchToProfile = async () => { throw new Error('boom'); };
(async () => {
  const savedLocalBefore = localStorage.getItem('hermes-webui-session');
  let profileSwitchCompleted = false;
  if (intent && intent.hasParam) {
    try {
      if (intent.valid) {
        if (typeof switchToProfile === 'function') {
          profileSwitchCompleted = await switchToProfile(intent.name) === true;
          if (profileSwitchCompleted && typeof _consumeProfileQueryParamFromLocation === 'function') _consumeProfileQueryParamFromLocation();
        }
      } else {
        console.warn('[boot] ignored invalid profile query', intent.name);
        if (typeof _consumeProfileQueryParamFromLocation === 'function') _consumeProfileQueryParamFromLocation();
      }
    } catch (e) {
      console.warn('[boot] profile query switch failed', e);
    }
  }
  const blocksSavedLocal = _profileQueryBlocksSavedLocalRestore(intent, null);
  if (blocksSavedLocal && profileSwitchCompleted && localStorage.getItem('hermes-webui-session') === savedLocalBefore) localStorage.removeItem('hermes-webui-session');
  const afterBoot = window.location.pathname + window.location.search + window.location.hash;
  _consumeComposerPrefillParamsFromLocation();
  const afterPrefill = window.location.pathname + window.location.search + window.location.hash;
  const promoted = _sessionUrlForSid('abc 123');
  console.log(JSON.stringify({
    blocksSavedLocal,
    profileSwitchCompleted,
    savedLocalAfter: localStorage.getItem('hermes-webui-session'),
    afterBoot,
    afterPrefill,
    promoted,
    warns,
  }));
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""
    payload = json.loads(_run_node(source))
    assert payload["blocksSavedLocal"] is True
    assert payload["profileSwitchCompleted"] is False
    assert payload["savedLocalAfter"] == "saved-local"
    assert payload["afterBoot"] == "/app/?profile=vops&q=hello&keep=1#frag"
    assert payload["afterPrefill"] == "/app/?profile=vops&keep=1#frag"
    assert payload["promoted"] == "/app/session/abc%20123?profile=vops&keep=1#frag"
    assert payload["warns"] == [["[boot] profile query switch failed", "Error: boom"]]


def test_unsuccessful_profile_query_result_keeps_retry_url_without_warning():
    source = _node_prelude() + """
function applyUrl(rel) {
  const next = new URL(rel, 'https://example.test');
  window.location.href = next.href;
  window.location.pathname = next.pathname;
  window.location.search = next.search;
  window.location.hash = next.hash;
}
global.window = {
  location: {},
  history: {
    state: null,
    calls: [],
    replaceState(state, title, url) {
      this.calls.push({ state, title, url });
      this.state = state;
      applyUrl(url);
    }
  }
};
global.document = { baseURI: 'https://example.test/app/' };
const warns = [];
console.warn = (...args) => { warns.push(args.map(String)); };
applyUrl('/app/?profile=vops&q=hello&keep=1#frag');
global.localStorage = {
  store: { 'hermes-webui-session': 'saved-local' },
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(this.store, key) ? this.store[key] : null;
  },
  removeItem(key) {
    delete this.store[key];
  }
};
evalSession('_profileQueryIntentFromLocation');
evalSession('_consumeProfileQueryParamFromLocation');
evalBoot('_profileQueryBlocksSavedLocalRestore');
const intent = _profileQueryIntentFromLocation();
global.switchToProfile = async () => false;
(async () => {
  const savedLocalBefore = localStorage.getItem('hermes-webui-session');
  let profileSwitchCompleted = false;
  if (intent && intent.hasParam) {
    try {
      if (intent.valid) {
        if (typeof switchToProfile === 'function') {
          profileSwitchCompleted = await switchToProfile(intent.name) === true;
          if (profileSwitchCompleted && typeof _consumeProfileQueryParamFromLocation === 'function') _consumeProfileQueryParamFromLocation();
        }
      } else {
        console.warn('[boot] ignored invalid profile query', intent.name);
        if (typeof _consumeProfileQueryParamFromLocation === 'function') _consumeProfileQueryParamFromLocation();
      }
    } catch (e) {
      console.warn('[boot] profile query switch failed', e);
    }
  }
  const blocksSavedLocal = _profileQueryBlocksSavedLocalRestore(intent, null);
  if (blocksSavedLocal && profileSwitchCompleted && localStorage.getItem('hermes-webui-session') === savedLocalBefore) localStorage.removeItem('hermes-webui-session');
  console.log(JSON.stringify({
    blocksSavedLocal,
    profileSwitchCompleted,
    savedLocalAfter: localStorage.getItem('hermes-webui-session'),
    url: window.location.pathname + window.location.search + window.location.hash,
    historyCalls: window.history.calls,
    warns,
  }));
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""
    payload = json.loads(_run_node(source))
    assert payload["blocksSavedLocal"] is True
    assert payload["profileSwitchCompleted"] is False
    assert payload["savedLocalAfter"] == "saved-local"
    assert payload["url"] == "/app/?profile=vops&q=hello&keep=1#frag"
    assert payload["historyCalls"] == []
    assert payload["warns"] == []


def test_invalid_profile_query_warns_and_skips_switch():
    source = _node_prelude() + """
function applyUrl(rel) {
  const next = new URL(rel, 'https://example.test');
  window.location.href = next.href;
  window.location.pathname = next.pathname;
  window.location.search = next.search;
  window.location.hash = next.hash;
}
global.window = {
  location: {},
  history: {
    state: null,
    calls: [],
    replaceState(state, title, url) {
      this.calls.push({ state, title, url });
      this.state = state;
      applyUrl(url);
    }
  }
};
global.document = { baseURI: 'https://example.test/app/' };
const warns = [];
console.warn = (...args) => { warns.push(args); };
applyUrl('/app/?profile=../bad&q=hello&keep=1#frag');
evalSession('_profileQueryIntentFromLocation');
evalSession('_consumeProfileQueryParamFromLocation');
const intent = _profileQueryIntentFromLocation();
const switched = [];
global.switchToProfile = async (name) => { switched.push(name); };
(async () => {
  if (intent && intent.hasParam) {
    try {
      if (intent.valid) {
        if (typeof switchToProfile === 'function') await switchToProfile(intent.name);
      } else {
        console.warn('[boot] ignored invalid profile query', intent.name);
        if (typeof _consumeProfileQueryParamFromLocation === 'function') _consumeProfileQueryParamFromLocation();
      }
    } catch (e) {
      console.warn('[boot] profile query switch failed', e);
    }
  }
  console.log(JSON.stringify({
    intent,
    switched,
    warns,
    url: window.location.pathname + window.location.search + window.location.hash,
    historyCalls: window.history.calls,
  }));
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""
    payload = json.loads(_run_node(source))
    assert payload["intent"] == {"hasParam": True, "valid": False, "name": "../bad"}
    assert payload["switched"] == []
    assert payload["warns"] == [["[boot] ignored invalid profile query", "../bad"]]
    assert payload["url"] == "/app/?q=hello&keep=1#frag"
    assert payload["historyCalls"] == [{"state": None, "title": "", "url": "/app/?q=hello&keep=1#frag"}]


def test_prefill_cleanup_still_strips_q_prompt_and_send():
    source = _node_prelude() + """
function applyUrl(rel) {
  const next = new URL(rel, 'https://example.test');
  window.location.href = next.href;
  window.location.pathname = next.pathname;
  window.location.search = next.search;
  window.location.hash = next.hash;
}
global.window = {
  location: {},
  history: {
    state: null,
    calls: [],
    replaceState(state, title, url) {
      this.calls.push({ state, title, url });
      this.state = state;
      applyUrl(url);
    }
  }
};
global.document = { baseURI: 'https://example.test/app/' };
applyUrl('/app/?q=hello&prompt=hi&send=1&keep=1#frag');
evalSession('_consumeComposerPrefillParamsFromLocation');
_consumeComposerPrefillParamsFromLocation();
console.log(JSON.stringify({
  url: window.location.pathname + window.location.search + window.location.hash,
  historyCalls: window.history.calls,
}));
"""
    payload = json.loads(_run_node(source))
    assert payload["url"] == "/app/?keep=1#frag"
    assert payload["historyCalls"] == [{"state": None, "title": "", "url": "/app/?keep=1#frag"}]


def test_profile_query_blocks_only_implicit_saved_local_restore():
    source = _node_prelude() + """
evalBoot('_profileQueryBlocksSavedLocalRestore');
global.localStorage = {
  store: { 'hermes-webui-session': 'saved-local' },
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(this.store, key) ? this.store[key] : null;
  },
  setItem(key, value) {
    this.store[key] = String(value);
  },
  removeItem(key) {
    delete this.store[key];
  }
};
const validProfile = { hasParam: true, valid: true };
const invalidProfile = { hasParam: true, valid: false };
const blocksImplicit = _profileQueryBlocksSavedLocalRestore(validProfile, null);
if (blocksImplicit) localStorage.removeItem('hermes-webui-session');
const implicitAfter = localStorage.getItem('hermes-webui-session');
localStorage.setItem('hermes-webui-session', 'saved-local');
const allowsExplicit = _profileQueryBlocksSavedLocalRestore(validProfile, 'session-123');
if (allowsExplicit) localStorage.removeItem('hermes-webui-session');
const explicitAfter = localStorage.getItem('hermes-webui-session');
console.log(JSON.stringify({
  blocksImplicit,
  allowsExplicit,
  ignoresInvalid: _profileQueryBlocksSavedLocalRestore(invalidProfile, null),
  implicitAfter,
  explicitAfter,
}));
"""
    payload = json.loads(_run_node(source))
    assert payload == {
        "blocksImplicit": True,
        "allowsExplicit": False,
        "ignoresInvalid": False,
        "implicitAfter": None,
        "explicitAfter": "saved-local",
    }


def test_profile_transition_reasoning_refresh_hides_stale_chip_until_destination_resolves():
    source = f"""
const uiSrc = {UI_JS!r};
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = uiSrc.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = uiSrc.indexOf('{{', start), depth = 1; i++;
  while (depth > 0 && i < uiSrc.length) {{
    if (uiSrc[i] === '{{') depth++;
    else if (uiSrc[i] === '}}') depth--;
    i++;
  }}
  return uiSrc.slice(start, i);
}}
function makeEl() {{
  return {{
    style: {{}}, classList: {{ toggle(){{}}, add(){{}}, remove(){{}} }},
    setAttribute(){{}}, querySelectorAll(){{ return []; }},
  }};
}}
const els = {{
  composerReasoningWrap: makeEl(), composerReasoningLabel: makeEl(),
  composerReasoningChip: makeEl(), composerMobileReasoningAction: makeEl(),
}};
global.$ = id => els[id] || null;
global.S = {{ session: {{ model: 'gpt-5', model_provider: 'openai' }} }};
global._highlightReasoningOption = () => {{}};
global._applyReasoningOptions = () => {{}};
global._reasoningEffortQuery = () => '?model=gpt-5';
const pending = [];
let calls = 0;
global.api = () => {{
  calls++;
  return {{ then(ok) {{ pending.push(ok); return {{ catch() {{}} }}; }} }};
}};
var _currentReasoningEffort = 'low';
var _currentReasoningEffortsSupported = ['low', 'high'];
var _lastReasoningFetchKey = '?model=gpt-5';
var _reasoningFetchSeq = 7;
eval(extractFunc('_normalizeReasoningEffort'));
eval(extractFunc('_formatReasoningEffortLabel'));
eval(extractFunc('_applyReasoningChip'));
eval(extractFunc('fetchReasoningChip'));
eval(extractFunc('refreshProfileTransitionReasoningChip'));
fetchReasoningChip();
refreshProfileTransitionReasoningChip();
const beforeDestination = {{
  effort: _currentReasoningEffort,
  cachedKey: _lastReasoningFetchKey,
  wrapDisplay: els.composerReasoningWrap.style.display,
  calls,
}};
pending[0]({{ reasoning_effort: 'low', supported_efforts: ['low', 'high'] }});
const afterPreviousResponse = {{ effort: _currentReasoningEffort, wrapDisplay: els.composerReasoningWrap.style.display }};
pending[1]({{ reasoning_effort: 'high', supported_efforts: ['low', 'high'] }});
console.log(JSON.stringify({{ beforeDestination, afterPreviousResponse, afterDestination: _currentReasoningEffort, calls }}));
"""
    payload = json.loads(_run_node(source))
    assert payload["beforeDestination"] == {
        "effort": "",
        "cachedKey": "?model=gpt-5",
        "wrapDisplay": "none",
        "calls": 2,
    }
    assert payload["afterPreviousResponse"] == {"effort": "", "wrapDisplay": "none"}
    assert payload["afterDestination"] == "high"
    assert payload["calls"] == 2
    assert "refreshProfileTransitionReasoningChip" in PANELS_JS
    assert PANELS_JS.index("refreshProfileTransitionReasoningChip") > PANELS_JS.index("S.activeProfile = data.active || name")
    background = PANELS_JS[PANELS_JS.index("function _refreshProfileSwitchBackground"):PANELS_JS.index("async function loadProfilesPanel")]
    for refresh in (
        "_ensureComposerControlVisibilityState",
        "_setComposerControlOrder",
        "_renderComposerControlChips",
        "_renderComposerSituationalControlChips",
        "_applyComposerFooterVisibilitySettings",
        "_applyTitlebarProfileVisibility",
    ):
        assert refresh in background


def test_profile_transitions_fetch_destination_reasoning_after_hiding_stale_chip():
    source = f"""
const uiSrc = {UI_JS!r};
const panelsSrc = {PANELS_JS!r};
const sessionsSrc = {SESSIONS_JS!r};
function extractFunc(src, name) {{
  const re = new RegExp('(?:async\\\\s+)?function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start), depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
function makeEl() {{ return {{ style: {{}}, disabled: false, classList: {{ add(){{}}, remove(){{}}, toggle(){{}} }}, setAttribute(){{}}, querySelectorAll(){{ return []; }} }}; }}
const els = {{ composerReasoningWrap: makeEl(), composerReasoningLabel: makeEl(), composerReasoningChip: makeEl(), composerMobileReasoningAction: makeEl() }};
global.$ = id => els[id] || null;
global.window = {{}};
global.document = {{ title: '' }};
global.localStorage = {{ removeItem(){{}} }};
global.S = {{ activeProfile: 'default', activeProfileIsDefault: true, session: null, messages: [] }};
global._highlightReasoningOption = () => {{}};
global._applyReasoningOptions = () => {{}};
global._applyModelToDropdown = model => model;
global._modelStateForSelect = (_, model) => ({{ model, model_provider: 'openai' }});
global.renderSessionList = async () => {{}};
global.startGatewaySSE = () => {{}};
global.showToast = () => {{}};
global.t = value => value;
global.assistantDisplayName = () => 'Hermes';
global._profileSwitchPanelLoad = async () => {{}};
global._refreshProfileSwitchBackground = () => {{}};
var _profileSwitchGeneration = 0;
var _skillsData = null, _workspaceList = null;
var _currentReasoningEffort = 'low';
var _currentReasoningEffortsSupported = ['low', 'high'];
var _profileTransitionReasoningContext = null;
var _lastReasoningFetchKey = null;
var _reasoningFetchSeq = 0;
eval(extractFunc(uiSrc, '_normalizeReasoningEffort'));
eval(extractFunc(uiSrc, '_formatReasoningEffortLabel'));
eval(extractFunc(uiSrc, '_reasoningEffortContext'));
eval(extractFunc(uiSrc, '_reasoningEffortQuery'));
eval(extractFunc(uiSrc, '_applyReasoningChip'));
eval(extractFunc(uiSrc, 'fetchReasoningChip'));
eval(extractFunc(uiSrc, 'refreshProfileTransitionReasoningChip'));
eval(extractFunc(uiSrc, 'syncTopbar'));
eval(extractFunc(panelsSrc, 'switchToProfile'));
eval(extractFunc(sessionsSrc, '_switchProfileForSessionLoad'));
const pending = [];
const reasoningUrls = [];
global.api = (url) => {{
  if (url === '/api/profile/switch') return Promise.resolve({{ active: 'vops', is_default: false, default_model: 'gpt-high', default_model_provider: 'openai' }});
  if (url.startsWith('/api/reasoning')) {{
    reasoningUrls.push(url);
    return {{ then(ok) {{ pending.push(ok); return {{ catch() {{}} }}; }} }};
  }}
  throw new Error('unexpected API ' + url);
}};
fetchReasoningChip();
(async () => {{
  await switchToProfile('vops');
  const blankBoot = {{ hidden: els.composerReasoningWrap.style.display, urls: reasoningUrls.slice() }};
  pending[0]({{ reasoning_effort: 'low', supported_efforts: ['low', 'high'] }});
  const blankBootAfterOld = _currentReasoningEffort;
  pending[1]({{ reasoning_effort: 'high', supported_efforts: ['low', 'high'] }});
  const blankBootAfterNew = _currentReasoningEffort;
  S.activeProfile = 'default'; S.activeProfileIsDefault = true;
  S.session = {{ model: 'old-model', model_provider: 'old-provider', profile: 'default' }};
  _currentReasoningEffort = 'low'; _currentReasoningEffortsSupported = ['low', 'high']; _profileTransitionReasoningContext = null; _lastReasoningFetchKey = null;
  fetchReasoningChip();
  await _switchProfileForSessionLoad('vops');
  const directLoad = {{ hidden: els.composerReasoningWrap.style.display, urls: reasoningUrls.slice(2) }};
  pending[2]({{ reasoning_effort: 'low', supported_efforts: ['low', 'high'] }});
  const directLoadAfterOld = _currentReasoningEffort;
  pending[3]({{ reasoning_effort: 'high', supported_efforts: ['low', 'high'] }});
  console.log(JSON.stringify({{ blankBoot, blankBootAfterOld, blankBootAfterNew, directLoad, directLoadAfterOld, directLoadAfterNew: _currentReasoningEffort }}));
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
    payload = json.loads(_run_node(source))
    assert payload["blankBoot"] == {
        "hidden": "none",
        "urls": ["/api/reasoning", "/api/reasoning?model=gpt-high&provider=openai"],
    }
    assert payload["blankBootAfterOld"] == ""
    assert payload["blankBootAfterNew"] == "high"
    assert payload["directLoad"] == {
        "hidden": "none",
        "urls": ["/api/reasoning?model=old-model&provider=old-provider", "/api/reasoning?model=gpt-high&provider=openai"],
    }
    assert payload["directLoadAfterOld"] == ""
    assert payload["directLoadAfterNew"] == "high"


def test_blank_profile_transition_context_clears_before_explicit_model_change():
    source = f"""
const uiSrc = {UI_JS!r};
const bootSrc = {BOOT_JS!r};
function extractFunc(src, name) {{
  const re = new RegExp('(?:async\\\\s+)?function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start), depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
function makeEl() {{ return {{ style: {{}}, classList: {{ toggle(){{}} }}, setAttribute(){{}}, querySelectorAll(){{ return []; }} }}; }}
const els = {{
  modelSelect: {{ value: 'claude-sonnet' }},
  composerReasoningWrap: makeEl(),
  composerReasoningLabel: makeEl(),
  composerReasoningChip: makeEl(),
  composerMobileReasoningAction: makeEl()
}};
global.$ = id => els[id] || null;
global.S = {{ activeProfile: 'vops', session: null }};
global._highlightReasoningOption = () => {{}};
global._applyReasoningOptions = () => {{}};
global._modelStateForSelect = (_, model) => ({{ model, model_provider: 'anthropic' }});
var _currentReasoningEffort = 'high';
var _currentReasoningEffortsSupported = ['low', 'high'];
var _profileTransitionReasoningContext = {{ profile: 'vops', model: 'gpt-high', provider: 'openai' }};
var _lastReasoningFetchKey = '?model=gpt-high&provider=openai';
var _reasoningFetchSeq = 1;
const urls = [];
global.api = (url) => {{
  urls.push(url);
  return Promise.resolve({{ reasoning_effort: 'medium', supported_efforts: ['medium'] }});
}};
eval(extractFunc(uiSrc, '_normalizeReasoningEffort'));
eval(extractFunc(uiSrc, '_formatReasoningEffortLabel'));
eval(extractFunc(uiSrc, '_reasoningEffortContext'));
eval(extractFunc(uiSrc, '_reasoningEffortQuery'));
eval(extractFunc(uiSrc, '_applyReasoningChip'));
eval(extractFunc(uiSrc, 'fetchReasoningChip'));
eval(extractFunc(uiSrc, 'refreshProfileTransitionReasoningChip'));
eval(extractFunc(uiSrc, 'clearProfileTransitionReasoningContext'));
eval(extractFunc(uiSrc, 'syncReasoningChip'));
clearProfileTransitionReasoningContext();
syncReasoningChip();
const handler = bootSrc.slice(bootSrc.indexOf("$('modelSelect').onchange=async()=>"), bootSrc.indexOf("$('msg').addEventListener", bootSrc.indexOf("$('modelSelect').onchange=async()=>")));
const clearPos = handler.indexOf("clearProfileTransitionReasoningContext");
const rememberBlankPos = handler.indexOf("_rememberEmptyComposerModelOverride");
const noSessionSyncPos = handler.indexOf("syncReasoningChip()", rememberBlankPos);
console.log(JSON.stringify({{
  urls,
  context: _profileTransitionReasoningContext,
  lastKey: _lastReasoningFetchKey,
  clearPos,
  noSessionSyncPos
}}));
"""
    payload = json.loads(_run_node(source))
    assert payload["urls"] == ["/api/reasoning?model=claude-sonnet&provider=anthropic"]
    assert payload["context"] is None
    assert payload["lastKey"] == "?model=claude-sonnet&provider=anthropic"
    assert payload["clearPos"] >= 0
    assert payload["clearPos"] < payload["noSessionSyncPos"]


# ── #6559: profile tab URL helper ──────────────────────────────────────────

def test_profile_tab_url_builds_correct_href():
    source = _node_prelude() + """
evalSession('_profileTabUrl');
const PANELS_SRC = """ + repr(PANELS_JS) + """;
function makeGlobal() {
  globalThis.window = { location: { href: 'https://example.test/app/', pathname: '/app/', search: '?keep=1', hash: '#frag' } };
  globalThis.document = { baseURI: 'https://example.test/app/' };
  globalThis.sessionStorage = { store: {}, getItem(k) { return this.store[k] || null; }, setItem(k, v) { this.store[k] = v; } };
}
makeGlobal();
// Test 1: basic href
const basic = _profileTabUrl('vops');
// Test 2: with sessionStorage context
const ctx = 'abc123token';
globalThis.sessionStorage.setItem('hermes-tab-profile-ctx', ctx);
const withCtx = _profileTabUrl('vops');
// Test 3: encoded profile name
const encoded = _profileTabUrl('my-profile');
// Test 4: panels.js renders <a> with href
const anchorCreateMatch = PANELS_SRC.match(/createElement\('a'\)/);
const anchorHrefMatch = PANELS_SRC.match(/profileHref = _profileTabUrl\(p\.name\)/);
const setsHrefMatch = PANELS_SRC.match(/opt\.href = profileHref/);
console.log(JSON.stringify({
  basic, withCtx, encoded,
  createsAnchor: !!anchorCreateMatch,
  usesProfileHref: !!anchorHrefMatch,
  setsHrefAttr: !!setsHrefMatch
}));
"""
    payload = json.loads(_run_node(source))
    assert payload["createsAnchor"], "dropdown should create <a> elements"
    assert payload["usesProfileHref"], "dropdown should call _profileTabUrl"
    assert payload["setsHrefAttr"], "dropdown should set opt.href"
    assert "profile=vops" in payload["basic"], f"basic href should contain profile=vops, got {payload['basic']}"
    assert payload["basic"].startswith("/app/"), f"basic href should preserve path, got {payload['basic']}"
    assert "tab_context=abc123token" in payload["withCtx"], f"withCtx should contain tab_context, got {payload['withCtx']}"
    assert "profile=my-profile" in payload["encoded"], f"encoded should contain profile=my-profile, got {payload['encoded']}"


def test_profile_tab_url_preserves_existing_query_and_hash():
    source = _node_prelude() + """
evalSession('_profileTabUrl');
globalThis.window = { location: { href: 'https://example.test/app/?q=hello&keep=1#frag', pathname: '/app/', search: '?q=hello&keep=1', hash: '#frag' } };
globalThis.document = { baseURI: 'https://example.test/app/' };
globalThis.sessionStorage = { getItem() { return null; } };
const url = _profileTabUrl('vops');
console.log(JSON.stringify({ url }));
"""
    payload = json.loads(_run_node(source))
    assert "profile=vops" in payload["url"], f"url should contain profile=vops, got {payload['url']}"
    assert "keep=1" in payload["url"], f"url should preserve keep=1, got {payload['url']}"
    assert "#frag" in payload["url"], f"url should preserve hash, got {payload['url']}"


def test_profile_dropdown_creates_anchor_elements_with_click_guards():
    render_start = PANELS_JS.index("function renderProfileDropdown(")
    render_end = PANELS_JS.index("function toggleProfileDropdown(", render_start)
    render_func = PANELS_JS[render_start:render_end]
    assert "createElement('a')" in render_func, "must create <a> elements"
    assert "opt.href = profileHref" in render_func, "must set href attribute"
    assert "addEventListener" in render_func, "must use addEventListener"
    has_mod = "e.button !== 0" in render_func or "e.ctrlKey" in render_func
    assert has_mod, "must check modifier keys"
    assert "preventDefault" in render_func, "must call preventDefault on plain click"
    assert "switchToProfile(p.name)" in render_func, "must call switchToProfile on plain click"


def test_log_redaction_removes_tab_context_from_path():
    import re as _re
    redact_re = _re.compile(r'(?<=[?&])tab_context=[^&\s]+')
    cases = [
        ("/api/sessions/events", "/api/sessions/events"),
        ("/api/sessions/events?tab_context=abc123", "/api/sessions/events?tab_context=<redacted>"),
        ("/api/chat/stream?stream_id=xyz", "/api/chat/stream?stream_id=xyz"),
        ("/api/chat/stream?stream_id=xyz&tab_context=abc123&keep=1",
         "/api/chat/stream?stream_id=xyz&tab_context=<redacted>&keep=1"),
        ("/api/profile/active?tab_context=abc123", "/api/profile/active?tab_context=<redacted>"),
        ("?tab_context=abc123", "?tab_context=<redacted>"),
        ("/api/upload?tab_context=abc123", "/api/upload?tab_context=<redacted>"),
    ]
    for raw, expected in cases:
        result = redact_re.sub('tab_context=<redacted>', raw)
        assert result == expected, f"redact({raw!r}) = {result!r}, expected {expected!r}"
    server_src = (Path(__file__).parent.parent / "server.py").read_text(encoding="utf-8")
    assert "_REDACT_TAB_CONTEXT" in server_src
    assert "tab_context=<redacted>" in server_src
