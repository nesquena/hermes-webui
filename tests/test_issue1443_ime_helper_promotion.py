"""Regression tests for issue #1443 — promote `_isImeEnter` to all Safari-affected Enter guards.

PR #1441 (v0.50.264) widened the chat composer's IME-Enter guard from `e.isComposing`
to a `_isImeEnter(e)` helper in `static/boot.js`. The helper combines three signals
(`e.isComposing || e.keyCode === 229 || _imeComposing`) so it catches the Safari race
where the committing keydown for an IME composition fires AFTER `compositionend` with
`isComposing=false`.

Six other Enter-input handlers were left on the original `e.isComposing` guard:

  - `static/sessions.js` — session rename (~line 1693)
  - `static/sessions.js` — project create  (~line 1987)
  - `static/sessions.js` — project rename  (~line 2015)
  - `static/ui.js`        — app dialog (confirm/prompt) (~line 2482)
  - `static/ui.js`        — message edit (Enter to save) (~line 4106)
  - `static/ui.js`        — workspace rename (~line 5007)

Issue #1443 promotes the helper to `window._isImeEnter` (defined in boot.js) and
replaces the 6 `e.isComposing` guards with `window._isImeEnter(e)`. These tests pin
each site so a future cleanup that strips the windowed call trips a test.

The state-free part of the helper (`e.isComposing || e.keyCode === 229`) is what the
6 non-composer sites rely on — it works for any focused input on Safari without needing
per-input composition listeners or a per-input flag.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _windowed_guard(ev: str) -> str:
    """Return regex for `if(window._isImeEnter && window._isImeEnter(<ev>))` shape."""
    return (
        rf"if\s*\(\s*window\._isImeEnter\s*&&\s*"
        rf"window\._isImeEnter\s*\(\s*{ev}\s*\)\s*\)\s*"
    )


# ── Promotion: `window._isImeEnter` is exported from boot.js ─────────────────


def test_isimeenter_helper_is_exposed_on_window():
    """boot.js must attach `_isImeEnter` to `window` so other modules can reuse it."""
    assert re.search(
        r"window\._isImeEnter\s*=\s*_isImeEnter\s*;?",
        BOOT_JS,
    ), (
        "boot.js must export `window._isImeEnter = _isImeEnter` so "
        "static/sessions.js and static/ui.js can call the same Safari-aware "
        "helper without duplicating the IIFE per input (issue #1443)."
    )


# ── No raw `e.isComposing` guards remain in the 6 non-composer sites ─────────


def test_no_isComposing_guards_remain_in_sessions_js():
    """sessions.js must not contain a raw `e.isComposing` Enter-guard anymore."""
    leaks = re.findall(r"\b(?:e2?)\.isComposing\b", SESSIONS_JS)
    assert not leaks, (
        f"sessions.js still contains {len(leaks)} raw `e.isComposing` guard(s); "
        f"all Enter-input handlers should route through window._isImeEnter "
        f"(issue #1443)."
    )


def test_no_isComposing_guards_remain_in_ui_js():
    """ui.js must not contain a raw `e.isComposing` Enter-guard anymore."""
    leaks = re.findall(r"\b(?:e2?)\.isComposing\b", UI_JS)
    assert not leaks, (
        f"ui.js still contains {len(leaks)} raw `e.isComposing` guard(s); "
        f"all Enter-input handlers should route through window._isImeEnter "
        f"(issue #1443)."
    )


# ── Each of the 6 specific sites uses the promoted helper ────────────────────


def test_session_rename_uses_windowed_helper():
    """Session rename (sessions.js ~1693) must use window._isImeEnter."""
    # The session rename block: `inp.onkeydown=e2=>{ if(e2.key==='Enter'){ <guard> ...
    pattern = re.compile(
        r"inp\.onkeydown\s*=\s*e2\s*=>\s*\{\s*"
        r"if\s*\(\s*e2\.key\s*===\s*'Enter'\s*\)\s*\{\s*"
        + _windowed_guard("e2"),
        re.DOTALL,
    )
    assert pattern.search(SESSIONS_JS), (
        "Session rename Enter handler in static/sessions.js must use "
        "window._isImeEnter(e2) (issue #1443)."
    )


def test_project_create_and_rename_use_windowed_helper():
    """Project create + project rename (sessions.js ~1987 and ~2015) both use window._isImeEnter."""
    # Both project blocks share the shape `inp.onkeydown=(e)=>{...}` (note the parens).
    pattern = re.compile(
        r"inp\.onkeydown\s*=\s*\(\s*e\s*\)\s*=>\s*\{\s*"
        r"if\s*\(\s*e\.key\s*===\s*'Enter'\s*\)\s*\{\s*"
        + _windowed_guard("e"),
        re.DOTALL,
    )
    matches = pattern.findall(SESSIONS_JS)
    assert len(matches) >= 2, (
        f"Project create AND project rename Enter handlers in static/sessions.js "
        f"must both use window._isImeEnter(e); found {len(matches)} of 2 expected "
        f"(issue #1443)."
    )


def test_app_dialog_uses_windowed_helper():
    """App dialog confirm/prompt (ui.js ~2482) must use window._isImeEnter."""
    # Pattern: `document.addEventListener('keydown',e=>{ ... if(e.key==='Enter'){
    #   if(window._isImeEnter && window._isImeEnter(e)) return;`
    pattern = re.compile(
        r"document\.addEventListener\(\s*'keydown'\s*,\s*e\s*=>\s*\{[\s\S]*?"
        r"if\s*\(\s*e\.key\s*===\s*'Enter'\s*\)\s*\{\s*"
        + _windowed_guard("e"),
        re.DOTALL,
    )
    assert pattern.search(UI_JS), (
        "App dialog confirm/prompt Enter handler in static/ui.js must use "
        "window._isImeEnter(e) (issue #1443)."
    )


def _matching_brace(src: str, open_at: int) -> int:
    depth = 0
    for idx in range(open_at, len(src)):
        char = src[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return idx
    raise AssertionError("unbalanced brace")


def _function_source(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.index(marker)
    return src[start : _matching_brace(src, src.index("{", start)) + 1]


def _arrow_statement(src: str, marker: str) -> str:
    start = src.index(marker)
    end = _matching_brace(src, src.index("{", start)) + 1
    while end < len(src) and src[end] in " \t\r\n":
        end += 1
    if end < len(src) and src[end] == ")":
        end += 1
        while end < len(src) and src[end] in " \t\r\n":
            end += 1
    if end < len(src) and src[end] == ";":
        end += 1
    return src[start:end]


def _message_edit_keydown() -> str:
    return _arrow_statement(UI_JS, "ta.addEventListener('keydown'")


def _clarify_keydown() -> str:
    return _arrow_statement(MESSAGES_JS, "input.onkeydown = (e) =>")


# Outcomes of the real helper. HOLD means the handler must neither
# preventDefault nor submit. edit-guard-* removes the helper so only the
# retained window._isImeEnter return can stop an IME Enter.
_SEND = {"predicate": True, "prevented": True, "sends": 1}
_HOLD = {"predicate": False, "prevented": False, "sends": 0}
_ENTER_CASES = {
    "enter": _SEND,
    "shift": _HOLD,
    "ime-composing": _HOLD,
    "ime-229": _HOLD,
    "ime-flag": _HOLD,
    "shift-mode-plain": _HOLD,
    "shift-mode-shift": _SEND,
    "shift-mode-ime": _HOLD,
    "ctrl-mode-plain": _HOLD,
    "ctrl-mode-ctrl": _SEND,
    "ctrl-mode-numpad": _SEND,
    "mobile-plain": _HOLD,
    "mobile-ctrl": _SEND,
    "other-key": _HOLD,
    "edit-guard-composing": _HOLD,
    "edit-guard-229": _HOLD,
    "edit-guard-flag": _HOLD,
    "edit-guard-plain": _SEND,
}
# Clarify is a single-line input with no newline to insert, so every Enter but an
# IME one submits it, whatever the send key or device (#8769). clarify-guard-*
# removes window._isImeEnter so only the handler's own fallback can stop an IME Enter.
_CLARIFY_SEND = {"prevented": True, "sends": 1}
_CLARIFY_HOLD = {"prevented": False, "sends": 0}
_CLARIFY_CASES = {
    "enter": _CLARIFY_SEND,
    "shift": _CLARIFY_SEND,
    "ime-composing": _CLARIFY_HOLD,
    "ime-229": _CLARIFY_HOLD,
    "ime-flag": _CLARIFY_HOLD,
    "shift-mode-plain": _CLARIFY_SEND,
    "shift-mode-shift": _CLARIFY_SEND,
    "shift-mode-ime": _CLARIFY_HOLD,
    "ctrl-mode-plain": _CLARIFY_SEND,
    "ctrl-mode-ctrl": _CLARIFY_SEND,
    "ctrl-mode-numpad": _CLARIFY_SEND,
    "mobile-plain": _CLARIFY_SEND,
    "mobile-ctrl": _CLARIFY_SEND,
    "other-key": _CLARIFY_HOLD,
    "clarify-guard-composing": _CLARIFY_HOLD,
    "clarify-guard-229": _CLARIFY_HOLD,
    "clarify-guard-plain": _CLARIFY_SEND,
}


def _drive_enter_handlers() -> list:
    """Run the real predicate and the clarify + inline-edit handlers."""
    if shutil.which("node") is None:
        pytest.skip("node unavailable")
    edit = _message_edit_keydown()
    clarify = _clarify_keydown()
    functions = "\n".join(
        _function_source(BOOT_JS, name)
        for name in (
            "_isImeEnter",
            "_hasFinePointerCoexisting",
            "_isTouchOnlyDevice",
            "_isNumpadEnter",
            "_composerShouldSendOnKey",
        )
    )
    script = """
