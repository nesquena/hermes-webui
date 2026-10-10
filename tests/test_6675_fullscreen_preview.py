"""Static + behavioural regression tests for workspace file-preview fullscreen (#6675).

Covers:
- The fullscreen toggle button exists in the preview header (index.html) with i18n keys.
- The CSS fixed-overlay fallback and native :fullscreen rules exist (style.css).
- The English i18n keys exist (i18n.js).
- boot.js exits fullscreen when the preview is cleared or the workspace panel closes.
- Behaviour (node): toggle falls back to the fixed overlay when the Fullscreen API is
  unavailable, and toggles back off; with the API available it requests native
  fullscreen and syncs on fullscreenchange.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    assert start >= 0, f"start marker not found: {start_marker!r}"
    end = source.find(end_marker, start)
    assert end > start, f"end marker not found: {end_marker!r}"
    return source[start:end]


def _run_node(script: str) -> dict:
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


# ── Static structure ──────────────────────────────────────────────────────────

def test_preview_header_has_fullscreen_button_with_i18n():
    assert "id=\"btnFullscreenPreview\"" in INDEX_HTML
    assert "onclick=\"togglePreviewFullscreen()\"" in INDEX_HTML
    assert "data-i18n=\"preview_fullscreen\"" in INDEX_HTML
    assert "preview-fs-icon-expand" in INDEX_HTML
    assert "preview-fs-icon-compress" in INDEX_HTML


def test_i18n_en_has_fullscreen_keys():
    anchor = "    open_in_browser: 'Open in browser',\n"
    pos = I18N_JS.find(anchor)
    assert pos >= 0, "en open_in_browser anchor not found"
    after = I18N_JS[pos : pos + 300]
    assert "preview_fullscreen: 'Fullscreen'," in after
    assert "preview_fullscreen_exit: 'Exit fullscreen'," in after


def test_css_overlay_and_native_fullscreen_rules_exist():
    assert ".preview-area.preview-fullscreen{position:fixed;inset:0;z-index:9999;" in STYLE_CSS
    assert "#previewArea:fullscreen,#previewArea:-webkit-full-screen{" in STYLE_CSS
    assert "prefers-reduced-motion:reduce" in STYLE_CSS


def test_workspace_js_has_fullscreen_helpers_and_escape_handler():
    assert "function togglePreviewFullscreen(){" in WORKSPACE_JS
    assert "function _previewFsEnterOverlay(){" in WORKSPACE_JS
    assert "function _previewFsExitOverlay(){" in WORKSPACE_JS
    assert "function _exitPreviewFullscreen(){" in WORKSPACE_JS
    assert "classList.add('preview-fullscreen')" in WORKSPACE_JS
    assert "classList.remove('preview-fullscreen')" in WORKSPACE_JS
    # Overlay dismisses on Escape
    assert "e.key === 'Escape' && _previewFsMode === 'overlay'" in WORKSPACE_JS
    # Button is shown for every preview kind via showPreview()
    assert "fsBtn.style.display='inline-flex'" in WORKSPACE_JS


def test_boot_js_exits_fullscreen_on_clear_and_panel_close():
    # clearPreview() leaves fullscreen before tearing the preview down
    clear = _extract_block(BOOT_JS, "function clearPreview(opts={}){", "if(typeof renderBreadcrumb")
    assert "typeof _exitPreviewFullscreen==='function'" in clear
    # Closing the workspace panel leaves fullscreen so the fixed overlay never lingers
    panel = _extract_block(BOOT_JS, "function _setWorkspacePanelMode(mode){", "document.documentElement.dataset.workspacePanel")
    assert "typeof _exitPreviewFullscreen==='function'" in panel


# ── Behaviour (node) ──────────────────────────────────────────────────────────

FULLSCREEN_BLOCK = _extract_block(
    WORKSPACE_JS,
    "let _previewFsMode=null; // null | 'api' | 'overlay'",
    "async function copyPreviewRelativePath(){",
)

_HARNESS = """
function makeButton(){
  const els={};
  const btn={
    title:'',aria:'',
    querySelector(sel){
      if(!els[sel]) els[sel]={style:{display:''},textContent:''};
      return els[sel];
    },
    setAttribute(k,v){ this[k]=v; },
  };
  return btn;
}
function makePreviewArea(){
  const classes=new Set();
  return {
    classList:{
      add(c){classes.add(c);},
      remove(c){classes.delete(c);},
      contains(c){return classes.has(c);},
    },
    hasClass(c){return classes.has(c);},
    requestFullscreen:null,
  };
}
let __doc=null;
const document={
  get fullscreenEnabled(){return __doc.fullscreenEnabled;},
  get webkitFullscreenEnabled(){return __doc.webkitFullscreenEnabled;},
  get fullscreenElement(){return __doc.fullscreenElement;},
  get webkitFullscreenElement(){return __doc.webkitFullscreenElement;},
  // __doc.exits / __doc.wkExits contam as chamadas de saída: é a prova de que a
  // saída só acontece no elemento dono (nunca no fullscreen de outro dono).
  exitFullscreen(){__doc.exits=(__doc.exits||0)+1;__doc.fullscreenElement=null;return Promise.resolve();},
  webkitExitFullscreen(){__doc.wkExits=(__doc.wkExits||0)+1;__doc.webkitFullscreenElement=null;return Promise.resolve();},
};
"""


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_overlay_fallback_toggles_on_and_off_when_api_unavailable():
    script = _HARNESS + f"""
