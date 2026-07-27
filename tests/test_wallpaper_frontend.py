"""Executable and static contracts for the custom wallpaper frontend."""

import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
WALLPAPER_I18N_KEYS = (
    "settings_wallpaper_title",
    "settings_wallpaper_description",
    "settings_wallpaper_choose",
    "settings_wallpaper_drop",
    "settings_wallpaper_preview_alt",
    "settings_wallpaper_saved_file",
    "settings_wallpaper_opacity",
    "settings_wallpaper_scope",
    "settings_wallpaper_scope_chat",
    "settings_wallpaper_scope_app",
    "settings_wallpaper_save",
    "settings_wallpaper_clear",
    "settings_wallpaper_saving",
    "settings_wallpaper_saved",
    "settings_wallpaper_cleared",
    "settings_wallpaper_confirm_clear",
    "settings_wallpaper_invalid_type",
    "settings_wallpaper_invalid_size",
    "settings_wallpaper_unavailable",
    "settings_wallpaper_reconciliation",
    "settings_wallpaper_failed",
    "settings_wallpaper_invalid_response",
    "settings_wallpaper_image_unavailable",
    "settings_wallpaper_invalid_upload",
    "settings_wallpaper_invalid_metadata",
    "settings_wallpaper_not_found",
    "settings_wallpaper_too_large",
    "settings_wallpaper_storage_failed",
    "settings_wallpaper_timeout",
    "settings_wallpaper_network_failed",
)


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
    assert 'data-i18n-alt="settings_wallpaper_preview_alt"' in html
    assert '<script src="static/wallpaper.js?v=__WEBUI_VERSION__" defer></script>' in html
    assert "hermes-wallpaper-meta" in html
    bridge = html[html.index("hermes-wallpaper-meta") - 400:html.index("hermes-wallpaper-meta") + 900]
    assert "v.has_wallpaper!==true" in bridge
    assert "typeof v.mime_type!=='string'" in bridge
    assert "Object.keys(v).sort().join(',')" in bridge


def test_wallpaper_i18n_and_service_worker_contracts() -> None:
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    for key in WALLPAPER_I18N_KEYS:
        assert f"{key}:" in i18n
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "'./static/wallpaper.js' + VQ" in sw
    assert "url.pathname.includes('/api/')" in sw


def test_wallpaper_i18n_keys_are_english_fallback_owned() -> None:
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    locale_starts = list(
        re.finditer(
            r"^  ('[^']+'|[A-Za-z][A-Za-z0-9-]*): \{$", i18n, re.MULTILINE
        )
    )
    end = i18n.index("\n};", locale_starts[-1].start())
    blocks = {
        match.group(1).strip("'"): i18n[
            match.start() : locale_starts[index + 1].start()
            if index + 1 < len(locale_starts)
            else end
        ]
        for index, match in enumerate(locale_starts)
    }

    assert len(blocks) == 15
    for key in WALLPAPER_I18N_KEYS:
        assert f"{key}:" in blocks["en"]
    assert any(
        f"{key}:" not in block
        for locale, block in blocks.items()
        if locale != "en"
        for key in WALLPAPER_I18N_KEYS
    )
    assert "_locale[key] ?? LOCALES.en[key]" in i18n


def test_wallpaper_locale_switch_updates_alt_and_owned_dynamic_text() -> None:
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    wallpaper = (STATIC / "wallpaper.js").read_text(encoding="utf-8")

    assert "document.querySelectorAll('[data-i18n-alt]')" in i18n
    assert "el.setAttribute('alt', val)" in i18n
    assert "document.dispatchEvent(new CustomEvent('hermes:locale-changed'" in i18n
    assert "document.addEventListener('hermes:locale-changed'" in wallpaper
    assert "statusKey" in wallpaper
    assert "setStatusKey" in wallpaper
    assert "refreshLocalizedWallpaperText" in wallpaper


def test_wallpaper_errors_are_localized_instead_of_rendering_raw_messages() -> None:
    source = (STATIC / "wallpaper.js").read_text(encoding="utf-8")

    assert "function wallpaperErrorKey" in source
    assert "error&&error.message)||text(" not in source
    for key in (
        "settings_wallpaper_invalid_response",
        "settings_wallpaper_image_unavailable",
        "settings_wallpaper_invalid_upload",
        "settings_wallpaper_invalid_metadata",
        "settings_wallpaper_not_found",
        "settings_wallpaper_too_large",
        "settings_wallpaper_storage_failed",
        "settings_wallpaper_timeout",
        "settings_wallpaper_network_failed",
    ):
        assert key in source


