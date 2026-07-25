import json
import shutil
import subprocess
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
        let _terminalStateReached = false;
        let _streamFinalized = false;
        let _pendingStreamEndRecovery = true;
        let _streamEndRecoveryAttempts = {attempts};
        let _streamEndRecoveryTimer = null;
        globalThis.S = {{
          session: {{ session_id: 'sid-1' }},
          activeStreamId: {current_active_stream!r},
          messages: [{{ role: 'assistant', content: 'rebuilt transcript placeholder' }}]
        }};
        globalThis.assistantText = {assistant_text!r};
        globalThis.INFLIGHT = {{}};
        globalThis.assistantBody = {{ isConnected: {str(bool(assistant_text)).lower()}, textContent: {assistant_text!r} }};
        globalThis.$ = () => null;
        globalThis._isActiveSession = () => active;
        globalThis._isSessionCurrentPane = () => {str((active if current_pane is None else current_pane)).lower()};
        globalThis._clearStreamEndRecovery = () => {{
          _pendingStreamEndRecovery = false;
          _streamEndRecoveryAttempts = 0;
          _streamEndRecoveryTimer = null;
        }};
        globalThis._scheduleStreamEndRecovery = (_source, delay = 180) => {{
          _pendingStreamEndRecovery = true;
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
        {fallback}
        {visible}
        {reconcile}
        {recovery}
        (async () => {{
          for (let i = 0; i < {repeats}; i += 1) {{
            await _runStreamEndRecovery({{}});
            if (i + 1 < {repeats}) _pendingStreamEndRecovery = true;
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
            attempts: _streamEndRecoveryAttempts,
            pending: _pendingStreamEndRecovery,
            finalized: _streamFinalized,
            terminal: _terminalStateReached
          }}));
        }})().catch((error) => {{
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        }});
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
    return json.loads(proc.stdout)


def _run_restore_stale_guard_case():
    restore = _extract("_restoreSettledSession")
    script = textwrap.dedent(
        """
        let closeCalls = 0;
        let apiCalls = 0;
        let activeSid = 'sid-1';
        let streamId = 'old-stream';
        let _streamFinalized = false;
        globalThis.S = {
          session: { session_id: 'sid-1' },
          activeStreamId: 'replacement-stream',
        };
        globalThis._isActiveSession = () => true;
        globalThis.api = async () => {
          apiCalls += 1;
          return { session: null };
        };
        globalThis._closeSource = () => { closeCalls += 1; };
        """ + restore + """
        (async () => {
          const status = await _restoreSettledSession({}, { status: true });
          console.log(JSON.stringify({
            status,
            closeCalls,
            apiCalls,
            activeStreamId: S.activeStreamId,
          }));
        })().catch((error) => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
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


def test_long_tail_recovery_terminates_without_rescheduling_when_stream_is_inactive():
    result = _run_recovery_case(
        active=True,
        restore_results=["active", False],
        attempts=15,
        assistant_text="final streamed answer",
        stream_status={"active": False, "replay_available": False},
    )

    assert result["wireCalls"] == 0
    assert result["scheduleDelays"] == []
    assert result["renderCalls"] == 0
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
