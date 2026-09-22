"""Regression tests for #6711 — preview fullscreen + font-size zoom.

The gate's exact-head review at `1aff9a80` raised five finding groups (F1–F5).
This file covers the ones that are behavioural and mechanically checkable, and
pins the two that must not silently regress in CSS.

  F1 — zoom left advertised text modes partially unscaled. Headings were
       hard-coded (24/20/17/15/14/13px) and `.preview-md table` was fixed at
       12px, so A−/A+ moved body copy while headings and CSV grids stood still.
       At 36px body text outgrew every heading, reversing hierarchy.
  F2 — `parseInt('bad')` is NaN and Math.min/Math.max propagate NaN, so one
       malformed stored value wrote `--preview-font-size: NaNpx` and every later
       A−/A+ stayed NaN. Separately, always writing the resolved value pinned an
       inline style that silently defeated the `data-font-size` mapping.
  F3 — the editor's Escape handler did not stopPropagation, so a single Escape
       while editing in fullscreen both discarded the edit view AND exited
       fullscreen. The editor owns the first Escape.
  F4 — covered app chrome (`.app-titlebar` at z-index:20) stayed in tab order
       behind the covering panel, with no inert/focus management.
  F5 — `.mobile-open` is only written by `_setWorkspacePanelMode()`, so a
       breakpoint change while open left mode and class out of step
       (desktop fullscreen -> phone width -> exit landed off-screen).

The JS behaviours run through a Node VM over the exact shipped source; the CSS
findings are asserted against the shipped stylesheet because a hard-coded px
value is exactly what F1 was.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS_PATH = REPO_ROOT / "static" / "workspace.js"
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
STYLE_CSS_PATH = REPO_ROOT / "static" / "style.css"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function(name: str, prefix: str = "function") -> str:
    source = _read(WORKSPACE_JS_PATH)
    if f"{prefix} {name}(" not in source:
        source = _read(BOOT_JS_PATH)
    return extract_function(source, name, prefix=prefix)


def _function_with_defaults(name: str, prefix: str = "function") -> str:
    """Like ``_function()`` but safe for signatures with default parameters.

    ``extract_function()`` brace-matches from the FIRST ``{``, which for
    ``setPreviewFullscreen(active, opts={})`` is the default-value brace — it
    returns the signature only, and every assertion against the body then fails
    against a string that has no body at all. Match the parameter-closing ``){``
    instead and brace-match from there.
    """
    source = _read(WORKSPACE_JS_PATH)
    marker = f"{prefix} {name}("
    if marker not in source:
        source = _read(BOOT_JS_PATH)
    start = source.index(marker)
    params_close = source.index("){", start)
    brace = params_close + 1
    depth = 0
    i = brace
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"{name} body never closed")


# ── F1: CSS zoom coverage ────────────────────────────────────────────────────


def test_headings_scale_with_the_zoom_variable():
    """h1–h6 must derive from --preview-font-size, not hard-coded px."""
    css = _read(STYLE_CSS_PATH)
    for level in range(1, 7):
        # Match every rule whose selector list ends with this heading. The
        # shared weight/colour rule (`.preview-md h1,…,hN{font-weight:700…}`)
        # also ends in `.preview-md hN{`, so pick the rules that set a size.
        bodies = [
            m.group(1)
            for m in re.finditer(rf"\.preview-md h{level}\{{([^}}]*)\}}", css)
        ]
        assert bodies, f".preview-md h{level} rule not found"
        sized = [b for b in bodies if "font-size" in b]
        assert sized, f".preview-md h{level} sets no font-size"
        for rule in sized:
            assert "--preview-font-size" in rule, (
                f".preview-md h{level} has a font-size that does not follow the "
                f"zoom variable, so A−/A+ leaves headings behind: {rule}"
            )
            assert not re.search(r"font-size:\s*\d+(\.\d+)?px", rule), (
                f".preview-md h{level} is hard-coded in px again: {rule}"
            )


def test_table_font_size_follows_zoom_variable():
    """CSV renders into #previewMd as a table — a fixed 12px made zoom a no-op."""
    css = _read(STYLE_CSS_PATH)
    match = re.search(r"\.preview-md table\{([^}]*)\}", css)
    assert match, ".preview-md table rule not found"
    rule = match.group(1)
    assert "--preview-font-size" in rule, (
        ".preview-md table is hard-coded, so A−/A+ does nothing for CSV previews"
    )


def test_default_zoom_matches_historical_body_size():
    """The CSS variable default must stay 13px so the zoom rework is not also a
    visible default-change (the review flagged the 12px default delta)."""
    css = _read(STYLE_CSS_PATH)
    assert "--preview-font-size:13px" in css, (
        "--preview-font-size default changed; markdown body was 13px before zoom"
    )


# ── F2: clamping + precedence ────────────────────────────────────────────────


def _preview_fs_constants() -> str:
    """The clamp bounds are module-level `const`s (not inside the function), so
    the Node VM needs them injected alongside the extracted function bodies.

    Emitted as `var`: a `const`/`let` declared inside `eval()` is scoped to that
    eval and would not be visible to the function bodies evaluated afterwards,
    whereas `var` lands on the enclosing (here: global) scope.
    """
    src = _read(WORKSPACE_JS_PATH)
    consts = []
    for name in ("_PREVIEW_FS_MIN", "_PREVIEW_FS_MAX"):
        match = re.search(rf"const {name}\s*=\s*(\d+)\s*;", src)
        assert match, f"{name} not found in static/workspace.js"
        consts.append(f"var {name} = {match.group(1)};")
    return "\n".join(consts)


_F2_HARNESS_HEAD = r"""
const params = __PARAMS__;
// The read/get paths consult localStorage and getComputedStyle, and
// _getPreviewFontSize reaches for document.documentElement — none of which
// exist in a bare Node VM. A missing `document` throws ReferenceError, which
// the production try/catch swallows, so the fallback silently returned its
// default instead of the computed value and the test passed for the wrong
// reason. Stub all three so the read path runs for real.
const __store = { 'hermes-preview-font-size': params.stored };
var localStorage = {
  getItem: (k) => (k in __store ? __store[k] : null),
  setItem: (k, v) => { __store[k] = String(v); },
};
var document = { documentElement: { nodeType: 1, tagName: 'HTML' } };
function getComputedStyle(){ return { getPropertyValue: () => params.computedVar }; }
eval(params.consts);
eval(params.clamp);
eval(params.read);
eval(params.get);

// JSON.stringify(NaN) is "null", which is indistinguishable from a legitimate
// null. Report the raw type and the NaN-ness explicitly so a leaked NaN cannot
// masquerade as a correct rejection.
function describe(v){
  return {
    type: (typeof v),
    isNull: v === null,
    isNan: (typeof v === 'number' && Number.isNaN(v)),
    value: Number.isFinite(v) ? v : null,
  };
}

const out = {};
out.bad = describe(_clampPreviewFontSize('bad'));
out.nan = describe(_clampPreviewFontSize(NaN));
out.empty = describe(_clampPreviewFontSize(''));
out.undefinedValue = describe(_clampPreviewFontSize(undefined));
out.nullValue = describe(_clampPreviewFontSize(null));
out.obj = describe(_clampPreviewFontSize({}));

// real values clamp into range rather than passing through
out.low = describe(_clampPreviewFontSize(2));
out.high = describe(_clampPreviewFontSize(999));
out.ok = describe(_clampPreviewFontSize(18));
out.stringNum = describe(_clampPreviewFontSize('20'));

// The read path over a malformed stored value must reject it too.
out.readBad = describe(_readPreviewFontSize());

// Precedence: no stored value -> the app's computed variable wins.
out.getFallsBack = describe(_getPreviewFontSize());

console.log(JSON.stringify(out));
"""


def _run_f2(*, stored=None, computed_var="13px") -> dict:
    payload = {
        "consts": _preview_fs_constants(),
        "clamp": _function("_clampPreviewFontSize"),
        "read": _function("_readPreviewFontSize"),
        "get": _function("_getPreviewFontSize"),
        "stored": stored,
        "computedVar": computed_var,
    }
    js = _F2_HARNESS_HEAD.replace("__PARAMS__", json.dumps(payload))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_malformed_font_size_never_becomes_nan():
    """The original bug: parseInt('bad') -> NaN -> Math.max/min(NaN) -> NaN,
    which got persisted and applied as `NaNpx` permanently. JSON turns NaN into
    null, so the harness reports NaN-ness explicitly — otherwise a leaked NaN
    would look identical to a correct rejection."""
    result = _run_f2()
    for key in ("bad", "nan", "empty", "undefinedValue", "nullValue", "obj"):
        entry = result[key]
        assert entry["isNan"] is False, (
            f"_clampPreviewFontSize({key}) returned NaN, which is exactly the "
            f"value that used to be persisted and applied as `NaNpx`"
        )
        assert entry["isNull"] is True, (
            f"_clampPreviewFontSize({key}) returned {entry!r}; a malformed value "
            f"must be rejected (null) instead of propagating"
        )


def test_font_size_is_clamped_into_supported_range():
    result = _run_f2()
    assert result["low"]["value"] == 8, f"below-range value not clamped: {result['low']}"
    assert result["high"]["value"] == 36, f"above-range value not clamped: {result['high']}"
    assert result["ok"]["value"] == 18, f"in-range value altered: {result['ok']}"
    assert result["stringNum"]["value"] == 20, (
        f"numeric string not accepted: {result['stringNum']}"
    )


def test_read_path_rejects_a_malformed_stored_value():
    """A bad localStorage entry must not survive `_readPreviewFontSize()`."""
    result = _run_f2(stored="bad")
    assert result["readBad"]["isNan"] is False, (
        "_readPreviewFontSize returned NaN for a malformed stored value"
    )
    assert result["readBad"]["isNull"] is True, (
        f"_readPreviewFontSize must reject a malformed stored value, got {result['readBad']!r}"
    )


def test_setter_refuses_to_persist_a_malformed_value():
    """_setPreviewFontSize must not write NaN to storage or the CSS variable."""
    body = _function("_setPreviewFontSize")
    assert "_clampPreviewFontSize" in body, (
        "_setPreviewFontSize must route through the clamp so NaN can never be "
        "persisted or applied"
    )
    assert re.search(r"if\s*\(\s*clamped\s*===\s*null\s*\)\s*return", body), (
        "_setPreviewFontSize must bail out on an unclampable value"
    )


def test_getter_falls_back_to_the_computed_app_font_size():
    """When no explicit zoom is stored, the getter must read the resolved
    `--preview-font-size` (which carries the data-font-size mapping) instead of
    a bare 12 — otherwise the app-level small/large/xlarge mapping is defeated."""
    body = _function("_getPreviewFontSize")
    assert "getComputedStyle" in body, (
        "_getPreviewFontSize must consult the computed --preview-font-size so the "
        "global font-size preference is honoured when no zoom was chosen"
    )
    assert "_readPreviewFontSize" in body, (
        "_getPreviewFontSize must prefer an explicit stored zoom when present"
    )


def test_getter_prefers_a_stored_zoom_over_the_app_font_size():
    """A user's own A−/A+ choice must win over the app-level mapping — proven by
    running the real function against both inputs."""
    stored_wins = _run_f2(stored="22", computed_var="16px")
    assert stored_wins["getFallsBack"]["value"] == 22, (
        f"a stored zoom must win over the computed variable, got {stored_wins['getFallsBack']}"
    )
    mapping_wins = _run_f2(stored=None, computed_var="16px")
    assert mapping_wins["getFallsBack"]["value"] == 16, (
        f"with no stored zoom the computed (data-font-size) value must be used, "
        f"got {mapping_wins['getFallsBack']}"
    )


# ── F3: Escape ownership ─────────────────────────────────────────────────────


def test_editor_escape_stops_propagation():
    """Without stopPropagation the document Escape handler also exits
    fullscreen, so one Escape did two things."""
    src = _read(WORKSPACE_JS_PATH)
    idx = src.find("$('previewEditArea').onkeydown=e=>{")
    assert idx != -1, "previewEditArea Escape handler not found"
    block = src[idx:idx + 400]
    assert "stopPropagation" in block, (
        "the editor's Escape handler must stopPropagation so the first Escape "
        "belongs to the editor and does not also exit fullscreen"
    )


def test_document_escape_still_exits_fullscreen():
    """The second Escape (and Escape outside the editor) must still exit."""
    src = _read(BOOT_JS_PATH)
    idx = src.find("preview-fullscreen-active")
    assert idx != -1, "document-level fullscreen Escape handler not found"
    block = src[idx:idx + 500]
    assert "setPreviewFullscreen(false)" in block, (
        "the document Escape handler must still exit fullscreen"
    )


# ── F4: covered chrome + focus lifecycle ─────────────────────────────────────


def test_fullscreen_css_hides_the_app_titlebar():
    """The titlebar stayed in tab order behind the covering panel."""
    css = _read(STYLE_CSS_PATH)
    assert re.search(
        r"html\.preview-fullscreen-active\s+\.app-titlebar\s*\{[^}]*display\s*:\s*none",
        css,
    ), (
        "fullscreen must hide .app-titlebar, otherwise its New conversation / "
        "Reload / profile / menu controls remain tabbable behind the panel"
    )


def test_fullscreen_lifecycle_entrypoints_manage_covered_chrome():
    """F4 needs the enter/exit helpers actually invoked by setPreviewFullscreen —
    checking the helper bodies alone would pass even if nothing called them, which
    is exactly the shape of dead-code that the gate caught elsewhere."""
    fs = _function_with_defaults("setPreviewFullscreen")
    assert "_previewFullscreenEnter(panel)" in fs, (
        "setPreviewFullscreen must invoke the enter helper, otherwise no chrome is "
        "ever marked inert and focus is never moved into the panel"
    )
    assert "_previewFullscreenExit(panel)" in fs, (
        "setPreviewFullscreen must invoke the exit helper, otherwise inert is never "
        "lifted and focus is never restored"
    )


def test_fullscreen_enter_inerts_covered_chrome():
    body = _function("_previewFullscreenEnter")
    assert "inert" in body, (
        "_previewFullscreenEnter must mark covered chrome inert so background "
        "controls leave the tab order"
    )
    assert "_previewFsPrevFocus" in body, (
        "_previewFullscreenEnter must snapshot the previously focused element"
    )


def test_fullscreen_exit_restores_focus():
    """Exit must lift inert, and queue a focus target instead of focusing

    immediately: clearPreview() hides the zoom/fullscreen controls right after
    the exit, so focusing inside `_previewFullscreenExit()` stranded focus on a
    display:none button (the gate's focus blocker). The flush lands it later."""
    body = _function("_previewFullscreenExit")
    assert "removeAttribute('inert')" in body or 'removeAttribute("inert")' in body, (
        "_previewFullscreenExit must lift inert from the restored chrome"
    )
    assert "_previewFsPendingFocus" in body, (
        "_previewFullscreenExit must queue a focus target rather than focus "
        "immediately, because the controls are hidden after it returns"
    )
    assert ".focus()" not in body, (
        "_previewFullscreenExit must not call focus() directly — the target may be "
        "hidden by the time clearPreview() finishes; _flushPreviewFullscreenFocus() "
        "owns the focus call"
    )
    flush = _function("_flushPreviewFullscreenFocus")
    assert ".focus()" in flush, (
        "nothing focuses the queued target, so focus ends up on <body>"
    )


# ── Greptile review (a): opening a preview must not persist an inherited size ─
#
# `_showPreviewZoomControls()` used to call `_setPreviewFontSize(_getPreviewFontSize())`,
# i.e. it wrote the resolved value into localStorage on every preview open. When
# no explicit zoom was stored, that resolved value came from the app-level
# `data-font-size` mapping — so merely opening a file pinned the global setting
# as a per-preview override, and a later change of the app font size no longer
# reached previews.


def test_opening_a_preview_does_not_persist_the_inherited_font_size():
    src = _read(WORKSPACE_JS_PATH)
    idx = src.find("function _showPreviewZoomControls(")
    assert idx != -1, "_showPreviewZoomControls not found"
    body = extract_function(src, "_showPreviewZoomControls")
    assert "_setPreviewFontSize(" not in body, (
        "_showPreviewZoomControls must not call the persisting setter: opening a "
        "preview would stamp the inherited data-font-size value into storage"
    )
    assert "_applyPreviewFontSize(" in body, (
        "_showPreviewZoomControls must apply the resolved size without persisting it"
    )


def test_persisting_and_applying_are_separate_functions():
    """The A−/A+ buttons must still persist; the open path must not."""
    apply_body = _function("_applyPreviewFontSize")
    assert "localStorage" not in apply_body, (
        "_applyPreviewFontSize must not touch storage"
    )
    assert "--preview-font-size" in apply_body, (
        "_applyPreviewFontSize must still set the CSS variable"
    )
    set_body = _function("_setPreviewFontSize")
    assert "localStorage" in set_body, (
        "_setPreviewFontSize (used by the A−/A+ buttons) must still persist"
    )
    assert "_applyPreviewFontSize(" in set_body, (
        "_setPreviewFontSize should delegate the application to the apply helper"
    )


def test_zoom_button_handler_still_persists():
    """Removing persistence from the open path must not remove it from the buttons."""
    src = _read(WORKSPACE_JS_PATH)
    idx = src.find("_setPreviewFontSize(cur + delta)")
    assert idx != -1, (
        "the A−/A+ adjustment must call _setPreviewFontSize so the user's choice "
        "is persisted"
    )
    assert "_applyPreviewFontSizeToEditArea()" in src[idx : idx + 200], (
        "the zoom handler must also update the edit textarea size"
    )


# ── Greptile review (b): breakpoint reconciliation must refresh the controls ──


def test_breakpoint_reconciliation_resyncs_panel_ui_when_state_changes():
    body = _function("_reconcileWorkspacePanelBreakpoint", prefix="function")
    assert "syncWorkspacePanelUI()" in body, (
        "changing .mobile-open without re-syncing left the toggle label and "
        "aria-pressed/aria-expanded describing the previous viewport (Greptile)"
    )
    # It must only re-sync when the class actually changed, so an ordinary
    # resize that does not cross the breakpoint stays cheap.
    assert "before" in body and "after" in body, (
        "the reconciliation should compare the class before/after and only "
        "re-sync on a real change"
    )


def test_sync_workspace_panel_ui_reads_the_compact_class():
    """Guards why the re-sync is needed: the UI sync derives isOpen from
    `.mobile-open` on compact viewports."""
    body = extract_function(_read(BOOT_JS_PATH), "syncWorkspacePanelUI")
    assert "mobile-open" in body, (
        "syncWorkspacePanelUI no longer reads .mobile-open; if that changed, the "
        "breakpoint re-sync rationale needs revisiting"
    )
    assert "isCompact" in body, (
        "syncWorkspacePanelUI should branch on the compact viewport"
    )


# ── F5: breakpoint reconciliation ────────────────────────────────────────────


_F5_HARNESS = r"""
const params = __PARAMS__;
eval(params.reconcile);

// ── DOM doubles ──────────────────────────────────────────────────────────────
const classes = new Set();
const panel = {
  classList: {
    contains: (c) => classes.has(c),
    toggle: (c, on) => { if(on) classes.add(c); else classes.delete(c); },
    add: (c) => classes.add(c),
    remove: (c) => classes.delete(c),
  },
};
let _workspacePanelMode = params.mode;
let compact = params.compact;
let previewVisible = params.previewVisible;
const S = { session: params.hasSession ? { session_id: 's' } : null };

function _workspacePanelEls(){ return { panel }; }
function _isCompactWorkspaceViewport(){ return compact; }
function _hasWorkspacePreviewVisible(){ return previewVisible; }

_reconcileWorkspacePanelBreakpoint();
const afterCompact = panel.classList.contains('mobile-open');

// now the breakpoint changes back to desktop
compact = false;
_reconcileWorkspacePanelBreakpoint();
const afterDesktop = panel.classList.contains('mobile-open');

console.log(JSON.stringify({ afterCompact, afterDesktop, mode: _workspacePanelMode }));
"""


def _run_f5(*, mode: str, compact: bool, preview_visible: bool, has_session: bool) -> dict:
    payload = {
        "reconcile": _function("_reconcileWorkspacePanelBreakpoint"),
        "mode": mode,
        "compact": compact,
        "previewVisible": preview_visible,
        "hasSession": has_session,
    }
    js = _F5_HARNESS.replace("__PARAMS__", json.dumps(payload))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_breakpoint_change_restores_compact_open_state():
    """Entering a compact viewport while the panel is open must re-derive
    `.mobile-open` from the runtime mode, so the panel is not off-screen."""
    result = _run_f5(mode="preview", compact=True, preview_visible=True, has_session=True)
    assert result["afterCompact"] is True, (
        "resizing into a compact viewport while the panel is open left "
        ".mobile-open unset, so the panel renders off-screen"
    )


def test_breakpoint_change_clears_compact_class_on_desktop():
    result = _run_f5(mode="preview", compact=False, preview_visible=True, has_session=True)
    assert result["afterDesktop"] is False, (
        ".mobile-open must be cleared when the viewport is not compact"
    )


def test_breakpoint_change_leaves_a_closed_panel_closed():
    result = _run_f5(mode="closed", compact=True, preview_visible=False, has_session=True)
    assert result["afterCompact"] is False, (
        "a closed panel must not be re-opened by a breakpoint change"
    )


def test_breakpoint_change_keeps_open_browse_panel_without_session():
    """Greptile P1: a compact resize must not hide an open browse panel that has

    no session and no preview. `syncWorkspacePanelState()` deliberately keeps
    'browse' mode on fresh/empty-session boots, so the reconciler re-deriving a
    narrower predicate hid the panel while its runtime and persisted state still
    said open — the controls announced it as closed."""
    result = _run_f5(mode="browse", compact=True, preview_visible=False, has_session=False)
    assert result["afterCompact"] is True, (
        "resizing into a compact viewport with an open browse panel and no session "
        "left .mobile-open unset: the panel is hidden while its state says open"
    )


def test_breakpoint_change_keeps_open_browse_panel_without_preview_but_with_session():
    """Same divergence, the other axis: browse mode with a session but no preview."""
    result = _run_f5(mode="browse", compact=True, preview_visible=False, has_session=True)
    assert result["afterCompact"] is True, (
        "an open browse panel with a session but no preview must stay visible on compact"
    )


def test_reconciler_matches_the_setters_visibility_predicate():
    """The two writers of `.mobile-open` must agree on the predicate.

    The setter uses `open` alone; the reconciler must not add a second
    visibility predicate, or the breakpoint path and the mode path disagree."""
    reconcile = _function("_reconcileWorkspacePanelBreakpoint", prefix="function")
    setter = extract_function(_read(BOOT_JS_PATH), "_setWorkspacePanelMode")
    assert "panel.classList.toggle('mobile-open',open)" in reconcile.replace(" ", ""), (
        "the reconciler must derive .mobile-open from the runtime mode alone"
    )
    assert "panel.classList.toggle('mobile-open',open)" in setter.replace(" ", ""), (
        "the setter derives .mobile-open from the runtime mode alone"
    )
    # No extra visibility predicate on either side.
    for label, body in (("reconciler", reconcile), ("setter", setter)):
        assert "shouldShow" not in body, (
            f"{label} reintroduced a separate visibility predicate for .mobile-open"
        )


def test_resize_and_fullscreen_exit_call_the_reconciler():
    """The reconciler only helps if the real lifecycle paths invoke it."""
    boot = _read(BOOT_JS_PATH)
    idx = boot.find("window.addEventListener('resize'")
    assert idx != -1, "resize listener not found"
    assert "_reconcileWorkspacePanelBreakpoint" in boot[idx:idx + 500], (
        "the resize path must reconcile the panel breakpoint"
    )
    fs_exit = _function_with_defaults("setPreviewFullscreen")
    assert "_reconcileWorkspacePanelBreakpoint" in fs_exit, (
        "exiting fullscreen must reconcile the panel breakpoint"
    )


# ── Greptile review (b): an inline write re-pins the inherited size ───────────
#
# Fix (a) stopped the *storage* write, but `_applyPreviewFontSize()` still wrote
# the variable inline on <html>. An inline custom property outranks the
# `:root[data-font-size="…"]{--preview-font-size:…}` rule in the stylesheet, so
# the first preview open froze whatever the mapping resolved to and a later
# app-font change no longer reached previews — the same user-visible symptom as
# the storage bug, reached by a different path.


def _preview_font_css() -> str:
    """The shipped `data-font-size` rules that define --preview-font-size."""
    css = _read(STYLE_CSS_PATH)
    rules = re.findall(r':root\[data-font-size="[a-z]+"\]\{[^}]*--preview-font-size:[^}]*\}', css)
    assert len(rules) >= 3, f"expected the data-font-size rules, found {len(rules)}"
    return "\n".join(rules)


def _apply_preview_font_size(px: int, *, stored: str | None) -> dict:
    """Run the shipped `_applyPreviewFontSize()` in a real browser.

    Returns how the variable resolves for the caller's zoom, and again after the
    app-wide font size changes — the second read is the regression: a pinned
    inline value stays put instead of following `data-font-size`.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the preview-font browser test")

    js_parts = {
        "consts": _preview_fs_constants(),
        "clamp": _function("_clampPreviewFontSize"),
        "read": _function("_readPreviewFontSize"),
        "apply": _function("_applyPreviewFontSize"),
    }

    playwright = sync_playwright().start()
    # Only a missing/unlaunchable browser may skip; anything after a successful
    # launch must fail loudly so a real regression cannot hide behind a skip.
    try:
        browser = playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
    except Exception as exc:  # pragma: no cover - no browser binary in sandbox
        playwright.stop()
        pytest.skip(f"chromium unavailable for browser measurement: {exc}")

    try:
        page = browser.new_page()
        page.set_content("<!doctype html><html data-font-size='large'></html>")
        page.add_style_tag(content=_preview_font_css())
        return page.evaluate(
            """(parts) => {
              // Stub storage at closure scope so the direct eval below sees it,
              // then let the SHIPPED functions run against the real document.
              const store = { 'hermes-preview-font-size': parts.stored };
              const localStorage = {
                getItem: (k) => (k in store ? store[k] : null),
                setItem: (k, v) => { store[k] = String(v); },
              };
              eval(parts.consts);
              eval(parts.clamp);
              eval(parts.read);
              eval(parts.apply);
              const cs = () => getComputedStyle(document.documentElement)
                .getPropertyValue('--preview-font-size').trim();
              const inline = () => document.documentElement.style.getPropertyValue('--preview-font-size');
              _applyPreviewFontSize(parts.px);
              const before = { computed: cs(), inline: String(inline()) };
              // The user changes the app-wide font size.
              document.documentElement.dataset.fontSize = 'xlarge';
              const after = { computed: cs(), inline: String(inline()) };
              return { before, after };
            }""",
            {
                **js_parts,
                "stored": stored,
                "px": px,
            },
        )
    finally:
        browser.close()
        playwright.stop()


def test_opening_a_preview_does_not_pin_the_inherited_size_inline():
    """With no stored zoom, the app font size must keep driving previews."""
    r = _apply_preview_font_size(13, stored=None)
    assert r["before"]["inline"] == "", (
        "opening a preview with no stored zoom wrote --preview-font-size inline; "
        "the inline property outranks the data-font-size rule, so the inherited "
        "size is pinned and later app-font changes stop reaching previews "
        f"(inline={r['before']['inline']!r})"
    )
    assert r["before"]["computed"] == "14px", (
        "with data-font-size=large the stylesheet defines 14px; the preview "
        f"resolved {r['before']['computed']!r} instead"
    )
    assert r["after"]["computed"] == "16px", (
        "after the app font size changed to xlarge the preview must follow the "
        f"new mapping (16px), not stay pinned — got {r['after']['computed']!r}"
    )


def test_an_explicit_zoom_still_overrides_the_app_font_size():
    """A stored zoom is a user choice and must still win over the mapping."""
    r = _apply_preview_font_size(22, stored="22")
    assert r["before"]["inline"] == "22px", (
        "an explicit zoom must be written inline so it overrides the stylesheet "
        f"(inline={r['before']['inline']!r})"
    )
    assert r["before"]["computed"] == "22px", (
        f"the explicit zoom must apply, got {r['before']['computed']!r}"
    )
    assert r["after"]["computed"] == "22px", (
        "the user's explicit zoom must keep overriding a later app-font change, "
        f"got {r['after']['computed']!r}"
    )


# ── Fullscreen must neutralise the panel's own inline-size container ─────────
#
# `.rightpanel` is `container-type:inline-size` with a named container, and the
# preview header has `@container rightpanel (max-width:520px)` rules that drop
# button labels on a narrow PANE. Fullscreen covers the viewport, so those
# narrow-pane rules must stop applying — otherwise a fullscreen preview on a
# phone hides the header's button labels even though there is a full screen of
# width available. `.rightpanel.preview-fullscreen` therefore sets
# `container-type:normal`.
#
# Without this pinned, the property looks like an arbitrary declaration and is
# an easy thing to "clean up" — which silently reintroduces the collapsed


def test_fullscreen_neutralises_the_panel_container_for_narrow_panes():
    """The fullscreen rule must drop the panel's inline-size containment."""
    css = _read(STYLE_CSS_PATH)
    rule = re.search(r"\.rightpanel\.preview-fullscreen\{([^}]*)\}", css)
    assert rule, ".rightpanel.preview-fullscreen rule missing"
    body = rule.group(1).replace(" ", "")
    assert "container-type:normal" in body, (
        "the fullscreen rule must set container-type:normal: .rightpanel is an "
        "inline-size container and the preview header's @container rightpanel "
        "(max-width:520px) rules would otherwise keep hiding button labels in "
        "fullscreen, where the panel is as wide as the viewport"
    )
    assert "position:fixed" in body, "fullscreen must still cover the viewport"
    assert "100vw" in body and "100dvh" in body, (
        "fullscreen must still span the viewport (100vw / 100dvh)"
    )


def test_the_narrow_pane_container_rule_actually_exists():
    """Pin the rule the neutralisation exists for, so the pair cannot drift."""
    css = _read(STYLE_CSS_PATH)
    assert "@container rightpanel (max-width:520px)" in css, (
        "the narrow-pane @container rule is gone; if the panel no longer has "
        "width-based rules, container-type:normal in fullscreen may be removable "
        "— revisit both together"
    )
    m = re.search(r"@container rightpanel \(max-width:520px\)\{([^}]*)\}", css)
    assert m and "display:none" in m.group(1), (
        "the narrow-pane rule no longer hides anything, so the fullscreen "
        "neutralisation needs re-justifying"
    )


# ── Greptile review (d): the edit surface must follow an app-font change ──────
#
# `#previewEditArea` has no stylesheet rule of its own: its size is the inline
# `font-size` written by `_applyPreviewFontSizeToEditArea()`, and the textarea
# ships `font-size:12px` inline in index.html. Changing the app-wide font size
# while a text preview was open therefore resized the rendered preview (it reads
# `--preview-font-size` from CSS) while the editor and the zoom label kept the
# old value — three surfaces, two sizes.
#
# The refresh is measured in a real browser rather than asserted as source shape,
# because the bug is precisely about what the computed sizes resolve to.


_EDIT_FIXTURE = """
<textarea id="previewEditArea" style="font-size:12px"></textarea>
<span id="previewFontSizeLabel">12</span>
"""


def _run_app_font_change(*, stored=None) -> dict:
    """Change the app font size via the shipped `_applyFontSize()` and report
    what each of the three surfaces resolves to afterwards."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the preview-font browser test")

    parts = {
        "consts": _preview_fs_constants(),
        "clamp": _function("_clampPreviewFontSize"),
        "read": _function("_readPreviewFontSize"),
        "get": _function("_getPreviewFontSize"),
        "apply": _function("_applyPreviewFontSize"),
        "applyEdit": _function("_applyPreviewFontSizeToEditArea"),
        "refresh": _function("_refreshPreviewFontSize"),
        "applyFontSize": extract_function(_read(BOOT_JS_PATH), "_applyFontSize"),
        "previewFontCss": _preview_font_css(),
    }

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
    except Exception as exc:  # pragma: no cover - no browser binary in sandbox
        playwright.stop()
        pytest.skip(f"chromium unavailable for browser measurement: {exc}")

    try:
        page = browser.new_page()
        page.set_content("<!doctype html><html>" + _EDIT_FIXTURE + "</html>")
        page.add_style_tag(content=parts["previewFontCss"])
        return page.evaluate(
            """(p) => {
              const store = { 'hermes-preview-font-size': p.stored };
              const localStorage = {
                getItem: (k) => (k in store ? store[k] : null),
                setItem: (k, v) => { store[k] = String(v); },
                removeItem: (k) => { delete store[k]; },
              };
              eval(p.consts);
              eval(p.clamp);
              eval(p.read);
              eval(p.get);
              eval(p.apply);
              eval(p.applyEdit);
              eval(p.refresh);
              eval(p.applyFontSize);

              const ta = document.getElementById('previewEditArea');
              const label = document.getElementById('previewFontSizeLabel');
              const root = document.documentElement;
              const snapshot = () => ({
                edit: getComputedStyle(ta).fontSize,
                editInline: ta.style.fontSize,
                label: label.textContent,
                rootInline: root.style.getPropertyValue('--preview-font-size'),
                previewVar: getComputedStyle(root).getPropertyValue('--preview-font-size').trim(),
              });

              // Open the preview (no user zoom stored): the preview path applies
              // the inherited size without persisting it.
              _applyPreviewFontSize(_getPreviewFontSize());
              _applyPreviewFontSizeToEditArea();
              const before = snapshot();

              // The user changes the app-wide font size while it is open.
              _applyFontSize('xlarge');
              const after = snapshot();
              return { before, after };
            }""",
            parts | {"stored": stored},
        )
    finally:
        browser.close()
        playwright.stop()


def test_edit_surface_follows_an_app_font_change():
    """Inherited preview: editor, label and preview must all move together."""
    r = _run_app_font_change(stored=None)
    before, after = r["before"], r["after"]

    # Sanity: xlarge maps --preview-font-size to 16px (see style.css).
    assert after["previewVar"] == "16px", (
        f"precondition: the app font change must move the preview variable, "
        f"got {after['previewVar']!r}"
    )
    assert before["edit"] != after["edit"], (
        "the preview variable changed but the edit textarea kept its previous "
        f"size ({after['edit']!r}) — the editor is stale"
    )
    assert after["edit"] == after["previewVar"], (
        f"editor ({after['edit']!r}) and rendered preview ({after['previewVar']!r}) "
        "must resolve to the same size after an app-font change"
    )
    assert after["label"] == "16", (
        f"the zoom label must be refreshed to the new size, got {after['label']!r}"
    )


def test_an_explicit_zoom_survives_an_app_font_change():
    """A stored zoom is the user's choice and must still win."""
    r = _run_app_font_change(stored="22")
    after = r["after"]

    assert after["edit"] == "22px", (
        f"a stored zoom must keep driving the editor, got {after['edit']!r}"
    )
    assert after["label"] == "22", (
        f"a stored zoom must keep driving the label, got {after['label']!r}"
    )
    assert after["previewVar"] == "22px", (
        f"a stored zoom must keep overriding the app mapping, got {after['previewVar']!r}"
    )


def test_the_app_font_change_path_calls_the_refresh():
    """Pin the wiring: without the call, the measured fix cannot happen."""
    body = extract_function(_read(BOOT_JS_PATH), "_applyFontSize")
    assert "_refreshPreviewFontSize" in body, (
        "changing the app font size must re-resolve the preview typography, or "
        "an open preview's editor keeps the previous size (Greptile review)"
    )


# ── Gate re-review (21 Sep): three blockers + three follow-throughs ───────────
#
# 1. Escape could not exit from HTML/PDF iframe focus — the only Escape handler
#    is a document keydown listener and key events do not cross the iframe
#    browsing-context boundary, while README promised "Escape to exit".
# 2. clearPreview() stranded focus on a hidden control: the exit restored focus
#    to the fullscreen button, then clearPreview() hid it.
# 3. Fullscreen vanished at 641–900px: the ≤900px rule hides .rightpanel and the
#    display:flex!important rule only exists at ≤640px.
# Plus: invalid aria-modal on role=region; inert/aria-hidden clobbered; and
# hard-coded English labels instead of locale keys.

I18N_JS_PATH = REPO_ROOT / "static" / "i18n.js"
INDEX_HTML_PATH = REPO_ROOT / "static" / "index.html"

# Keys the gate asked to route through locale parity.
PREVIEW_FS_KEYS = (
    "preview_fullscreen_enter",
    "preview_fullscreen_exit",
    "preview_fullscreen_region",
    "preview_zoom_out",
    "preview_zoom_in",
)


def _top_level_locales() -> list[str]:
    src = _read(I18N_JS_PATH)
    keys = []
    for m in re.finditer(r"^  ('[^']+'|[A-Za-z][A-Za-z0-9-]*):\s*\{", src, re.M):
        keys.append(m.group(1).strip("'"))
    return keys


def _locale_block(locale: str) -> str:
    """Return a top-level locale's body using brace matching (not a window)."""
    src = _read(I18N_JS_PATH)
    marker = f"'{locale}'" if "-" in locale else locale
    m = re.search(r"^  " + re.escape(marker) + r":\s*\{", src, re.M)
    assert m, f"locale block not found: {locale}"
    # For quoted keys the marker itself contains a quote; find the body brace.
    brace = src.index("{", m.start())
    depth = 0
    i = brace
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace : i + 1]
        i += 1
    raise AssertionError(f"locale block never closed: {locale}")


