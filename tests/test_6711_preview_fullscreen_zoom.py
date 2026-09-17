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
    fs = extract_function(_read(WORKSPACE_JS_PATH), "setPreviewFullscreen")
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
    body = _function("_previewFullscreenExit")
    assert "removeAttribute('inert')" in body or 'removeAttribute("inert")' in body, (
        "_previewFullscreenExit must lift inert from the restored chrome"
    )
    assert ".focus()" in body, (
        "_previewFullscreenExit must move focus somewhere visible so it is never "
        "dropped onto <body>"
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


def test_resize_and_fullscreen_exit_call_the_reconciler():
    """The reconciler only helps if the real lifecycle paths invoke it."""
    boot = _read(BOOT_JS_PATH)
    idx = boot.find("window.addEventListener('resize'")
    assert idx != -1, "resize listener not found"
    assert "_reconcileWorkspacePanelBreakpoint" in boot[idx:idx + 500], (
        "the resize path must reconcile the panel breakpoint"
    )
    ws = _read(WORKSPACE_JS_PATH)
    fs_exit = extract_function(ws, "setPreviewFullscreen")
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
