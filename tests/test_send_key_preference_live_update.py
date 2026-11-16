"""Regression: send-key preference changes take effect without page reload.

This test catches the bug where an inline capture-phase interceptor in
index.html snapshotted ``_sendKey`` into a closure at page-parse time.
When the user later changed the setting (which updates ``window._sendKey``
via panels.js), the stale closure still called ``stopImmediatePropagation()``
and blocked the newly configured shortcut from reaching the boot.js
composer handler.

The fix removes the inline interceptor entirely — boot.js already
initialises ``window._sendKey`` synchronously and its composer handler
reads the live value on every event.

This Playwright test loads a minimal HTML page that includes the real
boot.js composer keydown handler (extracted verbatim), simulates a
preference change at runtime, and verifies the correct behaviour for
each send-key mode — including the transition from one mode to another
without a page reload.
"""
from __future__ import annotations

from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

REPO = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")


def _require_playwright():
    if sync_playwright is None:
        pytest.skip("playwright is unavailable; run `playwright install chromium`")
    return sync_playwright


def _extract_composer_handler() -> str:
    """Extract the real ``$('msg').addEventListener('keydown', …)`` handler
    from boot.js, including its full body and the closing ``);``."""
    signature = "$('msg').addEventListener('keydown',e=>"
    start = BOOT_JS.index(signature)
    brace = BOOT_JS.index("{", start)
    depth = 0
    for idx in range(brace, len(BOOT_JS)):
        char = BOOT_JS[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                # Include the closing ``);`` that completes the addEventListener call.
                end = BOOT_JS.index(");", idx) + 2
                return BOOT_JS[start:end]
    raise AssertionError("could not extract composer keydown listener")


def _extract_function(name: str, source: str) -> str:
    """Extract a function by name, matching braces properly."""
    pattern = f"function {name}("
    start = source.index(pattern)
    brace_start = source.index("{", start)
    depth = 0
    for i in range(brace_start, len(source)):
        c = source[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise ValueError(f"Could not extract function {name}")


def _extract_helper_functions() -> str:
    """Extract the helper functions the composer handler depends on."""
    names = ("_isImeEnter", "_isNumpadEnter", "_isTouchOnlyDevice")
    return "\n".join(_extract_function(n, BOOT_JS) for n in names)


# Minimal HTML that loads the real handler logic.
HARNESS_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"></head>
<body>
<textarea id="msg" rows="1"></textarea>
<script>
// _imeComposing is a module-level flag in boot.js that _isImeEnter references.
var _imeComposing = false;
// Minimal $ shim (boot.js uses $('msg') === document.getElementById('msg'))
function $(id) { return document.getElementById(id); }

// --- Real helper functions extracted from boot.js ---
__HELPERS__

// --- Synchronous _sendKey init (from boot.js line ~2362) ---
try { window._sendKey = localStorage.getItem('hermes-pref-send_key') || 'enter'; } catch(_) { window._sendKey = 'enter'; }

// --- Real composer handler extracted from boot.js ---
// send() stub: count invocations instead of doing real work.
window.__sendCount = 0;
window.send = function() { window.__sendCount++; };
var send = window.send;  // local alias so extracted handler's send() resolves

__HANDLER__
</script>
</body></html>
"""


def _build_harness_html() -> str:
    return HARNESS_HTML.replace("__HELPERS__", _extract_helper_functions()).replace(
        "__HANDLER__", _extract_composer_handler()
    )


@pytest.fixture
def page():
    _require_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context()
        pg = context.new_page()
        html = _build_harness_html()
        import tempfile

        tmp = Path(tempfile.mkstemp(suffix=".html")[1])
        tmp.write_text(html, encoding="utf-8")
        pg.goto(tmp.as_uri())
        pg.wait_for_load_state("domcontentloaded")
        yield pg
        browser.close()


def _set_send_key(page, mode: str):
    """Simulate panels.js applying a saved settings preference at runtime."""
    page.evaluate(
        f"""() => {{
            localStorage.setItem('hermes-pref-send_key', '{mode}');
            window._sendKey = '{mode}';
        }}"""
    )


def _press_enter(page, *, shift=False, ctrl=False, meta=False, numpad=False):
    """Dispatch a real Enter keydown on #msg with optional modifiers."""
    code = "NumpadEnter" if numpad else "Enter"
    location = 3 if numpad else 0
    page.evaluate(
        """
        ({shift, ctrl, meta, code, location}) => {
            const el = document.getElementById('msg');
            el.focus();
            el.dispatchEvent(new KeyboardEvent('keydown', {
                key: 'Enter',
                code: code,
                keyCode: 13,
                which: 13,
                location: location,
                shiftKey: shift,
                ctrlKey: ctrl,
                metaKey: meta,
                bubbles: true,
                cancelable: true,
            }));
        }
        """,
        {"shift": shift, "ctrl": ctrl, "meta": meta, "code": code, "location": location},
    )


def _send_count(page) -> int:
    return page.evaluate("__sendCount")


# ─── Static guard ─────────────────────────────────────────────────────────


def test_no_inline_interceptor_remains():
    """The stale-snapshot interceptor must be gone from index.html."""
    assert "stopImmediatePropagation" not in INDEX_HTML, (
        "index.html still contains a capture-phase interceptor with "
        "stopImmediatePropagation — this reintroduces the stale _sendKey bug"
    )


# ─── Behavioural tests ────────────────────────────────────────────────────


def test_enter_mode_plain_enter_sends(page):
    _set_send_key(page, "enter")
    _press_enter(page)
    assert _send_count(page) == 1


def test_enter_mode_shift_enter_does_not_send(page):
    _set_send_key(page, "enter")
    _press_enter(page, shift=True)
    assert _send_count(page) == 0


def test_shift_enter_mode_plain_enter_does_not_send(page):
    _set_send_key(page, "shift+enter")
    _press_enter(page)
    assert _send_count(page) == 0


def test_shift_enter_mode_shift_enter_sends(page):
    _set_send_key(page, "shift+enter")
    _press_enter(page, shift=True)
    assert _send_count(page) == 1


def test_ctrl_enter_mode_plain_enter_does_not_send(page):
    _set_send_key(page, "ctrl+enter")
    _press_enter(page)
    assert _send_count(page) == 0


def test_ctrl_enter_mode_ctrl_enter_sends(page):
    _set_send_key(page, "ctrl+enter")
    _press_enter(page, ctrl=True)
    assert _send_count(page) == 1


def test_ctrl_enter_mode_cmd_enter_sends(page):
    _set_send_key(page, "ctrl+enter")
    _press_enter(page, meta=True)
    assert _send_count(page) == 1


# ─── Core regression: live preference change ─────────────────────────────


def test_preference_change_enter_to_shift_enter_without_reload(page):
    """The bug: interceptor snapshotted 'enter' at load time. Changing to
    'shift+enter' at runtime didn't update the closure, so Shift+Enter was
    still blocked. After the fix, the live window._sendKey is read on every
    event, so the transition takes effect immediately."""
    # Start in 'enter' mode — plain Enter sends.
    _set_send_key(page, "enter")
    _press_enter(page)
    assert _send_count(page) == 1

    # Switch to 'shift+enter' without reloading.
    _set_send_key(page, "shift+enter")

    # Plain Enter should no longer send.
    _press_enter(page)
    assert _send_count(page) == 1  # unchanged

    # Shift+Enter should now send.
    _press_enter(page, shift=True)
    assert _send_count(page) == 2


def test_preference_change_shift_enter_to_enter_without_reload(page):
    """Reverse transition: 'shift+enter' → 'enter'."""
    _set_send_key(page, "shift+enter")
    _press_enter(page)  # no send
    _press_enter(page, shift=True)  # sends
    assert _send_count(page) == 1

    _set_send_key(page, "enter")
    _press_enter(page)  # now sends
    assert _send_count(page) == 2

    _press_enter(page, shift=True)  # no longer sends
    assert _send_count(page) == 2


def test_preference_change_enter_to_ctrl_enter_without_reload(page):
    """Transition: 'enter' → 'ctrl+enter'."""
    _set_send_key(page, "enter")
    _press_enter(page)  # sends
    assert _send_count(page) == 1

    _set_send_key(page, "ctrl+enter")
    _press_enter(page)  # no longer sends
    assert _send_count(page) == 1

    _press_enter(page, ctrl=True)  # sends
    assert _send_count(page) == 2