# ── Blocker 1: native Fullscreen API ownership (Escape from iframe focus) ─────


def test_fullscreen_uses_the_native_api_so_escape_works_from_iframe_focus():
    """A document keydown listener cannot see Escape pressed inside the iframe,

    so the mode must be owned by the browser's own fullscreen implementation."""
    body = _function_with_defaults("setPreviewFullscreen")
    assert "_requestNativePreviewFullscreen" in body, (
        "entering fullscreen must request native fullscreen, or Escape pressed "
        "while focus is inside #previewHtmlIframe/#previewPdfFrame never exits"
    )
    assert "_exitNativePreviewFullscreen" in body, (
        "leaving fullscreen must leave native fullscreen too"
    )


def test_native_fullscreen_helpers_are_fail_safe():
    """Browsers without the API (iOS Safari, non-video elements) must still get

    the overlay: request failures are swallowed and never throw."""
    req = _function("_requestNativePreviewFullscreen")
    assert "requestFullscreen" in req
    assert "catch" in req, "a rejected requestFullscreen must not break the toggle"
    exit_body = _function("_exitNativePreviewFullscreen")
    assert "exitFullscreen" in exit_body
    assert "fullscreenElement" in exit_body, (
        "exitFullscreen must only be called when the browser is actually in "
        "fullscreen, otherwise it rejects during a plain overlay exit"
    )


