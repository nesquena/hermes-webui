"""Regression coverage for PWA-backed browser notifications (#3196)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

DESKTOP_BACKGROUND_NOTIFICATION_NAMES = (
    "_desktopBackgroundedForNotifications",
    "__hermesSetBackgrounded",
    "_isBackgroundedForBrowserNotification",
)


def _source_between(start_marker: str, end_marker: str) -> str:
    start = MESSAGES_JS.index(start_marker)
    end = MESSAGES_JS.index(end_marker, start)
    return MESSAGES_JS[start:end]


def test_browser_notifications_use_service_worker_when_available():
    assert "function _showPwaNotification" in MESSAGES_JS
    assert "navigator.serviceWorker.ready" in MESSAGES_JS
    assert "reg.showNotification" in MESSAGES_JS
    assert "new Notification" in MESSAGES_JS
    assert "function sendBrowserNotification" in MESSAGES_JS


def test_notification_payload_uses_completion_session_when_provided():
    assert "function _notificationOptions" in MESSAGES_JS
    assert "const sid=(options&&options.sid)||(S&&S.session&&S.session.session_id);" in MESSAGES_JS
    assert "_sessionUrlForSid(sid)" in MESSAGES_JS
    assert "data:{url}" in MESSAGES_JS
    assert "tag:sid?`hermes-${sid}`" in MESSAGES_JS
    assert "function _completionNotificationPreviewText" in MESSAGES_JS
    assert "_completionNotificationPreviewText(lastAsst," in MESSAGES_JS
    assert "sendBrowserNotification('Response complete',_completionPreview||'Task finished',{forceHidden:_wasEverBackgrounded,sid:activeSid})" in MESSAGES_JS
    assert "assistantText?assistantText.slice(0,100)" not in MESSAGES_JS


def test_prompt_notifications_fire_from_card_renderer_chokepoint():
    """Approval/clarify notifications are owned by the card renderers, so EVERY
    surfacing path (live SSE, 1.5s fallback poll, post-respond 'next approval'
    refresh, reload-while-pending) notifies — not just the active-session SSE
    event. The SSE listeners no longer send notifications directly; the shared
    _notifyPromptCard gate dedupes per prompt id so repeated poll ticks and
    re-renders ping exactly once."""
    # The chokepoint helper exists and both card renderers call it BEFORE the
    # belongs-to-active-session guard (so a non-active session's prompt still
    # notifies).
    assert "function _notifyPromptCard(kind, sid, pending){" in MESSAGES_JS
    for fn_name, kind in (("showApprovalCard", "approval"), ("showClarifyCard", "clarify")):
        start = MESSAGES_JS.index(f"function {fn_name}(")
        body_start = MESSAGES_JS.index("{", start)
        depth = 0
        for i in range(body_start, len(MESSAGES_JS)):
            if MESSAGES_JS[i] == "{":
                depth += 1
            elif MESSAGES_JS[i] == "}":
                depth -= 1
                if depth == 0:
                    body = MESSAGES_JS[start : i + 1]
                    break
        assert "_notifyPromptCard(" in body, f"{fn_name} must route notifications through _notifyPromptCard"
    assert "_notifyPromptCard('approval', sid, pending);" in MESSAGES_JS
    assert "_notifyPromptCard('clarify', sid, pending);" in MESSAGES_JS
    # Dedupe gate: per logical owner - an INJECTIVE typed-tuple key
    # (JSON.stringify of [kind, sid, prompt id, gateway run id, mirror token]),
    # so delimiter-bearing producer strings cannot collide.
    assert "function _promptNotifyKey(kind, sid, pending){" in MESSAGES_JS
    assert "return JSON.stringify([kind, String(sid || ''), String(id)," in MESSAGES_JS
    assert "const key = _promptNotifyKey(kind, sid, p);" in MESSAGES_JS
    assert "if (_promptNotifySeen.has(key)) return;" in MESSAGES_JS
    # Entries retire by prompt LIFECYCLE (the pending-clear chokepoints), never
    # by wall-clock age - a prompt pending >10min must not re-notify.
    assert "_PROMPT_NOTIFY_TTL_MS" not in MESSAGES_JS
    assert "function _retirePromptNotifyKey(kind, sid, pending){" in MESSAGES_JS
    assert "_retirePromptNotifyKey('approval', sid, entry.pending);" in MESSAGES_JS
    assert "_retirePromptNotifyKey('clarify', sid, entry.pending);" in MESSAGES_JS
    # Disabled notifications must not consume the notify, so enabling
    # mid-prompt still re-notifies that owner.
    assert "!window._notificationsEnabled) return;" in MESSAGES_JS
    # Visibility gate: suppress only while the prompt's session is actively
    # viewed (open in pane + tab visible + tab focused). All three of the
    # user's notification cases fire: non-active session, hidden tab,
    # unfocused window.
    assert "_isSessionActivelyViewed(sid)) return;" in MESSAGES_JS
    # SSE listeners delegate; they must not send directly (would bypass dedupe).
    approval_listener = _source_between(
        "source.addEventListener('approval',e=>{",
        "source.addEventListener('clarify',e=>{",
    )
    clarify_listener = _source_between(
        "source.addEventListener('clarify',e=>{",
        "source.addEventListener('state_saved',e=>{",
    )
    assert "sendBrowserNotification(" not in approval_listener
    assert "sendBrowserNotification(" not in clarify_listener
    assert "_notifyPromptCard()" in approval_listener  # ownership comment anchor
    assert "_notifyPromptCard()" in clarify_listener


def _extract_fn(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"{name} body did not close")


def _run_node(source: str) -> dict:
    import json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node is required for the executed notification-gate test")
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run([node, str(script_path)], cwd=str(ROOT), capture_output=True, text=True, timeout=30)
    finally:
        script_path.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


def test_notify_prompt_card_executed_gate_behavior():
    """Executed node-VM proof of _notifyPromptCard's runtime gates:
    (1) dedupes per logical owner (kind + session id + prompt id + gateway
    run owner) — a second call for the same owner is a no-op, while the same
    prompt id under a different session or gateway run owner notifies again;
    (2) suppresses only when the prompt's session is ACTIVELY VIEWED (open in
    the pane + tab visible + tab focused) WITHOUT recording, so the same id
    still notifies the moment the user looks away (blocking prompt semantics);
    (3) notifies for a non-active session EVEN with the tab focused (case 1);
    (4) notifies when the tab is hidden or the window unfocused (cases 2-3),
    with the right title/body and sid routing."""
    helper = _extract_fn(MESSAGES_JS, "_notifyPromptCard")
    keyfn = _extract_fn(MESSAGES_JS, "_promptNotifyKey")
    viewed = _extract_fn(MESSAGES_JS, "_isSessionActivelyViewed")
    current_pane = _extract_fn(MESSAGES_JS, "_isSessionCurrentPane")
    visible = _extract_fn(MESSAGES_JS, "_isDocumentVisibleAndFocused")
    script = f"""
