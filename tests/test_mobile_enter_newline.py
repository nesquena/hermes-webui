"""Regression: mobile soft-keyboard Enter must insert a newline, not send.

This module covers the three production changes in this PR, each with a test
that provably fails when only that change is reverted:

A. ``_isTouchOnlyDevice()`` replaces media-query-only touch detection.
   Several iOS Safari builds report ``(any-pointer:fine)`` as **true** on a
   plain iPhone (Apple Pencil / pointer-emulation heuristics), so the upstream
   ``matchMedia('(pointer:coarse)') && !_hasFinePointerCoexisting()`` test
   evaluates false on exactly the devices it is meant to detect, and plain
   Enter sends instead of inserting a newline. ``_isTouchOnlyDevice()`` checks
   the phone/tablet UA **first** and never lets a fine-pointer signal veto it.

B. ``window._sendKey`` is initialised synchronously from ``localStorage``
   before the composer handler is registered. Without it, on a slow network the
   handler runs while ``window._sendKey`` is still undefined, so a user who
   configured ``shift+enter`` has plain Enter send the message.

C. The composer textarea carries ``enterkeyhint="enter"`` so the iOS software
   keyboard renders a return key rather than a "Go"/"Send" key.

The behavioural tests execute the **real** helpers and composer handler
extracted verbatim from ``static/boot.js`` — reverting the production code
changes what these tests run.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - playwright optional in some envs
    sync_playwright = None

REPO = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)


def _require_playwright():
    if sync_playwright is None:
        pytest.skip("playwright is unavailable; run `playwright install chromium`")
    return sync_playwright


# ─── Source extraction ────────────────────────────────────────────────────


def _extract_function(name: str, source: str) -> str:
    """Extract a top-level ``function name(...){...}`` with brace matching."""
    start = source.index(f"function {name}(")
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
    raise AssertionError(f"could not extract function {name}")


def _extract_composer_handler() -> str:
    """Extract the real ``$('msg').addEventListener('keydown', …)`` call."""
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
                end = BOOT_JS.index(");", idx) + 2
                return BOOT_JS[start:end]
    raise AssertionError("could not extract composer keydown listener")


def _extract_send_key_init() -> str:
    """Extract the synchronous ``window._sendKey`` bootstrap from boot.js.

    Returns an empty string when the line is absent, so reverting change B
    removes it from the harness too (which is what makes the test bite).
    """
    match = re.search(
        r"^try\{\s*window\._sendKey=localStorage\.getItem\([^\n]*$",
        BOOT_JS,
        re.MULTILINE,
    )
    return match.group(0) if match else ""


def _available_helpers() -> str:
    """Extract the touch/IME/numpad helpers the composer handler may call.

    ``_isTouchOnlyDevice`` is this PR's addition and ``_hasFinePointerCoexisting``
    is the upstream helper; include whichever exist so the harness stays valid
    with the production change reverted.
    """
    names = (
        "_isImeEnter",
        "_isNumpadEnter",
        "_hasFinePointerCoexisting",
        "_isTouchOnlyDevice",
    )
    out = []
    for name in names:
        if f"function {name}(" in BOOT_JS:
            out.append(_extract_function(name, BOOT_JS))
    return "\n".join(out)


HARNESS_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"></head>
<body>
<textarea id="msg" rows="1"></textarea>
<script>
var _imeComposing = false;
function $(id) { return document.getElementById(id); }

// --- Real helpers extracted from boot.js ---
__HELPERS__

// --- Real synchronous _sendKey bootstrap extracted from boot.js ---
// Empty when that line has been removed from production code.
__SENDKEY_INIT__

window.__sendCount = 0;
window.__defaultPrevented = 0;
window.send = function() { window.__sendCount++; };
var send = window.send;

// --- Real composer handler extracted from boot.js ---
__HANDLER__

// Observe whether the handler suppressed the browser's default newline.
document.getElementById('msg').addEventListener('keydown', function(e){
  if (e.defaultPrevented) window.__defaultPrevented++;
});
</script>
</body></html>
"""


def _build_harness_html() -> str:
    return (
        HARNESS_HTML.replace("__HELPERS__", _available_helpers())
        .replace("__SENDKEY_INIT__", _extract_send_key_init())
        .replace("__HANDLER__", _extract_composer_handler())
    )


def _write_harness() -> Path:
    tmp = Path(tempfile.mkstemp(suffix=".html")[1])
    tmp.write_text(_build_harness_html(), encoding="utf-8")
    return tmp