def test_a_browser_driven_fullscreen_exit_reconciles_the_overlay():
    """Escape handled by the browser ends native fullscreen directly, so the

    overlay class must be reconciled from the fullscreenchange event."""
    src = _read(WORKSPACE_JS_PATH)
    assert "addEventListener('fullscreenchange'" in src, (
        "without a fullscreenchange listener, browser-handled Escape leaves the "
        "panel stuck in preview-fullscreen presentation"
    )
    idx = src.index("addEventListener('fullscreenchange'")
    block = src[idx : idx + 700]
    assert "setPreviewFullscreen(false)" in block, (
        "the fullscreenchange handler must detach the overlay state"
    )


# ── Blocker 2: focus must land after the controls settle ─────────────────────


def _clear_preview_body() -> str:
    """`extract_function()` brace-matches from the first `{`, which for

    `clearPreview(opts={})` is the default-value brace — it returns the signature
    only. Slice to the next top-level declaration instead (the file declares
    top-level functions at column 0), same approach as the #6710 tests.
    """
    src = _read(BOOT_JS_PATH)
    declaration = "function clearPreview(opts={}){"
    start = src.index(declaration)
    offset = start + len(declaration)
    for line in src[offset:].split("\n"):
        if line.startswith(("function ", "async function ", "const ", "let ", "var ")):
            return src[start:offset]
        offset += len(line) + 1
    return src[start:]


