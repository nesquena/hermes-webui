#!/usr/bin/env python3
"""Real browser: the first settled reply frame must be visible (#7676).

Mirrors the verification the maintainer asked for on #7676 — a REAL browser,
`chat_activity_display_mode=transparent_stream`, NO network interruption — and
asserts in the settle frame, again on the next animation frame, 1.5s later, and
after the sidebar idle-state reconcile probe:

  1. exactly one visible node owns the final answer,
  2. no visible turn depends on a hidden `data-live-assistant` segment,
  3. `#liveAssistantTurn` no longer owns the settled reply,
  4. the transcript is never blank while it holds messages,
  5. no uncaught page error.

Matrix (virtualization on/off × worklog default expanded/closed × idle probe
none/equal-count/changed-count) covers both virtualization settings and both
idle-reconcile outcomes from the report.

It also runs the two #7676 runtime helpers against a REAL DOM inside the page:

  * `_normalizeTransferredLiveProse` reveals the retained final prose when no
    visible scene row owns it, and leaves it hidden when one does;
  * `_finalizeJustSettledTransparentScene` exists and refuses to claim a
    finalize for an unknown stream (so the full-rebuild fallback stays).

Run: python tests/browser_settle_visibility_7676.py
     SETTLE_VARIANTS="1" python tests/browser_settle_visibility_7676.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

from browser_conversation_lifecycle import _start_webui_server, _terminate_process

ROOT = Path(os.environ.get('WEBUI_TEST_ROOT', Path(__file__).resolve().parent.parent))

FINAL_TEXT = (
    "FINAL_ANSWER_MARKER Here is the complete answer you asked for. "
    + "It contains a long, detailed explanation of every step taken. " * 40
)

INIT = """
if(window===window.top && 'serviceWorker' in navigator){
  navigator.serviceWorker.register=()=>Promise.reject(new Error('Disabled in settle harness'));
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

ASSERTIONS = """
async () => {
  const marker = 'FINAL_ANSWER_MARKER';
  const visible = (el) => {
    if (!el) return false;
    if (el.hidden) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.height > 0;
  };
  const markerOwners = [];          // nodes whose own text carries the final answer
  const walker = document.createTreeWalker(document.getElementById('msgInner'), NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    if ((n.textContent || '').includes(marker)) markerOwners.push(n.parentElement);
  }
  const visibleOwners = markerOwners.filter(visible);
  const live = document.getElementById('liveAssistantTurn');
  const turns = [...document.querySelectorAll('#msgInner .assistant-turn')];
  const hiddenLiveOnlyTurns = turns.filter(t => {
    if (!visible(t)) return false;
    const segs = [...t.querySelectorAll('[data-live-assistant="1"]')];
    const textInTurn = (t.textContent || '').includes(marker);
    if (!textInTurn) return false;
    const hasVisibleNonLive = [...t.querySelectorAll('.assistant-segment,.transparent-event-row')]
      .some(el => visible(el) && (el.textContent || '').includes(marker));
    return !hasVisibleNonLive && segs.length > 0;
  });
  const liveOwnsSettled = !!(live && (live.textContent || '').includes(marker));
  const hiddenOwners = markerOwners.filter(el => !visible(el));
  return {
    ownerCount: markerOwners.length,
    visibleOwnerCount: visibleOwners.length,
    hiddenOwnerCount: hiddenOwners.length,
    hiddenOwnerClasses: hiddenOwners.slice(0, 6).map(el => `${el.className || ''}|hidden=${el.hidden}|h=${Math.round(el.getBoundingClientRect().height)}`),
    liveTurnPresent: !!live,
    liveOwnsSettled,
    hiddenLiveOnlyTurnCount: hiddenLiveOnlyTurns.length,
    turnCount: turns.length,
    msgCount: (S.messages || []).length,
    busy: S.busy,
    activeStreamId: S.activeStreamId,
    transRows: document.querySelectorAll('#msgInner .transparent-event-row').length,
    settleOwnerAttr: document.querySelectorAll('[data-anchor-settled-scene-owner="1"]').length,
    scrollable: (() => { const e = document.scrollingElement || document.documentElement;
      return { st: e.scrollTop, max: e.scrollHeight - e.clientHeight }; })(),
  };
}
"""

# Real-DOM checks of the #7676 helpers (these functions did not exist before the
# fix, so a missing function fails the run).
RUNTIME_CHECKS = """
() => {
  const out = { normalize: null, finalize: null };
  try {
    if (typeof _normalizeTransferredLiveProse !== 'function') {
      out.normalize = '_normalizeTransferredLiveProse is missing';
    } else {
      const answer = 'The final answer is 42, verified against the fixture.';
      const mkSeg = () => {
        const el = document.createElement('div');
        el.className = 'assistant-segment assistant-segment-worklog-source';
        el.setAttribute('data-live-assistant', '1');
        el.setAttribute('aria-hidden', 'true');
        el.hidden = true;
        el.textContent = answer;
        return el;
      };
      const mkTurn = () => {
        const t = document.createElement('div');
        t.className = 'assistant-turn';
        return t;
      };
      // Case A: no scene row left → reveal the retained prose.
      const tA = mkTurn(); const segA = mkSeg(); tA.appendChild(segA);
      document.body.appendChild(tA);
      _normalizeTransferredLiveProse(tA);
      const revealed = !segA.hidden && segA.getAttribute('aria-hidden') !== 'true'
        && !segA.classList.contains('assistant-segment-worklog-source')
        && segA.getBoundingClientRect().height > 0;
      // Case B: a visible scene row owns the prose → keep it hidden.
      const tB = mkTurn(); const segB = mkSeg(); tB.appendChild(segB);
      const rowB = document.createElement('div');
      rowB.className = 'transparent-event-row';
      rowB.setAttribute('data-anchor-scene-row', '1');
      rowB.textContent = 'Working… ' + answer;
      tB.appendChild(rowB);
      document.body.appendChild(tB);
      _normalizeTransferredLiveProse(tB);
      const kept = segB.hidden && segB.getAttribute('aria-hidden') === 'true'
        && segB.classList.contains('assistant-segment-worklog-source');
      tA.remove(); tB.remove();
      out.normalize = (revealed && kept) ? 'ok'
        : `revealed=${revealed} kept=${kept}`;
    }
  } catch (e) { out.normalize = 'ERR:' + e.message; }
  try {
    if (typeof _finalizeJustSettledTransparentScene !== 'function') {
      out.finalize = '_finalizeJustSettledTransparentScene is missing';
    } else {
      const unknown = _finalizeJustSettledTransparentScene('no-such-stream');
      out.finalize = unknown === false ? 'ok' : `unknown-stream returned ${unknown}`;
    }
  } catch (e) { out.finalize = 'ERR:' + e.message; }
  return out;
}
"""


def make_history(n):
    msgs = []
    for i in range(n // 2):
        msgs.append({'role': 'user', 'content': f'Question number {i}: please explain step {i}.'})
        msgs.append({'role': 'assistant',
                     'content': f'Answer number {i}: this is the historical reply for step {i}. '
                                + 'It has a few sentences of body text. ' * 6})
    return msgs


def session_payload(sid, workspace, history):
    return dict(
        session_id=sid,
        title='Settle fixture',
        model='',
        workspace=workspace,
        messages=history + [{'role': 'user', 'content': 'Do the work'}],
        message_count=len(history) + 1,
        tool_calls=[],
        active_stream_id='run-settle',
        pending_user_message='Do the work',
        pending_started_at=time.time(),
    )


def run(pw, base, state_settings, virt, expanded, idle_probe, history_len):
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 1280, 'height': 900}, bypass_csp=True)
    context.add_init_script(INIT)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    sid = 'settle-session'
    history = make_history(history_len)
    session = session_payload(sid, base, history)
    page.route('**/api/settings', lambda r: r.fulfill(json=state_settings))
    page.route('**/api/session?*', lambda r: r.fulfill(
        json={'session': session if parse_qs(urlsplit(r.request.url).query).get('session_id', [''])[0] == sid
              else dict(session_id='idle-fixture', messages=[], message_count=0, tool_calls=[], workspace=base)}))
    page.route('**/api/chat/stream/status?*', lambda r: r.fulfill(json={'active': True}))
    page.goto(base, wait_until='load')
    deadline = time.monotonic() + 30
    while not page.evaluate("typeof loadSession==='function' && S._bootReady === true"):
        assert time.monotonic() < deadline, ('boot timeout', errors)
        page.wait_for_timeout(50)

    page.evaluate("""async sid=>{await loadSession(sid);}""", sid)
    page.wait_for_function(
        "fixtureSources.some(s=>s.url.includes('api/chat/stream?')&&s.readyState===1)",
        timeout=15000)

    runtime_checks = page.evaluate(RUNTIME_CHECKS)

    # Multi-step turn: intro prose, two tool calls, then a long final answer.
    page.evaluate("()=>{const src=fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&s.readyState===1);window.__emit=(n,d,id)=>src.emit(n,d,id);window.__emit('token',{text:'Working on it. '});}")
    page.wait_for_timeout(120)
    for i in (1, 2):
        page.evaluate("""i=>{
          window.__emit('tool',{name:'terminal',tid:'settle-tool-'+i,args:{command:'run '+i},preview:'run '+i});
          window.__emit('tool_complete',{name:'terminal',tid:'settle-tool-'+i,preview:'output line '+i,duration:1});
        }""", i)
        page.wait_for_timeout(120)
    page.evaluate("()=>{window.__emit('token',{text:' " + FINAL_TEXT + "'});}")
    page.wait_for_timeout(400)

    live_before = page.evaluate(ASSERTIONS)

    # Terminal `done` — NO network interruption anywhere in this script.
    page.evaluate("""({sid,text})=>{
      const msgs=[
        {role:'user',content:'Do the work'},
        {role:'assistant',content:text,_anchor_stream_id:'run-settle'},
      ];
      window.__emit('done',{
        status:'completed',
        session:{session_id:sid,title:'Settle fixture',model:'',workspace:'ws',
                 messages:msgs,message_count:msgs.length,
                 tool_calls:[{name:'terminal',tid:'settle-tool-1',args:{command:'run 1'},preview:'run 1',snippet:'output line 1',done:true},
                             {name:'terminal',tid:'settle-tool-2',args:{command:'run 2'},preview:'run 2',snippet:'output line 2',done:true}]},
        usage:{input_tokens:100,output_tokens:200,duration_seconds:2.5,turn_cache_hit_percent:0},
      });
    }""", {'sid': sid, 'text': FINAL_TEXT})

    # Sample the SAME frame the settle completes in.
    settled = None
    for _ in range(120):
        page.wait_for_timeout(25)
        if not page.evaluate("()=>!!S.busy"):
            settled = page.evaluate(ASSERTIONS)
            break
    if settled is None:
        settled = {'error': 'never settled', 'busy': page.evaluate('()=>S.busy')}

    same_frame = settled
    next_frame = None
    after_wait = None
    if 'error' not in settled:
        page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
        next_frame = page.evaluate(ASSERTIONS)
        page.wait_for_timeout(1500)
        after_wait = page.evaluate(ASSERTIONS)
    else:
        after_wait = None

    idle_result = None
    after_idle = None
    if idle_probe != 'none':
        # 'equal'    → the sidebar row reports the same message count (no reload)
        # 'changed'  → the count moved (reload path) — both were in the report
        count = len(history) + 1 if idle_probe == 'equal' else len(history) + 2
        row = dict(session_id=sid, is_streaming=False, active_stream_id=None,
                   pending_user_message=None, has_pending_user_message=False,
                   pending_started_at=None, message_count=count)
        idle_result = page.evaluate("""row=>{try{return _reconcileActiveSessionIdleStateFromList([row]);}catch(e){return 'ERR:'+e.message;}}""", row)
        page.wait_for_timeout(1200)
        after_idle = page.evaluate(ASSERTIONS)

    context.close()
    browser.close()
    return {
        'virt': virt, 'expanded': expanded, 'idle_probe': idle_probe,
        'runtime_checks': runtime_checks,
        'live_before': live_before,
        'same_frame': same_frame,
        'next_frame': next_frame,
        'after_wait': after_wait,
        'idle_result': idle_result,
        'after_idle': after_idle,
        'pageerrors': errors,
    }


