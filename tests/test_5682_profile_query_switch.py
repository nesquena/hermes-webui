"""Regression tests for #5682 profile query boot switching."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
BOOT_JS = BOOT_JS_PATH.read_text(encoding="utf-8")
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
};
(async () => {
  const promoted = _sessionUrlForSid('abc 123');
  const savedLocalBefore = localStorage.getItem('hermes-webui-session');
  const profileSwitchProfileBefore = S.activeProfile || 'default';
  const profileSwitchIsDefaultBefore = !!S.activeProfileIsDefault;
  let profileSwitchCompleted = false;
  let profileSwitchChangedProfile = false;
  if (intent && intent.hasParam) {
    try {
      if (intent.valid) {
        if (typeof switchToProfile === 'function') {
          await switchToProfile(intent.name);
          profileSwitchCompleted = true;
          profileSwitchChangedProfile = (S.activeProfile || 'default') !== profileSwitchProfileBefore || !!S.activeProfileIsDefault !== profileSwitchIsDefaultBefore;
        }
      } else {
        console.warn('[boot] ignored invalid profile query', intent.name);
      }
    } catch (e) {
      console.warn('[boot] profile query switch failed', e);
    } finally {
      if (typeof _consumeProfileQueryParamFromLocation === 'function') _consumeProfileQueryParamFromLocation();
    }
  }
  const blocksSavedLocal = _profileQueryBlocksSavedLocalRestore(intent, null);
  if (blocksSavedLocal && profileSwitchCompleted && profileSwitchChangedProfile && localStorage.getItem('hermes-webui-session') === savedLocalBefore) localStorage.removeItem('hermes-webui-session');
  const savedLocalAfterSuppress = localStorage.getItem('hermes-webui-session');
  const savedLocalAfterReload = localStorage.getItem('hermes-webui-session');
  const keepsExplicitSession = _profileQueryBlocksSavedLocalRestore(intent, 'session-123');
  const afterProfile = window.location.pathname + window.location.search + window.location.hash;
  _consumeComposerPrefillParamsFromLocation();
  const afterPrefill = window.location.pathname + window.location.search + window.location.hash;
  const profilePos = bootSrc.indexOf("const profileIntent=(typeof _profileQueryIntentFromLocation==='function')?_profileQueryIntentFromLocation():null;");
  const renderPos = bootSrc.indexOf("await renderSessionList();", profilePos);
  const savedPos = bootSrc.indexOf("const saved=urlSession||savedLocal;", profilePos);
  const loadPos = bootSrc.indexOf("await loadSession(saved, {preserveActiveInput:true});", profilePos);
  const consumePos = bootSrc.indexOf("if(typeof _consumeProfileQueryParamFromLocation==='function') _consumeProfileQueryParamFromLocation();", profilePos);
  const completedPos = bootSrc.indexOf("_profileSwitchCompleted=true;", profilePos);
  const changedPos = bootSrc.indexOf("_profileSwitchChangedProfile=", completedPos);
  const cleanupGuardPos = bootSrc.indexOf("if(_profileQueryBlocksSavedLocal&&_profileSwitchCompleted&&_profileSwitchChangedProfile){", profilePos);
  const fetchPos = bootSrc.indexOf("fetchReasoningChip()");
  console.log(JSON.stringify({ intent, switched, promoted, afterProfile, afterPrefill, historyCalls: window.history.calls, profilePos, renderPos, savedPos, loadPos, consumePos, completedPos, changedPos, cleanupGuardPos, fetchPos, savedLocalBefore, savedLocalAfterSuppress, savedLocalAfterReload, blocksSavedLocal, keepsExplicitSession }));
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
    assert payload["fetchPos"] < payload["profilePos"]
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
evalBoot('_profileQueryBlocksSavedLocalRestore');
const intent = _profileQueryIntentFromLocation();
global.switchToProfile = async () => {};
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
          await switchToProfile(intent.name);
          profileSwitchCompleted = true;
          profileSwitchChangedProfile = (S.activeProfile || 'default') !== profileSwitchProfileBefore || !!S.activeProfileIsDefault !== profileSwitchIsDefaultBefore;
        }
      } else {
        console.warn('[boot] ignored invalid profile query', intent.name);
      }
    } catch (e) {
      console.warn('[boot] profile query switch failed', e);
    } finally {
      if (typeof _consumeProfileQueryParamFromLocation === 'function') _consumeProfileQueryParamFromLocation();
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
          await switchToProfile(intent.name);
          profileSwitchCompleted = true;
        }
      } else {
        console.warn('[boot] ignored invalid profile query', intent.name);
      }
    } catch (e) {
      console.warn('[boot] profile query switch failed', e);
    } finally {
      if (typeof _consumeProfileQueryParamFromLocation === 'function') _consumeProfileQueryParamFromLocation();
    }
  }
  const blocksSavedLocal = _profileQueryBlocksSavedLocalRestore(intent, null);
  if (blocksSavedLocal && profileSwitchCompleted && localStorage.getItem('hermes-webui-session') === savedLocalBefore) localStorage.removeItem('hermes-webui-session');
  console.log(JSON.stringify({
    blocksSavedLocal,
    profileSwitchCompleted,
    savedLocalAfter: localStorage.getItem('hermes-webui-session'),
    url: window.location.pathname + window.location.search + window.location.hash,
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
    assert payload["url"] == "/app/?q=hello&keep=1#frag"
    assert payload["warns"] == [["[boot] profile query switch failed", "Error: boom"]]


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
      }
    } catch (e) {
      console.warn('[boot] profile query switch failed', e);
    } finally {
      if (typeof _consumeProfileQueryParamFromLocation === 'function') _consumeProfileQueryParamFromLocation();
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