def test_clear_preview_flushes_focus_last():
    """clearPreview() hides the zoom/fullscreen controls, so the queued focus

    target must be resolved only after the panel mode and controls are final."""
    body = _clear_preview_body()
    assert "_flushPreviewFullscreenFocus" in body, (
        "clearPreview() must flush the queued focus target"
    )
    flush_at = body.index("_flushPreviewFullscreenFocus")
    hide_at = body.index("_showPreviewZoomControls(false, false)")
    assert flush_at > hide_at, (
        "focus is flushed before the controls are hidden, so it lands on a "
        "display:none button (the gate's stranded-focus blocker)"
    )


def test_focus_flush_never_targets_a_hidden_control():
    flush = _function("_flushPreviewFullscreenFocus")
    assert "_isFocusablePreviewTarget" in flush, (
        "the flush must check visibility before focusing"
    )
    assert "btnClearPreview" in flush or "btnWorkspacePanelToggle" in flush, (
        "the flush needs a fallback control that outlives the preview"
    )


# ── Blocker 3: fullscreen must survive the 641–900px band ────────────────────


def test_fullscreen_rule_declares_display():
    """The ≤900px media query sets .rightpanel{display:none}; the mobile

    display:flex!important rule only exists at ≤640px, so the fullscreen rule
    itself must re-establish display at every width."""
    css = _read(STYLE_CSS_PATH)
    match = re.search(r"\.rightpanel\.preview-fullscreen\{([^}]*)\}", css)
    assert match, ".rightpanel.preview-fullscreen rule not found"
    rule = match.group(1)
    assert "display:flex" in rule.replace(" ", ""), (
        "the fullscreen rule does not set display, so a desktop fullscreen "
        "preview resized to 641–900px is invisible while fullscreen stays active"
    )
    assert "!important" in rule.split("display:flex", 1)[1][:40], (
        "the display override must be !important to beat the ≤900px rule"
    )


