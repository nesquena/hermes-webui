from pathlib import Path
import pytest
import shutil
import subprocess
import textwrap

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    start = src.index(f"function {name}")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name} body not found")


def _event_listener_body(src: str, event_name: str) -> str:
    needle = f"source.addEventListener('{event_name}'"
    start = src.index(needle)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"event listener {event_name!r} body not found")


def test_terminal_done_render_preserves_manual_scroll_after_active_stream_is_cleared():
    done_block = _event_listener_body(MESSAGES_JS, "done")

    clear_idx = done_block.index("S.activeStreamId=null")
    render_idx = done_block.index("renderMessages({preserveScroll:true})")

    assert clear_idx < render_idx, (
        "the done handler should clear stream liveness before the final render, "
        "but must pass preserveScroll so renderMessages does not infer bottom-pin "
        "from S.activeStreamId alone"
    )


def test_terminal_done_removes_idle_live_turn_after_settled_render():
    done_block = _event_listener_body(MESSAGES_JS, "done")

    final_render_idx = done_block.index(
        "if(typeof _renderMessagesWithScrollSnapshot==='function') "
        "_renderMessagesWithScrollSnapshot({_prescrollSnapshot:_doneLiveScrollSnapshot});"
    )
    cleanup_idx = done_block.index("_removeIdleLiveAssistantTurn?.(activeSid,streamId)")

    assert final_render_idx < cleanup_idx, (
        "the done handler must remove any stale #liveAssistantTurn only after "
        "the final settled render has put the completed assistant message in the DOM"
    )


def test_delayed_done_bails_before_mutating_newer_active_stream():
    done_block = _event_listener_body(MESSAGES_JS, "done")

    owner_guard_idx = done_block.index("_streamDoneWouldOverwriteNewerPane(activeSid,streamId)")
    assert owner_guard_idx < done_block.index("_applyToAnchor('done'")
    assert owner_guard_idx < done_block.index("_clearOwnerInflightState()")
    assert owner_guard_idx < done_block.index("S.activeStreamId=null")
    assert owner_guard_idx < done_block.index("S.session=d.session")
    assert owner_guard_idx < done_block.index("_removeIdleLiveAssistantTurn?.(activeSid,streamId)")


