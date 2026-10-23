"""#7616 clarify field: resize must drive the transcript reservation (#8304 gate).

The free-text answer field auto-grows with its content (``#7616``). Two
resize-lifecycle defects survived that change:

1. **Typing did not update the reservation.** ``_bindClarifyInputAutoResize()``
   binds ``_autoResizeClarifyInput`` to the ``input`` event, but the resize
   helper changed the textarea height without calling
   ``_syncClarifyTranscriptSpace()``. Once the initial show timers had settled,
   the absolute-positioned card could grow while ``--clarify-card-height`` kept
   its old value, until an unrelated poll or window resize repaired it.

2. **Collapse → same-prompt poll → expand lost the grown height.** A collapsed
   card hides ``.clarify-response`` with ``display:none``
   (``style.css .clarify-card.collapsed .clarify-response``), so the textarea has
   no layout box and ``scrollHeight`` reads 0. ``showClarifyCard()`` still calls
   ``_autoResizeClarifyInput()`` on every poll, which wrote the 42px floor from
   that zero measurement while preserving the draft. Expanding through
   ``toggleClarifyCardCollapsed()`` updated transcript space but never
   remeasured, so a long answer came back one line tall.

These are event-driven tests, not source-sniffing: the real production
functions are extracted from ``static/messages.js`` by name and driven against a
minimal DOM whose layout box disappears exactly the way ``display:none`` does
(``getClientRects()`` empty, ``scrollHeight`` 0). Scroll-follow is recorded so
the fixes cannot be made by abandoning the reader's position.
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
    shutil.which("node") is None, reason="node is required for the clarify resize harness"
)

# The production functions under test, plus the constants they close over.
EXTRACTED_FUNCTIONS = (
    "_syncClarifyTranscriptSpace",
    "_clarifyFieldIsHidden",
    "_autoResizeClarifyInput",
    "_bindClarifyInputAutoResize",
    "toggleClarifyCardCollapsed",
    "_ensureClarifyResizeListener",
)


def _extract_function(src: str, name: str, *, required: bool = True) -> str | None:
    """Return one top-level ``function name(...) { ... }`` definition verbatim.

    ``required=False`` lets a helper introduced by the fix be absent (pre-fix
    heads), returning None so the harness still runs the PRE-fix production code
    and fails on real behavior rather than on a missing symbol.
    """
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


def _harness_source(scenario_js: str, *, extra_functions: tuple = ()) -> str:
    """Build the Node program: real functions + minimal DOM + one scenario."""
    src = MESSAGES_JS_PATH.read_text(encoding="utf-8")
    constants = "\n".join(
        line
        for line in src.splitlines()
        if line.startswith("const CLARIFY_INPUT_")
    )
    functions = "\n\n".join(
        body
        for body in (
            _extract_function(src, name, required=(name != "_clarifyFieldIsHidden"))
            for name in (*EXTRACTED_FUNCTIONS, *extra_functions)
        )
        if body is not None
    )
    # On a pre-fix head the helper does not exist. Pre-fix production code
    # measured unconditionally, so stand in with "never hidden" — the harness
    # then exercises the real pre-fix resize path and the tests fail on observed
    # behavior (stale reservation, floor written from a hidden field) rather
    # than on a missing symbol.
    hidden_helper = _extract_function(src, "_clarifyFieldIsHidden", required=False)
    hidden_fallback = (
        hidden_helper
        or "function _clarifyFieldIsHidden(input) { return false; }  // pre-fix stand-in"
    )
    return f"""
{constants}

{functions}

{hidden_fallback}

// ── minimal DOM ──────────────────────────────────────────────────────────────
const TIMERS = [];
const FRAMES = [];
let SCROLL_FOLLOW_CALLS = 0;
// Session switch, as opposed to collapse: the flyout is display:none, so the
// field has no layout box. Distinct from the collapsed class, which only hides
// the answer row.
let HIDDEN = false;

