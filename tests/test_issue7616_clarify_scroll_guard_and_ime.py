"""#7616 clarify field: a growing field must not yank a scrolled-away reader (#8304).

Two defects survived the resize lifecycle work pinned by
``test_issue7616_clarify_resize_lifecycle``:

1. **Resizing re-pinned the reader.** ``_syncClarifyTranscriptSpace`` samples
   ``wasNearBottom`` BEFORE the resize is applied, then acts on that stale
   answer from a frame callback and again from the 420 ms settle timer. A reader
   who scrolls up inside that window is still carrying the old "they were at the
   bottom" answer and gets snapped back — a 607 px gap collapsed to 0 in review's
   Chromium run, where master kept 600 px. Pasting while already scrolled up
   re-pinned immediately.

   The fix captures ``_messageScrollInputGeneration`` at schedule time and
   requires it unchanged at settle time, alongside the live
   ``_messageUserUnpinned`` flag. The reservation is still written either way —
   only the scroll is conditional, because skipping the reservation would
   reintroduce the stale-``--clarify-card-height`` bug the previous file pins.

2. **Enter during IME composition submitted.** The keydown handler checked only
   ``e.key``. Chromium's IMEs report ``key:"Process"``, but WebKit — including
   the macOS app's WKWebView — delivers ``key:"Enter"`` with
   ``isComposing:true``, so confirming a Japanese or Chinese word sent a
   half-typed answer.

These are event-driven tests against the PRODUCTION functions, extracted from
``static/messages.js`` by name. They fail on the reviewed head with a re-pin
count of 1 and a submit count of 1 respectively.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS_PATH = ROOT / "static" / "messages.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required for the clarify harness"
)

EXTRACTED_FUNCTIONS = (
    "_syncClarifyTranscriptSpace",
    "_clarifyFieldIsHidden",
    "_autoResizeClarifyInput",
    "_bindClarifyInputAutoResize",
    "toggleClarifyCardCollapsed",
    "showClarifyCard",
)


def _extract_function(src: str, name: str, *, required: bool = True) -> str | None:
    marker = f"function {name}("
    start = src.find(marker)
    if start < 0:
        if required:
            raise AssertionError(f"{name}() not found in static/messages.js")
        return None
    depth = 0
    seen_brace = False
    for idx in range(start, len(src)):
        char = src[idx]
        if char == "{":
            depth += 1
            seen_brace = True
        elif char == "}":
            depth -= 1
            if seen_brace and depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name}() has unbalanced braces")


def _run_node(program: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise AssertionError(f"node harness failed:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_scenario(scenario_js: str, *, globals_js: str = "") -> dict:
    src = MESSAGES_JS_PATH.read_text(encoding="utf-8")
    constants = "\n".join(
        line for line in src.splitlines() if line.startswith("const CLARIFY_INPUT_")
    )
    functions = "\n\n".join(
        body
        for body in (
            _extract_function(src, name, required=(name != "_clarifyFieldIsHidden"))
            for name in EXTRACTED_FUNCTIONS
        )
        if body is not None
    )
    hidden = _extract_function(src, "_clarifyFieldIsHidden", required=False) or (
        "function _clarifyFieldIsHidden(input) { return false; }"
    )
    return _run_node(
        f"""
{constants}

{functions}

{hidden}

{globals_js}

// ── minimal DOM ──────────────────────────────────────────────────────────────
const TIMERS = [];
const FRAMES = [];
let SCROLL_FOLLOW_CALLS = 0;
let NEAR_BOTTOM = true;
let RESERVATION_WRITES = 0;

// Scroll-input generation + unpinned flag. Declared exactly as ui.js declares
// them so the production helper reads the real globals, and mutated by the
// scenario to simulate the reader scrolling up after the resize was scheduled.
let _messageScrollInputGeneration = 0;
let _messageUserUnpinned = false;

