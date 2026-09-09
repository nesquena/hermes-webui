"""Headless browser contract tests for capability-only dismissal rendering."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")


def _function(name):
    source = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    start = source.find(f"function {name}(")
    assert start >= 0
    brace = source.find("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{": depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(name)


def _run(source):
    result = subprocess.run([NODE], input=source, cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _preamble():
    funcs = "\n".join(_function(name) for name in ("_isProviderErrorCardMessage", "_messageIsRenderable", "_providerErrorDismissalButtonHtml"))
    return funcs + """
function msgContent(m){return String(m&&m.content||'').trim();}
function _isContextCompactionMessage(){return false;}
function _isPreservedCompressionTaskListMessage(){return false;}
function _isRecoveryControlMessage(){return false;}
function _messageHasReasoningPayload(){return false;}
function _assistantMessageHasVisibleContent(m){return !!msgContent(m);}
function _isReadOnlySession(){return false;}
function t(k){return k;}
function esc(v){return String(v||'');}
function li(){return '<svg></svg>';}
"""


def test_only_server_reference_renders_without_content_or_index():
    result = _run(_preamble() + """
global.S={session:{session_id:'s1'},busy:false,activeStreamId:null};
const ref='a'.repeat(64);
const button=_providerErrorDismissalButtonHtml({_provider_error_dismiss_ref:ref},0,false);
const invalid='Z'.repeat(64);
console.log(JSON.stringify({button,invalid:_isProviderErrorCardMessage({_provider_error_dismiss_ref:invalid}),invalidButton:_providerErrorDismissalButtonHtml({_provider_error_dismiss_ref:invalid},0,false),hasContent:button.includes('provider failed'),hasIndex:button.includes('message-index'),hasRef:button.includes('data-dismiss-ref')}));
""")
    assert result["hasContent"] is False and result["hasIndex"] is False and result["hasRef"] is True
    assert result["invalid"] is False and result["invalidButton"] == ""


def test_dismissed_provider_marker_hides_only_marked_row_and_ordinary_tombstone_survives():
    result = _run(_preamble() + """
global.S={session:{session_id:'s1'},busy:false,activeStreamId:null};
const ref='c'.repeat(64);
console.log(JSON.stringify({provider:_messageIsRenderable({role:'assistant',content:'provider failed',_provider_error_dismiss_ref:ref,_dismissed:true}),ordinary:_messageIsRenderable({_dismissed:true,role:'assistant',content:'answer'}),imported:_messageIsRenderable({role:'assistant',content:'provider failed',_error:true,_provider_error_type:'error',_dismissed:true}),button:_providerErrorDismissalButtonHtml({_provider_error_dismiss_ref:ref,_dismissed:true},0,false)}));
""")
    assert result == {"provider": False, "ordinary": True, "imported": True, "button": ""}


def test_real_dom_dismissal_posts_only_reference_and_honors_localized_focus_contract():
    playwright_api = pytest.importorskip("playwright.sync_api")
    source = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    dismiss_start = source.index("async function dismissProviderError")
    dismiss_end = source.index("\nfunction _assistantAnchorSceneFinalAnswerText", dismiss_start)
    dismiss_source = source[dismiss_start:dismiss_end]
    ref = "b" * 64
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is unavailable: {exc}")
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_content(
            f'<button data-dismiss-ref="{ref}" aria-label="Dismiss localized error">remove</button>'
        )
        page.evaluate(
            """
            window.S={session:{session_id:'session-1'},busy:false,activeStreamId:null};
            window._loadSessionGeneration=4;
            window.confirmation=null;
            window.requestBody=null;
            window.t=(key)=>({dismiss_error_card:'Localized dismiss',dismiss_error_confirm:'Localized confirm',remove:'remove'})[key]||key;
            window.showConfirmDialog=async (options)=>{window.confirmation=options;return true;};
            window.api=async (_path, options)=>{window.requestBody=JSON.parse(options.body);return {};};
            window.loadSession=async ()=>document.querySelector('button').remove();
            window.showToast=()=>{};
            """
        )
        page.evaluate(f"(async()=>{{{dismiss_source};window.dismissProviderError=dismissProviderError;}})()")
        page.evaluate("window.dismissProviderError(document.querySelector('button'))")
        assert page.evaluate("window.requestBody") == {
            "session_id": "session-1",
            "dismiss_ref": ref,
        }
        assert page.evaluate("window.confirmation.focusCancel") is True
        assert page.evaluate("window.confirmation.confirmLabel") == "remove"
        assert page.locator("button").count() == 0
        browser.close()
