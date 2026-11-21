"""Bounded real sidebar refresh + forced-layout timing, without app/server state.

Fixture generation and synthetic unread/viewed-store setup are excluded. Production
child projection, grouping/window/row construction and layout are included. This
is component timing, not full-app latency; API/filtering/stream transport excluded.
"""
import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.browser_lineage_scroll_selection import ROOT, script  # noqa: E402
from tests.browser_session_virtual_geometry import geometry_script  # noqa: E402

FIXTURE = r"""
function prepare(count, kind){
 activeSidForSidebar='other';window._sidebarDensity=kind==='compact'?'compact':'detailed';
 fixtureSessions=[];
 for(let i=0;i<count;i++){
  const summary=kind==='all'||(kind==='sparse'&&i===0);
  fixtureSessions.push({session_id:'p'+i,title:'Conversation '+i,message_count:3,
   updated_at:count-i,_compression_segment_count:summary?4:0,
   _lineage_segments:summary?Array.from({length:4},(_,j)=>({session_id:'prior'+i+'-'+j,title:'Earlier turn '+j,updated_at:j})):[]});
  if(summary)fixtureSessions.push({session_id:'c'+i,title:'Child '+i,message_count:3,
   parent_session_id:'p'+i,relationship_type:'child_session',raw_source:'subagent',session_source:'other',
   attention:{kind:'approval',count:1}});
 }
 const viewed=Object.fromEntries(fixtureSessions.map(s=>[s.session_id,{message_count:3,transcript_generation:0}]));
 _getSessionViewedCounts=()=>viewed;_hasSessionCompletionUnread=()=>false;
 window.parents=fixtureSessions.filter(s=>s.session_id.startsWith('p'));
 document.querySelector('#sessionList').scrollTop=0;
 delete document.querySelector('#sessionList')._sessionVirtualLayout;
}
function refresh(){
 groups=[{label:'Today',items:_attachChildSessionsToSidebarRows(parents,fixtureSessions,fixtureSessions)}];
 repaint();
}
function sample(){
 const start=performance.now();refresh();const rendered=performance.now();
 const list=document.querySelector('#sessionList');const height=list.scrollHeight;
 const end=performance.now();
 return {refreshMs:rendered-start,layoutMs:end-rendered,totalMs:end-start,height,
  rows:list.querySelectorAll('.session-date-body>.session-item').length,
  nodes:list.querySelectorAll('*').length};
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--before-ref')
    parser.add_argument('--projects', action='store_true', help='Include 20 production project controls and six date headers')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    def source(path):
        if args.before_ref:
            return subprocess.check_output(['git', 'show', f'{args.before_ref}:{path}'], cwd=ROOT, text=True)
        return (ROOT / path).read_text()

    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        page.set_content('<div id="sessionList" style="width:300px;height:520px;overflow:auto"></div>')
        page.add_style_tag(content=source('static/style.css'))
        page.add_script_tag(content=(geometry_script if args.projects else script)(source('static/sessions.js')))
        page.add_script_tag(content=source('static/i18n.js'))
        fixture = FIXTURE
        if args.projects:
            fixture = fixture.replace(" groups=[{label:'Today',items:_attachChildSessionsToSidebarRows(parents,fixtureSessions,fixtureSessions)}];",
                                      " const rows=_attachChildSessionsToSidebarRows(parents,fixtureSessions,fixtureSessions);"
                                      "groups=Array.from({length:6},(_,i)=>({label:'Date '+i,items:rows.slice(Math.floor(i*rows.length/6),Math.floor((i+1)*rows.length/6))}));")
        page.add_script_tag(content=fixture)
        if args.projects:
            page.evaluate('projectScene();')
        page.evaluate('document.documentElement.dataset.skin="graphite"')
        for count in [500, 2000]:
            for kind in ['sparse', 'all', 'plain', 'compact']:
                page.evaluate('([n,k])=>prepare(n,k)', [count, kind])
                cold = page.evaluate('sample()')
                samples = [page.evaluate('sample()') for _ in range(5)]
                result = dict(count=count, kind=kind, cold=cold, samples=samples,
                              medianMs=statistics.median(s['totalMs'] for s in samples))
                results.append(result)
                print(json.dumps(result))
                if not args.before_ref:
                    assert all(s['rows'] < 80 for s in samples), 'Sidebar DOM must stay bounded'
        browser.close()
    (args.output / 'report.json').write_text(json.dumps(dict(ref=args.before_ref, results=results), indent=2))


if __name__ == '__main__':
    main()
