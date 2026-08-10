"""Static regression coverage for pinch/pan/zoom gestures in the image lightbox.

The enlarged-image lightbox (open via _openImgLightbox on any .msg-media-img
or .attach-thumb) previously rendered a plain <img> with no zoom support —
touch users could not pinch to zoom, matching the exact gap the Mermaid
viewer had before its gesture work. This PR ports the Mermaid viewer's
gesture architecture (Pointer Events for single-finger pan, Touch Events
for two-finger pinch, wheel for cursor-anchored zoom) to the image
lightbox stage.

This file pins the wiring at the source level, mirroring
tests/test_issue4075_mermaid_lightbox.py.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "static" / "ui.js"
STYLE = ROOT / "static" / "style.css"


class TestZoomableStageStructure:
    def test_lightbox_builds_viewport_canvas_img_stage(self):
        """_openImgLightboxWithNav must build viewport > canvas > img so the
        canvas carries the transform (zoom/pan) inside a clipping viewport."""
        src = UI.read_text(encoding="utf-8")
        assert "const viewport = document.createElement('div');" in src
        assert "viewport.className = 'img-lightbox-viewport';" in src
        assert "canvas.className = 'img-lightbox-canvas';" in src
        assert "canvas.appendChild(img);" in src
        assert "viewport.appendChild(canvas);" in src

    def test_zoom_mount_happens_after_stage_is_in_dom(self):
        """The gesture mount must run AFTER document.body.appendChild(lb):
        a synchronously decoded image (data: URL / cache hit) triggers the
        initial fit inside the mount call, and a detached viewport measures
        0x0, which fit-zooms the image to a near-zero scale."""
        src = UI.read_text(encoding="utf-8")
        mount_line = "lb._zoom = _mountImgLightboxZoom(viewport, canvas, img, lb);"
        assert mount_line in src
        assert src.index(mount_line) > src.index("document.body.appendChild(lb);")

    def test_dedicated_zoom_mount_helper_exists(self):
        src = UI.read_text(encoding="utf-8")
        assert "function _mountImgLightboxZoom(viewport, canvas, img, lb) {" in src

    def test_img_draggable_disabled(self):
        """Native image drag would fight the pan gesture — must be disabled."""
        src = UI.read_text(encoding="utf-8")
        assert "img.draggable = false;" in src


class TestTouchPinchZoom:
    def test_touch_listeners_non_passive(self):
        """touchstart/touchmove need {passive:false} so preventDefault works
        (same requirement as the Mermaid viewer)."""
        src = UI.read_text(encoding="utf-8")
        assert "viewport.addEventListener('touchstart', _imgOnTouchStart, {passive: false});" in src
        assert "viewport.addEventListener('touchmove', _imgOnTouchMove, {passive: false});" in src

    def test_pinch_state_machinery_present(self):
        src = UI.read_text(encoding="utf-8")
        for marker in (
            "state.pinchStartDist = _imgTouchDist(e.touches);",
            "state.pinchStartScale = state.scale;",
            "state.pinchStartCX",
            "state.pinchStartCY",
            "const currDist = _imgTouchDist(e.touches);",
        ):
            assert marker in src, f"missing pinch marker: {marker}"

    def test_two_finger_start_ends_pointer_drag(self):
        src = UI.read_text(encoding="utf-8")
        assert "state.pinching = true;" in src
        assert "_imgEndPointerDrag();" in src

    def test_touchcancel_resets_pinching(self):
        """Without touchcancel, pinching could stay true and block Pointer
        events permanently after a system interrupt."""
        src = UI.read_text(encoding="utf-8")
        assert "touchcancel', function _imgOnTouchCancel(){ state.pinching = false; })" in src


class TestPointerPanAndGuards:
    def test_pointer_handlers_declared(self):
        src = UI.read_text(encoding="utf-8")
        for marker in (
            "viewport.onpointerdown = _imgOnPointerDown;",
            "viewport.onpointermove = _imgOnPointerMove;",
            "viewport.onpointerup = _imgEndPointerDrag;",
            "viewport.onpointercancel = _imgEndPointerDrag;",
            "viewport.onpointerleave = _imgEndPointerDrag;",
        ):
            assert marker in src, f"missing pointer handler: {marker}"

    def test_pointer_handlers_guarded_by_pinching(self):
        """While a two-finger pinch is active the Pointer handlers must step
        aside so the two event systems never fight."""
        src = UI.read_text(encoding="utf-8")
        assert src.count("if(state.pinching) return;") >= 2

    def test_drag_uses_pointer_capture(self):
        src = UI.read_text(encoding="utf-8")
        assert "viewport.setPointerCapture(e.pointerId)" in src


class TestWheelZoom:
    def test_wheel_zooms_anchored_at_cursor(self):
        src = UI.read_text(encoding="utf-8")
        assert "viewport.onwheel = _imgZoomFromWheel;" in src
        assert "const factor = Math.exp((-(Number(e.deltaY) || 0)) * 0.0015);" in src


class TestNavigationKeepsZoom:
    def test_navigate_sets_pending_nav_flag(self):
        """Switching images must keep the current zoom level; the zoom state
        re-centres once the new image loads."""
        src = UI.read_text(encoding="utf-8")
        assert "if(lb._zoom) lb._zoom.pendingNav = true;" in src
        assert "state.pendingNav = false;" in src
        assert "if(state.pendingNav){" in src


class TestCleanup:
    def test_close_removes_img_zoom_resize_listener(self):
        """_closeImgLightbox must tear down the image-zoom resize listener so
        closing the lightbox does not leak a window listener."""
        src = UI.read_text(encoding="utf-8")
        assert "lb._imgZoomResizeHandler" in src
        assert "window.removeEventListener('resize', lb._imgZoomResizeHandler);" in src
        assert "lb._imgZoomResizeTimer" in src


class TestZoomableStageCss:
    def test_viewport_clips_and_disables_native_touch_actions(self):
        src = STYLE.read_text(encoding="utf-8")
        for line in src.splitlines():
            if line.strip().startswith(".img-lightbox-viewport{"):
                assert "touch-action:none" in line, "viewport must set touch-action:none"
                assert "overflow:hidden" in line, "viewport must clip the canvas"
                assert "cursor:grab" in line, "viewport must advertise grab"
                break
        else:
            raise AssertionError(".img-lightbox-viewport selector not found in style.css")

    def test_canvas_is_transformable_absolute_stage(self):
        src = STYLE.read_text(encoding="utf-8")
        for line in src.splitlines():
            if line.strip().startswith(".img-lightbox-canvas{"):
                assert "position:absolute" in line
                assert "transform-origin:0 0" in line
                assert "left:0" in line and "top:0" in line
                break
        else:
            raise AssertionError(".img-lightbox-canvas selector not found in style.css")

    def test_image_fills_canvas_and_does_not_capture_pointer(self):
        """The img must fill its natural-size canvas and let pointer events
        fall through to the viewport so gestures stay on one element."""
        src = STYLE.read_text(encoding="utf-8")
        for line in src.splitlines():
            if line.strip().startswith(".img-lightbox-canvas img{"):
                assert "max-width:none" in line and "max-height:none" in line
                assert "pointer-events:none" in line
                break
        else:
            raise AssertionError(".img-lightbox-canvas img selector not found in style.css")


class TestBackdropDismissalPreserved:
    def test_undragged_viewport_click_bubbles_to_backdrop(self):
        """The gesture viewport covers 90vw x 90vh and the img is
        pointer-events:none, so an undragged click on the viewport's
        letterboxed area must bubble to the lightbox backdrop handler
        (lb.onclick -> _closeImgLightbox). Unconditionally stopping
        propagation here regressed close-on-backdrop for small/portrait
        images, so suppression must be conditional on a prior drag or on a
        non-viewport target (the transformed canvas)."""
        src = UI.read_text(encoding="utf-8")
        assert "const wasDragged = state.dragged;" in src
        assert "if(wasDragged || e.target !== viewport){" in src
        assert "if(e.stopPropagation) e.stopPropagation();" in src
        # Backdrop close handler must still be wired on the lightbox root.
        assert "lb.onclick = () => _closeImgLightbox(lb);" in src

    def test_drag_flag_reset_on_viewport_click(self):
        """state.dragged must be consumed and cleared on every viewport
        click so the post-drag suppression is one-shot per gesture."""
        src = UI.read_text(encoding="utf-8")
        assert "const wasDragged = state.dragged;" in src
        assert "state.dragged = false;" in src
