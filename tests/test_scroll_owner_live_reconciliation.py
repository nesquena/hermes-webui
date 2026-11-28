"""Real-browser owned-window reconciliation; synthetic data, isolated server state."""
import os
import time
from pathlib import Path

import pytest

from tests._browser_engines import resolved_engines
from tests.browser_conversation_lifecycle import _start_webui_server, _terminate_process

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def isolated_webui(tmp_path_factory):
    state = tmp_path_factory.mktemp('live-reconciliation')
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'TMPDIR') if k in os.environ}
    env.update(HOME=str(state), HERMES_HOME=str(state), HERMES_BASE_HOME=str(state),
               HERMES_WEBUI_STATE_DIR=str(state / 'webui'),
               HERMES_CONFIG_PATH=str(state / 'config.yaml'),
               HERMES_WEBUI_HOST='127.0.0.1', HERMES_WEBUI_SKIP_ONBOARDING='1',
               HERMES_WEBUI_AGENT_DIR=str(state / 'no-agent'))
    proc, log, _, base = _start_webui_server(ROOT, env, state)
    try:
        yield base
    finally:
        _terminate_process(proc)
        log.close()


@pytest.fixture(params=resolved_engines() or ["chromium"])
def page(request, isolated_webui):
    if not resolved_engines():
        # Default engine set found nothing launchable on this host (e.g. no
        # WebKit system libraries and no Chromium). Skip the module's tests
        # instead of erroring every parametrized fixture; an explicit BROWSERS
        # request never lands here (strict — it fails loudly at launch below).
        pytest.skip("no launchable Playwright engine; set BROWSERS to force one")
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as pw:
        browser = getattr(pw, request.param).launch(headless=True)
        context = browser.new_context(bypass_csp=True, service_workers='block')
        page = context.new_page()
        # Optional immutable source control for proving the regression fails
        # before the fix; all renderer calls still execute in the real browser.
        control = os.environ.get('SCROLL_LIVE_UI_SOURCE')
        if control:
            source = Path(control).read_text()
            page.route('**/static/ui.js*', lambda route: route.fulfill(
                status=200, content_type='application/javascript', body=source))
        page.goto(isolated_webui, wait_until='load')
        deadline = time.monotonic() + 30
        while not page.evaluate("typeof renderMessages==='function' && S._bootReady===true"):
            assert time.monotonic() < deadline, 'boot timeout'
            page.wait_for_timeout(50)
        page.evaluate("""()=>{
          window._virtualizeTranscript=true;
          window._chatActivityDisplayMode='transparent_stream';
          S.session={session_id:'fixture',messages:[],tool_calls:[]};
          S.messages=[{role:'user',content:'Question'}];
          S.busy=true; S.activeStreamId='run-fixture'; INFLIGHT.fixture={};
          renderMessages();
        }""")
        try:
            yield page
        finally:
            context.close()
            browser.close()


@pytest.mark.parametrize('owned_option', ['_windowOnly', '_ownedPrepend'])
def test_parser_ahead_missing_staged_live_turn_stays_connected(page, owned_option):
    result = page.evaluate("""option=>{
      const turn=document.createElement('div');
      turn.id='liveAssistantTurn'; turn.dataset.sessionId='fixture';
      turn.innerHTML='<div data-live-assistant="1" data-live-segment-seq="1"><div class="msg-text">Parser ahead</div></div>';
      document.getElementById('liveAssistantTurn')?.remove();
      document.getElementById('msgInner').appendChild(turn);
      const parserTarget=turn.querySelector('.msg-text');
      renderMessages({[option]:true,preserveScroll:true});
      parserTarget.append(' continued');
      return {connected:parserTarget.isConnected, same:document.getElementById('liveAssistantTurn')===turn,
        count:document.querySelectorAll('#liveAssistantTurn').length,
        text:document.getElementById('msgInner').textContent};
    }""", owned_option)
    assert result['connected'] and result['same'], result
    assert result['count'] == 1, result
    assert 'Parser ahead continued' in result['text'], result


