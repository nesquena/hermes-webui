"""Tests for #4771: failed gateway approval clicks must keep the card actionable."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_fn(src: str, name: str, prefix: str = "function ") -> str:
    start = src.index(f"{prefix}{name}(")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"{name} body not closed")


def _run_node(script: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
        tf.write(script)
        script_path = tf.name
    try:
        result = subprocess.run(
            [NODE, script_path],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return json.loads(result.stdout)
    finally:
        Path(script_path).unlink(missing_ok=True)


def _run_failure_case(api_js: str) -> dict:
    helpers = "\n".join(
        [
            _extract_fn(MESSAGES_JS, "_approvalDismissKey"),
            _extract_fn(MESSAGES_JS, "_getDismissedApprovals"),
            _extract_fn(MESSAGES_JS, "_isApprovalDismissed"),
            _extract_fn(MESSAGES_JS, "_unmarkApprovalDismissed"),
            _extract_fn(MESSAGES_JS, "_promptActiveSessionId"),
            _extract_fn(MESSAGES_JS, "_approvalPromptBelongsToActiveSession"),
            _extract_fn(MESSAGES_JS, "_rememberApprovalPending"),
            _extract_fn(MESSAGES_JS, "_clearApprovalPendingForSession"),
            _extract_fn(MESSAGES_JS, "_renderPendingApprovalForActiveSession"),
            _extract_fn(MESSAGES_JS, "_approvalResponseMatches"),
            _extract_fn(MESSAGES_JS, "_setApprovalControlsDisabled"),
            _extract_fn(MESSAGES_JS, "_setPromptFlyoutHidden"),
            _extract_fn(MESSAGES_JS, "showApprovalCard"),
            _extract_fn(MESSAGES_JS, "_restoreFailedApprovalResponse"),
            _extract_fn(MESSAGES_JS, "respondApproval", prefix="async function "),
        ]
    )
    script = f"""
