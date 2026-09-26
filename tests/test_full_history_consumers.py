"""Real browser coverage for consumers of a bounded session detail window."""
from pathlib import Path
import shutil

import pytest
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def browser():
    if sync_playwright is None:
        pytest.skip("Playwright unavailable")
    with sync_playwright() as p:
        if not shutil.which("node") or not Path(p.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium unavailable")
        instance = p.chromium.launch(headless=True, args=["--no-sandbox"])
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    page.set_content('''<button id="btnDownload">Download</button>
      <span id="workspaceArtifactsCount"></span>
      <section id="workspaceArtifacts"></section>''')
    page.add_script_tag(content='''
      const S={activeProfile:'default',session:{session_id:'a',workspace:'/workspace',
        _messages_truncated:true},messages:[{role:'assistant',content:'tail'}],toolCalls:[]};
      let _loadSessionGeneration=1;
      let _loadingSessionId='a';
      const $=id=>document.getElementById(id);
      const t=key=>key;
      const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',
        '"':'&quot;',"'":'&#39;'}[c]));
      const setStatus=v=>window.statusText=v;
      window.requests=[]; window.downloads=[];
      window.full={session_id:'a',_messages_truncated:false,_messages_start:0,
        messages:[{role:'user',content:'OLDEST MESSAGE'},
          {role:'assistant',content:'old',tool_calls:[{function:{name:'write_file',
            arguments:JSON.stringify({path:'old.md'})}}]},
          {role:'assistant',content:'tail'}]};
      window.api=(url)=>{requests.push(url);return new Promise((resolve,reject)=>{
        window.resolveFetch=()=>resolve({session:structuredClone(full)});
        window.rejectFetch=()=>reject(new Error('offline'));
      });};
      URL.createObjectURL=blob=>{window.lastBlob=blob;return 'blob:export';};
      URL.revokeObjectURL=()=>{};
      HTMLAnchorElement.prototype.click=function(){downloads.push(this.download);};
    ''')
    sessions = (ROOT / 'static/sessions.js').read_text()
    start = sessions.find('function _sessionSnapshotOwner(')
    end = sessions.find('const SESSION_ARCHIVED_PAGE_SIZE', start)
    # Missing production helper on the RED baseline is an explicit regression.
    if start >= 0 and end > start:
        page.add_script_tag(content=sessions[start:end])
    messages = (ROOT / 'static/messages.js').read_text()
    start = messages.index('function transcript(')
    end = messages.index('\n}\n', start) + 3
    page.add_script_tag(content=messages[start:end])
    page.evaluate('() => { window.mockApi=window.api; }')
    page.add_script_tag(content=(ROOT / 'static/workspace.js').read_text())
    page.evaluate('() => { window.api=window.mockApi; }')
    boot = (ROOT / 'static/boot.js').read_text()
    start = boot.index("$('btnDownload').onclick=")
    end = boot.index('\nfunction _buildSessionExportUrl', start)
    page.add_script_tag(content=boot[start:end])
    yield page
    page.close()


def test_export_loads_complete_snapshot_without_touching_stream(page):
    page.click('#btnDownload')
    assert page.evaluate('requests.length') == 1
    assert 'msg_limit=all' in page.evaluate('requests[0]')
    page.evaluate("S.messages.push({role:'assistant',content:'live delta'});resolveFetch()")
    page.wait_for_function('downloads.length === 1')
    assert 'OLDEST MESSAGE' in page.evaluate('lastBlob.text()')
    assert page.evaluate('S.messages.map(m=>m.content)') == ['tail', 'live delta']
    assert page.evaluate('S.session._messages_truncated') is True


@pytest.mark.parametrize('switch', [
    "S.session={session_id:'b'};_loadingSessionId='b'",
    "S.activeProfile='other'",
    "_loadSessionGeneration+=2",  # A -> B -> A
    "_loadingSessionId='b'",  # switch started, old header still displayed
])
def test_export_rejects_departed_owner(page, switch):
    page.click('#btnDownload')
    assert page.evaluate('requests.length') == 1
    page.evaluate(f'{switch};resolveFetch()')
    page.wait_for_function('!document.getElementById("btnDownload").disabled')
    assert page.evaluate('downloads') == []


@pytest.mark.parametrize('failure', [
    'rejectFetch()',
    'full._messages_truncated=true;resolveFetch()',
    "full.session_id='b';resolveFetch()",
    'full.messages=null;resolveFetch()',
    'full._messages_start=10;resolveFetch()',
])
def test_export_never_silently_downloads_partial_history(page, failure):
    page.click('#btnDownload')
    assert page.evaluate('requests.length') == 1
    page.evaluate(failure)
    page.wait_for_function('!document.getElementById("btnDownload").disabled')
    assert page.evaluate('downloads') == []
    assert page.evaluate('Boolean(statusText)') is True


def test_artifacts_load_on_demand_and_keep_live_tail(page):
    page.evaluate('renderSessionArtifacts()')
    assert page.evaluate('requests.length') == 0
    page.evaluate("switchWorkspacePanelTab('artifacts')")
    assert page.evaluate('requests.length') == 1
    page.evaluate('renderSessionArtifacts();renderSessionArtifacts()')
    assert page.evaluate('requests.length') == 1
    page.evaluate('''S.toolCalls=[{name:'write_file',args:{path:'live.md'}}];resolveFetch()''')
    page.wait_for_selector('[data-artifact-path="old.md"]')
    assert page.locator('[data-artifact-path="live.md"]').count() == 1
    assert page.evaluate('S.messages.map(m=>m.content)') == ['tail']
    page.evaluate('renderSessionArtifacts()')
    assert page.evaluate('requests.length') == 1


def test_artifacts_failure_exposes_retry_and_does_not_claim_completeness(page):
    page.evaluate("switchWorkspacePanelTab('artifacts')")
    assert page.evaluate('requests.length') == 1
    page.evaluate('rejectFetch()')
    page.wait_for_selector('[data-artifacts-retry]')
    assert '…' in page.locator('#workspaceArtifactsCount').inner_text()
    page.click('[data-artifacts-retry]')
    assert page.evaluate('requests.length') == 2
    page.evaluate('resolveFetch()')
    page.wait_for_selector('[data-artifact-path="old.md"]')


def test_artifacts_late_old_owner_cannot_overwrite_new_session(page):
    page.evaluate("switchWorkspacePanelTab('artifacts')")
    assert page.evaluate('requests.length') == 1
    page.evaluate('''window.resolveA=resolveFetch;
      S.session={session_id:'b',_messages_truncated:true};_loadingSessionId='b';
      _loadSessionGeneration++;switchWorkspacePanelTab('artifacts');''')
    assert page.evaluate('requests.length') == 2
    page.evaluate("full.session_id='b';full.messages=[{role:'assistant',tool_calls:[{name:'write_file',args:{path:'b.md'}}]}];resolveFetch()")
    page.wait_for_selector('[data-artifact-path="b.md"]')
    page.evaluate("full.session_id='a';resolveA()")
    assert page.locator('[data-artifact-path="b.md"]').count() == 1
    assert page.locator('[data-artifact-path="old.md"]').count() == 0