let area=makePreviewArea();
area.classList.add('visible');
const btn=makeButton();
const $=(id)=>id==='previewArea'?area:id==='btnFullscreenPreview'?btn:null;
const t=(k)=>k;
__doc={{fullscreenEnabled:false,fullscreenElement:null}};
{FULLSCREEN_BLOCK}
togglePreviewFullscreen();
const on={{mode:_previewFsMode,overlay:area.hasClass('preview-fullscreen'),
           compressVisible:btn.querySelector('.preview-fs-icon-compress').style.display!=='none'}};
togglePreviewFullscreen();
const off={{mode:_previewFsMode,overlay:area.hasClass('preview-fullscreen'),
            expandVisible:btn.querySelector('.preview-fs-icon-expand').style.display!=='none'}};
process.stdout.write(JSON.stringify({{on,off}}));
"""
    payload = _run_node(script)
    # Enter: overlay class applied, mode='overlay', compress icon shown
    assert payload["on"] == {"mode": "overlay", "overlay": True, "compressVisible": True}
    # Exit: overlay class removed, mode back to null, expand icon shown
    assert payload["off"] == {"mode": None, "overlay": False, "expandVisible": True}


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_native_api_path_requests_fullscreen_and_syncs_on_change():
    script = _HARNESS + f"""
