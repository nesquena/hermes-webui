"""#5867: hands-free voice mode must recover from every stream terminal and
honor the configured silence grace on recognition end.

Two defects fixed in static/boot.js + static/messages.js:

1. Stuck on 'thinking': the only voice-mode exit used to be the done
   handler's autoReadLastAssistant call, so apperror/cancel/network-error
   terminals pinned the indicator at 'thinking' forever. Every terminal
   path already funnels through _setActivePaneIdleIfOwner, which now
   invokes window._voiceModeOnResponseComplete; a watchdog covers turns
   that die before a stream ever opens (e.g. send() fails pre-SSE).
2. Fast onend: Chromium endpointing fires onend well before the
   configured silence grace; the armed _silenceTimer must keep sole
   ownership of the auto-send instead of onend clearing it and sending
   immediately.

Review hardening folded into the fix:
- The funnel reports {outcome, sessionId, streamId} for the stream that
  settled: only 'done' speaks the last assistant row; cancel/error/settled
  resume listening silently; a terminal whose sessionId differs from
  _voiceModeThinkingSid is ignored entirely.
- The deferred done->speak callback captures a turn token; a new 'thinking'
  claim invalidates stale timers.
- 'thinking' holds while the composer holds a restored draft (watchdog and
  funnel paths alike) — resuming recognition would overwrite it.
- The watchdog's liveness check is `INFLIGHT[pin] || S.busy ||
  S.activeStreamId`: the pinned turn's inflight run and the visible
  session's own stream both count, so neither an idle-switch nor a
  background terminal can reopen the mic over a live run.
- The silence timer binds its pending send to the arming session; a
  mid-grace switch bails to listening (cross-session loadSession cancels
  the timer outright), while a same-session composer change at the
  deadline is the user correcting the utterance — the live composer text
  is sent.
- After a cross-session loadSession finishes, voice mode retires the
  outgoing session's recognizer (late callbacks detached) and reopens
  the mic only when the new chat is idle with an empty composer — a
  saved draft or a live run stays paused.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function


ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _brace_block(src: str, start: int) -> str:
    """Return the balanced {...} block starting at the first '{' after start."""
    brace = src.find("{", start)
    assert brace >= 0, "opening brace not found"
    depth = 0
    for i in range(brace, len(src)):
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[brace : i + 1]
    raise AssertionError("unbalanced braces")


def _extract_block(src: str, marker: str) -> str:
    start = src.find(marker)
    assert start >= 0, f"{marker!r} not found"
    return _brace_block(src, start)


def _extract_window_assign(src: str, name: str) -> str:
    start = src.find(f"window.{name}=function")
    assert start >= 0, f"window.{name} assignment not found"
    return src[start : src.find("{", start)] + _brace_block(src, start) + ";"


# --------------------------------------------------------------------------
# Source-level wiring assertions (no node required)
# --------------------------------------------------------------------------


def test_idle_funnel_invokes_voice_mode_completion_hook():
    idle_body = extract_function(MESSAGES_JS, "_setActivePaneIdleIfOwner")
    assert "setBusy(false)" in idle_body
    assert "window._voiceModeOnResponseComplete" in idle_body, (
        "the shared idle transition must release voice mode's 'thinking' pin"
    )
    # The hook learns which stream settled — a background terminal must not
    # release the owner pinned to another session.
    assert "sessionId:activeSid" in idle_body
    assert "streamId:streamId" in idle_body


def test_every_terminal_path_reaches_the_idle_funnel():
    # done keeps the autoRead hook AND flows through the funnel with an
    # explicit 'done' outcome — the only speakable terminal.
    done_body = _extract_block(MESSAGES_JS, "source.addEventListener('done'")
    assert "autoReadLastAssistant" in done_body
    assert "_terminalOutcome='done';_setActivePaneIdleIfOwner();" in done_body

    for marker, fn in [
        ("source.addEventListener('apperror'", None),
        ("source.addEventListener('cancel'", None),
        (None, "_handleStreamError"),
        (None, "_finalizeStreamEndFallback"),
    ]:
        body = (
            _extract_block(MESSAGES_JS, marker)
            if marker
            else extract_function(MESSAGES_JS, fn)
        )
        outcome = "cancel" if marker and "cancel" in marker else "error"
        assert f"_terminalOutcome='{outcome}';_setActivePaneIdleIfOwner();" in body

    # A settled-session restore observed no terminal event — the last row may
    # be a cancel marker, so it resumes listening without speech.
    restore_body = extract_function(MESSAGES_JS, "_restoreSettledSession")
    assert "_terminalOutcome='settled';_setActivePaneIdleIfOwner();" in restore_body


def test_onend_keeps_silence_grace_ownership():
    onend_body = _extract_block(BOOT_JS, "_recognition.onend=")
    assert "clearTimeout(_silenceTimer)" not in onend_body, (
        "onend must not clear the armed silence timer — doing so bypasses "
        "the configured grace and sends on the first endpointed pause"
    )
    assert "if(!_silenceTimer)" in onend_body, (
        "onend should only arm a grace timer when none is pending"
    )

    onresult_body = _extract_block(BOOT_JS, "_recognition.onresult=")
    assert "_armSilenceTimer();" in onresult_body
    arm_body = extract_function(BOOT_JS, "_armSilenceTimer")
    assert "_voiceModeSend();" in arm_body
    assert "_voiceSilenceMs()" in arm_body


def test_voice_mode_send_and_deactivate_clear_pending_timers():
    send_body = extract_function(BOOT_JS, "_voiceModeSend")
    assert "clearTimeout(_silenceTimer);" in send_body

    deactivate_body = extract_function(BOOT_JS, "_deactivate")
    assert "clearTimeout(_silenceTimer);" in deactivate_body
    assert "_clearThinkingWatchdog();" in deactivate_body


def test_thinking_watchdog_declared_and_armed():
    assert "let _thinkingWatchdog=null;" in BOOT_JS
    arm_body = extract_function(BOOT_JS, "_armThinkingWatchdog")
    assert "_thinkingWatchdog=setInterval" in arm_body
    # Liveness belongs to the pinned turn's inflight run; the visible
    # session's own stream counts as busy too.
    assert "INFLIGHT[pin]" in arm_body
    assert "S.busy||S.activeStreamId" in arm_body
    assert "_startListening();" in arm_body
    # A restored unsent draft must survive the re-arm — recognition results
    # write straight into the textarea.
    assert "ta.value" in arm_body

    set_state_body = extract_function(BOOT_JS, "_setState")
    assert "_armThinkingWatchdog();" in set_state_body
    assert "_clearThinkingWatchdog();" in set_state_body
    # A fresh 'thinking' claim invalidates a previous turn's deferred speak.
    assert "_voiceModeTurnSeq" in set_state_body
    assert "_voiceModeResponseTimer" in set_state_body


def test_response_complete_hook_defined():
    assert "window._voiceModeOnResponseComplete=function" in BOOT_JS
    hook = _extract_window_assign(BOOT_JS, "_voiceModeOnResponseComplete")
    assert "_voiceModeState==='thinking'" in hook
    assert "_speakResponse();" in hook
    # Outcome gates speech; sessionId gates ownership (background terminals
    # can't release another session's pin); the seq re-check invalidates a
    # stale deferred speak.
    assert "details.outcome" in hook
    assert "details.sessionId" in hook
    assert "_voiceModeThinkingSid" in hook
    assert "_voiceModeTurnSeq" in hook
    assert "_voiceModeResponseTimer" in hook


def test_silence_timer_binds_send_to_owner():
    # The pending send captures its owning session + utterance at arm time;
    # the guard lives in _voiceModeSend (the literal timer-callback shape is
    # pinned by test_issue4761_voice_mode_config).
    arm_body = extract_function(BOOT_JS, "_armSilenceTimer")
    assert "_voiceSendOwner=" in arm_body
    assert "S.session&&S.session.session_id" in arm_body
    send_body = extract_function(BOOT_JS, "_voiceModeSend")
    assert "owner.sid" in send_body
    assert "_startListening(); return;" in send_body
    # The guard is session-scoped only: a same-session composer change is
    # the user correcting the utterance — the live text must be sent, not
    # bailed on (comparing owner.text here dropped the send).
    assert "owner.text" not in send_body
    assert "ta.value" in send_body
    # Cross-session loadSession cancels a pending timer outright.
    assert "window._voiceModeCancelPendingSend" in BOOT_JS
    assert "window._voiceModeCancelPendingSend" in SESSIONS_JS


def test_session_loaded_hook_defined_and_called():
    assert "window._voiceModeOnSessionLoaded=function" in BOOT_JS
    hook = _extract_window_assign(BOOT_JS, "_voiceModeOnSessionLoaded")
    # Only the 'listening' pin can be stranded; thinking/speaking own
    # their lifecycle via the watchdog and TTS recovery.
    assert "_voiceModeState!=='listening'" in hook
    # The outgoing session's recognizer is retired — its late callbacks
    # must be detached before abort so they can't write into the new
    # session's composer.
    for handler in ("onresult", "onend", "onerror"):
        assert f"_recognition.{handler}=null" in hook
    # Reopen the mic only on an idle chat with an empty composer.
    assert "S.busy" in hook
    assert "S.activeStreamId" in hook
    assert "INFLIGHT" in hook
    assert "ta.value" in hook
    assert "_startListening();" in hook
    # The cross-session call site runs after the draft restore, so the
    # composer already reflects the new session's saved state.
    idx_draft = SESSIONS_JS.find("_restoreComposerDraft(_draft, sid")
    idx_hook = SESSIONS_JS.find("window._voiceModeOnSessionLoaded(sid)")
    assert -1 < idx_draft < idx_hook


# --------------------------------------------------------------------------
# Behavioral simulation via node (executes the real extracted functions)
# --------------------------------------------------------------------------

pytestmark_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, (
        f"node subprocess failed:\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


_HARNESS = r"""
const calls = [];
globalThis.window = globalThis;
globalThis.localStorage = {
  getItem: (k) => (k === 'hermes-voice-silence-ms' ? '300' : null),
};
const ta = { value: '' };
const indicator = { className: '' };
const label = { textContent: '' };
const bar = { style: {} };
const _locale = { _speech: 'en-US' };
let _voiceModeActive = true;
let _voiceModeState = 'idle';
let _recognition = null;
let _silenceTimer = null;
let _voiceSendOwner = null;
let _voiceModeThinkingSid = null;
let _browserTtsKeepAlive = null;
let _browserTtsWatchdog = null;
let _browserTtsSuppressNextErrorRearm = false;
let _thinkingWatchdog = null;
let _voiceModeResponseTimer = null;
let _voiceModeTurnSeq = 0;
const S = { session: { session_id: 'sid-5867' }, busy: false, activeStreamId: null };
let INFLIGHT = {};
let sendCalls = 0;
let sendTexts = [];
const SEND_LIVE = __SEND_LIVE__;
function send() {
  sendCalls += 1;
  sendTexts.push(ta.value);
  const text = ta.value;
  ta.value = ''; // send() consumes the composer contents up front
  const sid = S.session && S.session.session_id;
  if (SEND_LIVE) { S.busy = true; S.activeStreamId = 'st-1'; INFLIGHT[sid] = { messages: [] }; }
  else { ta.value = text; delete INFLIGHT[sid]; } // a pre-stream failure restores the unsent draft
}
function _speakResponse() { calls.push('speak'); _setState('speaking'); }
function t(k) { return k; }
function showToast() {}
function autoResize() {}
function _micOriginNeedsSecureContext() { return false; }
function _deactivate() {}
// Dump the standard snapshot after ms; `extra` may be a thunk so late-bound
// fields (draft, owner) are read at fire time, not when _dump is armed.
function _dump(extra, ms) {
  setTimeout(() => {
    console.log(JSON.stringify(Object.assign(
      { calls, state: _voiceModeState, sendCalls },
      typeof extra === 'function' ? extra() : (extra || {}))));
    process.exit(0);
  }, ms);
}
let _recInstance = null;
class SpeechRecognition {
  constructor() { _recInstance = this; }
  start() {}
  abort() { calls.push('abort'); if (this.onend) this.onend(); }
  stop() {}
}
// Compress the thinking watchdog's real interval so the test stays fast;
// the polling logic itself is unchanged.
const _origSetInterval = globalThis.setInterval;
globalThis.setInterval = (fn, ms, ...a) => _origSetInterval(fn, ms >= 4000 ? 25 : ms, ...a);
__FNS__
// Record invocations of the extracted _startListening for assertions.
const _innerStartListening = _startListening;
_startListening = function () { calls.push('listen'); return _innerStartListening.apply(this, arguments); };
"""


_FUNNEL_FN = extract_function(MESSAGES_JS, "_setActivePaneIdleIfOwner")


def _harness(extra_fns: str = "", send_live: bool = True) -> str:
    fns = "\n".join(
        extract_function(BOOT_JS, name)
        for name in (
            "_voiceSilenceMs",
            "_clearBrowserTtsRecovery",
            "_clearThinkingWatchdog",
            "_armThinkingWatchdog",
            "_setState",
            "_startListening",
            "_armSilenceTimer",
            "_voiceModeSend",
        )
    )
    fns += "\n" + _extract_window_assign(BOOT_JS, "_voiceModeOnResponseComplete")
    fns += "\n" + _extract_window_assign(BOOT_JS, "_voiceModeCancelPendingSend")
    fns += "\n" + _extract_window_assign(BOOT_JS, "_voiceModeOnSessionLoaded")
    fns += "\n" + extra_fns
    return _HARNESS.replace("__FNS__", fns).replace(
        "__SEND_LIVE__", "true" if send_live else "false"
    )


def _funnel_setup(active: bool, inflight: str, sid="sid-5867", stream="st-1") -> str:
    """Closure deps for the extracted _setActivePaneIdleIfOwner: activeSid/
    streamId name the settling stream (attachLiveStream's params), not the
    UI's active session."""
    return f"""
let activeSid = '{sid}';
let streamId = '{stream}';
let _terminalOutcome = 'settled';
function _isActiveSession() {{ return {str(active).lower()}; }}
INFLIGHT = {inflight};
let setBusyCalls = [];
function setBusy(v) {{ setBusyCalls.push(v); S.busy = v; }}
function setComposerStatus() {{}}
function setStatus() {{}}
"""


# Same-session funnel: the visible session's own stream settled.
_FUNNEL_SAME = _funnel_setup(True, "{ 'sid-5867': { messages: [] } }")
# A background stream settled while the UI session is not its owner and has
# no INFLIGHT entry — the broad idle condition still admits the hook call.
_FUNNEL_BG = _funnel_setup(False, "{}", sid="bg-sid", stream="st-bg")
# A's stream settled while the visible B is itself in-flight.
_FUNNEL_BG_LIVE = _funnel_setup(False, "{ 'sid-B': { messages: [] } }", stream="st-A")

# One listening turn: recognizer up, a final transcript captured.
_UTTERANCE = r"""
    _startListening();
    _recInstance.onresult({
      resultIndex: 0,
      results: [{ 0: { transcript: 'draft' }, isFinal: true }],
    });
"""


@pytestmark_node
def test_onend_defers_to_silence_grace_timer():
    """Fast onend after a final result must NOT send immediately — the armed
    silence timer stays the sole auto-send gate (#5867 problem 2)."""
    script = (
        _harness()
        + _UTTERANCE
        + r"""
    _recInstance.onend(); // endpointing fires right after the final result
    setTimeout(() => { calls.push(['early', sendCalls, _voiceModeState]); }, 80);
    _dump(null, 600); // 'late' marker folded into the snapshot below
    """
    )
    out = _run_node(script)
    early = next(c for c in out["calls"] if isinstance(c, list) and c[0] == "early")
    assert early[1] == 0 and early[2] == "listening", (
        f"fast onend must not send before the silence grace: {out}"
    )
    assert out["sendCalls"] == 1 and out["state"] == "thinking", (
        f"silence timer should own the send after the grace: {out}"
    )


@pytestmark_node
def test_terminal_hook_recovers_thinking_to_speaking():
    """A stream terminal (apperror/cancel/error) reaches
    window._voiceModeOnResponseComplete via the idle funnel, releasing the
    'thinking' pin into the speak transition (#5867 problem 1)."""
    script = (
        _harness()
        + _UTTERANCE
        + r"""
    setTimeout(() => {
      // Simulate the terminal-path funnel call (e.g. apperror tail).
      window._voiceModeOnResponseComplete();
    }, 450);
    _dump(null, 1000);
    """
    )
    out = _run_node(script)
    assert out["sendCalls"] == 1
    assert "speak" in out["calls"], (
        f"terminal hook must release thinking into speak: {out}"
    )
    assert out["state"] == "speaking"


@pytestmark_node
def test_thinking_watchdog_recovers_when_no_terminal_arrives():
    """If the stream dies without a terminal event reaching the funnel (and
    the composer holds no restored draft), the watchdog must re-arm
    listening instead of pinning at 'thinking' forever."""
    script = (
        _harness(send_live=True)
        + _UTTERANCE
        + r"""
    // grace fires ~300ms -> _voiceModeSend -> send() consumes the composer
    // and opens the stream, which then dies without ever emitting a
    // terminal event -> watchdog (compressed to 25ms) re-arms listening.
    setTimeout(() => {
      S.busy = false; S.activeStreamId = null;
      delete INFLIGHT['sid-5867']; // the dead turn left no inflight run
    }, 350);
    _dump(null, 800);
    """
    )
    out = _run_node(script)
    assert out["sendCalls"] == 1
    assert "listen" in out["calls"], (
        f"watchdog must re-arm listening after sustained dead 'thinking': {out}"
    )
    assert out["state"] == "listening"


@pytestmark_node
def test_onend_without_text_restarts_listening():
    """No captured text keeps the pre-existing restart behavior."""
    script = (
        _harness()
        + r"""
    _startListening();
    _recInstance.onend(); // ended with no speech
    _dump(null, 700);
    """
    )
    out = _run_node(script)
    assert out["sendCalls"] == 0
    assert "listen" in out["calls"]
    assert out["state"] == "listening"


@pytest.mark.parametrize(
    ("outcome", "expect_speak", "end_state"),
    [
        # Stop/cancel and apperror/network-error funnel into the hook: voice
        # mode must clear 'thinking' and go back to listening without
        # reading the partial reply or the cancel marker aloud.
        ("cancel", False, "listening"),
        ("error", False, "listening"),
        # 'done' keeps the delayed speak path — regression guard for the
        # outcome gating itself.
        ("done", True, "speaking"),
    ],
)
@pytestmark_node
def test_terminal_outcome_funnel(outcome, expect_speak, end_state):
    script = (
        _harness(extra_fns=_FUNNEL_FN)
        + _FUNNEL_SAME
        + _UTTERANCE
        + f"""
    // silence grace (~300ms) -> _voiceModeSend -> 'thinking', stream live
    setTimeout(() => {{ _terminalOutcome='{outcome}';_setActivePaneIdleIfOwner(); }}, 450);
    _dump(null, {1400 if expect_speak else 1000});
    """
    )
    out = _run_node(script)
    assert ("speak" in out["calls"]) == expect_speak, out
    assert out["state"] == end_state
    if not expect_speak:
        assert "listen" in out["calls"], f"terminal must resume listening: {out}"


@pytestmark_node
def test_stale_done_callback_cannot_speak_over_new_turn():
    """A done terminal's delayed speak must not fire once a new turn has
    claimed 'thinking' — the turn token invalidates the stale callback even
    though state is 'thinking' again when it fires."""
    script = (
        _harness(extra_fns=_FUNNEL_FN)
        + _FUNNEL_SAME
        + _UTTERANCE
        + r"""
    // Turn 1 sends (~300ms) and its done terminal schedules speak at +400ms.
    setTimeout(() => { _terminalOutcome='done';_setActivePaneIdleIfOwner(); }, 450);
    // 100ms later the user starts a new turn: state returns to 'thinking'
    // under a fresh turn token before the stale callback's 400ms deadline.
    setTimeout(() => {
      _setState('listening');   // user-visible transition between turns
      ta.value = 'next turn';   // the new draft the user is sending
      _voiceModeSend();
    }, 550);
    _dump(null, 1400);
    """
    )
    out = _run_node(script)
    assert "speak" not in out["calls"], (
        f"stale turn-1 callback must not speak over the new stream: {out}"
    )
    assert out["sendCalls"] == 2
    assert out["state"] == "thinking"


@pytestmark_node
def test_background_terminal_cannot_release_voice_owner():
    """Session B owns voice mode ('thinking', failed-send draft restored in
    the composer) while background stream A settles. The broad idle
    condition admits the hook call (_isActiveSession false, no INFLIGHT[B]),
    but A's terminal must not release B: state, owner token, and draft are
    untouched and recognition does not resume."""
    script = (
        _harness(extra_fns=_FUNNEL_FN)
        + _FUNNEL_BG
        + _UTTERANCE
        + r"""
    // grace (~300ms) -> _voiceModeSend pins _voiceModeThinkingSid='sid-5867'
    // and enters 'thinking'.
    setTimeout(() => {
      ta.value = 'restored draft';  // send() failure restored the draft
      S.busy = false;               // B's send is between admission stages
      _terminalOutcome='error';_setActivePaneIdleIfOwner(); // stream A's terminal reaches the funnel
    }, 450);
    _dump(() => ({ owner: _voiceModeThinkingSid, draft: ta.value }), 1000);
    """
    )
    out = _run_node(script)
    # The only 'listen' is the initial _startListening() — nothing after the
    # background terminal may resume recognition or speak for session B.
    assert out["calls"].count("listen") == 1 and "speak" not in out["calls"], (
        f"background terminal must not touch the other session's voice mode: {out}"
    )
    assert out["state"] == "thinking"
    assert out["owner"] == "sid-5867"
    assert out["draft"] == "restored draft"


@pytestmark_node
def test_error_terminal_preserves_restored_draft():
    """Same-session complement: B's own error terminal with a restored draft
    in the composer holds 'thinking' instead of resuming recognition over
    the draft. Clearing the draft lets the watchdog resume listening."""
    script = (
        _harness(extra_fns=_FUNNEL_FN)
        + _FUNNEL_SAME
        + _UTTERANCE
        + r"""
    setTimeout(() => {
      ta.value = 'restored draft';
      S.busy = false; S.activeStreamId = null;
      _terminalOutcome='error';_setActivePaneIdleIfOwner(); // B's own terminal
    }, 450);
    setTimeout(() => {
      calls.push(['withDraft', _voiceModeState, ta.value]);
      ta.value = '';                    // user clears the restored draft
    }, 750);
    setTimeout(() => { calls.push(['afterClear', _voiceModeState]); }, 950);
    _dump(null, 1000);
    """
    )
    calls = _run_node(script)["calls"]
    with_draft = next(c for c in calls if isinstance(c, list) and c[0] == "withDraft")
    after_clear = next(
        c for c in calls if isinstance(c, list) and c[0] == "afterClear"
    )
    assert "speak" not in calls
    assert with_draft[1] == "thinking" and with_draft[2] == "restored draft", (
        f"error terminal must not resume recognition over a restored draft: {calls}"
    )
    assert after_clear[1] == "listening", (
        f"clearing the draft must let the watchdog resume listening: {calls}"
    )


@pytestmark_node
def test_switch_to_idle_session_keeps_pinned_turn_thinking():
    """Voice turn running in A, user switches to idle chat B: the visible
    S.busy/S.activeStreamId clear, but INFLIGHT[A] still holds the live run.
    The watchdog must judge by the pinned owner — stay 'thinking', start no
    new recognizer — or a spoken aside would be sent as a new turn in B."""
    script = (
        _harness(extra_fns=_FUNNEL_FN)
        + _FUNNEL_SAME
        + _UTTERANCE
        + r"""
    // grace (~300ms) -> _voiceModeSend pins 'sid-5867' and enters 'thinking';
    // send() leaves INFLIGHT['sid-5867'] + S.busy for the live stream.
    // The 'thinking' entry's own +300ms _startListening lands ~630ms, so the
    // switch marker is placed after it to isolate post-switch listens.
    setTimeout(() => {
      S.session = { session_id: 'sid-B' };
      S.busy = false; S.activeStreamId = null; // idle B (sessions.js ~2894)
      calls.push(['switched']);
    }, 700);
    _dump(() => ({ owner: _voiceModeThinkingSid }), 1500);
    """
    )
    out = _run_node(script)
    switch_idx = next(
        i for i, c in enumerate(out["calls"]) if isinstance(c, list) and c[0] == "switched"
    )
    post_switch = out["calls"][switch_idx + 1 :]
    assert "listen" not in post_switch and "speak" not in post_switch, (
        f"idle-session switch must not resume recognition on the pinned turn: {out}"
    )
    assert out["state"] == "thinking"
    assert out["owner"] == "sid-5867"


@pytestmark_node
def test_background_terminal_during_visible_stream_keeps_thinking():
    """A finishes in the background while B is streaming: A's terminal clears
    INFLIGHT[A] and the funnel skips the hook (B is the in-flight pane). The
    watchdog must still see the visible session's live run — 'thinking' holds
    until B goes idle instead of reopening the mic into B's stream."""
    script = (
        _harness(extra_fns=_FUNNEL_FN)
        + _FUNNEL_BG_LIVE
        + _UTTERANCE
        + r"""
    // grace (~300ms) -> _voiceModeSend pins 'sid-5867', send() leaves
    // INFLIGHT['sid-5867'] and S.busy for A's live stream.
    setTimeout(() => {
      // User is now on B, which is itself streaming; A's terminal cleared
      // INFLIGHT[A] and reached the funnel, which skipped the voice hook.
      S.session = { session_id: 'sid-B' };
      S.busy = true; S.activeStreamId = 'st-B';
      delete INFLIGHT['sid-5867'];
      _terminalOutcome='done';_setActivePaneIdleIfOwner(); // funnel guard skips: B is in-flight
      calls.push(['bStreaming']);
    }, 700);
    setTimeout(() => {
      // B goes idle; only now may the watchdog release 'thinking'.
      S.busy = false; S.activeStreamId = null;
      delete INFLIGHT['sid-B'];
      calls.push(['bIdle']);
    }, 1100);
    _dump(() => ({ owner: _voiceModeThinkingSid }), 1600);
    """
    )
    out = _run_node(script)
    marker = lambda name: next(
        i
        for i, c in enumerate(out["calls"])
        if isinstance(c, list) and c[0] == name
    )
    during = out["calls"][marker("bStreaming") + 1 : marker("bIdle")]
    assert "listen" not in during and "speak" not in during, (
        f"visible streaming session must keep the pinned turn 'thinking': {out}"
    )
    assert out["state"] == "listening", (
        f"once B is idle the watchdog may resume listening: {out}"
    )


@pytestmark_node
def test_pending_send_owner_bound_cross_session():
    """A silence timer armed on session A must not fire its send after a
    mid-grace switch to B (loadSession restores B's draft into the
    composer) — it bails back to listening, leaving the composer
    untouched. A same-session change is different: that is the user
    correcting the utterance and is covered by
    test_composer_edit_during_grace_sends_corrected_text."""
    script = (
        _harness()
        + _UTTERANCE.replace("'draft'", "'hello'")
        + """
    // silence timer armed for 'sid-5867' + utterance 'hello'; ~300ms grace.
    setTimeout(() => {
      S.session = { session_id: 'sid-B' };
      ta.value = 'B saved draft';
      calls.push(['mutated']);
    }, 200);
    _dump(() => ({ draft: ta.value }), 900);
    """
    )
    out = _run_node(script)
    assert out["sendCalls"] == 0, (
        f"pending send must not fire after the owning session changed: {out}"
    )
    assert out["state"] == "listening"
    assert out["draft"] == "B saved draft", (
        f"composer must survive the bailed send: {out}"
    )


@pytestmark_node
def test_composer_edit_during_grace_sends_corrected_text():
    """Editing the recognized utterance during the silence grace is a
    correction, not a bail: at the deadline the live composer text is
    sent. A recognition result arriving after the send is a fresh
    utterance — it must not overwrite the correction back into a
    not-yet-sent composer."""
    script = (
        _harness()
        + _UTTERANCE.replace("'draft'", "'hello'")
        + r"""
    // Timer armed for 'sid-5867' (~300ms grace); the user edits mid-grace.
    setTimeout(() => { ta.value = 'hello corrected'; }, 200);
    // The send fires ~300ms; a recognition result afterwards starts a new
    // utterance rather than clobbering the correction that already sent.
    setTimeout(() => {
      calls.push(['afterSend', sendCalls, ta.value]);
      _recInstance.onresult({
        resultIndex: 0,
        results: [{ 0: { transcript: 'next' }, isFinal: true }],
      });
    }, 450);
    _dump(() => ({ sends: sendTexts }), 950);
    """
    )
    out = _run_node(script)
    after_send = next(
        c for c in out["calls"] if isinstance(c, list) and c[0] == "afterSend"
    )
    assert after_send[1] == 1 and after_send[2] == "", (
        f"the corrected utterance must send at the grace deadline: {out}"
    )
    # The second send's text carries the recognizer's retained _finalText as
    # a prefix (a fresh recognizer resets it) — the part that matters is the
    # correction having been sent first, as its own turn.
    assert out["sends"][0] == "hello corrected" and out["sends"][1].endswith("next"), (
        f"the edited text is sent first; a later result is its own turn: {out}"
    )
    assert out["state"] == "thinking"


@pytest.mark.parametrize(
    ("composer_after_load", "busy", "expect_relisten"),
    [
        # Idle chat, empty composer — the ended recognizer is retired and
        # the mic reopens (the bug left 'listening' with nothing live).
        ("", False, True),
        # A restored draft keeps the mic paused — the user's text wins.
        ("B saved draft", False, False),
        # A live run on the new session must not be steered into.
        ("", True, False),
    ],
)
@pytestmark_node
def test_session_loaded_hook_reopens_mic_only_on_idle_empty_composer(
    composer_after_load, busy, expect_relisten
):
    """After loadSession's cross-switch cancels the pending send and the
    new session's composer is in place, _voiceModeOnSessionLoaded retires
    the stale recognizer (late callbacks detached) and reopens the mic
    only when the chat is idle and its composer is empty."""
    script = (
        _harness()
        + _UTTERANCE
        + f"""
    // Silence timer armed on A; endpointing may already have ended the
    // recognizer. Switch mid-grace to B, in loadSession's order:
    const oldRec = _recInstance;
    setTimeout(() => {{
      window._voiceModeCancelPendingSend();           // sessions.js ~2277
      S.session = {{ session_id: 'sid-B' }};
      S.busy = {str(busy).lower()};
      ta.value = {json.dumps(composer_after_load)};   // B's draft state
      window._voiceModeOnSessionLoaded('sid-B');      // sessions.js ~2947
      calls.push([
        'afterHook',
        _recInstance === oldRec,
        !oldRec.onresult && !oldRec.onend && !oldRec.onerror,
      ]);
    }}, 200);
    _dump(() => ({{ draft: ta.value }}), 900);
    """
    )
    out = _run_node(script)
    after_hook = next(
        c for c in out["calls"] if isinstance(c, list) and c[0] == "afterHook"
    )
    assert "abort" in out["calls"], (
        f"the outgoing session's recognizer must be retired: {out}"
    )
    assert after_hook[2] is True, (
        f"late callbacks on the stale recognizer must be detached: {out}"
    )
    assert out["sendCalls"] == 0, f"no send may fire after the switch: {out}"
    assert out["draft"] == composer_after_load, (
        f"the new session's composer must survive the hook: {out}"
    )
    listen_count = out["calls"].count("listen")
    if expect_relisten:
        assert listen_count == 2 and after_hook[1] is False, (
            f"idle empty chat must reopen the mic with a fresh recognizer: {out}"
        )
        assert out["state"] == "listening"
    else:
        assert listen_count == 1 and after_hook[1] is True, (
            f"draft/live-run chat must stay paused without a new recognizer: {out}"
        )


@pytestmark_node
def test_loadsession_cancel_drops_pending_send():
    """The cross-session loadSession hook cancels an armed silence timer
    outright so it can never fire after the switch."""
    script = (
        _harness()
        + _UTTERANCE
        + r"""
    setTimeout(() => {
      S.session = { session_id: 'sid-B' };
      window._voiceModeCancelPendingSend(); // loadSession's cross-switch call
    }, 200);
    _dump(null, 900);
    """
    )
    out = _run_node(script)
    assert out["sendCalls"] == 0, f"cancelled timer must never send: {out}"


@pytestmark_node
def test_thinking_watchdog_preserves_restored_draft():
    """send() failing pre-stream restores the typed text into the composer.
    The watchdog must not resume recognition while that draft sits there —
    the next onresult would overwrite it — but must resume once cleared."""
    script = (
        _harness(send_live=False)
        + _UTTERANCE
        + r"""
    // grace (~300ms) -> _voiceModeSend -> send() fails pre-stream and
    // restores the draft into the composer.
    // Watchdog polls at 25ms (compressed); after ~450ms it would normally
    // have resumed listening — with the draft it must hold 'thinking'.
    setTimeout(() => {
      calls.push(['withDraft', _voiceModeState]);
      ta.value = '';          // user clears the restored draft
    }, 750);
    setTimeout(() => { calls.push(['afterClear', _voiceModeState]); }, 900);
    _dump(null, 950);
    """
    )
    calls = _run_node(script)["calls"]
    with_draft = next(c for c in calls if isinstance(c, list) and c[0] == "withDraft")
    after_clear = next(
        c for c in calls if isinstance(c, list) and c[0] == "afterClear"
    )
    assert with_draft[1] == "thinking", (
        f"watchdog must not resume recognition over a restored draft: {calls}"
    )
    assert after_clear[1] == "listening", (
        f"clearing the draft must let the watchdog resume listening: {calls}"
    )
