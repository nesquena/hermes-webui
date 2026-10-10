"""App-icon tint settings and PWA integration."""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
BOOT = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.headers = {}
        self.wfile = self

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None


def _get(path):
    from api.routes import handle_get

    handler = _FakeHandler()
    handle_get(handler, urlparse(f"http://example.com{path}"))
    return handler


def test_icon_tint_is_a_validated_appearance_setting(tmp_path, monkeypatch):
    from api import config

    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    assert config._SETTINGS_DEFAULTS["icon_tint"] == "#08EBF1"
    assert "icon_tint" in config._SETTINGS_HEX_COLOR_KEYS

    saved = config.save_settings({"icon_tint": "#e5484d"})
    assert saved["icon_tint"] == "#E5484D"

    saved = config.save_settings({"icon_tint": "red; background: url(evil)"})
    assert saved["icon_tint"] == "#E5484D"


def test_tinted_favicon_route_uses_requested_color():
    handler = _get("/static/favicon.svg?tint=E5484D")

    assert handler.status == 200
    assert handler.header("Content-Type").startswith("image/svg+xml")
    assert handler.header("Cache-Control") == "no-store"
    assert "#E5484D" in bytes(handler.body).decode("utf-8")

    session_handler = _get("/session/static/favicon.svg?tint=7C3AED")
    assert session_handler.status == 200
    assert "#7C3AED" in bytes(session_handler.body).decode("utf-8")


def test_tinted_favicon_handles_source_gradient_color_without_collision():
    handler = _get("/static/favicon.svg?tint=3889FD")
    svg = bytes(handler.body).decode("utf-8")

    assert handler.status == 200
    assert svg.count('stop-color="#3889FD"') == 1
    assert svg.count('stop-color="#2760B1"') == 1


@pytest.mark.parametrize(
    "path",
    [
        "/manifest.json",
        "/manifest.webmanifest",
        "/session/manifest.json",
        "/session/manifest.webmanifest",
    ],
)
def test_manifest_tints_only_svg_and_preserves_raster_icons(path, monkeypatch):
    from api import routes

    monkeypatch.setattr(routes, "load_settings", lambda: {"icon_tint": "#E5484D"})
    handler = _get(path)
    manifest = json.loads(bytes(handler.body).decode("utf-8"))
    source = json.loads((ROOT / "static" / "manifest.json").read_text(encoding="utf-8"))

    assert handler.status == 200
    assert handler.header("Cache-Control") == "no-store"
    assert manifest["icons"][0] == {
        **source["icons"][0],
        "src": "static/favicon.svg?tint=E5484D",
    }
    assert manifest["icons"][1:] == source["icons"][1:]
    assert manifest["shortcuts"] == source["shortcuts"]


@pytest.mark.parametrize("path", ["/static/favicon.svg", "/session/static/favicon.svg"])
@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_unavailable_favicon_returns_static_404(path, kind, tmp_path, monkeypatch):
    from api import config

    static_root = tmp_path / "static"
    static_root.mkdir()
    if kind == "directory":
        (static_root / "favicon.svg").mkdir()
    monkeypatch.setattr(config, "get_static_root", lambda: static_root)

    handler = _get(path)
    assert handler.status == 404
    assert json.loads(handler.body) == {"error": "not found"}


class _IconLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "link":
            link = dict(attrs)
            if (
                "icon" in link.get("rel", "").split()
                or link.get("rel") == "apple-touch-icon"
            ):
                self.links.append(link)


