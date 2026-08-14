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
  const paramsStart = src.indexOf('(', start);
  let paramsDepth = 0;
  let paramsEnd = -1;
  for (let p = paramsStart; p < src.length; p++) {{
    if (src[p] === '(') paramsDepth++;
    else if (src[p] === ')' && --paramsDepth === 0) {{
      paramsEnd = p;
      break;
    }}
  }}
  let i = src.indexOf('{{', paramsEnd);
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
  const savedPos = bootSrc.indexOf("const saved=_profileScopedUrlSession.blocked?null:(urlSession||savedLocal);", profilePos);
  const loadPos = bootSrc.indexOf("await loadSession(saved, _profileScopedUrlSession.loadOptions);", profilePos);
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


def test_profile_scoped_url_session_boot_state_fails_closed_and_disables_owner_discovery():
    source = _node_prelude() + """
global.S = { activeProfile: 'default', activeProfileIsDefault: true };
evalSession('_profileMatchesActiveProfile');
evalBoot('_profileScopedUrlSessionState');
const valid = { hasParam:true, valid:true, name:'team_1' };
const invalid = { hasParam:true, valid:false, name:'Bad' };
const cases = {
  ready: _profileScopedUrlSessionState(valid, 'abc123', true, 'team_1'),
  failedSwitch: _profileScopedUrlSessionState(valid, 'abc123', false, 'default'),
  mismatchedActive: _profileScopedUrlSessionState(valid, 'abc123', true, 'other'),
  invalid: _profileScopedUrlSessionState(invalid, 'abc123', false, 'default'),
  profileless: _profileScopedUrlSessionState(null, 'abc123', false, 'default'),
  noUrlSession: _profileScopedUrlSessionState(valid, null, false, 'default'),
};
console.log(JSON.stringify(cases));
"""
    payload = json.loads(_run_node(source))
    assert payload["ready"] == {
        "explicit": True,
        "blocked": False,
        "loadOptions": {"preserveActiveInput": True, "skipProfileResolve": True},
    }
    for key in ("failedSwitch", "mismatchedActive", "invalid"):
        assert payload[key]["explicit"] is True
        assert payload[key]["blocked"] is True
        assert payload[key]["loadOptions"]["skipProfileResolve"] is True
    for key in ("profileless", "noUrlSession"):
        assert payload[key] == {
            "explicit": False,
            "blocked": False,
            "loadOptions": {"preserveActiveInput": True},
        }


def test_duplicate_profile_query_is_invalid_and_profileless_url_clears_stale_intent():
    assert "if(opts.clearProfileIntent) _setActiveSessionUrl(sid,null);" in SESSIONS_JS
    assert "_setActiveSessionUrl(S.session.session_id,opts.clearProfileIntent?null:undefined);" in SESSIONS_JS
    source = _node_prelude() + """
global.window = {
  location: new URL('https://example.test/app/?profile=team&profile=other&keep=1#frag'),
  history: {
    calls: [],
    pushState(state, title, url) { this.calls.push({state,title,url}); }
  }
};
global.document = { baseURI: 'https://example.test/app/' };
evalSession('_profileQueryIntentFromLocation');
evalSession('_sessionUrlForSid');
evalSession('_setActiveSessionUrl');
const intent = _profileQueryIntentFromLocation();
const profileless = _sessionUrlForSid('abc123', null);
const inherited = _sessionUrlForSid('abc123');
_setActiveSessionUrl('abc123', null);
console.log(JSON.stringify({intent,profileless,inherited,promoted:window.history.calls[0]}));
"""
    payload = json.loads(_run_node(source))
    assert payload["intent"] == {"hasParam": True, "valid": False, "name": "team"}
    assert payload["profileless"] == "/app/session/abc123?keep=1#frag"
    assert payload["inherited"] == "/app/session/abc123?profile=team&profile=other&keep=1#frag"
    assert payload["promoted"] == {
        "state": {"session_id": "abc123"},
        "title": "",
        "url": "/app/session/abc123?keep=1#frag",
    }