def test_done_owner_gate_rejects_same_pane_newer_stream():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")

    helper = _function_body(MESSAGES_JS, "_streamDoneWouldOverwriteNewerPane")
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        let S = {{activeStreamId:'stream-old', session:{{session_id:'sid-1'}}}};
        let INFLIGHT = {{'sid-1': {{streamId:'stream-old'}}}};
        function _isSessionCurrentPane(sid){{ return S.session && S.session.session_id === sid; }}
        {helper}

        assert.strictEqual(_streamDoneWouldOverwriteNewerPane('sid-1','stream-old'), false);
        S.activeStreamId = 'stream-new';
        assert.strictEqual(_streamDoneWouldOverwriteNewerPane('sid-1','stream-old'), true);
        S.activeStreamId = 'stream-old';
        INFLIGHT['sid-1'] = {{streamId:'stream-new'}};
        assert.strictEqual(_streamDoneWouldOverwriteNewerPane('sid-1','stream-old'), true);
        S.session = {{session_id:'sid-2'}};
        assert.strictEqual(_streamDoneWouldOverwriteNewerPane('sid-1','stream-old'), false);
        """
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize(
    "scenario",
    [
        "same_session_new_stream",
        "origin_continuation_new_stream",
        "session_switch",
    ],
)
def test_done_postprocess_raf_rechecks_stream_owner_before_mutating(scenario):
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")

    script = textwrap.dedent(
        r"""
        const assert = require('assert');
        const fs = require('fs');
        const messagesSrc = fs.readFileSync('static/messages.js', 'utf8');
        const scenario = '__SCENARIO__';

        class FakeClassList {
          constructor(el){ this.el = el; this.set = new Set(); }
          add(...names){ for(const name of names) this.set.add(name); this.el.className = Array.from(this.set).join(' '); }
          remove(...names){ for(const name of names) this.set.delete(name); this.el.className = Array.from(this.set).join(' '); }
          contains(name){ return this.set.has(name); }
        }
        class FakeElement {
          constructor(tag){
            this.tagName = tag;
            this.children = [];
            this.attributes = {};
            this.dataset = {};
            this.style = {};
            this.parentElement = null;
            this.isConnected = true;
            this.textContent = '';
            this.innerHTML = '';
            this.className = '';
            this.classList = new FakeClassList(this);
          }
          setAttribute(name, value){
            this.attributes[name] = String(value);
            if(name === 'id') this.id = String(value);
            if(name === 'class') this.className = String(value);
            if(name.startsWith('data-')){
              const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
              this.dataset[key] = String(value);
            }
          }
          getAttribute(name){ return this.attributes[name] ?? null; }
          appendChild(child){ child.parentElement = this; this.children.push(child); return child; }
          remove(){ this.isConnected = false; if(this.parentElement){ this.parentElement.children = this.parentElement.children.filter(c => c !== this); } }
          querySelector(){ return null; }
          querySelectorAll(){ return []; }
        }

        const rafQueue = [];
        const timeoutQueue = [];
        global.window = {
          _removeIdleLiveAssistantTurnCalls: [],
          _removeIdleLiveAssistantTurn(sid, streamId){ this._removeIdleLiveAssistantTurnCalls.push([sid, streamId]); },
          _streamJustFinished: false,
          addEventListener(){},
          removeEventListener(){},
        };
        global.document = {
          baseURI: 'http://test.local/',
          hidden: false,
          visibilityState: 'visible',
          hasFocus: () => true,
          wasDiscarded: false,
          createElement: tag => new FakeElement(tag),
          createTextNode: text => { const n = new FakeElement('#text'); n.textContent = String(text); return n; },
          querySelector: () => null,
          addEventListener(){},
          removeEventListener(){},
        };
        global.location = {href:'http://test.local/'};
        global.performance = {now: () => 1000};
        global.requestAnimationFrame = cb => { rafQueue.push(cb); return rafQueue.length; };
        global.cancelAnimationFrame = () => {};
        global.setTimeout = (cb, _ms) => { timeoutQueue.push(cb); return timeoutQueue.length; };
        global.clearTimeout = () => {};

        const emptyState = new FakeElement('div');
        const turn = new FakeElement('div');
        turn.id = 'liveAssistantTurn';
        turn.dataset.sessionId = 'sid-1';
        const blocks = new FakeElement('div');
        turn.appendChild(blocks);
        const byId = {emptyState, liveAssistantTurn: turn, msgInner: new FakeElement('div')};
        global.$ = id => byId[id] || null;
        global._assistantTurnBlocks = () => blocks;
        global.appendThinking = () => {};
        global.closeOtherLiveStreams = () => {};
        global.closeLiveStream = () => {};
        global.resetTurnWorkspaceMutations = () => {};
        global._resetStreamScrollFollow = () => {};
        global._suspendSessionStreamForLiveChat = () => {};
        global._bindStreamHiddenTracker = () => {};
        global._shouldUseLiveProseFade = () => false;
        global._isSessionCurrentPane = sid => S.session && S.session.session_id === sid;
        global._isDocumentVisibleAndFocused = () => true;
        global._isSessionActivelyViewed = sid => S.session && S.session.session_id === sid;
        global._streamDoneWouldOverwriteNewerPane = function(activeSid, streamId){
          if(!_isSessionCurrentPane(activeSid)) return false;
          const expectedStreamId = String(streamId || '');
          const activeStreamId = String(S && S.activeStreamId || '');
          if(activeStreamId && activeStreamId !== expectedStreamId) return true;
          const inflight = INFLIGHT && INFLIGHT[activeSid];
          const inflightStreamId = String(inflight && inflight.streamId || '');
          return !!(inflightStreamId && inflightStreamId !== expectedStreamId);
        };
        global._stripXmlToolCalls = value => String(value || '');
        global._extractInlineThinkingFromContent = (content, reasoning) => {
          const visible = String(content || '');
          const thinking = String(reasoning || '');
          return {content:visible, reasoning:thinking, thinkingText:thinking, displayText:visible, inThinking:false};
        };
        global._splitThinkFromContent = (content, reasoning) => ({content:String(content || ''), reasoning:String(reasoning || '')});
        global._captureMessageScrollSnapshot = () => ({});
        global._renderMessagesWithScrollSnapshot = () => { renderCount += 1; };
        global.renderMessages = () => { renderCount += 1; };
        global.syncTopbar = () => {};
        global.renderSessionList = () => {};
        global.setBusy = value => { S.busy = !!value; };
        global.setComposerStatus = () => {};
        global.setStatus = () => {};
        global.clearInflightState = () => {};
        global.clearInflight = () => {};
        global._resumeSessionStreamAfterLiveChat = () => {};
        global.clearLiveToolCards = () => {};
        global.removeThinking = () => {};
        global.finalizeThinkingCard = () => {};
        global._clearApprovalPendingForSession = () => {};
        global._clearClarifyPendingForSession = () => {};
        global.stopApprovalPolling = () => {};
        global.stopClarifyPolling = () => {};
        global.hideApprovalCard = () => {};
        global.hideClarifyCard = () => {};
        global._markSessionViewed = () => {};
        global._markSessionCompletionUnread = () => {};
        global._markSessionCompletedInList = () => {};
        global.playNotificationSound = () => {};
        global.sendBrowserNotification = () => {};
        global._shouldForceCompletionNotification = () => false;
        global._completionNotificationPreviewText = () => '';
        global.scrollIfPinned = () => {};
        global.scrollToBottom = () => {};
        global._shouldFollowMessagesOnDomReplace = () => false;
        global._armKeepSettledWorklogOpen = () => {};
        global._disarmKeepSettledWorklogOpen = () => {};
        global.noteWorkspaceMutationsFromToolCalls = () => {};
        global.loadDir = () => {};
        global.renderSessionArtifacts = () => {};
        global._hydrateTodosFromSession = () => {};
        global.clearVisibleMessageRowCache = () => {};
        global.saveInflightState = () => {};
        global.snapshotLiveTurnHtmlForSession = () => {};
        global._syncCtxIndicator = () => {};
        global._mergeUsageForCtxIndicator = (a, b) => ({...(b || {}), ...(a || {})});
        global.updateQueueBadge = () => {};
        global._setActiveSessionUrl = () => {};
        global.localStorage = {setItem(){}, getItem(){ return null; }};
        global.renderMd = text => String(text || '');
        global.esc = text => String(text || '');
        global.enhanceMarkdownTables = () => {};
        global.addCopyButtons = () => { calls.copy += 1; };
        global.highlightCode = () => { calls.highlight += 1; };
        global.renderKatexBlocks = () => { calls.katex += 1; };

        const calls = {highlight:0, copy:0, katex:0};
        let renderCount = 0;
        const S = global.S = {
          session: {session_id:'sid-1', message_count:1, pending_started_at:1},
          messages: [{role:'user', content:'question'}],
          toolCalls: [],
          activeStreamId: 'stream-old',
          busy: true,
        };
        const INFLIGHT = global.INFLIGHT = {};
        const LIVE_STREAMS = global.LIVE_STREAMS = {};
        global._STREAM_WAS_HIDDEN = {};
        global._STREAM_NOTIFICATION_BACKGROUND = {};
        global._desktopBackgroundedForNotifications = false;
        global._approvalSessionId = null;
        global._clarifySessionId = null;

        class FakeEventSource {
          static instances = [];
          static OPEN = 1;
          static CONNECTING = 0;
          constructor(){ this.listeners = {}; this.readyState = 1; FakeEventSource.instances.push(this); }
          addEventListener(name, fn){ (this.listeners[name] ||= []).push(fn); }
          emit(name, data){ for(const fn of this.listeners[name] || []) fn({data:JSON.stringify(data), lastEventId:`stream-old:2`}); }
          close(){ this.readyState = 2; }
        }
        global.EventSource = FakeEventSource;

        const attachStart = messagesSrc.indexOf('function attachLiveStream(');
        const attachEnd = messagesSrc.indexOf('\nfunction transcript(){', attachStart);
        if(attachStart < 0 || attachEnd < 0) throw new Error('attachLiveStream source boundary not found');
        eval(messagesSrc.slice(attachStart, attachEnd));

        attachLiveStream('sid-1', 'stream-old');
        const source = FakeEventSource.instances[0];
        source.emit('token', {text:'partial'});
        while(rafQueue.length) rafQueue.shift()();
        assert.strictEqual(blocks.children.length, 1, 'token should create a live assistant segment');
        rafQueue.length = 0;
        const doneSessionId = scenario === 'origin_continuation_new_stream' ? 'sid-cont' : 'sid-1';
        const doneSession = {
          session_id: doneSessionId,
          message_count:2,
          messages:[
            {role:'user', content:'question'},
            {role:'assistant', content:'final answer', timestamp: 2},
          ],
        };
        if(scenario === 'origin_continuation_new_stream') doneSession.parent_session_id = 'sid-1';
        source.emit('done', {
          session: {
            ...doneSession,
          },
          usage:{duration_seconds:1},
        });
        assert.ok(rafQueue.length > 0, 'done should schedule postprocess RAF for the live assistant body');
        if(scenario === 'session_switch'){
          S.session = {session_id:'sid-2', message_count:1};
          S.messages = [{role:'user', content:'next question'}];
          S.activeStreamId = 'stream-new';
          INFLIGHT['sid-2'] = {streamId:'stream-new'};
        }else{
          S.activeStreamId = 'stream-new';
          INFLIGHT[S.session.session_id] = {streamId:'stream-new'};
        }
        INFLIGHT['sid-1'] = {streamId:'stream-new'};
        while(rafQueue.length) rafQueue.shift()();

        assert.strictEqual(calls.highlight, 0, 'old done RAF must not highlight after owner rotation');
        assert.strictEqual(calls.copy, 0, 'old done RAF must not add copy buttons after owner rotation');
        assert.strictEqual(calls.katex, 0, 'old done RAF must not render KaTeX after owner rotation');
        assert.strictEqual(S.activeStreamId, 'stream-new');
        process.stdout.write(JSON.stringify({calls, renderCount, activeStreamId:S.activeStreamId}));
        """
    ).replace("__SCENARIO__", scenario)
    result = subprocess.run([node, "-e", script], cwd=REPO, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout


def test_remove_idle_live_assistant_turn_is_session_and_liveness_guarded():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")

    helper = _function_body(UI_JS, "_removeIdleLiveAssistantTurn")
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        let S = {{activeStreamId:null, session:{{session_id:'sid-1'}}}};
        let INFLIGHT = {{}};
        let _sessionHtmlCacheSid = 'sid-1';
        const cacheDeletes = [];
        const _sessionHtmlCache = {{delete: sid => cacheDeletes.push(sid)}};
        let currentTurn = null;
        function $(id){{ return id === 'liveAssistantTurn' ? currentTurn : null; }}
        {helper}
        function turn(sid, streamId){{
          return {{
            dataset: {{sessionId: sid}},
            getAttribute(name){{ return name === 'data-anchor-stream-id' ? streamId : ''; }},
            removed: false,
            remove(){{ this.removed = true; }},
          }};
        }}

        currentTurn = turn('sid-1','stream-1');
        assert.strictEqual(_removeIdleLiveAssistantTurn('sid-1','stream-1'), true);
        assert.strictEqual(currentTurn.removed, true);
        assert.deepStrictEqual(cacheDeletes, ['sid-1']);
        assert.strictEqual(_sessionHtmlCacheSid, null);

        currentTurn = turn('sid-1','stream-1');
        S.activeStreamId = 'stream-1';
        assert.strictEqual(_removeIdleLiveAssistantTurn('sid-1','stream-1'), false);
        assert.strictEqual(currentTurn.removed, false);

        S.activeStreamId = null;
        INFLIGHT = {{'sid-1': {{streamId:'stream-1'}}}};
        assert.strictEqual(_removeIdleLiveAssistantTurn('sid-1','stream-1'), false);
        assert.strictEqual(currentTurn.removed, false);

        INFLIGHT = {{}};
        currentTurn = turn('sid-2','stream-1');
        assert.strictEqual(_removeIdleLiveAssistantTurn('sid-1','stream-1'), false);
        assert.strictEqual(currentTurn.removed, false);

        currentTurn = turn('sid-1','stream-2');
        assert.strictEqual(_removeIdleLiveAssistantTurn('sid-1','stream-1'), false);
        assert.strictEqual(currentTurn.removed, false);

        currentTurn = turn('sid-1','');
        assert.strictEqual(_removeIdleLiveAssistantTurn('sid-1','stream-1'), false);
        assert.strictEqual(currentTurn.removed, false);
        """
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout


def test_render_messages_preserve_scroll_option_uses_user_pin_state_not_stream_liveness():
    render_body = _function_body(UI_JS, "renderMessages")
    scroll_helper = _function_body(UI_JS, "_scrollAfterMessageRender")
    follow_helper = _function_body(UI_JS, "_followMessagesAfterDomReplace")

    assert "function renderMessages(options)" in render_body
    assert "const preserveScroll=!!(options&&options.preserveScroll);" in render_body
    assert "_scrollAfterMessageRender(preserveScroll, scrollSnapshot);" in render_body
    assert "const scrollSnapshot=(preserveScroll||_messageUserUnpinned)?_captureMessageScrollSnapshot():null" in render_body
    assert "if(preserveScroll){" in scroll_helper
    # #4124: a reader clearly away from the bottom (>250px) is treated as an active
    # reading position, so the forced follow-to-bottom is gated behind it.
    assert "const readerAwayFromBottom=" in scroll_helper
    assert "Number(scrollSnapshot.bottom)>250" in scroll_helper
    assert "// Keep master's follow heuristic" in scroll_helper
    assert "if(!readerAwayFromBottom && !_messageUserUnpinned && _followMessagesAfterDomReplace()) return;\n    _restoreMessageScrollSnapshot(scrollSnapshot);\n    _maybeShowNewMessageScrollCue(scrollSnapshot);\n    return;\n  }" in scroll_helper
    assert "_shouldFollowMessagesOnDomReplace()" in follow_helper
    assert "scrollToBottom();" in follow_helper
    # Mid-stream re-render branch (issue: wipe-rows0 scrollHeight-collapse jump-back).
    # An unpinned reader (scrolled up into history) must have their pre-wipe viewport
    # RESTORED — scrollIfPinned() is a no-op for the unpinned case and cannot undo the
    # browser's scrollTop clamp from the inner.innerHTML='' wipe, so it stranded the
    # reader at the top. Pinned/tail-following readers still take scrollIfPinned().
    assert "if(S.activeStreamId){" in scroll_helper
    assert "if(_messageUserUnpinned && scrollSnapshot){\n      _restoreMessageScrollSnapshot(scrollSnapshot);\n      _maybeShowNewMessageScrollCue(scrollSnapshot);\n      return;\n    }\n    scrollIfPinned();\n    return;\n  }" in scroll_helper