def test_tablet_band_is_covered_by_the_fullscreen_selector():
    """Assert the cascade shape the gate described, at the widths it named."""
    css = _read(STYLE_CSS_PATH)
    # The ≤900px rule that hides the panel must still exist …
    assert ".rightpanel{display:none}" in css, (
        "the ≤900px hide rule was removed; the blocker was the missing override"
    )
    # … and the fullscreen rule must win over it by specificity + !important.
    assert ".rightpanel.preview-fullscreen{display:flex!important" in css.replace(" ", ""), (
        "the fullscreen selector must override the tablet hide rule"
    )


# ── Follow-through: valid modal semantics ────────────────────────────────────


def test_fullscreen_uses_dialog_role_not_region():
    """aria-modal is only valid on a role that supports modality; role=region

    has no modal concept, so the pair was invalid."""
    enter = _function("_previewFullscreenEnter")
    assert "'dialog'" in enter, (
        "a covering modal overlay must use role=dialog (aria-modal is invalid "
        "on role=region)"
    )
    assert "'region'" not in enter, (
        "role=region is still set, so aria-modal remains invalid"
    )
    assert "'aria-modal'" in enter, "the modal declaration itself must remain"


# ── Follow-through: prior inert/aria-hidden values are preserved ─────────────


def test_inert_and_aria_hidden_are_restored_not_blindly_removed():
    """Exit must restore what was there; unconditional removeAttribute() wiped

    values another subsystem may have owned."""
    enter = _function("_previewFullscreenEnter")
    exit_body = _function("_previewFullscreenExit")
    assert "hadInert" in enter and "ariaHidden" in enter, (
        "_previewFullscreenEnter must snapshot each element's prior inert/"
        "aria-hidden state"
    )
    assert "hadInert" in exit_body and "ariaHidden" in exit_body, (
        "_previewFullscreenExit must restore the snapshot rather than deleting "
        "the attributes unconditionally"
    )