@pytest.mark.parametrize('owned_option', ['_windowOnly', '_ownedPrepend'])
def test_richer_staged_projection_keeps_structure_and_parser_segment(page, owned_option):
    result = page.evaluate("""option=>{
      document.getElementById('liveAssistantTurn')?.remove();
      S.messages.push({role:'assistant',content:'Earlier segment',_live:true,_liveSegmentSeq:1,_activityBurstId:'a'},
        {role:'assistant',content:'Tail',_live:true,_liveSegmentSeq:2,_activityBurstId:'b'});
      renderMessages();
      const turn=document.getElementById('liveAssistantTurn');
      const segments=turn.querySelectorAll('[data-live-assistant="1"]');
      if(segments.length<2)throw new Error('fixture must render multiple live segments: '+turn.outerHTML);
      const tail=segments[segments.length-1];
      const parserTarget=tail.querySelector('.msg-text')||tail;
      parserTarget.textContent='Tail parser ahead';
      // The parser DOM predates the earlier segment projected by the next render.
      segments[0].remove();
      renderMessages({[option]:true,preserveScroll:true});
      parserTarget.append(' continued');
      const rebuilt=document.getElementById('liveAssistantTurn');
      return {sameTurn:rebuilt===turn,connected:parserTarget.isConnected,
        sameTail:rebuilt.querySelector('[data-live-segment-seq="2"]')===tail,
        count:rebuilt.querySelectorAll('[data-live-assistant="1"]').length,text:rebuilt.textContent};
    }""", owned_option)
    assert not result['sameTurn'], result
    assert result['connected'] and result['sameTail'], result
    assert result['count'] == 2, result
    assert 'Earlier segment' in result['text'] and 'Tail parser ahead continued' in result['text'], result


@pytest.mark.parametrize('live,restore,saved_open,expected_open', [
    (False, False, True, False),  # ordinary settlement ignores the live open state
    (False, True, True, True),    # owned-window rebuild preserves explicit intent
    (False, True, False, False),
    (True, False, True, True),
    (True, False, False, False),
])
def test_activity_disclosure_restoration_is_opt_in(page, live, restore, saved_open, expected_open):
    result = page.evaluate("""({live,restore,savedOpen})=>{
      window._worklogDetailsExpandedByDefault=false;
      const key='disclosure-contract';
      _writeActivityDisclosureState(key,savedOpen);
      const host=document.createElement('div');
      document.getElementById('msgInner').append(host);
      const group=ensureActivityGroup(host,{activityKey:key,live,restoreDisclosure:restore});
      return {open:!group.classList.contains('tool-call-group-collapsed'),
        expanded:group.querySelector('.activity-summary').getAttribute('aria-expanded')};
    }""", {'live': live, 'restore': restore, 'savedOpen': saved_open})
    assert result == {'open': expected_open, 'expanded': str(expected_open).lower()}