def test_cached_render_path_uses_same_scroll_policy_as_fresh_render():
    render_body = _function_body(UI_JS, "renderMessages")
    cached_branch = render_body[render_body.index("if(sid&&sid!==_sessionHtmlCacheSid") : render_body.index("const compressionState=")]

    assert "_scrollAfterMessageRender(preserveScroll, scrollSnapshot);" in cached_branch
    assert "if(S.activeStreamId){scrollIfPinned();}else{scrollToBottom();}" not in cached_branch


def test_session_switch_and_idle_session_load_keep_default_bottom_pin_behavior():
    load_session = _function_body(SESSIONS_JS, "loadSession")
    idle_branch = load_session[load_session.index("}else{\n      S.busy=false;") : load_session.index("// Sync context usage indicator")]

    # #3326: the idle branch now renders with a CONDITIONAL preserveScroll —
    # `renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined)`.
    # For a normal cross-session idle load (currentSid!==sid) sameSessionForceReload
    # is false, so the arg is undefined and the default bottom-pin behavior (#1690)
    # is unchanged. preserveScroll only applies to a same-session external
    # force-refresh (#3239), which is a different code path from #1690's scenario.
    assert "syncTopbar();renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined);" in idle_branch
    # The idle path must NOT unconditionally preserveScroll — it stays bottom-pinned
    # for cross-session loads. Guard against a regression to an always-on preserve.
    assert "renderMessages({preserveScroll:true})" not in idle_branch
