"""Public composer behavior for the separate Fast mode toggle."""

import json
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")
UI = (ROOT / "static/ui.js").read_text(encoding="utf-8")
I18N = (ROOT / "static/i18n.js").read_text(encoding="utf-8")


def test_fast_button_dom_order_label_icon_and_accessibility():
    reasoning = HTML.index('id="composerReasoningWrap"')
    fast = HTML.index('id="composerFastModeButton"')
    toolsets = HTML.index('id="composerToolsetsWrap"')
    assert reasoning < fast < toolsets
    button = re.search(r'<button[^>]+id="composerFastModeButton".*?</button>', HTML, re.S).group(0)
    assert 'type="button"' in button
    assert 'aria-label="Fast mode' in button
    assert 'title="Fast mode' in button
    assert 'aria-pressed="false"' in button
    assert 'class="composer-fast-mode-icon"' in button
    assert re.search(r'<svg[^>]+fill="none"[^>]*>.*?<path', button, re.S), (
        "Fast must use an outlined SVG lightning icon while inactive"
    )
    assert 'class="composer-fast-mode-label"' in button
    assert 'data-i18n="composer_fast_mode">Fast</span>' in button


def test_fast_selected_state_fills_yellow_bolt_with_static_glow():
    icon_match = re.search(r"\.composer-fast-mode-icon\{([^}]*)\}", CSS)
    selected_icon_match = re.search(
        r'\[aria-pressed="true"\] \.composer-fast-mode-icon\{([^}]*)\}', CSS
    )
    selected_path_match = re.search(
        r'\[aria-pressed="true"\] \.composer-fast-mode-icon path\{([^}]*)\}', CSS
    )
    assert icon_match and selected_icon_match and selected_path_match
    icon_rule = icon_match.group(1)
    selected_icon_rule = selected_icon_match.group(1)
    selected_path_rule = selected_path_match.group(1)
    assert "color:var(--muted)" in icon_rule
    assert "color:var(--warning)" in selected_icon_rule
    assert "drop-shadow(" in selected_icon_rule
    assert "fill:currentColor" in selected_path_rule
    assert "@keyframes" not in CSS[CSS.index('.composer-fast-mode-btn'):CSS.index('.composer-reasoning-icon')]


def test_mobile_fast_action_follows_reasoning_and_has_on_off_copy():
    reasoning = HTML.index('id="composerMobileReasoningAction"')
    fast = HTML.index('id="composerMobileFastModeAction"')
    context = HTML.index('id="composerMobileContextAction"')
    assert reasoning < fast < context
    assert 'id="composerMobileFastModeLabel"' in HTML
    assert 'data-i18n="composer_fast_mode"' in HTML
    assert 'data-i18n="composer_fast_off"' in HTML
    assert "composer_fast_on" in UI and "composer_fast_off" in UI


def test_fast_mode_copy_has_strict_locale_parity():
    keys = (
        "composer_fast_mode",
        "composer_fast_on",
        "composer_fast_off",
        "composer_fast_mode_profile",
        "composer_fast_mode_on",
        "composer_fast_mode_off",
        "composer_fast_mode_enabled",
        "composer_fast_mode_disabled",
        "composer_fast_mode_failed",
    )
    for key in keys:
        assert I18N.count(f"{key}:") == 15
    assert HTML.count('data-i18n-title="composer_fast_mode_profile"') == 2
    assert HTML.count('data-i18n-aria-label="composer_fast_mode_profile"') == 2


def test_responsive_css_hides_desktop_but_keeps_mobile_action():
    assert ".composer-fast-mode-btn[hidden]{display:none!important" in CSS.replace(" ", "")
    assert ".composer-footer.cf-burger .composer-fast-mode-btn" in CSS
    phone = CSS[CSS.index("@media(max-width:640px)") :]
    assert ".composer-fast-mode-btn" in phone
    assert "display:none!important" in phone
    assert ".composer-mobile-config-action" in CSS
    assert "min-height:44px" in CSS


