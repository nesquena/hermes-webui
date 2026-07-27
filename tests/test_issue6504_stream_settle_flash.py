import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract(name: str) -> str:
    marker = f"async function {name}("
    start = MESSAGES_JS.find(marker)
    if start < 0:
        marker = f"function {name}("
        start = MESSAGES_JS.find(marker)
    assert start >= 0, f"missing function: {name}"
    brace = MESSAGES_JS.find("{", start)
    depth = 0
    for i in range(brace, len(MESSAGES_JS)):
        ch = MESSAGES_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return MESSAGES_JS[start : i + 1]
    raise AssertionError(f"unclosed function: {name}")


def _extract_event_body(event_name: str) -> str:
    marker = f"source.addEventListener('{event_name}'"
    start = MESSAGES_JS.find(marker)
    assert start >= 0, f"missing event listener: {event_name}"
    brace = MESSAGES_JS.find("{", start)
    depth = 0
    for i in range(brace, len(MESSAGES_JS)):
        ch = MESSAGES_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "let transportGeneration=1;\n" + MESSAGES_JS[brace + 1 : i]
    raise AssertionError(f"unclosed event listener: {event_name}")


def _extract_reconnect_preflight_body() -> str:
    anchor = "function _handleStreamError(source,activeTransportGeneration){"
    start = MESSAGES_JS.find(anchor)
    assert start >= 0, "missing _handleStreamError anchor"
    marker = "(async()=>{"
    start = MESSAGES_JS.find(marker, start)
    assert start >= 0, "missing reconnect preflight IIFE"
    brace = MESSAGES_JS.find("{", start)
    depth = 0
    for i in range(brace, len(MESSAGES_JS)):
        ch = MESSAGES_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return MESSAGES_JS[brace + 1 : i]
    raise AssertionError("unclosed reconnect preflight IIFE")


def _extract_attach_preflight_body() -> str:
    anchor = "// Reattach path can carry stale stream ids after server restart; preflight"
    start = MESSAGES_JS.find(anchor)
    assert start >= 0, "missing attach reconnect preflight anchor"
    marker = "(async()=>{"
    start = MESSAGES_JS.rfind(marker, 0, start)
    assert start >= 0, "missing attach reconnect preflight IIFE"
    brace = MESSAGES_JS.find("{", start)
    depth = 0
    for i in range(brace, len(MESSAGES_JS)):
        ch = MESSAGES_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return MESSAGES_JS[brace + 1 : i]
    raise AssertionError("unclosed attach reconnect preflight IIFE")


def _extract_restore_timeout_body() -> str:
    anchor = "const _restoreTimer=setTimeout(()=>{"
    start = MESSAGES_JS.find(anchor)
    assert start >= 0, "missing reconnect restore timeout"
    brace = MESSAGES_JS.find("{", start)
    depth = 0
    for i in range(brace, len(MESSAGES_JS)):
        ch = MESSAGES_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "let retainedTransportGeneration=1;\n" + MESSAGES_JS[brace + 1 : i]
    raise AssertionError("unclosed reconnect restore timeout")


def _run_node_script(script: str) -> subprocess.CompletedProcess[str]:
    wrapped = textwrap.dedent(
        f"""
        (async()=>{{
        {script}
        }})().catch((error)=>{{
          console.error(error);
          process.exit(1);
        }});
        """
    )
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".cjs",
        delete=False,
    ) as handle:
        handle.write(wrapped)
        script_path = Path(handle.name)
    try:
        return subprocess.run(
            [NODE, str(script_path)],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)


