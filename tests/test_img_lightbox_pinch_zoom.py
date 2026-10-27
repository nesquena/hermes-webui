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
import re
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
            "function _imgAnchorPinch(touches) {",
            "state.pinchStartDist = _imgTouchDist(touches);",
            "state.pinchStartScale = state.scale;",
            "state.pinchStartCX",
            "state.pinchStartCY",
            "const currDist = _imgTouchDist(e.touches);",
        ):
            assert marker in src, f"missing pinch marker: {marker}"

    def test_pinch_baseline_follows_the_contact_pair(self):
        """Maintainer re-gate 2026-10-15T17:54:15Z (static/ui.js:3088): the
        baseline belongs to the two contacts it was measured on. Replacing one
        finger mid-pinch (or lifting one of three) used to keep the stale
        baseline, so the next move rescaled and repositioned the image — an
        iPhone pinch dropped from 0.1638 to 0.1260 after a 1px move. The
        identifiers of the pair must be tracked, and the baseline re-anchored
        whenever that pair changes."""
        src = UI.read_text(encoding="utf-8")
        assert "state.pinchIds = _imgPinchPair(touches);" in src, (
            "the anchored pair's identifiers must be recorded with the baseline"
        )
        assert "return [touches[0].identifier, touches[1].identifier];" in src, (
            "the pair must be identified by touch identifier, not by index"
        )
        start = src.index("function _imgOnTouchMove(e) {")
        move = src[start:src.index("function _imgOnTouchEnd(e) {", start)]
        assert "if(!_imgSamePinchPair(e.touches)) _imgAnchorPinch(e.touches);" in move, (
            "a changed contact pair must re-anchor the baseline before the ratio math"
        )
        # The stale pair must not survive the end of a pinch, or a later pinch
        # that happens to reuse the identifiers would keep a dead baseline.
        end = src[src.index("function _imgOnTouchEnd(e) {"):]
        assert "state.pinchIds = null;" in end[:400], (
            "pinch ids must be cleared when the pinch ends"
        )

    def test_touch_updates_need_geometry(self):
        """Maintainer re-gate 2026-10-15T17:54:15Z (static/ui.js:2795): a pinch
        that starts while the image is still decoding must not record a
        baseline against unknown dimensions — the fit that follows would then
        be overwritten by the next touch update (a 4000x3000 image jumped from
        its fitted 351px width to 4080px on an iPhone)."""
        src = UI.read_text(encoding="utf-8")
        start = src.index("function _imgOnTouchStart(e) {")
        body = src[start:src.index("function _imgOnTouchMove(e) {", start)]
        assert "if(!state.boxW || !state.boxH) return;" in body, (
            "touchstart must ignore a gesture before the image has dimensions"
        )
        move = src[src.index("function _imgOnTouchMove(e) {"):src.index("function _imgOnTouchEnd(e) {")]
        assert "if(!state.boxW || !state.boxH) return;" in move, (
            "touchmove must ignore updates before the image has dimensions"
        )

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
        start = src.index("function _imgZoomFromWheel(e) {")
        wheel = src[start:src.index("function _onResize() {", start)]
        assert "const factor = Math.exp((-(Number(e.deltaY) || 0)) * lineScale * 0.0015);" in wheel, (
            "the wheel factor must still be anchored on the event's deltaY"
        )

    def test_wheel_delta_mode_is_normalized(self):
        """Maintainer re-gate 2026-10-15T17:54:15Z (static/ui.js:3133): a
        line- or page-mode wheel event reports a delta of a few units, not
        pixels, so treating it as pixels made a three-line tick zoom by 0.45%.
        The delta's units must be normalized first, with the same 30/600
        multipliers the Mermaid viewer's zoom uses."""
        src = UI.read_text(encoding="utf-8")
        start = src.index("function _imgZoomFromWheel(e) {")
        wheel = src[start:src.index("function _onResize() {", start)]
        assert "const deltaMode = Number(e.deltaMode) || 0;" in wheel, (
            "the wheel handler must read deltaMode"
        )
        assert "const lineScale = deltaMode === 1 ? 30 : deltaMode === 2 ? 600 : 1;" in wheel, (
            "line/page deltas must be scaled to pixels (30/600, the Mermaid recipe)"
        )
        assert "* lineScale * 0.0015" in wheel, (
            "the normalized delta must be what the zoom factor multiplies"
        )


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
        the new natural size and re-clamp the kept scale to the new bounds.
        Fit is carried over, not the raw scale (re-gate 2026-10-08T23:18:39Z,
        static/ui.js:2800): an image that was at its fit must open the new one
        at ITS OWN fit — keeping the numeric scale crops a 4000x3000 successor
        and shrinks a smaller one on the reverse navigation."""
        src = UI.read_text(encoding="utf-8")
        # Scope every probe to _onImgLoad's body: an identical at-fit pattern
        # lives in _onResize (`wasAtFit || !state.boxW`), so a file-wide index
        # would silently match the wrong function.
        load_start = src.index("function _onImgLoad() {")
        load_end = src.index("function _imgOwnsDrag(e) {", load_start)
        load = src[load_start:load_end]
        nav_idx = load.index("if(state.pendingNav){")
        # fitScale must be recomputed inside the pendingNav branch, not only
        # by _fit() on the very first load.
        fit_idx = load.index("state.fitScale = _imgFitScale();")
        assert nav_idx < fit_idx
        # The at-fit decision must be captured BEFORE state.fitScale is
        # overwritten, inside the pendingNav branch.
        at_fit_marker = (
            "const wasAtFit = Math.abs(state.scale - state.fitScale) < 1e-9;"
        )
        assert at_fit_marker in load, (
            "the pendingNav branch must capture whether the previous image was "
            "at its fit before recomputing state.fitScale"
        )
        at_fit_idx = load.index(at_fit_marker)
        assert nav_idx < at_fit_idx < fit_idx, (
            "the at-fit test must be captured in the pendingNav branch before "
            "state.fitScale is recomputed"
        )
        tail = load[fit_idx:load.index("_centerPan();", fit_idx)]
        assert "? state.fitScale" in tail, (
            "an image that was at its fit must open the new image at its own fit"
        )
        # A genuine user zoom is re-clamped against the new image's bounds
        # (min = _imgMinScale(), max = 8) before recentring.
        assert "Math.max(_imgMinScale(), Math.min(8, state.scale))" in tail, (
            "a retained user zoom must be re-clamped to the new image's bounds"
        )
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

    def test_image_fills_canvas_and_keeps_native_context_menu(self):
        """The img must fill its natural-size canvas and stay a real hit
        target so the browser's image context menu (Save image as, Copy
        image, Open image in new tab) keeps working; gestures bubble to the
        viewport anyway."""
        src = STYLE.read_text(encoding="utf-8")
        for line in src.splitlines():
            if line.strip().startswith(".img-lightbox-canvas img{"):
                assert "max-width:none" in line and "max-height:none" in line
                assert "pointer-events:none" not in line
                assert "-webkit-user-drag:none" in line
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
        images, so suppression must be conditional on a prior drag or on the
        interaction belonging to the image pixels.

        The click's target alone cannot decide that: pointer capture (taken in
        _imgOnPointerDown) retargets the trusted click of a real mouse press on
        image pixels to the viewport, so the decision also uses the recorded
        press origin and a hit-test of the click point against the transformed
        canvas (maintainer gate recheck of #6896, 2026-10-06)."""
        src = UI.read_text(encoding="utf-8")
        assert "const wasDragged = state.dragged;" in src
        assert "const pressOnImage = state.pressOnImage;" in src
        assert "state.pressOnImage = !!((e.target && e.target !== viewport) ||" in src
        assert "function _imgPointOnCanvas(x, y) {" in src
        assert "const onImage = pressOnImage ||" in src
        assert "if(wasDragged || onImage){" in src
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
            assert "_mermaidViewerIcon('fit')" in src
            assert "fitBtn.onclick" in src
            assert "lb._zoom.fit" in src
            assert "lb.appendChild(fitBtn);" in src

        def test_fit_button_css(self):
            """The .img-lightbox-fit selector must exist in style.css as a
            36px circle matching the close button, with the counter's dark
            recipe for legibility over light images."""
            css = STYLE.read_text(encoding="utf-8")
            assert ".img-lightbox-fit{" in css
            assert ".img-lightbox-fit:hover" in css
            assert ".img-lightbox-fit svg{" in css
            rule = re.search(r"\.img-lightbox-fit\s*\{[^}]*\}", css).group(0)
            assert "width:36px" in rule
            assert "height:36px" in rule
            assert "border-radius:50%" in rule
            assert "background:rgba(0,0,0,.5)" in rule