# ── Follow-through: locale parity for the new labels ─────────────────────────


def test_every_locale_defines_the_new_preview_labels():
    locales = _top_level_locales()
    assert "en" in locales, "the English locale block is missing"
    missing = {}
    for loc in locales:
        body = _locale_block(loc)
        absent = [k for k in PREVIEW_FS_KEYS if k not in body]
        if absent:
            missing[loc] = absent
    assert not missing, (
        f"locales missing the new preview/fullscreen keys: {missing}"
    )


def test_no_hardcoded_english_labels_remain_in_the_fullscreen_path():
    """The gate asked for these three labels to go through locale keys."""
    ws = _read(WORKSPACE_JS_PATH)
    assert "btn.title = active ? 'Exit fullscreen' : 'Fullscreen'" not in ws, (
        "the fullscreen button label is still hard-coded English"
    )
    assert "`Fullscreen preview: ${path}`" not in ws, (
        "the fullscreen region label is still hard-coded English"
    )
    button = _read(INDEX_HTML_PATH)
    zoom_out = re.search(r'id="btnPreviewZoomOut"[^>]*>', button)
    assert zoom_out, "btnPreviewZoomOut not found"
    tag = zoom_out.group(0)
    assert 'data-i18n-title="preview_zoom_out"' in tag, (
        "the zoom-out button is not wired to a locale key"
    )
    zoom_in = re.search(r'id="btnPreviewZoomIn"[^>]*>', button)
    assert zoom_in, "btnPreviewZoomIn not found"
    assert 'data-i18n-title="preview_zoom_in"' in zoom_in.group(0), (
        "the zoom-in button is not wired to a locale key"
    )