let area=makePreviewArea();
area.classList.add('visible');
area.requestFullscreen=()=>{{__doc.fullscreenElement=area;return Promise.resolve();}};
const btn=makeButton();
const $=(id)=>id==='previewArea'?area:id==='btnFullscreenPreview'?btn:null;
const t=(k)=>k;
__doc={{fullscreenEnabled:true,fullscreenElement:null}};
{FULLSCREEN_BLOCK}
(async()=>{{
  await togglePreviewFullscreen();
  const entered={{mode:_previewFsMode,apiActive:__doc.fullscreenElement===area}};
  // user presses Escape → browser exits → fullscreenchange fires
  __doc.fullscreenElement=null;
  _previewFsOnChange();
  const exited={{mode:_previewFsMode,overlay:area.hasClass('preview-fullscreen')}};
  process.stdout.write(JSON.stringify({{entered,exited}}));
}})().catch(err=>{{console.error(err);process.exit(1);}});
"""
    payload = _run_node(script)
    assert payload["entered"] == {"mode": "api", "apiActive": True}
    assert payload["exited"] == {"mode": None, "overlay": False}


# ── Lifecycle do pedido nativo pendente (PR #6682, ponto 2) ───────────────────
#
# Enquanto requestFullscreen() não assenta, _previewFsMode continua null: exit,
# clear e close não alcançam nada e os dois settlements (sucesso e rejeição)
# reativam estado já desmontado. Os testes rodam os callers REAIS
# (togglePreviewFullscreen/_exitPreviewFullscreen) sobre um pedido controlado
# pelo teste, para poder atrasar cada settlement até depois do teardown.

_PENDING_SETUP = """
let area=makePreviewArea();
area.classList.add('visible');
const settlers=[];
let requests=0;
area.requestFullscreen=()=>{
  requests++;
  return new Promise((res,rej)=>{
    settlers.push({
      res(){ __doc.fullscreenElement=area; res(); },
      rej,
    });
  });
};
const btn=makeButton();
const $=(id)=>id==='previewArea'?area:id==='btnFullscreenPreview'?btn:null;
const t=(k)=>k;
__doc={fullscreenEnabled:true,fullscreenElement:null};
"""

_TICK = "await new Promise(r=>setTimeout(r,0));\n  "


def _pending_script(body: str) -> str:
    return _HARNESS + _PENDING_SETUP + FULLSCREEN_BLOCK + "\n" + body


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_late_success_after_exit_does_not_reactivate_state():
    """Saída atrasada: o sucesso de um pedido cancelado não pode reativar o
    modo api nem deixar o elemento deste preview preso em fullscreen."""
    script = _pending_script(
        f"""(async()=>{{
  togglePreviewFullscreen();
  const pending={{mode:_previewFsMode,requests}};
  _exitPreviewFullscreen();
  settlers[0].res();
  {_TICK}const after={{mode:_previewFsMode,overlay:area.hasClass('preview-fullscreen'),
            nativeActive:__doc.fullscreenElement===area,requests,
            exits:__doc.exits||0}};
  process.stdout.write(JSON.stringify({{pending,after}}));
}})().catch(err=>{{console.error(err);process.exit(1);}});
"""
    )
    payload = _run_node(script)
    assert payload["pending"] == {"mode": None, "requests": 1}
    assert payload["after"] == {
        "mode": None,
        "overlay": False,
        "nativeActive": False,
        "requests": 1,
        "exits": 1,
    }


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_late_rejection_after_exit_does_not_enter_overlay():
    """Saída atrasada: a rejeição de um pedido cancelado não pode trazer o
    overlay de volta por cima do preview já desmontado."""
    script = _pending_script(
        f"""(async()=>{{
  togglePreviewFullscreen();
  _exitPreviewFullscreen();
  settlers[0].rej(new Error('denied'));
  {_TICK}const after={{mode:_previewFsMode,overlay:area.hasClass('preview-fullscreen'),
            requests,exits:__doc.exits||0}};
  process.stdout.write(JSON.stringify({{after}}));
}})().catch(err=>{{console.error(err);process.exit(1);}});
"""
    )
    payload = _run_node(script)
    assert payload["after"] == {
        "mode": None,
        "overlay": False,
        "requests": 1,
        "exits": 0,
    }


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_repeated_click_cancels_pending_request_single_flight():
    """Clique repetido durante o voo invalida a intenção em vez de disparar um
    segundo pedido; um novo clique volta a pedir normalmente."""
    script = _pending_script(
        f"""(async()=>{{
  togglePreviewFullscreen();
  const first={{requests,mode:_previewFsMode}};
  togglePreviewFullscreen();
  const afterClick={{requests,mode:_previewFsMode}};
  settlers[0].res();
  {_TICK}const afterLate={{requests,mode:_previewFsMode,
            overlay:area.hasClass('preview-fullscreen'),
            nativeActive:__doc.fullscreenElement===area}};
  togglePreviewFullscreen();
  const restarted={{requests}};
  settlers[1].res();
  {_TICK}const again={{requests,mode:_previewFsMode}};
  process.stdout.write(JSON.stringify({{first,afterClick,afterLate,restarted,again}}));
}})().catch(err=>{{console.error(err);process.exit(1);}});
"""
    )
    payload = _run_node(script)
    assert payload["first"] == {"requests": 1, "mode": None}
    # single-flight: o clique repetido cancela, não empilha um segundo pedido
    assert payload["afterClick"] == {"requests": 1, "mode": None}
    # o settlement do pedido cancelado não reativa nada nem prende o elemento
    assert payload["afterLate"] == {
        "requests": 1,
        "mode": None,
        "overlay": False,
        "nativeActive": False,
    }
    assert payload["restarted"] == {"requests": 2}
    assert payload["again"] == {"requests": 2, "mode": "api"}


# ── Posse do fullscreen nativo (PR #6682, ponto 3) ────────────────────────────
#
# O fullscreen é document-wide, não do preview: _previewFsOnChange() adotava
# qualquer document.fullscreenElement e o teardown saía desse elemento alheio.
# Aqui o dono externo (caminho padrão e prefixed) não pode ser adotado nem
# derrubado, e o elemento DESTE preview continua saindo nos dois caminhos.

_OWNERSHIP_SETUP = """
let area=makePreviewArea();
area.classList.add('visible');
const btn=makeButton();
const $=(id)=>id==='previewArea'?area:id==='btnFullscreenPreview'?btn:null;
const t=(k)=>k;
const outside={classList:{add(){},remove(){},contains(){return false;}}};
"""


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_outside_fullscreen_owner_is_not_adopted_or_exited():
    """Dono externo no caminho padrão: sem adotar mode='api' e sem cancelar o
    fullscreen alheio quando o preview é limpo."""
    script = _HARNESS + _OWNERSHIP_SETUP + f"""
