#!/usr/bin/env python3
"""Real browser: a memory recall notice must not reflow the composer (#8940).

C70-UX1 regression guard. The first version of the recall indicator wrote the
notice into the composer status slot. In a populated layout that widened the
footer past its fit, so the footer-fit pass switched it to the compact
`cf-icons` stage: persona, workspace and model lost their labels, Saved Prompts
was hidden and the settled composer grew (92.39 -> 102.39 CSS px in the Gate
run) until the notice expired.

This drives the real app (real server, real stylesheet, real footer-fit
observers and real timers) with a controlled EventSource on a populated
session, and compares the composer against its own pre-recall baseline:

  * 1400x900 desktop with the sidebar AND the workspace panel open,
  * 834x1194 touch tablet.

Phases, each measured after the asynchronous footer fit has settled:

  baseline  live turn streaming, no recall yet
  active    `memory_recall` received (the notice must be visible somewhere)
  expired   past the 6 s lifetime of the former composer notice
  settled   terminal `done`, recall persisted on the turn as _memoryRecall

In every phase after the baseline the composer must keep: the same
`.composer-wrap` height, no `cf-icons`/`cf-burger` stage, readable persona,
workspace and model labels at their baseline widths, and a visible, clickable
Saved Prompts button.

Synthetic sessions/transport; no provider, credentials or production state.
A failure leaves the server log, a screenshot and the measurements in
LIFECYCLE_ARTIFACT_DIR (a kept temporary directory when that is unset).
Run: python tests/browser_memory_recall_composer_layout.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from browser_conversation_lifecycle import _start_webui_server, _terminate_process
from browser_settle_visibility_7676 import INIT, make_history

ROOT = Path(os.environ.get('WEBUI_TEST_ROOT', Path(__file__).resolve().parent.parent))

SID = 'recall-layout'
STREAM = 'run-recall-layout'
MODEL = 'anthropic/claude-sonnet-4.6'
RECALL = '\U0001f9e0 Honcho — recalled 3 memories'
# Lifetime of the former composer notice (setComposerStatus(message, 6000)).
FORMER_NOTICE_MS = 6000

VIEWPORTS = (
    # name, width, height, touch, open both side panels
    ('desktop-panels-open', 1400, 900, False, True),
    ('tablet', 834, 1194, True, False),
)

# Geometry and control state of the composer, read after the footer fit settled.
MEASURE = """
() => {
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const label = id => {
    const el = document.getElementById(id);
    const r = el ? el.getBoundingClientRect() : null;
    return {
      text: el ? (el.textContent || '').trim() : '',
      visible: visible(el),
      width: r ? Math.round(r.width * 100) / 100 : 0,
    };
  };
  const wrap = document.querySelector('.composer-wrap');
  const footer = document.querySelector('.composer-footer');
  const saved = document.getElementById('btnSavedPrompts');
  let savedHit = false;
  if (visible(saved)) {
    const r = saved.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    savedHit = !!(hit && (hit === saved || saved.contains(hit)));
  }
  const sidebar = document.querySelector('.layout');
  const panel = document.querySelector('.rightpanel');
  return {
    composerHeight: wrap ? wrap.getBoundingClientRect().height : 0,
    footerStage: footer ? ['cf-icons', 'cf-burger'].filter(c => footer.classList.contains(c)) : ['missing'],
    persona: label('profileChipLabel'),
    workspace: label('composerWorkspaceLabel'),
    model: label('composerModelLabel'),
    savedPrompts: {visible: visible(saved), hit: savedHit},
    sidebarOpen: !!(sidebar && !sidebar.classList.contains('sidebar-collapsed')
      && visible(document.querySelector('.sidebar'))),
    workspacePanelOpen: visible(panel) && panel.getBoundingClientRect().width > 0,
    sessionRows: [...document.querySelectorAll('.sidebar .session-item')].filter(visible).length,
    busy: !!S.busy,
  };
}
"""

# Placement-agnostic: the notice must be on screen while active, wherever it lives.
RECALL_VISIBLE = """
text => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    if (!(n.textContent || '').includes(text)) continue;
    const el = n.parentElement;
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0) return true;
  }
  return false;
}
"""

# Let every observer-driven footer fit (ResizeObserver / MutationObserver ->
# requestAnimationFrame) run, then sample. A same-task read misses the fit.
SETTLE_FIT = """
async () => {
  for (let i = 0; i < 4; i++) await new Promise(r => requestAnimationFrame(() => r()));
  await new Promise(r => setTimeout(r, 400));
  for (let i = 0; i < 2; i++) await new Promise(r => requestAnimationFrame(() => r()));
}
"""


def session_payload(workspace, history):
    return dict(
        session_id=SID,
        title='Memory recall layout',
        model=MODEL,
        workspace=workspace,
        messages=history + [{'role': 'user', 'content': 'Plan the release'}],
        message_count=len(history) + 1,
        tool_calls=[],
        active_stream_id=STREAM,
        pending_user_message='Plan the release',
        pending_started_at=time.time(),
    )


def sidebar_rows(workspace):
    now = time.time()
    rows = [dict(session_id=SID, title='Memory recall layout', model=MODEL, workspace=workspace,
                 message_count=25, created_at=now - 600, updated_at=now, last_message_at=now,
                 is_streaming=True, active_stream_id=STREAM)]
    for i in range(12):
        ts = now - 3600 * (i + 1)
        rows.append(dict(session_id=f'recall-layout-older-{i}', title=f'Earlier conversation {i}',
                         model=MODEL, workspace=workspace, message_count=6 + i,
                         created_at=ts, updated_at=ts, last_message_at=ts))
    return rows


def compare(phase, base, now):
    """Return the ways `now` differs from the pre-recall composer `base`."""
    bad = []
    if abs(now['composerHeight'] - base['composerHeight']) > 0.5:
        bad.append(f"{phase}: composer height {base['composerHeight']} -> {now['composerHeight']}")
    if now['footerStage']:
        bad.append(f"{phase}: footer switched to compact stage {now['footerStage']}")
    for key in ('persona', 'workspace', 'model'):
        b, n = base[key], now[key]
        if not n['visible'] or not n['text']:
            bad.append(f"{phase}: {key} label not readable ({n})")
        elif abs(n['width'] - b['width']) > 0.5:
            bad.append(f"{phase}: {key} label width {b['width']} -> {n['width']}")
    if not (now['savedPrompts']['visible'] and now['savedPrompts']['hit']):
        bad.append(f"{phase}: Saved Prompts not reachable ({now['savedPrompts']})")
    return bad


def save_evidence(page, artifact_dir, name, report):
    try:
        (artifact_dir / f'{name}.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        if page is not None:
            page.screenshot(path=str(artifact_dir / f'{name}.png'), timeout=10000)
    except Exception as exc:  # noqa: BLE001 - never hide the original failure
        print(f'   could not save evidence for {name}: {exc}', file=sys.stderr)


def run(pw, base_url, workspace, artifact_dir, name, width, height, touch, open_panels):
    browser = pw.chromium.launch(headless=True)
    failures, errors, phases = [], [], {}
    page = None
    completed = False
    try:
        context = browser.new_context(viewport={'width': width, 'height': height},
                                      has_touch=touch, bypass_csp=True)
        context.add_init_script(INIT)
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        session = session_payload(workspace, make_history(24))

        def _serve_session(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if query.get('session_id', [''])[0] == SID:
                route.fulfill(json={'session': session})
            else:
                route.fulfill(json={'session': dict(session_id='idle-fixture', messages=[],
                                                    message_count=0, tool_calls=[],
                                                    workspace=workspace)})

        def _serve_sessions(route):
            # Populated sidebar: the live fixture plus a page of older conversations.
            if urlsplit(route.request.url).path != '/api/sessions':
                route.fallback()
                return
            data = route.fetch().json()
            data['sessions'] = sidebar_rows(workspace) + list(data.get('sessions') or [])
            route.fulfill(json=data)

        page.route('**/api/session?*', _serve_session)
        page.route('**/api/sessions*', _serve_sessions)
        page.route('**/api/chat/stream/status?*', lambda r: r.fulfill(json={'active': True}))
        page.goto(base_url, wait_until='load')
        deadline = time.monotonic() + 30
        while not page.evaluate("typeof loadSession==='function' && S._bootReady===true"):
            assert time.monotonic() < deadline, ('boot timeout', errors)
            page.wait_for_timeout(50)

        page.evaluate("async sid=>{await loadSession(sid);}", SID)
        page.wait_for_function(
            "fixtureSources.some(s=>s.url.includes('api/chat/stream?')&&s.readyState===1)",
            timeout=15000)
        if open_panels:
            page.evaluate("()=>{toggleSidebar(false);toggleWorkspacePanel(true);}")
        page.evaluate("""()=>{
          const src=fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&s.readyState===1);
          window.__emit=(n,d,id)=>src.emit(n,d,id);
          window.__emit('token',{text:'Looking at the release checklist. '});
        }""")
        page.evaluate(SETTLE_FIT)
        page.wait_for_timeout(300)
        page.evaluate(SETTLE_FIT)

        phases['baseline'] = page.evaluate(MEASURE)
        base = phases['baseline']
        # The baseline itself must be the populated, full-label layout the
        # regression broke; otherwise the comparison proves nothing.
        assert base['busy'], (name, 'stream not live', base)
        assert not base['footerStage'], (name, 'baseline already compact', base)
        for key in ('persona', 'workspace', 'model'):
            assert base[key]['visible'] and base[key]['text'], (name, key, base)
        assert base['savedPrompts']['visible'] and base['savedPrompts']['hit'], (name, base)
        if open_panels:
            assert base['sidebarOpen'] and base['workspacePanelOpen'], (name, 'panels', base)
            assert base['sessionRows'] >= 2, (name, 'sidebar not populated', base)

        page.evaluate("m=>window.__emit('memory_recall',{session_id:'%s',message:m})" % SID, RECALL)
        page.evaluate(SETTLE_FIT)
        phases['active'] = page.evaluate(MEASURE)
        if not page.evaluate(RECALL_VISIBLE, RECALL):
            failures.append('active: recall notice is not visible')

        # Real timers: wait out the former notice's lifetime from the event.
        page.wait_for_timeout(FORMER_NOTICE_MS + 500)
        page.evaluate(SETTLE_FIT)
        phases['expired'] = page.evaluate(MEASURE)

        page.evaluate("""({sid,stream,model,workspace,recall})=>{
          const msgs=[
            {role:'user',content:'Plan the release'},
            {role:'assistant',content:'Looking at the release checklist. Done.',
             _anchor_stream_id:stream,_memoryRecall:recall},
          ];
          window.__emit('done',{
            status:'completed',
            session:{session_id:sid,title:'Memory recall layout',model,workspace,
                     messages:msgs,message_count:msgs.length,tool_calls:[]},
            usage:{input_tokens:100,output_tokens:20,duration_seconds:1.5,turn_cache_hit_percent:0},
          });
        }""", {'sid': SID, 'stream': STREAM, 'model': MODEL, 'workspace': workspace,
              'recall': RECALL})
        deadline = time.monotonic() + 10
        while page.evaluate("()=>!!S.busy"):
            assert time.monotonic() < deadline, (name, 'turn did not settle')
            page.wait_for_timeout(50)
        page.evaluate(SETTLE_FIT)
        phases['settled'] = page.evaluate(MEASURE)

        for phase in ('active', 'expired', 'settled'):
            failures += compare(phase, base, phases[phase])
        if errors:
            failures.append(f'page errors: {errors}')
        print(json.dumps({'viewport': name, 'sessionRows': base['sessionRows'], **{
            phase: {'composerHeight': m['composerHeight'], 'footerStage': m['footerStage'],
                    'labels': [m[k]['width'] for k in ('persona', 'workspace', 'model')],
                    'savedPrompts': m['savedPrompts']['hit']}
            for phase, m in phases.items()}}), flush=True)
        completed = True
    finally:
        if failures or not completed:
            save_evidence(page, artifact_dir, name, {
                'completed': completed, 'failures': failures, 'pageErrors': errors, 'phases': phases,
            })
        browser.close()
    return failures


def main() -> int:
    from playwright.sync_api import sync_playwright

    failed = 0
    artifact_env = (os.environ.get('LIFECYCLE_ARTIFACT_DIR') or '').strip()
    artifact_dir = Path(artifact_env or tempfile.mkdtemp(prefix='webui-recall-layout-artifacts-'))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='webui-recall-layout-') as temp:
        state = Path(temp)
        workspace = state / 'release-planning'
        workspace.mkdir()
        env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'TMPDIR') if k in os.environ}
        env.update(
            HOME=temp, HERMES_HOME=temp, HERMES_BASE_HOME=temp,
            HERMES_WEBUI_STATE_DIR=str(state / 'webui'),
            HERMES_CONFIG_PATH=str(state / 'config.yaml'),
            HERMES_WEBUI_HOST='127.0.0.1', HERMES_WEBUI_SKIP_ONBOARDING='1',
            HERMES_WEBUI_AGENT_DIR=str(state / 'no-agent'),
            HERMES_WEBUI_DEFAULT_WORKSPACE=str(workspace),
        )
        proc, log, _, base_url = _start_webui_server(ROOT, env, artifact_dir)
        try:
            with sync_playwright() as pw:
                for name, width, height, touch, open_panels in VIEWPORTS:
                    try:
                        bad = run(pw, base_url, str(workspace), artifact_dir, name, width, height, touch, open_panels)
                    except Exception as exc:  # noqa: BLE001
                        bad = [f'EXCEPTION {exc}']
                    failed += bool(bad)
                    print(f'VIEWPORT {name} {width}x{height} -> {"FAIL" if bad else "ok"}')
                    for b in bad:
                        print('   FAIL:', b)
        finally:
            _terminate_process(proc)
            log.close()
    if failed:
        print(f'Artifacts: {artifact_dir}', file=sys.stderr)
    elif not artifact_env:
        shutil.rmtree(artifact_dir, ignore_errors=True)
    print(f'{"PASS" if not failed else "FAIL"}: memory recall keeps the composer layout '
          f'({len(VIEWPORTS) - failed}/{len(VIEWPORTS)} viewports)')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