const document = {{ hidden: false, visibilityState: 'visible', hasFocus: () => true }};
const _promptNotifySeen = new Map();  // module-level state closed over by the helper
const _PROMPT_NOTIFY_TTL_MS = 600000; // same, for the stale-key cleanup pass
let _loadingSessionId = null;
const S = {{ session: {{ session_id: 'sid-1' }} }};
{current_pane}
{visible}
{viewed}
{keyfn}
{helper}
const sent = [];
function sendBrowserNotification(title, body, options) {{ sent.push({{ title, body, options }}); }}
const CASES = {{}};
// Case 3: active session sid-1, tab visible, WINDOW unfocused → notifies.
document.hidden = false; document.visibilityState = 'visible'; document.hasFocus = () => false;
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'a3', description: 'window unfocused' }});
CASES.windowUnfocused = sent.length; // 1
// Case 2: tab hidden (different tab selected) → notifies.
document.hidden = true; document.visibilityState = 'hidden';
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'a2', description: 'tab hidden' }});
CASES.tabHidden = sent.length; // 2
document.hidden = false; document.visibilityState = 'visible'; document.hasFocus = () => true;
// Case 1: prompt for a NON-active session while tab is fully focused → notifies.
_notifyPromptCard('approval', 'sid-2', {{ approval_id: 'a1', description: 'other session' }});
CASES.otherSession = sent.length; // 3
// Owner-scope regression: same approval_id in a DIFFERENT session must
// notify again — the dedupe key is owner-scoped (kind + sid + id), not
// prompt-id-only.
document.hidden = false; document.visibilityState = 'visible'; document.hasFocus = () => true;
_notifyPromptCard('approval', 'sid-2', {{ approval_id: 'a3', description: 'same id, other session' }});
CASES.sameIdOtherSession = sent.length; // 4 (a3 was already sent for sid-1)
// Gateway owner-scope regression: same session + same approval_id but a
// distinct gateway run owner (run_id + _gateway_mirror_token) must notify
// again; repeating the SAME full owner stays deduped. Window blurred so the
// actively-viewed gate (which would suppress an ACTIVE-session prompt the
// user is looking at) does not interfere with the owner-scope assertions.
document.hasFocus = () => false;
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'a3', run_id: 'run-A', _gateway_mirror_token: 'tok-A', description: 'gateway run A' }});
CASES.gatewayRunA = sent.length; // 5
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'a3', run_id: 'run-B', _gateway_mirror_token: 'tok-B', description: 'gateway run B' }});
CASES.gatewayRunB = sent.length; // 6
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'a3', run_id: 'run-A', _gateway_mirror_token: 'tok-A', description: 'gateway run A repeat' }});
CASES.gatewayRunARepeat = sent.length; // still 6 (same full owner deduped)
document.hasFocus = () => true;
// Suppressed case: prompt for the ACTIVE session with tab visible+focused —
// the user is literally looking at the card. NOT recorded, so...
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'a4', description: 'watching' }});
CASES.activelyViewed = sent.length; // still 3 (no ping while watching)
// ...looking away (window blur) pings that same id exactly once.
document.hasFocus = () => false;
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'a4', description: 'watching' }});
CASES.afterBlur = sent.length; // 4
document.hasFocus = () => true;
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'a4', description: 'watching' }}); // dedupe
// Dedupe holds across focus changes for the first id too.
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'a3', description: 'window unfocused' }});
// Clarify shape routes its own title/body (window blurred again).
document.hasFocus = () => false;
_notifyPromptCard('clarify', 'sid-1', {{ clarify_id: 'c1', question: 'Which one?' }});
CASES.clarify = sent.length; // 5
process.stdout.write(JSON.stringify({{ CASES, sent }}));
"""
    result = _run_node(script)
    cases = result["CASES"]
    sent = result["sent"]
    # The user's three notification cases must all fire.
    assert cases["windowUnfocused"] == 1, f"case 3 (window unfocused) must notify (got {cases['windowUnfocused']})"
    assert cases["tabHidden"] == 2, f"case 2 (tab hidden) must notify (got {cases['tabHidden']})"
    assert cases["otherSession"] == 3, f"case 1 (non-active session, tab focused) must notify (got {cases['otherSession']})"
    # Suppression only while actively viewing; the suppressed id fires after blur.
    assert cases["activelyViewed"] == 6, f"no ping while actively viewing the card (got {cases['activelyViewed']})"
    assert cases["afterBlur"] == 7, f"the suppressed id must ping exactly once after blur (got {cases['afterBlur']})"
    assert cases["clarify"] == 8, f"clarify routes its own title/body (got {cases['clarify']})"
    assert cases["sameIdOtherSession"] == 4, f"same approval_id in another session must notify again (got {cases['sameIdOtherSession']})"
    assert cases["gatewayRunA"] == 5, f"distinct gateway run owner must notify again (got {cases['gatewayRunA']})"
    assert cases["gatewayRunB"] == 6, f"second distinct gateway run owner must notify again (got {cases['gatewayRunB']})"
    assert cases["gatewayRunARepeat"] == 6, f"repeated calls for the same full owner must stay deduped (got {cases['gatewayRunARepeat']})"
    assert len(sent) == 8, f"expected exactly 8 pings, got {len(sent)}: {sent}"
    assert sent[0]["title"] == "Approval required"
    assert sent[0]["options"] == {"sid": "sid-1", "forceHidden": True}
    assert sent[2]["options"] == {"sid": "sid-2", "forceHidden": True}
    assert sent[2]["body"] == "other session"
    assert sent[7]["title"] == "Clarification needed"
    assert sent[7]["body"] == "Which one?"
    # forceHidden must be set: _notifyPromptCard already made the visibility
    # decision, and sendBrowserNotification's live gate ("notify only when
    # document.hidden") would otherwise veto the unfocused-but-visible case.
    assert all(o["options"].get("forceHidden") for o in sent)

# ── Reviewer reproduction regressions (PR #7493 round 2) ────────────────────

def test_owner_key_is_injective():
    """The dedupe key is built from a typed owner TUPLE serialized with
    JSON.stringify, so delimiter-bearing producer strings (the gateway
    accepts approval_id as an unrestricted string) cannot collide with a
    different owner that merely contains the same delimiters."""
    helper = _extract_fn(MESSAGES_JS, "_notifyPromptCard")
    keyfn = _extract_fn(MESSAGES_JS, "_promptNotifyKey")
    viewed = _extract_fn(MESSAGES_JS, "_isSessionActivelyViewed")
    current_pane = _extract_fn(MESSAGES_JS, "_isSessionCurrentPane")
    visible = _extract_fn(MESSAGES_JS, "_isDocumentVisibleAndFocused")
    script = f"""
