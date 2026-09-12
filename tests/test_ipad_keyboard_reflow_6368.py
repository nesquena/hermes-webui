"""Regression guard for #6368: iPad keeps a horizontal layout offset after the
on-screen keyboard is dismissed.

The fix has two halves and this file pins both:

* ``static/boot.js`` — ``_syncKeyboardBottomInset()`` is the single keyboard
  state authority: it writes ``--keyboard-bottom-inset`` *and* mirrors the state
  on ``body.keyboard-visible``, and every exit that cannot prove an unzoomed,
  occluding keyboard (no ``visualViewport``, no touch surface, pinch zoom, zero
  inset) clears both. The document horizontal reset is bound to the
  keyboard-visible → dismissed transition so it fires exactly once per
  dismissal, instead of on every ``visualViewport`` event (which cancelled the
  horizontal position a pinch/pan had just set).
* ``static/style.css`` — ``.layout{overflow-x:clip}``, the one-frame
  ``html.viewport-reflow`` transform and ``body.keyboard-visible`` are scoped to
  touch/mobile surfaces instead of applying to every desktop layout.

Review-driven coverage (PR #6377):
  1. coarse/no-fine tablet executes the reflow, fine-pointer desktop does not;
  2. pinch zoom does not reset the document's horizontal offset;
  3. keyboard show → dismiss resets X exactly once while preserving Y;
  4. the reflow + keyboard-visible classes are cleaned up on every exit;
  5. the ``.layout`` clipping stays touch/mobile-scoped.
  6. a tablet whose primary pointer is fine (iPad + trackpad/Magic Keyboard)
     still gets the reflow and the horizontal reset.

The behavior tests extract the real functions out of ``static/boot.js`` and run
them under ``node`` with a stubbed ``visualViewport``/DOM, the same pattern
already used by ``tests/test_5552_viewport_anchor_surrogate.py``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOT_JS_PATH = ROOT / "static" / "boot.js"
STYLE_CSS_PATH = ROOT / "static" / "style.css"

# The functions under test, in dependency order.
FUNCTIONS = [
    "_isPhoneWidthViewport",
    "_isTouchKeyboardViewport",
    "_isTouchCapableViewport",
    "_hasFinePointerCoexisting",
    "_syncKeyboardBottomInset",
    "_resetDocumentHorizontalOffset",
    "_forceMobileViewportReflow",
]


def _boot_js() -> str:
    assert BOOT_JS_PATH.exists(), f"static/boot.js not found at {BOOT_JS_PATH}"
    return BOOT_JS_PATH.read_text(encoding="utf-8")


def _style_css() -> str:
    assert STYLE_CSS_PATH.exists(), f"static/style.css not found at {STYLE_CSS_PATH}"
    return STYLE_CSS_PATH.read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    """Return the full source of ``function <name>(...) { ... }``.

    Brace matching (not a regex) so nested blocks, arrow functions and template
    literals are preserved verbatim. Mirrors the extractor used by the other
    behavior tests in this directory.
    """
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"static/boot.js no longer defines {name}()"
    brace = src.find("{", start)
    assert brace != -1, f"{name}() has no body"
    depth = 0
    i = brace
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces while extracting {name}()")


def _keyboard_state_decl(src: str) -> tuple[str, str]:
    """Return (identifier, declaration) for the keyboard-visible state flag."""
    match = re.search(r"^(let|var) (_keyboard\w*) *= *false;", src, re.M)
    assert match, (
        "static/boot.js must keep a module-level boolean that tracks whether the "
        "on-screen keyboard is occluding the viewport (the state authority for "
        "body.keyboard-visible)"
    )
    return match.group(2), match.group(0)


def _media_blocks(css: str) -> list[tuple[str, str]]:
    """Return [(media_query, block_body)] for every @media block, nesting aware."""
    blocks: list[tuple[str, str]] = []
    for match in re.finditer(r"@media([^{]*)\{", css):
        query = match.group(1).strip()
        depth = 0
        i = match.end() - 1
        while i < len(css):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        blocks.append((query, css[match.end() : i]))
    return blocks


def _css_outside_media(css: str) -> str:
    """The CSS with every @media block body removed (top-level rules only)."""
    out = []
    pos = 0
    for match in re.finditer(r"@media[^{]*\{", css):
        out.append(css[pos : match.start()])
        depth = 0
        i = match.end() - 1
        while i < len(css):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        pos = i + 1
    out.append(css[pos:])
    return "".join(out)


# ── static guards ────────────────────────────────────────────────────────────


def test_layout_clipping_is_scoped_to_touch_and_mobile_surfaces():
    """`.layout{overflow-x:clip}` must not apply to every desktop layout (#6377)."""
    css = _style_css()
    scoped_rules = [
        ".layout{overflow-x:clip;}",
        "html.viewport-reflow .layout{transform:translateZ(0);}",
        "body.keyboard-visible{overflow-x:hidden;position:relative;}",
    ]
    unscoped = _css_outside_media(css)
    for rule in scoped_rules:
        assert rule not in unscoped, (
            f"{rule!r} must not be applied globally — the keyboard-offset "
            "geometry it guards only exists on touch/mobile surfaces"
        )

    blocks = _media_blocks(css)
    for rule in scoped_rules:
        queries = [
            query
            for query, body in blocks
            if rule in body and "any-pointer" in query and "coarse" in query
        ]
        assert queries, (
            f"{rule!r} must live inside a touch-scoped media query such as "
            "@media (max-width: 640px), (any-pointer: coarse) so iPad-with-"
            "trackpad (fine primary pointer) is still covered"
        )


def test_keyboard_visible_class_is_written_and_cleared_by_the_state_authority():
    """`body.keyboard-visible` used to be dead CSS: nothing added the class."""
    src = _boot_js()
    fn = _extract_function(src, "_syncKeyboardBottomInset")
    assert "classList.add('keyboard-visible')" in fn, (
        "_syncKeyboardBottomInset() must add body.keyboard-visible while the "
        "keyboard occludes the viewport"
    )
    assert "classList.remove('keyboard-visible')" in fn, (
        "_syncKeyboardBottomInset() must remove body.keyboard-visible"
    )
    # The removal must sit in the shared cleanup helper, so every non-occluded
    # exit (no visualViewport, no touch surface, pinch zoom, zero inset) clears
    # the class instead of leaving it stranded on screen.
    assert re.search(r"const clearKeyboardState=\(\)=>\{[^}]*classList\.remove\('keyboard-visible'\)", fn), (
        "class removal must live in the single cleanup helper called by every "
        "ineligible/zero-inset exit"
    )
    assert fn.count("clearKeyboardState();") >= 3, (
        "every exit that cannot prove an occluding keyboard must call the "
        "cleanup helper (found "
        f"{fn.count('clearKeyboardState();')} call sites)"
    )


def test_horizontal_reset_is_bound_to_the_dismissal_transition():
    """The pre-fix code ran scrollTo on every eligible visualViewport event."""
    src = _boot_js()
    reflow = _extract_function(src, "_forceMobileViewportReflow")
    assert "window.scrollTo" not in reflow, (
        "_forceMobileViewportReflow() must not scroll unconditionally — that "
        "cancelled the horizontal position a pinch/pan had just set"
    )
    assert re.search(
        r"if\(keyboardTransition==='dismissed'\) _resetDocumentHorizontalOffset\(\);",
        reflow,
    ), (
        "the document horizontal reset must be gated on the "
        "keyboard-visible -> dismissed transition"
    )
    reset = _extract_function(src, "_resetDocumentHorizontalOffset")
    assert "window.scrollTo(0, window.scrollY)" in reset, (
        "_resetDocumentHorizontalOffset() must reset only X and keep the current "
        "vertical scroll position"
    )


def test_reflow_gate_uses_the_touch_capable_predicate():
    """"touch surface available" — not "touch-primary" — is the geometry gate."""
    src = _boot_js()
    fn = _extract_function(src, "_isTouchCapableViewport")
    assert "matchMedia('(any-pointer:coarse)')" in fn, (
        "_isTouchCapableViewport() must key off any-pointer:coarse so a tablet "
        "with a trackpad/mouse attached still reflows"
    )
    reflow = _extract_function(src, "_forceMobileViewportReflow")
    assert "_isTouchCapableViewport()" in reflow, (
        "_forceMobileViewportReflow() must gate on _isTouchCapableViewport()"
    )
    assert "_isTouchKeyboardViewport()" not in reflow, (
        "_isTouchKeyboardViewport() excludes fine-pointer-coexisting devices and "
        "must no longer gate layout geometry"
    )
    # The composer Enter semantics keep the stricter predicate — don't widen it.
    assert "!_hasFinePointerCoexisting()" in _extract_function(
        src, "_isTouchKeyboardViewport"
    ), "the touch-primary predicate must stay untouched for Enter semantics"


# ── behavior (real JS under node, stubbed visualViewport/DOM) ────────────────

_HARNESS_HEAD = r"""
const fs = require('fs');
const src = fs.readFileSync('static/boot.js', 'utf8');
const OUT = {};
let MEDIA = {};
// Media queries are keyed by the exact string boot.js passes to matchMedia.
function setMedia(opts) {
  const o = opts || {};
  MEDIA = {
    '(max-width: 640px)': !!o.mobile,
    '(max-width: 900px)': !!(o.mobile || o.coarse),
    '(any-pointer:coarse)': !!o.coarse,
    '(any-pointer:fine)': !!o.fine,
    '(pointer:coarse)': !!o.primaryCoarse,
    '(hover:none) and (pointer:coarse)': !!o.touchPrimary,
  };
}
globalThis.matchMedia = q => ({ media: q, matches: !!MEDIA[q] });

let VV = null;
let scrollCalls = [];
let rafQueue = [];
let bodyClasses = new Set();
let rootClasses = new Set();
let styleProps = {};
let resyncCount = 0;
let reflowAdds = 0;

const makeClassList = (set, onAdd) => ({
  add: c => { set.add(c); if (onAdd) onAdd(c); },
  remove: c => set.delete(c),
  contains: c => set.has(c),
});
const noteReflowAdd = c => { if (c === 'viewport-reflow') reflowAdds += 1; };
const layoutEl = { offsetWidth: 1024 };
globalThis.document = {
  documentElement: {
    style: {
      setProperty: (k, v) => { styleProps[k] = v; },
      removeProperty: k => { delete styleProps[k]; },
    },
    classList: makeClassList(rootClasses, noteReflowAdd),
  },
  body: { classList: makeClassList(bodyClasses) },
  querySelector: sel => (sel === '.layout' ? layoutEl : null),
};
globalThis.window = globalThis;
Object.defineProperty(globalThis, 'visualViewport', {
  get: () => VV,
  configurable: true,
});
globalThis.scrollTo = (x, y) => { scrollCalls.push([x, y]); };
globalThis.requestAnimationFrame = cb => { rafQueue.push(cb); return rafQueue.length; };
globalThis.syncWorkspacePanelState = () => { resyncCount += 1; };

function runRaf() { const q = rafQueue; rafQueue = []; q.forEach(cb => cb()); }
function reset() {
  scrollCalls = [];
  rafQueue = [];
  bodyClasses = new Set();
  rootClasses = new Set();
  styleProps = {};
  resyncCount = 0;
  reflowAdds = 0;
  document.documentElement.classList = makeClassList(rootClasses, noteReflowAdd);
  document.body.classList = makeClassList(bodyClasses);
  VV = { height: 900, offsetTop: 0, scale: 1 };
  globalThis.innerHeight = 900;
  globalThis.scrollY = 512;
  __STATE__ = false;
}
function keyboardUp(px) { VV.height = globalThis.innerHeight - px; VV.scale = 1; }
function keyboardDown() { VV.height = globalThis.innerHeight; VV.scale = 1; }
function pinch(scale, height) { VV.scale = scale; VV.height = height; }
function snap() {
  return {
    scrollCalls: scrollCalls.slice(),
    reflowAdds: reflowAdds,
    bodyKeyboardVisible: bodyClasses.has('keyboard-visible'),
    rootReflow: rootClasses.has('viewport-reflow'),
    inset: styleProps['--keyboard-bottom-inset'] || null,
    resync: resyncCount,
  };
}
function tick() { _forceMobileViewportReflow(); runRaf(); }
"""

_HARNESS_TAIL = r"""
// S1 — fine-pointer desktop: a resize must not touch the document scroll or
// paint any tablet/keyboard state.
setMedia({ fine: true });
reset();
VV = { height: 600, offsetTop: 0, scale: 1 };
tick(); tick(); tick();
OUT.fine_pointer_desktop = snap();

// S2 — coarse tablet (no fine pointer): keyboard up, then dismiss.
setMedia({ coarse: true, primaryCoarse: true, touchPrimary: true });
reset();
tick();                                    // no keyboard yet
OUT.tablet_idle = snap();
keyboardUp(300);
tick();
OUT.tablet_keyboard_up = snap();
keyboardDown();
tick();
OUT.tablet_dismissed = snap();
tick(); tick(); tick();                    // no further transitions
OUT.tablet_after_dismiss = snap();

// S3 — iPad + trackpad/Magic Keyboard: any-pointer:coarse but primary pointer
// is fine, so the touch-primary pair no longer matches.
setMedia({ coarse: true, fine: true });
reset();
keyboardUp(280);
tick();
OUT.ipad_trackpad_keyboard_up = snap();
keyboardDown();
tick();
OUT.ipad_trackpad_dismissed = snap();

// S4 — pinch zoom while the keyboard is up: no scroll reset, state cleaned.
setMedia({ coarse: true, primaryCoarse: true, touchPrimary: true });
reset();
keyboardUp(300);
tick();
OUT.pinch_before = snap();
pinch(2, 420);
tick(); tick(); tick();
OUT.pinch_zoom = snap();

// S5 — a phone-width viewport still reflows (regression guard for the phone
// repaint path that must keep working).
setMedia({ mobile: true, coarse: true, primaryCoarse: true, touchPrimary: true });
reset();
globalThis.innerHeight = 844;
VV = { height: 844, offsetTop: 0, scale: 1 };
keyboardUp(336);
tick();
OUT.phone_keyboard_up = snap();
keyboardDown();
tick();
OUT.phone_dismissed = snap();

setMedia({ fine: true });
reset();
tick();
OUT.desktop_after_phone = snap();

console.log('RESULT ' + JSON.stringify(OUT));
"""


def _run_harness() -> dict:
    src = _boot_js()
    state_name, state_decl = _keyboard_state_decl(src)
    preamble = "\n".join(_extract_function(src, name) for name in FUNCTIONS)
    script = (
        _HARNESS_HEAD.replace("__STATE__", state_name)
        + "\n"
        + state_decl
        + "\n"
        + preamble
        + "\n"
        + _HARNESS_TAIL
    )
    node = shutil.which("node")
    if not node:
        pytest.skip("node executable is required for JavaScript behavior checks")
    try:
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"node behavior check timed out\nstdout:\n{exc.stdout or '<empty>'}")
    assert result.returncode == 0, (
        "node behavior check failed\n"
        f"stdout:\n{result.stdout or '<empty>'}\nstderr:\n{result.stderr or '<empty>'}"
    )
    payload = [
        line for line in result.stdout.splitlines() if line.startswith("RESULT ")
    ]
    assert payload, f"harness produced no RESULT line\nstdout:\n{result.stdout}"
    return json.loads(payload[-1][len("RESULT ") :])