def verdict(res):
    bad = []
    checks = res.get('runtime_checks') or {}
    for name in ('normalize', 'finalize'):
        if checks.get(name) != 'ok':
            bad.append(f'runtime:{name}: {checks.get(name)}')
    for label in ('same_frame', 'next_frame', 'after_wait', 'after_idle'):
        r = res.get(label)
        if not r:
            continue
        if 'error' in r:
            bad.append(f'{label}: {r}')
            continue
        if r['visibleOwnerCount'] != 1:
            bad.append(f"{label}: visibleOwnerCount={r['visibleOwnerCount']} (want 1) hidden={r['hiddenOwnerCount']} classes={r['hiddenOwnerClasses']}")
        if r['hiddenLiveOnlyTurnCount']:
            bad.append(f"{label}: hiddenLiveOnlyTurnCount={r['hiddenLiveOnlyTurnCount']}")
        if r['liveOwnsSettled']:
            bad.append(f"{label}: liveAssistantTurn still owns the settled reply")
        if r['msgCount'] > 1 and r['visibleOwnerCount'] == 0:
            bad.append(f"{label}: BLANK transcript (msgCount={r['msgCount']})")
    if res.get('pageerrors'):
        bad.append(f"pageerrors: {res['pageerrors']}")
    return bad


def variants():
    only = os.environ.get('SETTLE_VARIANTS')
    out = [(virt, expanded, idle)
           for virt in (True, False)
           for expanded in (True, False)
           for idle in ('none', 'equal', 'changed')]
    if only is not None:
        wanted = {int(x) for x in only.split(',') if x.strip() != ''}
        out = [v for i, v in enumerate(out) if i in wanted]
    return out