class TestRegate20261015:
    """Maintainer re-gate 2026-10-15T17:54:15Z (review 5537610966): Codex drove
    the gesture code on desktop and iPhone (390x844) and found seven silent
    edge cases, each with a reproduction and a small exact fix. Items 3, 5 and
    7 are pinned in the classes above; these are the pointer/resize ones. The
    browser reproduction for EVERY item lives in
    tests/_img_lightbox_composed_checks.js (driven by
    tests/test_img_lightbox_gestures_composed.py) — the `gate1015_*` checks
    fail on the pre-fix head and pass after the fix.
    """

    def test_fit_and_error_cancel_the_gesture_in_flight(self):
        """Item 1 (static/ui.js:2795): a fresh fit owns the geometry and a
        failed load drops it, so both must end any gesture that is in flight —
        a pinch that outlived them rescaled the next frame with a baseline
        measured against the previous one (a 4000x3000 image jumped from its
        fitted 351px width to 4080px on an iPhone)."""
        src = UI.read_text(encoding="utf-8")
        fit = src[src.index("function _fit() {"):src.index("function _centerPan() {")]
        assert "_imgCancelGesture();" in fit, "a fresh fit must cancel the gesture in flight"
        err = src[src.index("function _imgOnError() {"):src.index("function _imgPointOnCanvas(")]
        assert "_imgCancelGesture();" in err, "a failed load must cancel the gesture in flight"

    def test_wheel_zoom_rebases_the_held_pan(self):
        """Item 2 (static/ui.js:3134): a wheel zoom under a held pan must move
        the drag baseline with the image, otherwise the next pointermove
        re-applies the whole accumulated displacement at the new scale (674px
        for a 1px move on desktop)."""
        src = UI.read_text(encoding="utf-8")
        assert "function _imgRebaseDrag() {" in src
        assert "state.dragStartX = state.x - (state.pointerX - state.dragOriginX);" in src
        wheel = src[src.index("function _imgZoomFromWheel(e) {"):src.index("function _onResize() {")]
        assert "_imgRebaseDrag();" in wheel, (
            "the wheel handler must rebase the pan it zoomed under"
        )

    def test_keyboard_zoom_rebases_the_held_pan_and_the_pinch(self):
        """Greptile P2 on head 860c0e85 (2026-10-15T19:09:46Z, static/ui.js:3293,
        "Zoom keys make gestures jump"): the +/- keys zoom through
        ``state.zoomBy``, which is a zoom like the wheel's, so an in-flight pan or
        pinch holds a baseline measured against the pre-zoom geometry. Rebase
        BOTH: a pinch keeps ``pinchStartScale``, so without the pinch rebase the
        next touchmove rescaled from the old scale and undid the keyboard zoom."""
        src = UI.read_text(encoding="utf-8")
        assert "function _imgRebasePinch() {" in src
        pinch = src[src.index("function _imgRebasePinch() {"):src.index("function _imgZoomFromWheel(e) {")]
        assert "state.pinchStartScale = state.scale;" in pinch
        assert "state.pinchStartDist = state.pinchDist;" in pinch
        assert "state.pinchStartCX = state.pinchCX;" in pinch
        assert "state.pinchStartCY = state.pinchCY;" in pinch
        zoom = src[src.index("state.zoomBy = function(factor){"):src.index("state.cancelGesture = _imgCancelGesture;")]
        assert "_imgRebaseDrag();" in zoom, "the keyboard zoom must rebase a held pan"
        assert "_imgRebasePinch();" in zoom, "the keyboard zoom must re-anchor an active pinch"
        # The re-anchor needs the LIVE contact geometry, so both handlers record
        # it (the anchor on a fresh pinch, every move afterwards).
        anchor = src[src.index("function _imgAnchorPinch(touches) {"):src.index("function _imgOnTouchStart(e) {")]
        assert "state.pinchDist = state.pinchStartDist;" in anchor
        assert "state.pinchCX = state.pinchStartCX;" in anchor
        move = src[src.index("function _imgOnTouchMove(e) {"):src.index("function _imgOnTouchEnd(e) {")]
        assert "state.pinchCX = cx;" in move
        assert "state.pinchCY = cy;" in move
        assert "state.pinchDist = currDist;" in move

    def test_drag_baseline_follows_the_clamped_position(self):
        """Item 6 (static/ui.js:2993): the pan is clamped to the stage, and the
        baseline must follow the CLAMPED position — otherwise a drag reversed
        after overshooting an edge does nothing until the overshoot is
        retraced."""
        src = UI.read_text(encoding="utf-8")
        start = src.index("function _imgOnPointerMove(e) {")
        move = src[start:src.index("function _imgEndPointerDrag(e) {", start)]
        apply_idx = move.index("_imgApplyTransform();")
        assert move.index("state.dragStartX = state.x - dx;") > apply_idx, (
            "the baseline must be rebased AFTER the clamped transform"
        )
        assert "state.dragStartY = state.y - dy;" in move

    def test_resize_recomputes_the_fit_baseline(self):
        """Item 4 (static/ui.js:3145): a resize changes the fit, so the zoomed
        branch must recompute state.fitScale before recentring — keeping the
        old baseline left an obsolete zoom-out limit (0.25 after a desktop ->
        iPhone resize whose real fit was 0.08775)."""
        src = UI.read_text(encoding="utf-8")
        start = src.index("function _onResize() {")
        resize = src[start:src.index("viewport.onpointerdown", start)]
        assert "_imgCancelGesture();" in resize, "a resize must end the gesture in flight"
        branch = resize[resize.index("} else {"):]
        fit_idx = branch.index("state.fitScale = _imgFitScale();")
        center_idx = branch.index("_centerPan();")
        assert fit_idx < center_idx, "the fit baseline must be recomputed before recentring"
        assert "Math.max(_imgMinScale(), Math.min(8, state.scale))" in branch, (
            "the chosen scale must be re-clamped to the new baseline"
        )