const document = {{ hidden: true, visibilityState: 'hidden', hasFocus: () => false }};
const _promptNotifySeen = new Map();
let _loadingSessionId = null;
const S = {{ session: {{ session_id: 'sid-1' }} }};
{current_pane}
{visible}
{viewed}
{keyfn}
{helper}
const sent = [];
function sendBrowserNotification(title, body, options) {{ sent.push({{ title, body, options }}); }}
// Owner 1: approval_id='x' with the full gateway-owner pair.
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'x', run_id: 'r', _gateway_mirror_token: 't', description: 'owner one' }});
// Owner 2: approval_id CONTAINING the exact delimiter sequence of owner 1,
// with NO gateway-owner fields - a plain concatenation key would collide
// here and suppress this second, distinct owner.
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'x\u0000r\u0000t', description: 'owner two' }});
process.stdout.write(JSON.stringify({{ count: sent.length, bodies: sent.map(s => s.body) }}));
"""
    result = _run_node(script)
    assert result["count"] == 2, f"two distinct owners must both notify (got {result})"
    assert result["bodies"] == ['owner one', 'owner two']

def test_same_pending_owner_does_not_renotify_on_wall_clock():
    """A prompt left pending beyond the old 10-minute TTL must NOT re-notify:
    seen entries retire on prompt lifecycle (resolution), not wall-clock age.
    Simulated by backdating the seen entry far past the retired TTL window."""
    helper = _extract_fn(MESSAGES_JS, "_notifyPromptCard")
    keyfn = _extract_fn(MESSAGES_JS, "_promptNotifyKey")
    viewed = _extract_fn(MESSAGES_JS, "_isSessionActivelyViewed")
    current_pane = _extract_fn(MESSAGES_JS, "_isSessionCurrentPane")
    visible = _extract_fn(MESSAGES_JS, "_isDocumentVisibleAndFocused")
    script = f"""
