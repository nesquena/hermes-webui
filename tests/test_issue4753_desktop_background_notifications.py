"""Behavioral coverage for desktop-backgrounded notification delivery (#4753)."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    body_start = source.index("){", start) + 1
    depth = 0
    for idx in range(body_start, len(source)):
        char = source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"{name} function body did not close")


def _notification_contract_source() -> str:
    start = MESSAGES_SRC.index("let _desktopBackgroundedForNotifications=false;")
    end = MESSAGES_SRC.index("function _isSessionCurrentPane", start)
    tracker_start = MESSAGES_SRC.index("const _STREAM_NOTIFICATION_BACKGROUND={};")
    tracker_end = MESSAGES_SRC.index("\n", tracker_start)
    send = _extract_function(MESSAGES_SRC, "sendBrowserNotification")
    return MESSAGES_SRC[start:end] + "\n" + MESSAGES_SRC[tracker_start:tracker_end] + "\n" + send


def _background_history_contract_source() -> str:
    notification_start = MESSAGES_SRC.index("let _desktopBackgroundedForNotifications=false;")
    notification_end = MESSAGES_SRC.index("function _isSessionCurrentPane", notification_start)
    tracker_start = MESSAGES_SRC.index("const LIVE_STREAMS={};")
    tracker_end = MESSAGES_SRC.index("function closeLiveStream", tracker_start)
    return (
        MESSAGES_SRC[notification_start:notification_end]
        + "\n"
        + MESSAGES_SRC[tracker_start:tracker_end]
    )


def _run_notification_case(*, document_hidden: bool, desktop_backgrounded: bool = False, options=None):
    payload = {
        "documentHidden": document_hidden,
        "desktopBackgrounded": desktop_backgrounded,
        "options": options or {},
    }
    script = (
        "const source = " + json.dumps(_notification_contract_source()) + ";\n"
        + "const params = " + json.dumps(payload) + ";\n"
        + r"""
const vm = require('vm');
const shown = [];
const direct = [];

function Notification(title, options) {
  direct.push({ title, options });
}
Notification.permission = 'granted';

const context = {
  document: { hidden: params.documentHidden },
  window: { _notificationsEnabled: true },
  Notification,
  assistantDisplayName: () => 'Hermes',
  _notificationOptions: (body, options) => ({ body, tag: options && options.sid ? options.sid : '' }),
  _showPwaNotification: (title, body, options) => {
    shown.push({ title, body, options });
    return Promise.resolve();
  },
};
context.window.Notification = Notification;

vm.createContext(context);
vm.runInContext(source, context);
context.window.__hermesSetBackgrounded(params.desktopBackgrounded);
vm.runInContext(
  "sendBrowserNotification('Response complete','Task finished'," + JSON.stringify(params.options) + ");",
  context
);

console.log(JSON.stringify({
  shown,
  direct,
  documentHidden: context.document.hidden,
  setterType: typeof context.window.__hermesSetBackgrounded,
}));
"""
    )
    temp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
    temp.write(script)
    temp.close()
    try:
        result = subprocess.run([NODE, temp.name], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"node failed: {result.stderr}")
        return json.loads(result.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(temp.name)


def test_visible_desktop_backgrounded_tab_notifies_without_page_visibility_hidden():
    result = _run_notification_case(document_hidden=False, desktop_backgrounded=True)

    assert result["setterType"] == "function"
    assert result["documentHidden"] is False
    assert len(result["shown"]) == 1
    assert result["shown"][0]["title"] == "Response complete"


def test_visible_foreground_tab_stays_silent_without_force():
    result = _run_notification_case(document_hidden=False, desktop_backgrounded=False)

    assert result["shown"] == []
    assert result["direct"] == []


def test_real_hidden_tab_still_notifies_without_desktop_background_flag():
    result = _run_notification_case(document_hidden=True, desktop_backgrounded=False)

    assert len(result["shown"]) == 1
    assert result["shown"][0]["body"] == "Task finished"


def test_force_hidden_still_notifies_visible_documents():
    result = _run_notification_case(
        document_hidden=False,
        desktop_backgrounded=False,
        options={"forceHidden": True},
    )

    assert len(result["shown"]) == 1


def test_late_done_still_notifies_after_desktop_backgrounded_tab_returns_foreground():
    script = (
        "const source = " + json.dumps(_background_history_contract_source()) + ";\n"
        + r"""
const vm = require('vm');
const context = {
  document: {
    hidden: false,
    addEventListener: () => {},
  },
  window: {},
};
vm.createContext(context);
vm.runInContext(source, context);
vm.runInContext(
  "_STREAM_WAS_HIDDEN['session-1']={streamId:'stream-1',wasHidden:false};" +
  "_STREAM_NOTIFICATION_BACKGROUND['session-1']={streamId:'stream-1',wasBackgrounded:false};",
  context
);
context.window.__hermesSetBackgrounded(true);
context.window.__hermesSetBackgrounded(false);
const first = vm.runInContext("_shouldForceCompletionNotification('session-1','stream-1')", context);
const second = vm.runInContext("_shouldForceCompletionNotification('session-1','stream-1')", context);
console.log(JSON.stringify({ first, second }));
"""
    )
    temp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
    temp.write(script)
    temp.close()
    try:
        result = subprocess.run([NODE, temp.name], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"node failed: {result.stderr}")
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(temp.name)

    assert payload["first"] is True
    assert payload["second"] is False
