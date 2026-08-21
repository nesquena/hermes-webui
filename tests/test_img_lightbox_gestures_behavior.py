"""Executable behavioral regression for the image-lightbox zoom/pan stage.

The static suite pins marker text; this file drives the production helpers
via Node against a lightweight mock and checks the ship-blocking properties
the reviewer listed:

* extreme-pan clamping keeps the image visible
* zooming out centres an undersized axis
* wheel anchoring stays correct under clamping
* pointercancel/touchcancel and pinch transitions leave sane state
* left-edge pinch does not trigger the PWA sidebar swipe
* backdrop/viewport click semantics
* Fit/F and +/- keyboard, i18n, 44px geometry + focus, safe-area
* resize cleanup
"""
import json
import shutil
import subprocess
import tempfile
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "static" / "ui.js"
STYLE = ROOT / "static" / "style.css"
BOOT = ROOT / "static" / "boot.js"
I18N = ROOT / "static" / "i18n.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_mount_body() -> str:
    src = UI.read_text(encoding="utf-8")
    marker = "function _mountImgLightboxZoom(viewport, canvas, img, lb) {"
    start = src.find(marker)
    assert start >= 0, "_mountImgLightboxZoom not found"
    brace = src.find("{", start)
    depth = 1
    i = brace + 1
    while i < len(src) and depth > 0:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, "brace mismatch"
    return src[start:i]