function setTimeoutStub(fn) {{ TIMERS.push(fn); return TIMERS.length; }}
function requestAnimationFrameStub(fn) {{ FRAMES.push(fn); return FRAMES.length; }}
function clearTimeoutStub() {{}}
function runTimers() {{ while (TIMERS.length) TIMERS.shift()(); }}
function runFrames() {{ while (FRAMES.length) FRAMES.shift()(); }}
// The production helpers schedule their deferred re-measure through the global
// `setTimeout` / `requestAnimationFrame` names. Point those at the queues above
// so `settle()` actually drains them: with Node's real timers the callbacks fire
// long after the JSON below has been written, and the assertions would only ever
// read the immediate measurement — the delayed ones were never checked at all.
globalThis.setTimeout = setTimeoutStub;
globalThis.requestAnimationFrame = requestAnimationFrameStub;
globalThis.clearTimeout = clearTimeoutStub;

// Stubs for collaborators outside the extracted set.
function _clarifyMessagesNearBottom() {{ return true; }}
function scrollToBottom() {{ SCROLL_FOLLOW_CALLS++; }}
function _syncClarifyCollapseButton() {{}}

// `_ensureClarifyResizeListener` registers on `window`; give it one and let the
// scenario fire the event the way the browser would.
const WINDOW_HANDLERS = {{}};
globalThis.window = {{
  addEventListener(type, fn) {{
    (WINDOW_HANDLERS[type] = WINDOW_HANDLERS[type] || []).push(fn);
  }},
}};
function fireResize() {{ (WINDOW_HANDLERS["resize"] || []).forEach((fn) => fn()); }}
// Module-level flag the production listener guards on (declared at messages.js
// top level, outside the extracted function body).
let _clarifyResizeListenerReady = false;

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

const messagesStyleProps = {{}};
const messages = {{
  classList: makeClassList(),
  style: {{
    setProperty: (k, v) => {{ messagesStyleProps[k] = v; }},
    removeProperty: (k) => {{ delete messagesStyleProps[k]; }},
  }},
}};

// #7616 finding (window resize / phone rotate): a narrower column makes each
// logical line wrap onto more rendered rows. Model that with a NARROW switch so
// a resize event can change the content height the way a real reflow would.
let NARROW = false;

const input = {{
  value: "",
  disabled: false,
  dataset: {{}},
  style: {{ height: "", overflowY: "" }},
  _listeners: {{}},
  addEventListener(type, fn) {{ this._listeners[type] = fn; }},
  dispatch(type) {{ if (this._listeners[type]) this._listeners[type](); }},
  removeAttribute() {{}},
  focus() {{}},
  // A laid-out textarea reports its content height; a display:none one reports 0.
  get scrollHeight() {{
    if (this.getClientRects().length === 0) return 0;
    const rows = String(this.value).split("\\n").reduce(
      (n, line) => n + (NARROW ? Math.max(1, Math.ceil(line.length / 45)) : 1), 0);
    return Math.max(1, rows) * 20;
  }},
  getClientRects() {{
    return (HIDDEN || card.classList.contains("collapsed")) ? [] : [{{}}];
  }},
}};

function currentInputHeight() {{
  const parsed = parseFloat(input.style.height);
  return Number.isFinite(parsed) ? parsed : 42;
}}

const inner = {{ getBoundingClientRect: () => ({{ height: 100 + currentInputHeight() }}) }};
const card = {{
  classList: makeClassList(),
  querySelector(sel) {{ return sel === ".clarify-inner" ? inner : null; }},
  getBoundingClientRect: () => ({{ height: 56 }}),
}};

const REGISTRY = {{
  messages,
  clarifyInput: input,
  clarifyCard: card,
  clarifyQuestion: {{ textContent: "" }},
  clarifyChoices: {{ innerHTML: "", style: {{}} }},
  msg: null,
}};
function $(id) {{ return REGISTRY[id] || null; }}
globalThis.$ = $;
globalThis.document = {{
  activeElement: null,
  createElement: () => ({{
    style: {{}}, dataset: {{}}, classList: makeClassList(),
    appendChild() {{}}, setAttribute() {{}}, focus() {{}},
  }}),
}};