# ─── Media-query shim ─────────────────────────────────────────────────────

# Reproduces the iOS Safari quirk: a touch-primary phone that ALSO reports
# (any-pointer:fine) as true. Installed before any page script runs.
IOS_SAFARI_MATCHMEDIA_QUIRK = """
(() => {
  const real = window.matchMedia.bind(window);
  window.matchMedia = (q) => {
    if (q === '(pointer:coarse)') return {matches: true, media: q};
    if (q === '(any-pointer:fine)') return {matches: true, media: q};
    return real(q);
  };
})();
"""

# A plain desktop: no coarse pointer, fine pointer present.
DESKTOP_MATCHMEDIA = """
(() => {
  const real = window.matchMedia.bind(window);
  window.matchMedia = (q) => {
    if (q === '(pointer:coarse)') return {matches: false, media: q};
    if (q === '(any-pointer:fine)') return {matches: true, media: q};
    return real(q);
  };
})();
"""


def _press_enter(page, *, shift=False, ctrl=False, meta=False, numpad=False):
    page.evaluate(
        """
        ({shift, ctrl, meta, code, location}) => {
            const el = document.getElementById('msg');
            el.focus();
            el.dispatchEvent(new KeyboardEvent('keydown', {
                key: 'Enter', code, keyCode: 13, which: 13, location,
                shiftKey: shift, ctrlKey: ctrl, metaKey: meta,
                bubbles: true, cancelable: true,
            }));
        }
        """,
        {
            "shift": shift,
            "ctrl": ctrl,
            "meta": meta,
            "code": "NumpadEnter" if numpad else "Enter",
            "location": 3 if numpad else 0,
        },
    )


def _send_count(page) -> int:
    return page.evaluate("__sendCount")


# ─── Change A: UA-first touch detection ───────────────────────────────────


def test_iphone_reporting_fine_pointer_still_treats_enter_as_newline():
    """Change A. iPhone UA + ``(any-pointer:fine)`` true → Enter must NOT send.

    Reverting to ``matchMedia('(pointer:coarse)') && !_hasFinePointerCoexisting()``
    makes ``_mobileDefault`` false here, so plain Enter sends and this fails.
    """
    playwright = _require_playwright()
    harness = _write_harness()
    with playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            user_agent=IPHONE_UA,
            viewport={"width": 390, "height": 844},
            has_touch=True,
            is_mobile=True,
        )
        ctx.add_init_script(IOS_SAFARI_MATCHMEDIA_QUIRK)
        page = ctx.new_page()
        page.goto(harness.as_uri())
        page.wait_for_load_state("domcontentloaded")

        # Sanity: the quirk is actually in place, otherwise the test is vacuous.
        assert page.evaluate("matchMedia('(pointer:coarse)').matches") is True
        assert page.evaluate("matchMedia('(any-pointer:fine)').matches") is True

        _press_enter(page)
        sends = _send_count(page)
        prevented = page.evaluate("__defaultPrevented")
        browser.close()

    assert sends == 0, (
        "plain Enter sent the message on an iPhone that reports (any-pointer:fine); "
        "the soft keyboard's return key must insert a newline"
    )
    assert prevented == 0, "Enter should fall through so the browser inserts the newline"


def test_android_phone_treats_enter_as_newline():
    """Change A. Android UA → Enter must NOT send, regardless of media queries."""
    playwright = _require_playwright()
    harness = _write_harness()
    android_ua = (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
    )
    with playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            user_agent=android_ua,
            viewport={"width": 412, "height": 915},
            has_touch=True,
            is_mobile=True,
        )
        ctx.add_init_script(IOS_SAFARI_MATCHMEDIA_QUIRK)
        page = ctx.new_page()
        page.goto(harness.as_uri())
        page.wait_for_load_state("domcontentloaded")
        _press_enter(page)
        sends = _send_count(page)
        browser.close()
    assert sends == 0, "plain Enter must insert a newline on an Android phone"


def test_mobile_ctrl_enter_still_sends():
    """Change A must not cost mobile users a way to send from the keyboard."""
    playwright = _require_playwright()
    harness = _write_harness()
    with playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            user_agent=IPHONE_UA,
            viewport={"width": 390, "height": 844},
            has_touch=True,
            is_mobile=True,
        )
        ctx.add_init_script(IOS_SAFARI_MATCHMEDIA_QUIRK)
        page = ctx.new_page()
        page.goto(harness.as_uri())
        page.wait_for_load_state("domcontentloaded")

        _press_enter(page, ctrl=True)
        ctrl_sends = _send_count(page)
        _press_enter(page, numpad=True)
        total_sends = _send_count(page)
        browser.close()

    assert ctrl_sends == 1, "Ctrl+Enter must still send on mobile"
    assert total_sends == 2, "Numpad Enter must still send on mobile"


