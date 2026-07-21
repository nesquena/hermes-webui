"""Executable and static contracts for the custom wallpaper frontend."""

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def _node(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_wallpaper_dom_controls_and_boot_bridge_exist() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<div id="wallpaperLayer" aria-hidden="true"></div>' in html
    assert html.index('id="wallpaperLayer"') < html.index('class="app-titlebar"')
    for element_id in (
        "wallpaperSettingsField", "wallpaperDescription", "wallpaperFileInput",
        "wallpaperDropZone", "wallpaperPreview", "wallpaperFileName",
        "wallpaperStatus", "wallpaperOpacity", "wallpaperOpacityValue",
        "wallpaperScope", "wallpaperScopeChat", "wallpaperScopeApp",
        "wallpaperSaveBtn", "wallpaperClearBtn",
    ):
        assert f'id="{element_id}"' in html
    assert 'accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp"' in html
    assert 'aria-describedby="wallpaperDescription wallpaperStatus"' in html
    assert 'role="status" aria-live="polite" aria-atomic="true"' in html
    assert '<script src="static/wallpaper.js?v=__WEBUI_VERSION__" defer></script>' in html
    assert "hermes-wallpaper-meta" in html
    bridge = html[html.index("hermes-wallpaper-meta") - 400:html.index("hermes-wallpaper-meta") + 900]
    assert "v.has_wallpaper!==true" in bridge
    assert "typeof v.mime_type!=='string'" in bridge
    assert "Object.keys(v).sort().join(',')" in bridge


def test_wallpaper_i18n_and_service_worker_contracts() -> None:
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    for key in (
        "settings_wallpaper_title", "settings_wallpaper_description",
        "settings_wallpaper_choose", "settings_wallpaper_opacity",
        "settings_wallpaper_scope", "settings_wallpaper_scope_chat",
        "settings_wallpaper_scope_app", "settings_wallpaper_save",
        "settings_wallpaper_clear", "settings_wallpaper_saving",
        "settings_wallpaper_saved", "settings_wallpaper_cleared",
        "settings_wallpaper_confirm_clear", "settings_wallpaper_invalid_type",
        "settings_wallpaper_invalid_size", "settings_wallpaper_failed",
    ):
        assert f"{key}:" in i18n
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "'./static/wallpaper.js' + VQ" in sw
    assert "url.pathname.includes('/api/')" in sw


def test_wallpaper_controller_cache_subpath_and_request_contract() -> None:
    source = (STATIC / "wallpaper.js").read_text(encoding="utf-8")
    script = f"""
const vm=require('vm');
const elements={{}};
const root={{dataset:{{}},style:{{setProperty(k,v){{this[k]=v}},removeProperty(k){{delete this[k]}}}}}};
const storage=new Map();
const calls=[];
const context={{
  console, URL: URL,
  document:{{baseURI:'https://example.test/hermes/',documentElement:root,getElementById:(id)=>elements[id]||null,addEventListener:()=>{{}}}},
  location:{{href:'https://example.test/hermes/',origin:'https://example.test'}},
  localStorage:{{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}},
  Image:class{{set src(v){{this._src=v; if(this.onload)this.onload()}}}},
  api:async(path,opts)=>{{calls.push([path,opts]);return {{has_wallpaper:false,opacity:.8,scope:'chat',mime_type:null,image_version:null}}}},
  setTimeout,clearTimeout,confirm:()=>true,
  window:null
}};
context.window=context;
vm.createContext(context);vm.runInContext({json.dumps(source)},context);
const W=context.HermesWallpaper;
const good={{has_wallpaper:true,opacity:.5,scope:'app',mime_type:'image/png',image_version:'a'.repeat(64)}};
const bad=[{{...good,opacity:true}},{{...good,opacity:2}},{{...good,scope:'desktop'}},{{...good,image_version:'A'.repeat(64)}},{{has_wallpaper:false,opacity:.8,scope:'chat',mime_type:'image/png',image_version:null}}];
(async()=>{{
  const normalized=W.normalizeInfo(good);
  const url=W.imageUrl(normalized.image_version);
  const badRejected=bad.every(v=>{{try{{W.normalizeInfo(v);return false}}catch(e){{return true}}}});
  W._setSavedForTest(good);
  await W._requestForTest('patch',null,.4,'chat');
  await W._requestForTest('delete',null,.8,'chat');
  console.log(JSON.stringify({{url,badRejected,calls:calls.map(([p,o])=>[p,o.method,o.retries,o.headers||null,o.body||null])}}));
}})();
"""
    result = _node(script)
    assert result["url"] == "https://example.test/hermes/api/wallpaper/image?v=" + "a" * 64
    assert result["badRejected"] is True
    assert result["calls"][0] == [
        "/api/wallpaper", "PATCH", 0, None,
        '{"opacity":0.4,"scope":"chat"}',
    ]
    assert result["calls"][1] == ["/api/wallpaper", "DELETE", 0, None, None]


def test_wallpaper_layer_stacking_chat_scope_and_inactive_guards() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert '#wallpaperLayer{position:fixed;inset:0' in css
    assert 'background-image:var(--wallpaper-image)' in css
    assert 'background-position:center;background-size:cover;background-repeat:no-repeat' in css
    assert 'opacity:var(--wallpaper-opacity);pointer-events:none;z-index:0' in css
    assert ':root[data-wallpaper="active"] #wallpaperLayer' in css
    assert ':root[data-wallpaper="active"] .layout{position:relative;z-index:1;}' in css
    chat = ':root[data-wallpaper="active"][data-wallpaper-scope="chat"]'
    assert chat + ' .main{background:transparent;}' in css
    assert chat + ' #mainChat' in css
    assert 'color-mix(in srgb,var(--bg) 82%,transparent)' in css
    assert 'linear-gradient(to bottom,transparent,color-mix(in srgb,var(--bg) 82%,transparent))' in css
    assert 'color-mix(in srgb,var(--input-bg) 90%,transparent)' in css
    assert '.app-titlebar{display:flex;align-items:center;justify-content:center;height:38px;' in css


def test_wallpaper_app_scope_covers_shell_without_parent_opacity() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    selector = ':root[data-wallpaper="active"][data-wallpaper-scope="app"]'
    for surface in (
        '.app-titlebar', '.rail', '.sidebar', '.sidebar .panel-view', '.main',
        '.main-view', '.topbar', '#mainChat', '.messages-shell', '.messages',
        '.empty-state', '.composer-wrap', '.composer-box', '.rightpanel',
        '.rightpanel .panel-header', '.workspace-panel-tabs',
    ):
        assert selector + ' ' + surface in css
    wallpaper_block = css[css.index('/* Active wallpaper rendering'):]
    assert 'opacity:' not in wallpaper_block.replace('opacity:var(--wallpaper-opacity)', '')
    assert 'overflow-x:' not in wallpaper_block


def test_wallpaper_skin_inventory_has_active_override_coverage() -> None:
    boot = (STATIC / "boot.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    names = [
        'default','ares','mono','graphite','github','codex','terracotta','slate',
        'poseidon','sisyphus','charizard','sienna','catppuccin','hepburn','nous',
        'neon','neon-soft','neon-paint','geist-contrast','zeus','verdigris',
    ]
    for name in names:
        assert name in boot.lower()
    forced = ['graphite','github','codex','terracotta','geist-contrast']
    for name in forced:
        assert f'[data-skin="{name}"][data-wallpaper="active"][data-wallpaper-scope="app"]' in css


def test_wallpaper_controller_uses_explicit_lifecycle_not_appearance_autosave() -> None:
    controller = (STATIC / "wallpaper.js").read_text(encoding="utf-8")
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    assert "function beginWallpaperSettingsSession" in controller
    assert "function endWallpaperSettingsSession" in controller
    assert "_releaseWallpaperDraftUrl" in controller
    assert "beginWallpaperSettingsSession()" in panels
    assert "endWallpaperSettingsSession()" in panels
    assert "wallpaper" not in panels[panels.index("function _appearancePayloadFromUi"):panels.index("function _scheduleAppearanceAutosave")]
