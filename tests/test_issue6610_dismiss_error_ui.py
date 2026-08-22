"""Behavioral tests for the issue #6610 provider-error dismissal affordance."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> dict:
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
    return json.loads(result.stdout)


def _extract_function(name: str) -> str:
    source = UI_JS.read_text(encoding="utf-8")
    marker = f"async function {name}("
    if marker not in source:
        marker = f"function {name}("
    start = source.find(marker)
    if start < 0:
        raise AssertionError(f"{name} is not present in static/ui.js")
    signature_end = re.search(r"\)\s*\{", source[start:])
    if not signature_end:
        raise AssertionError(f"{name} signature is not complete")
    brace = start + signature_end.end() - 1
    depth = 1
    cursor = brace + 1
    while depth and cursor < len(source):
        if source[cursor] == "{":
            depth += 1
        elif source[cursor] == "}":
            depth -= 1
        cursor += 1
    return source[start:cursor]


def _node_preamble() -> str:
    names = (
        "_isProviderErrorCardMessage",
        "_providerErrorDismissalSessionAllowed",
        "_messageIsRenderable",
        "_messageSessionIndexForRawIdx",
        "_providerErrorDismissalButtonHtml",
        "dismissProviderError",
    )
    funcs = "\n".join(_extract_function(name) for name in names)
    return f"""
{funcs}
function msgContent(m) {{
  const value = m && m.content;
  return String(Array.isArray(value) ? value.map(p => p && (p.text || p.content) || '').join('') : value || '').trim();
}}
function _isContextCompactionMessage() {{ return false; }}
function _isPreservedCompressionTaskListMessage() {{ return false; }}
function _isRecoveryControlMessage(m) {{ return !!(m && m.recovery_control); }}
function _messageHasReasoningPayload() {{ return false; }}
function _assistantMessageHasVisibleContent(m) {{ return !!msgContent(m); }}
function _isReadOnlySession(s) {{ return !!(s && (s.read_only || s.is_read_only)); }}
function t(key) {{ return {{dismiss_error_card:'Dismiss error card'}}[key] || key; }}
function esc(value) {{ return String(value || ''); }}
function li() {{ return '<svg></svg>'; }}
let _oldestIdx = 40;
function _messageSessionIndexBase() {{ return Number(_oldestIdx) || 0; }}
"""


def test_eligible_legacy_row_has_action_and_absolute_index():
    result = _run_node(
        _node_preamble()
        + """
global.S = {session:{session_id:'s1'}, busy:false, activeStreamId:null};
const row = {role:'assistant', content:'provider failed', _error:true, provider_details:'quota', timestamp:7};
const button = _providerErrorDismissalButtonHtml(row, 3, false);
console.log(JSON.stringify({eligible:_isProviderErrorCardMessage(row), button, renderable:_messageIsRenderable(row)}));
"""
    )
    assert result["eligible"] is True
    assert 'data-message-index="43"' in result["button"]
    assert "dismissProviderError" in result["button"]
    assert result["renderable"] is True


def test_tombstone_hides_only_provider_error_rows_and_control_rows_have_no_action():
    result = _run_node(
        _node_preamble()
        + """
global.S = {session:{session_id:'s1'}, busy:false, activeStreamId:null};
const dismissed = {role:'assistant', content:'provider failed', _error:true, provider_details:'quota', _dismissed:true};
const ordinary = {role:'assistant', content:'normal answer', _dismissed:true};
const excluded = [
  {role:'assistant', content:'cancelled', _error:true, provider_details:'cancel', provider_details_label:'Cancellation details'},
  {role:'assistant', content:'interrupted', _error:true, type:'interrupted'},
  {role:'assistant', content:'recovery', _error:true, recovery_control:true},
  {role:'assistant', content:'compression', _error:true, _compressionRecovery:{kind:'x'}},
  {role:'assistant', content:'**Goal command failed:** local failure', _error:true},
  {role:'assistant', content:'**Task cancelled:** Task cancelled.', _error:true, provider_details:'Task cancelled.'},
];
console.log(JSON.stringify({dismissed:_messageIsRenderable(dismissed), ordinary:_messageIsRenderable(ordinary), excluded:excluded.map(x => !!_providerErrorDismissalButtonHtml(x, 0, false))}));
"""
    )
    assert result == {"dismissed": False, "ordinary": True, "excluded": [False, False, False, False, False, False]}


def test_action_is_omitted_for_read_only_and_busy_sessions():
    result = _run_node(
        _node_preamble()
        + """
const row = {role:'assistant', content:'provider failed', _error:true, provider_details:'quota'};
global.S = {session:{session_id:'s1', read_only:true}, busy:false, activeStreamId:null};
const readOnly = _providerErrorDismissalButtonHtml(row, 0, true);
global.S = {session:{session_id:'s1'}, busy:true, activeStreamId:'run-1'};
const busy = _providerErrorDismissalButtonHtml(row, 0, false);
console.log(JSON.stringify({readOnly, busy}));
"""
    )
    assert result == {"readOnly": "", "busy": ""}


def test_action_is_omitted_for_external_and_foreign_profile_sessions():
    result = _run_node(
        _node_preamble()
        + """