// Collaborators showClarifyCard() reaches for. The flyout toggle is what makes
// the layout box come back, so it flips HIDDEN rather than being inert.
function _setPromptFlyoutHidden(_card, hidden) {{ HIDDEN = !!hidden; }}
function _rememberClarifyPending() {{ return "sid"; }}
function _clarifyPromptBelongsToActiveSession() {{ return true; }}
function _ensureClarifyCardDom() {{ return card; }}
function _startClarifyCountdown() {{}}
function _clearClarifyCountdownTimer() {{}}
function _clearClarifyHideTimer() {{}}
function _clarifySetControlsDisabled() {{}}
const S = {{ session: {{ session_id: "sess" }} }};
let _clarifySignature = null, _clarifySessionId = null, _clarifyId = null, _clarifyVisibleSince = 0;

// ── scenario ─────────────────────────────────────────────────────────────────
const R = {{}};
function record(key, value) {{ R[key] = value; }}
// Accessors are plain functions so a scenario can call them directly.
function reservation() {{ return messagesStyleProps["--clarify-card-height"] || null; }}
function dockHeight() {{ return messagesStyleProps["--clarify-dock-height"] || null; }}
function height() {{ return input.style.height; }}
function overflow() {{ return input.style.overflowY; }}
function value() {{ return input.value; }}
function hidden() {{ return input.getClientRects().length === 0; }}
function remeasureFlag() {{ return input.dataset.clarifyRemeasure || null; }}
function bound() {{ return input.style.height; }}
function scrollFollow() {{ return SCROLL_FOLLOW_CALLS; }}
function type(text) {{ input.value = text; input.dispatch("input"); }}
function settle() {{ runFrames(); runTimers(); }}
function setHidden(hidden) {{ HIDDEN = !!hidden; }}
function setNarrow(narrow) {{ NARROW = !!narrow; }}

{scenario_js}