def _run_recovery_case(
    *,
    active: bool,
    restore_results,
    attempts: int,
    assistant_text: str,
    repeats: int = 1,
    stream_status=None,
    current_active_stream: str = "stream-1",
    stream_id: str = "stream-1",
    current_pane: bool | None = None,
):
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    owner = _extract("_ownsActiveStreamOrBackground")
    recovery_owner = _extract("_currentPaneRecoveryOwnerLost")
    fallback = _extract("_finalizeStreamEndFallback")
    visible = _extract("_visibleLiveAssistantAnswerPresent")
    reconcile = _extract("_reconcileStreamEndRecoveryExhaustion")
    recovery = _extract("_runStreamEndRecovery")
    restore_results = list(restore_results)
    stream_status = stream_status or {}
    script = textwrap.dedent(
        f"""
        let renderCalls = 0;
        let clearLiveToolCalls = 0;
        let removeThinkingCalls = 0;
        let sessionListCalls = 0;
        let idleCalls = 0;
        let closeCalls = 0;
        let wireCalls = 0;
        let finalizeThinkingCalls = 0;
        let scheduleDelays = [];
        let composerStatuses = [];
        let active = {str(active).lower()};
        let activeSid = 'sid-1';
        let streamId = {stream_id!r};
        let liveReasoningText = '';
        let reasoningText = '';
        let _persistTimer = null;
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _terminalStateReached = false;
        let _streamFinalized = false;
        let _streamEndRecoveryLease = {{ generation: 1, source: {{}}, timer: null, attempts: {attempts} }};
        globalThis.S = {{
          session: {{ session_id: 'sid-1' }},
          activeStreamId: {current_active_stream!r},
          messages: [{{ role: 'assistant', content: 'rebuilt transcript placeholder' }}]
        }};
        globalThis.LIVE_STREAMS = {{
          'sid-1': {{ streamId: {stream_id!r}, source: {{}}, ownerToken: 1, transportGeneration: 1 }}
        }};
        globalThis.assistantText = {assistant_text!r};
        globalThis.INFLIGHT = {{}};
        globalThis.assistantBody = {{ isConnected: {str(bool(assistant_text)).lower()}, textContent: {assistant_text!r} }};
        globalThis.$ = () => null;
        globalThis._isActiveSession = () => active;
        globalThis._isSessionCurrentPane = () => {str((active if current_pane is None else current_pane)).lower()};
        globalThis._clearStreamEndRecovery = () => {{
          _streamEndRecoveryLease = null;
        }};
        globalThis._scheduleStreamEndRecovery = (_source, delay = 180, transportGeneration = 1, recoveryAttempts = 0) => {{
          _streamEndRecoveryLease = {{
            generation: transportGeneration,
            source: _source,
            timer: null,
            attempts: recoveryAttempts,
          }};
          scheduleDelays.push(delay);
        }};
        let restoreResults = {json.dumps(restore_results)};
        globalThis._restoreSettledSession = async () => restoreResults.length ? restoreResults.shift() : false;
        globalThis.api = async () => ({json.dumps(stream_status)});
        globalThis.setComposerStatus = (status) => {{ composerStatuses.push(status); }};
        globalThis._runJournalReplayParams = () => '';
        globalThis.EventSource = function(url) {{ this.url = url; this.readyState = 0; }};
        globalThis._wireSSE = () => {{ wireCalls += 1; }};
        globalThis.document = {{ baseURI: 'http://localhost:8787/' }};
        globalThis.location = {{ href: 'http://localhost:8787/' }};
        globalThis._cancelThrottledSnapshotTimer = () => {{}};
        globalThis._cancelAnimationFramePendingStreamRender = () => {{}};
        globalThis._streamFadeCleanupReduceMotionListener = () => {{}};
        globalThis._smdEndParser = () => {{}};
        globalThis.finalizeThinkingCard = () => {{ finalizeThinkingCalls++; }};
        globalThis._clearOwnerInflightState = () => {{}};
        globalThis._clearStreamHidden = () => {{}};
        globalThis._clearStreamNotificationBackground = () => {{}};
        globalThis._flushReasoningToAnchor = () => {{}};
        globalThis._scheduleAnchorRegistryCleanup = () => {{}};
        globalThis._clearAnchorProseIncrementalNode = () => {{}};
        globalThis._clearApprovalForOwner = () => {{}};
        globalThis._clearClarifyForOwner = () => {{}};
        globalThis.clearLiveToolCards = () => {{ clearLiveToolCalls++; }};
        globalThis.removeThinking = () => {{ removeThinkingCalls++; }};
        globalThis.renderMessages = () => {{ renderCalls++; }};
        globalThis.renderSessionList = () => {{ sessionListCalls++; }};
        globalThis._setActivePaneIdleIfOwner = () => {{ idleCalls++; }};
        globalThis._closeSource = () => {{ closeCalls++; }};
        {current_owner}
        {current_owner_active}
        {_extract("_captureCurrentLiveTransportGeneration")}
        {owner}
        {recovery_owner}
        {fallback}
        {visible}
        {reconcile}
        {recovery}
        (async () => {{
          for (let i = 0; i < {repeats}; i += 1) {{
            const transportGeneration = _streamEndRecoveryLease ? _streamEndRecoveryLease.generation : 1;
            await _runStreamEndRecovery({{}}, transportGeneration);
          }}
          console.log(JSON.stringify({{
            renderCalls,
            clearLiveToolCalls,
            removeThinkingCalls,
            sessionListCalls,
            idleCalls,
            closeCalls,
            wireCalls,
            finalizeThinkingCalls,
            activeStreamId: S.activeStreamId,
            scheduleDelays,
            composerStatuses,
            attempts: _streamEndRecoveryLease ? _streamEndRecoveryLease.attempts : 0,
            pending: !!_streamEndRecoveryLease,
            finalized: _streamFinalized,
            terminal: _terminalStateReached
          }}));
        }})().catch((error) => {{
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        }});
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_restore_stale_guard_case():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    owner = _extract("_ownsActiveStreamOrBackground")
    recovery = _extract("_currentPaneRecoveryOwnerLost")
    restore = _extract("_restoreSettledSession")
    script = textwrap.dedent(
        """
        let closeCalls = 0;
        let apiCalls = 0;
        let activeSid = 'sid-1';
        let streamId = 'old-stream';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _streamFinalized = false;
        let _pendingStreamEndRecovery = true;
        let _streamEndRecoveryAttempts = 3;
        let _streamEndRecoveryTimer = {};
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'replacement-stream',
        };
        globalThis.LIVE_STREAMS = {};
        globalThis._loadingSessionId = null;
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = (sid) => !globalThis._loadingSessionId || globalThis._loadingSessionId === sid;
        globalThis.api = async () => {
          apiCalls += 1;
          return { session: null };
        };
        globalThis._closeSource = () => { closeCalls += 1; delete LIVE_STREAMS['sid-1']; };
        globalThis._clearStreamEndRecovery = () => {
          _pendingStreamEndRecovery = false;
          _streamEndRecoveryAttempts = 0;
          _streamEndRecoveryTimer = null;
        };
        """ + current_owner + """
        """ + current_owner_active + """
        """ + owner + """
        """ + recovery + """
        """ + restore + """
        (async () => {
          const status = await _restoreSettledSession({}, { status: true });
          console.log(JSON.stringify({
            status,
            closeCalls,
            apiCalls,
            activeStreamId: S.activeStreamId,
            pending: _pendingStreamEndRecovery,
          }));
        })().catch((error) => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_restore_same_generation_replacement_case(*, reject: bool):
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    current_transport_generation = _extract("_captureCurrentLiveTransportGeneration")
    owner = _extract("_ownsActiveStreamOrBackground")
    recovery = _extract("_currentPaneRecoveryOwnerLost")
    clear_recovery = _extract("_clearStreamEndRecovery")
    close_source = _extract("_closeSource")
    restore = _extract("_restoreSettledSession")
    script = textwrap.dedent(
        """
        let renderCalls = 0;
        let sessionListCalls = 0;
        let idleCalls = 0;
        let clearLiveToolCalls = 0;
        let removeThinkingCalls = 0;
        let hydrateCalls = 0;
        let oldSourceCloseCalls = 0;
        let replacementSourceCloseCalls = 0;
        let resolveSession;
        let rejectSession;
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _streamFinalized = false;
        let _persistTimer = null;
        let _pendingStreamEndRecovery = true;
        let _streamEndRecoveryAttempts = 4;
        let _streamEndRecoveryTimer = {};
        let _streamEndRecoveryLease = null;
        const oldSource = {
          readyState: 1,
          close() { oldSourceCloseCalls += 1; this.readyState = 2; },
        };
        const replacementSource = {
          readyState: 1,
          close() { replacementSourceCloseCalls += 1; this.readyState = 2; },
        };
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [{ role: 'assistant', content: 'replacement pane answer' }],
          toolCalls: [{ id: 'tool-1' }],
        };
        globalThis.INFLIGHT = {};
        globalThis.assistantText = 'replacement pane answer';
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: oldSource, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis._loadingSessionId = null;
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = () => true;
        globalThis.api = () => new Promise((resolve, reject) => {
          resolveSession = resolve;
          rejectSession = reject;
        });
        globalThis._cancelThrottledSnapshotTimer = () => {};
        globalThis._clearAnchorProseIncrementalNode = () => {};
        globalThis._cancelAnimationFramePendingStreamRender = () => {};
        globalThis._streamFadeCleanupReduceMotionListener = () => {};
        globalThis._smdEndParser = () => {};
        globalThis.finalizeThinkingCard = () => {};
        globalThis._clearOwnerInflightState = () => {};
        globalThis._flushReasoningToAnchor = () => {};
        globalThis._scheduleAnchorRegistryCleanup = () => {};
        globalThis._clearApprovalForOwner = () => {};
        globalThis._clearClarifyForOwner = () => {};
        globalThis._isSessionActivelyViewed = () => true;
        globalThis._markSessionCompletionUnread = () => {};
        globalThis.clearLiveToolCards = () => { clearLiveToolCalls += 1; };
        globalThis.removeThinking = () => { removeThinkingCalls += 1; };
        globalThis._filterRecoveryControlMessages = (messages) => messages;
        globalThis._carryForwardEphemeralTurnFields = (_current, next) => next;
        globalThis._attachProjectedAnchorSceneToLastAssistant = () => {};
        globalThis._hydrateTodosFromSession = () => { hydrateCalls += 1; };
        globalThis.localStorage = { setItem: () => {} };
        globalThis._setActiveSessionUrl = () => {};
        globalThis._replaceMarkerOnlyAssistantWithStreamError = () => false;
        globalThis.showToast = () => {};
        globalThis._mergeSettledToolCallsWithLiveMetadata = (toolCalls) => toolCalls;
        globalThis._markSessionViewed = () => {};
        globalThis._messageRenderableMessageCount = () => 1;
        globalThis._currentMessageRenderWindowSize = () => 50;
        globalThis._messageRenderWindowSize = 50;
        globalThis.syncTopbar = () => {};
        globalThis.renderMessages = () => { renderCalls += 1; };
        globalThis.renderSessionList = () => { sessionListCalls += 1; };
        globalThis._setActivePaneIdleIfOwner = () => { idleCalls += 1; };
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + current_transport_generation
        + """
        """
        + owner
        + """
        """
        + recovery
        + """
        """
        + clear_recovery
        + """
        """
        + close_source
        + """
        """
        + restore
        + """
        (async () => {
          const pending = _restoreSettledSession(oldSource, {
            status: true,
            preserveVisibleOnShorterTerminalSnapshot: true,
            transportGeneration: 1,
          });
              _streamEndRecoveryLease = {
                generation: 2,
                source: replacementSource,
                timer: {},
                attempts: 4,
              };
              _pendingStreamEndRecovery = true;
              _streamEndRecoveryAttempts = 4;
              _streamEndRecoveryTimer = {};
          LIVE_STREAMS['sid-1'] = {
            streamId: 'stream-1',
            source: replacementSource,
            ownerToken: 1,
            transportGeneration: 2,
          };
          if ("""
        + ("true" if reject else "false")
        + """) {
            rejectSession(new Error('network failed'));
          } else {
            resolveSession({
              session: {
                session_id: 'sid-1',
                messages: [{ role: 'assistant', content: 'stale settled answer' }],
                tool_calls: [{ id: 'tool-stale' }],
                message_count: 1,
              }
            });
          }
          const result = await pending;
          console.log(JSON.stringify({
            result,
            renderCalls,
            sessionListCalls,
            idleCalls,
            clearLiveToolCalls,
            removeThinkingCalls,
            hydrateCalls,
            pending: !!_streamEndRecoveryLease,
            attempts: _streamEndRecoveryLease ? _streamEndRecoveryLease.attempts : 0,
            activeStreamId: S.activeStreamId,
            ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
            transportGeneration: LIVE_STREAMS['sid-1'].transportGeneration,
            currentSourceIsReplacement: LIVE_STREAMS['sid-1'].source === replacementSource,
            oldSourceCloseCalls,
            replacementSourceCloseCalls,
            messages: S.messages,
          }));
        })().catch((error) => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_active_recovery_exhaustion_switches_to_slow_poll_instead_of_finalizing():
    result = _run_recovery_case(
        active=True,
        restore_results=["active"],
        attempts=9,
        assistant_text="final streamed answer",
    )

    assert result["renderCalls"] == 0
    assert result["activeStreamId"] == "stream-1"
    assert result["scheduleDelays"] == [1000]
    assert result["closeCalls"] == 1
    assert result["finalized"] is False
    assert result["terminal"] is False


def test_repeated_active_recovery_polls_never_pin_partial_answer_as_terminal():
    result = _run_recovery_case(
        active=True,
        restore_results=["active", "active"],
        attempts=9,
        assistant_text="partial visible answer",
        repeats=2,
    )

    assert result["renderCalls"] == 0
    assert result["scheduleDelays"] == [1000, 1000]
    assert result["closeCalls"] == 1
    assert result["activeStreamId"] == "stream-1"
    assert result["finalized"] is False


def test_repeated_active_recovery_preserves_attempts_after_transport_close():
    result = _run_recovery_case(
        active=True,
        restore_results=["active", "active"],
        attempts=9,
        assistant_text="partial visible answer",
        repeats=2,
    )

    assert result["scheduleDelays"] == [1000, 1000]
    assert result["attempts"] == 11
    assert result["pending"] is True
    assert result["finalized"] is False


def test_error_during_stream_end_recovery_reschedules_on_advanced_generation():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    current_event_owner = _extract("_currentLiveEventSourceOwnsStream")
    current_transport_generation = _extract("_captureCurrentLiveTransportGeneration")
    owner = _extract("_ownsActiveStreamOrBackground")
    close_source = _extract("_closeSource")
    error_body = _extract_event_body("error")
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _terminalStateReached = false;
        let _streamFinalized = false;
        let _streamEndRecoveryLease = { generation: 1, source: null, timer: {}, attempts: 10 };
        let closeCalls = 0;
        let scheduleDelays = [];
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [],
        };
        const liveSource = {
          readyState: 1,
          close() { closeCalls += 1; this.readyState = 2; },
        };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: liveSource, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis.source = liveSource;
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = () => true;
        globalThis.snapshotLiveTurnHtmlForSession = () => {};
        globalThis._clearLiveRunStatusTimer = () => {};
        globalThis.hideLiveRunStatus = () => {};
        globalThis.closeLiveStream = () => { delete LIVE_STREAMS['sid-1']; };
        globalThis._rememberRunJournalCursor = () => {};
        globalThis._scheduleStreamEndRecovery = (_source, delay, transportGeneration, attempts) => {
          _streamEndRecoveryLease = {
            generation: transportGeneration,
            source: _source,
            timer: {},
            attempts,
          };
          scheduleDelays.push(delay);
        };
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + current_event_owner
        + """
        """
        + current_transport_generation
        + """
        """
        + owner
        + """
        """
        + close_source
        + """
        const handler = async (e) => {
        """
        + error_body
        + """
        };
        await handler({ currentTarget: liveSource, target: liveSource, lastEventId: 'run_1:10', data: '{}' });
        console.log(JSON.stringify({
          closeCalls,
          scheduleDelays,
          currentGeneration: LIVE_STREAMS['sid-1'].transportGeneration,
          currentSourceIsNull: LIVE_STREAMS['sid-1'].source === null,
          recoveryGeneration: _streamEndRecoveryLease && _streamEndRecoveryLease.generation,
          recoveryAttempts: _streamEndRecoveryLease && _streamEndRecoveryLease.attempts,
        }));
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["closeCalls"] == 1
    assert result["scheduleDelays"] == [1000]
    assert result["currentGeneration"] == 2
    assert result["currentSourceIsNull"] is True
    assert result["recoveryGeneration"] == 2
    assert result["recoveryAttempts"] == 10


def test_fallback_without_visible_live_answer_still_rebuilds_when_recovery_stops():
    result = _run_recovery_case(
        active=True,
        restore_results=["error"],
        attempts=0,
        assistant_text="",
    )

    assert result["renderCalls"] == 1
    assert result["clearLiveToolCalls"] == 1
    assert result["removeThinkingCalls"] == 1
    assert result["activeStreamId"] is None
    assert result["closeCalls"] == 1
    assert result["finalized"] is True


def test_background_session_fallback_keeps_existing_rebuild_path():
    result = _run_recovery_case(
        active=False,
        restore_results=["error"],
        attempts=0,
        assistant_text="background answer",
    )

    assert result["renderCalls"] == 0
    assert result["clearLiveToolCalls"] == 0
    assert result["activeStreamId"] == "stream-1"
    assert result["sessionListCalls"] == 1
    assert result["closeCalls"] == 1


def test_replacement_stream_stale_guard_bails_before_old_recovery_fetches_or_finalizes():
    result = _run_restore_stale_guard_case()

    assert result["status"] == "stale"
    assert result["closeCalls"] == 1
    assert result["apiCalls"] == 0
    assert result["activeStreamId"] == "replacement-stream"
    assert result["pending"] is False


def test_long_tail_recovery_reattaches_when_stream_status_stays_active():
    result = _run_recovery_case(
        active=True,
        restore_results=["active"],
        attempts=15,
        assistant_text="final streamed answer",
        stream_status={"active": True},
    )

    assert result["wireCalls"] == 1
    assert result["scheduleDelays"] == []
    assert result["composerStatuses"] == ["Reconnected"]
    assert result["finalized"] is False
    assert result["pending"] is False


def test_direct_error_recovery_rebuilds_even_with_visible_live_answer():
    result = _run_recovery_case(
        active=True,
        restore_results=["error"],
        attempts=3,
        assistant_text="final streamed answer",
        stream_status={"active": False, "replay_available": False},
    )

    assert result["wireCalls"] == 0
    assert result["scheduleDelays"] == []
    assert result["renderCalls"] == 1
    assert result["clearLiveToolCalls"] == 1
    assert result["removeThinkingCalls"] == 0
    assert result["activeStreamId"] is None
    assert result["finalized"] is True


def test_long_tail_recovery_rebuilds_when_stream_turns_inactive_after_exhaustion():
    result = _run_recovery_case(
        active=True,
        restore_results=["active", False],
        attempts=15,
        assistant_text="final streamed answer",
        stream_status={"active": False, "replay_available": False},
    )

    assert result["wireCalls"] == 0
    assert result["scheduleDelays"] == []
    assert result["renderCalls"] == 1
    assert result["clearLiveToolCalls"] == 1
    assert result["removeThinkingCalls"] == 0
    assert result["activeStreamId"] is None
    assert result["finalized"] is True


def test_long_tail_recovery_without_visible_live_answer_still_rebuilds_when_stream_turns_inactive():
    result = _run_recovery_case(
        active=True,
        restore_results=["active", False],
        attempts=15,
        assistant_text="",
        stream_status={"active": False, "replay_available": False},
    )

    assert result["wireCalls"] == 0
    assert result["scheduleDelays"] == []
    assert result["renderCalls"] == 1
    assert result["clearLiveToolCalls"] == 1
    assert result["removeThinkingCalls"] == 1
    assert result["activeStreamId"] is None
    assert result["finalized"] is True


def test_long_tail_recovery_does_not_reattach_stale_stream_over_replacement_owner():
    result = _run_recovery_case(
        active=True,
        restore_results=["active"],
        attempts=15,
        assistant_text="replacement answer",
        stream_status={"active": True},
        current_active_stream="replacement-stream",
        stream_id="old-stream",
    )

    assert result["wireCalls"] == 0
    assert result["scheduleDelays"] == []
    assert result["closeCalls"] == 1
    assert result["pending"] is False
    assert result["activeStreamId"] == "replacement-stream"


def test_long_tail_recovery_does_not_reattach_after_pane_ownership_switch_during_status_await():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    owner = _extract("_ownsActiveStreamOrBackground")
    recovery_owner = _extract("_currentPaneRecoveryOwnerLost")
    reconcile = _extract("_reconcileStreamEndRecoveryExhaustion")
    script = textwrap.dedent(
        """
        let closeCalls = 0;
        let wireCalls = 0;
        let composerStatuses = [];
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _streamFinalized = false;
        let _terminalStateReached = false;
        let _pendingStreamEndRecovery = true;
        let _streamEndRecoveryAttempts = 15;
        let _streamEndRecoveryTimer = {};
        let resolveStatus;
        const source = {};
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [],
        };
        globalThis.LIVE_STREAMS = {};
        LIVE_STREAMS['sid-1'] = { streamId: 'stream-1', source, ownerToken: 1, transportGeneration: 1 };
        globalThis._loadingSessionId = null;
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = (sid) => !globalThis._loadingSessionId || globalThis._loadingSessionId === sid;
        globalThis.api = () => new Promise((resolve) => { resolveStatus = resolve; });
        globalThis.setComposerStatus = (status) => { composerStatuses.push(status); };
        globalThis._restoreSettledSession = async () => false;
        globalThis._runJournalReplayParams = () => '';
        globalThis.EventSource = function(url) { this.url = url; };
        globalThis._wireSSE = () => { wireCalls += 1; LIVE_STREAMS[activeSid] = { owner: streamId }; };
        globalThis.document = { baseURI: 'http://localhost:8787/' };
        globalThis.location = { href: 'http://localhost:8787/' };
        globalThis._closeSource = () => { closeCalls += 1; delete LIVE_STREAMS['sid-1']; };
        globalThis._clearStreamEndRecovery = () => {
          _pendingStreamEndRecovery = false;
          _streamEndRecoveryAttempts = 0;
          _streamEndRecoveryTimer = null;
        };
        """ + current_owner + """
        """ + current_owner_active + """
        """ + owner + """
        """ + recovery_owner + """
        """ + reconcile + """
        (async () => {
          const pending = _reconcileStreamEndRecoveryExhaustion(source);
          globalThis._loadingSessionId = 'sid-2';
          S.activeStreamId = 'replacement-stream';
          resolveStatus({ active: true, replay_available: false });
          await pending;
          console.log(JSON.stringify({
            closeCalls,
            wireCalls,
            composerStatuses,
            pending: _pendingStreamEndRecovery,
            activeStreamId: S.activeStreamId,
            liveOwner: LIVE_STREAMS['sid-1'] || null,
          }));
        })().catch((error) => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["closeCalls"] == 1
    assert result["wireCalls"] == 0
    assert result["composerStatuses"] == []
    assert result["pending"] is False
    assert result["activeStreamId"] == "replacement-stream"
    assert result["liveOwner"] is None


def test_restore_settled_session_does_not_mutate_after_pane_ownership_switch_during_await():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    owner = _extract("_ownsActiveStreamOrBackground")
    recovery_owner = _extract("_currentPaneRecoveryOwnerLost")
    restore = _extract("_restoreSettledSession")
    script = textwrap.dedent(
        """
        let closeCalls = 0;
        let renderCalls = 0;
        let sessionListCalls = 0;
        let idleCalls = 0;
        let clearLiveToolCalls = 0;
        let removeThinkingCalls = 0;
        let hydrateCalls = 0;
        let resolveSession;
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _streamFinalized = false;
        let _pendingStreamEndRecovery = true;
        let _streamEndRecoveryAttempts = 4;
        let _streamEndRecoveryTimer = {};
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [{ role: 'assistant', content: 'replacement pane answer' }],
          toolCalls: [{ id: 'tool-1' }],
        };
        globalThis.INFLIGHT = {};
        globalThis.assistantText = 'replacement pane answer';
        globalThis.LIVE_STREAMS = { 'sid-1': { streamId: 'stream-1', source: {}, ownerToken: 1, transportGeneration: 1 } };
        globalThis._loadingSessionId = null;
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = (sid) => !globalThis._loadingSessionId || globalThis._loadingSessionId === sid;
        globalThis.api = () => new Promise((resolve) => { resolveSession = resolve; });
        globalThis._closeSource = () => { closeCalls += 1; };
        globalThis._clearStreamEndRecovery = () => {
          _pendingStreamEndRecovery = false;
          _streamEndRecoveryAttempts = 0;
          _streamEndRecoveryTimer = null;
        };
        globalThis._persistTimer = null;
        globalThis._cancelThrottledSnapshotTimer = () => {};
        globalThis._clearAnchorProseIncrementalNode = () => {};
        globalThis._cancelAnimationFramePendingStreamRender = () => {};
        globalThis._streamFadeCleanupReduceMotionListener = () => {};
        globalThis._smdEndParser = () => {};
        globalThis.finalizeThinkingCard = () => {};
        globalThis._clearOwnerInflightState = () => {};
        globalThis._flushReasoningToAnchor = () => {};
        globalThis._scheduleAnchorRegistryCleanup = () => {};
        globalThis._clearApprovalForOwner = () => {};
        globalThis._clearClarifyForOwner = () => {};
        globalThis._isSessionActivelyViewed = () => true;
        globalThis._markSessionCompletionUnread = () => {};
        globalThis.clearLiveToolCards = () => { clearLiveToolCalls += 1; };
        globalThis.removeThinking = () => { removeThinkingCalls += 1; };
        globalThis._filterRecoveryControlMessages = (messages) => messages;
        globalThis._carryForwardEphemeralTurnFields = (_current, next) => next;
        globalThis._attachProjectedAnchorSceneToLastAssistant = () => {};
        globalThis._hydrateTodosFromSession = () => { hydrateCalls += 1; };
        globalThis.localStorage = { setItem: () => {} };
        globalThis._setActiveSessionUrl = () => {};
        globalThis._replaceMarkerOnlyAssistantWithStreamError = () => false;
        globalThis.showToast = () => {};
        globalThis._mergeSettledToolCallsWithLiveMetadata = (toolCalls) => toolCalls;
        globalThis._markSessionViewed = () => {};
        globalThis._messageRenderableMessageCount = () => 1;
        globalThis._currentMessageRenderWindowSize = () => 50;
        globalThis._messageRenderWindowSize = 50;
        globalThis.syncTopbar = () => {};
        globalThis.renderMessages = () => { renderCalls += 1; };
        globalThis.renderSessionList = () => { sessionListCalls += 1; };
        globalThis._setActivePaneIdleIfOwner = () => { idleCalls += 1; };
        """ + current_owner + """
        """ + current_owner_active + """
        """ + owner + """
        """ + recovery_owner + """
        """ + restore + """
        (async () => {
          const pending = _restoreSettledSession({}, { preserveVisibleOnShorterTerminalSnapshot: true });
          globalThis._loadingSessionId = 'sid-2';
          S.activeStreamId = 'replacement-stream';
          resolveSession({
            session: {
              session_id: 'sid-1',
              messages: [{ role: 'assistant', content: 'stale settled answer' }],
              tool_calls: [{ id: 'tool-stale' }],
              message_count: 1,
            }
          });
          const result = await pending;
          console.log(JSON.stringify({
            result,
            closeCalls,
            renderCalls,
            sessionListCalls,
            idleCalls,
            clearLiveToolCalls,
            removeThinkingCalls,
            hydrateCalls,
            pending: _pendingStreamEndRecovery,
            activeStreamId: S.activeStreamId,
            messages: S.messages,
          }));
        })().catch((error) => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["result"] is True
    assert result["closeCalls"] == 1
    assert result["renderCalls"] == 0
    assert result["sessionListCalls"] == 0
    assert result["idleCalls"] == 0
    assert result["clearLiveToolCalls"] == 0
    assert result["removeThinkingCalls"] == 0
    assert result["hydrateCalls"] == 0
    assert result["pending"] is False
    assert result["activeStreamId"] == "replacement-stream"
    assert result["messages"] == [{"role": "assistant", "content": "replacement pane answer"}]


def test_restore_settled_session_does_not_mutate_after_same_stream_token_replacement_during_await():
    result = _run_restore_same_generation_replacement_case(reject=False)

    assert result["result"] == "stale"
    assert result["renderCalls"] == 0
    assert result["sessionListCalls"] == 0
    assert result["idleCalls"] == 0
    assert result["clearLiveToolCalls"] == 0
    assert result["removeThinkingCalls"] == 0
    assert result["hydrateCalls"] == 0
    assert result["pending"] is True
    assert result["attempts"] == 4
    assert result["activeStreamId"] == "stream-1"
    assert result["ownerToken"] == 1
    assert result["transportGeneration"] == 2
    assert result["currentSourceIsReplacement"] is True
    assert result["oldSourceCloseCalls"] == 0
    assert result["replacementSourceCloseCalls"] == 0
    assert result["messages"] == [{"role": "assistant", "content": "replacement pane answer"}]


def test_restore_settled_session_rejection_does_not_mutate_after_same_stream_token_replacement():
    result = _run_restore_same_generation_replacement_case(reject=True)

    assert result["result"] == "stale"
    assert result["renderCalls"] == 0
    assert result["sessionListCalls"] == 0
    assert result["idleCalls"] == 0
    assert result["clearLiveToolCalls"] == 0
    assert result["removeThinkingCalls"] == 0
    assert result["hydrateCalls"] == 0
    assert result["pending"] is True
    assert result["attempts"] == 4
    assert result["activeStreamId"] == "stream-1"
    assert result["ownerToken"] == 1
    assert result["transportGeneration"] == 2
    assert result["currentSourceIsReplacement"] is True
    assert result["oldSourceCloseCalls"] == 0
    assert result["replacementSourceCloseCalls"] == 0
    assert result["messages"] == [{"role": "assistant", "content": "replacement pane answer"}]


def test_replacement_owner_blocks_queued_persist_snapshot_render_recovery_and_resume_callbacks():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    close_source = _extract("_closeSource")
    persist = _extract("persistInflightState")
    snapshot = _extract("snapshotLiveTurn")
    throttled_snapshot = _extract("_throttledSnapshotLiveTurn")
    throttled_persist = _extract("_throttledPersist")
    schedule_recovery = _extract("_scheduleStreamEndRecovery")
    page_hidden = _extract("_pageHiddenForStreamError")
    defer_hidden = _extract("_deferStreamErrorIfPageHidden")
    schedule_render = _extract("_scheduleRender")
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'old-stream';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _streamFinalized = false;
        let _terminalStateReached = false;
        let _pendingStreamEndRecovery = false;
        let _streamEndRecoveryTimer = null;
        let _streamEndRecoveryAttempts = 0;
        let _persistTimer = null;
        let _snapshotLiveTurnTimer = null;
        let _deferredStreamRecoveryBound = false;
        let _deferredStreamRecoveryResume = null;
        let _pendingRafHandle = null;
        let _renderPending = false;
        let _lastRenderMs = 0;
        let _cachedParsed = null;
        let _cachedParsedText = '';
        let _cachedParsedReasoning = '';
        let assistantText = 'visible answer';
        let liveReasoningText = '';
        let reasoningText = '';
        let segmentStart = 0;
        let renderCalls = 0;
        let persistWrites = 0;
        let snapshotWrites = 0;
        let recoveryCalls = 0;
        let reattachCalls = 0;
        let timerId = 0;
        const timers = new Map();
        const rafs = [];
        const listeners = [];
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'old-stream',
          messages: [{ role: 'assistant', content: 'visible answer' }],
        };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'old-stream', source: { readyState: 1, close() {} }, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis.INFLIGHT = {
          'sid-1': { messages: [{ role: 'assistant', content: 'visible answer' }], uploaded: [], toolCalls: [] }
        };
        globalThis.uploaded = [];
        globalThis.saveInflightState = () => { persistWrites += 1; };
        globalThis.snapshotLiveTurnHtmlForSession = () => { snapshotWrites += 1; };
        globalThis._runStreamEndRecovery = () => { recoveryCalls += 1; };
        globalThis._reattachOrRestoreAfterDeferredStreamError = () => { reattachCalls += 1; };
        globalThis._isSessionCurrentPane = () => true;
        globalThis._pageHiddenForStreamError = () => (
          document.visibilityState === 'hidden' || document.wasDiscarded === true
        );
        globalThis.setComposerStatus = () => {};
        globalThis.performance = { now: () => 100 };
        globalThis.requestAnimationFrame = (cb) => { rafs.push(cb); return ++timerId; };
        globalThis.cancelAnimationFrame = () => {};
        globalThis.setTimeout = (cb) => {
          const id = ++timerId;
          timers.set(id, cb);
          return id;
        };
        globalThis.clearTimeout = (id) => { timers.delete(id); };
        globalThis._parseStreamState = () => ({ displayText: 'visible answer' });
        globalThis._renderLiveThinking = () => { renderCalls += 1; };
        globalThis._stripXmlToolCalls = (text) => text;
        globalThis._shouldUseLiveProseFade = () => false;
        globalThis._upsertAnchorProcessProse = () => { renderCalls += 1; };
        globalThis.scrollIfPinned = () => {};
        globalThis.assistantBody = null;
        globalThis._fixMobileScrollJank = null;
        globalThis.window = {
          addEventListener: (_name, cb) => { listeners.push(cb); },
          removeEventListener: () => {},
          _fixMobileScrollJank: null,
        };
        globalThis.document = {
          visibilityState: 'hidden',
          wasDiscarded: false,
          addEventListener: (_name, cb) => { listeners.push(cb); },
          removeEventListener: () => {},
        };
        """ + current_owner + """
        """ + current_owner_active + """
        """ + close_source + """
        """ + persist + """
        """ + snapshot + """
        """ + throttled_snapshot + """
        """ + throttled_persist + """
        """ + schedule_recovery + """
        """ + page_hidden + """
        """ + defer_hidden + """
        """ + schedule_render + """
        _throttledPersist();
        _throttledSnapshotLiveTurn();
        _scheduleStreamEndRecovery({});
        _scheduleRender();
        _deferStreamErrorIfPageHidden({});
        LIVE_STREAMS['sid-1'] = { streamId: 'replacement-stream', source: { readyState: 1, close() {} }, ownerToken: 2, transportGeneration: 1 };
        S.activeStreamId = 'replacement-stream';
        document.visibilityState = 'visible';
        for (const cb of [...timers.values()]) cb();
        while (rafs.length) rafs.shift()();
        for (const cb of [...listeners]) cb();
        console.log(JSON.stringify({
          persistWrites,
          snapshotWrites,
          recoveryCalls,
          reattachCalls,
          renderCalls,
          activeStreamId: S.activeStreamId,
          ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
          deferredBound: _deferredStreamRecoveryBound,
        }));
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["persistWrites"] == 0
    assert result["snapshotWrites"] == 0
    assert result["recoveryCalls"] == 0
    assert result["reattachCalls"] == 0
    assert result["renderCalls"] == 0
    assert result["activeStreamId"] == "replacement-stream"
    assert result["ownerToken"] == 2
    assert result["deferredBound"] is False


def test_hidden_page_deferred_resume_clears_failed_source_before_reconnect():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    owner = _extract("_ownsActiveStreamOrBackground")
    close_source = _extract("_closeSource")
    defer_hidden = _extract("_deferStreamErrorIfPageHidden")
    wire = _extract("_wireSSE")
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _streamFinalized = false;
        let _terminalStateReached = false;
        let _deferredStreamRecoveryBound = false;
        let _deferredStreamRecoveryResume = null;
        let currentCloseCalls = 0;
        let replacementCloseCalls = 0;
        let reattachCalls = 0;
        const listeners = [];
        const currentSource = {
          readyState: 2,
          close() { currentCloseCalls += 1; },
          addEventListener() {},
        };
        const replacementSource = {
          readyState: 1,
          close() { replacementCloseCalls += 1; },
          addEventListener() {},
        };
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [],
        };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: currentSource, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis.snapshotLiveTurnHtmlForSession = () => {};
        globalThis._clearLiveRunStatusTimer = () => {};
        globalThis.hideLiveRunStatus = () => {};
        globalThis._rememberRunJournalCursor = () => {};
        globalThis._pageHiddenForStreamError = () => document.visibilityState === 'hidden';
        globalThis._reattachOrRestoreAfterDeferredStreamError = () => {
          reattachCalls += 1;
              _wireSSE(replacementSource,2);
        };
        globalThis.setComposerStatus = () => {};
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = () => true;
        globalThis.window = {
          addEventListener: (_name, cb) => { listeners.push(cb); },
          removeEventListener: (_name, cb) => {
            const idx = listeners.indexOf(cb);
            if (idx !== -1) listeners.splice(idx, 1);
          },
        };
        globalThis.document = {
          visibilityState: 'hidden',
          addEventListener: (_name, cb) => { listeners.push(cb); },
          removeEventListener: (_name, cb) => {
            const idx = listeners.indexOf(cb);
            if (idx !== -1) listeners.splice(idx, 1);
          },
        };
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + owner
        + """
        """
        + close_source
        + """
        """
        + wire
        + """
        """
        + defer_hidden
        + """
        const deferred = _deferStreamErrorIfPageHidden(currentSource);
        const sourceClearedBeforeResume = LIVE_STREAMS['sid-1'].source === null;
        document.visibilityState = 'visible';
        for (const cb of [...new Set(listeners)]) cb();
        console.log(JSON.stringify({
          deferred,
          sourceClearedBeforeResume,
          reattachCalls,
          currentSourceIsReplacement: LIVE_STREAMS['sid-1'].source === replacementSource,
          replacementCloseCalls,
          currentCloseCalls,
          ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
        }));
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["deferred"] is True
    assert result["sourceClearedBeforeResume"] is True
    assert result["reattachCalls"] == 1
    assert result["currentSourceIsReplacement"] is True
    assert result["replacementCloseCalls"] == 0
    assert result["currentCloseCalls"] == 0
    assert result["ownerToken"] == 1


def test_reconnect_preflight_rejection_does_not_recreate_same_id_replaced_owner():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    owner = _extract("_ownsActiveStreamOrBackground")
    recovery_owner = _extract("_currentPaneRecoveryOwnerLost")
    reconnect_preflight = _extract_reconnect_preflight_body()
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let reconnecting = true;
        let replayOnly = false;
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _persistTimer = null;
        let _snapshotLiveTurnTimer = null;
        let _streamEndRecoveryTimer = null;
        let _deferredStreamRecoveryResume = null;
        let _deferredStreamRecoveryBound = false;
        let eventSourceCalls = 0;
        let closeCalls = 0;
        let resolveStatus;
        let rejectStatus;
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [],
        };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: { readyState: 1, close() {} }, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = () => true;
        globalThis.api = () => new Promise((resolve, reject) => {
          resolveStatus = resolve;
          rejectStatus = reject;
        });
        globalThis.EventSource = function(url) {
          eventSourceCalls += 1;
          this.url = url;
          this.readyState = 0;
          this.addEventListener = () => {};
          this.close = () => {};
        };
        globalThis._runJournalReplayParams = () => '';
        globalThis.document = { baseURI: 'http://localhost:8787/' };
        globalThis.location = { href: 'http://localhost:8787/' };
        globalThis._clearOwnerInflightState = () => {};
        globalThis._clearApprovalForOwner = () => {};
        globalThis._clearClarifyForOwner = () => {};
        globalThis.clearLiveToolCards = () => {};
        globalThis.removeThinking = () => {};
        globalThis._setActivePaneIdleIfOwner = () => {};
        globalThis.renderMessages = () => {};
        globalThis.renderSessionList = () => {};
        globalThis.scrollToBottom = () => {};
        globalThis._isMessagePaneNearBottom = () => true;
        globalThis._isMessageReaderUnpinned = () => false;
        globalThis._queueDrainSid = null;
        globalThis._scheduleAnchorRegistryCleanup = () => {};
        globalThis._clearStreamEndRecovery = () => {};
        globalThis._cancelAnimationFramePendingStreamRender = () => {};
        globalThis._closeSource = () => { closeCalls += 1; };
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + owner
        + """
        """
        + recovery_owner
        + """
        (async () => {
          const pending = (async () => {
        """
        + reconnect_preflight
        + """
          })();
          LIVE_STREAMS['sid-1'] = {
            streamId: 'stream-1',
            source: { readyState: 1, close() {} },
            ownerToken: 2,
            transportGeneration: 1,
          };
          rejectStatus(new Error('lost status probe'));
          await pending;
          console.log(JSON.stringify({
            closeCalls,
            eventSourceCalls,
            ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
            activeStreamId: S.activeStreamId,
          }));
        })().catch((error) => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["closeCalls"] == 1
    assert result["eventSourceCalls"] == 0
    assert result["ownerToken"] == 2
    assert result["activeStreamId"] == "stream-1"


def test_done_fade_completion_does_not_mutate_after_same_id_owner_token_replacement():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    current_event_owner = _extract("_currentLiveEventSourceOwnsStream")
    owner = _extract("_ownsActiveStreamOrBackground")
    bail = _extract("_bailOutOfTerminalEventsFromStaleStream")
    done_body = _extract_event_body("done")
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _streamFinalized = false;
        let _terminalStateReached = false;
        let _persistTimer = null;
        let _streamEndRecoveryTimer = null;
        let delayedFinish = null;
        let renderCalls = 0;
        let idleCalls = 0;
        let sessionListCalls = 0;
        let closeCalls = 0;
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [{ role: 'assistant', content: 'replacement answer' }],
          toolCalls: [],
        };
        const liveSource = { readyState: 1, close() {} };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: liveSource, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis.INFLIGHT = {};
        globalThis.assistantText = 'replacement answer';
        globalThis.reasoningText = '';
        globalThis.liveReasoningText = '';
        globalThis.assistantBody = { textContent: 'replacement answer' };
        globalThis.source = liveSource;
        globalThis._clearStreamEndRecovery = () => {};
        globalThis._scheduleAnchorRegistryCleanup = () => {};
        globalThis._closeSource = () => { closeCalls += 1; };
        globalThis._cancelThrottledSnapshotTimer = () => {};
        globalThis._cancelAnimationFramePendingStreamRender = () => {};
        globalThis._streamFadeCleanupReduceMotionListener = () => {};
        globalThis.finalizeThinkingCard = () => {};
        globalThis._smdEndParser = () => {};
        globalThis.requestAnimationFrame = (cb) => cb();
        globalThis.highlightCode = () => {};
        globalThis.addCopyButtons = () => {};
        globalThis.renderKatexBlocks = () => {};
        globalThis._flushReasoningToAnchor = () => {};
        globalThis._applyToAnchor = () => {};
        globalThis._clearAnchorProseIncrementalNode = () => {};
        globalThis._isSessionCurrentPane = () => true;
        globalThis._isSessionActivelyViewed = () => true;
        globalThis._markSessionViewed = () => {};
        globalThis._clearOwnerInflightState = () => {};
        globalThis._markSessionCompletedInList = () => {};
        globalThis._clearApprovalForOwner = () => {};
        globalThis._clearClarifyForOwner = () => {};
        globalThis._shouldFollowMessagesOnDomReplace = () => false;
        globalThis._carryForwardEphemeralTurnFields = (_current, next) => next;
        globalThis._messagesTruncated = false;
        globalThis._filterRecoveryControlMessages = (messages) => messages;
        globalThis._hydrateTodosFromSession = () => {};
        globalThis.clearVisibleMessageRowCache = () => {};
        globalThis.localStorage = { setItem: () => {} };
        globalThis._setActiveSessionUrl = () => {};
        globalThis._replaceMarkerOnlyAssistantWithStreamError = () => false;
        globalThis.showToast = () => {};
        globalThis._splitThinkFromContent = (content, reasoning) => ({ content, reasoning });
        globalThis._mergeUsageForCtxIndicator = (_usage, fallback) => fallback;
        globalThis._syncCtxIndicator = () => {};
        globalThis._attachProjectedAnchorSceneToLastAssistant = () => {};
        globalThis._mergeSettledToolCallsWithLiveMetadata = (toolCalls) => toolCalls;
        globalThis.renderSessionArtifacts = () => {};
        globalThis.clearLiveToolCards = () => {};
        globalThis.syncTopbar = () => {};
        globalThis.renderMessages = () => { renderCalls += 1; };
        globalThis._renderMessagesWithScrollSnapshot = null;
        globalThis.scrollToBottom = () => {};
        globalThis.noteWorkspaceMutationsFromToolCalls = () => {};
        globalThis.loadDir = () => {};
        globalThis.autoReadLastAssistant = () => {};
        globalThis.playNotificationSound = () => {};
        globalThis._shouldForceCompletionNotification = () => false;
        globalThis._completionNotificationPreviewText = () => '';
        globalThis.sendBrowserNotification = () => {};
        globalThis._shouldUseLiveProseFade = () => true;
        globalThis._drainStreamFadeBeforeDone = (cb) => { delayedFinish = cb; };
        globalThis.renderSessionList = () => { sessionListCalls += 1; };
        globalThis._setActivePaneIdleIfOwner = () => { idleCalls += 1; };
        globalThis._isActiveSession = () => true;
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + current_event_owner
        + """
        """
        + owner
        + """
        """
        + bail
        + """
        const doneHandler = (e) => {
        """
        + done_body
        + """
        };
        doneHandler({ data: JSON.stringify({ session: { session_id: 'sid-1', messages: [{ role: 'assistant', content: 'stale settled answer' }], tool_calls: [], message_count: 1 } }) });
        LIVE_STREAMS['sid-1'] = {
          streamId: 'stream-1',
          source: { readyState: 1, close() {} },
          ownerToken: 2,
        };
        delayedFinish();
        console.log(JSON.stringify({
          renderCalls,
          idleCalls,
          sessionListCalls,
          closeCalls,
          ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
          activeStreamId: S.activeStreamId,
        }));
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["renderCalls"] == 0
    assert result["idleCalls"] == 0
    assert result["sessionListCalls"] == 0
    assert result["closeCalls"] == 0
    assert result["ownerToken"] == 2
    assert result["activeStreamId"] == "stream-1"


def test_stream_end_during_done_fade_keeps_completion_lease_alive_until_settle():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    current_event_owner = _extract("_currentLiveEventSourceOwnsStream")
    current_transport_generation = _extract("_captureCurrentLiveTransportGeneration")
    owner = _extract("_ownsActiveStreamOrBackground")
    retire_live_closure = _extract("_retireLiveClosure")
    close_source = _extract("_closeSource")
    done_body = _extract_event_body("done")
    stream_end_body = _extract_event_body("stream_end")
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _streamFinalized = false;
        let _terminalStateReached = false;
        let _persistTimer = null;
        let _streamEndRecoveryLease = null;
        let _acceptedCompletionLease = null;
        let _liveTransportGenerationSeq = 1;
        let delayedFinish = null;
        let retainOwnerCloseCalls = 0;
        let retireCloseCalls = 0;
        let sessionListCalls = 0;
        let idleCalls = 0;
        let _deferredStreamRecoveryResume = null;
        let _deferredStreamRecoveryBound = false;
        let uploaded = [];
        let _latestGoalStatus = null;
        let _pendingGoalContinuation = null;
        let _queueDrainSid = null;
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [{ role: 'assistant', content: 'replacement answer' }],
          toolCalls: [],
        };
        const liveSource = { readyState: 1, close() {} };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: liveSource, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis.INFLIGHT = {};
        globalThis.assistantText = 'replacement answer';
        globalThis.reasoningText = '';
        globalThis.liveReasoningText = '';
        globalThis.assistantBody = { textContent: 'replacement answer' };
        globalThis.source = liveSource;
        globalThis.$ = () => null;
        globalThis._clearStreamEndRecovery = () => { _streamEndRecoveryLease = null; };
        globalThis._scheduleStreamEndRecovery = () => {};
        globalThis._cancelThrottledSnapshotTimer = () => {};
        globalThis._cancelAnimationFramePendingStreamRender = () => {};
        globalThis._streamFadeCleanupReduceMotionListener = () => {};
        globalThis.finalizeThinkingCard = () => {};
        globalThis._smdEndParser = () => {};
        globalThis.requestAnimationFrame = (cb) => cb();
        globalThis.highlightCode = () => {};
        globalThis.addCopyButtons = () => {};
        globalThis.renderKatexBlocks = () => {};
        globalThis._flushReasoningToAnchor = () => {};
        globalThis._applyToAnchor = () => {};
        globalThis._clearAnchorProseIncrementalNode = () => {};
        globalThis._scheduleAnchorRegistryCleanup = () => {};
        globalThis._isSessionCurrentPane = () => true;
        globalThis._isSessionActivelyViewed = () => true;
        globalThis._markSessionViewed = () => {};
        globalThis._clearOwnerInflightState = () => {};
        globalThis._markSessionCompletedInList = () => {};
        globalThis._clearApprovalForOwner = () => {};
        globalThis._clearClarifyForOwner = () => {};
        globalThis._shouldFollowMessagesOnDomReplace = () => false;
        globalThis._carryForwardEphemeralTurnFields = (_current, next) => next;
        globalThis._messagesTruncated = false;
        globalThis._filterRecoveryControlMessages = (messages) => messages;
        globalThis._hydrateTodosFromSession = () => {};
        globalThis.clearVisibleMessageRowCache = () => {};
        globalThis.localStorage = { setItem: () => {} };
        globalThis._setActiveSessionUrl = () => {};
        globalThis._replaceMarkerOnlyAssistantWithStreamError = () => false;
        globalThis.showToast = () => {};
        globalThis._splitThinkFromContent = (content, reasoning) => ({ content, reasoning });
        globalThis._mergeUsageForCtxIndicator = (_usage, fallback) => fallback;
        globalThis._syncCtxIndicator = () => {};
        globalThis._attachProjectedAnchorSceneToLastAssistant = () => {};
        globalThis._mergeSettledToolCallsWithLiveMetadata = (toolCalls) => toolCalls;
        globalThis.renderSessionArtifacts = () => {};
        globalThis.clearLiveToolCards = () => {};
        globalThis.syncTopbar = () => {};
        globalThis.renderMessages = () => {};
        globalThis._renderMessagesWithScrollSnapshot = null;
        globalThis.scrollToBottom = () => {};
        globalThis.noteWorkspaceMutationsFromToolCalls = () => {};
        globalThis.loadDir = () => {};
        globalThis.autoReadLastAssistant = () => {};
        globalThis.playNotificationSound = () => {};
        globalThis._shouldForceCompletionNotification = () => false;
        globalThis._completionNotificationPreviewText = () => '';
        globalThis.sendBrowserNotification = () => {};
        globalThis._shouldUseLiveProseFade = () => true;
        globalThis._drainStreamFadeBeforeDone = (cb) => { delayedFinish = cb; };
        globalThis.renderSessionList = () => { sessionListCalls += 1; };
        globalThis._setActivePaneIdleIfOwner = () => { idleCalls += 1; };
        globalThis._isActiveSession = () => true;
        globalThis.window = { removeEventListener: () => {} };
        globalThis.snapshotLiveTurnHtmlForSession = () => {};
        globalThis._clearLiveRunStatusTimer = () => {};
        globalThis.hideLiveRunStatus = () => {};
        globalThis.closeLiveStream = (sessionId, expectedStreamId, expectedSource) => {
          const live = LIVE_STREAMS[sessionId];
          if (!live) return;
          if (expectedStreamId && live.streamId !== expectedStreamId) return;
          if (expectedSource && live.source !== expectedSource) return;
          delete LIVE_STREAMS[sessionId];
        };
        globalThis._nextLiveTransportGeneration = () => {
          _liveTransportGenerationSeq += 1;
          return _liveTransportGenerationSeq;
        };
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + current_event_owner
        + """
        """
        + current_transport_generation
        + """
        """
        + owner
        + """
        """
        + retire_live_closure
        + """
        """
        + close_source
        + """
        const doneHandler = (e) => {
        """
        + done_body
        + """
        };
        const streamEndHandler = async (e) => {
        """
        + stream_end_body
        + """
        };
        const originalCloseSource = _closeSource;
        _closeSource = (currentSource, options = null) => {
          if (options && options.retainOwner) retainOwnerCloseCalls += 1;
          else retireCloseCalls += 1;
          return originalCloseSource(currentSource, options);
        };
        globalThis._closeSource = _closeSource;
        doneHandler({
          data: JSON.stringify({
            session: {
              session_id: 'sid-1',
              messages: [{ role: 'assistant', content: 'replacement answer' }],
              tool_calls: [],
              message_count: 1,
            },
            usage: null,
          })
        });
        await streamEndHandler({ data: '{}' });
        delayedFinish();
        console.log(JSON.stringify({
          retainOwnerCloseCalls,
          retireCloseCalls,
          closureRetired: _closureRetired,
          liveEntryPresent: !!LIVE_STREAMS['sid-1'],
          activeStreamId: S.activeStreamId,
          sessionListCalls,
          idleCalls,
        }));
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["retainOwnerCloseCalls"] == 1
    assert result["retireCloseCalls"] == 1
    assert result["closureRetired"] is True
    assert result["liveEntryPresent"] is False
    assert result["activeStreamId"] is None
    assert result["sessionListCalls"] == 1
    assert result["idleCalls"] == 1


def test_error_during_done_fade_keeps_completion_lease_alive_until_settle():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    current_event_owner = _extract("_currentLiveEventSourceOwnsStream")
    current_transport_generation = _extract("_captureCurrentLiveTransportGeneration")
    owner = _extract("_ownsActiveStreamOrBackground")
    retire_live_closure = _extract("_retireLiveClosure")
    close_source = _extract("_closeSource")
    done_body = _extract_event_body("done")
    error_body = _extract_event_body("error")
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _streamFinalized = false;
        let _terminalStateReached = false;
        let _persistTimer = null;
        let _streamEndRecoveryLease = null;
        let _acceptedCompletionLease = null;
        let _liveTransportGenerationSeq = 1;
        let delayedFinish = null;
        let retainOwnerCloseCalls = 0;
        let retireCloseCalls = 0;
        let _deferredStreamRecoveryResume = null;
        let _deferredStreamRecoveryBound = false;
        let uploaded = [];
        let _latestGoalStatus = null;
        let _pendingGoalContinuation = null;
        let _queueDrainSid = null;
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [{ role: 'assistant', content: 'replacement answer' }],
          toolCalls: [],
        };
        const liveSource = { readyState: 1, close() {} };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: liveSource, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis.INFLIGHT = {};
        globalThis.assistantText = 'replacement answer';
        globalThis.reasoningText = '';
        globalThis.liveReasoningText = '';
        globalThis.assistantBody = { textContent: 'replacement answer' };
        globalThis.source = liveSource;
        globalThis.$ = () => null;
        globalThis._clearStreamEndRecovery = () => { _streamEndRecoveryLease = null; };
        globalThis._scheduleStreamEndRecovery = () => {};
        globalThis._cancelThrottledSnapshotTimer = () => {};
        globalThis._cancelAnimationFramePendingStreamRender = () => {};
        globalThis._streamFadeCleanupReduceMotionListener = () => {};
        globalThis.finalizeThinkingCard = () => {};
        globalThis._smdEndParser = () => {};
        globalThis.requestAnimationFrame = (cb) => cb();
        globalThis.highlightCode = () => {};
        globalThis.addCopyButtons = () => {};
        globalThis.renderKatexBlocks = () => {};
        globalThis._flushReasoningToAnchor = () => {};
        globalThis._applyToAnchor = () => {};
        globalThis._clearAnchorProseIncrementalNode = () => {};
        globalThis._scheduleAnchorRegistryCleanup = () => {};
        globalThis._isSessionCurrentPane = () => true;
        globalThis._isSessionActivelyViewed = () => true;
        globalThis._markSessionViewed = () => {};
        globalThis._clearOwnerInflightState = () => {};
        globalThis._markSessionCompletedInList = () => {};
        globalThis._clearApprovalForOwner = () => {};
        globalThis._clearClarifyForOwner = () => {};
        globalThis._shouldFollowMessagesOnDomReplace = () => false;
        globalThis._carryForwardEphemeralTurnFields = (_current, next) => next;
        globalThis._messagesTruncated = false;
        globalThis._filterRecoveryControlMessages = (messages) => messages;
        globalThis._hydrateTodosFromSession = () => {};
        globalThis.clearVisibleMessageRowCache = () => {};
        globalThis.localStorage = { setItem: () => {} };
        globalThis._setActiveSessionUrl = () => {};
        globalThis._replaceMarkerOnlyAssistantWithStreamError = () => false;
        globalThis.showToast = () => {};
        globalThis._splitThinkFromContent = (content, reasoning) => ({ content, reasoning });
        globalThis._mergeUsageForCtxIndicator = (_usage, fallback) => fallback;
        globalThis._syncCtxIndicator = () => {};
        globalThis._attachProjectedAnchorSceneToLastAssistant = () => {};
        globalThis._mergeSettledToolCallsWithLiveMetadata = (toolCalls) => toolCalls;
        globalThis.renderSessionArtifacts = () => {};
        globalThis.clearLiveToolCards = () => {};
        globalThis.syncTopbar = () => {};
        globalThis.renderMessages = () => {};
        globalThis._renderMessagesWithScrollSnapshot = null;
        globalThis.scrollToBottom = () => {};
        globalThis.noteWorkspaceMutationsFromToolCalls = () => {};
        globalThis.loadDir = () => {};
        globalThis.autoReadLastAssistant = () => {};
        globalThis.playNotificationSound = () => {};
        globalThis._shouldForceCompletionNotification = () => false;
        globalThis._completionNotificationPreviewText = () => '';
        globalThis.sendBrowserNotification = () => {};
        globalThis._shouldUseLiveProseFade = () => true;
        globalThis._drainStreamFadeBeforeDone = (cb) => { delayedFinish = cb; };
        globalThis.renderSessionList = () => {};
        globalThis._setActivePaneIdleIfOwner = () => {};
        globalThis._isActiveSession = () => true;
        globalThis.window = { removeEventListener: () => {} };
        globalThis.snapshotLiveTurnHtmlForSession = () => {};
        globalThis._clearLiveRunStatusTimer = () => {};
        globalThis.hideLiveRunStatus = () => {};
        globalThis._rememberRunJournalCursor = () => {};
        globalThis.closeLiveStream = (sessionId, expectedStreamId, expectedSource) => {
          const live = LIVE_STREAMS[sessionId];
          if (!live) return;
          if (expectedStreamId && live.streamId !== expectedStreamId) return;
          if (expectedSource && live.source !== expectedSource) return;
          delete LIVE_STREAMS[sessionId];
        };
        globalThis._nextLiveTransportGeneration = () => {
          _liveTransportGenerationSeq += 1;
          return _liveTransportGenerationSeq;
        };
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + current_event_owner
        + """
        """
        + current_transport_generation
        + """
        """
        + owner
        + """
        """
        + retire_live_closure
        + """
        """
        + close_source
        + """
        const doneHandler = (e) => {
        """
        + done_body
        + """
        };
        const errorHandler = async (e) => {
        """
        + error_body
        + """
        };
        const originalCloseSource = _closeSource;
        _closeSource = (currentSource, options = null) => {
          if (options && options.retainOwner) retainOwnerCloseCalls += 1;
          else retireCloseCalls += 1;
          return originalCloseSource(currentSource, options);
        };
        globalThis._closeSource = _closeSource;
        doneHandler({
          data: JSON.stringify({
            session: {
              session_id: 'sid-1',
              messages: [{ role: 'assistant', content: 'replacement answer' }],
              tool_calls: [],
              message_count: 1,
            },
            usage: null,
          })
        });
        await errorHandler({ currentTarget: liveSource, target: liveSource, lastEventId: 'run_1:11', data: '{}' });
        delayedFinish();
        console.log(JSON.stringify({
          retainOwnerCloseCalls,
          retireCloseCalls,
          closureRetired: _closureRetired,
          liveEntryPresent: !!LIVE_STREAMS['sid-1'],
          activeStreamId: S.activeStreamId,
        }));
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["retainOwnerCloseCalls"] == 1
    assert result["retireCloseCalls"] == 1
    assert result["closureRetired"] is True
    assert result["liveEntryPresent"] is False
    assert result["activeStreamId"] is None


@pytest.mark.parametrize("event_name", ["stream_end", "apperror", "error", "cancel"])
def test_terminal_callbacks_do_not_mutate_after_same_id_owner_token_replacement(event_name: str):
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    current_event_owner = _extract("_currentLiveEventSourceOwnsStream")
    owner = _extract("_ownsActiveStreamOrBackground")
    bail = _extract("_bailOutOfTerminalEventsFromStaleStream")
    event_body = _extract_event_body(event_name)
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _streamFinalized = false;
        let _terminalStateReached = false;
        let closeCalls = 0;
        let renderCalls = 0;
        let restoreCalls = 0;
        let handleErrorCalls = 0;
        let wireCalls = 0;
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [{ role: 'assistant', content: 'replacement answer' }],
        };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: { readyState: 1, close() {} }, ownerToken: 2, transportGeneration: 1 }
        };
        globalThis.source = { readyState: 1, close() {} };
        globalThis._clearStreamEndRecovery = () => {};
        globalThis._scheduleAnchorRegistryCleanup = () => {};
        globalThis._closeSource = () => { closeCalls += 1; };
        globalThis._restoreSettledSession = async () => { restoreCalls += 1; return false; };
        globalThis._handleStreamError = () => { handleErrorCalls += 1; };
        globalThis._wireSSE = () => { wireCalls += 1; };
        globalThis.renderMessages = () => { renderCalls += 1; };
        globalThis._deferStreamErrorIfOffline = () => false;
        globalThis._deferStreamErrorIfPageHidden = () => false;
        globalThis._pendingStreamEndRecovery = false;
        globalThis._isSessionCurrentPane = () => true;
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + current_event_owner
        + """
        """
        + owner
        + """
        """
        + bail
        + """
        const handler = async (e) => {
        """
        + event_body
        + """
        };
        handler({ data: '{}' }).then(() => {
          console.log(JSON.stringify({
            closeCalls,
            renderCalls,
            restoreCalls,
            handleErrorCalls,
            wireCalls,
            ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
            activeStreamId: S.activeStreamId,
          }));
        }).catch((error) => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["closeCalls"] == 0
    assert result["renderCalls"] == 0
    assert result["restoreCalls"] == 0
    assert result["handleErrorCalls"] == 0
    assert result["wireCalls"] == 0
    assert result["ownerToken"] == 2
    assert result["activeStreamId"] == "stream-1"


def test_restore_timeout_does_not_finalize_after_same_id_owner_token_replacement():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    owner = _extract("_ownsActiveStreamOrBackground")
    recovery_owner = _extract("_currentPaneRecoveryOwnerLost")
    restore_timeout = _extract_restore_timeout_body()
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _terminalStateReached = false;
        let _streamFinalized = false;
        let _restoreTimedOut = false;
        let closeCalls = 0;
        let handleErrorCalls = 0;
        let flushCalls = 0;
        let cleanupCalls = 0;
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [{ role: 'assistant', content: 'replacement answer' }],
        };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: { readyState: 1, close() {} }, ownerToken: 2, transportGeneration: 1 }
        };
        globalThis.source = { readyState: 1, close() {} };
        globalThis._isSessionCurrentPane = () => true;
        globalThis._isActiveSession = () => true;
        globalThis._deferStreamErrorIfOffline = () => false;
        globalThis._deferStreamErrorIfPageHidden = () => false;
        globalThis._flushReasoningToAnchor = () => { flushCalls += 1; };
        globalThis._scheduleAnchorRegistryCleanup = () => { cleanupCalls += 1; };
        globalThis._handleStreamError = () => { handleErrorCalls += 1; };
        globalThis._closeSource = () => { closeCalls += 1; };
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + owner
        + """
        """
        + recovery_owner
        + """
        const runRestoreTimeout = () => {
        """
        + restore_timeout
        + """
        };
        runRestoreTimeout();
        console.log(JSON.stringify({
          restoreTimedOut: _restoreTimedOut,
          closeCalls,
          handleErrorCalls,
          flushCalls,
          cleanupCalls,
          ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
          activeStreamId: S.activeStreamId,
        }));
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["restoreTimedOut"] is True
    assert result["closeCalls"] == 1
    assert result["handleErrorCalls"] == 0
    assert result["flushCalls"] == 0
    assert result["cleanupCalls"] == 0
    assert result["ownerToken"] == 2
    assert result["activeStreamId"] == "stream-1"


