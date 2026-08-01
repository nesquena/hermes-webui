"""Behavioral coverage for repeated same-session notification replacement (#6673)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "issue6673_repeat_notification.json").read_text(
        encoding="utf-8",
    ),
)
NODE = shutil.which("node")


def _run_node(script: str) -> dict:
    if NODE is None:
        pytest.skip("node executable is required for notification behavior checks")
    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".js",
        encoding="utf-8",
        delete=False,
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)
    assert result.returncode == 0, (
        f"notification behavior driver failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return json.loads(result.stdout)


def _driver(
    *,
    delivery: str,
    sends: list[dict],
    permission: str = "granted",
    via_public_sender: bool = False,
) -> dict:
    notification_options = extract_function(MESSAGES_JS, "_notificationOptions")
    show_notification = extract_function(MESSAGES_JS, "_showPwaNotification")
    request_permission = extract_function(MESSAGES_JS, "requestNotificationPermission")
    send_notification = extract_function(MESSAGES_JS, "sendBrowserNotification")
    registration = """\
const delivery = {calls: [], entries: new Map(), toasts: 0};
function recordNotification(method, title, options) {
  return Promise.resolve().then(() => {
    const existing = delivery.entries.has(options.tag);
    delivery.entries.set(options.tag, {title, options});
    if (!existing || options.renotify) delivery.toasts += 1;
    delivery.calls.push({method, title, options});
  });
}
"""
    delivery_setup = (
        """
navigator = {
  serviceWorker: {
    getRegistration: () => Promise.resolve({
      active: {},
      showNotification: (title, options) =>
        recordNotification('service-worker', title, options),
    }),
  },
};
"""
        if delivery == "service-worker"
        else (
            """
navigator = {
  serviceWorker: {
    getRegistration: () => Promise.resolve({
      active: null,
      showNotification: (title, options) =>
        recordNotification('service-worker', title, options),
    }),
  },
};
"""
            if delivery == "inactive-service-worker"
            else (
                """
navigator = {
  serviceWorker: {
    getRegistration: () => Promise.reject(new Error('registration rejected')),
  },
};
"""
                if delivery == "rejected-service-worker"
                else "navigator = {};"
            )
        )
    )
    return _run_node(
        f"""
let navigator = {{}};
{notification_options}
{show_notification}
{request_permission}
{send_notification}
{registration}
globalThis.S = {{session: {{}}}};
globalThis.location = {{
  origin: 'https://webui.test',
  href: 'https://webui.test/',
}};
globalThis._sessionUrlForSid = sid => '/?session=' + encodeURIComponent(sid);
globalThis.assistantDisplayName = () => 'Hermes';
globalThis.window = {{_notificationsEnabled: true}};
globalThis._isBackgroundedForBrowserNotification = () => true;
globalThis.showToast = () => {{}};
globalThis.Notification = function(title, options) {{
  return recordNotification('direct', title, options);
}};
globalThis.Notification.permission = {json.dumps(permission)};
globalThis.Notification.requestPermission = () => Promise.resolve('granted');
globalThis.window.Notification = globalThis.Notification;
globalThis.t = key => key;
{delivery_setup}