def test_session_reference_navigation_switches_before_load_and_fails_closed():
    source = _node_prelude() + f"""
const uiSrc = {UI_JS!r};
function extractUi(name) {{
  const re = new RegExp('(?:async\\\\s+)?function\\\\s+' + name + '\\\\s*\\\\(');
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
global.window = {{ location: new URL('https://example.test/app/') }};
global.document = {{ baseURI: 'https://example.test/app/' }};
global.S = {{ activeProfile: 'default', activeProfileIsDefault: true }};
global._showAllProfiles = false;
global._profileSwitchOpeningExistingSession = false;
evalSession('_profileMatchesActiveProfile');
evalSession('_sidebarSessionProfileName');
evalSession('_isValidProfileName');
evalSession('_switchToSessionProfile');
evalSession('_ensureSidebarSessionProfile');
evalSession('_openSessionReference');
evalSession('_sessionUrlForSid');
globalThis._navigateInternalSessionLink = (0, eval)('(' + extractUi('_navigateInternalSessionLink') + ')');
const events = [];
let switchResult = true;
let switchUpdatesProfile = true;
let switchThrows = false;
global.switchToProfile = async (name) => {{
  events.push({{ kind: 'switch', name, existingGuard: _profileSwitchOpeningExistingSession }});
  if (switchThrows) throw new Error('switch failed');
  if (switchResult && switchUpdatesProfile) {{
    S.activeProfile = name;
    S.activeProfileIsDefault = name === 'default';
  }}
  return switchResult;
}};
global.loadSession = async (sid, opts) => {{ events.push({{ kind: 'load', sid, skipProfileResolve: !!(opts && opts.skipProfileResolve), clearProfileIntent: !!(opts && opts.clearProfileIntent), force: !!(opts && opts.force) }}); }};
async function attempt(href, opts={{}}) {{
  events.length = 0;
  S.activeProfile = opts.active || 'default';
  S.activeProfileIsDefault = S.activeProfile === 'default';
  S.session = opts.session || null;
  switchResult = opts.switchResult === undefined ? true : opts.switchResult;
  switchUpdatesProfile = opts.switchUpdatesProfile !== false;
  switchThrows = opts.switchThrows === true;
  const result = await _navigateInternalSessionLink(href);
  return {{ result, events: events.slice() }};
}}
(async () => {{
  const success = await attempt('/app/session/abc123?profile=team_1');
  const failed = await attempt('/app/session/abc123?profile=team_1', {{ switchResult: false }});
  const threw = await attempt('/app/session/abc123?profile=team_1', {{ switchThrows: true }});
  const mismatched = await attempt('/app/session/abc123?profile=team_1', {{ switchUpdatesProfile: false }});
  const invalid = await attempt('/app/session/abc123?profile=Bad');
  const duplicate = await attempt('/app/session/abc123?profile=team_1&profile=other');
  const crossProfileSameSid = await attempt('/app/session/abc123?profile=team_1', {{ active:'alpha', session:{{session_id:'abc123',profile:'alpha'}} }});
  const same = await attempt('/app/session/abc123?profile=team_1', {{ active: 'team_1' }});
  const profileless = await attempt('/app/session/plain123');
  console.log(JSON.stringify({{ success, failed, threw, mismatched, invalid, duplicate, crossProfileSameSid, same, profileless }}));
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
    payload = json.loads(_run_node(source))
    assert payload["success"] == {
        "result": True,
        "events": [
            {"kind": "switch", "name": "team_1", "existingGuard": True},
            {"kind": "load", "sid": "abc123", "skipProfileResolve": True, "clearProfileIntent": False, "force": False},
        ],
    }
    assert payload["failed"] == {"result": False, "events": [{"kind": "switch", "name": "team_1", "existingGuard": True}]}
    assert payload["threw"] == {"result": False, "events": [{"kind": "switch", "name": "team_1", "existingGuard": True}]}
    assert payload["mismatched"] == {"result": False, "events": [{"kind": "switch", "name": "team_1", "existingGuard": True}]}
    assert payload["invalid"] == {"result": False, "events": []}
    assert payload["duplicate"] == {"result": False, "events": []}
    assert payload["crossProfileSameSid"] == {
        "result": True,
        "events": [
            {"kind": "switch", "name": "team_1", "existingGuard": True},
            {"kind": "load", "sid": "abc123", "skipProfileResolve": True, "clearProfileIntent": False, "force": True},
        ],
    }
    assert payload["same"] == {
        "result": True,
        "events": [{"kind": "load", "sid": "abc123", "skipProfileResolve": True, "clearProfileIntent": False, "force": False}],
    }
    assert payload["profileless"] == {
        "result": True,
        "events": [{"kind": "load", "sid": "plain123", "skipProfileResolve": False, "clearProfileIntent": True, "force": False}],
    }


def test_sidebar_open_keeps_ordinary_profile_filtered_navigation():
    source = _node_prelude() + """
global._showAllProfiles = false;
global._profileSwitchOpeningExistingSession = false;
global.S = { activeProfile: 'default', activeProfileIsDefault: true };
evalSession('_profileMatchesActiveProfile');
evalSession('_sidebarSessionProfileName');
evalSession('_switchToSessionProfile');
evalSession('_ensureSidebarSessionProfile');
evalSession('_openSidebarSession');
const events = [];
global._isExternalSession = () => false;
global.switchToProfile = async (name) => { events.push({kind:'switch', name}); return true; };
global.loadSession = async (sid) => { events.push({kind:'load', sid}); };
global.renderSessionListFromCache = () => {};
(async () => {
  await _openSidebarSession({session_id:'ordinary', profile:'team_1'});
  console.log(JSON.stringify(events));
})().catch(error => { console.error(error); process.exit(1); });
"""
    assert json.loads(_run_node(source)) == [{"kind": "load", "sid": "ordinary"}]


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