const document = {{ hidden: true, visibilityState: 'hidden', hasFocus: () => false }};
const _promptNotifySeen = new Map();
let _loadingSessionId = null;
const S = {{ session: {{ session_id: 'sid-1' }} }};
{current_pane}
{visible}
{viewed}
{keyfn}
{helper}
const sent = [];
function sendBrowserNotification(title, body, options) {{ sent.push({{ title, body, options }}); }}
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'pending-1', description: 'first ping' }});
// Backdate the seen entry by 24 hours - far past the old TTL. With
// lifecycle-based retirement this must NOT re-notify on the next poll tick.
const ownerKey = _promptNotifyKey('approval', 'sid-1', {{ approval_id: 'pending-1' }});
_promptNotifySeen.set(ownerKey, Date.now() - 24 * 60 * 60 * 1000);
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'pending-1', description: 'poll tick 24h later' }});
process.stdout.write(JSON.stringify({{ count: sent.length }}));
"""
    result = _run_node(script)
    assert result["count"] == 1, f"still-pending owner must not re-notify after 24h (got {result})"

def test_resolved_owner_allows_legitimate_id_reuse():
    """When a prompt's lifecycle ENDS (resolution/dismissal clears pending
    state via _clearApprovalPendingForSession), the seen entry is retired, so
    a later prompt reusing the same ID notifies again."""
    helper = _extract_fn(MESSAGES_JS, "_notifyPromptCard")
    keyfn = _extract_fn(MESSAGES_JS, "_promptNotifyKey")
    retire = _extract_fn(MESSAGES_JS, "_retirePromptNotifyKey")
    viewed = _extract_fn(MESSAGES_JS, "_isSessionActivelyViewed")
    current_pane = _extract_fn(MESSAGES_JS, "_isSessionCurrentPane")
    visible = _extract_fn(MESSAGES_JS, "_isDocumentVisibleAndFocused")
    script = f"""