@pytest.fixture(scope="module")
def behavior() -> dict:
    return _run_harness()


def test_coarse_tablet_executes_while_fine_pointer_desktop_does_not(behavior):
    desktop = behavior["fine_pointer_desktop"]
    assert desktop["scrollCalls"] == [], (
        "a fine-pointer desktop must never get the document horizontal reset"
    )
    assert desktop["bodyKeyboardVisible"] is False
    assert desktop["rootReflow"] is False
    assert desktop["inset"] is None

    tablet = behavior["tablet_keyboard_up"]
    assert tablet["reflowAdds"] >= 1, (
        "a coarse-pointer tablet must enter the reflow path"
    )
    assert tablet["resync"] >= 1, (
        "the reflow must resync the workspace panel/sidebar state"
    )
    assert tablet["bodyKeyboardVisible"] is True, (
        "body.keyboard-visible must be applied while the keyboard occludes the "
        "viewport (it was unreachable CSS before this fix)"
    )
    assert tablet["inset"] == "300px"


def test_ipad_with_trackpad_also_reflows_and_resets(behavior):
    """fine primary pointer + touch surface must still be covered (grreview P1)."""
    up = behavior["ipad_trackpad_keyboard_up"]
    assert up["reflowAdds"] >= 1, (
        "iPad + trackpad reported as fine pointer must still reflow"
    )
    assert up["bodyKeyboardVisible"] is True
    # --keyboard-bottom-inset stays scoped to the touch-primary pair that reads it.
    assert up["inset"] is None

    dismissed = behavior["ipad_trackpad_dismissed"]
    assert dismissed["scrollCalls"] == [[0, 512]], (
        "iPad + trackpad dismissal must reset exactly X, preserving Y"
    )
    assert dismissed["reflowAdds"] >= 1