def main() -> int:
    history_len = int(os.environ.get('SETTLE_HISTORY', '40'))
    settings = dict(
        theme='dark', skin='default',
        chat_activity_display_mode='transparent_stream',
        worklog_details_expanded_default=True,
        virtualize_transcript=True,
        show_token_usage=True,
    )
    failed = 0
    total = 0
    with tempfile.TemporaryDirectory(prefix='webui-settle-7676-') as temp:
        state = Path(temp)
        env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'TMPDIR') if k in os.environ}
        env.update(
            HOME=temp, HERMES_HOME=temp, HERMES_BASE_HOME=temp,
            HERMES_WEBUI_STATE_DIR=str(state / 'webui'),
            HERMES_CONFIG_PATH=str(state / 'config.yaml'),
            HERMES_WEBUI_HOST='127.0.0.1', HERMES_WEBUI_SKIP_ONBOARDING='1',
            HERMES_WEBUI_AGENT_DIR=str(state / 'no-agent'),
        )
        proc, log, _, base = _start_webui_server(ROOT, env, state)
        try:
            with sync_playwright() as pw:
                for virt, expanded, idle_probe in variants():
                    total += 1
                    s = dict(settings)
                    s['virtualize_transcript'] = virt
                    s['worklog_details_expanded_default'] = expanded
                    try:
                        res = run(pw, base, s, virt, expanded, idle_probe, history_len)
                    except Exception as exc:  # noqa: BLE001
                        print(f'VARIANT virt={virt} expanded={expanded} idle={idle_probe} EXCEPTION {exc}')
                        failed += 1
                        continue
                    bad = verdict(res)
                    failed += bool(bad)
                    print(f'VARIANT virt={virt} expanded={expanded} idle={idle_probe} -> '
                          f'{"FAIL" if bad else "ok"}')
                    print('  runtime:', json.dumps(res['runtime_checks']))
                    for label in ('live_before', 'same_frame', 'next_frame', 'after_wait', 'after_idle'):
                        r = res.get(label)
                        if not r or 'error' in r:
                            continue
                        print(f'  {label}:', json.dumps({k: r[k] for k in
                              ('visibleOwnerCount', 'hiddenOwnerCount', 'hiddenLiveOnlyTurnCount',
                               'liveOwnsSettled', 'transRows', 'settleOwnerAttr', 'msgCount', 'busy') if k in r}))
                    for b in bad:
                        print('   FAIL:', b)
        finally:
            _terminate_process(proc)
            log.close()
    print(f'{"PASS" if failed == 0 else "FAIL"}: {total - failed}/{total} variants')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