const window = {};
const navigator = {userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'};
function matchMedia(){ return {matches: false}; }
const KeyboardEvent = {DOM_KEY_LOCATION_NUMPAD: 3};
let _imeComposing = false;
""" + functions + """
let editSends = 0;
let clarifySends = 0;
const ta = {addEventListener(type, fn){ if(type==='keydown') this.onKeyDown = fn; }};
const bar = {querySelector(sel){ return {click(){ if(String(sel).indexOf('msg-edit-send')!==-1) editSends += 1; }}; }};
function cancelEdit(){}
const row = {};
const originalText = '';
const body = {};
""" + edit + """
const editHandler = ta.onKeyDown;
function respondClarify(){ clarifySends += 1; }
const input = {};
""" + clarify + """
const clarifyHandler = input.onkeydown;
const DESKTOP = navigator.userAgent;
const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)';
function makeEvent(spec){
  return {
    key: spec.key || 'Enter',
    shiftKey: !!spec.shift,
    ctrlKey: !!spec.ctrl,
    metaKey: !!spec.meta,
    altKey: false,
    isComposing: !!spec.composing,
    keyCode: spec.keyCode == null ? 13 : spec.keyCode,
    code: spec.code || 'Enter',
    location: spec.location || 0,
    defaultPrevented: false,
    preventDefault(){ this.defaultPrevented = true; },
  };
}
const results = [];
function check(label, spec, handlers){
  _imeComposing = !!spec.imeFlag;
  window._sendKey = spec.sendKey || 'enter';
  navigator.userAgent = spec.mobile ? IPHONE : DESKTOP;
  window._isImeEnter = spec.noIme ? undefined : _isImeEnter;
  window._composerShouldSendOnKey = spec.noHelper ? undefined : _composerShouldSendOnKey;
  for(const name of handlers){
    editSends = 0;
    clarifySends = 0;
    const event = makeEvent(spec);
    (name==='edit' ? editHandler : clarifyHandler)(event);
    results.push({
      handler: name,
      label,
      prevented: event.defaultPrevented,
      sends: name==='edit' ? editSends : clarifySends,
      predicate: _composerShouldSendOnKey(makeEvent(spec)),
    });
  }
}
const both = ['edit', 'clarify'];
check('enter', {}, both);
check('shift', {shift: true}, both);
check('ime-composing', {composing: true}, both);
check('ime-229', {keyCode: 229}, both);
check('ime-flag', {imeFlag: true}, both);
check('shift-mode-plain', {sendKey: 'shift+enter'}, both);
check('shift-mode-shift', {sendKey: 'shift+enter', shift: true}, both);
check('shift-mode-ime', {sendKey: 'shift+enter', shift: true, composing: true}, both);
check('ctrl-mode-plain', {sendKey: 'ctrl+enter'}, both);
check('ctrl-mode-ctrl', {sendKey: 'ctrl+enter', ctrl: true}, both);
check('ctrl-mode-numpad', {sendKey: 'ctrl+enter', code: 'NumpadEnter', location: 3}, both);
check('mobile-plain', {mobile: true}, both);
check('mobile-ctrl', {mobile: true, ctrl: true}, both);
check('other-key', {key: 'a'}, both);
check('edit-guard-composing', {composing: true, noHelper: true}, ['edit']);
check('edit-guard-229', {keyCode: 229, noHelper: true}, ['edit']);
check('edit-guard-flag', {imeFlag: true, noHelper: true}, ['edit']);
check('edit-guard-plain', {noHelper: true}, ['edit']);
check('clarify-guard-composing', {composing: true, noIme: true}, ['clarify']);
check('clarify-guard-229', {keyCode: 229, noIme: true}, ['clarify']);
check('clarify-guard-plain', {noIme: true}, ['clarify']);
console.log(JSON.stringify(results));
"""
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _enter_row(rows: list, handler: str, label: str) -> dict:
    return next(row for row in rows if row["handler"] == handler and row["label"] == label)


def test_message_edit_uses_windowed_helper():
    """Inline edit decides Enter with the shared helper and still drops IME Enter.

    The callback used to begin with `if(e.key==='Enter' && !e.shiftKey)`. That
    spelling is no longer the decision. The helper chooses send vs newline, and
    `window._isImeEnter` still returns before preventDefault and Send — including
    when the helper itself is missing and the old comparison is only the fallback.
    """
    edit = _message_edit_keydown()
    assert re.search(r"if\s*\(\s*window\._composerShouldSendOnKey\s*\?", edit), (
        "Message edit must decide Enter through window._composerShouldSendOnKey "
        "(issue #1443, send-key helper)."
    )
    assert re.search(_windowed_guard("e") + r"return\s*;", edit), (
        "Message edit Enter-to-save handler in static/ui.js must use "
        "window._isImeEnter(e) (issue #1443)."
    )
    assert edit.index("window._isImeEnter") < edit.index("preventDefault") < edit.index(".msg-edit-send")
    assert "if(e.key==='Enter' && !e.shiftKey)" not in edit
    assert "if(e.key==='Enter'&&!e.shiftKey)" not in edit

    rows = _drive_enter_handlers()
    for label in (
        "ime-composing",
        "ime-229",
        "ime-flag",
        "edit-guard-composing",
        "edit-guard-229",
        "edit-guard-flag",
    ):
        row = _enter_row(rows, "edit", label)
        assert row["prevented"] is False and row["sends"] == 0, row
    plain = _enter_row(rows, "edit", "enter")
    assert plain["predicate"] is True and plain["prevented"] is True and plain["sends"] == 1
    fallback = _enter_row(rows, "edit", "edit-guard-plain")
    assert fallback["prevented"] is True and fallback["sends"] == 1, fallback


def test_edit_follows_shared_enter_predicate_and_clarify_submits_every_enter():
    """Inline edit follows `_composerShouldSendOnKey`, including IME; clarify does not.

    For the edit textarea, Shift+Enter / Ctrl+Enter / the mobile newline default
    must not be collapsed back to plain Enter-without-Shift. Clarify is a
    single-line field, so a held Enter would do nothing at all: every Enter but an
    IME one submits it, whatever the send key or device (#8769).
    """
    clarify = _clarify_keydown()
    assert "_composerShouldSendOnKey" not in clarify
    assert "respondClarify()" in clarify
    rows = _drive_enter_handlers()
    seen = {(row["handler"], row["label"]) for row in rows}
    for label, expect in _ENTER_CASES.items():
        row = _enter_row(rows, "edit", label)
        assert row["predicate"] is expect["predicate"], row
        assert row["prevented"] is expect["prevented"], row
        assert row["sends"] == expect["sends"], row
    for label, expect in _CLARIFY_CASES.items():
        row = _enter_row(rows, "clarify", label)
        assert row["prevented"] is expect["prevented"], row
        assert row["sends"] == expect["sends"], row
    assert seen == {("edit", label) for label in _ENTER_CASES} | {("clarify", label) for label in _CLARIFY_CASES}


def test_workspace_rename_uses_windowed_helper():
    """Workspace rename (ui.js ~5007) must use window._isImeEnter."""
    # Pattern: `inp.onkeydown=(e2)=>{ if(e2.key==='Enter'){ if(window._isImeEnter && ...
    pattern = re.compile(
        r"inp\.onkeydown\s*=\s*\(\s*e2\s*\)\s*=>\s*\{\s*"
        r"if\s*\(\s*e2\.key\s*===\s*'Enter'\s*\)\s*\{\s*"
        + _windowed_guard("e2"),
        re.DOTALL,
    )
    assert pattern.search(UI_JS), (
        "Workspace rename Enter handler in static/ui.js must use "
        "window._isImeEnter(e2) (issue #1443)."
    )


# ── Helper still has the 3-guard shape (regression on PR #1441) ──────────────


def test_isimeenter_still_has_three_guards():
    """The helper itself must still combine all three guards. Promotion to `window`
    must not have stripped any of them."""
    pattern = re.compile(
        r"function\s+_isImeEnter\s*\(\s*e\s*\)\s*\{[^}]*"
        r"e\.isComposing"
        r"[^}]*"
        r"e\.keyCode\s*===\s*229"
        r"[^}]*"
        r"_imeComposing"
        r"[^}]*\}",
        re.DOTALL,
    )
    assert pattern.search(BOOT_JS), (
        "_isImeEnter must still combine e.isComposing, keyCode===229, and "
        "_imeComposing flag after promotion to window (PR #1441 + issue #1443)."
    )