const output = {{}};
const _store = {{}};
const localStorage = {{
  getItem: key => Object.prototype.hasOwnProperty.call(_store, key) ? _store[key] : null,
  setItem: (key, value) => {{ _store[key] = String(value); }},
}};
const S = {{ session: {{ session_id: 'sess-1' }} }};
const _DISMISSED_APPROVALS_KEY = 'hermes_dismissed_approvals';
let syncTopbarCalls = 0;
let showToastCalls = [];
let statusCalls = [];
let hideCalls = 0;
let renderCalls = 0;
let _approvalSessionId = 'sess-1';
let _approvalCurrentId = 'appr-1';
let _approvalPendingBySession = new Map();
let _approvalResponding = null;
let _approvalSignature = '';
let _approvalVisibleSince = 0;
let _approvalHideTimer = null;
const _clarifyPendingBySession = new Map();
const buttons = new Map();
function makeButton(id) {{
  return {{
    id,
    disabled: false,
    classList: {{
      values: new Set(),
      add(value) {{ this.values.add(value); }},
      remove(value) {{ this.values.delete(value); }},
      contains(value) {{ return this.values.has(value); }},
    }},
  }};
}}
['approvalBtnOnce','approvalBtnSession','approvalBtnAlways','approvalBtnDeny'].forEach(id => buttons.set(id, makeButton(id)));
const card = {{
  classList: {{
    values: new Set(['visible']),
    add(value) {{ this.values.add(value); }},
    remove(value) {{ this.values.delete(value); }},
    contains(value) {{ return this.values.has(value); }},
    toggle(value, force) {{
      if (force === undefined) {{
        if (this.values.has(value)) this.values.delete(value);
        else this.values.add(value);
      }} else if (force) this.values.add(value);
      else this.values.delete(value);
    }},
  }},
  hidden: false,
  attributes: {{}},
  setAttribute(name, value) {{ this.attributes[name] = String(value); }},
  removeAttribute(name) {{ delete this.attributes[name]; }},
  querySelector() {{ return null; }},
}};
const approvalDesc = {{ textContent: '' }};
const approvalCmd = {{ textContent: '' }};
const approvalCounter = {{ textContent: '', style: {{ display: '' }} }};
const msg = {{}};
const document = {{ activeElement: null }};
const elements = {{
  approvalCard: card,
  approvalDesc,
  approvalCmd,
  approvalCounter,
  approvalBtnOnce: buttons.get('approvalBtnOnce'),
  approvalBtnSession: buttons.get('approvalBtnSession'),
  approvalBtnAlways: buttons.get('approvalBtnAlways'),
  approvalBtnDeny: buttons.get('approvalBtnDeny'),
  msg,
}};
function $(id) {{ return elements[id] || null; }}
function syncTopbar() {{ syncTopbarCalls += 1; }}
function hideApprovalCard() {{ hideCalls += 1; card.classList.remove('visible'); }}
function _clearApprovalHideTimer() {{}}
function _syncApprovalCollapseButton() {{}}
function _syncApprovalTranscriptSpace() {{}}
function applyLocaleToDOM() {{}}
function setTimeout(fn) {{ return 1; }}
function showToast(msg) {{ showToastCalls.push(msg); }}
function setStatus(msg) {{ statusCalls.push(msg); }}
function t(key) {{ return key; }}
{api_js}
{helpers}
const pending = {{
  approval_id: 'appr-1',
  _session_id: 'sess-1',
  command: 'rm -rf /tmp/test',
  description: 'Dangerous command approval',
  pattern_key: 'dangerous_command',
  pattern_keys: ['dangerous_command'],
}};
_approvalPendingBySession.set('sess-1', {{ pending, pendingCount: 1 }});
showApprovalCard(pending, 1);
const realRenderPendingApprovalForActiveSession = _renderPendingApprovalForActiveSession;
renderCalls = 0;
_renderPendingApprovalForActiveSession = function() {{
  renderCalls += 1;
  return realRenderPendingApprovalForActiveSession();
}};
respondApproval('once').then(() => {{
  output.sessionId = _approvalSessionId;
  output.approvalId = _approvalCurrentId;
  output.pendingApprovalId = _approvalPendingBySession.get('sess-1').pending.approval_id;
  output.hideCalls = hideCalls;
  output.renderCalls = renderCalls;
  output.toast = showToastCalls[0] || null;
  output.status = statusCalls[0] || null;
  output.onceDisabled = buttons.get('approvalBtnOnce').disabled;
  output.onceLoading = buttons.get('approvalBtnOnce').classList.contains('loading');
  output.denyDisabled = buttons.get('approvalBtnDeny').disabled;
  output.cardVisible = card.classList.contains('visible');
  process.stdout.write(JSON.stringify(output));
}});
"""
    return _run_node(script)


def test_failed_gateway_approval_keeps_card_and_reenables_buttons():
    out = _run_failure_case(
        """function api() {
  const err = new Error('Gateway approval could not be relayed because the active run is unavailable. Reopen the session or retry after it reconnects.');
  err.status = 409;
  return Promise.reject(err);
}"""
    )
    assert out["sessionId"] == "sess-1"
    assert out["approvalId"] == "appr-1"
    assert out["pendingApprovalId"] == "appr-1"
    assert out["hideCalls"] == 0
    assert out["renderCalls"] == 1
    assert out["onceDisabled"] is False
    assert out["onceLoading"] is False
    assert out["denyDisabled"] is False
    assert out["cardVisible"] is True
    assert "active run is unavailable" in out["toast"]
    assert "active run is unavailable" in out["status"]


def test_rejected_ok_false_approval_keeps_card_and_reenables_buttons():
    out = _run_failure_case(
        """function api() {
  return Promise.resolve({
    ok: false,
    error: 'Approval response not accepted for this session.',
  });
}"""
    )
    assert out["sessionId"] == "sess-1"
    assert out["approvalId"] == "appr-1"
    assert out["pendingApprovalId"] == "appr-1"
    assert out["hideCalls"] == 0
    assert out["renderCalls"] == 1
    assert out["onceDisabled"] is False
    assert out["onceLoading"] is False
    assert out["denyDisabled"] is False
    assert out["cardVisible"] is True
    assert "Approval response not accepted for this session." == out["toast"]
    assert "Approval response not accepted for this session." == out["status"]


def test_poll_rerender_keeps_inflight_buttons_disabled_and_blocks_duplicates():
    helpers = "\n".join(
        [
            _extract_fn(MESSAGES_JS, "_approvalDismissKey"),
            _extract_fn(MESSAGES_JS, "_getDismissedApprovals"),
            _extract_fn(MESSAGES_JS, "_isApprovalDismissed"),
            _extract_fn(MESSAGES_JS, "_unmarkApprovalDismissed"),
            _extract_fn(MESSAGES_JS, "_promptActiveSessionId"),
            _extract_fn(MESSAGES_JS, "_approvalPromptBelongsToActiveSession"),
            _extract_fn(MESSAGES_JS, "_rememberApprovalPending"),
            _extract_fn(MESSAGES_JS, "_clearApprovalPendingForSession"),
            _extract_fn(MESSAGES_JS, "_renderPendingApprovalForActiveSession"),
            _extract_fn(MESSAGES_JS, "_approvalResponseMatches"),
            _extract_fn(MESSAGES_JS, "_setApprovalControlsDisabled"),
            _extract_fn(MESSAGES_JS, "_setPromptFlyoutHidden"),
            _extract_fn(MESSAGES_JS, "showApprovalCard"),
            _extract_fn(MESSAGES_JS, "_restoreFailedApprovalResponse"),
            _extract_fn(MESSAGES_JS, "respondApproval", prefix="async function "),
        ]
    )
    script = f"""
