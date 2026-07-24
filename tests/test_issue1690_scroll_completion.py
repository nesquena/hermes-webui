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
    paren = src.index("(", start)
    depth = 0
    signature_end = -1
    for i in range(paren, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                signature_end = i
                break
    assert signature_end >= 0, f"function {name} signature not found"
    brace = src.index("{", signature_end)
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


def test_done_owner_pane_false_pending_cleanup_is_delete_only():
    approval_owner = _function_body(MESSAGES_JS, "_clearApprovalForOwner")
    clarify_owner = _function_body(MESSAGES_JS, "_clearClarifyForOwner")
    approval_clear = _function_body(MESSAGES_JS, "_clearApprovalPendingForSession")
    clarify_clear = _function_body(MESSAGES_JS, "_clearClarifyPendingForSession")

    assert "_clearApprovalPendingForSession(activeSid,{sync:false})" in approval_owner
    assert "_clearClarifyPendingForSession(activeSid,{sync:false})" in clarify_owner
    assert "options&&options.sync===false" in approval_clear
    assert "options&&options.sync===false" in clarify_clear


@pytest.mark.parametrize(
    "scenario",
    [
        "same_session_new_stream",
        "origin_continuation_new_stream",
        "loading_only_session_switch",
        "session_switch",
        "completed_session_switch",
        "stream_just_finished_timer_owner_rotation",
        "stream_just_finished_replacement_without_done",
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
        const vm = require('vm');
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
            this.style = {setProperty(){}};
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
          matchMedia(){ return {matches:false, addEventListener(){}, removeEventListener(){}, addListener(){}, removeListener(){}}; },
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
          createDocumentFragment: () => new FakeElement('#fragment'),
          getElementById: id => byId[id] || null,
          querySelector: () => null,
          addEventListener(){},
          removeEventListener(){},
        };
        global.location = {href:'http://test.local/'};
        global.performance = {now: () => 1000};
        global.requestAnimationFrame = cb => { rafQueue.push(cb); return rafQueue.length; };
        global.cancelAnimationFrame = () => {};
        global.setTimeout = (cb, ms) => { timeoutQueue.push({cb, ms}); return timeoutQueue.length; };
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
        global._shouldUseLiveProseFade = () => !scenario.startsWith('stream_just_finished_');
        global._shouldUseTransparentStreamFade = () => false;
        global._isDocumentVisibleAndFocused = () => true;
        global._isSessionActivelyViewed = sid => S.session && S.session.session_id === sid;
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
        global.syncTopbar = () => { calls.syncTopbar += 1; };
        const orderLog = [];
        global.api = (_url, opts) => {
          if(opts && opts.body){
            try{
              const entry = JSON.parse(opts.body);
              calls.anchorPersist.push(entry);
              const scene = entry && entry.scene;
              const terminalPersisted = !!(scene && scene.lifecycle && scene.lifecycle.terminal_state === 'completed');
              orderLog.push({
                type: terminalPersisted ? 'anchorPersistTerminal' : 'anchorPersist',
                ownerInflightPresent: INFLIGHT['sid-1'] !== undefined,
                approvalPresent: global._approvalPendingBySession.has('sid-1'),
                clarifyPresent: global._clarifyPendingBySession.has('sid-1'),
              });
            }catch(_){}
          }
          return Promise.resolve({});
        };
        global.renderSessionList = () => { calls.renderSessionList += 1; };
        global.setBusy = value => { calls.setBusy.push(value); S.busy = !!value; };
        global.setComposerStatus = value => { calls.composerStatus.push(value); };
        global.setStatus = value => { calls.status.push(value); };
        global.clearInflightState = sid => { calls.clearInflightState.push(sid); orderLog.push({type:'clearInflightState', sid}); };
        global.clearInflight = () => { calls.clearInflight += 1; };
        global._resumeSessionStreamAfterLiveChat = () => {};
        global.clearLiveToolCards = () => {};
        global.removeThinking = () => {};
        global.finalizeThinkingCard = () => { calls.finalizeThinking += 1; };
        global.stopApprovalPolling = () => { calls.stopApproval += 1; };
        global.stopClarifyPolling = () => { calls.stopClarify += 1; };
        global.hideApprovalCard = () => { calls.hideApproval += 1; };
        global.hideClarifyCard = () => { calls.hideClarify += 1; };
        global._markSessionViewed = () => {};
        global._markSessionCompletionUnread = () => {};
        global._markSessionCompletedInList = () => {};
        global.playNotificationSound = () => { calls.playNotification += 1; };
        global.sendBrowserNotification = () => { calls.browserNotification += 1; };
        global._shouldForceCompletionNotification = () => false;
        global._completionNotificationPreviewText = () => '';
        global.scrollIfPinned = () => { calls.scrollIfPinned += 1; };
        global.scrollToBottom = () => { calls.scrollToBottom += 1; };
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
        global._loadingSessionId = null;

        const calls = {
          highlight:0,
          copy:0,
          katex:0,
          setBusy:[],
          composerStatus:[],
          status:[],
          clearInflight:0,
          clearInflightState:[],
          clearApprovalPending:[],
          clearClarifyPending:[],
          syncTopbar:0,
          scrollIfPinned:0,
          scrollToBottom:0,
          finalizeThinking:0,
          stopApproval:0,
          stopClarify:0,
          hideApproval:0,
          hideClarify:0,
          renderSessionList:0,
          playNotification:0,
          browserNotification:0,
          anchorEvents:[],
          anchorPersist:[],
        };
        let renderCount = 0;
        const S = global.S = {
          session: {session_id:'sid-1', message_count:1, pending_started_at:1},
          messages: [{role:'user', content:'question'}],
          toolCalls: [],
          activeStreamId: 'stream-old',
          busy: true,
        };
        const originalSession = S.session;
        const originalMessages = S.messages;
        const INFLIGHT = global.INFLIGHT = {};
        const LIVE_STREAMS = global.LIVE_STREAMS = {};
        global._STREAM_WAS_HIDDEN = {};
        global._STREAM_NOTIFICATION_BACKGROUND = {};
        global._desktopBackgroundedForNotifications = false;
        global._approvalSessionId = null;
        global._clarifySessionId = null;
        global._approvalPendingBySession = new Map([
          ['sid-1', {pending:{_session_id:'sid-1', id:'approval-old'}, pendingCount:1}],
          ['sid-control', {pending:{_session_id:'sid-control', id:'approval-control'}, pendingCount:1}],
        ]);
        global._clarifyPendingBySession = new Map([
          ['sid-1', {pending:{_session_id:'sid-1', id:'clarify-old'}}],
          ['sid-control', {pending:{_session_id:'sid-control', id:'clarify-control'}}],
        ]);
        window.HermesAssistantTurnAnchors = {
          createAssistantTurnAnchorRegistry(){
            return {anchor:{activity_events:[], lifecycle:{}, identity:{session_id:'sid-1', stream_id:'stream-old'}}};
          },
          applyAssistantTurnAnchorSourceEvent(registry, event){
            calls.anchorEvents.push({...event});
            const sourceType = String(event && event.source_event_type || '');
            if(sourceType === 'done'){
              registry.anchor.lifecycle = {status:'completed', terminal_state:'completed'};
              registry.anchor.activity_events.push({
                row_id:'done',
                role:'terminal',
                kind:'terminal_status',
                source_event_type:'done',
              });
            }else if(sourceType === 'token'){
              registry.anchor.activity_events.push({
                row_id:'token',
                role:'prose',
                kind:'process_prose',
                source_event_type:'token',
                text:String(event && event.text || ''),
              });
            }
            return {applied:true};
          },
          projectAssistantTurnAnchorActivityScene(registry){
            const hasTerminal = (registry.anchor.activity_events || []).some(row => row && row.role === 'terminal');
            return {
              version:'activity_scene_v1',
              mode:'compact_worklog',
              identity:{session_id:'sid-1', stream_id:'stream-old'},
              lifecycle:{...(registry.anchor.lifecycle || {})},
              terminal_state:(registry.anchor.lifecycle && registry.anchor.lifecycle.terminal_state) || null,
              activity_rows:hasTerminal ? [...(registry.anchor.activity_events || [])] : [],
            };
          },
        };

        class FakeEventSource {
          static instances = [];
          static OPEN = 1;
          static CONNECTING = 0;
          constructor(){ this.listeners = {}; this.readyState = 1; FakeEventSource.instances.push(this); }
          addEventListener(name, fn){ (this.listeners[name] ||= []).push(fn); }
          emit(name, data){ for(const fn of this.listeners[name] || []) fn({data:JSON.stringify(data), lastEventId:`stream-old:2`}); }
          close(){
            this.readyState = 2;
            orderLog.push({
              type:'sourceClose',
              ownerInflightPresent: INFLIGHT['sid-1'] !== undefined,
              approvalPresent: global._approvalPendingBySession.has('sid-1'),
              clarifyPresent: global._clarifyPendingBySession.has('sid-1'),
              terminalPersisted: calls.anchorPersist.some(entry => {
                const scene = entry && entry.scene;
                return !!(scene && scene.lifecycle && scene.lifecycle.terminal_state === 'completed');
              }),
            });
          }
        }
        global.EventSource = FakeEventSource;

        const attachStart = messagesSrc.indexOf('function _isSessionCurrentPane(');
        const attachEnd = messagesSrc.indexOf('\nfunction transcript(){', attachStart);
        if(attachStart < 0 || attachEnd < 0) throw new Error('attachLiveStream source boundary not found');
        function extractFunctionSource(src, name){
          const start = src.indexOf(`function ${name}`);
          if(start < 0) throw new Error(`${name} source not found`);
          const paren = src.indexOf('(', start);
          let parenDepth = 0;
          let signatureEnd = -1;
          for(let i = paren; i < src.length; i += 1){
            if(src[i] === '(') parenDepth += 1;
            else if(src[i] === ')'){
              parenDepth -= 1;
              if(parenDepth === 0){
                signatureEnd = i;
                break;
              }
            }
          }
          if(signatureEnd < 0) throw new Error(`${name} signature not found`);
          const brace = src.indexOf('{', signatureEnd);
          let depth = 0;
          for(let i = brace; i < src.length; i += 1){
            if(src[i] === '{') depth += 1;
            else if(src[i] === '}'){
              depth -= 1;
              if(depth === 0) return src.slice(start, i + 1);
            }
          }
          throw new Error(`${name} source boundary not found`);
        }
        const context = vm.createContext(global);
        vm.runInContext(extractFunctionSource(messagesSrc, '_clearApprovalPendingForSession'), context, {filename:'static/messages.js'});
        vm.runInContext(extractFunctionSource(messagesSrc, '_clearClarifyPendingForSession'), context, {filename:'static/messages.js'});
        vm.runInContext(messagesSrc.slice(attachStart, attachEnd), context, {filename:'static/messages.js'});
        const attachLiveStream = context.attachLiveStream;

        function takeCooldownTimer(){
          const index = timeoutQueue.findIndex(timer => timer && timer.ms === 5000);
          assert.notStrictEqual(index, -1, 'expected a stream-finished cooldown timer');
          return timeoutQueue.splice(index, 1)[0].cb;
        }
        function drainQueuedWork(){
          let guard = 0;
          while((timeoutQueue.length || rafQueue.length) && guard++ < 100){
            while(timeoutQueue.length) timeoutQueue.shift().cb();
            while(rafQueue.length) rafQueue.shift()();
          }
          assert.ok(guard < 100, 'queued callbacks should drain');
        }
        function resetMutationCalls(){
          calls.highlight = 0;
          calls.copy = 0;
          calls.katex = 0;
          calls.setBusy = [];
          calls.composerStatus = [];
          calls.status = [];
          calls.clearInflight = 0;
          calls.clearInflightState = [];
          calls.clearApprovalPending = [];
          calls.clearClarifyPending = [];
          calls.syncTopbar = 0;
          calls.scrollIfPinned = 0;
          calls.scrollToBottom = 0;
          calls.finalizeThinking = 0;
          calls.stopApproval = 0;
          calls.stopClarify = 0;
          calls.hideApproval = 0;
          calls.hideClarify = 0;
          calls.renderSessionList = 0;
          calls.playNotification = 0;
          calls.browserNotification = 0;
          renderCount = 0;
          window._removeIdleLiveAssistantTurnCalls = [];
        }

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
        if(scenario === 'loading_only_session_switch'){
          global._loadingSessionId = 'sid-2';
          context._loadingSessionId = 'sid-2';
          resetMutationCalls();
        }
        source.emit('done', {
          session: {
            ...doneSession,
          },
          usage:{duration_seconds:1},
        });
        if(scenario === 'stream_just_finished_timer_owner_rotation' || scenario === 'stream_just_finished_replacement_without_done'){
          assert.strictEqual(window._streamJustFinished, true, 'active done should arm the external-refresh cooldown');
          const oldOwnerKey = window._streamJustFinishedOwner;
          assert.ok(oldOwnerKey, 'active done should record the cooldown owner key');
          assert.ok(timeoutQueue.length > 0, 'active done should queue a cooldown timer');
          const oldDoneTimer = takeCooldownTimer();
          S.activeStreamId = 'stream-new';
          attachLiveStream('sid-1', 'stream-new');
          assert.strictEqual(window._streamJustFinished, true, 'new owner attach must not clear the old cooldown synchronously');
          if(scenario === 'stream_just_finished_replacement_without_done'){
            oldDoneTimer();
            assert.strictEqual(window._streamJustFinished, false, 'old cooldown timer must eventually clear its own grace when the replacement has no DONE');
            assert.strictEqual(window._streamJustFinishedOwner, undefined, 'old cooldown owner token should be retired when no newer DONE supersedes it');
            process.stdout.write(JSON.stringify({streamJustFinished:window._streamJustFinished}));
            process.exit(0);
          }
          const newerSource = FakeEventSource.instances[FakeEventSource.instances.length - 1];
          newerSource.emit('done', {
            session: {
              session_id:'sid-1',
              message_count:3,
              messages:[
                {role:'user', content:'question'},
                {role:'assistant', content:'first final', timestamp:2},
                {role:'assistant', content:'new final', timestamp:3},
              ],
            },
            usage:{duration_seconds:1},
          });
          const newerOwnerKey = window._streamJustFinishedOwner;
          assert.ok(newerOwnerKey, 'newer done should record its own cooldown owner key');
          assert.notStrictEqual(newerOwnerKey, oldOwnerKey, 'newer same-session done should use a distinct owner key');
          assert.ok(timeoutQueue.length > 0, 'newer done should queue its own cooldown timer');
          const newerDoneTimer = takeCooldownTimer();
          oldDoneTimer();
          assert.strictEqual(window._streamJustFinished, true, 'old cooldown timer must not clear newer done grace');
          assert.strictEqual(window._streamJustFinishedOwner, newerOwnerKey, 'old cooldown timer must preserve newer owner key');
          newerDoneTimer();
          assert.strictEqual(window._streamJustFinished, false, 'newer cooldown timer must clear its own flag');
          assert.strictEqual(window._streamJustFinishedOwner, undefined, 'newer cooldown owner token should be retired');
          process.stdout.write(JSON.stringify({streamJustFinished:window._streamJustFinished}));
          process.exit(0);
        }
        if(scenario !== 'loading_only_session_switch'){
          assert.ok(timeoutQueue.length > 0, 'fade-enabled done should remain pending behind a timeout');
        }
        if(scenario === 'loading_only_session_switch'){
          drainQueuedWork();
          assert.strictEqual(calls.highlight, 0, 'loading-only old done must not highlight the transitioning pane');
          assert.strictEqual(calls.copy, 0, 'loading-only old done must not add copy buttons to the transitioning pane');
          assert.strictEqual(calls.katex, 0, 'loading-only old done must not render KaTeX in the transitioning pane');
          assert.deepStrictEqual(calls.setBusy, [], 'loading-only old done must not clear busy state');
          assert.deepStrictEqual(calls.composerStatus, [], 'loading-only old done must not clear composer status');
          assert.deepStrictEqual(calls.status, [], 'loading-only old done must not clear status');
          assert.strictEqual(calls.clearInflight, 0, 'loading-only old done must not clear active-pane inflight state');
          assert.deepStrictEqual(calls.clearInflightState, ['sid-1'], 'loading-only old done must retire the owner persisted inflight state');
          assert.strictEqual(INFLIGHT['sid-1'], undefined, 'loading-only old done must delete the owner INFLIGHT entry');
          assert.strictEqual(global._approvalPendingBySession.has('sid-1'), false, 'loading-only old done must delete owner approval pending state');
          assert.strictEqual(global._clarifyPendingBySession.has('sid-1'), false, 'loading-only old done must delete owner clarify pending state');
          assert.strictEqual(global._approvalPendingBySession.has('sid-control'), true, 'loading-only old done must preserve control-session approval pending state');
          assert.strictEqual(global._clarifyPendingBySession.has('sid-control'), true, 'loading-only old done must preserve control-session clarify pending state');
          assert.strictEqual(calls.syncTopbar, 0, 'loading-only old done must not sync the transitioning pane topbar');
          assert.strictEqual(calls.scrollIfPinned, 0, 'loading-only old done must not scroll the transitioning pane');
          assert.strictEqual(calls.scrollToBottom, 0, 'loading-only old done must not bottom-scroll the transitioning pane');
          assert.strictEqual(calls.finalizeThinking, 0, 'loading-only old done must not finalize Thinking UI');
          assert.strictEqual(calls.stopApproval, 0, 'loading-only old done must not stop active-pane approval polling');
          assert.strictEqual(calls.stopClarify, 0, 'loading-only old done must not stop active-pane clarify polling');
          assert.strictEqual(calls.hideApproval, 0, 'loading-only old done must not hide approval UI');
          assert.strictEqual(calls.hideClarify, 0, 'loading-only old done must not hide clarify UI');
          assert.strictEqual(renderCount, 0, 'loading-only old done must not render the transitioning pane');
          assert.deepStrictEqual(window._removeIdleLiveAssistantTurnCalls, [], 'loading-only old done must not remove live turns');
          const persistedTerminal = calls.anchorPersist
            .map(entry => entry && entry.scene)
            .find(scene => scene && scene.lifecycle && scene.lifecycle.terminal_state === 'completed');
          assert.ok(persistedTerminal, 'loading-only old done must persist a terminal Anchor lifecycle for the owner session');
          assert.strictEqual(S.activeStreamId, 'stream-old', 'loading-only old done must not clear the original active stream before successor attach');
          assert.strictEqual(S.session, originalSession, 'loading-only old done must not replace the original session object before successor attach');
          assert.strictEqual(S.messages, originalMessages, 'loading-only old done must not replace the original messages array before successor attach');
          const cleanupIndex = orderLog.findIndex(entry => entry.type === 'clearInflightState' && entry.sid === 'sid-1');
          const persistIndex = orderLog.findIndex(entry => entry.type === 'anchorPersistTerminal');
          const closeIndex = orderLog.findIndex(entry => entry.type === 'sourceClose');
          assert.notStrictEqual(cleanupIndex, -1, 'loading-only old done must record owner inflight cleanup');
          assert.notStrictEqual(persistIndex, -1, 'loading-only old done must record terminal Anchor persistence');
          assert.notStrictEqual(closeIndex, -1, 'loading-only old done must close the owner source');
          assert.ok(cleanupIndex < closeIndex, 'owner inflight cleanup must happen before owner source close');
          assert.ok(persistIndex < closeIndex, 'terminal Anchor persistence must happen before owner source close');
          const closeEntry = orderLog[closeIndex];
          assert.strictEqual(closeEntry.ownerInflightPresent, false, 'owner INFLIGHT must already be absent at source close');
          assert.strictEqual(closeEntry.approvalPresent, false, 'owner approval pending state must already be absent at source close');
          assert.strictEqual(closeEntry.clarifyPresent, false, 'owner clarify pending state must already be absent at source close');
          assert.strictEqual(closeEntry.terminalPersisted, true, 'terminal persistence request must already exist at source close');
          S.session = {session_id:'sid-2', message_count:1, pending_started_at:1};
          S.messages = [{role:'user', content:'next question'}];
          S.activeStreamId = 'stream-new';
          attachLiveStream('sid-2', 'stream-new');
        }else if(scenario === 'session_switch' || scenario === 'completed_session_switch'){
          if(scenario === 'session_switch'){
            global._loadingSessionId = 'sid-2';
            context._loadingSessionId = 'sid-2';
          }
          S.session = {
            session_id: scenario === 'session_switch' ? 'sid-1' : 'sid-2',
            message_count:1,
            pending_started_at:1,
          };
          S.messages = [{role:'user', content:'next question'}];
          S.activeStreamId = 'stream-new';
          attachLiveStream('sid-2', 'stream-new');
        }else if(scenario === 'origin_continuation_new_stream'){
          S.session = {session_id:'sid-cont', parent_session_id:'sid-1', message_count:1, pending_started_at:1};
          S.messages = [{role:'user', content:'continuation question'}];
          S.activeStreamId = 'stream-new';
          attachLiveStream('sid-cont', 'stream-new');
        }else{
          S.activeStreamId = 'stream-new';
          attachLiveStream('sid-1', 'stream-new');
        }
        resetMutationCalls();
        drainQueuedWork();

        assert.strictEqual(calls.highlight, 0, 'old done RAF must not highlight after owner rotation');
        assert.strictEqual(calls.copy, 0, 'old done RAF must not add copy buttons after owner rotation');
        assert.strictEqual(calls.katex, 0, 'old done RAF must not render KaTeX after owner rotation');
        assert.deepStrictEqual(calls.setBusy, [], 'stale done must not clear busy state for the newer owner');
        assert.deepStrictEqual(calls.composerStatus, [], 'stale done must not clear composer status for the newer owner');
        assert.deepStrictEqual(calls.status, [], 'stale done must not clear status for the newer owner');
        assert.strictEqual(calls.clearInflight, 0, 'stale done must not clear active-pane inflight state');
        assert.strictEqual(calls.finalizeThinking, 0, 'stale done must not finalize newer-owner thinking UI');
        assert.strictEqual(calls.hideApproval, 0, 'stale done must not hide newer-owner approval UI');
        assert.strictEqual(calls.hideClarify, 0, 'stale done must not hide newer-owner clarify UI');
        assert.strictEqual(renderCount, 0, 'stale done must not render the newer pane');
        assert.deepStrictEqual(window._removeIdleLiveAssistantTurnCalls, [], 'stale done must not remove newer live turns');
        assert.strictEqual(source.readyState, 2, 'old EventSource should still be closed as stale-owner teardown');
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
