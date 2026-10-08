"""Source-level secondary guards for image-lightbox gestures.

The behavioral proof now lives in ``test_img_lightbox_gestures_composed.py``,
which drives the real lightbox in Chromium (Playwright) through the listeners
the application registers. This file keeps cheap source locks as a secondary
regression net so a marker regression fails fast without launching a browser.

No dynamic code evaluation is used here — the threat scan treats that pattern
as SUSPICIOUS.
"""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "static" / "ui.js"
STYLE = ROOT / "static" / "style.css"
BOOT = ROOT / "static" / "boot.js"
I18N = ROOT / "static" / "i18n.js"


def _fit_rule() -> str:
    css = STYLE.read_text(encoding="utf-8")
    m = re.search(r"\.img-lightbox-fit\s*\{[^}]*\}", css)
    assert m, ".img-lightbox-fit rule not found"
    return m.group(0)


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
        assert "const pressOnImage = state.pressOnImage;" in src
        assert "const onImage = pressOnImage ||" in src
        assert "function _imgPointOnCanvas(x, y) {" in src
        assert "state.pressOnImage = !!((e.target && e.target !== viewport) ||" in src
        assert "e.stopPropagation" in src

    def test_image_keeps_pointer_events_for_native_context_menu(self):
        css = STYLE.read_text(encoding="utf-8")
        line = next(l for l in css.splitlines() if l.strip().startswith(".img-lightbox-canvas img{"))
        assert "pointer-events:none" not in line


class TestMaintainerGate20261006:
    """Source locks for the 2026-10-06 gate-certification fixes on #6896.

    The behavioural proof lives in ``test_img_lightbox_gestures_composed.py``
    (real Chromium input). These locks keep a marker regression failing fast
    without launching a browser.
    """

    def test_pointer_capture_retargeting_is_compensated(self):
        src = UI.read_text(encoding="utf-8")
        # The press origin is recorded before the capture is taken...
        assert "state.pressOnImage = !!((e.target && e.target !== viewport) ||" in src
        # ...and only for the press that owns the gesture: the guards must come
        # first, otherwise a refused second pointer re-classifies the active
        # gesture and can block a letterbox dismissal (greptile review of
        # #6896, 2026-10-05).
        start = src.index("function _imgOnPointerDown(e) {")
        end = src.index("function _imgOnPointerMove(e) {", start)
        down = src[start:end]
        guard_at = down.index("if(state.dragging) return;")
        record_at = down.index("state.pressOnImage = !!((e.target && e.target !== viewport) ||")
        assert guard_at < record_at, (
            "the gesture-ownership guards must precede the press-origin recording"
        )
        # ...and the click decision falls back to it plus a geometric hit-test
        # of the transformed canvas, because pointer capture retargets the
        # trusted click on image pixels to the viewport.
        start = src.index("function _onViewportClick(e) {")
        end = src.index("function _imgTouchDist(", start)
        body = src[start:end]
        assert "function _imgPointOnCanvas(x, y) {" in src
        assert "const onImage = pressOnImage ||" in body
        assert "_imgPointOnCanvas(Number(e.clientX), Number(e.clientY))" in body
        assert "if(wasDragged || onImage){" in body
        # The old target-only test is gone: it treated a retargeted image click
        # as a letterbox press and dismissed the dialog.
        assert "e.target !== viewport){" not in body

    def test_browser_shortcut_modifiers_are_not_hijacked(self):
        src = UI.read_text(encoding="utf-8")
        start = src.index("// Single keyboard handler")
        end = src.index("document.addEventListener('keydown', lb._keyHandler);", start)
        body = src[start:end]
        guard_at = body.index("if(e.ctrlKey || e.metaKey || e.altKey) return;")
        for shortcut in (
            "e.key==='f' || e.key==='F'",
            "e.key==='+' || e.key==='='",
            "e.key==='-' || e.key==='_'",
        ):
            assert guard_at < body.index(shortcut), (
                f"the modifier guard must precede the {shortcut} shortcut"
            )


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
        # Icon-only control (maintainer visual review of #6896, 2026-10-08):
        # the accessible name is the translated title and the glyph is the
        # shared Mermaid `fit` icon, so it needs no visible text of its own.
        assert "t('img_lightbox_fit_title')" in src
        assert "_mermaidViewerIcon('fit')" in src

    def test_non_english_label_exists(self):
        i18n = I18N.read_text(encoding="utf-8")
        assert "img_lightbox_fit_title" in i18n
        assert "img_lightbox_fit_title: '\u91cd\u7f6e\u7f29\u653e\u4ee5\u9002\u5e94 (F)'" in i18n
        assert "Reset zoom to fit (F)" in i18n

    def test_fit_button_geometry_and_focus_scoped_to_rule(self):
        rule = _fit_rule()
        # A 36px circle matching .img-lightbox-close -- the old 44px text pill
        # overlapped the image band and lost legibility over light images.
        assert "width:36px" in rule, "fit rule must be a 36px circle"
        assert "height:36px" in rule, "fit rule must be a 36px circle"
        assert "border-radius:50%" in rule, "fit rule must match the close circle"
        # The same dark recipe as the counter, so both controls stay legible
        # over light images at every width.
        assert "background:rgba(0,0,0,.5)" in rule, "fit rule must use the counter's dark recipe"
        css = STYLE.read_text(encoding="utf-8")
        assert ".img-lightbox-fit:focus-visible" in css
        # The shared Mermaid `fit` glyph is stroked, not filled.
        assert ".img-lightbox-fit svg{" in css
        assert "safe-area-inset-top" in css
        assert "safe-area-inset-right" in css
        # rule itself must reference safe-area so the button, not some other element, is safe-area-aware
        assert "env(safe-area-inset-" in rule