def test_stateful_fullscreen_button_has_no_static_i18n_key():
    """The label flips between "Fullscreen" and "Exit fullscreen", so a static

    data-i18n key would latch the wrong one — same convention as the workspace
    panel toggles. The sync function owns it and applyLocaleToDOM re-syncs."""
    src = _read(INDEX_HTML_PATH)
    tag = re.search(r'id="btnPreviewFullscreen"[^>]*>', src)
    assert tag, "btnPreviewFullscreen not found"
    assert "data-i18n-title=" not in tag.group(0), (
        "the stateful fullscreen button carries a static i18n key"
    )
    assert "data-i18n-aria-label=" not in tag.group(0), (
        "the stateful fullscreen button carries a static i18n aria key"
    )
    i18n = _read(I18N_JS_PATH)
    apply_idx = i18n.index("function applyLocaleToDOM()")
    tail = i18n[apply_idx : apply_idx + 2000]
    assert "_setPreviewFullscreenButtonState" in tail, (
        "a locale change must re-sync the fullscreen button's stateful label"
    )


# ── Greptile (21 Sep, post-fix): the exit must not flush before the caller ────
#
# `setPreviewFullscreen(false)` flushed the queued focus target itself. On the
# clearPreview() path that flush happened while the fullscreen button was STILL
# visible, so focus moved onto it — and clearPreview() then hid that exact
# button, leaving focus stranded and the final flush with no pending target.
# The exit must defer the flush to that caller, while keeping the immediate
# restore for exit paths that leave the preview open.