@pytest.mark.parametrize('virtualized', [False, True])
def test_default_path_reconciles_preserved_live_turn_once(page, virtualized):
    """The preserved live turn must reconcile exactly once per render (#3877).

    renderMessages used to retain BOTH the inline re-attach pass and the
    extracted _reconcilePreservedLiveTurn call. On the default render path the
    rebuild happens through a full wipe, so the two passes saw DIFFERENT
    rebuilt turns: pass one moved the parser-owned tail into the freshly built
    turn, and pass two then found the earlier segment as the preserved turn's
    last remaining live segment and replaced the rebuilt tail with it. With a
    two-segment live turn whose earlier segment is longer than the parser tail,
    that disconnects the real smd parser target and renders the earlier prose
    twice (release gate af988ebd, regression 1; virtualization off and on).
    """
    result = page.evaluate("""async virtualized=>{
      const earlierProse='EARLIER-PROSE-MARKER '
        +'and plenty of settled prose so this segment outgrows the tail '.repeat(6);
      window._virtualizeTranscript=virtualized;
      document.getElementById('liveAssistantTurn')?.remove();
      S.messages.push(
        {role:'assistant',content:earlierProse,_live:true,_liveSegmentSeq:1,_activityBurstId:'a'},
        {role:'assistant',content:'Tail',_live:true,_liveSegmentSeq:2,_activityBurstId:'b'});
      renderMessages();
      const turn=document.getElementById('liveAssistantTurn');
      const segments=turn.querySelectorAll('[data-live-assistant=\"1\"]');
      if(segments.length<2) throw new Error('fixture must render two live segments');
      // The shipped smd parser owns the rendered tail's .msg-body, exactly as
      // messages.js binds it mid-stream.
      const tail=segments[segments.length-1];
      const target=tail.querySelector('.msg-body')||tail;
      const parser=window.smd.parser(window.smd.default_renderer(target));
      window.smd.parser_write(parser,'Tail streamed');
      target.__smdParser=parser;
      // The default-path re-render rebuilds through the full wipe; afterwards
      // the parser must still own a connected target and stream into it.
      renderMessages({preserveScroll:true});
      window.smd.parser_write(parser,' parser still owns this');
      window.smd.parser_end(parser);
      const rebuilt=document.getElementById('liveAssistantTurn');
      const innerText=document.getElementById('msgInner').textContent;
      return {
        connected:target.isConnected,
        seqs:rebuilt?Array.from(rebuilt.querySelectorAll('[data-live-assistant=\"1\"]'))
          .map(segment=>segment.getAttribute('data-live-segment-seq')):[],
        earlierCopies:innerText.split('EARLIER-PROSE-MARKER').length-1,
        tailLanded:innerText.includes('Tail streamed parser still owns this')
      };
    }""", virtualized)
    assert result['connected'], (virtualized, result)
    assert result['seqs'] == ['1', '2'], (virtualized, result)
    assert result['earlierCopies'] == 1, (virtualized, result)
    assert result['tailLanded'], (virtualized, result)