def test_live_tint_changes_only_svg_favicon():
    if not shutil.which("node"):
        pytest.skip("Node.js is required for the live favicon DOM test")
    links = _IconLinks()
    links.feed(INDEX)
    assert any(link.get("href") == "static/favicon.ico" for link in links.links)
    assert any(link.get("href") == "static/favicon-32.png" for link in links.links)
    assert any(
        link.get("href") == "static/apple-touch-icon.png" for link in links.links
    )
    assert any(link.get("href") == "static/favicon.svg" for link in links.links)

    # Execute the actual production function against a minimal DOM, not a rewritten selector.
    function = BOOT[
        BOOT.index("function _normalizeIconTint(") : BOOT.index(
            "function _pickIconTint("
        )
    ]
    script = (
        """
const assert = require('node:assert/strict');
const links = JSON.parse(process.argv[1]);
for (const link of links) {
  link.original = {...link};
}
const $ = () => null;
const document = {
  querySelectorAll(selector) {
    if (!selector.startsWith('link[')) return [];
    return links.filter(link => selector.split(',').some(part => {
      const match = part.match(/^link\\[rel([~=])=\\"([^\\"]+)\\"\\](?:\\[type=\\"([^\\"]+)\\"\\])?$/);
      if (!match) throw new Error(`Unexpected selector: ${part}`);
      const relMatches = match[1] === '~' ? (link.rel || '').split(/\\s+/).includes(match[2]) : link.rel === match[2];
      return relMatches && (!match[3] || link.type === match[3]);
    }));
  }
};
"""
        + function
        + """
_applyIconTint('#E5484D');
for (const link of links) {
  if (link.original.href === 'static/favicon.svg') {
    assert.equal(link.href, 'static/favicon.svg?tint=E5484D');
    assert.equal(link.type, 'image/svg+xml');
  } else {
    assert.deepEqual(link, {...link.original, original: link.original});
  }
}
"""
    )
    subprocess.run(
        ["node", "-e", script, json.dumps(links.links)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_icon_tint_picker_updates_preview_presets_and_inline_marks():
    if not shutil.which("node"):
        pytest.skip("Node.js is required for the icon picker DOM test")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = fs.readFileSync('static/index.html', 'utf8');
const js = fs.readFileSync('static/boot.js', 'utf8');
const css = fs.readFileSync('static/style.css', 'utf8');
assert.ok(html.includes('id="iconTintLabel"'));
const field = html.slice(html.indexOf('id="iconTintLabel"'), html.indexOf('id="fontSizePickerGrid"'));
assert.match(field, /id="iconTintPreview"[^>]*src="static\/favicon.svg\?tint=08EBF1"/);
assert.match(field, /data-icon-tint-val="#08EBF1"/);
assert.match(field, /data-icon-tint-val="#E5484D"/);
assert.match(field, /data-icon-tint-val="custom"/);
assert.match(field, /id="settingsIconTint"[^>]*oninput="_pickIconTint\(this.value\)"/);
assert.doesNotMatch(field.match(/<input type="color"[^>]*>/)[0], / style=/);
assert.match(css, /\.icon-tint-pick-btn[^\n]*min-height:48px/);
assert.match(css, /\.icon-tint-custom[^\n]*min-height:48px/);
assert.match(field, /Safari and installed-app icons keep the default color/);
const start = js.indexOf('function _normalizeIconTint(');
const end = js.indexOf('function _applyFontSize(', start);
const links = [{href:'static/favicon.svg', type:'image/svg+xml', rel:'icon'}, {href:'static/apple-touch-icon.png', rel:'apple-touch-icon'}];
assert.match(html, /id="app-titlebar-mark"[\s\S]*?<stop offset="0" stop-color="#08EBF1"\/><stop offset="1" stop-color="#3889FD"\/>/);
assert.match(html, /class="empty-logo"[\s\S]*?class="hm-g0"[\s\S]*?class="hm-g1"/);
const stops = {'#app-titlebar-mark stop:first-child':[{style:{stopColor:''}}], '#app-titlebar-mark stop:last-child':[{style:{stopColor:''}}], '.empty-logo .hm-g0':[{style:{stopColor:''}}], '.empty-logo .hm-g1':[{style:{stopColor:''}}]};
const cells = ['#08EBF1','#E5484D','#7C3AED','#F59E0B','custom'].map(value=>({dataset:{iconTintVal:value}, tagName:value==='custom'?'LABEL':'BUTTON', classList:{active:false, toggle(_name,on){this.active=on;}},setAttribute(name,value){this[name]=value;}}));
const window = {};
const input = {value:'#08EBF1'};
const preview = {src:'static/favicon.svg?tint=08EBF1'};
const localStorage = {values:{},setItem(key,value){this.values[key]=value;}};
const document = {
  querySelectorAll(selector){
    if(selector==='link[rel~="icon"][type="image/svg+xml"]') return links.filter(link=>link.rel==='icon'&&link.type==='image/svg+xml');
    if(selector==='#iconTintPickerGrid .icon-tint-pick-btn') return cells;
    if(stops[selector]) return stops[selector];
    throw new Error(`Unexpected selector: ${selector}`);
  }
};
const $ = id => ({settingsIconTint:input,iconTintPreview:preview})[id];
let saves = 0;
function _scheduleAppearanceAutosave(){saves++;}
eval(js.slice(start,end));
_pickIconTint('#E5484D');
assert.equal(preview.src,'static/favicon.svg?tint=E5484D');
assert.equal(links[0].href,preview.src);
assert.equal(links[1].href,'static/apple-touch-icon.png');
assert.equal(input.value,'#E5484D');
assert.equal(localStorage.values['hermes-icon-tint'],'#E5484D');
assert.deepEqual(cells.map(cell=>cell.classList.active),[false,true,false,false,false]);
assert.equal(cells[1]['aria-pressed'],'true');
assert.equal(cells[0]['aria-pressed'],'false');
assert.equal(stops['#app-titlebar-mark stop:first-child'][0].style.stopColor,'#E5484D');
assert.equal(stops['.empty-logo .hm-g0'][0].style.stopColor,'#E5484D');
assert.equal(stops['#app-titlebar-mark stop:last-child'][0].style.stopColor,'#A03236');
assert.equal(stops['.empty-logo .hm-g1'][0].style.stopColor,'#A03236');
_pickIconTint('#123456');
assert.deepEqual(cells.map(cell=>cell.classList.active),[false,false,false,false,true]);
_pickIconTint('#08EBF1');
assert.deepEqual(cells.map(cell=>cell.classList.active),[true,false,false,false,false]);
for(const group of Object.values(stops)) assert.equal(group[0].style.stopColor,'');
assert.equal(saves,3);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_icon_tint_applies_on_boot_without_opening_settings():
    if not shutil.which("node"):
        pytest.skip("Node.js is required for the boot tint test")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const boot = fs.readFileSync('static/boot.js','utf8');
const start = boot.indexOf("const fontSize=(s.font_size||localStorage.getItem('hermes-font-size')||'default');");
const end = boot.indexOf("if(typeof setLocale==='function')",start);
assert.ok(start>=0 && end>start);
const apply = new Function('s','localStorage','_applyFontSize','_applyIconTintFromServer','iconTintEditAtRequest',boot.slice(start,end));
const storage = {getItem:()=>null,setItem:()=>{}};
const calls = [];
let font = null;
apply({icon_tint:'#E5484D',font_size:'large'},storage,value=>{font=value;},(tint,edit)=>{calls.push([tint,edit]);},0);
assert.deepEqual(calls,[['#E5484D',0]]);
assert.equal(font,'large');
apply({icon_tint:'#7C3AED'},storage,()=>{},(tint,edit)=>{calls.push([tint,edit]);},0);
assert.deepEqual(calls[1],['#7C3AED',0]);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_icon_tint_from_server_does_not_revert_a_newer_pick():
    if not shutil.which("node"):
        pytest.skip("Node.js is required for the tint GET race test")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const boot = fs.readFileSync('static/boot.js','utf8');
const input = {value:'#08EBF1'};
const preview = {src:''};
const link = {href:'static/favicon.svg'};
const storage = {};
const localStorage = {getItem:key=>storage[key]||null,setItem:(key,value)=>{storage[key]=value;}};
const document = {querySelectorAll:selector=>selector==='link[rel~="icon"][type="image/svg+xml"]'?[link]:[]};
const window = {};
const $ = id=>({settingsIconTint:input,iconTintPreview:preview})[id]||null;
eval(boot.slice(boot.indexOf('function _normalizeIconTint('),boot.indexOf('function _applyFontSize(')));
const atRequest = _iconTintEditCount();
_pickIconTint('#7C3AED'); // user picks while a settings GET is still in flight
assert.equal(_applyIconTintFromServer('#E5484D', atRequest), false);
assert.equal(input.value, '#7C3AED');
assert.equal(preview.src, 'static/favicon.svg?tint=7C3AED');
assert.equal(link.href, 'static/favicon.svg?tint=7C3AED');
assert.equal(localStorage.getItem('hermes-icon-tint'), '#7C3AED');
// A later GET with no intervening edit is still allowed to apply.
assert.equal(_applyIconTintFromServer('#E5484D', _iconTintEditCount()), true);
assert.equal(input.value, '#E5484D');
assert.equal(localStorage.getItem('hermes-icon-tint'), '#E5484D');
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_older_autosave_response_does_not_replace_newer_icon_pick():
    if not shutil.which("node"):
        pytest.skip("Node.js is required for the autosave ordering test")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const boot = fs.readFileSync('static/boot.js','utf8');
const panels = fs.readFileSync('static/panels.js','utf8');
const input = {value:'#08EBF1'};
const preview = {src:''};
const link = {href:'static/favicon.svg'};
const storage = {};
const localStorage = {getItem:key=>storage[key]||null,setItem:(key,value)=>{storage[key]=value;}};
const document = {querySelectorAll:selector=>selector==='link[rel~="icon"][type="image/svg+xml"]'?[link]:[]};
const $ = id=>({settingsIconTint:input,iconTintPreview:preview})[id]||null;
const window = {};
const _ensureComposerControlVisibilityState=()=>{};
const _renderComposerControlChips=()=>{};
const _renderComposerSituationalControlChips=()=>{};
let nextTimer = null;
const setTimeout = callback=>{nextTimer=callback;return 1;};
const clearTimeout = ()=>{nextTimer=null;};
let _settingsAppearanceAutosaveTimer = null;
let _settingsAppearanceAutosaveRetryPayload = null;
const _appearancePayloadFromUi = ()=>({icon_tint:input.value,font_size:'large'});
const _rememberAppearanceSaved = ()=>{};
const _setAppearanceAutosaveStatus = ()=>{};
const pending = [];
const api = (_path,options)=>new Promise(resolve=>pending.push({payload:JSON.parse(options.body),resolve}));
const _enqueueSettingsPost = options=>api('/api/settings',options);
eval(boot.slice(boot.indexOf('function _normalizeIconTint('),boot.indexOf('function _applyFontSize(')));
eval(panels.slice(panels.indexOf('function _scheduleAppearanceAutosave('),panels.indexOf('function _retryAppearanceAutosave(')));
(async()=>{
  _pickIconTint('#E5484D');
  nextTimer(); // Red POST is in flight.
  assert.equal(pending[0].payload.icon_tint,'#E5484D');
  _pickIconTint('#7C3AED'); // Purple is selected before its debounce fires.
  pending[0].resolve({icon_tint:'#E5484D'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(input.value,'#7C3AED');
  assert.equal(preview.src,'static/favicon.svg?tint=7C3AED');
  assert.equal(localStorage.getItem('hermes-icon-tint'),'#7C3AED');
  _scheduleAppearanceAutosave(); // Another Appearance change (font size).
  nextTimer();
  assert.equal(pending[1].payload.icon_tint,'#7C3AED');
  pending[1].resolve({icon_tint:'#7C3AED'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(input.value,'#7C3AED');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_icon_tint_picker_uses_skin_rings_and_aligns_wrapped_phone_rows():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    for skin in ("graphite", "codex", "terracotta", "github", "geist-contrast"):
        assert f':root[data-skin="{skin}"] ' in css
        assert (
            f':root[data-skin="{skin}"] #mainSettings .icon-tint-pick-btn.active' in css
            or f':root[data-skin="{skin}"] .icon-tint-pick-btn.active' in css
        )
    geist_rule = css[
        css.index(':root[data-skin="geist-contrast"] .side-menu-item.active,') :
    ]
    geist_rule = geist_rule[
        : geist_rule.index("{background:var(--surface-subtle)!important;")
    ]
    for picker in ("skin", "theme", "font-size", "icon-tint"):
        assert (
            f':root[data-skin="geist-contrast"] #mainSettings .{picker}-pick-btn.active'
            in geist_rule
        )
    assert (
        "#mainSettings .icon-tint-control{display:flex;align-items:flex-start;" in css
    )


def test_icon_tint_control_autosaves():
    assert 'id="settingsIconTint"' in INDEX
    assert "function _pickIconTint(" in BOOT
    assert "_applyIconTint" in BOOT
    assert (
        "_scheduleAppearanceAutosave()" in BOOT[BOOT.index("function _pickIconTint(") :]
    )
    assert "icon_tint:" in PANELS[PANELS.index("function _appearancePayloadFromUi(") :]
    assert "body.icon_tint=iconTint;" in PANELS.replace(" ", "")


def test_tinted_icon_routes_are_public_when_auth_is_enabled(monkeypatch):
    from api.auth import _invalidate_password_hash_cache, check_auth

    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")
    _invalidate_password_hash_cache()
    try:
        assert (
            check_auth(
                _FakeHandler(),
                SimpleNamespace(path="/static/favicon.svg", query="tint=E5484D"),
            )
            is True
        )
        assert (
            check_auth(
                _FakeHandler(),
                SimpleNamespace(
                    path="/session/static/favicon.svg", query="tint=E5484D"
                ),
            )
            is True
        )
    finally:
        monkeypatch.delenv("HERMES_WEBUI_PASSWORD", raising=False)
        _invalidate_password_hash_cache()
