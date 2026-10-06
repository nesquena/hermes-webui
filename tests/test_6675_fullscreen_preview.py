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
  get fullscreenElement(){return __doc.fullscreenElement;},
  // __doc.exits conta as chamadas de saída: é a prova de que a saída só pode
  // acontecer no elemento dono (nunca no fullscreen de outro dono).
  exitFullscreen(){__doc.exits=(__doc.exits||0)+1;__doc.fullscreenElement=null;return Promise.resolve();},
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