(async () => {{
  for (const send of {json.dumps(sends)}) {{
    if ({json.dumps(via_public_sender)}) {{
      await sendBrowserNotification(send.title, send.body, {{...send.options, force: true}});
    }} else {{
      await _showPwaNotification(send.title, send.body, send.options);
    }}
  }}
  console.log(JSON.stringify({{
    calls: delivery.calls,
    toasts: delivery.toasts,
    entryCount: delivery.entries.size,
    tags: [...delivery.entries.keys()],
  }}));
}})().catch(error => {{
  console.error(error.stack || error);
  process.exitCode = 1;
}});
""",
    )


def _send(title: str, body: str, *, sid: str | None = FIXTURE["sid"]) -> dict:
    options = {} if sid is None else {"sid": sid}
    return {"title": title, "body": body, "options": options}


def test_reported_two_test_clicks_request_second_toast_with_stable_tag():
    result = _driver(
        delivery="service-worker",
        sends=[
            _send(FIXTURE["title"], FIXTURE["body"]),
            _send(FIXTURE["title"], "The task finished again."),
        ],
        via_public_sender=True,
    )

    assert result["toasts"] == 2
    assert result["entryCount"] == 1
    assert result["tags"] == [FIXTURE["expected_tag"]]


def test_service_worker_delivery_preserves_grouping():
    result = _driver(
        delivery="service-worker",
        sends=[_send("Approval required", "Approve the tool call.")],
    )

    call = result["calls"][0]
    assert call["method"] == "service-worker"
    assert call["options"]["tag"] == FIXTURE["expected_tag"]
    assert call["options"]["renotify"] is True
    assert call["options"]["data"]["url"] == FIXTURE["expected_url"]


def test_direct_fallback_preserves_payload():
    result = _driver(
        delivery="direct",
        sends=[_send("Clarification needed", "Choose a value.")],
    )

    call = result["calls"][0]
    assert call["method"] == "direct"
    assert call["title"] == "Clarification needed"
    assert call["options"]["body"] == "Choose a value."
    assert call["options"]["tag"] == FIXTURE["expected_tag"]
    assert call["options"]["renotify"] is True
    assert call["options"]["data"]["url"] == FIXTURE["expected_url"]


def test_direct_delivery_renotifies_repeated_constructor_calls():
    result = _driver(
        delivery="direct",
        sends=[
            _send(FIXTURE["title"], FIXTURE["body"]),
            _send(FIXTURE["title"], "The task finished again."),
        ],
        via_public_sender=True,
    )

    assert [call["method"] for call in result["calls"]] == ["direct", "direct"]
    assert result["toasts"] == 2
    assert result["entryCount"] == 1
    assert result["tags"] == [FIXTURE["expected_tag"]]


def test_public_sender_awaits_delivery_after_permission_grant():
    result = _driver(
        delivery="service-worker",
        sends=[_send(FIXTURE["title"], FIXTURE["body"])],
        permission="default",
        via_public_sender=True,
    )

    assert [call["method"] for call in result["calls"]] == ["service-worker"]
    assert result["tags"] == [FIXTURE["expected_tag"]]


@pytest.mark.parametrize("delivery", ["inactive-service-worker", "rejected-service-worker"])
def test_direct_fallback_when_service_worker_registration_unavailable(delivery: str):
    result = _driver(
        delivery=delivery,
        sends=[_send("Clarification needed", "Choose a value.")],
        via_public_sender=True,
    )

    call = result["calls"][0]
    assert call["method"] == "direct"
    assert call["options"]["tag"] == FIXTURE["expected_tag"]
    assert call["options"]["renotify"] is True


def test_categories_and_denial_keep_existing_behavior():
    result = _driver(
        delivery="service-worker",
        sends=[
            _send("Approval required", "Approve the tool call."),
            _send("Clarification needed", "Choose a value."),
        ],
    )

    assert [call["title"] for call in result["calls"]] == [
        "Approval required",
        "Clarification needed",
    ]
    assert all(call["options"]["tag"] == FIXTURE["expected_tag"] for call in result["calls"])

    denied = _driver(
        delivery="service-worker",
        sends=[_send(FIXTURE["title"], FIXTURE["body"])],
        permission="denied",
        via_public_sender=True,
    )
    assert denied["calls"] == []


def test_grouping_keeps_one_notification_entry():
    result = _driver(
        delivery="service-worker",
        sends=[_send(FIXTURE["title"], f"send {index}") for index in range(3)],
    )

    assert result["entryCount"] == 1
    assert result["tags"] == [FIXTURE["expected_tag"]]


def test_repeated_missing_session_id_uses_generic_grouping_tag():
    result = _driver(
        delivery="service-worker",
        sends=[
            _send("Approval required", "Approve the tool call.", sid=None),
            _send("Approval required", "Approve it again.", sid=None),
        ],
        via_public_sender=True,
    )

    assert result["toasts"] == 2
    assert result["entryCount"] == 1
    assert result["tags"] == [FIXTURE["generic_tag"]]
    assert result["calls"][-1]["options"]["data"]["url"] == FIXTURE["generic_url"]