class TestResizeCleanup:
    def test_resize_handler_and_timer_cleaned_on_close(self):
        src = UI.read_text(encoding="utf-8")
        assert "lb._imgZoomResizeHandler" in src
        assert "lb._imgZoomResizeTimer" in src
        assert "window.removeEventListener('resize', lb._imgZoomResizeHandler);" in src


class TestReviewFollowups20261005:
    """Source locks for the 2026-10-05 greptile review follow-ups on #6896.

    The behavioural proof for each of these lives in
    ``test_img_lightbox_gestures_composed.py`` (real Chromium). These locks keep
    a marker regression failing fast without launching a browser.
    """

    def _img_pointer_section(self) -> str:
        """The lightbox pointer-handler block only (not the Mermaid viewer's)."""
        src = UI.read_text(encoding="utf-8")
        start = src.index("function _imgOwnsDrag(e) {")
        end = src.index("function _onViewportClick(e) {")
        return src[start:end]

    def test_second_pointer_cannot_take_over_the_pan(self):
        section = self._img_pointer_section()
        # A press while a pan is already active is refused, so a second
        # pointer can never re-anchor dragOrigin/dragStart (which teleported
        # the image to the new pointer before).
        assert "if(state.dragging) return;" in section
        assert "state.dragPointerId = e.pointerId != null ? e.pointerId : null;" in section
        # Move/end only serve the owning pointer, and the capture is released
        # with the owning pointer's id when the drag really ends.
        assert "function _imgOwnsDrag(e) {" in section
        assert "if(!_imgOwnsDrag(e)) return;" in section
        assert "viewport.releasePointerCapture" in section
        assert "state.dragPointerId = null;" in section

    def test_keyboard_shortcuts_ignore_editable_targets(self):
        src = UI.read_text(encoding="utf-8")
        start = src.index("// Single keyboard handler")
        end = src.index("document.addEventListener('keydown', lb._keyHandler);", start)
        body = src[start:end]
        # Escape stays first (modal convention), then the editable-target
        # bail-out must come BEFORE every zoom/navigation shortcut so typing
        # in a background field is never hijacked.
        assert "if(e.key==='Escape'){ _closeImgLightbox(lb); return; }" in body
        assert "tgt.isContentEditable" in body
        assert "tag === 'input' || tag === 'textarea' || tag === 'select'" in body
        guard_at = body.index("tgt.isContentEditable")
        for shortcut in (
            "e.key==='f' || e.key==='F'",
            "e.key==='+' || e.key==='='",
            "e.key==='-' || e.key==='_'",
            "e.key==='ArrowLeft'",
        ):
            assert guard_at < body.index(shortcut), (
                f"the editable-target guard must precede the {shortcut} shortcut"
            )

    def test_focus_moves_into_the_dialog_and_is_restored(self):
        src = UI.read_text(encoding="utf-8")
        assert "lb.setAttribute('tabindex', '-1');" in src
        # The dialog grabs focus only after it is in the DOM, and the opener
        # is remembered for restoration on close.
        assert src.index("lb._restoreFocus = (") > src.index("document.body.appendChild(lb);")
        assert "if(typeof lb.focus === 'function')" in src
        assert "document.contains(lb._restoreFocus)" in src
        assert "lb._restoreFocus = null;" in src
        # The container focus must not paint an outline over the backdrop.
        css = STYLE.read_text(encoding="utf-8")
        for line in css.splitlines():
            if line.strip().startswith(".img-lightbox{"):
                assert "outline:none" in line, ".img-lightbox must not outline its programmatic focus"
                break
        else:
            raise AssertionError(".img-lightbox selector not found in style.css")

    def test_failed_load_clears_the_stale_stage(self):
        src = UI.read_text(encoding="utf-8")
        start = src.index("function _imgOnError() {")
        end = src.index("function _imgOnPointerDown(e) {")
        body = src[start:end]
        assert "img.onerror = _imgOnError;" in src
        for marker in (
            "state.pendingNav = false;",
            "state.boxW = 0;",
            "state.boxH = 0;",
            "canvas.style.width = '';",
            "canvas.style.height = '';",
            "canvas.style.transform = '';",
        ):
            assert marker in body, f"_imgOnError must reset {marker}"
        # The user's zoom level belongs to the navigation contract ("keep the
        # current zoom level when switching images"), so the error path must
        # drop the geometry WITHOUT resetting state.scale.
        assert "state.scale = 1;" not in body, (
            "_imgOnError must not reset the user's zoom level (greptile follow-up)"
        )


