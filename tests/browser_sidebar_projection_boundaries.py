"""Unread persistence, tall stable-order anchors and native chip focus boundaries.

Production grouping/window/row code and CSS, real browser Storage. Session payloads
and transport/navigation are fixture seams; no live Agent or user state.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._sidebar_child_status_helpers import ROOT  # noqa: E402
from tests.browser_session_virtual_geometry import geometry_script  # noqa: E402


def function(source, name):
    return re.search(r'^function ' + name + r'\(.*?^\}', source, re.M | re.S).group()


def boundary_script(source):
    component = geometry_script(source).replace(
        'const _showArchived=false, _sessionSelectMode=false, _showAllProfiles=false;',
        'const _showArchived=false, _showAllProfiles=false;let _sessionSelectMode=false;')
    # Replace only synthetic unread seams with the production store implementation.
    for name in ['_hasSessionCompletionUnread', '_getSessionViewedCounts',
                 '_sessionTranscriptGenerationForUnread', '_sessionViewedCountRecord',
                 '_setSessionViewedCount', '_hasUnreadForSession']:
        if name == '_hasUnreadForSession':
            component = component.replace(function(component, name), '')
        else:
            component = re.sub(r'^function ' + name + r'\([^\n]*\n', '', component, flags=re.M)
    stores = source[source.index('function _getSessionViewedCounts('):
                    source.index('// Keep the sidebar polling snapshot current')]
    constants = '\n'.join(line for line in source.splitlines()
                          if re.match(r'^const SESSION_(VIEWED_COUNTS|COMPLETION_UNREAD|OBSERVED_STREAMING)', line))
    return component + constants + stores + function(source, 'toggleSessionSelectMode') + r"""
