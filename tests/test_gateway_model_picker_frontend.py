"""
Frontend regression tests for issue #2 (gateway model freshness).

These guard the WebUI side of the fix:

1. ``refreshGatewayModelOptions`` exists on the global ``window`` object
   and re-fetches ``/api/models`` (so callers on other modules — like the
   gateway SSE handler in sessions.js — can call it without imports).
2. Opening the composer model dropdown calls
   ``refreshGatewayModelOptions`` so the picker shows console-side
   gateway changes within seconds (not on the next 24 h cache miss).
3. The gateway SSE ``sessions_changed`` handler in sessions.js also
   calls it — so changes propagate even while the dropdown is closed.

Run with ``node`` if available; skipped otherwise.
"""

import json
import pathlib
import shutil
import subprocess

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_JS = REPO_ROOT / "static" / "ui.js"
SESSIONS_JS = REPO_ROOT / "static" / "sessions.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def test_refresh_gateway_model_options_is_exported_on_window():
    """``refreshGatewayModelOptions`` must be reachable from sessions.js
    (which imports nothing — it runs in the same global scope as ui.js).
    Without ``window.refreshGatewayModelOptions = …`` the SSE hook would
    silently no-op."""
    src = UI_JS.read_text()
    assert "function refreshGatewayModelOptions" in src, \
        "refreshGatewayModelOptions(): function declaration missing"
    assert "window.refreshGatewayModelOptions" in src, (
        "refreshGatewayModelOptions must be attached to window so the "
        "gateway SSE handler in sessions.js can call it without imports."
    )


def test_toggle_model_dropdown_triggers_gateway_refresh():
    """Opening the picker must refresh the gateway slice — otherwise a
    user opening the dropdown right after a console-side change still
    sees stale options."""
    src = UI_JS.read_text()
    # Locate the toggleModelDropdown body and ensure it references the
    # refresh helper.
    start = src.find("function toggleModelDropdown")
    assert start >= 0, "toggleModelDropdown not found in ui.js"
    end = src.find("function ", start + 1)
    body = src[start:end if end > 0 else len(src)]
    assert "refreshGatewayModelOptions" in body, (
        "toggleModelDropdown does not call refreshGatewayModelOptions(); "
        "users would see stale gateway models when re-opening the picker."
    )


def test_sessions_changed_sse_triggers_gateway_refresh():
    """Gateway SSE ``sessions_changed`` must refresh the model picker so
    console-side changes propagate to open WebUI tabs without user action.
    """
    src = SESSIONS_JS.read_text()
    idx = src.find("'sessions_changed'")
    if idx < 0:
        idx = src.find('"sessions_changed"')
    assert idx >= 0, "sessions_changed listener not found in sessions.js"
    # Look at the surrounding ~3000 chars for the refresh call.
    window = src[idx:idx + 3000]
    assert "refreshGatewayModelOptions" in window, (
        "sessions_changed handler does not call "
        "window.refreshGatewayModelOptions(); console-side gateway model "
        "changes will not reach already-open WebUI tabs."
    )


def test_refresh_strips_only_gateway_groups_not_user_providers(tmp_path):
    """Drive the actual JS function with a jsdom-style stub and verify
    that only gateway optgroups are rebuilt — openai / anthropic / etc.
    must survive untouched so the user's selection is preserved."""
    driver = tmp_path / "driver.js"
    driver.write_text(r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// Minimal stubs for the bits of ui.js that refreshGatewayModelOptions touches.
const optgroups = [];
function makeOptgroup(label, providerId){
  const og = {tagName:'OPTGROUP', label, dataset:{provider: providerId||''},
              children: [], appendChild(c){ this.children.push(c); c.parentNode=this; },
              remove(){ const i = optgroups.indexOf(this); if(i>=0) optgroups.splice(i,1); }};
  optgroups.push(og);
  return og;
}
function makeOption(value,text){
  return {tagName:'OPTION', value, textContent:text};
}

const sel = {
  id:'modelSelect',
  value:'openai/gpt-4',
  get options(){ const out = []; for(const og of optgroups) for(const o of og.children) out.push(o); return out; },
  appendChild(og){ optgroups.push(og); },
  querySelectorAll(){ return optgroups.slice(); },
};

global.window = { _activeProvider: null };
global.document = {
  createElement(t){
    if(t==='optgroup') return makeOptgroup('','');
    if(t==='option')   return {tagName:'OPTION', value:'', textContent:''};
    return {};
  }
};
global.location = { href: 'http://test/' };
global.URL = require('url').URL;

// Stub fetch to return the test scenario.
const SCENARIO = JSON.parse(process.argv[3]);
global.fetch = async (url, init) => ({
  ok:true, status:200,
  json: async () => SCENARIO,
});
function $(id){ return id==='modelSelect' ? sel : null; }
function _redirectIfUnauth(){ return false; }
function syncModelChip(){}
let _dynamicModelLabels = {};

// Seed the select with the "before" state.
const ogOpenai = makeOptgroup('openai','openai');
ogOpenai.appendChild(makeOption('openai/gpt-4','GPT-4'));
const ogStaleGw = makeOptgroup('gateway-prod','gateway-prod');
ogStaleGw.appendChild(makeOption('gateway-prod/old-model','Old'));

// Pull just refreshGatewayModelOptions out of ui.js.
const m = src.match(/function _isGatewayProvider[\s\S]*?\nfunction toggleModelDropdown/);
if(!m){ console.error('FAIL_EXTRACT'); process.exit(2); }
eval(m[0].replace(/\nfunction toggleModelDropdown[\s\S]*$/, ''));

(async () => {
  const changed = await refreshGatewayModelOptions();
  const out = optgroups.map(og => ({
    label: og.label,
    provider: og.dataset.provider,
    models: og.children.map(c => c.value),
  }));
  console.log(JSON.stringify({changed, groups: out}));
})();
""")
    scenario = json.dumps({
        "groups": [
            {"provider": "openai",
             "provider_id": "openai",
             "models": [{"id": "openai/gpt-4", "label": "GPT-4"}]},
            {"provider": "gateway-prod",
             "provider_id": "gateway-prod",
             "models": [{"id": "gateway-prod/new-model", "label": "New"}]},
        ],
    })
    res = subprocess.run(
        [NODE, str(driver), str(UI_JS), scenario],
        capture_output=True, text=True, timeout=15,
    )
    assert res.returncode == 0, f"node failed: {res.stderr or res.stdout}"
    out = json.loads(res.stdout.strip().splitlines()[-1])
    providers = [g["provider"] for g in out["groups"]]
    assert "openai" in providers, "openai optgroup must NOT be touched"
    assert "gateway-prod" in providers, "fresh gateway optgroup must be appended"
    # Stale gateway model is gone.
    flat = [m for g in out["groups"] for m in g["models"]]
    assert "gateway-prod/old-model" not in flat, "stale gateway model not removed"
    assert "gateway-prod/new-model" in flat, "fresh gateway model not added"
    # Non-gateway model survived.
    assert "openai/gpt-4" in flat, "non-gateway model was incorrectly removed"