def test_desktop_enter_still_sends():
    """Change A must not alter desktop behaviour."""
    playwright = _require_playwright()
    harness = _write_harness()
    with playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context()
        ctx.add_init_script(DESKTOP_MATCHMEDIA)
        page = ctx.new_page()
        page.goto(harness.as_uri())
        page.wait_for_load_state("domcontentloaded")
        _press_enter(page)
        sends = _send_count(page)
        browser.close()
    assert sends == 1, "plain Enter must still send on a desktop browser"


# ─── Change B: synchronous _sendKey bootstrap ─────────────────────────────


def test_stored_shift_enter_preference_applies_before_settings_fetch():
    """Change B. A stored ``shift+enter`` must hold on the very first keypress.

    Reverting the synchronous ``window._sendKey`` bootstrap leaves the value
    undefined while the async ``/api/settings`` fetch is still in flight, so the
    handler falls into the default branch and plain Enter sends.
    """
    playwright = _require_playwright()
    harness = _write_harness()
    with playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context()
        ctx.add_init_script(DESKTOP_MATCHMEDIA)
        # Seed the preference the way a returning user's browser would have it,
        # before any page script runs.
        ctx.add_init_script(
            "try{localStorage.setItem('hermes-pref-send_key','shift+enter');}catch(e){}"
        )
        page = ctx.new_page()
        page.goto(harness.as_uri())
        page.wait_for_load_state("domcontentloaded")

        assert (
            page.evaluate("localStorage.getItem('hermes-pref-send_key')")
            == "shift+enter"
        ), "harness failed to seed the preference; test would be vacuous"

        _press_enter(page)
        plain_sends = _send_count(page)
        _press_enter(page, shift=True)
        after_shift = _send_count(page)
        browser.close()

    assert plain_sends == 0, (
        "plain Enter sent the message even though the stored preference is "
        "shift+enter; window._sendKey was not initialised before the handler ran"
    )
    assert after_shift == 1, "Shift+Enter must send when the preference is shift+enter"


def test_send_key_bootstrap_precedes_composer_handler():
    """Change B, structural. The bootstrap must sit ABOVE the handler in boot.js.

    Ordering is what closes the race; a bootstrap placed after the
    ``addEventListener`` call would still leave the first keypress undefined.
    """
    init = _extract_send_key_init()
    assert init, "synchronous window._sendKey bootstrap is missing from boot.js"
    handler_idx = BOOT_JS.index("$('msg').addEventListener('keydown',e=>")
    assert BOOT_JS.index(init) < handler_idx, (
        "window._sendKey bootstrap must appear before the composer keydown "
        "listener is registered"
    )


# ─── Change C: enterkeyhint on the composer ───────────────────────────────


def test_composer_textarea_declares_enterkeyhint():
    """Change C, structural. iOS renders the return key from ``enterkeyhint``.

    The rendered keycap is a native-keyboard behaviour with no DOM-observable
    effect in a headless browser, so this asserts the attribute is present and
    parsed rather than claiming to verify the keyboard itself.
    """
    match = re.search(r"<textarea id=\"msg\"[^>]*>", INDEX_HTML)
    assert match, "composer textarea not found in index.html"
    assert 'enterkeyhint="enter"' in match.group(0), (
        "composer textarea must declare enterkeyhint=\"enter\" so the iOS "
        "software keyboard shows a return key instead of Go/Send"
    )


def test_enterkeyhint_reaches_the_dom():
    """Change C. The attribute must survive parsing as the DOM property."""
    playwright = _require_playwright()
    match = re.search(r"<textarea id=\"msg\"[^>]*>", INDEX_HTML)
    assert match, "composer textarea not found in index.html"
    snippet = (
        "<!doctype html><html><head><meta charset='utf-8'></head><body>"
        + match.group(0)
        + "</textarea></body></html>"
    )
    tmp = Path(tempfile.mkstemp(suffix=".html")[1])
    tmp.write_text(snippet, encoding="utf-8")
    with playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        page.goto(tmp.as_uri())
        hint = page.evaluate("document.getElementById('msg').enterKeyHint")
        browser.close()
    assert hint == "enter", f"enterKeyHint resolved to {hint!r}, expected 'enter'"