@pytest.mark.parametrize(
    ("event_name", "event_payload"),
    [
        ("token", {"text": "stale token"}),
        ("interim_assistant", {"text": "stale interim"}),
        ("reasoning", {"text": "stale reasoning"}),
        ("tool", {"id": "tool-1", "name": "search"}),
        ("tool_complete", {"id": "tool-1", "name": "search"}),
    ],
)
def test_queued_live_events_do_not_mutate_after_same_id_owner_token_replacement(
    event_name: str,
    event_payload: dict,
):
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    current_event_owner = _extract("_currentLiveEventSourceOwnsStream")
    owner = _extract("_ownsActiveStreamOrBackground")
    bail = _extract("_bailOutOfTerminalEventsFromStaleStream")
    event_body = _extract_event_body(event_name)
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _terminalStateReached = false;
        let _streamFinalized = false;
        let closeCalls = 0;
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [],
        };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: { readyState: 1, close() {} }, ownerToken: 2, transportGeneration: 1 }
        };
        globalThis.source = { readyState: 1, close() {} };
        globalThis.assistantText = 'replacement answer';
        globalThis.reasoningText = 'replacement reasoning';
        globalThis.liveReasoningText = 'replacement reasoning';
        globalThis._scheduleAnchorRegistryCleanup = () => {};
        globalThis._closeSource = () => { closeCalls += 1; };
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = () => true;
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + current_event_owner
        + """
        """
        + owner
        + """
        """
        + bail
        + """
        const handler = (e) => {
        """
        + event_body
        + """
        };
        handler({ data: JSON.stringify("""
        + json.dumps(event_payload)
        + """) });
        console.log(JSON.stringify({
          closeCalls,
          assistantText,
          reasoningText,
          liveReasoningText,
          ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
          activeStreamId: S.activeStreamId,
        }));
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["closeCalls"] == 0
    assert result["assistantText"] == "replacement answer"
    assert result["reasoningText"] == "replacement reasoning"
    assert result["liveReasoningText"] == "replacement reasoning"
    assert result["ownerToken"] == 2
    assert result["activeStreamId"] == "stream-1"


def test_old_source_callback_does_not_retire_current_same_token_owner():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    current_event_owner = _extract("_currentLiveEventSourceOwnsStream")
    owner = _extract("_ownsActiveStreamOrBackground")
    bail = _extract("_bailOutOfTerminalEventsFromStaleStream")
    token_body = _extract_event_body("token")
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _terminalStateReached = false;
        let _streamFinalized = false;
        let closeCalls = 0;
        const oldSource = { readyState: 2, close() {} };
        const newSource = { readyState: 1, close() {} };
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [],
        };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: newSource, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis.assistantText = 'replacement answer';
        globalThis.syncInflightAssistantMessage = () => {};
        globalThis._completeAutomaticCompressionOnLiveProgress = () => {};
        globalThis.appendThinking = () => {};
        globalThis._liveThinkingPlacement = () => null;
        globalThis._freshSegment = false;
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = () => true;
        globalThis._closeSource = () => { closeCalls += 1; };
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + current_event_owner
        + """
        """
        + owner
        + """
        """
        + bail
        + """
        const handler = (e) => {
          const source = e.currentTarget;
        """
        + token_body
        + """
        };
        handler({ currentTarget: oldSource, target: oldSource, data: JSON.stringify({ text: 'stale token' }) });
        console.log(JSON.stringify({
          closeCalls,
          closureRetired: _closureRetired,
          assistantText,
          currentSourceIsNew: LIVE_STREAMS['sid-1'].source === newSource,
          ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
        }));
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["closeCalls"] == 0
    assert result["closureRetired"] is False
    assert result["assistantText"] == "replacement answer"
    assert result["currentSourceIsNew"] is True
    assert result["ownerToken"] == 1


