"""Real browser coverage for consumers of a bounded session detail window."""
import ast
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
    page.set_default_timeout(5000)
    page.set_content('''<button id="btnDownload">Download</button>
      <span id="workspaceArtifactsCount"></span>
      <section id="workspaceArtifacts"></section>''')
    page.add_script_tag(content='''
      const S={activeProfile:'default',session:{session_id:'a',workspace:'/workspace',
        _messages_truncated:true},messages:[{role:'assistant',content:'tail'}],toolCalls:[]};
      let _loadSessionGeneration=1;
      let _loadingSessionId='a';
      const $=id=>document.getElementById(id);
      const t=(key,vars={})=>Object.entries(vars).reduce(
        (text,[name,value])=>text.replace('{'+name+'}',String(value)),
        (window.testTranslations||{})[key]||key);
      const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',
        '"':'&quot;',"'":'&#39;'}[c]));
      const setStatus=v=>window.statusText=v;
      window.requests=[]; window.downloads=[];
      window.full={session_id:'a',_messages_truncated:false,_messages_offset:0,
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
    panels = (ROOT / 'static/panels.js').read_text()
    start = panels.index('function _syncHermesPanelSessionActions(){')
    end = panels.index('\n}\n', start) + 3
    page.add_script_tag(content=panels[start:end])
    page.evaluate('''() => { const handler=$('btnDownload').onclick;
      $('btnDownload').onclick=()=>{window.downloadTask=handler();}; }''')
    yield page
    page.close()


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('state', ['loading', 'failure', 'loaded', 'empty'])
def test_artifacts_responsive_history_states(page, tmp_path, width, state):
    """Use production panel markup, CSS and English text at both breakpoints."""
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 900})
    html = (ROOT / 'static/index.html').read_text()
    start = html.index('<aside class="rightpanel">')
    panel = html[start:html.index('</aside>', start) + len('</aside>')]
    page.evaluate("""panel => {
      document.body.innerHTML='<div class="layout"><main style="flex:1"></main>'+panel+'</div>';
      document.documentElement.dataset.workspacePanel='open';
      document.querySelector('.rightpanel').classList.add('mobile-open');
    }""", panel)
    page.add_style_tag(content=(ROOT / 'static/style.css').read_text())
    lines = (ROOT / 'static/i18n.js').read_text().splitlines()
    translations = {
        key: ast.literal_eval(next(line for line in lines if line.startswith(f'    {key}:'))
                              .split(':', 1)[1].strip().removesuffix(','))
        for key in ('loading', 'session_history_failed', 'steer_recovery_retry')
    }
    page.evaluate('values=>window.testTranslations=values', translations)
    if state == 'empty':
        page.evaluate('full.messages=[]')
    page.evaluate("switchWorkspacePanelTab('artifacts')")
    if state == 'failure':
        page.evaluate('rejectFetch()')
        page.wait_for_selector('[data-artifacts-retry]')
        assert 'Could not load complete' in page.locator('#workspaceArtifacts').inner_text()
    elif state == 'loaded':
        page.evaluate('resolveFetch()')
        page.wait_for_selector('[data-artifact-path="old.md"]')
    elif state == 'empty':
        page.evaluate('resolveFetch()')
        page.wait_for_function("document.getElementById('workspaceArtifacts').textContent.includes('No artifacts detected')")
    else:
        assert 'Loading' in page.locator('#workspaceArtifacts').inner_text()
    root = page.locator('#workspaceArtifacts')
    assert root.is_visible()
    assert root.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    box = root.bounding_box()
    assert box is not None and box['width'] > 100
    assert box['x'] >= -1 and box['x'] + box['width'] <= width + 1
    count = page.locator('#workspaceArtifactsCount').inner_text()
    assert ('…' in count) == (state in {'loading', 'failure'})
    page.screenshot(path=str(tmp_path / f'artifacts-{width}-{state}.png'))
    if state == 'failure':
        page.click('[data-artifacts-retry]')
        assert page.evaluate('requests.length') == 2
        page.evaluate('resolveFetch()')
        page.wait_for_selector('[data-artifact-path="old.md"]')
    assert errors == []


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
    page.evaluate('downloadTask')
    assert page.evaluate('downloads') == []


@pytest.mark.parametrize('failure', [
    'rejectFetch()',
    'full._messages_truncated=true;resolveFetch()',
    "full.session_id='b';resolveFetch()",
    'full.messages=null;resolveFetch()',
    'full._messages_offset=10;resolveFetch()',
])
def test_export_never_silently_downloads_partial_history(page, failure):
    page.click('#btnDownload')
    assert page.evaluate('requests.length') == 1
    page.evaluate(failure)
    page.wait_for_function('!document.getElementById("btnDownload").disabled')
    assert page.evaluate('downloads') == []
    assert page.evaluate('statusText') == 'session_history_failed'


def test_artifacts_load_on_demand_and_keep_live_tail(page):
    # Production loads metadata first; its truncation flag is false even when
    # _ensureMessagesLoaded has only loaded a page into S.messages.
    page.evaluate('S.session._messages_truncated=false;renderSessionArtifacts()')
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


def test_artifacts_include_middle_of_large_history(page):
    page.evaluate('''full.messages=Array.from({length:80},(_,i)=>({
      role:'assistant',tool_calls:[{name:'write_file',args:{path:`file-${i}.md`}}]}));
      S.messages=full.messages.slice(-15);switchWorkspacePanelTab('artifacts');resolveFetch();''')
    page.wait_for_function("document.getElementById('workspaceArtifactsCount').textContent==='80'")
    assert page.locator('[data-artifact-path="file-60.md"]').count() == 1
    assert page.locator('[data-artifact-path]').count() == 80


def test_artifacts_reuse_same_version_but_refresh_changed_history(page):
    page.evaluate("window._isSessionCurrentPane=sid=>sid===S.session.session_id;switchWorkspacePanelTab('artifacts');resolveFetch()")
    page.wait_for_selector('[data-artifact-path="old.md"]')
    page.evaluate("switchWorkspacePanelTab('files');switchWorkspacePanelTab('artifacts');projectSessionArtifactsForOwner('a')")
    assert page.evaluate('requests.length') == 1
    page.evaluate("S.session.message_count=80;projectSessionArtifactsForOwner('a')")
    assert page.evaluate('requests.length') == 2
    page.evaluate('resolveFetch()')
    page.wait_for_function("document.getElementById('workspaceArtifactsCount').textContent==='1'")


def test_old_download_does_not_enable_empty_new_session(page):
    page.click('#btnDownload')
    page.evaluate("S.session={session_id:'empty'};S.messages=[];_loadingSessionId='empty';_loadSessionGeneration++;_syncHermesPanelSessionActions();resolveFetch()")
    page.evaluate('downloadTask')
    assert page.locator('#btnDownload').is_disabled()
    assert page.evaluate('downloads') == []


def test_old_download_does_not_enable_new_pending_download(page):
    page.click('#btnDownload')
    page.evaluate("window.oldTask=downloadTask;window.resolveA=resolveFetch;S.session={session_id:'b'};_loadingSessionId='b';_loadSessionGeneration++;_syncHermesPanelSessionActions()")
    page.click('#btnDownload')
    page.evaluate('resolveA()')
    page.evaluate('oldTask')
    assert page.locator('#btnDownload').is_disabled()
    page.evaluate("full.session_id='b';resolveFetch()")
    page.evaluate('downloadTask')
    assert not page.locator('#btnDownload').is_disabled()
    assert page.evaluate('downloads') == ['hermes-b.md']