class TestMaintainerReworkup20261006:
    """Source locks for the 2026-10-06 re-warmup finding on #6896.

    The behavioural proof (a real mouse-down → navigation → small move in
    Chromium) lives in ``test_img_lightbox_gestures_composed.py``. These locks
    keep a marker regression failing fast without launching a browser.
    """

    def test_navigation_cancels_the_in_flight_pan(self):
        src = UI.read_text(encoding="utf-8")
        # The cancel hook exists and is exposed on the mount's state object.
        assert "function _imgCancelGesture() {" in src
        assert "state.cancelGesture = _imgCancelGesture;" in src
        # _navigateLightbox cancels BEFORE the src swap, so the old gesture
        # cannot outlive the image whose coordinate space it was recorded in.
        start = src.index("function _navigateLightbox(lb, direction) {")
        end = src.index("function _closeImgLightbox(lb) {", start)
        nav = src[start:end]
        cancel_at = nav.index(
            "if(lb._zoom && lb._zoom.cancelGesture) lb._zoom.cancelGesture();"
        )
        swap_at = nav.index("lbImg.src = nextImg.src;")
        assert cancel_at < swap_at, (
            "the navigation must cancel the in-flight pan before swapping the image"
        )
        # A press armed while the new image was still loading is dropped by the
        # pendingNav re-centre as well.
        start = src.index("function _onImgLoad() {")
        end = src.index("function _imgOwnsDrag(e) {", start)
        load = src[start:end]
        assert load.index("_centerPan();") < load.index("_imgCancelGesture();"), (
            "the pendingNav re-centre must drop a gesture armed against the "
            "previous geometry"
        )
        # Cancelling is geometry-only: the navigation contract keeps zoom.
        helper = src[
            src.index("function _imgCancelGesture() {"):
            src.index("function _onViewportClick(e) {")
        ]
        assert "state.scale" not in helper, (
            "_imgCancelGesture must not touch the user's zoom level"
        )
        # ...and it must NOT release the pointer capture: releasing it lets a
        # held-button release outside the viewport land on the backdrop and
        # dismiss the dialog (greptile review of #6896, 2026-10-06). The
        # browser releases the capture implicitly when the pointer goes up.
        assert "releasePointerCapture" not in helper, (
            "_imgCancelGesture must keep the pointer capture (greptile P2)"
        )
        assert "state.dragging = false;" in helper

    def test_navigation_also_cancels_the_in_flight_pinch(self):
        src = UI.read_text(encoding="utf-8")
        helper = src[
            src.index("function _imgCancelGesture() {"):
            src.index("function _onViewportClick(e) {")
        ]
        # A two-finger pinch keeps pinching/pinchStart* alive and only clears
        # dragging (_imgOnTouchStart), so the drag-only early return used to let
        # an in-flight zoom outlive a navigation; the next _imgOnTouchMove then
        # applied the previous image's pinchStart baselines to the freshly
        # centred new image (maintainer review of #6896, 2026-10-06: two fingers
        # down, ArrowRight, spread -> 544px jump).
        assert "state.pinching = false;" in helper, (
            "the image-change cancel must end an in-flight pinch too"
        )
        # The pinch reset must precede the drag-only early return, which a pinch
        # never satisfies.
        assert helper.index("state.pinching = false;") < helper.index(
            "if(!state.dragging) return;"
        ), "the pinch reset must come before the drag-only early return"