def test_fast_mode_runtime_dedupes_and_suppresses_stale_responses(tmp_path):
    start = UI.index("// ── Fast mode")
    end = UI.index("// ── Session toolsets chip", start)
    source = UI[start:end]
    script = f"""
const vm=require('vm');
const els={{}};
function makeEl(){{return {{style:{{}},disabled:false,hidden:false,textContent:'',attrs:{{}},classList:{{toggle(){{}},add(){{}},remove(){{}}}},setAttribute(k,v){{this.attrs[k]=String(v)}}}}}}
['composerFastModeButton','composerMobileFastModeAction','composerMobileFastModeLabel'].forEach(k=>els[k]=makeEl());
const pending=[]; const urls=[];
const tr={{composer_fast_mode_on:'Fast mode on (profile-wide)',composer_fast_mode_off:'Fast mode off (profile-wide)',composer_fast_on:'On',composer_fast_off:'Off'}};
const context={{console,URLSearchParams,$:(id)=>els[id],t:(key)=>tr[key]||key,S:{{session:{{model:'gpt-old',model_provider:'openai'}}}},api:(url)=>{{urls.push(url);return new Promise((resolve,reject)=>pending.push({{resolve,reject}}))}},showToast:()=>{{}}}};
vm.createContext(context); vm.runInContext({json.dumps(source)},context);
context.syncFastMode(); context.syncFastMode();
context.S.session={{model:'gpt-new',model_provider:'openai'}}; context.syncFastMode();
pending[1].resolve({{supported:true,enabled:true,service_tier:'priority'}});
setImmediate(()=>{{pending[0].resolve({{supported:false,enabled:false,service_tier:''}});setImmediate(()=>{{console.log(JSON.stringify({{urls,pressed:els.composerFastModeButton.attrs['aria-pressed'],hidden:els.composerFastModeButton.hidden,label:els.composerMobileFastModeLabel.textContent}}))}})}});
"""
    path = tmp_path / "fast-test.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(path)], capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert len(payload["urls"]) == 2, "routine sync must dedupe an in-flight identity"
    assert "gpt-old" in payload["urls"][0] and "gpt-new" in payload["urls"][1]
    assert payload == {**payload, "pressed": "true", "hidden": False, "label": "On"}