function setTimeoutStub(fn) {{ TIMERS.push(fn); return TIMERS.length; }}
function requestAnimationFrameStub(fn) {{ FRAMES.push(fn); return FRAMES.length; }}
function clearTimeoutStub() {{}}
function runTimers() {{ while (TIMERS.length) TIMERS.shift()(); }}
function runFrames() {{ while (FRAMES.length) FRAMES.shift()(); }}
globalThis.setTimeout = setTimeoutStub;
globalThis.requestAnimationFrame = requestAnimationFrameStub;
globalThis.clearTimeout = clearTimeoutStub;

function _clarifyMessagesNearBottom() {{ return NEAR_BOTTOM; }}
function scrollToBottom() {{ SCROLL_FOLLOW_CALLS++; }}
function _syncClarifyCollapseButton() {{}}
function lockComposerForClarify() {{}}
function unlockComposerForClarify() {{}}
function _setPromptFlyoutHidden() {{}}
function _clarifyMessagesScrollTop() {{ return 0; }}

function makeClassList(el) {{
  const set = new Set();
  return {{
    add: (...c) => c.forEach((x) => set.add(x)),
    remove: (...c) => c.forEach((x) => set.delete(x)),
    contains: (c) => set.has(c),
    toggle: (c, force) => {{
      const on = force === undefined ? !set.has(c) : !!force;
      if (on) set.add(c); else set.delete(c);
      return on;
    }},
  }};
}}

const styles = {{}};
const reservationProps = {{}};
const messages = {{
  classList: makeClassList(),
  style: {{
    setProperty(k, v) {{ reservationProps[k] = v; RESERVATION_WRITES++; }},
    removeProperty(k) {{ delete reservationProps[k]; }},
    getPropertyValue(k) {{ return reservationProps[k] || ""; }},
  }},
  querySelector() {{ return null; }},
}};

const input = {{
  value: "answer",
  style: {{}},
  dataset: {{}},
  scrollHeight: 600,
  disabled: false,
  classList: makeClassList(),
  getClientRects() {{ return [{{}}]; }},
  addEventListener() {{}},
  set onkeydown(fn) {{ this._onkeydown = fn; }},
  get onkeydown() {{ return this._onkeydown; }},
}};

const card = {{
  classList: makeClassList(),
  style: {{}},
  querySelector() {{ return null; }},
  getBoundingClientRect() {{ return {{ height: 200 }}; }},
}};
card.classList.add("visible");

const ELEMENTS = {{
  messages, clarifyCard: card, clarifyInput: input, clarifySubmit: null,
  clarifyQuestion: null, clarifyChoices: null, clarifyHint: null,
  clarifyCollapseBtn: null,
}};
function $(id) {{ return ELEMENTS[id] || null; }}
globalThis.$ = $;

// ── scenario ─────────────────────────────────────────────────────────────────
{scenario_js}

// ── report ───────────────────────────────────────────────────────────────────
console.log(JSON.stringify({{
  scrolls: SCROLL_FOLLOW_CALLS,
  reservations: RESERVATION_WRITES,
  renderedHeight: input.style.height,
}}));
"""
    )


def test_reader_who_scrolls_up_is_not_snapped_back():
    """A reader who scrolls away during the settle window keeps their position."""
    result = _run_scenario(
        """
_syncClarifyTranscriptSpace(card, {immediate: false});
// The reader scrolls up AFTER the resize was scheduled but before it settles.
_messageScrollInputGeneration++;
_messageUserUnpinned = true;
runFrames();
runTimers();
"""
    )
    assert result["scrolls"] == 0, (
        f"the reader scrolled up after the field grew, so scrollToBottom must "
        f"not run; it ran {result['scrolls']} time(s) and yanked them back to "
        f"the bottom"
    )
    # The reservation is still updated — skipping it would reintroduce the
    # stale --clarify-card-height bug pinned by the resize-lifecycle file.
    assert result["reservations"] >= 1, (
        "the transcript reservation must still be written when the scroll is "
        "suppressed; only the scroll is conditional"
    )


def test_reader_who_stays_at_the_bottom_is_still_followed():
    """The original behaviour is preserved for a reader who does not move."""
    result = _run_scenario(
        """