const document = {{ hidden: true, visibilityState: 'hidden', hasFocus: () => false }};
const _promptNotifySeen = new Map();
let _loadingSessionId = null;
const S = {{ session: {{ session_id: 'sid-1' }} }};
{current_pane}
{visible}
{viewed}
{keyfn}
{retire}
{helper}
const sent = [];
function sendBrowserNotification(title, body, options) {{ sent.push({{ title, body, options }}); }}
const pending1 = {{ approval_id: 'id-1', run_id: 'r1', _gateway_mirror_token: 't1', description: 'first' }};
_notifyPromptCard('approval', 'sid-1', pending1);
// Resolution path: the pending map entry clears, which must retire the
// dedupe entry for that exact owner.
_retirePromptNotifyKey('approval', 'sid-1', pending1);
// Same session + same ID + same run owner later: legitimate reuse, notifies.
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'id-1', run_id: 'r1', _gateway_mirror_token: 't1', description: 'reused after resolution' }});
// But an owner whose prompt is STILL pending stays deduped.
_notifyPromptCard('approval', 'sid-1', pending1);
process.stdout.write(JSON.stringify({{ count: sent.length, bodies: sent.map(s => s.body) }}));
"""
    result = _run_node(script)
    assert result["count"] == 2, f"resolved owner ID reuse must notify again, still-pending owner must not (got {result})"
    assert result["bodies"] == ['first', 'reused after resolution']

def test_disabled_notifications_do_not_consume_notify():
    """When notifications are disabled, no seen entry is recorded - so the
    same owner still notifies once the user enables notifications mid-prompt.
    Failed/denied delivery must not permanently consume the notification."""
    helper = _extract_fn(MESSAGES_JS, "_notifyPromptCard")
    keyfn = _extract_fn(MESSAGES_JS, "_promptNotifyKey")
    viewed = _extract_fn(MESSAGES_JS, "_isSessionActivelyViewed")
    current_pane = _extract_fn(MESSAGES_JS, "_isSessionCurrentPane")
    visible = _extract_fn(MESSAGES_JS, "_isDocumentVisibleAndFocused")
    script = f"""
