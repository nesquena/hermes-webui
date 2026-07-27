"""Executable and static contracts for the custom wallpaper frontend."""

from collections import Counter
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


def test_wallpaper_i18n_keys_exist_in_every_locale() -> None:
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    locale_starts = list(re.finditer(
        r"^  ('[^']+'|[A-Za-z][A-Za-z0-9-]*): \{$", i18n, re.MULTILINE
    ))
    end = i18n.index("\n};", locale_starts[-1].start())
    blocks = {
        match.group(1).strip("'"): i18n[
            match.start(): locale_starts[index + 1].start()
            if index + 1 < len(locale_starts) else end
        ]
        for index, match in enumerate(locale_starts)
    }
    assert set(blocks) == {
        "en", "it", "ja", "ru", "es", "de", "zh", "zh-Hant", "pt",
        "ko", "fr", "cs", "tr", "pl", "vi",
    }
    expected_keys = list(WALLPAPER_I18N_KEYS)
    for locale, block in blocks.items():
        actual_keys = re.findall(
            r"^    (settings_wallpaper_[A-Za-z0-9_]+):", block, re.MULTILINE
        )
        assert Counter(actual_keys) == Counter(expected_keys), (
            f"{locale} wallpaper key ownership differs: {actual_keys}"
        )
        assert actual_keys == expected_keys, (
            f"{locale} wallpaper key order differs: {actual_keys}"
        )
    assert "_locale[key] ?? LOCALES.en[key]" in i18n


def test_wallpaper_simplified_chinese_uses_approved_wording() -> None:
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    zh = i18n[i18n.index("  zh: {"):i18n.index("  'zh-Hant': {")]
    for expected in (
        "settings_wallpaper_title: '壁纸'",
        "settings_wallpaper_description: '选择一张不超过 10 MB 的 JPEG、PNG 或 WebP 图片。更改仅在保存后生效。'",
        "settings_wallpaper_scope_chat: '仅聊天'",
        "settings_wallpaper_scope_app: '整个应用'",
        "settings_wallpaper_saved: '壁纸已保存。'",
        "settings_wallpaper_invalid_type: '请选择 JPEG、PNG 或 WebP 图片。'",
        "settings_wallpaper_network_failed: '无法连接到壁纸存储。请检查网络连接。'",
    ):
        assert expected in zh


def test_wallpaper_browser_waits_for_language_autosave_response() -> None:
    browser_test = (ROOT / "tests" / "test_wallpaper_browser.py").read_text(
        encoding="utf-8"
    )
    test_start = browser_test.index(
        "def test_wallpaper_settings_retranslate_owned_status_and_preserve_filename("
    )
    test_source = browser_test[test_start:]

    assert "with page.expect_response(" in test_source
    assert 'response.request.method == "POST"' in test_source
    assert 'response.url == f"{base_url}/api/settings"' in test_source
    assert "response.ok" in test_source
    assert "settings_response.finished()" in test_source
    assert "assert settings_response.status == 200" in test_source


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


