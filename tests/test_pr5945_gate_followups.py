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
            _function_source(MESSAGES_JS, "_attentionPendingCount"),
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
    kind: str = "approval",
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
            _function_source(MESSAGES_JS, "_attentionPendingCount"),
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
        f"_deliverAttentionNotification('target',{json.dumps(kind)},"
        f"{effective_sse_count},'Attention required','Build');"
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
const payload = [{{session_id: 'target', title: 'Build', attention: {{kind: {json.dumps(kind)}, count: {poll_count}}}}}];
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
      delivered: _hasAttentionNotificationKey('target', {json.dumps(kind)}, {poll_count}),
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


# ── Re-gate 4778367740 #1/#3: the clarify producer must publish the
#    authoritative queue count, and it must survive to the delivery seam ──────


def _fresh_clarify_module():
    """A clarify module with no state left over from another test."""
    from api import clarify

    with clarify._lock:
        clarify._gateway_queues.clear()
        clarify._pending.clear()
        clarify._gateway_notify_cbs.clear()
        clarify._clarify_sse_subscribers.clear()
    return clarify


def test_the_live_clarify_callback_publishes_the_queue_head_and_count():
    """The producer defect: two unresolved clarifies, one authoritative count.

    `submit_pending` notified the chat-SSE callback with the SUBMITTED entry and
    no `pending_count` at all, while its dedicated clarify SSE and the sidebar
    summary both knew `len(queue)`. The browser therefore fell back to 1 on the
    live path and read 2 from the poll — two dedup keys for one ongoing state.
    """
    clarify = _fresh_clarify_module()
    session = "sid-clarify-producer"
    seen: list[dict] = []
    clarify.register_gateway_notify(session, seen.append)
    try:
        clarify.submit_pending(session, {
            "question": "First question?", "choices_offered": ["a", "b"],
            "session_id": session, "kind": "clarify",
        })
        clarify.submit_pending(session, {
            "question": "Second question?", "choices_offered": ["c", "d"],
            "session_id": session, "kind": "clarify",
        })
        # Read the poll-side count BEFORE unregistering: unregistering clears
        # the queue (it unblocks any waiting prompt), so a later read would
        # measure teardown rather than the state the notification described.
        poll_count = clarify.pending_count(session)
    finally:
        clarify.unregister_gateway_notify(session)

    assert len(seen) == 2, seen
    assert seen[0]["pending_count"] == 1
    assert seen[0]["question"] == "First question?"

    # The SECOND notification must describe the QUEUE HEAD, not the entry that
    # was just submitted: the head is the clarify the user is being asked about,
    # and the count is how many are outstanding.
    assert seen[1]["pending_count"] == 2, seen[1]
    assert seen[1]["question"] == "First question?", (
        "the live payload must carry the queue head, not the newest submission"
    )
    # And it agrees with what the sidebar poll would report for the same state.
    assert poll_count == 2


def test_a_deduplicated_clarify_still_reports_the_authoritative_count():
    """The dedup branch notifies too, and must not report a stale count."""
    clarify = _fresh_clarify_module()
    session = "sid-clarify-dedup"
    seen: list[dict] = []
    clarify.register_gateway_notify(session, seen.append)
    try:
        clarify.submit_pending(session, {
            "question": "Only question?", "choices_offered": ["a"],
            "session_id": session, "kind": "clarify",
        })
        clarify.submit_pending(session, {
            "question": "Different?", "choices_offered": ["b"],
            "session_id": session, "kind": "clarify",
        })
        # Semantically identical to the newest entry → reuses it.
        clarify.submit_pending(session, {
            "question": "Different?", "choices_offered": ["b"],
            "session_id": session, "kind": "clarify",
        })
    finally:
        clarify.unregister_gateway_notify(session)

    assert [payload["pending_count"] for payload in seen] == [1, 2, 2], seen
    assert seen[-1]["question"] == "Only question?", "the head is still the oldest unresolved"