def test_current_owner_reconnect_path_keeps_owner_alive_until_rewire():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    owner = _extract("_ownsActiveStreamOrBackground")
    recovery_owner = _extract("_currentPaneRecoveryOwnerLost")
    close_source = _extract("_closeSource")
    wire = _extract("_wireSSE")
    error_body = _extract_event_body("error")
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _terminalStateReached = false;
        let _streamFinalized = false;
        let _pendingStreamEndRecovery = false;
        let _reconnectAttempted = false;
        let eventSourceCalls = 0;
        let oldSourceCloseCalls = 0;
        let resolveStatus;
        const timers = [];
        const oldSource = {
          readyState: 1,
          close() { oldSourceCloseCalls += 1; this.readyState = 2; },
        };
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [],
        };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: oldSource, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis.source = oldSource;
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = () => true;
        globalThis._deferStreamErrorIfOffline = () => false;
        globalThis._deferStreamErrorIfPageHidden = () => false;
        globalThis.recordClientSSEError = () => {};
        globalThis.api = () => new Promise((resolve) => { resolveStatus = resolve; });
        globalThis.setComposerStatus = () => {};
        globalThis.snapshotLiveTurnHtmlForSession = () => {};
        globalThis._clearLiveRunStatusTimer = () => {};
        globalThis.hideLiveRunStatus = () => {};
        globalThis.closeLiveStream = () => { throw new Error('owner retired during reconnect'); };
        globalThis._runJournalReplayParams = () => '';
        globalThis.document = { baseURI: 'http://localhost:8787/' };
        globalThis.location = { href: 'http://localhost:8787/' };
        globalThis.EventSource = function(url) {
          eventSourceCalls += 1;
          this.url = url;
          this.readyState = 0;
          this.addEventListener = () => {};
          this.close = () => {};
        };
        globalThis.setTimeout = (cb) => { timers.push(cb); return timers.length; };
        globalThis.clearTimeout = () => {};
        globalThis._restoreSettledSession = async () => false;
        globalThis._rememberRunJournalCursor = () => {};
        function _bailOutOfTerminalEventsFromStaleStream(){ return false; }
        function _retireLiveClosure(src){
          if(src&&typeof src.close==='function') src.close();
          _closureRetired = true;
        }
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + owner
        + """
        """
        + recovery_owner
        + """
        """
        + close_source
        + """
        """
        + wire
        + """
        const handler = async (e) => {
        """
        + error_body
        + """
        };
        await handler({ data: '{}' });
        timers.shift()();
        resolveStatus({ active: true });
        await Promise.resolve();
        await Promise.resolve();
        console.log(JSON.stringify({
          eventSourceCalls,
          oldSourceCloseCalls,
          ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
          currentSourceIsOld: LIVE_STREAMS['sid-1'].source === oldSource,
          hasLiveSource: !!LIVE_STREAMS['sid-1'].source,
          closureRetired: _closureRetired,
        }));
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["eventSourceCalls"] == 1
    assert result["oldSourceCloseCalls"] >= 1
    assert result["ownerToken"] == 1
    assert result["currentSourceIsOld"] is False
    assert result["hasLiveSource"] is True
    assert result["closureRetired"] is False


def test_initial_preflight_and_reconnect_error_publish_one_replacement_under_same_owner():
    attach_preflight = _extract_attach_preflight_body()
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    current_event_owner = _extract("_currentLiveEventSourceOwnsStream")
    current_transport_generation = _extract("_captureCurrentLiveTransportGeneration")
    next_transport_generation = _extract("_nextLiveTransportGeneration")
    owner = _extract("_ownsActiveStreamOrBackground")
    recovery_owner = _extract("_currentPaneRecoveryOwnerLost")
    clear_recovery = _extract("_clearStreamEndRecovery")
    close_source = _extract("_closeSource")
    bail = _extract("_bailOutOfTerminalEventsFromStaleStream")
    wire = _extract("_wireSSE")
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let reconnecting = true;
        let _liveOwnerToken = 1;
        let _liveTransportGenerationSeq = 1;
        let _closureRetired = false;
        let _terminalStateReached = false;
        let _streamFinalized = false;
        let _pendingStreamEndRecovery = false;
        let _reconnectAttempted = false;
        let _persistTimer = null;
        let _snapshotLiveTurnTimer = null;
        let _streamEndRecoveryTimer = null;
        let _deferredStreamRecoveryResume = null;
        let _deferredStreamRecoveryBound = false;
        let cursorCalls = 0;
        let reconnectStatuses = [];
        const timers = [];
        const createdSources = [];
        class FakeEventSource {
          constructor(url) {
            this.url = url;
            this.readyState = 1;
            this.closed = false;
            this.listeners = {};
            createdSources.push(this);
          }
          addEventListener(name, cb) {
            (this.listeners[name] ||= []).push(cb);
          }
          dispatch(name, event) {
            for (const cb of this.listeners[name] || []) cb(event);
          }
          close() {
            this.closed = true;
            this.readyState = 2;
          }
        }
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [],
        };
        globalThis.INFLIGHT = {};
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: null, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = () => true;
        globalThis._deferStreamErrorIfOffline = () => false;
        globalThis._deferStreamErrorIfPageHidden = () => false;
        globalThis.recordClientSSEError = () => {};
        globalThis.setComposerStatus = () => {};
        globalThis.snapshotLiveTurnHtmlForSession = () => {};
        globalThis._clearLiveRunStatusTimer = () => {};
        globalThis.hideLiveRunStatus = () => {};
        globalThis.closeLiveStream = () => { throw new Error('owner retired during reconnect'); };
        globalThis._runJournalReplayParams = () => '';
        globalThis.document = { baseURI: 'http://localhost:8787/' };
        globalThis.location = { href: 'http://localhost:8787/' };
        globalThis.EventSource = FakeEventSource;
        globalThis.setTimeout = (cb) => { timers.push(cb); return timers.length; };
        globalThis.clearTimeout = () => {};
        globalThis._restoreSettledSession = async () => false;
        globalThis._rememberRunJournalCursor = () => { cursorCalls += 1; };
        globalThis.api = () => new Promise((resolve) => { reconnectStatuses.push(resolve); });
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + current_event_owner
        + """
        """
        + current_transport_generation
        + """
        """
        + next_transport_generation
        + """
        """
        + owner
        + """
        """
        + recovery_owner
        + """
        """
        + clear_recovery
        + """
        """
        + close_source
        + """
        """
        + bail
        + """
        """
        + wire
        + """
        const runAttachPreflight = async () => {
        """
        + attach_preflight
        + """
        };
        await Promise.resolve();
        const initialAttach = runAttachPreflight();
        reconnectStatuses.shift()({ active: true, replay_available: false });
        await initialAttach;
        const firstSource = createdSources[0];
        const firstGeneration = LIVE_STREAMS['sid-1'].transportGeneration;
        const errorListenerCount = firstSource.listeners.error.length;
        firstSource.dispatch('error', { data: '{}' });
        const gapGeneration = LIVE_STREAMS['sid-1'].transportGeneration;
        const gapHasNoSource = LIVE_STREAMS['sid-1'].source === null;
        timers.shift()();
        reconnectStatuses.shift()({ active: true, replay_available: false });
        await Promise.resolve();
        await Promise.resolve();
        const replacementSource = createdSources[1];
        console.log(JSON.stringify({
          createdSourceCount: createdSources.length,
          cursorCalls,
          errorListenerCount,
          ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
          firstGeneration,
          gapGeneration,
          finalGeneration: LIVE_STREAMS['sid-1'].transportGeneration,
          gapHasNoSource,
          firstSourceClosed: firstSource.closed,
          replacementSourceClosed: replacementSource.closed,
          currentSourceIsReplacement: LIVE_STREAMS['sid-1'].source === replacementSource,
        }));
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["createdSourceCount"] == 2
    assert result["cursorCalls"] == 2
    assert result["errorListenerCount"] == 2
    assert result["ownerToken"] == 1
    assert result["firstGeneration"] == 2
    assert result["gapGeneration"] == 3
    assert result["finalGeneration"] == 4
    assert result["gapHasNoSource"] is True
    assert result["firstSourceClosed"] is True
    assert result["replacementSourceClosed"] is False
    assert result["currentSourceIsReplacement"] is True