def test_wallpaper_save_completion_preserves_newer_draft_and_url() -> None:
    source = (STATIC / "wallpaper.js").read_text(encoding="utf-8")
    script = f"""
const vm=require('vm');
const handlers={{}};
function element(id){{return {{id,value:'',files:[],disabled:false,hidden:true,src:'',textContent:'',style:{{}},classList:{{toggle(){{}}}},setAttribute(){{}},addEventListener(type,fn){{handlers[id+':'+type]=fn}}}}}}
const ids=['wallpaperFileInput','wallpaperDropZone','wallpaperOpacity','wallpaperOpacityValue','wallpaperScopeApp','wallpaperScopeChat','wallpaperPreview','wallpaperFileName','wallpaperSaveBtn','wallpaperClearBtn','wallpaperSettingsField','wallpaperStatus'];
const elements=Object.fromEntries(ids.map(id=>[id,element(id)]));
elements.wallpaperOpacity.value='80';elements.wallpaperScopeChat.value='chat';elements.wallpaperScopeChat.checked=true;elements.wallpaperScopeApp.value='app';
const root={{dataset:{{}},style:{{setProperty(k,v){{this[k]=v}},removeProperty(k){{delete this[k]}}}}}};
const revoked=[];let nextBlob=0;
class TestURL extends URL{{static createObjectURL(){{return 'blob:draft-'+(++nextBlob)}}static revokeObjectURL(url){{revoked.push(url)}}}}
let resolvePost;
const postResult=new Promise(resolve=>{{resolvePost=resolve}});
const empty={{has_wallpaper:false,opacity:.8,scope:'chat',mime_type:null,image_version:null}};
const saved={{has_wallpaper:true,opacity:.8,scope:'chat',mime_type:'image/png',image_version:'b'.repeat(64)}};
const context={{
  console,URL:TestURL,
  document:{{baseURI:'https://example.test/hermes/',documentElement:root,getElementById:id=>elements[id]||null,addEventListener(){{}},querySelectorAll:()=>[elements.wallpaperScopeChat,elements.wallpaperScopeApp],querySelector:()=>[elements.wallpaperScopeChat,elements.wallpaperScopeApp].find(r=>r.checked)}},
  location:{{href:'https://example.test/hermes/'}},localStorage:{{getItem(){{return null}},setItem(){{}},removeItem(){{}}}},
  Image:class{{set src(v){{this._src=v;if(this.onload)this.onload()}}}},
  api:async(path)=>path==='/api/wallpaper/info'?empty:postResult,
  showConfirmDialog:async()=>true,setTimeout,clearTimeout,window:null
}};
context.window=context;vm.createContext(context);vm.runInContext({json.dumps(source)},context);
const first={{name:'first.png',type:'image/png',size:10}};const second={{name:'second.png',type:'image/png',size:10}};
(async()=>{{
  context.beginWallpaperSettingsSession();await Promise.resolve();await Promise.resolve();
  elements.wallpaperFileInput.files=[first];handlers['wallpaperFileInput:change']();
  handlers['wallpaperSaveBtn:click']();await Promise.resolve();
  elements.wallpaperFileInput.files=[second];handlers['wallpaperFileInput:change']();
  elements.wallpaperScopeChat.checked=false;elements.wallpaperScopeApp.checked=true;handlers['wallpaperScopeApp:change']();
  elements.wallpaperOpacity.value='55';handlers['wallpaperOpacity:input']();
  resolvePost(saved);await postResult;await new Promise(resolve=>setTimeout(resolve,0));
  const during={{image:root.style['--wallpaper-image'],opacity:root.style['--wallpaper-opacity'],scope:root.dataset.wallpaperScope,saveDisabled:elements.wallpaperSaveBtn.disabled,revoked:[...revoked]}};
  context.endWallpaperSettingsSession();
  console.log(JSON.stringify({{during,after:{{image:root.style['--wallpaper-image'],opacity:root.style['--wallpaper-opacity'],scope:root.dataset.wallpaperScope,revoked:[...revoked]}}}}));
}})();
"""
    result = _node(script)
    assert result["during"] == {
        "image": 'url("blob:draft-2")',
        "opacity": "0.55",
        "scope": "app",
        "saveDisabled": False,
        "revoked": ["blob:draft-1"],
    }
    assert result["after"] == {
        "image": 'url("https://example.test/hermes/api/wallpaper/image?v=' + "b" * 64 + '")',
        "opacity": "0.8",
        "scope": "chat",
        "revoked": ["blob:draft-1", "blob:draft-2"],
    }