const output = {{}};
const _store = {{}};
const localStorage = {{
  getItem: key => Object.prototype.hasOwnProperty.call(_store, key) ? _store[key] : null,
  setItem: (key, value) => {{ _store[key] = String(value); }},
}};
const S = {{ session: {{ session_id: 'sess-1' }} }};
const _DISMISSED_APPROVALS_KEY = 'hermes_dismissed_approvals';
let _approvalSessionId = 'sess-1';
let _approvalCurrentId = 'appr-1';
let _approvalPendingBySession = new Map();
let _approvalResponding = null;
let _approvalSignature = '';
let _approvalVisibleSince = 0;
let _approvalHideTimer = null;
const _clarifyPendingBySession = new Map();
let resolveApi;
let apiCalls = 0;
const buttons = new Map();
function makeButton(id) {{
  return {{
    id,
    disabled: false,
    classList: {{
      values: new Set(),
      add(value) {{ this.values.add(value); }},
      remove(value) {{ this.values.delete(value); }},
      contains(value) {{ return this.values.has(value); }},
    }},
  }};
}}
['approvalBtnOnce','approvalBtnSession','approvalBtnAlways','approvalBtnDeny'].forEach(id => buttons.set(id, makeButton(id)));
const card = {{
  classList: {{
    values: new Set(['visible']),
    add(value) {{ this.values.add(value); }},
    remove(value) {{ this.values.delete(value); }},
    contains(value) {{ return this.values.has(value); }},
    toggle(value, force) {{
      if (force === undefined) {{
        if (this.values.has(value)) this.values.delete(value);
        else this.values.add(value);
      }} else if (force) this.values.add(value);
      else this.values.delete(value);
    }},
  }},
  hidden: false,
  attributes: {{}},
  setAttribute(name, value) {{ this.attributes[name] = String(value); }},
  removeAttribute(name) {{ delete this.attributes[name]; }},
  querySelector() {{ return null; }},
}};
const approvalDesc = {{ textContent: '' }};
const approvalCmd = {{ textContent: '' }};
const approvalCounter = {{ textContent: '', style: {{ display: '' }} }};
const msg = {{}};
const document = {{ activeElement: null }};
const elements = {{
  approvalCard: card,
  approvalDesc,
  approvalCmd,
  approvalCounter,
  approvalBtnOnce: buttons.get('approvalBtnOnce'),
  approvalBtnSession: buttons.get('approvalBtnSession'),
  approvalBtnAlways: buttons.get('approvalBtnAlways'),
  approvalBtnDeny: buttons.get('approvalBtnDeny'),
  msg,
}};
function $(id) {{ return elements[id] || null; }}
function syncTopbar() {{}}
function hideApprovalCard() {{ card.classList.remove('visible'); }}
function _clearApprovalHideTimer() {{}}
function _syncApprovalCollapseButton() {{}}
function _syncApprovalTranscriptSpace() {{}}
function applyLocaleToDOM() {{}}
function setTimeout(fn) {{ return 1; }}
function showToast() {{}}
function setStatus() {{}}
function t(key) {{ return key; }}
function api() {{
  apiCalls += 1;
  return new Promise(resolve => {{ resolveApi = resolve; }});
}}
{helpers}
const pending = {{
  approval_id: 'appr-1',
  _session_id: 'sess-1',
  command: 'rm -rf /tmp/test',
  description: 'Dangerous command approval',
  pattern_key: 'dangerous_command',
  pattern_keys: ['dangerous_command'],
}};
_approvalPendingBySession.set('sess-1', {{ pending, pendingCount: 1 }});
showApprovalCard(pending, 1);
const first = respondApproval('once');
_renderPendingApprovalForActiveSession();
const second = respondApproval('once');
output.apiCallsDuringFlight = apiCalls;
output.onceDisabledDuringFlight = buttons.get('approvalBtnOnce').disabled;
output.onceLoadingDuringFlight = buttons.get('approvalBtnOnce').classList.contains('loading');
resolveApi({{ ok: false }});
Promise.all([first, second]).then(() => {{
  output.apiCallsFinal = apiCalls;
  output.onceDisabledAfter = buttons.get('approvalBtnOnce').disabled;
  output.onceLoadingAfter = buttons.get('approvalBtnOnce').classList.contains('loading');
  output.cardVisibleAfter = card.classList.contains('visible');
  process.stdout.write(JSON.stringify(output));
}});
"""
    out = _run_node(script)
    assert out["apiCallsDuringFlight"] == 1
    assert out["onceDisabledDuringFlight"] is True
    assert out["onceLoadingDuringFlight"] is True
    assert out["apiCallsFinal"] == 1
    assert out["onceDisabledAfter"] is False
    assert out["onceLoadingAfter"] is False
    assert out["cardVisibleAfter"] is True