def test_exit_defers_the_focus_flush_when_the_caller_asks():
    body = _function_with_defaults("setPreviewFullscreen")
    assert "opts.deferFocus" in body, (
        "setPreviewFullscreen must accept a deferFocus option, otherwise a caller "
        "that hides the controls right after cannot prevent the stranded focus"
    )
    flush_line = [l for l in body.split("\n") if "_flushPreviewFullscreenFocus" in l]
    assert flush_line, "the exit no longer flushes focus at all"
    assert all("deferFocus" in l for l in flush_line), (
        "the exit flushes focus unconditionally, so clearPreview() strands it"
    )


def test_clear_preview_defers_the_flush_and_owns_it():
    """The caller hides the controls, so the caller must defer the helper's

    flush and perform the single flush itself afterwards."""
    body = _clear_preview_body()
    compact = body.replace(" ", "")
    assert "setPreviewFullscreen(false,{deferFocus:true})" in compact, (
        "clearPreview() must ask the exit to defer the flush, since it hides the "
        "controls immediately afterwards"
    )
    # Exactly one flush CALL (comments mention the name too, so count code lines
    # only), and it comes after the controls are hidden.
    code_lines = [
        l for l in body.split("\n")
        if not l.strip().startswith("//")
    ]
    flush_calls = [i for i, l in enumerate(code_lines) if "_flushPreviewFullscreenFocus()" in l]
    assert len(flush_calls) == 1, (
        f"clearPreview() must flush exactly once, got {len(flush_calls)}"
    )
    hide_calls = [i for i, l in enumerate(code_lines)
                  if "_showPreviewZoomControls(false, false)" in l]
    assert hide_calls, "clearPreview() no longer hides the preview controls"
    assert flush_calls[0] > hide_calls[0], (
        "clearPreview() flushes focus before the controls are hidden, so focus "
        "lands on a display:none button"
    )


def test_other_exit_paths_still_restore_focus_immediately():
    """Toolbar toggle / document Escape leave the preview open, so the exit must

    still restore focus right away — only the deferring caller opts out."""
    body = _function_with_defaults("setPreviewFullscreen")
    assert "if(!opts.deferFocus) _flushPreviewFullscreenFocus();" in body.replace("  ", " "), (
        "the non-deferring exit path lost its focus restore"
    )