def test_fast_mode_mutation_reconciles_current_model_after_switch(tmp_path):
    start = UI.index("// ── Fast mode")
    end = UI.index("// ── Session toolsets chip", start)
    source = UI[start:end]
    script = f"""
const vm=require('vm');
const els={{}};
function makeEl(){{return {{style:{{}},disabled:false,hidden:false,textContent:'',attrs:{{}},classList:{{toggle(){{}},add(){{}},remove(){{}}}},setAttribute(k,v){{this.attrs[k]=String(v)}}}}}}
['composerFastModeButton','composerMobileFastModeAction','composerMobileFastModeLabel'].forEach(k=>els[k]=makeEl());
const pending=[]; const calls=[];
const tr={{composer_fast_mode_on:'Fast mode on (profile-wide)',composer_fast_mode_off:'Fast mode off (profile-wide)',composer_fast_on:'On',composer_fast_off:'Off',composer_fast_mode_enabled:'enabled',composer_fast_mode_disabled:'disabled'}};
const context={{console,URLSearchParams,$:(id)=>els[id],t:(key)=>tr[key]||key,S:{{session:{{model:'gpt-old',model_provider:'openai'}}}},api:(url,opts)=>{{calls.push({{url,method:(opts&&opts.method)||'GET'}});return new Promise((resolve,reject)=>pending.push({{resolve,reject}}))}},showToast:()=>{{}}}};
vm.createContext(context); vm.runInContext({json.dumps(source)},context);
context._applyFastModeState({{supported:true,enabled:false,service_tier:''}},false,false);
context.toggleFastMode();
context.S.session={{model:'gpt-new',model_provider:'openai'}}; context.syncFastMode();
pending[1].resolve({{supported:true,enabled:false,service_tier:''}});
setImmediate(()=>{{
  pending[0].resolve({{supported:true,enabled:true,service_tier:'priority'}});
  setImmediate(()=>{{
    pending[2].resolve({{supported:true,enabled:true,service_tier:'priority'}});
    setImmediate(()=>console.log(JSON.stringify({{calls,pressed:els.composerFastModeButton.attrs['aria-pressed'],label:els.composerMobileFastModeLabel.textContent}})));
  }});
}});
"""
    path = tmp_path / "fast-mutation-race-test.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(path)], capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert [call["method"] for call in payload["calls"]] == ["POST", "GET", "GET"]
    assert "gpt-new" in payload["calls"][1]["url"]
    assert "gpt-new" in payload["calls"][2]["url"]
    assert payload["pressed"] == "true"
    assert payload["label"] == "On"


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_fast_mode_profile_switch_invalidates_pending_mutation(tmp_path, outcome):
    start = UI.index("// ── Fast mode")
    end = UI.index("// ── Session toolsets chip", start)
    source = UI[start:end]
    settle = (
        "pending[0].resolve({supported:true,enabled:true,service_tier:'priority'});"
        if outcome == "success"
        else "pending[0].reject(new Error('write failed'));"
    )
    script = f"""
const vm=require('vm');
const els={{}};
function makeEl(){{return {{style:{{}},disabled:false,hidden:false,textContent:'',attrs:{{}},classList:{{toggle(){{}},add(){{}},remove(){{}}}},setAttribute(k,v){{this.attrs[k]=String(v)}}}}}}
['composerFastModeButton','composerMobileFastModeAction','composerMobileFastModeLabel'].forEach(k=>els[k]=makeEl());
const pending=[]; const calls=[]; const toasts=[];
const tr={{composer_fast_mode_on:'Fast mode on',composer_fast_mode_off:'Fast mode off',composer_fast_on:'On',composer_fast_off:'Off',composer_fast_mode_enabled:'enabled',composer_fast_mode_disabled:'disabled',composer_fast_mode_failed:'failed'}};
const context={{console,URLSearchParams,$:(id)=>els[id],t:(key)=>tr[key]||key,S:{{activeProfile:'alpha',session:{{profile:'alpha',model:'gpt-old',model_provider:'openai'}}}},api:(url,opts)=>{{calls.push({{url,method:(opts&&opts.method)||'GET'}});return new Promise((resolve,reject)=>pending.push({{resolve,reject}}))}},showToast:(text)=>toasts.push(text)}};
vm.createContext(context); vm.runInContext({json.dumps(source)},context);
context._applyFastModeState({{supported:true,enabled:false,service_tier:''}},false,false);
context.toggleFastMode();
context.S.activeProfile='beta'; context.S.session=null;
context.refreshProfileTransitionFastMode('gpt-new','openai');
{settle}
setImmediate(()=>{{
  pending[1].resolve({{supported:true,enabled:false,service_tier:''}});
  setImmediate(()=>console.log(JSON.stringify({{calls,toasts,pressed:els.composerFastModeButton.attrs['aria-pressed'],label:els.composerMobileFastModeLabel.textContent}})));
}});
"""
    path = tmp_path / f"fast-profile-race-{outcome}.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(path)], capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert [call["method"] for call in payload["calls"]] == ["POST", "GET"]
    assert "gpt-new" in payload["calls"][1]["url"]
    assert payload["toasts"] == []
    assert payload["pressed"] == "false"
    assert payload["label"] == "Off"


def test_fast_mode_refresh_hooks_cover_profile_boot_and_model_switch():
    combined = "\n".join(
        (ROOT / "static" / name).read_text(encoding="utf-8")
        for name in ("boot.js", "sessions.js", "panels.js", "ui.js")
    )
    assert "syncFastMode()" in combined
    assert "refreshProfileTransitionFastMode(" in combined
    assert "clearProfileTransitionFastModeContext()" in combined
    assert combined.count("syncFastMode") >= 4
