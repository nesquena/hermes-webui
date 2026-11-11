"""Tests for #5099 (Ctrl+K must not steal kill-line in text fields).

Emacs-adjacent users expect Ctrl+K to kill to end-of-line while the composer
or other editable fields are focused. Cmd/Ctrl+K should still create a new chat
when the event target is outside text inputs. Cmd/Ctrl+B toggles the sidebar
on the same kind of non-text control, and also stays quiet when a text field
is focused even if that field is not the event target.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

BOOT_JS = (Path(__file__).parent.parent / "static" / "boot.js").read_text(encoding="utf-8")


def _ctrl_k_branch_window() -> str:
    idx = BOOT_JS.find("(e.metaKey||e.ctrlKey)&&e.key==='k'")
    assert idx >= 0, "Cmd/Ctrl+K handler not found in boot.js"
    return BOOT_JS[idx:idx + 1500]


def _global_keydown_listener() -> str:
    """The boot.js listener that owns both the Ctrl+B and Ctrl+K shortcuts."""
    signature = "document.addEventListener('keydown',async e=>{"
    start = BOOT_JS.find(signature)
    assert start >= 0, "global keydown listener not found in boot.js"
    depth = 0
    for idx in range(start, len(BOOT_JS)):
        char = BOOT_JS[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return BOOT_JS[start:idx + 3]
    raise AssertionError("global keydown listener is not closed")


def _drive_sidebar_shortcuts() -> dict:
    """Run the real listener against a Node DOM shim. No source-spelling checks."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    listener = _global_keydown_listener()
    script = (
        "const listenerSrc = " + json.dumps(listener) + ";\n"
        + r"""
const factory = new Function('document', 'api',
  'const $ = api.$;\n' +
  'const _handleAttachChoiceKeydown = api._handleAttachChoiceKeydown;\n' +
  'const toggleSidebar = api.toggleSidebar;\n' +
  'const _isDesktopWidth = api._isDesktopWidth;\n' +
  'const _currentSessionIsReusableEmptyChat = api._currentSessionIsReusableEmptyChat;\n' +
  'const newSession = api.newSession;\n' +
  'const closeMobileSidebar = api.closeMobileSidebar;\n' +
  'const _enterBelongsToFocusedControl = () => false;\n' +
  'const respondApproval = () => {};\n' +
  'const toggleSettings = () => {};\n' +
  'const skipOnboarding = () => {};\n' +
  'const _closeSettingsPanel = () => {};\n' +
  'const closeWsDropdown = () => {};\n' +
  'const clearSessionSearch = () => {};\n' +
  'const filterSessions = () => {};\n' +
  'let _imeComposing = false;\n' +
  'let _currentPanel = "";\n' +
  listenerSrc + '\nreturn document._handler;'
);
function makeEl(tag, editable) {
  return { tagName: tag, isContentEditable: !!editable };
}
const input = makeEl('INPUT', false);
const textarea = makeEl('TEXTAREA', false);
const editable = makeEl('DIV', true);
const button = makeEl('BUTTON', false);
let active = null;
const calls = { prevent: 0, toggle: 0, newSession: 0, closeMobile: 0 };
const document = {
  get activeElement() { return active; },
  body: { tagName: 'BODY' },
  documentElement: { tagName: 'HTML' },
  querySelector() { return null; },
  addEventListener(type, fn) {
    if (type !== 'keydown') throw new Error('unexpected listener ' + type);
    this._handler = fn;
  },
};
const api = {
  $(id) {
    return {
      id: id,
      value: '',
      classList: { contains() { return false; } },
      style: { display: 'none' },
      focus() {},
      blur() {},
    };
  },
  _handleAttachChoiceKeydown() { return false; },
  toggleSidebar() { calls.toggle += 1; },
  _isDesktopWidth() { return true; },
  _currentSessionIsReusableEmptyChat() { return false; },
  async newSession() { calls.newSession += 1; },
  closeMobileSidebar() { calls.closeMobile += 1; },
};
const handler = factory(document, api);
const cases = [
  ['b-input', 'b', input, input],
  ['b-textarea', 'b', textarea, textarea],
  ['b-contenteditable', 'b', editable, editable],
  ['k-input', 'k', input, input],
  ['k-textarea', 'k', textarea, textarea],
  ['k-contenteditable', 'k', editable, editable],
  ['b-focused-text-field', 'b', button, textarea],
  ['k-focused-text-field', 'k', button, textarea],
  ['b-button', 'b', button, button],
  ['k-button', 'k', button, button],
];
(async () => {
  const out = {};
  for (const [id, key, target, focused] of cases) {
    calls.prevent = 0;
    calls.toggle = 0;
    calls.newSession = 0;
    calls.closeMobile = 0;
    active = focused;
    const event = {
      key: key,
      metaKey: false,
      ctrlKey: true,
      shiftKey: false,
      altKey: false,
      target: target,
      defaultPrevented: false,
      isComposing: false,
      preventDefault() {
        this.defaultPrevented = true;
        calls.prevent += 1;
      },
    };
    await handler(event);
    out[id] = {
      prevent: calls.prevent,
      toggle: calls.toggle,
      newSession: calls.newSession,
      closeMobile: calls.closeMobile,
      defaultPrevented: event.defaultPrevented,
    };
  }
  console.log(JSON.stringify(out));
})().catch(err => {
  console.error(err && err.stack || err);
  process.exit(1);
});
"""
    )
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            "shortcut harness failed\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return json.loads(result.stdout)


