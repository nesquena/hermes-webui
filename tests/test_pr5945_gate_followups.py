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
