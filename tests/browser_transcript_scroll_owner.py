#!/usr/bin/env python3
"""Independent, real-browser transcript geometry gate (no provider or user state).

Run: .venv/bin/python tests/browser_transcript_scroll_owner.py
BROWSERS=chromium,webkit (default); VIEWPORTS=desktop,narrow,mobile (default).
SCROLL_ACTIVITY_MODE=compact_worklog|transparent_stream|hide_all_activity.
SCROLL_FIXTURE=tools supports SCROLL_TOOL_TURNS=12 and SCROLL_TOOL_STEPS=55;
use 4 turns x 240 steps to exercise oversized assistant turns.
--baseline-ref REV serves immutable git versions of ui.js and sessions.js.
Artifacts, including failures and the exact source hashes, go outside the repo.
The oracle compares *content* coordinates and DOM identity, never scrollTop
alone. Synthetic data crosses the network boundary; render/scroll/load handlers
are production code. No test-only renderer or geometry-changing CSS is used.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright
from browser_conversation_lifecycle import _start_webui_server, _terminate_process
def live_fixture(count):
    tools = [dict(name='terminal', tid=f'tool-{i}', args={'command': f'printf result-{i}'},
                  preview=f'result-{i}', snippet=f'result-{i}', done=True) for i in range(count)]
    rows = [dict(row_id=f'tool:{t["tid"]}', local_id=t['tid'], role='tool', kind='tool_call',
                 source_event_type='tool_complete', status='completed', order_index=i,
                 tool=t, payload=t) for i, t in enumerate(tools)]
    return dict(stream_id='run-fixture', last_seq=1000, last_event_id='run-fixture:1000',
                messages=[], tool_calls=tools, last_assistant_text='', last_reasoning_text='',
                anchor_activity_scene=dict(version='activity_scene_v1',
                                           identity=dict(session_id='fixture', stream_id='run-fixture', run_id='run-fixture'),
                                           activity_rows=rows))

SSE_INIT = """
// A controlling service worker bypasses Playwright's page route fixtures in
// WebKit. This test targets reconnect rendering, not PWA cache behavior.
if(window===window.top && 'serviceWorker' in navigator){
  navigator.serviceWorker.register=()=>Promise.reject(new Error('Disabled in reconnect harness'));
}
window.fixtureSources=[];
class FixtureEventSource {
  static OPEN=1; static CONNECTING=0; static CLOSED=2;
  constructor(url){this.url=String(url);this.readyState=1;this.listeners={};window.fixtureSources.push(this);}
  addEventListener(name,fn){(this.listeners[name]||=[]).push(fn);}
  removeEventListener(){}
  close(){this.readyState=2;}
  emit(name,data,id){for(const fn of this.listeners[name]||[])fn({data:JSON.stringify(data),lastEventId:id||''});}
}
window.EventSource=FixtureEventSource;
"""

# Guard access itself: sandboxed preview frames prohibit navigator.serviceWorker.
INIT = "try { if (window === window.top && 'serviceWorker' in navigator) navigator.serviceWorker.register=()=>Promise.reject(new Error('Disabled in scroll harness')); } catch (_) {}\n" + SSE_INIT[SSE_INIT.index('window.fixtureSources=[];'):]

ROOT = Path(os.environ.get('WEBUI_TEST_ROOT', Path(__file__).resolve().parent.parent))
VIEWPORTS = {'desktop': (1440, 1000), 'narrow': (820, 900), 'mobile': (390, 844)}
COUNT = 380


def messages():
    source = os.environ.get('SCROLL_SESSION_FILE')
    if source:
        payload = json.loads(Path(source).read_text())
        data = payload if isinstance(payload, list) else payload.get('messages', [])
        assert len(data) >= 340, 'local fixture needs at least 340 messages'
        return [dict(m, _test_id=i) for i,m in enumerate(data)]
    if os.environ.get('SCROLL_FIXTURE') == 'tools':
        result = []
        turns=int(os.environ.get('SCROLL_TOOL_TURNS','12'))
        steps=int(os.environ.get('SCROLL_TOOL_STEPS','55'))
        assert turns>0 and steps>0, 'tool fixture dimensions must be positive'
        for turn in range(turns):
            result.append(dict(role='user', content=f'Public research request {turn}'))
            for step in range(steps):
                tid = f'public-{turn}-{step}'
                result.append(dict(role='assistant', content='', tool_calls=[dict(
                    id=tid, type='function', function=dict(name='web_search', arguments=json.dumps({'query': f'public topic {step}'})))]))
                result.append(dict(role='tool', tool_call_id=tid, name='web_search', content='Public synthetic search result. ' * 12))
            result.append(dict(role='assistant', content=f'## Research summary {turn}\n\n' + '\n\n'.join(
                f'Paragraph {i}: a visible summary of public synthetic research.' for i in range(30))))
        return [dict(m, _test_id=i, _ts=1700000000+i) for i,m in enumerate(result)]
    result = []
    for i in range(COUNT):
        # Every fourth assistant is over ten desktop viewport heights. All
        # paragraphs carry stable searchable content, including within a row.
        text = f'OWNERROW{i:04d}\n\n'
        if i % 8 == 7:
            text += '\n\n'.join(f'Paragraph {j:04d}: synthetic reading landmark {i}.' for j in range(360))
            text += '\n\n```python\n' + '\n'.join(f'print({j})' for j in range(90)) + '\n```'
            text += '\n\n| Key | Value |\n| --- | --- |\n' + '\n'.join(f'| {j} | fixture |' for j in range(30))
        else:
            text += 'Synthetic transcript context.\n\n' * (2 + i % 5)
        result.append(dict(role='assistant' if i % 2 else 'user', content=text, _test_id=i))
    return result


# The frame sampler observes real layout only. The stable id is read from the
# fixture message, not the shifting raw index or the virtualizer's height cache.
PROBE = r"""() => {
  const ids=new WeakMap();let serial=0;
  const rows=()=>Array.from(document.querySelectorAll('#msgInner [data-msg-idx]'))
    .filter(n=>!n.parentElement.closest('[data-msg-idx]'));
  window.ownerSnapshot=()=>{
    const c=document.querySelector('#messages'),v=c.getBoundingClientRect();
    const contentErrors=[];
    const rs=rows().map(n=>{
      const m=S.messages[Number(n.dataset.msgIdx)],r=n.getBoundingClientRect();
      const expected=/^## (Research summary \d+)/m.exec(typeof m?.content==='string'?m.content:'');
      const actual=n.querySelector('h2');
      if(expected&&actual&&actual.textContent!==expected[1])
        contentErrors.push({id:m._test_id,expected:expected[1],actual:actual.textContent});
      if(!ids.has(n))ids.set(n,++serial);
      return {id:m?m._test_id:null,node:ids.get(n),top:r.top-v.top,height:r.height,
        bottom:r.bottom-v.top};
    }).filter(r=>r.id!==null&&r.height>0);
    const landmarks=[];
    for(const n of rows()) {
      const nr=n.getBoundingClientRect();
      if(nr.bottom<v.top-1600||nr.top>v.bottom+1600)continue;
      const id=S.messages[Number(n.dataset.msgIdx)]?._test_id;
      Array.from(n.querySelectorAll('p,pre,tr,h1,h2,h3,li')).forEach((el,i)=>{
        const r=el.getBoundingClientRect(),top=r.top-v.top;
        if(r.bottom>=v.top-1600&&r.top<=v.bottom+1600)
          landmarks.push({id:id+':'+i,top,bottom:r.bottom-v.top});
      });
    }
    // Compact worklogs are visible siblings of hidden source segments. Observe
    // their actual clipped content, not just the indexed message containers.
    const activityNodes=Array.from(document.querySelectorAll(
      '#msgInner .tool-card-row,#msgInner .wl-reason,#msgInner .tool-worklog-summary,#msgInner .agent-activity-thinking'));
    const clippedRect=n=>{
      const r=n.getBoundingClientRect();let top=r.top,bottom=r.bottom;
      for(let p=n.parentElement;p&&p!==c;p=p.parentElement){
        if(['hidden','clip','auto','scroll'].includes(getComputedStyle(p).overflowY)){
          const pr=p.getBoundingClientRect();top=Math.max(top,pr.top);bottom=Math.min(bottom,pr.bottom);
        }
      }
      return {top:top-v.top,bottom:bottom-v.top,height:Math.max(0,bottom-top)};
    };
    for(const n of activityNodes){
      if(n.closest('[data-msg-idx]'))continue;
      const rect=clippedRect(n);if(rect.height<=0||getComputedStyle(n).visibility==='hidden')continue;
      const key=n.dataset.worklogAnchorKey;
      const source=key&&rows().find(s=>s.dataset.worklogAnchorKey===key);
      const message=source&&S.messages[Number(source.dataset.msgIdx)];
      const toolKey=n.dataset.toolDisclosureKey||n.dataset.liveTid;
      // Identity does not depend on new production anchoring helpers. Full
      // content distinguishes unindexed projections without shifting ordinals.
      const turn=n.closest('.assistant-turn');
      const first=turn?.querySelector('[data-msg-idx]');
      const turnId=first&&S.messages[Number(first.dataset.msgIdx)]?._test_id;
      const id=message?message._test_id:'activity:'+(toolKey||(turnId+':'+n.className+':'+n.textContent.trim()));
      if(!ids.has(n))ids.set(n,++serial);
      rs.push({id,node:ids.get(n),...rect});
      Array.from(n.querySelectorAll('p,pre,tr,h1,h2,h3,li')).forEach((p,i)=>{
        const r=clippedRect(p);if(r.height<=0||r.bottom < -1600||r.top>v.height+1600)return;
        landmarks.push({id:id+':'+i,top:r.top,bottom:r.bottom});
      });
    }
    rs.sort((a,b)=>a.top-b.top);
    return {time:performance.now(),wheel:window.ownerWheel||0,rows:rs,landmarks,contentErrors,
      visible:rs.filter(r=>r.bottom>2&&r.top<v.height-2).map(r=>r.id),height:v.height,top:c.scrollTop,max:c.scrollHeight-c.clientHeight,
      oldest:_oldestIdx,truncated:_messagesTruncated,session:S.session.session_id,
      selectionLength:String(getSelection()).length};
  };
  window.ownerWheel=0;window.ownerFrames=[];window.ownerSampling=false;
  document.querySelector('#messages').addEventListener('wheel',e=>{
    if(e.isTrusted)window.ownerWheel+=e.deltaY;
  },{passive:true});
  window.ownerStart=()=>{
    window.ownerFrames=[];window.ownerWheel=0;window.ownerSampling=true;
    const tick=()=>{if(!window.ownerSampling)return;
      window.ownerFrames.push(ownerSnapshot());requestAnimationFrame(tick);};tick();
  };
  window.ownerStop=()=>{window.ownerSampling=false;return window.ownerFrames;};
}"""


def movement(a, b):
    """Pixel travel of the same content, even when spacer heights change."""
    previous_landmarks = {r['id']: r for r in a.get('landmarks', []) if r['bottom'] > r['top']}
    shared_landmarks = [(previous_landmarks[r['id']], r) for r in b.get('landmarks', [])
                        if r['id'] in previous_landmarks and r['bottom'] > r['top']]
    if shared_landmarks:
        # A row can grow above the reader while its visible paragraph stays
        # stationary. Measure that paragraph, not the moving container edge.
        shared_landmarks.sort(key=lambda pair: abs(pair[0]['top']))
        old, new = shared_landmarks[0]
        return old['top'] - new['top']
    previous = {r['id']: r for r in a['rows']}
    common = [(previous[r['id']], r) for r in b['rows'] if r['id'] in previous]
    assert common, 'content discontinuity: no overlapping row to measure'
    # Prefer the content intersecting the viewport, not a far-off pinned tail.
    common.sort(key=lambda pair: min(abs(pair[0]['top']), abs(pair[0]['bottom'])))
    old, new = common[0]
    return old['top'] - new['top']


def assert_snapshot(frame):
    assert not frame.get('contentErrors'), ('wrong rendered message content', frame['contentErrors'])
    assert frame['visible'], ('blank viewport', frame)
    assert len(frame['rows']) < COUNT // 2, ('unbounded mounted transcript', len(frame['rows']))


def assert_frames(frames, direction, *, identity=True):
    assert len(frames) >= 3, 'sampler did not observe browser frames'
    assert_snapshot(frames[0])
    for a, b in zip(frames, frames[1:], strict=False):
        assert_snapshot(b)
        delta = movement(a, b)
        # Wheel dispatch/compositor presentation can straddle a sample. Two
        # 600px events of slack tolerate that, but never a tall-row teleport.
        budget = abs(b['wheel'] - a['wheel']) + 1200
        assert abs(delta) <= budget + 100, ('content teleport', delta, budget, a, b)
        assert direction * delta >= -150, ('opposite-direction content jump', delta, a, b)
        if identity:
            old = {r['id']: r['node'] for r in a['rows']}
            replaced = [r['id'] for r in b['rows'] if r['id'] in old and r['node'] != old[r['id']]]
            assert not replaced, ('retained rows recreated during window shift', replaced)
    travel = sum(movement(a, b) for a, b in zip(frames, frames[1:], strict=False))
    intended = frames[-1]['wheel'] - frames[0]['wheel']
    # Endpoint exemption is direction-aware, using actual transcript bounds.
    last = frames[-1]
    endpoint = (direction > 0 and last['max'] - last['top'] < 3) or (
        direction < 0 and last['top'] < 3 and not last['truncated'] and last['oldest'] == 0)
    assert endpoint or direction * travel >= abs(intended) * .65, (
        'input lost/stuck (loaded top is not an endpoint while older remains)', travel, intended, last)
    return dict(frames=len(frames), travel=travel, intended=intended)


def oracle_self_test():
    def sample(top=0, wheel=0, **extra):
        return dict(rows=[dict(id=7,node=1,top=top,bottom=top+20000,height=20000)],
                    visible=[7], wheel=wheel, top=100, max=100, height=800,
                    truncated=True, oldest=50, **extra)
    for name, frames, direction in [
        ('8k backwards within tall row', [sample(), sample(8000,600), sample(8000,1200)], 1),
        ('13k forwards within tall row', [sample(), sample(-13000,600), sample(-13000,1200)], 1),
        ('up stuck at bottom', [sample(), sample(0,-600), sample(0,-1200)], -1),
    ]:
        try:
            assert_frames(frames, direction)
        except AssertionError:
            continue
        raise AssertionError('oracle accepted ' + name)
    print('PASS oracle self-test: tall-row jumps and wrong-direction endpoint rejected', flush=True)


def production_message_window():
    """Load the real pure pagination helpers without importing server state."""
    namespace = {}
    wanted = {
        'api/models.py': {'_is_empty_partial_activity_message'},
        'api/routes.py': {'_message_counts_as_renderable_for_window',
                          '_tool_call_ids_in_messages', '_tool_result_matches_call_ids',
                          '_message_window_for_display'},
    }
    for filename, names in wanted.items():
        tree = ast.parse((ROOT / filename).read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        assert {node.name for node in nodes} == names, f'pagination helper contract changed: {filename}'
        exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec'), namespace)
    return namespace['_message_window_for_display']


class Transport:
    def __init__(self, workspace):
        self.window = production_message_window()
        self.data = messages()
        self.workspace = workspace
        self.streaming = False
        self.count = len(self.data)
        self.hold = False
        self.pending = []
        self.requests = []

    def route(self, route):
        q = parse_qs(urlsplit(route.request.url).query)
        sid = q.get('session_id', [''])[0]
        limit = int(q.get('msg_limit', [50])[0])
        end = int(q.get('msg_before', [self.count])[0])
        start = max(0, end-limit)
        if sid == 'other':
            data = [dict(role='assistant',content='OTHER SESSION SENTINEL',_test_id=999)]
            start, end = 0, 1
        else:
            data, start = self.window(self.data, msg_limit=limit, msg_before=end)
            end = start + len(data)
        payload = dict(session_id=sid, title='Public synthetic scroll fixture', model='',
                       workspace=self.workspace, messages=data, message_count=self.count if sid != 'other' else 1,
                       tool_calls=[], _messages_truncated=start>0, _messages_offset=start)
        if self.streaming and sid == 'fixture':
            payload.update(active_stream_id='run-fixture',pending_user_message='Synthetic stream',
                           pending_started_at=time.time(),runtime_journal_snapshot=live_fixture(2))
        self.requests.append(dict(sid=sid,start=start,end=end,limit=limit))
        if self.hold and sid == 'fixture':
            self.pending.append((route, payload))
        else:
            route.fulfill(json={'session':payload})

    def release(self):
        assert self.pending, 'delayed history request never reached transport'
        self.hold = False
        pending, self.pending = self.pending, []
        for route, payload in pending:
            route.fulfill(json={'session':payload})


def wait(page, expression, timeout=20):
    deadline = time.monotonic()+timeout
    while not page.evaluate(expression):
        assert time.monotonic()<deadline, 'timeout: '+expression
        page.wait_for_timeout(40)


def snapshot(page):
    return page.evaluate('ownerSnapshot()')


def idle(page):
    page.wait_for_timeout(300)
    before = snapshot(page)
    page.wait_for_timeout(1500)
    after = snapshot(page)
    assert abs(movement(before,after)) <= 3, ('delayed idle snap', before,after)
    assert_snapshot(after)


def pump(page, direction, count=30):
    box = page.locator('#messages').bounding_box()
    page.mouse.move(box['x']+box['width']*.55,box['y']+box['height']*.5)
    page.evaluate('ownerStart()')
    # Sampling is independent rAF, not one expensive Python/DOM round trip per
    # input event. Wheel events are trusted browser input, never dispatchEvent.
    for _ in range(count):
        page.mouse.wheel(0,direction*600)
        page.wait_for_timeout(35)
    page.wait_for_timeout(120)
    return page.evaluate('ownerStop()')


def position(page):
    # Setup only: place well inside a mounted ten-viewport-height assistant.
    result=page.evaluate("""() => {
      const c=document.querySelector('#messages');
      const candidates=Array.from(document.querySelectorAll('#msgInner [data-msg-idx]'));
      const row=candidates.find(n=>n.getBoundingClientRect().height>10000);
      if(!row)return null;
      c.scrollTop+=row.getBoundingClientRect().top-c.getBoundingClientRect().top+3500;
      return {id:S.messages[Number(row.dataset.msgIdx)]._test_id,height:row.getBoundingClientRect().height};
    }""")
    assert result, 'fixture must mount a row at least 10,000px tall'
    page.wait_for_timeout(400)
    return result


def continuity_case(page, transport, evidence):
    position(page)
    # Select real text inside the tall row; window updates must not clear it.
    page.evaluate("""() => {
      const s=ownerSnapshot(),id=s.visible[0];
      const row=Array.from(document.querySelectorAll('#msgInner [data-msg-idx]'))
        .find(n=>S.messages[Number(n.dataset.msgIdx)]._test_id===id);
      const walker=document.createTreeWalker(row,NodeFilter.SHOW_TEXT);
      let n;while((n=walker.nextNode()))if(n.textContent.trim().length>20)break;
      const r=document.createRange();r.setStart(n,0);r.setEnd(n,20);
      const sel=getSelection();sel.removeAllRanges();sel.addRange(r);
      window.ownerSelection=String(sel);
    }""")
    first = pump(page,-1,3)
    evidence['up']=first
    assert_frames(first,-1,identity=False)
    # Selection retention has its own independent case; keep travel running.
    idle(page)
    for name, direction in [('down',1),('reverse',-1),('down-again',1)]:
        frames = pump(page,direction,40)
        evidence[name]=frames
        assert_frames(frames,direction,identity=False)
        idle(page)


def prepend_case(page, transport, evidence, variant):
    position(page)
    transport.hold=True
    page.evaluate('void (window.ownerPending=_loadOlderMessages())')
    deadline=time.monotonic()+5
    while not transport.pending:
        assert time.monotonic()<deadline, 'history request not issued'
        page.wait_for_timeout(30)
    if variant=='switch':
        page.evaluate("async()=>await loadSession('other')")
        before=snapshot(page)
        transport.release()
        page.evaluate('async()=>await ownerPending')
        page.wait_for_timeout(500)
        after=snapshot(page)
        assert after['session']=='other' and [r['id'] for r in after['rows']]==[999], after
    else:
        if variant=='input':
            evidence['input']=pump(page,1,5)
        before=snapshot(page)
        transport.release()
        page.evaluate('async()=>await ownerPending')
        page.wait_for_timeout(100)
        after=snapshot(page)
        assert after['oldest']<before['oldest'], ('older data not prepended',before,after,transport.requests)
        assert abs(movement(before,after))<=4, ('prepend moved current content',before,after)
        assert len(after['rows'])<COUNT//2, 'prepend mounted whole history'
    evidence.update(before=before,after=after,requests=transport.requests)
    idle(page)


def image_case(page, transport, evidence):
    position(page)
    # A real decoded image expands above the currently visible paragraph in the
    # same mounted row. Inserting a pending img is fixture setup, not a repair.
    page.evaluate("""() => {
      const s=ownerSnapshot(),id=s.visible[0];
      const row=Array.from(document.querySelectorAll('#msgInner [data-msg-idx]'))
        .find(n=>S.messages[Number(n.dataset.msgIdx)]._test_id===id);
      const body=row.querySelector('.msg-text,.assistant-text,.markdown-body')||row;
      const img=document.createElement('img');img.id='ownerDelayedImage';body.prepend(img);
      const ps=Array.from(row.querySelectorAll('p'));
      window.ownerLandmark=ps.find(p=>p.getBoundingClientRect().top>document.querySelector('#messages').getBoundingClientRect().top);
      if(!ownerLandmark)throw Error('missing image content landmark');
    }""")
    # Let insertion of the still-empty image participate in normal layout
    # before measuring the later decode; these are two separate mutations.
    page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    before=page.evaluate('ownerLandmark.getBoundingClientRect().top')
    page.evaluate("""() => {ownerDelayedImage.src='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="900"><rect width="300" height="900" fill="teal"/></svg>');}""")
    wait(page,'ownerDelayedImage.complete && ownerDelayedImage.naturalHeight===900')
    page.wait_for_timeout(500)
    after=page.evaluate('ownerLandmark.getBoundingClientRect().top')
    evidence.update(before=before,after=after)
    assert abs(after-before)<=4, ('late image above moved reading landmark',before,after)
    idle(page)


def identity_case(page, transport, evidence):
    position(page)
    page.evaluate("""() => {
      const v=ownerSnapshot().visible[0];
      const row=Array.from(document.querySelectorAll('#msgInner [data-msg-idx]'))
        .find(n=>S.messages[Number(n.dataset.msgIdx)]._test_id===v);
      const walker=document.createTreeWalker(row,NodeFilter.SHOW_TEXT);
      let n;while((n=walker.nextNode()))if(n.textContent.trim().length>20)break;
      const r=document.createRange();r.setStart(n,0);r.setEnd(n,20);
      getSelection().removeAllRanges();getSelection().addRange(r);
      window.ownerSelection=String(getSelection());
    }""")
    frames=pump(page,1,3)
    evidence['selection_frames']=frames
    assert_frames(frames,1)
    assert page.evaluate('String(getSelection())===ownerSelection'), 'selection lost while row remained mounted'
    # Then cross several short/tall rows; only identities common to successive
    # windows must survive. Offscreen eviction is allowed.
    frames=pump(page,-1,45)
    evidence['shift_frames']=frames
    assert_frames(frames,-1)
    memberships={tuple(r['id'] for r in f['rows']) for f in frames}
    assert len(memberships)>1, 'identity gate did not cross a mounted window boundary'


def cold_case(page, transport, evidence):
    position(page)
    idle(page)
    before=snapshot(page)
    page.evaluate("async()=>await loadSession('other')")
    page.evaluate("async()=>await loadSession('fixture')")
    for _ in range(10):
        if page.evaluate('S.messages.length>=300'):break
        page.evaluate('async()=>await _loadOlderMessages()')
    position(page)
    idle(page)
    after=snapshot(page)
    old={r['id']:r['height'] for r in before['rows']}
    shared=[r for r in after['rows'] if r['id'] in old]
    evidence.update(before=before,after=after)
    assert shared, 'cold rerender has no common geometry'
    differences=[(r['id'],old[r['id']],r['height']) for r in shared if abs(old[r['id']]-r['height'])>2]
    assert not differences, ('cold/warm row geometry mismatch',differences)
    assert page.locator('#msgInner pre').count()>0, 'mixed code not rendered'
    assert page.locator('#msgInner table').count()>0, 'mixed table not rendered'


def stream_case(page, transport, evidence):
    position(page)
    wait(page,"fixtureSources.some(s=>s.url.includes('/api/chat/stream?')&&s.readyState===1)")
    before=snapshot(page)
    page.evaluate("""() => {
      const source=fixtureSources.findLast(s=>s.url.includes('/api/chat/stream?')&&s.readyState===1);
      source.emit('token',{text:'## Synthetic streamed answer\\n\\n'+'New material below the reading position. '.repeat(300)},'run-fixture:1001');
    }""")
    page.wait_for_timeout(500)
    after=snapshot(page)
    evidence.update(before=before,after=after)
    assert abs(movement(before,after))<=4, ('stream stole reading position',before,after)
    assert page.evaluate("document.querySelector('#liveAssistantTurn').textContent.includes('Synthetic streamed answer')"), 'stream did not render'
    idle(page)
    frames=pump(page,-1,8)
    evidence['scroll_frames']=frames
    assert_frames(frames,-1,identity=False)


def natural_case(page, transport, evidence):
    """Cold tail to older history using trusted input, no forced tall-row setup."""
    evidence['initial']=snapshot(page)
    assert_snapshot(evidence['initial'])
    for label, direction in [('up',-1),('down',1),('reverse',-1)]:
        frames=pump(page,direction,int(os.environ.get('SCROLL_WHEEL_COUNT','80')))
        evidence[label]=frames
        assert_frames(frames,direction,identity=False)
        idle(page)
    evidence['requests']=transport.requests


def activity_case(page, transport, evidence):
    """Prepend while the viewport contains only projected Worklog reasoning."""
    page.evaluate("""() => {
      const reason=Array.from($('msgInner').querySelectorAll('.wl-reason'))
        .find(n=>n.textContent.includes('Activity landmark 200'));
      const group=reason?.closest('.tool-worklog-group');
      if(!group)throw Error('fixture did not project activity reasoning');
      if(group.classList.contains('tool-call-group-collapsed'))
        group.querySelector('.tool-worklog-summary').click();
    }""")
    page.wait_for_timeout(300)
    box=page.locator('#messages').bounding_box()
    page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2)
    page.mouse.wheel(0,-600)
    page.wait_for_timeout(300)
    evidence['setup']=page.evaluate("""() => {
      const c=$('messages');
      const reasons=Array.from($('msgInner').querySelectorAll('.wl-reason'));
      const reason=reasons.find(n=>n.textContent.includes('Activity landmark 200'));
      if(!reason)throw Error('fixture did not project activity reasoning');
      const group=reason.closest('.tool-worklog-group');
      if(group.classList.contains('tool-call-group-collapsed'))
        group.querySelector('.tool-worklog-summary').click();
      const mark=Array.from(reason.querySelectorAll('p')).find(p=>p.textContent.startsWith('Activity landmark 200:'));
      if(!mark)throw Error('missing activity landmark');
      c.scrollTop+=mark.getBoundingClientRect().top-c.getBoundingClientRect().top-100;
      const offset=mark.getBoundingClientRect().top-c.getBoundingClientRect().top;
      const visibleSegments=Array.from($('msgInner').querySelectorAll('[data-msg-idx]')).filter(n=>{
        const b=n.getBoundingClientRect(),r=c.getBoundingClientRect();return b.height>0&&b.bottom>r.top&&b.top<r.bottom;}).length;
      const seen=ownerSnapshot().visible.length;
      reason.style.visibility='hidden';
      const hidden=ownerSnapshot().visible.length;
      reason.style.visibility='';
      return {offset,visibleSegments,oldest:_oldestIdx,seen,hidden};
    }""")
    # Capture the placed landmark immediately; a subsequent window/measurement
    # write is part of the behavior under test, not fixture setup to hide.
    state="""() => {
      const c=$('messages'),r=c.getBoundingClientRect();
      const mark=Array.from($('msgInner').querySelectorAll('.wl-reason p')).find(p=>p.textContent.startsWith('Activity landmark 200:'));
      const segments=Array.from($('msgInner').querySelectorAll('[data-msg-idx]')).filter(n=>{
        const b=n.getBoundingClientRect();return b.height>0&&b.bottom>r.top&&b.top<r.bottom;});
      return {offset:mark?.getBoundingClientRect().top-r.top,visibleSegments:segments.length,oldest:_oldestIdx};
    }"""
    before=evidence['setup']
    assert before['visibleSegments']==0 and 0<before['offset']<200, ('activity-only setup failed',before)
    assert before['seen'] and not before['hidden'], ('activity visibility mutation bite failed',before)
    page.evaluate('async()=>await _loadOlderMessages()')
    after=page.evaluate(state)
    evidence.update(before=before,after=after)
    assert after['oldest']<before['oldest'], 'history prepend did not occur'
    assert after['offset'] is not None and abs(after['offset']-before['offset'])<=4, ('activity prepend drift',before,after)
    idle(page)


def disclosure_case(page, transport, evidence):
    position(page)
    page.evaluate("""() => {
      const cards=Array.from(document.querySelectorAll('#msgInner .thinking-card'));
      if(!cards.length)throw Error('fixture did not render thinking disclosure');
      const card=cards[cards.length-1];
      _setWorklogDetailDisclosureOpen(card,true);
      const body=_worklogDetailScrollableBody(card);
      window.ownerDisclosureKey=_worklogDetailBaseKey(card);
      body.scrollTop=120;
      window.ownerDisclosureOffset=body.scrollTop;
      if(ownerDisclosureOffset<100)throw Error('disclosure not scrollable');
    }""")
    page.evaluate('async()=>await _loadOlderMessages()')
    evidence['state']=page.evaluate("""() => {
      const card=Array.from(document.querySelectorAll('#msgInner .thinking-card')).find(n=>_worklogDetailBaseKey(n)===ownerDisclosureKey);
      return {exists:!!card,open:card?.classList.contains('open'),before:ownerDisclosureOffset,
        after:card?_worklogDetailScrollableBody(card).scrollTop:null};
    }""")
    state=evidence['state']
    assert state['exists'] and state['open'] and abs(state['after']-state['before'])<2, state


def cache_case(page, transport, evidence):
    # Revisit the exact same paginated projection so the real HTML cache can hit.
    page.evaluate("async()=>await loadSession('other')")
    page.evaluate("async()=>await loadSession('fixture')")
    page.wait_for_timeout(400)
    page.evaluate("""() => {
      window.ownerCacheHits=0;
      const original=_sessionHtmlCache.get.bind(_sessionHtmlCache);
      _sessionHtmlCache.get=key=>{const value=original(key);if(key==='fixture'&&value)ownerCacheHits++;return value;};
    }""")
    page.evaluate("async()=>await loadSession('other')")
    page.evaluate("async()=>await loadSession('fixture')")
    page.wait_for_timeout(400)
    evidence['state']=page.evaluate("""() => ({hits:ownerCacheHits,
      sid:S.session.session_id,stamp:$('msgInner').dataset.windowSession,
      observed:_messageWindowObserved?.sid,anchor:!!_messageWindowSnapshot()})""")
    assert evidence['state']['hits']>0, 'cache path was not exercised'
    assert evidence['state']['stamp']==evidence['state']['sid']=='fixture', evidence
    assert evidence['state']['observed']=='fixture' and evidence['state']['anchor'], evidence
    before=snapshot(page)
    page.evaluate('async()=>await _loadOlderMessages()')
    after=snapshot(page)
    assert abs(movement(before,after))<=4, ('cache re-entry prepend drift',before,after)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref')
    parser.add_argument('--artifacts',default='/tmp/hermes-scroll-evidence')
    parser.add_argument('--cases',default='continuity,identity,prepend,input,switch,image,cold,stream')
    args=parser.parse_args()
    activity_mode=os.environ.get('SCROLL_ACTIVITY_MODE','compact_worklog')
    if activity_mode not in ('compact_worklog','transparent_stream','hide_all_activity'):
        raise ValueError('invalid SCROLL_ACTIVITY_MODE: '+activity_mode)
    oracle_self_test()
    artifact=Path(args.artifacts)/('baseline' if args.baseline_ref else 'candidate')
    artifact.mkdir(parents=True,exist_ok=True)
    sources={}
    for name in ('ui.js','sessions.js'):
        sources[name]=subprocess.check_output(['git','show',f'{args.baseline_ref}:static/{name}'],cwd=ROOT) if args.baseline_ref else (ROOT/'static'/name).read_bytes()
    provenance=dict(ref=args.baseline_ref,head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                    activity_mode=activity_mode,
                    fixture=os.environ.get('SCROLL_FIXTURE','text'),
                    tool_turns=os.environ.get('SCROLL_TOOL_TURNS','12'),
                    tool_steps=os.environ.get('SCROLL_TOOL_STEPS','55'),
                    source_sha256={k:hashlib.sha256(v).hexdigest() for k,v in sources.items()})
    (artifact/'provenance.json').write_text(json.dumps(provenance,indent=2))
    results=[]
    with tempfile.TemporaryDirectory(prefix='hermes-scroll-owner-') as temp:
        state=Path(temp)
        env={k:os.environ[k] for k in ('PATH','SYSTEMROOT','TMPDIR') if k in os.environ}
        env.update(HOME=temp,HERMES_HOME=temp,HERMES_BASE_HOME=temp,
                   HERMES_WEBUI_STATE_DIR=str(state/'webui'),HERMES_CONFIG_PATH=str(state/'config.yaml'),
                   HERMES_WEBUI_HOST='127.0.0.1',HERMES_WEBUI_SKIP_ONBOARDING='1',
                   HERMES_WEBUI_AGENT_DIR=str(state/'no-agent'),HERMES_WEBUI_DEFAULT_WORKSPACE=temp)
        proc,log,_,base=_start_webui_server(ROOT,env,artifact)
        try:
            with sync_playwright() as pw:
                for engine in os.environ.get('BROWSERS','chromium,webkit').split(','):
                    browser=getattr(pw,engine).launch(headless=True)
                    try:
                        for size in os.environ.get('VIEWPORTS','desktop,narrow,mobile').split(','):
                            width,height=VIEWPORTS[size]
                            for case in args.cases.split(','):
                                key=f'{engine}-{size}-{case}'
                                context=browser.new_context(viewport=dict(width=width,height=height),bypass_csp=True)
                                context.add_init_script(INIT)
                                # Local private-fixture runs must not fetch transcript media
                                # from the public network. Existing DOM rendering remains real.
                                if os.environ.get('SCROLL_SESSION_FILE'):
                                    context.route('**/*', lambda r: r.continue_() if urlsplit(r.request.url).hostname in ('127.0.0.1','localhost') else r.abort())
                                page=context.new_page()
                                errors=[]
                                page.on('pageerror',lambda e, errors=errors:errors.append(str(e)))
                                transport=Transport(temp)
                                transport.streaming = case == 'stream'
                                if case=='activity':
                                    m=next(m for m in reversed(transport.data) if m.get('role')=='assistant' and m.get('tool_calls'))
                                    m['content']='\n\n'.join(f'Activity landmark {n}: public synthetic research reasoning.' for n in range(350))
                                if case=='disclosure':
                                    m=next(m for m in reversed(transport.data) if m.get('role')=='assistant')
                                    m['content']='<think>'+'\n'.join(f'Public reasoning line {n}' for n in range(250))+'</think>\n\n'+m['content']
                                page.route('**/api/chat/stream/status?*',lambda r:r.fulfill(json={'active':True}))
                                page.route('**/api/session?*',transport.route)
                                # Freeze both candidate and baseline JS for the entire
                                # run: concurrent edits cannot create hybrid evidence.
                                for name,body in sources.items():
                                    page.route(f'**/static/{name}*',lambda r,request,body=body:r.fulfill(body=body,content_type='application/javascript'))
                                evidence={}
                                try:
                                    page.goto(base,wait_until='load')
                                    wait(page,"typeof loadSession==='function' && S._bootReady===true")
                                    page.evaluate("""mode=>{window._virtualizeTranscript=true;
                                      window._sessionEndlessScrollEnabled=true;
                                      window._chatActivityDisplayMode=mode;
                                      window._transparentStream=mode==='transparent_stream';}
                                    """,activity_mode)
                                    assert page.evaluate("chatActivityMode()") == activity_mode
                                    if case=='disclosure':page.evaluate("window._simplifiedToolCalling=false;window._showThinking=true")
                                    page.evaluate("async()=>await loadSession('fixture')")
                                    # Grow through genuine cold paginated responses, never assign S.messages.
                                    for _ in range(0 if case=='natural' else 10):
                                        if page.evaluate('S.messages.length>=300'): break
                                        page.evaluate('async()=>await _loadOlderMessages()')
                                    page.wait_for_timeout(600)
                                    page.evaluate(PROBE)
                                    assert page.evaluate('S.messages.length>0' if case=='natural' else 'S.messages.length>=300'), 'missing fixture history'
                                    if case=='continuity':continuity_case(page,transport,evidence)
                                    elif case in ('prepend','input','switch'):prepend_case(page,transport,evidence,case)
                                    elif case=='image':image_case(page,transport,evidence)
                                    elif case=='identity':identity_case(page,transport,evidence)
                                    elif case=='cold':cold_case(page,transport,evidence)
                                    elif case=='stream':stream_case(page,transport,evidence)
                                    elif case=='natural':natural_case(page,transport,evidence)
                                    elif case=='cache':cache_case(page,transport,evidence)
                                    elif case=='disclosure':disclosure_case(page,transport,evidence)
                                    elif case=='activity':activity_case(page,transport,evidence)
                                    else:raise ValueError('unknown case: '+case)
                                    assert not errors, errors
                                    results.append(dict(test=key,status='PASS'))
                                except Exception as exc:
                                    results.append(dict(test=key,status='FAIL',error=str(exc)))
                                    evidence['error']=str(exc)
                                finally:
                                    evidence['browser_errors']=errors
                                    try:
                                        evidence['final']=snapshot(page)
                                        if not os.environ.get('SCROLL_SESSION_FILE'):
                                            page.screenshot(path=str(artifact/(key+'.png')))
                                    except Exception as exc:evidence['capture_error']=str(exc)
                                    (artifact/(key+'.json')).write_text(json.dumps(evidence,indent=2))
                                    console_result=results[-1]
                                    if os.environ.get('SCROLL_SESSION_FILE') and console_result['status']=='FAIL':
                                        # Assertions include painted row text. Keep full evidence
                                        # local, never echo a private transcript into job notices.
                                        console_result=dict(test=key,status='FAIL',error='Private fixture failure; inspect local artifacts.')
                                    print(json.dumps(console_result),flush=True)
                                    context.close()
                    finally:browser.close()
        finally:
            _terminate_process(proc)
            log.close()
    (artifact/'results.json').write_text(json.dumps(dict(provenance=provenance,results=results),indent=2))
    failed=sum(r['status']=='FAIL' for r in results)
    print(f'{len(results)-failed}/{len(results)} passed; artifacts: {artifact}',flush=True)
    return int(bool(failed))


if __name__=='__main__':
    raise SystemExit(main())
