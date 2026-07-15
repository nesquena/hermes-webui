"""Regression tests for attention alerts on session attention state.

Approval/clarify prompts can surface through the sidebar session metadata rather
than the active live SSE stream. The sidebar badge path must play the distinct
attention sound when a session newly needs user input, without blasting sounds
for already-existing badges on initial load.
"""
from pathlib import Path
import json
import shutil
import subprocess

import pytest

REPO = Path(__file__).parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _body_from_brace(src: str, brace: int, label: str) -> str:
    assert brace >= 0, f"body opening brace not found for: {label}"
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"body did not close for: {label}"
    return src[brace + 1 : i - 1]


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"function not found: {name}"
    signature_end = src.find("){", start)
    assert signature_end >= 0, f"function body not found: {name}"
    return _body_from_brace(src, signature_end + 1, name)


def _function_source(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"function not found: {name}"
    signature_end = src.find("){", start)
    assert signature_end >= 0, f"function body not found: {name}"
    body = _body_from_brace(src, signature_end + 1, name)
    return src[start : signature_end + 2] + body + "}"


def _run_attention_notification_probe(steps: str) -> list[dict]:
    if NODE is None:  # pragma: no cover - node is installed in CI
        pytest.skip("node not on PATH")
    functions = "\n".join(
        (
            _function_source(MESSAGES_JS, "_attentionSoundKey"),
            _function_source(MESSAGES_JS, "_hasAttentionNotificationKey"),
            _function_source(MESSAGES_JS, "_markAttentionNotificationKey"),
            _function_source(MESSAGES_JS, "_clearAttentionNotificationKey"),
            _function_source(MESSAGES_JS, "sendBrowserNotification"),
            _function_source(SESSIONS_JS, "_sessionAttentionSoundSignature"),
            _function_source(SESSIONS_JS, "_syncSessionAttentionSoundState"),
        )
    )
    script = f"""
global.window = global;
global.document = {{hidden: false, hasFocus: () => true}};
global.S = {{session: {{session_id: 'other'}}}};
global.playAttentionSound = () => {{}};
global.Notification = {{permission: 'granted'}};
global._notificationsEnabled = true;
global._isBackgroundedForBrowserNotification = () => document.hidden;
const notifications = [];
global._showPwaNotification = (title, body, options) => {{notifications.push({{title, body, options}}); return Promise.resolve();}};
let _sessionAttentionSoundPrimed = false;
const _sessionAttentionSoundState = new Map();
{functions}
{steps}
console.log(JSON.stringify(notifications));
"""
    completed = subprocess.run(
        [NODE, "-e", script],
        cwd=REPO,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(completed.stdout)


def _run_notification_delivery_probe(*, service_worker_succeeds: bool, direct_succeeds: bool, permission: str = "granted") -> dict:
    """Execute the real notification delivery path and report durable delivery effects."""
    if NODE is None:  # pragma: no cover - node is installed in CI
        pytest.skip("node not on PATH")
    functions = "\n".join(
        (
            _function_source(MESSAGES_JS, "_notificationOptions"),
            _function_source(MESSAGES_JS, "_showPwaNotification"),
            _function_source(MESSAGES_JS, "sendBrowserNotification"),
        )
    )
    script = f"""
global.window = global;
global.document = {{hidden: true}};
global.location = {{origin: 'https://example.test', href: 'https://example.test/', pathname: '/'}};
global.S = {{session: {{session_id: 'target'}}}};
global._notificationsEnabled = true;
global._isBackgroundedForBrowserNotification = () => true;
global._sessionUrlForSid = sid => `/?session=${{sid}}`;
global.assistantDisplayName = () => 'Hermes';
global.requestNotificationPermission = () => Promise.resolve('granted');
let delivered = 0;
let direct = 0;
function Notification() {{
  direct += 1;
  if(!{str(direct_succeeds).lower()}) throw new Error('direct failed');
}}
Notification.permission = {json.dumps(permission)};
global.Notification = Notification;
Object.defineProperty(global, 'navigator', {{value: {{serviceWorker: {{
  getRegistration: () => Promise.resolve({{
    active: true,
    showNotification: () => {"Promise.resolve()" if service_worker_succeeds else "Promise.reject(new Error('sw failed'))"}
  }})
}}}}, configurable: true}});
{functions}
sendBrowserNotification('Approval required','Tool approval needed',{{
  sid:'target', onDelivered:()=>{{delivered += 1;}}
}});
setTimeout(()=>console.log(JSON.stringify({{delivered,direct}})),25);
"""
    completed = subprocess.run(
        [NODE, "-e", script],
        cwd=REPO,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(completed.stdout)


def test_sidebar_attention_state_plays_distinct_sound_on_new_attention_only():
    sync_body = _function_body(SESSIONS_JS, "_syncSessionAttentionSoundState")
    apply_body = _function_body(SESSIONS_JS, "_applySessionListPayload")

    assert "let _sessionAttentionSoundPrimed = false;" in SESSIONS_JS
    assert "const _sessionAttentionSoundState = new Map();" in SESSIONS_JS
    assert "_syncSessionAttentionSoundState(_allSessions);" in apply_body
    assert "if(!_sessionAttentionSoundPrimed)" in sync_body
    assert "_sessionAttentionSoundPrimed=true;" in sync_body
    assert "playKey=typeof _attentionSoundKey==='function'?_attentionSoundKey(s.session_id,kind,count):`${s.session_id}:${sig}`;" in sync_body
    assert "if(playKey&&typeof playAttentionSound==='function') playAttentionSound(playKey);" in sync_body
    assert "playNotificationSound" not in sync_body


def test_attention_signature_tracks_kind_and_count_for_badge_changes():
    signature_body = _function_body(SESSIONS_JS, "_sessionAttentionSoundSignature")

    assert "attention.kind" in signature_body
    assert "Number.isFinite(count)" in signature_body
    assert "count<=0" in signature_body
    assert "approval" in signature_body
    assert "clarify" in signature_body
    assert "return `${kind}:${Math.max(1,count||1)}`;" in signature_body


def test_clearing_attention_does_not_notify_and_rearms_same_request():
    notifications = _run_attention_notification_probe(
        """
document.hidden = true;
_syncSessionAttentionSoundState([]);
_syncSessionAttentionSoundState([{session_id:'target',title:'Build',attention:{kind:'approval',count:1}}]);
_syncSessionAttentionSoundState([{session_id:'target',title:'Build',attention:null}]);
_syncSessionAttentionSoundState([{session_id:'target',title:'Build',attention:{kind:'approval',count:1}}]);
"""
    )

    assert [item["title"] for item in notifications] == [
        "Waiting for permission decision",
        "Waiting for permission decision",
    ]


def test_sidebar_deduplicates_attention_already_notified_by_active_sse():
    notifications = _run_attention_notification_probe(
        """
_syncSessionAttentionSoundState([]);
_markAttentionNotificationKey('target','clarify',1);
S.session.session_id = 'other';
_syncSessionAttentionSoundState([{session_id:'target',title:'Build',attention:{kind:'clarify',count:1}}]);
"""
    )

    assert notifications == []


@pytest.mark.parametrize("initially_enabled", [True, False])
def test_ineligible_active_sse_does_not_suppress_later_background_notification(initially_enabled):
    notifications = _run_attention_notification_probe(
        f"""
_syncSessionAttentionSoundState([]);
window._notificationsEnabled = {str(initially_enabled).lower()};
document.hidden = false;
if(!_hasAttentionNotificationKey('target','approval',1)
  && sendBrowserNotification('Approval required','Tool approval needed',{{sid:'target'}})){{
  _markAttentionNotificationKey('target','approval',1);
}}
S.session.session_id = 'other';
window._notificationsEnabled = true;
document.hidden = true;
_syncSessionAttentionSoundState([{{session_id:'target',title:'Build',attention:{{kind:'approval',count:1}}}}]);
"""
    )

    assert [item["title"] for item in notifications] == ["Waiting for permission decision"]


@pytest.mark.parametrize(
    ("service_worker_succeeds", "direct_succeeds", "permission", "expected"),
    [
        (True, True, "granted", {"delivered": 1, "direct": 0}),
        (False, True, "granted", {"delivered": 1, "direct": 1}),
        (False, False, "granted", {"delivered": 0, "direct": 1}),
        (True, True, "default", {"delivered": 1, "direct": 0}),
    ],
)
def test_attention_key_is_marked_only_after_successful_notification_delivery(
    service_worker_succeeds, direct_succeeds, permission, expected
):
    assert _run_notification_delivery_probe(
        service_worker_succeeds=service_worker_succeeds,
        direct_succeeds=direct_succeeds,
        permission=permission,
    ) == expected


def test_attention_sound_is_softer_short_reverse_of_completion_sound():
    attention_body = _function_body(MESSAGES_JS, "playAttentionSound")
    completion_body = _function_body(MESSAGES_JS, "playNotificationSound")

    assert "osc.type='sine'" in attention_body
    assert "window._lastAttentionSoundAt" in attention_body
    assert "nowMs-window._lastAttentionSoundAt<900" in attention_body
    assert "window._attentionSoundSeenKeys" in attention_body
    assert "seen.has(dedupeKey)" in attention_body
    assert "seen.set(dedupeKey,nowMs)" in attention_body
    assert "300000" in attention_body
    assert "osc.frequency.setValueAtTime(880,ctx.currentTime);" in attention_body
    assert "osc.frequency.setValueAtTime(660,ctx.currentTime+0.075);" in attention_body
    assert "gain.gain.setValueAtTime(0.24,ctx.currentTime);" in attention_body
    assert "osc.stop(ctx.currentTime+0.24);" in attention_body
    assert "osc.frequency.setValueAtTime(660,ctx.currentTime);" in completion_body
    assert "osc.frequency.setValueAtTime(880,ctx.currentTime+0.1);" in completion_body
    assert "osc.stop(ctx.currentTime+0.3);" in completion_body