__doc={{fullscreenEnabled:true,fullscreenElement:outside}};
{FULLSCREEN_BLOCK}
_previewFsOnChange();
const adopted={{mode:_previewFsMode}};
_exitPreviewFullscreen();
const after={{mode:_previewFsMode,exits:__doc.exits||0,
              ownerKept:__doc.fullscreenElement===outside}};
process.stdout.write(JSON.stringify({{adopted,after}}));
"""
    payload = _run_node(script)
    assert payload["adopted"] == {"mode": None}
    assert payload["after"] == {
        "mode": None,
        "exits": 0,
        "ownerKept": True,
    }


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_outside_fullscreen_owner_is_not_adopted_or_exited_prefixed():
    """Mesmo cenário no caminho prefixed (webkit): sem adotar e sem chamar
    webkitExitFullscreen() sobre o dono externo."""
    script = _HARNESS + _OWNERSHIP_SETUP + f"""
__doc={{fullscreenEnabled:false,webkitFullscreenEnabled:true,
       fullscreenElement:null,webkitFullscreenElement:outside}};
{FULLSCREEN_BLOCK}
_previewFsOnChange();
const adopted={{mode:_previewFsMode}};
_exitPreviewFullscreen();
const after={{mode:_previewFsMode,exits:__doc.exits||0,wkExits:__doc.wkExits||0,
              ownerKept:__doc.webkitFullscreenElement===outside}};
process.stdout.write(JSON.stringify({{adopted,after}}));
"""
    payload = _run_node(script)
    assert payload["adopted"] == {"mode": None}
    assert payload["after"] == {
        "mode": None,
        "exits": 0,
        "wkExits": 0,
        "ownerKept": True,
    }


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_own_preview_element_still_enters_and_exits_on_both_paths():
    """Guarda anti-escopo-excessivo: o elemento DESTE preview continua entrando
    e saindo normalmente nos caminhos padrão e prefixed."""
    script = _HARNESS + _OWNERSHIP_SETUP + f"""
__doc={{fullscreenEnabled:true,fullscreenElement:null}};
area.requestFullscreen=()=>{{__doc.fullscreenElement=area;return Promise.resolve();}};
{FULLSCREEN_BLOCK}
(async()=>{{
  await togglePreviewFullscreen();
  const entered={{mode:_previewFsMode,own:__doc.fullscreenElement===area}};
  togglePreviewFullscreen();
  _previewFsOnChange();
  const exited={{mode:_previewFsMode,exits:__doc.exits||0,
                 nativeActive:!!__doc.fullscreenElement}};

  __doc={{fullscreenEnabled:false,webkitFullscreenEnabled:true,
         fullscreenElement:null,webkitFullscreenElement:null}};
  area.requestFullscreen=null; // força o caminho prefixed
  area.webkitRequestFullscreen=()=>{{__doc.webkitFullscreenElement=area;return Promise.resolve();}};
  await togglePreviewFullscreen();
  const enteredWk={{mode:_previewFsMode,own:__doc.webkitFullscreenElement===area}};
  togglePreviewFullscreen();
  _previewFsOnChange();
  const exitedWk={{mode:_previewFsMode,wkExits:__doc.wkExits||0,
                   nativeActive:!!__doc.webkitFullscreenElement}};
  process.stdout.write(JSON.stringify({{entered,exited,enteredWk,exitedWk}}));
}})().catch(err=>{{console.error(err);process.exit(1);}});
"""
    payload = _run_node(script)
    assert payload["entered"] == {"mode": "api", "own": True}
    assert payload["exited"] == {"mode": None, "exits": 1, "nativeActive": False}
    assert payload["enteredWk"] == {"mode": "api", "own": True}
    assert payload["exitedWk"] == {
        "mode": None,
        "wkExits": 1,
        "nativeActive": False,
    }


# ── Geometria do fallback (PR #6682, ponto 4) ─────────────────────────────────
#
# #previewArea vive dentro de .rightpanel, que sempre tem transform:translateX(0)
# — e transform vira containing block de position:fixed, então o fallback enchia
# os ~300px do painel, não a viewport (299x805 a 1440x844; 299x844 a 390x844).
# O teste mede a geometria real em Chromium headless, offline, com o CSS
# production e o bloco production do workspace.js.

_GEOMETRY_HTML = """<!doctype html>
<html><head><meta charset="utf-8"></head>
<body>
<div class="layout">
  <main style="flex:1;min-width:0"></main>
  <aside class="rightpanel mobile-open">
    <div class="preview-area visible" id="previewArea">
      <div class="preview-path" id="previewPath">
        <span id="previewPathText">/tmp/demo.md</span>
        <button id="btnFullscreenPreview" class="panel-icon-btn"><span class="preview-btn-label">Fullscreen</span></button>
      </div>
      <pre class="preview-code" id="previewCode"># demo</pre>
    </div>
  </aside>