let _sessionViewedCounts=null,_sessionCompletionUnread=null,_sessionObservedStreaming=null;
let _sessionCompletionUnreadClearedMemory={},_sessionProfileCounts=null;
const _sessionListSnapshotById=new Map(),_selectedSessions=new Set();
let _sessionListHasLoadedOnce=true,_allSessions=[];
window.storageWrites=[];
const originalSetItem=Storage.prototype.setItem;
Storage.prototype.setItem=function(k,v){storageWrites.push({key:k,units:v.length});return originalSetItem.call(this,k,v);};
function resetStores(){
 localStorage.clear();storageWrites.length=0;
 _sessionViewedCounts=null;_sessionCompletionUnread=null;_sessionCompletionUnreadClearedMemory={};
}
function inventory(n){
 resetStores();_sessionSelectMode=false;_allProjects.length=0;
 _expandedChildSessionKeys.clear();_expandedLineageKeys.clear();
 $('sessionList').scrollTop=0;delete $('sessionList')._sessionVirtualLayout;
 activeSidForSidebar='other';window._sidebarDensity='detailed';
 fixtureSessions=Array.from({length:n},(_,i)=>({session_id:'p'+i,title:'Conversation '+i,message_count:3}));
 _allSessions=fixtureSessions;
 groups=[{label:'Today',items:fixtureSessions}];repaint();
}
function tallScene(){
 inventory(120);
 const parent=fixtureSessions[0];parent.title='A measured expanded conversation';
 parent._child_sessions=Array.from({length:80},(_,i)=>({session_id:'c'+i,title:'Delegated child '+i,message_count:3,
   parent_session_id:'p0',relationship_type:'child_session',raw_source:'subagent',session_source:'other'}));
 parent._child_session_count=80;_expandedChildSessionKeys.add('p0');repaint();
 // Settle the active owner before scrolling into the tall expanded parent.
 activeSidForSidebar='p1';repaint();
 $('sessionList').scrollTop=2000;repaint();
}
function ownUnreadScene(){
 inventory(120);
 const parent=fixtureSessions[0];
 _sessionViewedCounts.p0={message_count:0,transcript_generation:0};
 parent._child_sessions=[{session_id:'c0',title:'Child awaiting approval',message_count:3,
  parent_session_id:'p0',attention:{kind:'approval',count:1},relationship_type:'child_session'}];
 parent._child_session_count=1;parent._child_session_attention={kind:'approval',count:1,title:'Waiting for permission decision'};
 _expandedChildSessionKeys.clear();repaint();
}
function boundaryState(){
 const l=$('sessionList'),row=l.querySelector('.session-item[data-sid="p0"]');
 const chip=row?.querySelector('.session-child-count'),dot=row?.querySelector(':scope > .session-state-indicator');
 return {geometry:geometry('p0'),visible:visible(),start:+l.dataset.sessionVirtualStart,end:+l.dataset.sessionVirtualEnd,
  focused:document.activeElement?.dataset.childOwner||document.activeElement.tagName,
  focusVisible:!!chip?.matches(':focus-visible'),cue:dot?{visibility:getComputedStyle(dot).visibility,opacity:getComputedStyle(dot).opacity}:null};
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--before-ref')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    def source(path):
        return (subprocess.check_output(['git', 'show', f'{args.before_ref}:{path}'], cwd=ROOT, text=True)
                if args.before_ref else (ROOT / path).read_text())

    results, errors = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(ignore_default_args=['--hide-scrollbars'])
        for width in [1280, 390]:
            context = browser.new_context(viewport={'width': width, 'height': 844})
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.route('http://fixture.test/**', lambda route: route.fulfill(content_type='text/html', body='<input id="sessionSearch" hidden><div id="sessionList" style="width:300px;height:520px;overflow:auto;background:var(--sidebar)"></div>'))
            page.goto('http://fixture.test/')
            page.add_style_tag(content=source('static/style.css'))
            page.add_script_tag(content=boundary_script(source('static/sessions.js')))
            page.add_script_tag(content=source('static/i18n.js'))
            page.evaluate('document.documentElement.dataset.skin="graphite"')
            for n in [200, 400, 800]:
                page.evaluate('(n)=>inventory(n)', n)
                page.wait_for_timeout(100)
                state = page.evaluate('''() => ({dom:$('sessionList').querySelectorAll('.session-date-body>.session-item').length,
                    viewed:Object.keys(JSON.parse(localStorage.getItem(SESSION_VIEWED_COUNTS_KEY)||'{}')),
                    writes:storageWrites.filter(w=>w.key===SESSION_VIEWED_COUNTS_KEY).length,
                    clearWrites:storageWrites.filter(w=>w.key.startsWith(SESSION_COMPLETION_UNREAD_CLEARED_PREFIX)).length,
                    units:storageWrites.reduce((sum,w)=>sum+w.units,0)})''')
                failures = []
                if len(state['viewed']) > state['dom'] or state['writes'] > state['dom']:
                    failures.append('layout persisted offscreen first-sight owners')
                if not state['writes'] or state['dom'] >= 80:
                    failures.append('rendered boundary not exercised or DOM unbounded')
                # Layout-only projection must not mutate a fresh origin at all.
                pure = page.evaluate('''() => {resetStores();_sessionVirtualLayout($('sessionList'),groups[0].items.map(session=>({session,group:groups[0]})),'','other');return storageWrites.length;}''')
                if pure:
                    failures.append('layout-only projection writes storage')
                generation = page.evaluate('''() => {
                    resetStores();const row=fixtureSessions[0];
                    row.transcript_generation=1;row.transcript_generation_baseline=1;
                    _sessionViewedCounts={p0:{message_count:3,transcript_generation:0}};
                    const projected=_hasUnreadForSession(row,false),writes=storageWrites.length;
                    const before=JSON.stringify(_sessionViewedCounts);
                    const rendered=_hasUnreadForSession(row);
                    return {projected,rendered,writes,before,record:_sessionViewedCounts.p0};
                }''')
                if (not generation['projected'] or not generation['rendered'] or generation['writes']
                        or generation['record'] != dict(message_count=1, transcript_generation=1)):
                    failures.append('read-only generation projection diverges from rendered acknowledgement')
                results.append(dict(scene='fresh-inventory', width=width, n=n, state=state, layoutWrites=pure,
                                    generation=generation, failures=failures))
            for transition in ['batch', 'own-attention']:
                page.evaluate('tallScene()')
                page.wait_for_timeout(150)
                before = page.evaluate('boundaryState()')
                assert before['geometry']['top'] < 0 < before['geometry']['bottom'], before
                assert before['geometry']['scrollTop'] == 2000, before
                page.screenshot(path=str(args.output / f'{width}-{transition}-before.png'))
                page.evaluate('toggleSessionSelectMode()' if transition == 'batch' else "fixtureSessions[0].attention={kind:'approval',count:1};repaint()")
                page.wait_for_timeout(150)
                after = page.evaluate('boundaryState()')
                page.screenshot(path=str(args.output / f'{width}-{transition}-after.png'))
                failures = []
                if after['geometry']['top'] is None or abs(after['geometry']['top'] - before['geometry']['top']) > 1:
                    failures.append('measured tall stable-order parent lost or moved')
                if after['geometry']['rows'] >= 80:
                    failures.append('unbounded DOM')
                results.append(dict(scene=transition, width=width, before=before, after=after, failures=failures))
            page.evaluate('ownUnreadScene()')
            page.wait_for_timeout(100)
            chip = page.locator('.session-item[data-sid="p0"] .session-child-count')
            chip.click()
            page.mouse.move(380, 700)
            page.wait_for_timeout(250)
            pointer = page.evaluate('boundaryState()')
            page.screenshot(path=str(args.output / f'{width}-pointer-after.png'))
            failures = []
            if pointer['cue'] is None or pointer['cue']['visibility'] != 'visible' or pointer['cue']['opacity'] == '0':
                failures.append('pointer-only chip focus hides parent cue after leave')
            results.append(dict(scene='pointer', width=width, state=pointer, failures=failures))
            chip.focus()
            keyboard = []
            for key in ['Enter', 'Space', 'Enter', 'Space']:
                page.keyboard.press(key)
                page.wait_for_timeout(50)
                keyboard.append(page.evaluate('boundaryState()'))
            results.append(dict(scene='keyboard', width=width, states=keyboard,
                                failures=[] if all(s['focused'] == 'p0' for s in keyboard) else ['consecutive keys lost owner']))
            page.evaluate("const input=document.createElement('input');input.id='unrelated';document.body.appendChild(input);input.focus();repaint()")
            assert page.locator('#unrelated').evaluate('(e)=>e===document.activeElement')
            context.close()
        browser.close()
    report = dict(results=results, errors=errors)
    (args.output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(cases=len(results), failures=sum(bool(r['failures']) for r in results), errors=errors)))
    if errors or any(r['failures'] for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