def test_wallpaper_mutation_isolates_sessions_and_restores_after_reconcile() -> None:
    source = (STATIC / "wallpaper.js").read_text(encoding="utf-8")
    script = f"""
const vm=require('vm');
const source={json.dumps(source)};
const empty={{has_wallpaper:false,opacity:.8,scope:'chat',mime_type:null,image_version:null}};
const saved={{has_wallpaper:true,opacity:.8,scope:'chat',mime_type:'image/png',image_version:'c'.repeat(64)}};
function harness(mode){{
  const handlers={{}};
  function element(id){{return {{id,value:'',files:[],disabled:false,hidden:true,src:'',textContent:'',style:{{}},classList:{{toggle(){{}}}},setAttribute(){{}},addEventListener(type,fn){{handlers[id+':'+type]=fn}}}}}}
  const ids=['wallpaperFileInput','wallpaperDropZone','wallpaperOpacity','wallpaperOpacityValue','wallpaperScopeApp','wallpaperScopeChat','wallpaperPreview','wallpaperFileName','wallpaperSaveBtn','wallpaperClearBtn','wallpaperSettingsField','wallpaperStatus'];
  const elements=Object.fromEntries(ids.map(id=>[id,element(id)]));
  elements.wallpaperOpacity.value='80';elements.wallpaperScopeChat.value='chat';elements.wallpaperScopeChat.checked=true;elements.wallpaperScopeApp.value='app';
  const root={{dataset:{{}},style:{{setProperty(k,v){{this[k]=v}},removeProperty(k){{delete this[k]}}}}}};
  const revoked=[];let nextBlob=0,infoCalls=0,resolveMutation,rejectMutation;
  class TestURL extends URL{{static createObjectURL(){{return 'blob:'+mode+'-'+(++nextBlob)}}static revokeObjectURL(url){{revoked.push(url)}}}}
  const mutation=new Promise((resolve,reject)=>{{resolveMutation=resolve;rejectMutation=reject}});
  const context={{
    console,URL:TestURL,
    document:{{baseURI:'https://example.test/hermes/',documentElement:root,getElementById:id=>elements[id]||null,addEventListener(){{}},querySelectorAll:()=>[elements.wallpaperScopeChat,elements.wallpaperScopeApp],querySelector:()=>[elements.wallpaperScopeChat,elements.wallpaperScopeApp].find(r=>r.checked)}},
    location:{{href:'https://example.test/hermes/'}},localStorage:{{getItem(){{return null}},setItem(){{}},removeItem(){{}}}},
    Image:class{{set src(v){{this._src=v;if(this.onload)this.onload()}}}},
    api:async(path)=>{{
      if(path==='/api/wallpaper/info'){{
        infoCalls++;
        if(infoCalls===1||mode==='session'&&infoCalls===2)return empty;
        if(mode==='reconcile-failure')throw new Error('Failed to fetch');
        return saved;
      }}
      return mutation;
    }},
    showConfirmDialog:async()=>true,setTimeout,clearTimeout,window:null
  }};
  context.window=context;vm.createContext(context);vm.runInContext(source,context);
  const install=name=>{{elements.wallpaperFileInput.files=[{{name,type:'image/png',size:10}}];handlers['wallpaperFileInput:change']()}};
  const edit=()=>{{elements.wallpaperScopeChat.checked=false;elements.wallpaperScopeApp.checked=true;handlers['wallpaperScopeApp:change']();elements.wallpaperOpacity.value='55';handlers['wallpaperOpacity:input']()}};
  const snapshot=()=>({{image:root.style['--wallpaper-image'],opacity:root.style['--wallpaper-opacity'],scope:root.dataset.wallpaperScope,saveDisabled:elements.wallpaperSaveBtn.disabled,revoked:[...revoked]}});
  return {{context,handlers,elements,install,edit,snapshot,resolveMutation,rejectMutation}};
}}
async function settle(){{await new Promise(resolve=>setTimeout(resolve,0))}}
async function runSession(){{
  const h=harness('session');h.context.beginWallpaperSettingsSession();await settle();
  h.install('old.png');h.handlers['wallpaperSaveBtn:click']();await Promise.resolve();
  h.context.endWallpaperSettingsSession();h.context.beginWallpaperSettingsSession();await settle();
  h.install('new.png');h.edit();h.resolveMutation(saved);await settle();
  const during=h.snapshot();h.context.endWallpaperSettingsSession();return {{during,after:h.snapshot()}};
}}
async function runFailure(mode){{
  const h=harness(mode);h.context.beginWallpaperSettingsSession();await settle();
  h.install('old.png');h.handlers['wallpaperSaveBtn:click']();await Promise.resolve();h.install('new.png');h.edit();
  const error=new Error('server');error.status=500;h.rejectMutation(error);await settle();await settle();
  return h.snapshot();
}}
(async()=>console.log(JSON.stringify({{session:await runSession(),success:await runFailure('reconcile-success'),failure:await runFailure('reconcile-failure')}})))();
"""
    result = _node(script)
    for snapshot, prefix in (
        (result["session"]["during"], "session"),
        (result["success"], "reconcile-success"),
        (result["failure"], "reconcile-failure"),
    ):
        assert snapshot == {
            "image": f'url("blob:{prefix}-2")',
            "opacity": "0.55",
            "scope": "app",
            "saveDisabled": False,
            "revoked": [f"blob:{prefix}-1"],
        }
    assert result["session"]["after"] == {
        "image": 'url("https://example.test/hermes/api/wallpaper/image?v=' + "c" * 64 + '")',
        "opacity": "0.8",
        "scope": "chat",
        "saveDisabled": False,
        "revoked": ["blob:session-1", "blob:session-2"],
    }


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


def test_wallpaper_geist_scopes_force_empty_state_transparent() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    geist = ':root[data-skin="geist-contrast"][data-wallpaper="active"]'
    for scope in ("chat", "app"):
        selector = geist + f'[data-wallpaper-scope="{scope}"] .empty-state'
        selector_start = css.index(selector)
        declaration_start = css.index("{", selector_start)
        assert css[declaration_start:css.index("}", declaration_start) + 1] == (
            "{background:transparent!important;}"
        )


def test_wallpaper_forced_skins_explicitly_override_shell_backgrounds() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    selector = (
        ':root:is([data-skin="graphite"],[data-skin="github"],'
        '[data-skin="codex"],[data-skin="terracotta"],'
        '[data-skin="geist-contrast"])[data-wallpaper="active"]'
        '[data-wallpaper-scope="app"]'
    )
    block = css[css.index(selector):]
    transparency_selector = (
        selector
        + " :is(.app-titlebar,.rail,.sidebar,.rightpanel,.main,.topbar,"
        ".composer-wrap)"
    )
    assert transparency_selector + "{background:transparent!important;}" in css
    assert selector + " .sidebar .panel-view" in css
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