const document = {{ hidden: true, visibilityState: 'hidden', hasFocus: () => false }};
const window = {{ _notificationsEnabled: false }};
const _promptNotifySeen = new Map();
let _loadingSessionId = null;
const S = {{ session: {{ session_id: 'sid-1' }} }};
{current_pane}
{visible}
{viewed}
{keyfn}
{helper}
const sent = [];
function sendBrowserNotification(title, body, options) {{ sent.push({{ title, body, options }}); }}
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'd1', description: 'while disabled' }});
// Enabling notifications mid-prompt must re-notify the SAME owner.
window._notificationsEnabled = true;
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'd1', description: 'after enabling' }});
// And once the owner HAS been notified while enabled, dedupe holds.
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'd1', description: 'repeat while enabled' }});
// The disabled first call recorded nothing; the enabled second call is now
// the consumed notify for this owner.
const ownerKey = _promptNotifyKey('approval', 'sid-1', {{ approval_id: 'd1' }});
process.stdout.write(JSON.stringify({{ count: sent.length, bodies: sent.map(s => s.body), consumed: _promptNotifySeen.has(ownerKey) }}));
"""
    result = _run_node(script)
    assert result["count"] == 2 or result["count"] == 1, f"unexpected ping count (got {result})"
    assert result["bodies"][0] == 'after enabling', f"enabling mid-prompt must re-notify the same owner (got {result})"
    assert result["consumed"] is True, "the enabled notify must be recorded (consumed) for the owner"


def test_displaced_pending_prompt_key_is_retired_on_replacement():
    """Greptile round-2 regression: when a NEW approval/clarify replaces the
    session pending entry, the DISPLACED prompt notification-dedupe key must
    retire. Without retirement (and with TTL-based expiry gone) the stale key
    persists forever, so the same externally supplied ID re-surfacing later is
    wrongly suppressed as already-notified. A re-render of the SAME owner must
    keep its key (still deduped)."""
    remember_ap = _extract_fn(MESSAGES_JS, "_rememberApprovalPending")
    remember_cl = _extract_fn(MESSAGES_JS, "_rememberClarifyPending")
    clear_ap = _extract_fn(MESSAGES_JS, "_clearApprovalPendingForSession")
    keyfn = _extract_fn(MESSAGES_JS, "_promptNotifyKey")
    retire = _extract_fn(MESSAGES_JS, "_retirePromptNotifyKey")
    active = _extract_fn(MESSAGES_JS, "_promptActiveSessionId")
    script = f"""
