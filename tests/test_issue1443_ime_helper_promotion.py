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

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


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


def test_message_edit_uses_windowed_helper():
    """Message edit Enter-to-save (ui.js ~4106) must use window._isImeEnter."""
    # Pattern: `ta.addEventListener('keydown', e => { if(e.key==='Enter' && !e.shiftKey)
    #   { if(window._isImeEnter && window._isImeEnter(e)) return;`
    pattern = re.compile(
        r"ta\.addEventListener\(\s*'keydown'\s*,\s*e\s*=>\s*\{\s*"
        r"if\s*\(\s*e\.key\s*===\s*'Enter'\s*&&\s*!\s*e\.shiftKey\s*\)\s*\{\s*"
        + _windowed_guard("e"),
        re.DOTALL,
    )
    assert pattern.search(UI_JS), (
        "Message edit Enter-to-save handler in static/ui.js must use "
        "window._isImeEnter(e) (issue #1443)."
    )


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