</div>
</body></html>
"""

_GEOMETRY_SCRIPT = """
const $=(id)=>document.getElementById(id);
const t=(k)=>k;
""" + FULLSCREEN_BLOCK + """
window.__run=()=>{
  const el=document.getElementById('previewArea');
  const home={parent:el.parentNode,next:el.nextSibling};
  // Força o caminho de fallback: sem API nativa não há fullscreen de elemento.
  // Chromium também expõe o alias webkit, então os dois precisam cair.
  Object.defineProperty(document,'fullscreenEnabled',{configurable:true,get:()=>false});
  Object.defineProperty(document,'webkitFullscreenEnabled',{configurable:true,get:()=>false});
  togglePreviewFullscreen();
  const r=el.getBoundingClientRect();
  const label=el.querySelector('.preview-btn-label');
  const full={
    mode:_previewFsMode,
    overlay:el.classList.contains('preview-fullscreen'),
    width:Math.round(r.width),
    height:Math.round(r.height),
    inPanel:!!el.closest('.rightpanel'),
    labelDisplay:getComputedStyle(label).display,
    vw:window.innerWidth,
    vh:window.innerHeight,
  };
  _exitPreviewFullscreen();
  const restored={
    mode:_previewFsMode,
    overlay:el.classList.contains('preview-fullscreen'),
    inPanel:el.parentNode===home.parent,
    sameSlot:el.nextSibling===home.next,
  };
  return {full,restored};
};
"""


def _measure_geometry(viewport_width: int, viewport_height: int = 844) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependência ausente
        pytest.skip("playwright indisponível; rode o teste de geometria do fallback")

    playwright = sync_playwright().start()
    # Só a ausência do binário do browser pula o teste; depois de um launch
    # bem-sucedido qualquer falha de medição precisa aparecer como erro.
    try:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    except Exception as exc:  # pragma: no cover - sandbox sem browser
        playwright.stop()
        pytest.skip(f"chromium indisponível para medição: {exc}")

    try:
        page = browser.new_page(
            viewport={"width": viewport_width, "height": viewport_height}
        )
        page.set_content(_GEOMETRY_HTML)
        page.add_style_tag(content=STYLE_CSS)
        page.add_script_tag(content=_GEOMETRY_SCRIPT)
        return page.evaluate("() => window.__run()")
    finally:
        browser.close()
        playwright.stop()


def test_fallback_overlay_fills_viewport_at_1440_and_restores_dom():
    m = _measure_geometry(1440, 844)
    full, restored = m["full"], m["restored"]
    assert full["mode"] == "overlay", m
    assert full["overlay"] is True, m
    # O fallback precisa preencher a viewport, não os ~300px do painel.
    assert full["width"] >= 1440 - 2, m
    assert full["height"] >= 844 - 2, m
    # ... e a propriedade do DOM é restaurada em todo exit.
    assert restored == {
        "mode": None,
        "overlay": False,
        "inPanel": True,
        "sameSlot": True,
    }, m


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_fallback_overlay_keeps_rightpanel_container_queries():
    # 1440px: com o overlay no topo o container rightpanel resolve contra os
    # 1440px reais (rótulo visível); preso no painel de 300px ele sumiria.
    wide = _measure_geometry(1440, 844)
    assert wide["full"]["width"] >= 1440 - 2, wide
    assert wide["full"]["labelDisplay"] != "none", wide
    # 390px: as mesmas regras responsivas continuam valendo dentro do overlay.
    narrow = _measure_geometry(390, 844)
    assert narrow["full"]["width"] >= 390 - 2, narrow
    assert narrow["full"]["labelDisplay"] == "none", narrow
    assert narrow["restored"]["inPanel"] is True, narrow


def test_rightpanel_keeps_its_transform():
    """Guarda contra o atalho proibido: remover o transform do painel mudaria o
    comportamento do painel inteiro em vez de escapar do ancestor no overlay."""
    assert re.search(r"\.rightpanel\{[^}]*transform:translateX\(0\)", STYLE_CSS)