class TestIssue5099CtrlKTextInputGuard:
    def test_ctrl_k_skips_text_inputs(self):
        branch = _ctrl_k_branch_window()
        assert "tagName==='INPUT'" in branch
        assert "tagName==='TEXTAREA'" in branch
        assert "isContentEditable" in branch
        assert "if(isText) return" in branch

    def test_ctrl_k_prevent_default_after_text_guard(self):
        branch = _ctrl_k_branch_window()
        guard_idx = branch.find("if(isText) return")
        prevent_idx = branch.find("e.preventDefault()")
        assert guard_idx >= 0 and prevent_idx >= 0, (
            "Ctrl+K must guard text inputs before calling preventDefault()"
        )
        assert guard_idx < prevent_idx, (
            "preventDefault() must not run before the text-input early return"
        )

    def test_ctrl_b_and_ctrl_k_leave_text_editing_alone(self):
        # Drives the real handlers. Ctrl+B consults the event target and the
        # focused element. Ctrl+K (#5099) still decides from the event target
        # alone; that split is the current product behavior, not a spelling check.
        outcomes = _drive_sidebar_shortcuts()
        quiet = (
            "b-input",
            "b-textarea",
            "b-contenteditable",
            "k-input",
            "k-textarea",
            "k-contenteditable",
            "b-focused-text-field",
        )
        for case_id in quiet:
            row = outcomes[case_id]
            assert row == {
                "prevent": 0,
                "toggle": 0,
                "newSession": 0,
                "closeMobile": 0,
                "defaultPrevented": False,
            }, case_id

        # Focus sits in a textarea while the event target is a button.
        # Ctrl+K does not use the active-element guard, so the shortcut runs.
        assert outcomes["k-focused-text-field"] == {
            "prevent": 1,
            "toggle": 0,
            "newSession": 1,
            "closeMobile": 1,
            "defaultPrevented": True,
        }
        assert outcomes["b-button"] == {
            "prevent": 1,
            "toggle": 1,
            "newSession": 0,
            "closeMobile": 0,
            "defaultPrevented": True,
        }
        assert outcomes["k-button"] == {
            "prevent": 1,
            "toggle": 0,
            "newSession": 1,
            "closeMobile": 1,
            "defaultPrevented": True,
        }

    def test_ctrl_k_still_creates_new_session_outside_inputs(self):
        branch = _ctrl_k_branch_window()
        assert "newSession()" in branch
        assert "closeMobileSidebar()" in branch