def test_owned_prepend_post_process_growth_settles_reader(page):  # noqa: F811
    """Post-processing growth after an owned prepend must not move the reader.

    Re-gate on 7902ab5f: the owned-window commit ran
    _postProcessWithAnchorSuppression in its rAF but never settled the reader
    afterwards, so an older-history load on a cached session painted one frame
    ~287px off and snapped back. The settle call must run inside the same
    guarded rAF; _settleMessageWindowReader's own input-epoch, scrollTop,
    revision, pinned-follower and two-viewport guards keep it from restoring an
    old reader after user input.
    """
    result = page.evaluate(r"""async () => {
      window._virtualizeTranscript=false;
      S.busy=false; delete S.activeStreamId; delete INFLIGHT.fixture;
      // A reader who scrolled up into history: tail-follow must stay off so
      // the only scroll writer under test is the reader machinery itself.
      // BARE assignments — these are let-bindings, not window properties.
      _messageUserUnpinned=true; _scrollPinned=false;
      // Truncated-history shape: 36 rendered messages, absolute session index
      // = _oldestIdx + raw, exactly the contract the real _loadOlderMessages
      // maintains when it prepends a page and lowers the offset.
      _oldestIdx=24;
      S.messages=Array.from({length:36},(_,i)=>({role:'user',content:'Body '+(24+i)}));
      renderMessages();
      // Drain the pending tail-reanchor microtask the small-content boot
      // render queued (it fires _reanchorPinnedTailAfterRender and would
      // otherwise jump to the bottom in the middle of the owned prepend).
      await new Promise(r=>requestAnimationFrame(()=>setTimeout(r,60)));
      // Invalidate the boot-armed scrollToBottom settle chain: its delayed
      // writers re-fire on every later layout growth and would swamp the
      // reader machinery under test.
      if(typeof _messageScrollInputGeneration==='number') _messageScrollInputGeneration++;
      _scrollPinned=false; _messageUserUnpinned=true;
      const c=document.getElementById('messages'), inner=document.getElementById('msgInner');
      const rows=Array.from(inner.querySelectorAll('[data-msg-idx]'));
      const target=rows.find(n=>n.dataset.msgIdx==='24');
      if(!target) return {error:'fixture row 24 missing', mounted:rows.length};
      c.scrollTop=target.getBoundingClientRect().top-c.getBoundingClientRect().top+c.scrollTop;
      const anchor=_messageWindowSnapshot();
      if(!anchor||anchor.sessionIndex!==48)
        return {error:'anchor session index', got:anchor&&anchor.sessionIndex};
      // Older history loads: prepend 12 messages and drop the offset — the
      // rows' absolute session indices are unchanged, their raw indices shift.
      S.messages=Array.from({length:12},(_,i)=>({role:'user',content:'Older '+(12+i)}))
        .concat(S.messages);
      _oldestIdx=12;
      // Post-processing grows content above the anchor (the deferred-row
      // materialization class the re-gate measured at ~287px).
      const orig=window.postProcessRenderedMessages;
      window.postProcessRenderedMessages=container=>{
        orig(container);
        const first=container.querySelector('[data-msg-idx]');
        if(first&&!first.dataset.grown){
          first.dataset.grown='1';
          const g=document.createElement('div');
          g.style.height='287px'; g.textContent='materialized deferred rows';
          first.insertBefore(g,first.firstChild);
        }
      };
      try{
        renderMessages({preserveScroll:true,_prependAnchor:anchor,_ownedPrepend:true});
        // First frame: the owned rAF ran (post-process, and the settle under
        // test) but the ResizeObserver has not delivered yet — this is the
        // frame the re-gate caught painting ~287px off without the settle.
        await new Promise(r=>requestAnimationFrame(r));
        const rowEarly=Array.from(inner.querySelectorAll('[data-msg-idx]'))
          .find(n=>Number(n.dataset.sessionMsgIdx)===anchor.sessionIndex);
        if(!rowEarly) return {error:'anchor row missing after prepend'};
        const deltaFirstFrame=rowEarly.getBoundingClientRect().top
          -c.getBoundingClientRect().top-anchor.rowOffset;
        // Then the suppression release's own rAF and a layout tick.
        await new Promise(r=>requestAnimationFrame(()=>setTimeout(r,40)));
        // Resolve the SAME content landmark by absolute session index.
        const row=Array.from(inner.querySelectorAll('[data-msg-idx]'))
          .find(n=>Number(n.dataset.sessionMsgIdx)===anchor.sessionIndex);
        if(!row) return {error:'anchor row missing after prepend',
          grown:!!inner.querySelector('[data-grown]')};
        const delta=row.getBoundingClientRect().top-c.getBoundingClientRect().top
          -anchor.rowOffset;
        return {deltaFirstFrame, delta, scrollTop:c.scrollTop,
          grown:!!inner.querySelector('[data-grown]'),
          sessionIdx:row.dataset.sessionMsgIdx};
      }finally{
        window.postProcessRenderedMessages=orig;
      }
    }""")
    assert 'error' not in result, result
    assert result['grown'], result
    assert result['sessionIdx'] == '48', result
    # The settle runs INSIDE the owned rAF: the growth is compensated before
    # the ResizeObserver could deliver, so even the first painted frame holds.
    assert abs(result['deltaFirstFrame']) < 1, result
    assert abs(result['delta']) < 1, result


def test_snapshot_leaves_display_only_rows_without_session_index(page):  # noqa: F811
    """A display-only recovered row must not snapshot as canonical message 0.

    Re-gate on 7902ab5f (should fix): the reader snapshot Number()-ed the row's
    EMPTY data-session-msg-idx into 0, so an anchored reader on a recovered
    prompt recorded canonical message 0. Empty or non-integer means "no session
    index"; the content-key fallback owns the row.
    """
    result = page.evaluate(r"""() => {
      window._virtualizeTranscript=false;
      S.busy=false; delete S.activeStreamId; delete INFLIGHT.fixture;
      const inner=document.getElementById('msgInner'), c=document.getElementById('messages');
      inner.innerHTML=''; inner.dataset.windowSession=S.session.session_id;
      const canon=document.createElement('div');
      canon.dataset.msgIdx='0'; canon.dataset.sessionMsgIdx='0';
      canon.dataset.messageAnchorKey='m0';
      canon.style.cssText='height:60px'; canon.textContent='canonical 0';
      inner.append(canon);
      const disp=document.createElement('div');
      disp.dataset.msgIdx='7'; disp.dataset.displayOnly='1';
      disp.dataset.sessionMsgIdx=''; disp.dataset.messageAnchorKey='m7-raw';
      disp.style.cssText='height:90px'; disp.textContent='recovered prompt';
      inner.insertBefore(disp, inner.firstChild);
      c.scrollTop=0;
      const snap=_messageWindowSnapshot();
      if(!snap) return {error:'snapshot returned null'};
      return {sessionIndex:snap.sessionIndex, key:snap.key,
        pickedDisplayOnly:snap.row===disp};
    }""")
    assert 'error' not in result, result
    assert result['pickedDisplayOnly'], result
    assert result['sessionIndex'] is None, result
    assert result['key'] == 'm7-raw', result