def test_reconnect_probe_does_not_replace_same_id_new_owner_after_status_await():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    owner = _extract("_ownsActiveStreamOrBackground")
    recovery_owner = _extract("_currentPaneRecoveryOwnerLost")
    close_source = _extract("_closeSource")
    wire = _extract("_wireSSE")
    error_body = _extract_event_body("error")
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _terminalStateReached = false;
        let _streamFinalized = false;
        let _pendingStreamEndRecovery = false;
        let _reconnectAttempted = false;
        let eventSourceCalls = 0;
        let resolveStatus;
        const timers = [];
        const oldSource = { readyState: 1, close() { this.readyState = 2; } };
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [],
        };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: oldSource, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis.source = oldSource;
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = () => true;
        globalThis._deferStreamErrorIfOffline = () => false;
        globalThis._deferStreamErrorIfPageHidden = () => false;
        globalThis.recordClientSSEError = () => {};
        globalThis.api = () => new Promise((resolve) => { resolveStatus = resolve; });
        globalThis.setComposerStatus = () => {};
        globalThis.snapshotLiveTurnHtmlForSession = () => {};
        globalThis._clearLiveRunStatusTimer = () => {};
        globalThis.hideLiveRunStatus = () => {};
        globalThis.closeLiveStream = () => { throw new Error('owner retired during reconnect'); };
        globalThis._runJournalReplayParams = () => '';
        globalThis.document = { baseURI: 'http://localhost:8787/' };
        globalThis.location = { href: 'http://localhost:8787/' };
        globalThis.EventSource = function(url) {
          eventSourceCalls += 1;
          this.url = url;
          this.readyState = 0;
          this.close = () => {};
        };
        globalThis.setTimeout = (cb) => { timers.push(cb); return timers.length; };
        globalThis.clearTimeout = () => {};
        globalThis._restoreSettledSession = async () => false;
        globalThis._rememberRunJournalCursor = () => {};
        function _bailOutOfTerminalEventsFromStaleStream(){ return false; }
        function _retireLiveClosure(src){
          if(src&&typeof src.close==='function') src.close();
          _closureRetired = true;
        }
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + owner
        + """
        """
        + recovery_owner
        + """
        """
        + close_source
        + """
        """
        + wire
        + """
        const handler = async (e) => {
        """
        + error_body
        + """
        };
        await handler({ data: '{}' });
        timers.shift()();
        LIVE_STREAMS['sid-1'] = {
          streamId: 'stream-1',
          source: { readyState: 1, close() {} },
          ownerToken: 2,
          transportGeneration: 1,
        };
        resolveStatus({ active: true });
        await Promise.resolve();
        await Promise.resolve();
        console.log(JSON.stringify({
          eventSourceCalls,
          ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
          activeStreamId: S.activeStreamId,
        }));
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["eventSourceCalls"] == 0
    assert result["ownerToken"] == 2
    assert result["activeStreamId"] == "stream-1"


def test_same_stream_replacement_transfers_anchor_cleanup_lease():
    schedule_cleanup = _extract("_scheduleAnchorRegistryCleanup")
    script = textwrap.dedent(
        """
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        const registry = {};
        const timers = [];
        globalThis._anchorRegistry = registry;
        globalThis._anchorRegistryMap = new Map([['stream-1', registry]]);
        globalThis.setTimeout = (cb) => { timers.push(cb); return timers.length; };
        """
        + schedule_cleanup
        + """
        _scheduleAnchorRegistryCleanup(10);
        _anchorRegistry._cleanupOwnerToken = 2;
        timers[0]();
        const retained = _anchorRegistryMap.has('stream-1');
        _liveOwnerToken = 2;
        _scheduleAnchorRegistryCleanup(10);
        timers[1]();
        const cleaned = !_anchorRegistryMap.has('stream-1');
        console.log(JSON.stringify({ retained, cleaned }));
        """
    )
    proc = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["retained"] is True
    assert result["cleaned"] is True


@pytest.mark.parametrize("reject", [False, True])
def test_cancel_continuation_does_not_mutate_after_same_id_owner_token_replacement(reject: bool):
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    current_event_owner = _extract("_currentLiveEventSourceOwnsStream")
    owner = _extract("_ownsActiveStreamOrBackground")
    bail = _extract("_bailOutOfTerminalEventsFromStaleStream")
    close_source = _extract("_closeSource")
    cancel_body = _extract_event_body("cancel")
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _terminalStateReached = false;
        let _streamFinalized = false;
        let _persistTimer = null;
        let renderCalls = 0;
        let attachCalls = 0;
        let closeSourceCalls = 0;
        let resolveSession;
        let rejectSession;
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [{ role: 'assistant', content: 'old cancel state' }],
          toolCalls: [],
        };
        const liveSource = { readyState: 1, close() { closeSourceCalls += 1; } };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: liveSource, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis.source = liveSource;
        globalThis.api = () => new Promise((resolve, rejectPromise) => {
          resolveSession = resolve;
          rejectSession = rejectPromise;
        });
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = () => true;
        globalThis._clearStreamEndRecovery = () => {};
        globalThis._cancelThrottledSnapshotTimer = () => {};
        globalThis._clearAnchorProseIncrementalNode = () => {};
        globalThis._cancelAnimationFramePendingStreamRender = () => {};
        globalThis._streamFadeCleanupReduceMotionListener = () => {};
        globalThis._smdEndParser = () => {};
        globalThis.finalizeThinkingCard = () => {};
        globalThis._clearOwnerInflightState = () => {};
        globalThis._clearStreamHidden = () => {};
        globalThis._clearStreamNotificationBackground = () => {};
        globalThis._clearApprovalForOwner = () => {};
        globalThis._clearClarifyForOwner = () => {};
        globalThis._flushReasoningToAnchor = () => {};
        globalThis._applyToAnchor = () => {};
        globalThis._scheduleAnchorRegistryCleanup = () => {};
        globalThis._isMessagePaneNearBottom = () => true;
        globalThis._isMessageReaderUnpinned = () => false;
        globalThis._attachProjectedAnchorSceneToLastAssistant = () => { attachCalls += 1; };
        globalThis._carryForwardEphemeralTurnFields = (_current, next) => next;
        globalThis._hydrateTodosFromSession = () => {};
        globalThis._retireLiveClosure = () => { closeSourceCalls += 1; _closureRetired = true; };
        globalThis.clearLiveToolCards = () => {};
        globalThis.removeThinking = () => {};
        globalThis._markSessionViewed = () => {};
        globalThis.renderMessages = () => { renderCalls += 1; };
        globalThis.scrollToBottom = () => {};
        globalThis.assistantDisplayName = () => 'Hermes';
        globalThis.renderSessionList = () => {};
        globalThis._setActivePaneIdleIfOwner = () => {};
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + current_event_owner
        + """
        """
        + owner
        + """
        """
        + bail
        + """
        """
        + close_source
        + """
        const handler = async (e) => {
        """
        + cancel_body
        + """
        };
        const handlerPromise = handler({ data: JSON.stringify({ message: 'cancelled' }) });
        await Promise.resolve();
        S.activeStreamId = 'stream-1';
        S.messages = [{ role: 'assistant', content: 'replacement answer' }];
        LIVE_STREAMS['sid-1'] = {
          streamId: 'stream-1',
          source: { readyState: 1, close() {} },
          ownerToken: 2,
          transportGeneration: 1,
        };
        if("""
        + ("true" if reject else "false")
        + """){
          rejectSession(new Error('cancel fetch failed'));
        }else{
          resolveSession({
            session: {
              session_id: 'sid-1',
              messages: [{ role: 'assistant', content: 'stale cancel snapshot' }],
              message_count: 1,
            }
          });
        }
        await handlerPromise;
        Promise.resolve().then(() => Promise.resolve()).then(() => {
          console.log(JSON.stringify({
            renderCalls,
            attachCalls,
            closeSourceCalls,
            messages: S.messages,
            ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
            activeStreamId: S.activeStreamId,
          }));
        });
        """
    )
    proc = _run_node_script(script)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["renderCalls"] == 0
    assert result["attachCalls"] == 0
    assert result["messages"] == [{"role": "assistant", "content": "replacement answer"}]
    assert result["ownerToken"] == 2
    assert result["activeStreamId"] == "stream-1"


def test_warning_clear_timer_does_not_clear_replacement_owner_status():
    current_owner = _extract("_currentLiveOwnerEntry")
    current_owner_active = _extract("_currentLiveOwnerActive")
    current_event_owner = _extract("_currentLiveEventSourceOwnsStream")
    owner = _extract("_ownsActiveStreamOrBackground")
    bail = _extract("_bailOutOfTerminalEventsFromStaleStream")
    warning_body = _extract_event_body("warning")
    script = textwrap.dedent(
        """
        let activeSid = 'sid-1';
        let streamId = 'stream-1';
        let _liveOwnerToken = 1;
        let _closureRetired = false;
        let _terminalStateReached = false;
        let _streamFinalized = false;
        let closeCalls = 0;
        const statusCalls = [];
        const timers = [];
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'stream-1',
          messages: [],
        };
        const liveSource = { readyState: 1, close() {} };
        globalThis.LIVE_STREAMS = {
          'sid-1': { streamId: 'stream-1', source: liveSource, ownerToken: 1, transportGeneration: 1 }
        };
        globalThis.source = liveSource;
        globalThis.setComposerStatus = (value) => { statusCalls.push(value); };
        globalThis.showToast = () => {};
        globalThis.t = (key) => key;
        globalThis.setTimeout = (cb) => { timers.push(cb); return timers.length; };
        globalThis._scheduleAnchorRegistryCleanup = () => {};
        globalThis._closeSource = () => { closeCalls += 1; };
        globalThis._isActiveSession = () => true;
        globalThis._isSessionCurrentPane = () => true;
        """
        + current_owner
        + """
        """
        + current_owner_active
        + """
        """
        + current_event_owner
        + """
        """
        + owner
        + """
        """
        + bail
        + """
        const handler = (e) => {
        """
        + warning_body
        + """
        };
        handler({ data: JSON.stringify({ type: 'fallback', message: 'Stale fallback warning' }) });
        LIVE_STREAMS['sid-1'] = {
          streamId: 'stream-1',
          source: { readyState: 1, close() {} },
          ownerToken: 2,
          transportGeneration: 1,
        };
        timers[0]();
        console.log(JSON.stringify({
          closeCalls,
          statusCalls,
          ownerToken: LIVE_STREAMS['sid-1'].ownerToken,
        }));
        """
    )
    proc = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["closeCalls"] == 0
    assert result["statusCalls"] == ["Stale fallback warning"]
    assert result["ownerToken"] == 2
