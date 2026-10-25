"""Real-browser owned-window reconciliation; synthetic data, isolated server state."""
import os
import time
from pathlib import Path

import pytest

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


@pytest.fixture(params=os.environ.get('BROWSERS', 'chromium,webkit').split(','))
def page(request, isolated_webui):
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