def test_restore_skips_display_only_rows_and_null_session_index(page):  # noqa: F811
    """The restore must not Number('') a display-only row into canonical 0.

    Re-gate on 7902ab5f (should fix): the restore lookup matched
    Number(node.dataset.sessionMsgIdx)===anchor.sessionIndex unguarded, so a
    null-index anchor or an empty dataset value latched the compensation onto
    message 0 or a display-only recovered row instead of falling through to the
    content key.
    """
    result = page.evaluate(r"""() => {
      window._virtualizeTranscript=false;
      S.busy=false; delete S.activeStreamId; delete INFLIGHT.fixture;
      const inner=document.getElementById('msgInner'), c=document.getElementById('messages');
      inner.innerHTML=''; inner.dataset.windowSession=S.session.session_id;
      const mkRow=(rawIdx,sessionIdx,key,displayOnly,height)=>{
        const r=document.createElement('div');
        r.dataset.msgIdx=String(rawIdx);
        r.dataset.sessionMsgIdx=sessionIdx;
        if(displayOnly) r.dataset.displayOnly='1';
        if(key) r.dataset.messageAnchorKey=key;
        r.style.cssText='height:'+height+'px';
        r.textContent='row '+(key||rawIdx);
        inner.append(r);
        return r;
      };
      // Case A: a null anchor session index (recovered-prompt snapshot) must
      // skip the index match entirely; the unique content key owns the row.
      // The trailing spacer makes the pane scrollable (a 180px transcript in a
      // 565px viewport clamps every scrollTop write to 0).
      mkRow(0,'0','m0',false,60);
      mkRow(7,'','m7-raw',true,60);
      mkRow(9,'9','m9',false,60);
      const spacerA=document.createElement('div');
      spacerA.style.cssText='height:900px'; spacerA.textContent='tail spacer';
      inner.append(spacerA);
      c.scrollTop=0;
      const anchorA={node:null,row:null,landmarkIndex:-1,sessionIndex:null,
        activityKind:'',activityKey:'',activitySourceIndex:null,key:'m9',offset:60};
      const m9Top=document.getElementById('msgInner').querySelector('[data-message-anchor-key="m9"]')
        .getBoundingClientRect().top-c.getBoundingClientRect().top;
      _restoreMessageWindowReader(inner,anchorA,100000);
      const afterA=c.scrollTop;
      // Case B: sessionIndex 0 with NO canonical 0 — the display-only row's
      // EMPTY index must not become a 0 match; no owner, no compensation.
      inner.innerHTML='';
      mkRow(5,'5','m5',false,60);
      mkRow(7,'','',true,60);
      mkRow(9,'9','m9',false,60);
      const spacerB=document.createElement('div');
      spacerB.style.cssText='height:900px'; spacerB.textContent='tail spacer';
      inner.append(spacerB);
      c.scrollTop=0;
      const anchorB={node:null,row:null,landmarkIndex:-1,sessionIndex:0,
        activityKind:'',activityKey:'',activitySourceIndex:null,key:'missing-key',offset:0};
      _restoreMessageWindowReader(inner,anchorB,100000);
      const afterB=c.scrollTop;
      return {afterA, afterB, m9Top};
    }""")
    # Case A: the restore must compensate to row m9's real measured offset
    # (its viewport top minus the anchor's within-row offset of 60).
    assert result['afterA'] == result['m9Top'] - 60, result
    # Case B: no legitimate owner → no compensation at all.
    assert result['afterB'] == 0, result
