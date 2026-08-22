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

    def test_navigation_recomputes_fit_baseline_for_new_image(self):
        """Navigation keeps the user's zoom level, but the fit baseline
        belongs to the NEW image: a stale fitScale from the previous image
        skews _imgMinScale() (zoom-out clamp) and the at-fit resize
        comparison. A small first image (fit 1.0) followed by a huge one
        (fit 0.1) would otherwise clamp zoom-out at 0.25 and make the real
        fit unreachable. The pendingNav branch must recompute fitScale from
        the new natural size and re-clamp the kept scale to the new bounds."""
        src = UI.read_text(encoding="utf-8")
        nav_idx = src.index("if(state.pendingNav){")
        # fitScale must be recomputed inside the pendingNav branch, not only
        # by _fit() on the very first load.
        fit_idx = src.index("state.fitScale = _imgFitScale();")
        assert nav_idx < fit_idx
        # The kept scale must be re-clamped against the new image's bounds
        # (min = _imgMinScale(), max = 8) before recentring.
        assert "state.scale = Math.max(_imgMinScale(), Math.min(8, state.scale));" in src
        # The fit formula must live in one shared helper so _fit() and the
        # pendingNav branch can never drift apart.
        assert "function _imgFitScale() {" in src
        assert "state.fitScale = fitScale;" in src


class TestInitialFitBounded:
    def test_fit_scale_capped_at_no_upscale(self):
        """_imgFitScale() must never exceed 1: the pre-PR lightbox
        (img{max-width:90vw;max-height:90vh;object-fit:contain}) constrained
        oversized images but left small ones at intrinsic size. An unbounded
        fit ratio on a tiny image (e.g. 50px in a 900px viewport -> 18)
        would exceed the feature's 8x zoom max and make the first wheel
        gesture snap from 18 down to 8 instead of zooming smoothly. The
        shared helper must store the bounded value in state.fitScale, which
        _imgMinScale() and the at-fit resize comparison depend on."""
        src = UI.read_text(encoding="utf-8")
        assert "return Math.min(1, size.width / state.boxW, size.height / state.boxH);" in src

    def test_fit_assigns_bounded_scale_to_state(self):
        """_fit() must store the bounded helper value in both state.fitScale
        (baseline for _imgMinScale / the at-fit resize check) and
        state.scale (the applied transform), so the initial view is never
        above the interactive 8x maximum and the first wheel gesture starts
        smoothly from the fitted scale."""
        src = UI.read_text(encoding="utf-8")
        fit_idx = src.index("const fitScale = _imgFitScale();")
        assert "state.fitScale = fitScale;" in src
        assert "state.scale = fitScale;" in src
        assert src.index("state.scale = fitScale;") > fit_idx


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


    class TestPanClamped:
        def test_clamp_function_exists(self):
            """_imgClampPan must exist and be called before every transform
            so the image can never be dragged wholly out of the clipped viewport
            with no reset path (the previous behaviour)."""
            src = UI.read_text(encoding="utf-8")
            assert "function _imgClampPan()" in src
            assert "_imgClampPan();" in src

        def test_clamp_centers_undersized_axis(self):
            """When the scaled image fits entirely inside the viewport on an
            axis, _imgClampPan must center that axis so the image stays fully
            visible and reachable without manual panning."""
            src = UI.read_text(encoding="utf-8")
            # The axis-centering formula: (size.width - scaledW) / 2
            assert "state.x = (size.width - scaledW) / 2;" in src
            assert "state.y = (size.height - scaledH) / 2;" in src

        def test_clamp_bounds_overflow_axis(self):
            """When the scaled image is larger than the viewport on an axis,
            _imgClampPan must clamp the offset between -(scaled - viewport) and
            0 so the image edge never moves past the viewport edge."""
            src = UI.read_text(encoding="utf-8")
            assert "state.x = Math.max(size.width - scaledW, Math.min(0, state.x));" in src
            assert "state.y = Math.max(size.height - scaledH, Math.min(0, state.y));" in src


    class TestSidebarSwipeExcludesLightbox:
        def test_img_lightbox_in_swipe_target(self):
            """_isInteractiveSwipeTarget must include .img-lightbox so the
            window-level PWA sidebar swipe recogniser does not claim touches
            that start inside the lightbox's left-edge viewport during a
            two-finger pinch."""
            src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
            assert ".img-lightbox" in src
            assert "_isInteractiveSwipeTarget" in src


    class TestKeyboardAccessibleZoom:
        def test_fit_keyboard_shortcut(self):
            """Pressing F must call lb._zoom.fit() to reset the zoom to fit,
            making the zoom control keyboard-operable rather than pointer-only."""
            src = UI.read_text(encoding="utf-8")
            assert "e.key==='f' || e.key==='F'" in src
            assert "lb._zoom && lb._zoom.fit" in src

        def test_zoom_plus_minus_shortcuts(self):
            """Pressing +/- must zoom in/out toward the viewport centre
            via lb._zoom.zoomBy, exposing zoom control to the keyboard."""
            src = UI.read_text(encoding="utf-8")
            assert "e.key==='+' || e.key==='='" in src
            assert "e.key==='-' || e.key==='_'" in src
            assert "lb._zoom.zoomBy" in src

        def test_fit_and_zoomBy_exposed_on_state(self):
            """The mount return must expose fit() and zoomBy(factor) so the
            keyboard handler and the in-DOM fit button can drive the zoom
            without pointer interaction."""
            src = UI.read_text(encoding="utf-8")
            assert "state.fit = _fit;" in src
            assert "state.zoomBy = function(factor)" in src


    class TestFitButtonPresent:
        def test_fit_button_created_in_lightbox(self):
            """A visible fit/reset button must be created inside the lightbox
            so users can reset the zoom without relying on gestures."""
            src = UI.read_text(encoding="utf-8")
            assert "fitBtn" in src
            assert "fitBtn.className = 'img-lightbox-fit';" in src
            assert "fitBtn.onclick" in src
            assert "lb._zoom.fit" in src
            assert "lb.appendChild(fitBtn);" in src

        def test_fit_button_css(self):
            """The .img-lightbox-fit selector must exist in style.css with
            positioning and hover state matching the lightbox button style."""
            css = STYLE.read_text(encoding="utf-8")
            assert ".img-lightbox-fit{" in css
            assert ".img-lightbox-fit:hover" in css