class TestRegate20261008:
    """Source locks for the 2026-10-08 re-gate item on #6896.

    " Cached image previews silently lose selected zoom. " — a cached image
    initializes synchronously at mount (``img.complete``) and the browser still
    dispatches its queued ``load`` afterwards, which re-ran the initializer
    through ``_fit()`` and dropped the user's zoom (observed 1.7578125 -> 0.9).
    The behavioural proof lives in the composed Chromium checks
    (``duplicate_load_keeps_zoom``); these locks keep a marker regression
    failing fast without a browser.
    """

    def _on_load(self) -> str:
        src = UI.read_text(encoding="utf-8")
        return src[
            src.index("function _onImgLoad() {"):
            src.index("function _imgOwnsDrag(e) {")
        ]

    def test_on_img_load_is_idempotent_for_an_initialized_frame(self):
        body = self._on_load()
        # The initialized frame is identified by source + natural size...
        assert "let _imgInitSrc = null;" in UI.read_text(encoding="utf-8")
        assert "const src = img.currentSrc || img.src || '';" in body
        assert (
            "if(!state.pendingNav && _imgInitSrc === src && _imgInitW === nw && _imgInitH === nh){"
            in body
        ), "a duplicate load for the already initialized frame must return early"
        # ...recorded on EVERY initialization (mount, navigation, duplicate-of-
        # a-new-frame), so the next duplicate can be recognised.
        assert "_imgInitSrc = src;" in body
        assert "_imgInitW = nw;" in body
        assert "_imgInitH = nh;" in body
        # A navigation must still initialize (that is the new image's geometry):
        # the early return is gated on `pendingNav` being false.
        guard = body.index(
            "if(!state.pendingNav && _imgInitSrc === src && _imgInitW === nw && _imgInitH === nh){"
        )
        assert body.index("state.boxW = nw || 800;") > guard

    def test_stale_event_does_not_consume_the_pending_navigation(self):
        body = self._on_load()
        # The reported ordering: the late event lands after the src swap. It
        # cannot be the frame that navigation asked for (a src swap reports its
        # own source), so it must leave pendingNav armed for the real load.
        # Consuming it made the next load re-fit and lost the zoom.
        stale = body.index("if(state.pendingNav && src && _imgInitSrc === src")
        consume = body.index("state.pendingNav = false;", body.index("state.boxW = nw || 800;"))
        assert stale < consume, "the stale-event guard must run before pendingNav is consumed"

    def test_img_error_clears_the_initialized_frame_tracking(self):
        src = UI.read_text(encoding="utf-8")
        err = src[
            src.index("function _imgOnError() {"):
            src.index("function _imgPointOnCanvas(")
        ]
        assert "_imgInitSrc = null;" in err, (
            "a failed load must drop the initialized-frame tracking, otherwise a "
            "later load of the same source is skipped as a duplicate"
        )
        assert "_imgInitW = 0;" in err
        assert "_imgInitH = 0;" in err