class TestGreptilePinchEndDrag20261016:
    """Greptile P2 on the merge-only head 6f462d82 (2026-10-16T01:39:15Z,
    static/ui.js:3235, "Dragging stops after pinching"): lifting one finger of a
    pinch left the contact still down unable to pan, because the pinch had ended
    the pointer drag and that contact never receives a new pointerdown. The pan
    must be re-armed from it, which needs the touch contacts tied to the pointer
    ids (the touch stream carries no id), and the id bookkeeping must not sit
    behind the early returns of ``_imgEndPointerDrag``.
    """

    def test_the_pan_is_re_armed_from_the_remaining_contact(self):
        src = UI.read_text(encoding="utf-8")
        assert "touchPointerIds" in src, (
            "the touch contacts must be tied to their pointer ids"
        )
        end = src[src.index("function _imgOnTouchEnd(e) {"):]
        body = end[: end.index("function _imgTrackTouchPointer(")]
        assert "_imgArmDragFromRemainingTouch(e.touches);" in body, (
            "the pinch end must hand the pan to the contact still down"
        )
        arm = src[src.index("function _imgArmDragFromRemainingTouch(touches) {"):]
        arm = arm[: arm.index("function _imgRebaseDrag() {")]
        assert "if(state.dragging) return;" in arm, (
            "a drag that survived the pinch keeps its own baseline"
        )
        assert "state.dragStartX = state.x;" in arm and "state.dragStartY = state.y;" in arm, (
            "the re-armed pan must be anchored on the frame the pinch produced"
        )

    def test_the_contact_bookkeeping_precedes_the_drag_guards(self):
        """`_imgEndPointerDrag` early-returns when the pinch already cleared
        `dragging`, so the id must leave the tracked set before those guards —
        otherwise the set goes stale and the wrong contact owns the pan."""
        src = UI.read_text(encoding="utf-8")
        body = src[src.index("function _imgEndPointerDrag(e) {"):]
        body = body[: body.index("function _imgCancelGesture() {")]
        track = body.index("_imgTrackTouchPointer(e.pointerId, false);")
        assert track < body.index("if(!state.dragging) return;"), (
            "the tracking must run before the drag guards"
        )
        down = src[src.index("function _imgOnPointerDown(e) {"):]
        down = down[: down.index("function _imgOnPointerMove(e) {")]
        assert down.index("_imgTrackTouchPointer(e.pointerId, true);") < down.index(
            "if(state.pinching) return;"
        ), "the tracking must run before the pinch guard"