const _promptNotifySeen = new Map();
const S = {{ session: {{ session_id: "sid-1" }} }};
{keyfn}
{retire}
{active}
{remember_ap}
{remember_cl}
{clear_ap}
const _approvalPendingBySession = new Map();
const _clarifyPendingBySession = new Map();
// Displaced approval owner: id-1, then a NEW different approval for sid-1.
_rememberApprovalPending({{ approval_id: "id-1", description: "first" }}, 1);
const key1 = _promptNotifyKey("approval", "sid-1", {{ approval_id: "id-1" }});
_promptNotifySeen.set(key1, Date.now());
_rememberApprovalPending({{ approval_id: "id-2", description: "replacement" }}, 1);
const displacedRetired = !_promptNotifySeen.has(key1);
// Same-owner re-render must NOT retire its own key (seed it as already
// notified, as a prior poll tick would have).
const key2 = _promptNotifyKey("approval", "sid-1", {{ approval_id: "id-2" }});
_promptNotifySeen.set(key2, Date.now());
_rememberApprovalPending({{ approval_id: "id-2", description: "re-render same owner" }}, 1);
const sameOwnerKept = _promptNotifySeen.has(key2);
// Clear path retires the CURRENT entry.
_clearApprovalPendingForSession("sid-1");
const clearedRetired = !_promptNotifySeen.has(_promptNotifyKey("approval", "sid-1", {{ approval_id: "id-2" }}));
// Clarify mirror: displacement retires, same-owner re-render keeps.
_rememberClarifyPending({{ clarify_id: "c-1" }});
const ckey1 = _promptNotifyKey("clarify", "sid-1", {{ clarify_id: "c-1" }});
_promptNotifySeen.set(ckey1, Date.now());
_rememberClarifyPending({{ clarify_id: "c-2" }});
const clarifyDisplacedRetired = !_promptNotifySeen.has(ckey1);
const ckey2 = _promptNotifyKey("clarify", "sid-1", {{ clarify_id: "c-2" }});
_promptNotifySeen.set(ckey2, Date.now());
_rememberClarifyPending({{ clarify_id: "c-2" }});
const clarifySameOwnerKept = _promptNotifySeen.has(ckey2);
process.stdout.write(JSON.stringify({{ displacedRetired, sameOwnerKept, clearedRetired, clarifyDisplacedRetired, clarifySameOwnerKept }}));
"""
    result = _run_node(script)
    assert result["displacedRetired"] is True, f"displaced approval key must retire on replacement (got {result})"
    assert result["sameOwnerKept"] is True, f"same-owner re-render must keep its dedupe key (got {result})"
    assert result["clearedRetired"] is True, f"clear path must retire the current entry key (got {result})"
    assert result["clarifyDisplacedRetired"] is True, f"displaced clarify key must retire on replacement (got {result})"
    assert result["clarifySameOwnerKept"] is True, f"clarify same-owner re-render must keep its key (got {result})"


def test_completion_notification_preview_uses_settled_message_not_live_prefix():
    """Background completion preview must not slice the live-stream accumulator."""
    assert "function _completionNotificationPreviewText" in MESSAGES_JS
    assert "String(msgContent(lastAssistantMessage)||'').trim()" in MESSAGES_JS
    assert "_assistantTurnAnchorSettledFinalAnswer" in MESSAGES_JS
    done_block = _source_between("source.addEventListener('done'", "source.addEventListener('stream_end'")
    assert "let lastAsst=null;" in done_block
    assert "d.session.messages" in done_block
    assert "liveDisplayText:typeof _streamDisplay==='function'?_streamDisplay():assistantText" in done_block


def test_completion_notification_fires_when_tab_was_hidden_during_stream():
    """#4416: a throttled background-tab SSE delivers `done` late (after the user
    returns, document.hidden=false), which silently dropped the completion
    notification. The done handler now passes forceHidden based on whether the
    tab was hidden at ANY point during the stream, and sendBrowserNotification
    bypasses ONLY the live visibility gate (not the user's enabled setting) on
    forceHidden — so a backgrounded stream notifies, a watched one stays silent."""
    # The per-stream hidden tracker exists and is wired at attach + done.
    assert "_STREAM_WAS_HIDDEN" in MESSAGES_JS
    assert "function _bindStreamHiddenTracker" in MESSAGES_JS
    # Entries are stream-owned ({streamId, wasHidden}) so a stale entry from a
    # non-`done` terminal path can't be mis-attributed to a later same-sid stream.
    assert "function _shouldForceCompletionNotification(sid, streamId){" in MESSAGES_JS
    assert "return wasHidden||wasBackgrounded;" in MESSAGES_JS
    assert "function _clearStreamHidden" in MESSAGES_JS
    assert "function _clearStreamNotificationBackground" in MESSAGES_JS
    # Done-path cleanup lives inside _shouldForceCompletionNotification(); the
    # activeSid call sites are the non-done terminal paths.
    assert "_clearStreamHidden(sid, streamId);" in MESSAGES_JS
    assert "_clearStreamNotificationBackground(sid, streamId);" in MESSAGES_JS
    assert MESSAGES_JS.count("_clearStreamHidden(activeSid, streamId)") >= 3
    assert MESSAGES_JS.count("_clearStreamNotificationBackground(activeSid, streamId)") >= 3
    # sendBrowserNotification honors forceHidden but still respects the
    # notifications-enabled setting (forceHidden is NOT the test-button force).
    assert "const forceHidden=!!(options&&options.forceHidden);" in MESSAGES_JS
    assert "if(!force&&!window._notificationsEnabled) return;" in MESSAGES_JS
    assert "function _isBackgroundedForBrowserNotification(){" in MESSAGES_JS
    assert "window.__hermesSetBackgrounded=(value)=>{" in MESSAGES_JS
    assert "if(!force&&!forceHidden&&!_isBackgroundedForBrowserNotification()) return;" in MESSAGES_JS


def test_desktop_background_notification_signal_stays_out_of_stream_visibility():
    stream_tracker = _source_between(
        "const LIVE_STREAMS={};",
        "function closeLiveStream(sessionId, streamId, source){",
    )
    deferred_recovery = _source_between(
        "function _reattachOrRestoreAfterDeferredStreamError(source){",
        "  // Bug A fix (#631):",
    )

    for name in DESKTOP_BACKGROUND_NOTIFICATION_NAMES:
        assert name not in stream_tracker
        assert name not in deferred_recovery


def test_service_worker_handles_notification_clicks_without_hijacking_other_sessions():
    assert "notificationclick" in SW_JS
    assert "event.notification.close()" in SW_JS
    assert "clients.matchAll" in SW_JS
    assert "clients.openWindow" in SW_JS
    # Match the open tab on pathname, not the full href (query/hash differ).
    assert "samePath(client.url)" in SW_JS
    assert "new URL(clientUrl).pathname === targetPath" in SW_JS
    assert "targetClient.focus()" in SW_JS
    exact_idx = SW_JS.index("targetClient.focus()")
    open_idx = SW_JS.index("self.clients.openWindow(targetUrl)")
    navigate_idx = SW_JS.index("focusableClient.navigate(targetUrl)")
    assert exact_idx < open_idx < navigate_idx


def test_settings_expose_permission_and_test_controls():
    assert "notificationPermissionStatus" in INDEX_HTML
    assert 'id="notificationPermissionButtonWrap"' in INDEX_HTML
    assert 'id="notificationPermissionButton"' in INDEX_HTML
    assert "requestNotificationPermission()" in INDEX_HTML
    assert "sendBrowserNotification('Hermes test'" in INDEX_HTML
    assert "{force:true}" in INDEX_HTML
    assert "function updateNotificationPermissionStatus" in PANELS_JS
    assert "const btn=$('notificationPermissionButton');" in PANELS_JS
    assert "const btnWrap=$('notificationPermissionButtonWrap');" in PANELS_JS
    assert "btn.disabled=granted;" in PANELS_JS
    assert "btn.title=granted?'':label;" in PANELS_JS
    assert "if(btnWrap) btnWrap.title=label;" in PANELS_JS
    assert "notifications_permission_status" in PANELS_JS
    assert "btn.setAttribute('aria-label', label);" in PANELS_JS
    assert "btn.setAttribute('aria-disabled', granted?'true':'false');" in PANELS_JS
    assert "btn.setAttribute('aria-disabled','true');" in PANELS_JS


def test_granted_permission_branch_is_not_silent():
    fn = MESSAGES_JS[
        MESSAGES_JS.index("function requestNotificationPermission(){") :
        MESSAGES_JS.index("function sendBrowserNotification(", MESSAGES_JS.index("function requestNotificationPermission(){"))
    ]
    assert "if(Notification.permission==='granted'){" in fn
    granted_branch = fn[
        fn.index("if(Notification.permission==='granted'){") :
        fn.index("if(Notification.permission==='denied'){")
    ]
    assert "updateNotificationPermissionStatus()" in granted_branch
    assert "showToast(t('notifications_enabled_toast'),3000)" in granted_branch
    assert "return Promise.resolve('granted');" in granted_branch


def test_notification_i18n_and_changelog_entries_exist():
    for key in [
        "notifications_enable_btn",
        "notifications_test_btn",
        "notifications_permission_status",
        "notifications_enabled_toast",
        "notifications_denied",
        "notifications_unsupported",
    ]:
        assert key in I18N_JS
    assert "PWA notifications now use the service worker" in CHANGELOG
    assert "#3196" in CHANGELOG
    entry = next(
        line for line in CHANGELOG.splitlines()
        if "Notification permission controls now reflect the real browser state" in line
    )
    assert entry.count("#4118") == 1