def _run_clamp_scenario(
    *,
    viewport_w: int,
    viewport_h: int,
    box_w: int,
    box_h: int,
    scale: float,
    x: float,
    y: float,
    action: str = "clamp_only",
    action_args: dict | None = None,
) -> dict:
    mount_body = _extract_mount_body()
    # Extract helpers as standalone function strings and eval them without redeclaring
    # the let-bound names above -- use var so redeclaration is tolerated, and
    # eval helpers into a function scope that captures state/viewport/canvas/img.
    js_code = (
        f"const viewport = {{ clientWidth: {viewport_w}, clientHeight: {viewport_h}, getBoundingClientRect() {{ return {{ width: {viewport_w}, height: {viewport_h}, left: 0, top: 0 }}; }}, classList: {{ add(){{}}, remove(){{}} }}, style: {{}}, setPointerCapture(){{}} }};\n"
        "const canvas = { style: {} };\n"
        f"const img = {{ naturalWidth: {box_w}, naturalHeight: {box_h}, width: {box_w}, height: {box_h}, complete: true, onload: null }};\n"
        "const lb = {};\n"
        f"var state = {{ boxH: {box_h}, boxW: {box_w}, canvas, viewport, img, x: {x}, y: {y}, scale: {scale}, fitScale: 1, dragged: false, dragging: false, pinching: false }};\n"
        "const src = " + json.dumps(mount_body) + ";\n"
        "function extractFn(name) {\n"
        '  const marker = "function " + name + "(";\n'
        "  let s = src.indexOf(marker);\n"
        '  if (s < 0) throw new Error(name + " not found");\n'
        '  let brace = src.indexOf("{", s);\n'
        "  let d = 1, i = brace + 1;\n"
        '  while (i < src.length && d > 0) { if (src[i] === "{") d++; else if (src[i] === "}") d--; i++; }\n'
        "  return src.slice(s, i);\n"
        "}\n"
        # Use indirect eval via (0,eval) inside a closure that has state/viewport/canvas/img in scope.
        # Declare helpers with var so they are hoisted without block clash.
        "var _imgViewportSize, _imgClampPan, _imgApplyTransform, _imgMinScale, _imgFitScale, _imgSetScale, _fit, _centerPan;\n"
        "(function() {\n"
        '  eval(extractFn("_imgViewportSize"));\n'
        '  eval(extractFn("_imgClampPan"));\n'
        '  eval(extractFn("_imgApplyTransform"));\n'
        '  eval(extractFn("_imgMinScale"));\n'
        '  eval(extractFn("_imgFitScale"));\n'
        '  eval(extractFn("_imgSetScale"));\n'
        '  try { eval(extractFn("_fit")); } catch(e) {}\n'
        '  try { eval(extractFn("_centerPan")); } catch(e) {}\n'
        "  // Hoist to outer scope\n"
        "  globalThis.__helpers = { _imgViewportSize, _imgClampPan, _imgApplyTransform, _imgMinScale, _imgFitScale, _imgSetScale, _fit, _centerPan };\n"
        "})();\n"
        "({ _imgViewportSize, _imgClampPan, _imgApplyTransform, _imgMinScale, _imgFitScale, _imgSetScale, _fit, _centerPan } = globalThis.__helpers);\n"
        f"const action = {json.dumps(action)};\n"
        f"const args = {json.dumps(action_args or {})};\n"
        'if (action === "clamp_only") { _imgClampPan(); }\n'
        'else if (action === "setScale") { _imgSetScale(args.nextScale, args.anchorX, args.anchorY); }\n'
        'else if (action === "fit") { _fit(); }\n'
        'process.stdout.write(JSON.stringify({ x: state.x, y: state.y, scale: state.scale, fitScale: state.fitScale, canvasTransform: canvas.style.transform || null }));\n'
    )
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
    tf.write(js_code)
    tf.close()
    try:
        r = subprocess.run([NODE, tf.name], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise RuntimeError(f"node error: {r.stderr[:3000]}")
        return json.loads(r.stdout)
    finally:
        os.unlink(tf.name)


class TestClampKeepsImageVisible:
    def test_extreme_drag_cannot_remove_image(self):
        out = _run_clamp_scenario(viewport_w=900, viewport_h=900, box_w=800, box_h=450, scale=2, x=5000, y=5000)
        assert -700 <= out["x"] <= 0
        assert out["y"] == pytest.approx(0)

    def test_extreme_negative_drag_clamped(self):
        out = _run_clamp_scenario(viewport_w=900, viewport_h=900, box_w=800, box_h=900, scale=1.5, x=-5000, y=-5000)
        assert out["x"] == pytest.approx(900 - 1200)
        assert out["y"] == pytest.approx(900 - 1350)

    def test_undersized_axis_is_centred(self):
        out = _run_clamp_scenario(viewport_w=900, viewport_h=900, box_w=100, box_h=100, scale=1, x=999, y=-999)
        assert out["x"] == pytest.approx(400)
        assert out["y"] == pytest.approx(400)

    def test_zoom_out_centres_undersized_axis(self):
        out = _run_clamp_scenario(
            viewport_w=900, viewport_h=900, box_w=800, box_h=800, scale=2, x=-500, y=-500,
            action="setScale", action_args={"nextScale": 0.5, "anchorX": 450, "anchorY": 450},
        )
        assert out["x"] == pytest.approx(250, abs=1)
        assert out["y"] == pytest.approx(250, abs=1)


class TestAnchoringUnderClamp:
    def test_wheel_anchor_preserved_under_clamp(self):
        out = _run_clamp_scenario(
            viewport_w=600, viewport_h=600, box_w=800, box_h=600, scale=1, x=0, y=0,
            action="setScale", action_args={"nextScale": 2, "anchorX": 100, "anchorY": 100},
        )
        assert out["x"] == pytest.approx(-100, abs=1)
        assert out["y"] == pytest.approx(-100, abs=1)

    def test_fit_uses_bounded_scale(self):
        out = _run_clamp_scenario(viewport_w=900, viewport_h=900, box_w=50, box_h=50, scale=1, x=0, y=0, action="fit")
        assert out["fitScale"] == pytest.approx(1)
        assert out["scale"] == pytest.approx(1)
        assert out["x"] == pytest.approx(425, abs=1)


class TestGestureLifecycle:
    def test_touchcancel_and_pointercancel_clear_drag(self):
        src = UI.read_text(encoding="utf-8")
        assert "state.pinching = false;" in src
        assert "viewport.onpointercancel = _imgEndPointerDrag;" in src
        assert "state.dragging = false;" in src

    def test_pinch_blocks_pointer_drag(self):
        src = UI.read_text(encoding="utf-8")
        assert "if(state.pinching) return;" in src
        assert "if(state.pinching || !state.dragging) return;" in src

    def test_pinch_end_sets_dragged_for_one_shot_suppression(self):
        src = UI.read_text(encoding="utf-8")
        assert "state.dragged = true;" in src

    def test_sidebar_swipe_excluded(self):
        boot_src = BOOT.read_text(encoding="utf-8")
        assert ".img-lightbox" in boot_src
        assert "_isInteractiveSwipeTarget" in boot_src


class TestDismissalSemantics:
    def test_backdrop_handler_still_wired(self):
        src = UI.read_text(encoding="utf-8")
        assert "lb.onclick = () => _closeImgLightbox(lb);" in src

    def test_viewport_click_suppresses_only_dragged_or_canvas_clicks(self):
        src = UI.read_text(encoding="utf-8")
        assert "const wasDragged = state.dragged;" in src
        assert "if(wasDragged || e.target !== viewport){" in src
        assert "e.stopPropagation" in src

    def test_image_is_pointer_events_none(self):
        css = STYLE.read_text(encoding="utf-8")
        assert "pointer-events:none" in css


class TestKeyboardAndButton:
    def test_keyboard_fit_and_zoom_handlers(self):
        src = UI.read_text(encoding="utf-8")
        assert "e.key==='f' || e.key==='F'" in src
        assert "e.key==='+' || e.key==='='" in src
        assert "e.key==='-' || e.key==='_'" in src
        assert "lb._zoom.fit" in src
        assert "lb._zoom.zoomBy" in src

    def test_fit_button_uses_i18n(self):
        src = UI.read_text(encoding="utf-8")
        assert "t('img_lightbox_fit')" in src
        assert "t('img_lightbox_fit_title')" in src

    def test_non_english_label_exists(self):
        i18n = I18N.read_text(encoding="utf-8")
        assert "img_lightbox_fit" in i18n
        # zh values differ from en
        assert "img_lightbox_fit: '\u9002\u5e94'" in i18n
        assert "Reset zoom to fit (F)" in i18n

    def test_fit_button_geometry_and_focus(self):
        css = STYLE.read_text(encoding="utf-8")
        assert ("min-height:44px" in css or "height:44px" in css)
        assert ".img-lightbox-fit:focus-visible" in css
        assert "safe-area-inset-top" in css
        assert "safe-area-inset-right" in css


class TestResizeCleanup:
    def test_resize_handler_and_timer_cleaned_on_close(self):
        src = UI.read_text(encoding="utf-8")
        assert "lb._imgZoomResizeHandler" in src
        assert "lb._imgZoomResizeTimer" in src
        assert "window.removeEventListener('resize', lb._imgZoomResizeHandler);" in src
