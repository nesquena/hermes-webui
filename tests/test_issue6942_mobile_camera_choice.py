from pathlib import Path
import re
import subprocess


REPO = Path(__file__).resolve().parent.parent


def read(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


def test_mobile_camera_choice_is_new_and_routes_through_existing_intake():
    html = read("static/index.html")
    boot = read("static/boot.js")
    assert 'id="attachChoicePopup"' in html
    assert 'id="cameraFileInput"' in html
    assert 'id="cameraFileInput" tabindex="-1"' in html
    assert 'id="cameraFileInput" tabindex="-1" aria-hidden="true"' in html
    assert 'accept="image/*" capture="environment"' in html
    assert "_shouldOfferCameraChoice" in boot
    assert "fine:matchMedia('(any-pointer:fine)')" in boot
    assert "addFiles(normalized)" in boot
    assert "cameraFileInput').onchange" in boot


def test_camera_reselection_is_collision_safe_without_changing_global_dedupe():
    boot = read("static/boot.js")
    ui = read("static/ui.js")
    camera = boot[boot.index("function _cameraFileWithUniqueName"):boot.index("$('btnAttach').onclick")]

    assert "new File([file],name" in camera
    assert "(${index++})" in camera
    assert "S.pendingFiles" in camera
    # Global dedupe is master's #8204 identity check (same original name, size and mtime), which this PR must not
    # change; the old name-only `.find(p=>p.name===f.name)` it used to pin was replaced by #8204.
    assert "S.pendingFiles.some(p=>_pendingFileOrigName(p)===f.name&&p.size===f.size&&p.lastModified===f.lastModified)" in ui
    assert "pendingFiles.push" not in camera
    assert "fetch(" not in camera


def test_choice_accessibility_layout_and_locale_parity():
    html = read("static/index.html")
    css = read("static/style.css")
    i18n = read("static/i18n.js")

    assert 'role="menu"' in html
    assert 'role="menuitem"' in html
    assert html.count('role="menuitem" tabindex="-1"') == 2
    assert 'aria-expanded="false"' in html
    assert "aria-hidden" in read("static/boot.js")
    assert "min-height:44px" in css
    assert ".attach-choice-popup[hidden]{display:none;}" in css
    assert ".attach-choice-popup button:focus-visible{background:var(--hover-bg);outline:2px solid var(--accent);outline-offset:1px;}" in css
    assert ".composer-left{display:flex" in css and "overflow-y:hidden" in css
    left_start = html.index('class="composer-left"')
    assert 'id="attachChoicePopup"' not in html[left_start:html.index('</div>', left_start)]
    assert "composer_attach_options" in i18n

    locales = re.findall(r"^  (?:[\"']?)([A-Za-z][A-Za-z0-9-]*)(?:[\"']?): \{", i18n, re.MULTILINE)
    assert len(locales) == 15
    for key in ("composer_attach_options", "composer_attach_photo", "composer_attach_files"):
        assert len(re.findall(rf"^    {key}:", i18n, re.MULTILINE)) == len(locales)


def test_popup_file_routes_and_media_query_semantics_behave():
    boot = read("static/boot.js")
    block = boot[boot.index("let _attachChoiceOpen"):boot.index("// ── Voice input")]
    touch = boot[boot.index("function _hasFinePointerCoexisting"):boot.index("function _isNumpadEnter")]
    block = touch + "\n" + block
    script = f"""
const assert = require('node:assert/strict');
const state = {{ width: 400, coarse: true, fine: false }};
class Element {{
  constructor(id) {{ this.id=id; this.attrs={{}}; this.listeners={{}}; this.hidden=true; this.value=''; this.dataset={{}}; this.focused=false; this.clicks=0; }}
  setAttribute(k,v) {{ this.attrs[k]=String(v); }}
  removeAttribute(k) {{ delete this.attrs[k]; }}
  getAttribute(k) {{ return this.attrs[k] ?? null; }}
  addEventListener(k,fn) {{ this.listeners[k]=fn; }}
  removeEventListener(k,fn) {{ if(this.listeners[k]===fn) delete this.listeners[k]; }}
  focus() {{ this.focused=true; document.activeElement=this; }}
  click() {{ this.clicks++; }}
  querySelector() {{ return choice; }}
  querySelectorAll(selector) {{ return selector.includes('[data-attach-choice]') ? [choice,files].filter(item=>item.dataset.attachChoice&&!item.getAttribute('disabled')) : []; }}
  contains(target) {{ return target===choice||target===files; }}
  closest(selector) {{
    if (this.id === 'choice' && selector.includes('[data-attach-choice]')) return this;
    if (this.id === 'popup' && selector.includes('#attachChoicePopup')) return this;
    if (this.id === 'attach' && selector.includes('#btnAttach')) return this;
    return null;
  }}
}}
const attach = new Element('attach');
const popup = new Element('popup');
const choice = new Element('choice'); choice.dataset.attachChoice='camera';
const files = new Element('files'); files.dataset.attachChoice='files';
const camera = new Element('camera');
const elements = {{ btnAttach:attach, attachChoicePopup:popup, fileInput:files, cameraFileInput:camera }};
const document = {{ listeners:{{}}, activeElement:null, addEventListener(k,fn){{this.listeners[k]=fn;}} }};
const mediaQueries = {{}};
const window = {{ listeners:{{}}, addEventListener(k,fn){{this.listeners[k]=fn;}}, matchMedia(q){{
  if(mediaQueries[q]) return mediaQueries[q];
  const query = {{
    get matches() {{ return q.includes('max-width: 640px') ? state.width <= 640 : q.includes('pointer:coarse') ? state.coarse : q.includes('any-pointer:fine') ? state.fine : false; }},
    listeners:[],
    addEventListener(k,fn) {{ if(k==='change') this.listeners.push(fn); }},
  }};
  mediaQueries[q] = query;
  return query;
}} }};
globalThis.document=document; globalThis.window=window;
globalThis.matchMedia=(q)=>window.matchMedia(q);
globalThis.$=(id)=>elements[id];
Object.defineProperty(globalThis,'navigator',{{value:{{userAgent:''}},configurable:true}});
globalThis.S={{ pendingFiles:[] }};
globalThis.addFiles=(items)=>{{ S.pendingFiles.push(...items); globalThis.admitted=items; }};
globalThis.console={{ log(){{}} }};
const frames=[]; globalThis.setTimeout=fn=>frames.push(fn);
const notifyCapabilityChange=()=>Object.values(mediaQueries).forEach(query=>query.listeners.forEach(fn=>fn()));
eval({block!r});
assert.equal(attach.getAttribute('aria-haspopup'),'menu');
attach.onclick({{preventDefault(){{}}}});
assert.equal(popup.hidden,false); assert.equal(attach.getAttribute('aria-expanded'),'true');
attach.listeners.pointerdown();
popup.listeners.focusout({{currentTarget:popup,relatedTarget:attach}});
assert.equal(popup.hidden,false);
attach.onclick({{preventDefault(){{}}}});
assert.equal(popup.hidden,true);
attach.onclick({{preventDefault(){{}}}});
attach.listeners.pointerdown();
popup.listeners.focusout({{currentTarget:popup,relatedTarget:null}});
assert.equal(popup.hidden,false);
attach.onclick({{preventDefault(){{}}}});
assert.equal(popup.hidden,true);
attach.onclick({{preventDefault(){{}}}});
assert.equal(popup.hidden,false);
_handleAttachChoiceKeydown({{key:'ArrowDown',preventDefault(){{}}}});
assert.equal(document.activeElement,files);
_handleAttachChoiceKeydown({{key:'ArrowUp',preventDefault(){{}}}});
assert.equal(document.activeElement,choice);
_handleAttachChoiceKeydown({{key:'ArrowDown',preventDefault(){{}}}});
assert.equal(document.activeElement,files);
_handleAttachChoiceKeydown({{key:'Home',preventDefault(){{}}}});
assert.equal(document.activeElement,choice);
_handleAttachChoiceKeydown({{key:'End',preventDefault(){{}}}});
assert.equal(document.activeElement,files);
state.width=1280; state.coarse=false; state.fine=true; notifyCapabilityChange();
assert.equal(popup.hidden,true); assert.equal(attach.getAttribute('aria-expanded'),null);
assert.equal(attach.getAttribute('aria-controls'),null);
assert.equal(attach.focused,true);
state.width=400; state.coarse=true; state.fine=false; notifyCapabilityChange();
attach.onclick({{preventDefault(){{}}}});
assert.equal(popup.hidden,false); assert.equal(attach.getAttribute('aria-expanded'),'true');
state.fine=true; mediaQueries['(any-pointer:fine)'].listeners.forEach(fn=>fn());
assert.equal(popup.hidden,true); assert.equal(attach.getAttribute('aria-expanded'),null);
state.fine=false; notifyCapabilityChange();
attach.focused=false;
attach.onclick({{preventDefault(){{}}}});
popup.listeners.focusout({{currentTarget:popup,relatedTarget:choice}}); assert.equal(popup.hidden,false);
popup.listeners.focusout({{currentTarget:popup,relatedTarget:files}}); assert.equal(popup.hidden,false);
popup.listeners.focusout({{currentTarget:popup,relatedTarget:attach}}); assert.equal(popup.hidden,true);
attach.onclick({{preventDefault(){{}}}});
popup.listeners.focusout({{currentTarget:popup,relatedTarget:new Element('outside')}}); assert.equal(popup.hidden,true);
state.width=844; notifyCapabilityChange(); attach.onclick({{preventDefault(){{}}}}); assert.equal(popup.hidden,false);
popup.listeners.click({{target:choice}});
assert.equal(attach.focused,false); assert.equal(camera.clicks,1);
camera.listeners.change(); assert.equal(attach.focused,false); frames.shift()(); assert.equal(attach.focused,true);
assert.equal(camera.listeners.cancel,undefined);
attach.focused=false; attach.onclick({{preventDefault(){{}}}}); popup.listeners.click({{target:choice}});
camera.listeners.cancel(); assert.equal(attach.focused,false); frames.shift()(); assert.equal(attach.focused,true);
assert.equal(camera.listeners.change,undefined); assert.equal(popup.hidden,true); assert.equal(attach.getAttribute('aria-expanded'),'false');
state.width=1280; state.coarse=false; state.fine=true; notifyCapabilityChange();
assert.equal(attach.getAttribute('aria-haspopup'),null);
assert.equal(attach.getAttribute('aria-expanded'),null);
assert.equal(attach.getAttribute('aria-controls'),null);
attach.onclick({{preventDefault(){{}}}}); assert.equal(files.clicks,1);
state.width=844; state.coarse=true; state.fine=true; navigator.userAgent='iPhone'; notifyCapabilityChange();
attach.onclick({{preventDefault(){{}}}}); assert.equal(popup.hidden,false);
assert.equal(attach.getAttribute('aria-haspopup'),'menu');
const first=new File(['x'],'image.jpg',{{type:'image/jpeg'}});
const second=new File(['y'],'image.jpg',{{type:'image/jpeg'}});
_handleCameraFiles([first,second]);
assert.deepEqual(admitted.map(file=>file.name),['image.jpg','image (1).jpg']);
"""
    result = subprocess.run(["node", "--eval", script], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_camera_choice_preserves_direct_desktop_and_hybrid_paths():
    boot = read("static/boot.js")
    attach = boot[boot.index("$('btnAttach').onclick=e=>"):boot.index("$('attachChoicePopup')?.addEventListener")]

    assert "if(_shouldOfferCameraChoice())" in attach
    assert "$('fileInput').value=''" in attach
    assert "pointer:coarse" in attach or "_shouldOfferCameraChoice" in attach
    assert "getUserMedia" not in attach
    assert 'id="btnAttach"' in read("static/index.html")
    assert '.composer-left{display:flex' in read("static/style.css")
    assert '.attach-choice-popup{position:absolute;left:8px' in read("static/style.css")


def test_popup_dismissal_has_one_owner_across_lifecycle_boundaries():
    boot = read("static/boot.js")
    panels = read("static/panels.js")

    assert "window._dismissAttachChoice=_dismissAttachChoice" in boot
    capability = boot[boot.index("function _handleAttachChoiceCapabilityChange"):boot.index("function _setAttachChoiceOpen")]
    assert "_dismissAttachChoice(!!(popup&&popup.contains(document.activeElement)))" in capability
    visibility = boot[boot.index("function _applyComposerFooterVisibilitySettings"):boot.index("window._applyComposerFooterVisibilitySettings")]
    assert "hidden.hide_composer_attach" in visibility
    assert "_dismissAttachChoice()" in visibility
    switch = panels[panels.index("async function switchPanel"):panels.index("async function switchPanel") + 2400]
    assert "prevPanel === 'chat'" in switch
    assert "_dismissAttachChoice()" in switch