def test_pinch_zoom_does_not_reset_the_document_horizontal_offset(behavior):
    before = behavior["pinch_before"]
    assert before["bodyKeyboardVisible"] is True, (
        "scenario setup: the keyboard must be up before the pinch"
    )
    assert before["scrollCalls"] == []

    pinch_state = behavior["pinch_zoom"]
    assert pinch_state["scrollCalls"] == [], (
        "pinch zoom must not reset the document offset — that is the horizontal "
        "position the user just zoomed/panned to"
    )
    assert pinch_state["bodyKeyboardVisible"] is False, (
        "the pinch exit must clean up body.keyboard-visible"
    )
    assert pinch_state["rootReflow"] is False, (
        "the one-frame reflow class must be released after the animation frame"
    )
    assert pinch_state["inset"] is None


def test_keyboard_show_then_dismiss_resets_x_once_and_preserves_y(behavior):
    assert behavior["tablet_idle"]["scrollCalls"] == []
    assert behavior["tablet_keyboard_up"]["scrollCalls"] == [], (
        "showing the keyboard must not scroll the document"
    )
    dismissed = behavior["tablet_dismissed"]
    assert dismissed["scrollCalls"] == [[0, 512]], (
        "dismissal must reset X exactly once while preserving Y, got "
        f"{dismissed['scrollCalls']}"
    )
    assert dismissed["bodyKeyboardVisible"] is False
    assert dismissed["inset"] is None
    assert behavior["tablet_after_dismiss"]["scrollCalls"] == [[0, 512]], (
        "later visualViewport events must not re-run the reset"
    )
    assert behavior["tablet_after_dismiss"]["rootReflow"] is False


def test_reflow_class_cleanup_leaves_no_stranded_state(behavior):
    # Every snapshot is taken after runRaf(), so the one-frame class must be gone
    # everywhere, and the keyboard state must match the last geometry.
    for key, snapshot in behavior.items():
        assert snapshot["rootReflow"] is False, (
            f"html.viewport-reflow stranded after runRaf() in scenario {key}"
        )
    assert behavior["phone_keyboard_up"]["bodyKeyboardVisible"] is True
    assert behavior["phone_dismissed"]["bodyKeyboardVisible"] is False
    assert behavior["desktop_after_phone"]["bodyKeyboardVisible"] is False
    assert behavior["desktop_after_phone"]["inset"] is None
    assert behavior["phone_dismissed"]["scrollCalls"] == [[0, 512]], (
        "the phone keyboard-dismiss path must keep resetting X once"
    )