def test_wallpaper_clear_uses_shared_app_confirmation_dialog() -> None:
    source = (STATIC / "wallpaper.js").read_text(encoding="utf-8")
    assert "global.confirm(" not in source
    assert "await global.showConfirmDialog({" in source
    assert "danger:true" in source
    assert "focusCancel:true" in source


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


def test_wallpaper_app_scope_exposes_single_layer_through_shell() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    selector = ':root[data-wallpaper="active"][data-wallpaper-scope="app"]'
    wallpaper_block = css[css.index("/* Active wallpaper rendering"):]
    assert "--wallpaper-chrome:" not in wallpaper_block
    assert "--wallpaper-main:" not in wallpaper_block
    for surface in (
        ".app-titlebar", ".rail", ".sidebar", ".rightpanel", ".main",
        ".topbar", ".composer-wrap", ".sidebar .panel-view",
        ".rightpanel .panel-header", ".workspace-panel-tabs", ".main-view",
        "#mainChat", ".messages-shell", ".messages", ".empty-state",
    ):
        assert selector + " " + surface in css
    assert "opacity:" not in wallpaper_block.replace(
        "opacity:var(--wallpaper-opacity)", ""
    )
    assert "overflow-x:" not in wallpaper_block


def test_wallpaper_chat_scope_does_not_override_titlebar() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    chat_start = css.index(
        ':root[data-wallpaper="active"][data-wallpaper-scope="chat"]'
    )
    app_start = css.index(
        ':root[data-wallpaper="active"][data-wallpaper-scope="app"]'
    )
    assert ".app-titlebar" not in css[chat_start:app_start]


def test_wallpaper_forced_skins_explicitly_override_shell_backgrounds() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    selector = (
        ':root:is([data-skin="graphite"],[data-skin="github"],'
        '[data-skin="codex"],[data-skin="terracotta"],'
        '[data-skin="geist-contrast"])[data-wallpaper="active"]'
        '[data-wallpaper-scope="app"]'
    )
    block = css[css.index(selector):]
    for surface in (
        ".app-titlebar", ".rail", ".sidebar", ".rightpanel", ".main",
        ".topbar", ".composer-wrap",
    ):
        assert surface in block
    assert "{background:transparent!important;}" in block
    assert selector + " .composer-box" in css
    assert "{background:var(--wallpaper-composer)!important;}" in block


def test_wallpaper_controller_previews_draft_and_restores_saved_on_exit() -> None:
    controller = (STATIC / "wallpaper.js").read_text(encoding="utf-8")
    assert "function renderSource(source,opacity,scope)" in controller
    assert "function renderDraft()" in controller
    assert "draftUrl||(saved.has_wallpaper?imageUrl(saved.image_version):'')" in controller
    assert "renderSource(source,draftOpacity,draftScope)" in controller
    assert "renderDraft();syncControls()" in controller
    assert (
        "function discardDraft(){_releaseWallpaperDraftUrl();draftFile=null;"
        "draftOpacity=saved.opacity;draftScope=saved.scope;draftRevision++;"
        "render(saved);syncControls()}"
    ) in controller
    reconcile = controller[
        controller.index("function beginWallpaperSettingsSession"):
        controller.index("function endWallpaperSettingsSession")
    ]
    assert "renderDraft()" in reconcile
    end = controller[
        controller.index("function endWallpaperSettingsSession"):
        controller.index("async function _requestForTest")
    ]
    assert "render(saved)" in end
    assert end.index("render(saved)") < end.index("_releaseWallpaperDraftUrl()")
    assert "enqueueMutation(" not in end


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
    selector = (
        ':root:is([data-skin="graphite"],[data-skin="github"],'
        '[data-skin="codex"],[data-skin="terracotta"],'
        '[data-skin="geist-contrast"])[data-wallpaper="active"]'
        '[data-wallpaper-scope="app"]'
    )
    assert selector in css


def test_wallpaper_controller_uses_explicit_lifecycle_not_appearance_autosave() -> None:
    controller = (STATIC / "wallpaper.js").read_text(encoding="utf-8")
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    assert "function beginWallpaperSettingsSession" in controller
    assert "function endWallpaperSettingsSession" in controller
    assert "_releaseWallpaperDraftUrl" in controller
    assert "beginWallpaperSettingsSession()" in panels
    assert "endWallpaperSettingsSession()" in panels
    assert "wallpaper" not in panels[panels.index("function _appearancePayloadFromUi"):panels.index("function _scheduleAppearanceAutosave")]