process.stdout.write(JSON.stringify({{
  result: Object.fromEntries(Object.entries(R).map(([k, v]) => [k, typeof v === "function" ? v() : v])),
  scrollFollow: SCROLL_FOLLOW_CALLS,
}}));
"""


def _run(scenario_js: str, *, extra_functions: tuple = ()) -> dict:
    result = subprocess.run(
        ["node", "-e", _harness_source(scenario_js, extra_functions=extra_functions)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )
    assert result.returncode == 0, f"harness failed:\n{result.stderr}"
    payload = json.loads(result.stdout)
    payload["scrollFollow"] = int(payload["scrollFollow"])
    return payload


# ── 1. growth and shrink after the show timers have settled ──────────────────


def test_typing_resynchronizes_the_transcript_reservation():
    """Growth after the show timers must move `--clarify-card-height` with it."""
    out = _run(
        """
        card.classList.add("visible");
        _bindClarifyInputAutoResize();
        _autoResizeClarifyInput();
        _syncClarifyTranscriptSpace(card, {immediate: true});
        settle();
        record("reservationBefore", reservation());

        // Type a long answer AFTER the show timers have all settled.
        type("line one\\nline two\\nline three\\nline four");
        settle();
        record("reservationAfterGrowth", reservation());
        record("heightAfterGrowth", height());
        """
    )
    # 4 lines * 20px + 2px border = 82px (clamped at the 42px floor for 1-2 lines).
    assert out["result"]["heightAfterGrowth"] == "82px", out["result"]
    before = out["result"]["reservationBefore"]
    after = out["result"]["reservationAfterGrowth"]
    assert after is not None, "typing left --clarify-card-height unset"
    assert after != before, (
        "typing changed the field height but the transcript reservation kept its "
        f"stale value ({before}) — card grows over the transcript"
    )
    # Reservation must equal the freshly measured card box, not a stale one.
    assert int(after.replace("px", "")) == 100 + 82 + 24, out["result"]
    assert out["scrollFollow"] > 0, "scroll-follow behavior was dropped"


def test_shrinking_resynchronizes_the_transcript_reservation():
    """The shrink case must also re-reserve, not only growth."""
    out = _run(
        """
        card.classList.add("visible");
        _bindClarifyInputAutoResize();
        _autoResizeClarifyInput();
        type("a\\nb\\nc\\nd\\ne\\nf");
        settle();
        record("reservationTall", reservation());

        type("a");
        settle();
        record("reservationShort", reservation());
        record("heightShort", height());
        record("overflowShort", overflow());
        """
    )
    tall = out["result"]["reservationTall"]
    short = out["result"]["reservationShort"]
    assert int(short.replace("px", "")) < int(tall.replace("px", "")), (
        f"shrinking the field did not shrink the reservation ({tall} -> {short})"
    )
    assert out["result"]["heightShort"] == "42px", out["result"]
    assert out["result"]["overflowShort"] == "hidden", out["result"]


# ── 2. grow → collapse → same-prompt poll → expand ──────────────────────────


def test_collapse_poll_expand_retains_the_grown_height_and_draft():
    """A same-prompt poll while collapsed must not stomp the grown height."""
    out = _run(
        """
        card.classList.add("visible");
        _bindClarifyInputAutoResize();
        _autoResizeClarifyInput();
        type("long answer line\\nanother line\\nthird line\\nfourth line");
        settle();
        record("heightGrown", height());
        record("valueGrown", value());

        // Collapse, then the same-prompt poll re-enters showClarifyCard().
        toggleClarifyCardCollapsed(true);
        record("hiddenAfterCollapse", hidden());
        record("dockAfterCollapse", dockHeight());

        // showClarifyCard() polls while collapsed: the field has no layout box.
        _autoResizeClarifyInput();
        settle();
        record("heightAfterCollapsedPoll", height());
        record("valueAfterCollapsedPoll", value());

        // Expand again.
        toggleClarifyCardCollapsed(false);
        settle();
        record("heightAfterExpand", height());
        record("valueAfterExpand", value());
        record("reservationAfterExpand", reservation());
        record("overflowAfterExpand", overflow());
        record("boundAfterExpand", bound());
        """
    )
    res = out["result"]
    assert res["hiddenAfterCollapse"] is True, res
    # The collapsed poll must not have written the 42px floor.
    assert res["heightAfterCollapsedPoll"] == res["heightGrown"], (
        "a same-prompt poll while collapsed destroyed the grown height "
        f"({res['heightGrown']} -> {res['heightAfterCollapsedPoll']}): it measured "
        "a display:none field (scrollHeight 0) and wrote the minimum"
    )
    assert res["heightAfterExpand"] == res["heightGrown"], (
        f"expanding did not remeasure the field ({res['heightGrown']} -> "
        f"{res['heightAfterExpand']})"
    )
    # Draft text survives the whole round trip.
    assert res["valueAfterCollapsedPoll"] == res["valueGrown"], res
    assert res["valueAfterExpand"] == res["valueGrown"], res
    # Bounded height + correct overflow at the grown size.
    assert res["boundAfterExpand"] == res["heightGrown"], res
    assert res["overflowAfterExpand"] == "hidden", res
    assert res["reservationAfterExpand"] is not None, res


def test_expand_reserves_space_for_the_remeasured_height():
    """Expansion must reserve for the real height, not the collapsed dock."""
    out = _run(
        """
        card.classList.add("visible");
        _bindClarifyInputAutoResize();
        _autoResizeClarifyInput();
        type("l1\\nl2\\nl3\\nl4\\nl5");
        settle();
        toggleClarifyCardCollapsed(true);
        _autoResizeClarifyInput();
        toggleClarifyCardCollapsed(false);
        settle();
        record("reservation", reservation());
        record("height", height());
        """
    )
    res = out["result"]
    # 5 lines * 20px + 2px border = 102px.
    assert res["height"] == "102px", res
    assert int(res["reservation"].replace("px", "")) == 100 + 102 + 24, (
        f"expanded reservation {res['reservation']} does not match the remeasured "
        "field height"
    )


# ── 3. behavior the fix must not regress ─────────────────────────────────────


def test_hidden_field_measurement_is_skipped_and_deferred():
    """A hidden field is never measured; the deferred flag is set instead."""
    out = _run(
        """
        card.classList.add("visible");
        _bindClarifyInputAutoResize();
        _autoResizeClarifyInput();
        type("kept draft\\nsecond line");
        settle();
        record("heightBeforeCollapse", height());

        card.classList.toggle("collapsed", true);
        _autoResizeClarifyInput();
        record("heightWhileHidden", height());
        record("remeasureWhileHidden", remeasureFlag());
        """
    )
    res = out["result"]
    assert res["heightWhileHidden"] == res["heightBeforeCollapse"], (
        "measuring a display:none field wrote a floor and lost the height"
    )
    assert res["remeasureWhileHidden"] == "1", (
        "a skipped measurement must leave a deferred remeasure flag"
    )


def test_listener_dedup_and_new_prompt_reset_are_preserved():
    """The bound-once listener and the new-prompt floor reset must survive."""
    src = MESSAGES_JS_PATH.read_text(encoding="utf-8")
    bind = _extract_function(src, "_bindClarifyInputAutoResize")
    assert 'dataset.autoResizeBound === "1"' in bind, (
        "listener dedup via dataset.autoResizeBound was dropped"
    )
    out = _run(
        """
        card.classList.add("visible");
        _bindClarifyInputAutoResize();
        _bindClarifyInputAutoResize();
        _bindClarifyInputAutoResize();
        record("listenerCount", String(Object.keys(input._listeners).length));
        record("bound", input.dataset.autoResizeBound);

        type("one\\ntwo");
        settle();
        record("heightGrown", height());

        // The hide/reset path collapses the field for the NEXT prompt.
        input.value = "";
        _autoResizeClarifyInput();
        settle();
        record("heightReset", height());
        """
    )
    res = out["result"]
    assert res["bound"] == "1", res
    assert res["listenerCount"] == "1", (
        f"repeated binds stacked listeners: {res['listenerCount']}"
    )
    assert res["heightReset"] == "42px", (
        f"new-prompt reset must collapse the field to the floor, got {res['heightReset']}"
    )


# ── 4. a new prompt must not inherit the previous answer's height ────────────


def test_new_prompt_after_session_switch_is_remeasured_not_inherited():
    """The show path must measure once the flyout is back, not before.

    ``showClarifyCard()`` calls ``_autoResizeClarifyInput()`` *before*
    ``_setPromptFlyoutHidden(card, false)``, so on every show that call lands on
    a field with no layout box: the gate parks a ``clarifyRemeasure`` marker and
    returns without measuring. The layout box only returns a few lines later, by
    which point nothing measured again — so the marker was write-only and a fresh
    prompt kept the previous answer's tall empty field until some later resize or
    poll happened to repair it.
    """
    out = _run(
        """
        // Session A: a tall answer grows the field.
        setHidden(false);
        card.classList.add("visible");
        _bindClarifyInputAutoResize();
        type("l1\\nl2\\nl3\\nl4");
        settle();
        record("heightTall", height());
        record("reservationTall", reservation());

        // Session switch hides the flyout, then session B's prompt arrives.
        setHidden(true);
        card.classList.remove("visible");
        showClarifyCard({question: "next?", choices: [], clarify_id: "c2"});
        settle();
        record("heightAfterShow", height());
        record("markerAfterShow", remeasureFlag());
        record("reservationAfterShow", reservation());
        """,
        extra_functions=("showClarifyCard",),
    )
    res = out["result"]
    # 4 lines * 20px + 2px border.
    assert res["heightTall"] == "82px", res
    assert res["markerAfterShow"] is None, (
        f"showClarifyCard left a deferred remeasure parked ({res['markerAfterShow']}): "
        "nothing consumed the marker after the flyout was unhidden"
    )
    assert res["heightAfterShow"] == "42px", (
        f"the new prompt inherited the previous answer's height "
        f"(82px -> {res['heightAfterShow']}): the field was never remeasured after "
        "the card became visible"
    )
    # The reservation must follow the remeasured height, not the stale one.
    assert int(res["reservationAfterShow"].replace("px", "")) == 100 + 42 + 24, (
        f"reservation {res['reservationAfterShow']} was not resynced to the "
        f"remeasured 42px field (was {res['reservationTall']})"
    )


def test_show_without_a_pending_marker_does_not_resize_twice():
    """The drain must be a no-op when the field was already measurable."""
    out = _run(
        """
        setHidden(false);
        card.classList.add("visible");
        _bindClarifyInputAutoResize();
        showClarifyCard({question: "first?", choices: [], clarify_id: "c1"});
        settle();
        record("height", height());
        record("marker", remeasureFlag());
        """,
        extra_functions=("showClarifyCard",),
    )
    res = out["result"]
    assert res["height"] == "42px", res
    assert res["marker"] is None, res


# ── 5. a window resize / phone rotation must re-wrap and re-reserve ──────────


def test_window_resize_remeasures_the_answer_field():
    """Narrowing the window must re-wrap the draft and move the reservation.

    The window resize listener only re-reserved transcript space; it never
    re-measured the field, so after a resize or a phone rotation the textarea
    kept the height and ``overflowY`` computed for its old width. A draft that
    used to fit then wrapped past the field and stayed clipped until the user
    typed again or an unrelated poll happened to run.
    """
    out = _run(
        """
        card.classList.add("visible");
        _bindClarifyInputAutoResize();
        _ensureClarifyResizeListener();
        type("line one: a clarification answer that runs long enough to need more than one row\\n"
             + "line two: a clarification answer that runs long enough to need more than one row\\n"
             + "line three: a clarification answer that runs long enough to need more than one row\\n"
             + "line four: a clarification answer that runs long enough to need more than one row");
        settle();
        record("heightBefore", height());
        record("overflowBefore", overflow());
        record("reservationBefore", reservation());

        // The column narrows (window drag / phone rotation): every logical line
        // now wraps onto two rendered rows.
        setNarrow(true);
        fireResize();
        settle();
        record("heightAfter", height());
        record("overflowAfter", overflow());
        record("reservationAfter", reservation());
        """
    )
    res = out["result"]
    # 4 logical rows * 20px + 2px border, wide layout.
    assert res["heightBefore"] == "82px", res
    # 8 wrapped rows * 20px + 2 = 162 content, clamped at the 160px cap.
    assert res["heightAfter"] == "160px", (
        f"the window resized but the answer field kept its old height "
        f"({res['heightBefore']} -> {res['heightAfter']}): the resize listener "
        "re-reserved transcript space without re-measuring the field, so the "
        "re-wrapped draft stays clipped at the old width"
    )
    assert res["overflowAfter"] == "auto", (
        f"past the cap the field must scroll, got overflowY={res['overflowAfter']!r}"
    )
    assert res["reservationAfter"] is not None, (
        "resize left --clarify-card-height unset"
    )
    assert int(res["reservationAfter"].replace("px", "")) == 100 + 160 + 24, (
        f"the reservation ({res['reservationAfter']}) does not follow the "
        f"re-measured 160px field"
    )
    assert out["scrollFollow"] > 0, "scroll-follow behavior was dropped"
