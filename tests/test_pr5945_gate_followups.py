"""Gate follow-ups for the background-attention notification PR.

1. A SELECTED session in a HIDDEN tab must deliver — being "active" only
   suppresses delivery while the page is actually visible.
2. Async delivery ownership is a unique generation, not the reusable
   ``sid:kind:count`` key: an A→B→A sequence must not let the FIRST A's
   late callbacks mark the second A delivered or eat its retry state, and
   a token-backed ``shouldDeliver`` runs immediately before display so
   cleared/replaced attention cannot surface late.
"""
import json
import subprocess

import pytest

from tests.test_session_attention_sound import (
    MESSAGES_JS,
    NODE,
    REPO,
    SESSIONS_JS,
    _function_source,
)


def _run_node_probe(script_body: str) -> dict:
    if NODE is None:  # pragma: no cover - node is installed in CI
        pytest.skip("node not on PATH")
    functions = "\n".join(
        (
            _function_source(MESSAGES_JS, "_attentionSoundKey"),
            _function_source(MESSAGES_JS, "_hasAttentionNotificationKey"),
            _function_source(MESSAGES_JS, "_markAttentionNotificationKey"),
            _function_source(MESSAGES_JS, "_clearAttentionNotificationKey"),
            _function_source(MESSAGES_JS, "_deliverAttentionNotification"),
            _function_source(MESSAGES_JS, "sendBrowserNotification"),
            _function_source(MESSAGES_JS, "_notificationOptions"),
            _function_source(MESSAGES_JS, "_showPwaNotification"),
        )
    )
    script = f"""
global.window = global;
global.location = {{origin: 'https://example.test', href: 'https://example.test/'}};
global._notificationsEnabled = true;
global._isBackgroundedForBrowserNotification = () => !!document.hidden;
global._sessionUrlForSid = sid => `/?session=${{sid}}`;
global.assistantDisplayName = () => 'Hermes';
const sw_shown = [];
// Node >=21 ships a read-only global `navigator`; plain assignment silently
// no-ops, so every override must go through defineProperty.
const _setNavigator = nav => Object.defineProperty(globalThis, 'navigator', {{value: nav, configurable: true}});
_setNavigator({{serviceWorker: {{getRegistration: () => Promise.resolve({{
  active: true,
  showNotification: (title, opts) => {{sw_shown.push({{title, tag: opts && opts.tag}}); return Promise.resolve();}},
}})}}}});
const direct_shown = [];
function Notification(title, opts) {{ direct_shown.push({{title, tag: opts && opts.tag}}); }}
Notification.permission = 'granted';
global.Notification = Notification;
{functions}
{script_body}
"""
    completed = subprocess.run(
        [NODE, "-e", script], cwd=REPO, check=True, text=True, capture_output=True
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_selected_session_delivers_while_tab_is_hidden():
    """The gate's exact repro, inverted: document.hidden + active session +
    an approval → delivery count must be > 0 through the REAL path."""
    result = _run_node_probe(
        """
global.document = {hidden: true, hasFocus: () => false};
global.S = {session: {session_id: 'target'}};
const ok = _deliverAttentionNotification('target','approval',1,'Approval required','Build');
setTimeout(() => console.log(JSON.stringify({
  ok, activeHiddenDeliveryCount: sw_shown.length + direct_shown.length,
  delivered: _hasAttentionNotificationKey('target','approval',1),
})), 20);
"""
    )
    assert result["activeHiddenDeliveryCount"] > 0
    assert result["delivered"] is True


def test_selected_session_stays_quiet_while_tab_is_visible():
    result = _run_node_probe(
        """
global.document = {hidden: false, hasFocus: () => true};
global.S = {session: {session_id: 'target'}};
const ok = _deliverAttentionNotification('target','approval',1,'Approval required','Build');
setTimeout(() => console.log(JSON.stringify({
  ok, count: sw_shown.length + direct_shown.length,
})), 20);
"""
    )
    assert result["count"] == 0


def test_stale_a_b_a_callbacks_cannot_poison_current_claim():
    """The gate's A→B→A repro: the FIRST A resolves late (after B and a NEW A
    replaced it) — its success must not mark the new A delivered, and a
    subsequent failure of the CURRENT A must leave retry state intact."""
    result = _run_node_probe(
        """
global.document = {hidden: true, hasFocus: () => false};
global.S = {session: {session_id: 'other'}};
// Deterministic manual delivery: capture onDelivered/onFailed per attempt.
const attempts = [];
global._showPwaNotification = (title, body, options) => new Promise((resolve, reject) => {
  attempts.push({resolve, reject, options});
});
const okA1 = _deliverAttentionNotification('sid','approval',1,'A','first A');
// B replaces A: clears A's key and claims B.
_clearAttentionNotificationKey('sid','approval',1);
window._attentionNotificationPendingKeys.delete('sid');
const okB = _deliverAttentionNotification('sid','clarify',1,'B','B');
attempts[1].resolve();  // B delivers normally
// New A attempt claims.
window._attentionNotificationPendingKeys.delete('sid');
_clearAttentionNotificationKey('sid','approval',1);
const okA2 = _deliverAttentionNotification('sid','approval',1,'A','second A');
// OLD A resolves late — must be a no-op (stale generation).
attempts[0].resolve();
setTimeout(() => {
  const deliveredAfterStaleSuccess = _hasAttentionNotificationKey('sid','approval',1);
  // CURRENT A now fails — retry state must be recorded (not eaten).
  attempts[2].reject(new Error('fail'));
  setTimeout(() => {
    const retry = window._attentionNotificationRetryKeys.get('sid');
    console.log(JSON.stringify({
      okA1, okB, okA2,
      staleCallbackPoisoned: deliveredAfterStaleSuccess,
      retryRecorded: !!(retry && retry.key === 'sid:approval:1'),
      canRetry: _deliverAttentionNotification('sid','approval',1,'A','retry A'),
    }));
  }, 10);
}, 10);
"""
    )
    assert result["okA1"] is True and result["okB"] is True and result["okA2"] is True
    assert result["staleCallbackPoisoned"] is False
    assert result["retryRecorded"] is True
    assert result["canRetry"] is True


def test_should_deliver_predicate_suppresses_late_display():
    """Attention cleared between scheduling and display must not surface:
    the token-backed shouldDeliver runs immediately before showNotification."""
    result = _run_node_probe(
        """
global.document = {hidden: true, hasFocus: () => false};
global.S = {session: {session_id: 'other'}};
let releaseRegistration;
_setNavigator({serviceWorker: {getRegistration: () => new Promise(res => {releaseRegistration = res;})}});
const ok = _deliverAttentionNotification('sid','approval',1,'A','body');
// The claim is withdrawn (attention resolved) BEFORE the SW registration
// resolves — the pending display must observe the dead token and abort.
window._attentionNotificationPendingKeys.delete('sid');
releaseRegistration({active: true, showNotification: (t, o) => {sw_shown.push({t}); return Promise.resolve();}});
setTimeout(() => console.log(JSON.stringify({
  ok, lateShown: sw_shown.length + direct_shown.length,
  delivered: _hasAttentionNotificationKey('sid','approval',1),
})), 20);
"""
    )
    assert result["lateShown"] == 0
    assert result["delivered"] is False


def _run_hidden_poll_tick_probe(
    *, hidden: bool, ticks: int = 3, sse_first: bool = False,
    poll_count: int = 1, sse_count: int | None = None, sse_settle_ms: int = 0,
) -> dict:
    """Drive the REAL production composition for the selected session.

    The list poll → payload → ``_syncSessionAttentionSoundState`` → delivery
    chain is what actually runs when the tab is hidden: the active session's
    SSE is torn down, so its own approval/clarify handlers cannot notify and
    this is the only remaining signal. Calling ``_deliverAttentionNotification``
    directly (as the earlier follow-up test did) bypasses the synchronizer and
    therefore cannot catch a synchronizer that drops the selected SID.

    Uses the real ``_showPwaNotification``/``_notificationOptions`` so the late
    ``onlyIfInactive`` visibility boundary is exercised rather than stubbed.
    """
    if NODE is None:  # pragma: no cover - node is installed in CI
        pytest.skip("node not on PATH")
    functions = "\n".join(
        (
            _function_source(MESSAGES_JS, "_attentionSoundKey"),
            _function_source(MESSAGES_JS, "_hasAttentionNotificationKey"),
            _function_source(MESSAGES_JS, "_markAttentionNotificationKey"),
            _function_source(MESSAGES_JS, "_clearAttentionNotificationKey"),
            _function_source(MESSAGES_JS, "_deliverAttentionNotification"),
            _function_source(MESSAGES_JS, "sendBrowserNotification"),
            _function_source(MESSAGES_JS, "_notificationOptions"),
            _function_source(MESSAGES_JS, "_showPwaNotification"),
            _function_source(SESSIONS_JS, "_sessionAttentionSoundSignature"),
            _function_source(SESSIONS_JS, "_syncSessionAttentionSoundState"),
        )
    )
    # The SSE path now derives its count from the payload, exactly like the
    # production handler; the poll independently derives its own from the
    # server-reported attention count. A test that feeds both the same literal
    # can never observe them disagreeing.
    # `sse_count` is what the SSE handler puts in the dedup key; `poll_count`
    # is what the sidebar poll derives from the server's attention count. They
    # are separate knobs on purpose: the whole defect was the two paths
    # disagreeing for the same underlying state.
    effective_sse_count = poll_count if sse_count is None else sse_count
    sse_line = (
        "_deliverAttentionNotification('target','approval',"
        f"{effective_sse_count},'Approval required','Build');"
        if sse_first
        else ""
    )
    script = f"""
global.window = global;
global.document = {{hidden: {json.dumps(hidden)}, hasFocus: () => {json.dumps(not hidden)}}};
global.location = {{origin: 'https://example.test', href: 'https://example.test/'}};
// The SELECTED session is the one that needs attention.
global.S = {{session: {{session_id: 'target'}}}};
global._notificationsEnabled = true;
global._isBackgroundedForBrowserNotification = () => !!document.hidden;
global._sessionUrlForSid = sid => `/?session=${{sid}}`;
global.assistantDisplayName = () => 'Hermes';
global.requestNotificationPermission = () => Promise.resolve('granted');
global.playAttentionSound = () => {{}};
const sink = [];
const _setNavigator = nav => Object.defineProperty(globalThis, 'navigator', {{value: nav, configurable: true}});
_setNavigator({{serviceWorker: {{getRegistration: () => Promise.resolve({{
  active: true,
  showNotification: (title, opts) => {{sink.push({{title, tag: opts && opts.tag}}); return Promise.resolve();}},
}})}}}});
// A direct Notification would also be a delivery; count it in the same sink so
// "one sink call" means one alert regardless of which transport served it.
function Notification(title, opts) {{ sink.push({{title, tag: opts && opts.tag}}); }}
Notification.permission = 'granted';
global.Notification = Notification;
let _sessionAttentionSoundPrimed = true;
const _sessionAttentionSoundState = new Map();
{functions}
{sse_line}
// The real list payload the sidebar applies, unchanged across ticks — the
// signature is stable, so only the first tick may deliver.
const payload = [{{session_id: 'target', title: 'Build', attention: {{kind: 'approval', count: {poll_count}}}}}];
let tick = 0;
// Let the SSE delivery SETTLE before the first poll tick when asked. Without
// the delay the poll's claim supersedes the still-in-flight SSE claim (the
// generation fence cancels it), which hides a key disagreement that a real
// user — whose first notification has long since been displayed — would see
// as a second alert.
setTimeout(function start() {{
(function nextTick() {{
  if (tick++ >= {ticks}) {{
    setTimeout(() => console.log(JSON.stringify({{
      sinkCalls: sink.length,
      sink,
      delivered: _hasAttentionNotificationKey('target', 'approval', 1),
    }})), 20);
    return;
  }}
  _syncSessionAttentionSoundState(payload);
  setTimeout(nextTick, 10);
}})();
}}, {sse_settle_ms});
"""
    completed = subprocess.run(
        [NODE, "-e", script], cwd=REPO, check=True, text=True, capture_output=True
    )
    return json.loads(completed.stdout)


def test_selected_session_hidden_delivers_exactly_once_through_the_poll_path():
    """Three hidden poll ticks on the SELECTED session → exactly one alert.

    This is the composition the gate reproduced: hidden tab tears down the
    active session's SSE, the list poll observes its attention, and the
    synchronizer used to drop that SID because it equalled the active one.
    """
    result = _run_hidden_poll_tick_probe(hidden=True)

    assert result["sinkCalls"] == 1, result
    assert result["delivered"] is True
    assert result["sink"][0]["tag"] == "hermes-target"


def test_selected_session_visible_delivers_nothing_through_the_poll_path():
    """Selected + visible stays suppressed.

    Note this is a GUARD, not a regression pin: `sendBrowserNotification`
    already short-circuits at `_isBackgroundedForBrowserNotification()` before
    `onlyIfInactive` is ever consulted, so it passes with or without the
    active-SID exclusion. Kept because the suppression still has to hold.
    """
    result = _run_hidden_poll_tick_probe(hidden=False)

    assert result["sinkCalls"] == 0, result
    assert result["delivered"] is False


def test_sse_then_poll_race_on_the_selected_session_still_alerts_once():
    """The SSE path claiming first must not produce a second alert from the poll."""
    result = _run_hidden_poll_tick_probe(hidden=True, sse_first=True)

    assert result["sinkCalls"] == 1, result
    assert result["delivered"] is True


def test_agreeing_counts_dedup_to_one_alert():
    """Two stacked approvals, both paths seeing count=2 → exactly one alert.

    Uses the same settle delay as the disagreement test, so the two differ only
    in whether the counts match.
    """
    result = _run_hidden_poll_tick_probe(
        hidden=True, sse_first=True, poll_count=2, sse_settle_ms=30
    )

    assert result["sinkCalls"] == 1, result


def test_disagreeing_counts_produce_the_double_alert():
    """Documents WHY the counts have to agree — this is the failure mode.

    The dedup authority is the `sid:kind:count` key. When the SSE path says 1
    and the poll says 2 for the same underlying "needs approval" state, the
    keys differ, the generation-backed pending/delivered claim never matches,
    and the user gets two notifications. This test asserts the broken shape on
    purpose; `test_the_sse_handlers_use_the_real_pending_count` is what pins
    production out of it.
    """
    result = _run_hidden_poll_tick_probe(
        hidden=True, sse_first=True, poll_count=2, sse_count=1, sse_settle_ms=30
    )

    assert result["sinkCalls"] == 2, result


def test_the_sse_handlers_use_the_real_pending_count():
    """Pin the source of the key disagreement, not just its symptom."""
    idx = MESSAGES_JS.index("source.addEventListener('approval'")
    approval = MESSAGES_JS[idx:MESSAGES_JS.index("source.addEventListener('clarify'", idx)]
    assert "_deliverAttentionNotification(activeSid,'approval',_approvalCount," in approval
    assert "_deliverAttentionNotification(activeSid,'approval',1," not in approval

    clarify_idx = MESSAGES_JS.index("source.addEventListener('clarify'")
    clarify = MESSAGES_JS[clarify_idx:MESSAGES_JS.index("source.addEventListener('state_saved'", clarify_idx)]
    assert "_deliverAttentionNotification(activeSid,'clarify',_clarifyCount," in clarify
    assert "_deliverAttentionNotification(activeSid,'clarify',1," not in clarify