_syncClarifyTranscriptSpace(card, {immediate: false});
runFrames();
runTimers();
"""
    )
    assert result["scrolls"] >= 1, (
        "a reader who was at the bottom and stayed there must still be followed "
        f"to the new bottom; scrolls={result['scrolls']}"
    )


def test_scroll_input_alone_suppresses_the_repin():
    """A moved scroll-input generation suppresses the scroll even if unpinned is false."""
    result = _run_scenario(
        """
_syncClarifyTranscriptSpace(card, {immediate: false});
_messageScrollInputGeneration++;   // reader moved, but the unpinned flag lagged
runFrames();
runTimers();
"""
    )
    assert result["scrolls"] == 0, (
        f"a changed scroll-input generation must suppress the scroll; it ran "
        f"{result['scrolls']} time(s)"
    )


def _extract_clarify_keydown_handler(src: str) -> str:
    """Return the PRODUCTION ``input.onkeydown = (e) => {...}`` arrow function.

    Extracted from ``showClarifyCard``'s body rather than re-typed, so the test
    exercises the shipped handler and cannot drift from it.
    """
    marker = "input.onkeydown = "
    start = src.find(marker)
    if start < 0:
        raise AssertionError("input.onkeydown assignment not found in static/messages.js")
    i = src.index("(", start + len(marker))
    depth = 0
    seen = False
    for j in range(i, len(src)):
        if src[j] == "(":
            depth += 1
            seen = True
        elif src[j] == ")":
            depth -= 1
            if seen and depth == 0:
                paren_end = j
                break
    else:
        raise AssertionError("unbalanced parameter list in the clarify keydown handler")

    body_start = src.index("{", paren_end)
    depth = 0
    for j in range(body_start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise AssertionError("unbalanced braces in the clarify keydown handler")


def _ime_scenario_result() -> dict:
    """Run the PRODUCTION keydown handler against synthetic IME/Enter events."""
    src = MESSAGES_JS_PATH.read_text(encoding="utf-8")
    handler = _extract_clarify_keydown_handler(src)
    program = f"""
const HANDLER = {handler};

let SUBMITS = 0;
function respondClarify() {{ SUBMITS++; }}
function _autoResizeClarifyInput() {{}}

const after = {{}};
const noop = () => {{}};
const event = (over) => Object.assign({{
  key: "Enter", shiftKey: false, isComposing: false, keyCode: 13, preventDefault: noop,
}}, over);

// WebKit (including the macOS app's WKWebView) delivers key:"Enter" with
// isComposing:true when an IME confirms a word.
HANDLER(event({{ isComposing: true }}));
after.composing = SUBMITS;
HANDLER(event({{ keyCode: 229 }}));
after.keyCode229 = SUBMITS;
HANDLER(event({{}}));
after.realEnter = SUBMITS;
HANDLER(event({{ shiftKey: true }}));
after.shiftEnter = SUBMITS;
console.log(JSON.stringify(after));
"""
    proc = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise AssertionError(f"node harness failed:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_ime_composition_enter_does_not_submit():
    """The production keydown handler must ignore a composition-confirming Enter."""
    after = _ime_scenario_result()

    assert after["composing"] == 0, (
        "WebKit delivers key:'Enter' with isComposing:true when an IME confirms "
        f"a word; the handler submitted {after['composing']} time(s), so a "
        "Japanese or Chinese user sends a half-typed answer"
    )
    assert after["keyCode229"] == 0, (
        f"keyCode 229 is the legacy composition signal; the handler submitted "
        f"{after['keyCode229']} time(s)"
    )
    assert after["realEnter"] == 1, (
        "a genuine Enter must still submit; the IME guard must not disable send"
    )
    assert after["shiftEnter"] == 1, "Shift+Enter must stay the newline path, not a send"