const row = {role:'assistant', content:'provider failed', _error:true, provider_details:'quota'};
const cases = [
  {is_cli_session:true},
  {session_source:'messaging'},
  {source_tag:'subagent'},
  {profile:'other'},
];
global.S = {session:{session_id:'s1'}, activeProfile:'default', busy:false, activeStreamId:null};
const results = cases.map(session => {
  global.S.session = Object.assign({session_id:'s1'}, session);
  return _providerErrorDismissalButtonHtml(row, 0, false);
});
console.log(JSON.stringify(results));
"""
    )
    assert result == ["", "", "", ""]


def test_browser_button_dialog_and_confirmed_reload_use_real_dom():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright is unavailable; run the issue 6610 browser proof")

    names = (
        "_isProviderErrorCardMessage",
        "_providerErrorDismissalSessionAllowed",
        "_messageSessionIndexForRawIdx",
        "_providerErrorDismissalButtonHtml",
        "_isAppDialogOpen",
        "_getAppDialogFocusable",
        "_finishAppDialog",
        "_ensureAppDialogBindings",
        "showConfirmDialog",
        "dismissProviderError",
    )
    funcs = "\n".join(_extract_function(name) for name in names)
    fixture = f"""
const APP_DIALOG={{resolve:null,kind:null,lastFocus:null}};
let _appDialogBound=false;
function $(id){{return document.getElementById(id);}}
function msgContent(m){{return String(m&&m.content||'').trim();}}
function t(key){{return {{dismiss_error_card:'Dismiss error card',dismiss_error_confirm:'Dismiss this card?',remove:'Remove',cancel:'Cancel'}}[key]||key;}}
function esc(value){{return String(value||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
function li(){{return '<svg></svg>';}}
function _messageSessionIndexBase(){{return 0;}}
{funcs}
var S={{session:{{session_id:'s1',profile:'default',session_source:'webui'}},activeProfile:'default',messages:[{{role:'assistant',content:'provider failed',_error:true,_provider_error_type:'error',provider_details:'quota',timestamp:7}}],busy:false,activeStreamId:null}};
var _loadSessionGeneration=1;
let resolveReload;
window.__reloadDone=new Promise(resolve=>{{resolveReload=resolve;}});
let requestBody=null;
async function api(path,opts){{requestBody={{path,body:JSON.parse(opts.body)}};return {{ok:true}};}}
async function loadSession(){{S.messages=[{{role:'assistant',content:'later answer'}}];document.getElementById('actions').innerHTML='';resolveReload();}}
window.__runDismissal=async()=>{{
  const row=S.messages[0];
  document.getElementById('actions').innerHTML=_providerErrorDismissalButtonHtml(row,0,false);
  const first=document.querySelector('.msg-dismiss-error-btn');
  first.click();
  await new Promise(resolve=>setTimeout(resolve,30));
  const cancelFocused=document.activeElement===document.getElementById('appDialogCancel');
  document.getElementById('appDialogCancel').click();
  await new Promise(resolve=>setTimeout(resolve,10));
  const restoredAfterCancel=!!document.querySelector('.msg-dismiss-error-btn')&&!first.disabled;
  document.querySelector('.msg-dismiss-error-btn').click();
  await new Promise(resolve=>setTimeout(resolve,30));
  const confirmFocused=document.activeElement===document.getElementById('appDialogCancel');
  document.getElementById('appDialogConfirm').click();
  await window.__reloadDone;
  return {{cancelFocused,restoredAfterCancel,confirmFocused,requestBody,buttonsAfterReload:document.querySelectorAll('.msg-dismiss-error-btn').length}};
}};
"""
    html = """
    <!doctype html><html><body>
      <div id="actions"></div>
      <div id="appDialogOverlay" style="display:none"><div id="appDialog" role="dialog">
        <h2 id="appDialogTitle"></h2><p id="appDialogDesc"></p>
        <input id="appDialogInput" style="display:none"><button id="appDialogCancel">Cancel</button>
        <button id="appDialogConfirm">Confirm</button><button id="appDialogClose">Close</button>
      </div></div>
    </body></html>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.set_content(html)
        page.add_script_tag(content=fixture)
        if not page.evaluate("typeof window.__runDismissal === 'function'"):
            raise AssertionError(f"browser fixture did not load: {page_errors}")
        result = page.evaluate("window.__runDismissal()")
        browser.close()

    assert result == {
        "cancelFocused": True,
        "restoredAfterCancel": True,
        "confirmFocused": True,
        "requestBody": {
            "path": "/api/session/message/dismiss-error",
            "body": {
                "session_id": "s1",
                "message_index": 0,
                "expected_message": {"role": "assistant", "content": "provider failed", "timestamp": 7},
            },
        },
        "buttonsAfterReload": 0,
    }


def test_confirmation_focuses_cancel_and_stale_session_completion_is_ignored():
    result = _run_node(
        _node_preamble()
        + """
global.S = {session:{session_id:'s1'}, messages:[{role:'assistant', content:'provider failed', _error:true, provider_details:'quota', timestamp:7}], busy:false, activeStreamId:null};
global._loadSessionGeneration = 4;
const button = {dataset:{rawIndex:'0', messageIndex:'40'}, disabled:false, attrs:{}, setAttribute(k,v){this.attrs[k]=v;}, removeAttribute(k){delete this.attrs[k];}};
let confirmOptions = null;
let apiCalls = 0;
async function showConfirmDialog(opts){ confirmOptions = opts; return true; }
async function api(){ apiCalls += 1; global.S.session = {session_id:'s2'}; return {ok:true}; }
async function loadSession(){ throw new Error('stale completion must not refresh the switched session'); }
function showToast(){ throw new Error('stale completion must not toast'); }
(async()=>{ await dismissProviderError(button); console.log(JSON.stringify({focusCancel:confirmOptions.focusCancel, apiCalls, disabled:button.disabled, session:S.session.session_id})); })();
"""
    )
    assert result == {"focusCancel": True, "apiCalls": 1, "disabled": True, "session": "s2"}