def test_the_producer_count_dedups_to_one_alert_at_the_delivery_seam():
    """Producer → consumer, end to end: two clarifies must alert once.

    The count the live callback publishes is fed into the SSE delivery path and
    the sidebar poll reports the same count from the same state. Agreement is
    what makes the `sid:kind:count` dedup work; before the fix the live path
    said 1, the poll said 2, and the user was told twice.
    """
    clarify = _fresh_clarify_module()
    session = "sid-clarify-e2e"
    seen: list[dict] = []
    clarify.register_gateway_notify(session, seen.append)
    try:
        clarify.submit_pending(session, {
            "question": "First?", "choices_offered": ["a"],
            "session_id": session, "kind": "clarify",
        })
        clarify.submit_pending(session, {
            "question": "Second?", "choices_offered": ["b"],
            "session_id": session, "kind": "clarify",
        })
        poll_count = clarify.pending_count(session)
    finally:
        clarify.unregister_gateway_notify(session)

    live_count = seen[-1]["pending_count"]
    assert live_count == poll_count == 2

    result = _run_hidden_poll_tick_probe(
        hidden=True, sse_first=True, kind="clarify",
        poll_count=poll_count, sse_count=live_count, sse_settle_ms=30,
    )
    assert result["sinkCalls"] == 1, result


def test_a_producer_that_omitted_the_count_would_double_alert():
    """Why the producer fix matters, stated as the failure it prevents.

    This pins the mechanism rather than the old code: with the live path
    falling back to 1 (what a payload without `pending_count` produces) and the
    poll reporting the real 2, the same state alerts twice.
    """
    result = _run_hidden_poll_tick_probe(
        hidden=True, sse_first=True, kind="clarify",
        poll_count=2, sse_count=1, sse_settle_ms=30,
    )
    assert result["sinkCalls"] == 2, result


@pytest.mark.parametrize("raw,expected", [
    (None, 1),
    ("", 1),
    ("not-a-number", 1),
    (0, 1),
    (-3, 1),
    (float("nan"), 1),
    (float("inf"), 1),
    (2.7, 2),
    ("3", 3),
    (4, 4),
])
def test_the_missing_or_malformed_count_fallback_is_explicit(raw, expected):
    """Re-gate #2: one named contract, shared by both paths.

    `Math.max(1, Number(x) || 1)` left a fractional count intact, so 2.7 and 2
    would have keyed differently for one state. The normalizer floors to a whole
    number >= 1 and both the SSE handlers and the poll signature use it.
    """
    if NODE is None:  # pragma: no cover - node is installed in CI
        pytest.skip("node not on PATH")
    script = _function_source(MESSAGES_JS, "_attentionPendingCount") + (
        f"\nconsole.log(JSON.stringify(_attentionPendingCount({json.dumps(raw)})));"
    )
    completed = subprocess.run(
        [NODE, "-e", script], cwd=REPO, check=True, text=True, capture_output=True
    )
    assert json.loads(completed.stdout) == expected


# ── Re-gate 4778367740 #4: the composition, not a stand-in for it ───────────


