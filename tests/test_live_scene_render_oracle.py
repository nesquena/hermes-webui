"""Fresh-vs-incremental oracle for live activity-scene DOM.

The shipping classic scripts run in Chromium, not a string or VM harness.  A is
rendered into a new empty turn after every scene mutation; B renders the same
mutations into one retained turn.  Their scene blocks must be byte-identical.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
UI_JS = (STATIC / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (STATIC / "messages.js").read_text(encoding="utf-8")
SMD_JS = (STATIC / "vendor" / "smd.min.js").read_text(encoding="utf-8")
SUPPORT_SCRIPTS = [
    (STATIC / name).read_text(encoding="utf-8")
    for name in ("i18n.js", "icons.js", "assistant_turn_anchors.js")
]
HARNESS_HTML = re.sub(r"<script\b[^>]*>.*?</script>", "", INDEX_HTML, flags=re.I | re.S)
HARNESS_HTML = re.sub(r"<link\b[^>]*>", "", HARNESS_HTML, flags=re.I)

_SCENARIO = r"""
async (mode) => {
  const stream = 'oracle-stream';
  Date.now = () => 1700000001000;  // Freeze elapsed timer labels across A and B.
  window._chatActivityDisplayMode = mode;
  window._showThinking = true;
  window._simplifiedToolCalling = true;
  S.session = {session_id: 'oracle-session', messages: [], pending_started_at: 1700000000};
  S.messages = [];
  S.toolCalls = [];
  S.activeStreamId = stream;
  $('msgInner').innerHTML = '';
  const rows = [];
  const steps = [];
  const upsert = (next) => steps.push(() => upsertNow(next));
  const upsertNow = (next) => {
    const index = rows.findIndex(row => row.row_id === next.row_id);
    if (index < 0) rows.push(next); else rows[index] = next;
  };
  const textRow = (id, role, source, text, status='running') => ({
    row_id:id, local_id:id, event_id:'event-'+id, role,
    kind:role === 'thinking' ? 'reasoning' : 'process_prose',
    source_event_type:source, status, text, created_at:1700000000,
    payload:{text}, identity:{event_id:'event-'+id}
  });
  const toolRow = (id, done, error=false, result='') => {
    const tool = {id, tid:id, name:'terminal', args:{command:'printf '+id},
      preview:result, snippet:result, done, is_error:error,
      duration:done ? 0.25 : undefined, created_at:1700000000};
    return {row_id:'tool:'+id, local_id:id, role:'tool', kind:'tool_call',
      source_event_type:done ? 'tool_complete' : 'tool_delta',
      status:done ? (error ? 'error' : 'completed') : 'running',
      order_index:rows.length, tool, payload:tool, created_at:1700000000};
  };
  const toolPair = (id, error=false, result='result-'+id) => {
    if (steps.length < 300) upsert(toolRow(id, false));
    if (steps.length < 300) upsert(toolRow(id, true, error, result));
  };
  for (let n=1; n<=5; n++) upsert(textRow('thinking-main', 'thinking', 'reasoning', 'Reasoning '.repeat(n)));
  toolPair('primary', false, Array.from({length:85}, (_,i)=>'line-'+i+'-abcdefghijklmno').join('\n'));
  upsert(toolRow('correction', false));
  const corrected = toolRow('correction', true, false, 'CORRECTED TOOL RESULT');
  // A same-key completion correction exercises result reuse separately from
  // the ordinary tool_delta -> tool_complete identity transition.
  corrected.source_event_type = 'tool_delta';
  upsert(corrected);
  for (let n=0; n<3; n++) toolPair('initial-'+n, n===2);
  for (let n=1; n<=5; n++) upsert(textRow('prose-main', 'prose', 'token', 'Visible prose '.repeat(n)));
  upsert({row_id:'interim-boundary', local_id:'interim-boundary', role:'lifecycle',
    kind:'lifecycle_status', source_event_type:'interim_assistant', status:'completed',
    text:'Interim boundary', created_at:1700000000});
  upsert({row_id:'compression', local_id:'compression', role:'lifecycle',
    kind:'lifecycle_status', source_event_type:'compressing', status:'running',
    text:'Compressing context', created_at:1700000000});
  upsert({row_id:'compression', local_id:'compression', role:'lifecycle',
    kind:'lifecycle_status', source_event_type:'compressed', status:'completed',
    text:'Context auto-compressed', created_at:1700000000});
  let cycle=0;
  while (steps.length < 300) {
    for (let n=0; n<4 && steps.length<300; n++)
      toolPair('cycle-'+cycle+'-'+n, n===3 && cycle%4===0);
    if (steps.length < 300)
      for (let n=1; n<=3 && steps.length<300; n++)
        upsert(textRow('prose-cycle-'+cycle, 'prose', 'token', 'Cycle '+cycle+' prose '.repeat(n)));
    cycle++;
  }
  if (steps.length !== 300) throw new Error('expected exactly 300 mutations');

  const makeTurn = () => {
    const turn = _createAssistantTurn();
    turn.id = 'liveAssistantTurn';
    return turn;
  };
  const scene = () => ({version:'activity_scene_v1', mode,
    identity:{session_id:'oracle-session', stream_id:stream, run_id:stream},
    activity_rows:rows.map(row => JSON.parse(JSON.stringify(row)))});
  let retained = makeTurn();
  $('msgInner').appendChild(retained);
  let disclosure = null;
  // Only the live elapsed label is clock-derived. Every data-event-at value is
  // fixed by the fixture; no generated/random ids need normalization.
  const volatileAttributes = ['data-active-turn-elapsed'];
  const normalized = (turn) => {
    const clone = _assistantTurnBlocks(turn).cloneNode(true);
    for (const attr of volatileAttributes)
      clone.querySelectorAll('['+attr+']').forEach(el => el.removeAttribute(attr));
    // Attribute insertion order is not DOM state (open rows stamp data-expanded
    // at a different time), so sort names without discarding their values.
    for (const el of clone.querySelectorAll('*')) {
      const attrs = Array.from(el.attributes, a => [a.name, a.value])
        .sort(([a], [b]) => a.localeCompare(b));
      for (const [name] of attrs) el.removeAttribute(name);
      for (const [name, value] of attrs) el.setAttribute(name, value);
    }
    return clone.innerHTML;
  };
  const openCard = (card) => {
    if (!card || card.classList.contains('open')) return;
    const header = card.querySelector('.tool-card-header,.thinking-card-header');
    if (!header) throw new Error('disclosure header missing');
    header.click();
  };
  const openPrimary = (turn) => openCard(turn.querySelector(
    '[data-anchor-row-id="tool:primary"] .tool-card'));
  const openOtherCards = (turn) => {
    for (const card of turn.querySelectorAll('.tool-card,.thinking-card')) {
      if (card.closest('[data-anchor-row-id="tool:primary"],[data-anchor-row-id="tool:correction"]')) continue;
      openCard(card);
    }
  };
  const canonicalizeCorrection = (turn) => {
    const card = turn.querySelector('[data-anchor-row-id="tool:correction"] .tool-card');
    // Reconcile an implicit default disclosure through the user click path;
    // content and status remain completely untouched.
    openCard(card);
    if (card) card.querySelector('.tool-card-header,.thinking-card-header').click();
  };
  for (let index=0; index<steps.length; index++) {
    steps[index]();
    const currentScene = scene();
    retained.remove();
    const fresh = makeTurn();
    $('msgInner').appendChild(fresh);
    if (!renderLiveAnchorActivityScene(stream, currentScene, {sessionId:'oracle-session'}))
      throw new Error('fresh render returned false at '+index);
    // B's user-opened disclosure is applied to A through the same click path.
    if (disclosure) openPrimary(fresh);
    openOtherCards(fresh);
    canonicalizeCorrection(fresh);
    const freshHtml = normalized(fresh);
    fresh.remove();
    $('msgInner').appendChild(retained);
    if (!renderLiveAnchorActivityScene(stream, currentScene, {sessionId:'oracle-session'}))
      throw new Error('incremental render returned false at '+index);
    openOtherCards(retained);
    canonicalizeCorrection(retained);
    const retainedHtml = normalized(retained);
    if (freshHtml !== retainedHtml) {
      let at=0;
      while (at<freshHtml.length && freshHtml[at]===retainedHtml[at]) at++;
      return {ok:false, mode, step:index, rows:currentScene.activity_rows.length,
        common:at, fresh:freshHtml.slice(Math.max(0,at-300),at+700),
        retained:retainedHtml.slice(Math.max(0,at-300),at+700)};
    }
    // Simulate disclosure between steps: B must retain it; every later A gets
    // the same open state before comparison (never normalized away).
    if (index === 6) {
      openPrimary(retained);
      const card = retained.querySelector('[data-anchor-row-id="tool:primary"] .tool-card');
      if (!card || !card.classList.contains('open')) throw new Error('user disclosure did not open');
      disclosure = {rowId:'tool:primary', open:true};
      if (normalized(fresh) === normalized(retained)) throw new Error('disclosure probe has no observable effect');
      // Re-render A too for the successful equality check at this same step.
      retained.remove();
      const openedFresh = makeTurn();
      $('msgInner').appendChild(openedFresh);
      renderLiveAnchorActivityScene(stream, currentScene, {sessionId:'oracle-session'});
      openPrimary(openedFresh);
      openOtherCards(openedFresh);
      canonicalizeCorrection(openedFresh);
      canonicalizeCorrection(retained);
      if (normalized(openedFresh) !== normalized(retained))
        return {ok:false, mode, step:index, detail:'disclosure mismatch'};
      openedFresh.remove();
      $('msgInner').appendChild(retained);
    }
  }
  const ids = Array.from(retained.querySelectorAll('[data-anchor-scene-row="1"]'))
    .map(el => el.getAttribute('data-anchor-row-id'));
  return {ok:true, mode, steps:steps.length, rows:ids.length,
    uniqueRows:new Set(ids).size, disclosureRetained:!!retained.querySelector(
      '[data-anchor-row-id="tool:primary"] .tool-card.open,[data-anchor-row-id="tool:primary"][data-expanded="1"]')};
}
"""


_CORRECTION_SCENARIO = r"""
async ({mode, field}) => {
  const stream='oracle-correction-stream';
  Date.now=()=>1700000001000;
  window._chatActivityDisplayMode=mode;
  window._showThinking=true;
  window._simplifiedToolCalling=true;
  S.session={session_id:'oracle-session',messages:[],pending_started_at:1700000000};
  S.messages=[]; S.toolCalls=[]; S.activeStreamId=stream;
  $('msgInner').innerHTML='';
  const makeTurn=()=>{const turn=_createAssistantTurn();turn.id='liveAssistantTurn';return turn;};
  const prefix='PREFIX-123456789'; // exactly 16 characters
  const suffix='SUFFIX-abcdefghijklmnopqrstuvwxy'; // exactly 32 characters
  if(prefix.length!==16||suffix.length!==32)throw new Error('bad collision fixture ends');
  const originalText=prefix+'a'.repeat(96)+suffix;
  const correctedText=prefix+'b'.repeat(96)+suffix;
  const row={row_id:'tool:collision',local_id:'collision',role:'tool',kind:'tool_call',
    source_event_type:'tool_complete',status:'completed',created_at:1700000000,
    tool:{id:'collision',tid:'collision',name:'terminal',args:{command:originalText},
      preview:originalText,snippet:originalText,done:true,duration:0.25,created_at:1700000000}};
  row.payload=row.tool;
  const scene=()=>({version:'activity_scene_v1',mode,
    identity:{session_id:'oracle-session',stream_id:stream,run_id:stream},
    activity_rows:[JSON.parse(JSON.stringify(row))]});
  let retained=makeTurn();$('msgInner').appendChild(retained);
  renderLiveAnchorActivityScene(stream,scene(),{sessionId:'oracle-session'});
  const select=turn=>turn.querySelector('[data-anchor-row-id="tool:collision"]');
  const first=select(retained);
  const firstDetail=first.querySelector('.tool-card-detail');
  const header=first.querySelector('.tool-card-header');
  if(header)header.click();
  const openedDetail=first.querySelector('.tool-card-detail');
  const beforeHtml=first.innerHTML;
  renderLiveAnchorActivityScene(stream,scene(),{sessionId:'oracle-session'});
  const unchanged=select(retained)===first&&first.isConnected&&first.innerHTML===beforeHtml&&
    first.querySelector('.tool-card-detail')===openedDetail;
  if(field==='nested_argument')row.tool.args.command=correctedText;
  else {row.tool.preview=correctedText;row.tool.snippet=correctedText;}
  const currentScene=scene();
  retained.remove();
  const fresh=makeTurn();$('msgInner').appendChild(fresh);
  renderLiveAnchorActivityScene(stream,currentScene,{sessionId:'oracle-session'});
  const freshHeader=select(fresh).querySelector('.tool-card-header');
  if(freshHeader)freshHeader.click();
  const freshHtml=select(fresh).innerHTML;
  const freshText=select(fresh).textContent;
  fresh.remove();$('msgInner').appendChild(retained);
  renderLiveAnchorActivityScene(stream,currentScene,{sessionId:'oracle-session'});
  const corrected=select(retained);
  return {unchanged,correctedVisible:corrected.textContent.includes(correctedText),
    freshVisible:freshText.includes(correctedText),freshMatches:corrected.innerHTML===freshHtml,
    detailReused:corrected.querySelector('.tool-card-detail')===firstDetail,
    nodeReused:corrected===first};
}
"""


_STATUS_SCENARIO = r"""
async (mode) => {
  const stream='oracle-status-stream';
  Date.now=()=>1700000001000;
  window._chatActivityDisplayMode=mode;
  window._showThinking=true;
  window._simplifiedToolCalling=true;
  S.session={session_id:'oracle-session',messages:[],pending_started_at:1700000000};
  S.messages=[]; S.toolCalls=[]; S.activeStreamId=stream;
  $('msgInner').innerHTML='';
  const turn=_createAssistantTurn(); turn.id='liveAssistantTurn';
  $('msgInner').appendChild(turn);
  const row={row_id:'lifecycle-status',local_id:'lifecycle-status',role:'lifecycle',
    kind:'lifecycle_status',source_event_type:'custom_status',status:'running',text:'',
    created_at:1700000000};
  const scene=()=>({version:'activity_scene_v1',mode,
    identity:{session_id:'oracle-session',stream_id:stream,run_id:stream},
    activity_rows:[{...row}]});
  renderLiveAnchorActivityScene(stream,scene(),{sessionId:'oracle-session'});
  const label=()=>turn.querySelector('.agent-activity-status-label').textContent;
  const before=label();
  const builder=mode==='compact_worklog'?'_anchorSceneNodeForRow':'_anchorSceneTransparentNodeForRow';
  const original=window[builder]; let builds=0;
  window[builder]=function(...args){builds++; return original.apply(this,args);};
  row.status='completed';
  renderLiveAnchorActivityScene(stream,scene(),{sessionId:'oracle-session'});
  window[builder]=original;
  return {before,after:label(),builds};
}
"""


def _function_block(src: str, name: str) -> str:
    """Extract a real classic-JS function for the composed browser harness."""
    marker = re.search(rf"(^|\n)\s*function\s+{re.escape(name)}\(", src)
    assert marker is not None, f"{name}() not found"
    start = marker.start()
    brace = src.find("{", marker.end())
    assert brace != -1, f"{name}() opening brace not found"
    depth = 0
    quote = None
    escaped = False
    for index in range(brace, len(src)):
        char = src[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
    raise AssertionError(f"{name}() closing brace not found")


_FADE_PRODUCER_SCRIPT = "\n".join(
    [
        "const _anchorProseSmdCache=new Map();",
        "let _streamFadeReduceMotionMql=null;",
        "let _streamFadeReduceMotion=false;",
        "let _streamFadeReduceMotionOnChange=null;",
        # Rendering/animation and parser-bookkeeping adapters deliberately stay
        # plain. The policy, persistent prose producer and vendored parser below
        # are the unmodified production implementations.
        "function _streamFadeRenderer(el){return window.smd.default_renderer(el);}",
        "function _safeSmdRenderer(el){return window.smd.default_renderer(el);}",
        "function _smdRendererWithoutUnderscoreEmphasis(renderer){return renderer;}",
        "function _smdMediaTailFlush(){}",
        "function _smdMediaTailClear(){}",
        "function _sanitizeSmdLinks(){}",
        "function enhanceMarkdownTables(){}",
        *(
            _function_block(MESSAGES_JS, name)
            for name in (
                "_shouldUseStreamFade",
                "_shouldUseTransparentStreamFade",
                "_streamFadeReduceMotionEnabled",
                "_shouldUseLiveProseFade",
                "_smdBindParserIdentity",
                "_smdClearParserIdentity",
                "_finalizeAnchorProseIncrementalNode",
                "_anchorProseIncrementalNode",
            )
        ),
        "window.__anchorProseIncrementalNode=_anchorProseIncrementalNode;",
    ]
)
_FADE_SCENARIO = r"""
async ({mode,phase}) => {
  if(phase==='start'){
    const stream='oracle-fade-stream';
    Date.now=()=>1700000001000;
    window._chatActivityDisplayMode=mode;
    window._showThinking=true;
    window._simplifiedToolCalling=true;
    window._fadeTextEffect=true;
    S.session={session_id:'oracle-session',messages:[],pending_started_at:1700000000};
    S.messages=[]; S.toolCalls=[]; S.activeStreamId=stream;
    $('msgInner').innerHTML='';
    const turn=_createAssistantTurn(); turn.id='liveAssistantTurn';
    $('msgInner').appendChild(turn);
    const row={row_id:'prose:fade',local_id:'live-prose:fade',event_id:'event-fade',
      role:'prose',kind:'process_prose',source_event_type:'token',status:'running',
      text:'Fade policy **prose** stays stable',created_at:1700000000,
      payload:{text:'Fade policy **prose** stays stable'},identity:{event_id:'event-fade'}};
    const scene=()=>({version:'activity_scene_v1',mode,
      identity:{session_id:'oracle-session',stream_id:stream,run_id:stream},
      activity_rows:[JSON.parse(JSON.stringify(row))]});
    const select=()=>turn.querySelector('[data-anchor-row-id="prose:fade"]');
    window.__fadeOracle={mode,row,scene,select,first:null,firstBody:null,selected:''};
    const oracle=window.__fadeOracle;
    renderLiveAnchorActivityScene(stream,scene(),{sessionId:'oracle-session'});
    oracle.first=select(); oracle.firstBody=oracle.first.querySelector('.msg-body');
    const range=document.createRange(); range.selectNodeContents(oracle.firstBody);
    const selection=getSelection(); selection.removeAllRanges(); selection.addRange(range);
    oracle.selected=selection.toString();
    const initialPolicy=_shouldUseLiveProseFade();
    const initialFade=oracle.firstBody.classList.contains('stream-fade-active');
    const realParser=!!oracle.firstBody.querySelector('p strong')&&oracle.selected.includes('Fade policy');
    renderLiveAnchorActivityScene(stream,scene(),{sessionId:'oracle-session'});
    return {initialPolicy,initialFade,realParser,status:row.status,
      unchangedNode:select()===oracle.first,
      unchangedBody:select().querySelector('.msg-body')===oracle.firstBody,
      unchangedConnected:oracle.first.isConnected&&oracle.firstBody.isConnected,
      selectionPreserved:!!oracle.selected&&selection.toString()===oracle.selected};
  }
  const oracle=window.__fadeOracle;
  const stream=S.activeStreamId;
  renderLiveAnchorActivityScene(stream,oracle.scene(),{sessionId:'oracle-session'});
  const transitioned=oracle.select();
  const transitionedBody=transitioned.querySelector('.msg-body');
  const result={effectivePolicy:_shouldUseLiveProseFade(),status:oracle.row.status,
    fadeCleared:!transitionedBody.classList.contains('stream-fade-active'),
    producerRebuilt:transitionedBody!==oracle.firstBody,
    connected:transitioned.isConnected&&transitionedBody.isConnected};
  oracle.row.text+=' Neighbor text now changes.';
  oracle.row.payload.text=oracle.row.text;
  renderLiveAnchorActivityScene(stream,oracle.scene(),{sessionId:'oracle-session'});
  const neighbor=oracle.select(); const neighborBody=neighbor.querySelector('.msg-body');
  // A live parser legitimately holds the final punctuation as lookahead.
  result.textNeighbor=neighbor.textContent.includes('Neighbor text now changes');
  result.neighborConnected=neighbor.isConnected&&neighborBody.isConnected;
  result.neighborFadeCleared=!neighborBody.classList.contains('stream-fade-active');
  result.neighborStatus=oracle.row.status;
  getSelection().removeAllRanges();
  return result;
}
"""


def _oracle_page(browser, incremental):
    page = browser.new_page()
    page.set_default_timeout(600_000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(
        "**/*",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html" if route.request.url == "http://harness.test/" else "text/plain",
            body=HARNESS_HTML if route.request.url == "http://harness.test/" else "",
        ),
    )
    page.goto("http://harness.test/", wait_until="domcontentloaded")
    for script in SUPPORT_SCRIPTS:
        page.add_script_tag(content=script)
    page.add_script_tag(content=UI_JS)
    page.evaluate("enabled => { window._liveSceneIncremental=enabled; }", incremental)
    return page, errors


def _fade_oracle_page(browser):
    page, errors = _oracle_page(browser, True)
    page.route(
        "**/static/vendor/smd.min.js",
        lambda route: route.fulfill(
            status=200, content_type="application/javascript", body=SMD_JS
        ),
    )
    page.evaluate(
        "async () => { window.smd = await import('/static/vendor/smd.min.js'); }"
    )
    page.add_script_tag(content=_FADE_PRODUCER_SCRIPT)
    return page, errors


@pytest.fixture(scope="module")
def browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        instance = playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        yield instance
        instance.close()


@pytest.mark.parametrize("mode", ["compact_worklog", "transparent_stream"])
@pytest.mark.parametrize("incremental", [True, False], ids=["incremental", "legacy"])
def test_300_step_live_scene_matches_fresh_render(browser, mode, incremental):
    page, errors = _oracle_page(browser, incremental)
    try:
        result = page.evaluate(_SCENARIO, mode)
    finally:
        page.close()
    assert not errors, errors
    if not result.get("ok"):
        print(result)
    assert result.get("ok") is True, result
    assert result == {
        "ok": True,
        "mode": mode,
        "steps": 300,
        "rows": result["rows"],
        "uniqueRows": result["rows"],
        "disclosureRetained": True,
    }, result
    assert result["rows"] > 50, result


@pytest.mark.parametrize("mode", ["compact_worklog", "transparent_stream"])
@pytest.mark.parametrize("field", ["tool_result", "nested_argument"])
def test_middle_only_tool_correction_matches_fresh_render(browser, mode, field):
    page, errors = _oracle_page(browser, True)
    try:
        result = page.evaluate(_CORRECTION_SCENARIO, {"mode": mode, "field": field})
    finally:
        page.close()
    assert not errors, errors
    assert result["unchanged"] is True, result
    assert result["freshVisible"] is True, result
    assert result["correctedVisible"] is True, result
    assert result["freshMatches"] is True, result


@pytest.mark.parametrize("mode", ["compact_worklog", "transparent_stream"])
def test_non_tool_status_only_change_renders(browser, mode):
    page, errors = _oracle_page(browser, True)
    try:
        result = page.evaluate(_STATUS_SCENARIO, mode)
    finally:
        page.close()
    assert not errors, errors
    assert result == {"before": "running", "after": "completed", "builds": 1}, result


@pytest.mark.parametrize(
    "mode,transition",
    [
        ("compact_worklog", "fade_setting"),
        ("compact_worklog", "reduced_motion"),
        ("transparent_stream", "reduced_motion"),
    ],
)
def test_unchanged_prose_observes_effective_fade_policy(browser, mode, transition):
    """Composed renderer + real policy/producer/parser; no text/status mutation."""
    page, errors = _fade_oracle_page(browser)
    try:
        initial = page.evaluate(_FADE_SCENARIO, {"mode": mode, "phase": "start"})
        assert initial == {
            "initialPolicy": True,
            "initialFade": True,
            "realParser": True,
            "status": "running",
            "unchangedNode": True,
            "unchangedBody": True,
            "unchangedConnected": True,
            "selectionPreserved": True,
        }, initial
        if transition == "reduced_motion":
            page.emulate_media(reduced_motion="reduce")
        else:
            page.evaluate("window._fadeTextEffect=false")
        page.wait_for_function("() => _shouldUseLiveProseFade() === false")
        result = page.evaluate(_FADE_SCENARIO, {"mode": mode, "phase": "after"})
    finally:
        page.close()
    assert not errors, errors
    assert result == {
        "effectivePolicy": False,
        "status": "running",
        "fadeCleared": True,
        "producerRebuilt": True,
        "connected": True,
        "textNeighbor": True,
        "neighborConnected": True,
        "neighborFadeCleared": True,
        "neighborStatus": "running",
    }, result