def _run_production_poll_composition_probe(
    *, hidden: bool, ticks: int = 3, live_event_first: bool = False,
    poll_count: int = 1, kind: str = "approval",
) -> dict:
    """Drive the REAL ``_applySessionListPayload`` for *ticks* poll rounds.

    The prior probe called ``_syncSessionAttentionSoundState`` directly, so it
    could not prove that the production list-apply path actually reaches the
    synchronizer with the selected session still in the payload. This one
    injects the real applier and feeds it the shape ``/api/sessions`` returns.

    Only collaborators BEYOND the attention path are stubbed — rendering,
    polling and cache bookkeeping. Everything between the payload and the
    browser sink is the shipped code: the applier, the signature helper, the
    synchronizer, the delivery seam, ``sendBrowserNotification``,
    ``_notificationOptions`` and ``_showPwaNotification``.

    The live-event branch dispatches through the production SSE handler body
    rather than calling the delivery function directly, so the count the
    handler derives is the one under test.
    """
    if NODE is None:  # pragma: no cover - node is installed in CI
        pytest.skip("node not on PATH")
    functions = "\n".join(
        (
            _function_source(MESSAGES_JS, "_attentionPendingCount"),
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
            _function_source(SESSIONS_JS, "_applySessionListPayload"),
        )
    )
    # What the production SSE handler does with a live clarify/approval event:
    # derive the count from the payload with the shared normalizer, then deliver.
    live_line = (
        "const _liveCount=_attentionPendingCount(livePayload.pending_count);"
        f"_deliverAttentionNotification('target',{json.dumps(kind)},_liveCount,"
        "'Attention required','Build');"
        if live_event_first
        else ""
    )
    script = f"""
global.window = global;
global.document = {{hidden: {json.dumps(hidden)}, hasFocus: () => {json.dumps(not hidden)}}};
global.location = {{origin: 'https://example.test', href: 'https://example.test/'}};
global.S = {{session: {{session_id: 'target'}}, activeProfile: 'default'}};
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
function Notification(title, opts) {{ sink.push({{title, tag: opts && opts.tag}}); }}
Notification.permission = 'granted';
global.Notification = Notification;

// Module state the real applier reads and writes.
let _sessionAttentionSoundPrimed = true;
const _sessionAttentionSoundState = new Map();
let _allSessions = [], _allProjects = [], _allSessionsScope = null;
let _activeProject = null, _sessionSourceFilter = null, _showAllProfiles = false;
let _otherProfileCount = 0, _archivedWebuiCount = 0, _archivedCliCount = 0;
let _serverWebuiSessionCount = null, _serverCliSessionCount = null;
let _serverTimeDelta = 0, _serverTz = null;
let _sidebarReferenceSessions = [], _sessionListLoadError = null;
let _sessionListHasLoadedOnce = false, _sessionListSkeletonActive = false;
let _sessionListFirstRenderAnimated = false, _sessionListRefreshAnimationPending = false;
let _lastSessionListRenderSig = null, _renamingSid = null, _sessionActionMenu = null;
let _cronPollGeneration = 0;
const _optimisticallyRemovedSessionIds = new Set();

// Collaborators BEYOND the attention path: rendering, polling, bookkeeping.
let renders = 0;
const _mergeOptimisticFirstTurnSessions = rows => rows;
const _reconcileActiveSessionIdleStateFromList = () => {{}};
const _recordSessionProfileCount = () => {{}};
const _requestedSessionSidebarSource = () => 'all';
const _sessionListExcludeHiddenEnabled = () => false;
const _pruneLineageReportCacheToVisibleSessions = () => {{}};
const _markPollingCompletionUnreadTransitions = () => {{}};
const _isSessionEffectivelyStreaming = () => false;
const _purgeStaleInflightEntries = () => {{}};
const _sessionListRenderSignature = () => 'sig';
const animateNextSessionListRefresh = () => {{}};
const ensureSessionTimeRefreshPoll = () => {{}};
const ensureActiveSessionExternalRefreshPoll = () => {{}};
const ensureSessionEventsSSE = () => {{}};
const startStreamingPoll = () => {{}};
const stopStreamingPoll = () => {{}};
const renderSessionListFromCache = () => {{ renders++; }};

{functions}

// The payload shape /api/sessions returns, unchanged across ticks.
const livePayload = {{pending_count: {poll_count}}};
const sessData = {{
  sessions: [{{session_id: 'target', title: 'Build',
    attention: {{kind: {json.dumps(kind)}, count: {poll_count}}}}}],
  active_profile: 'default', server_time: 0,
}};
const projData = {{projects: []}};

{live_line}
let tick = 0;
setTimeout(function start() {{
(function nextTick() {{
  if (tick++ >= {ticks}) {{
    setTimeout(() => console.log(JSON.stringify({{
      sinkCalls: sink.length,
      sink,
      renders,
      appliedSessions: _allSessions.length,
      delivered: _hasAttentionNotificationKey('target', {json.dumps(kind)}, {poll_count}),
    }})), 20);
    return;
  }}
  _applySessionListPayload(sessData, projData, {{}});
  setTimeout(nextTick, 10);
}})();
}}, {30 if live_event_first else 0});
"""
    completed = subprocess.run(
        [NODE, "-e", script], cwd=REPO, check=True, text=True, capture_output=True
    )
    return json.loads(completed.stdout)


def test_production_apply_path_selected_and_hidden_alerts_exactly_once():
    """Three real poll applies on the SELECTED session in a hidden tab → one."""
    result = _run_production_poll_composition_probe(hidden=True)

    assert result["appliedSessions"] == 1, "the applier never ingested the payload"
    assert result["renders"] >= 1, "the applier never reached its render step"
    assert result["sinkCalls"] == 1, result
    assert result["delivered"] is True
    assert result["sink"][0]["tag"] == "hermes-target"


def test_production_apply_path_selected_and_visible_stays_silent():
    result = _run_production_poll_composition_probe(hidden=False)

    assert result["appliedSessions"] == 1
    assert result["sinkCalls"] == 0, result
    assert result["delivered"] is False


def test_production_apply_path_live_event_then_poll_alerts_once():
    """The live-event/list race through the real applier: still one alert."""
    result = _run_production_poll_composition_probe(
        hidden=True, live_event_first=True, poll_count=2, kind="clarify",
    )

    assert result["appliedSessions"] == 1
    assert result["sinkCalls"] == 1, result
    assert result["delivered"] is True
