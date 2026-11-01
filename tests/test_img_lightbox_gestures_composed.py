"""Real-browser behavioural regression for the image-lightbox gestures.

The behavioural proof runs in Chromium (Playwright) against the real
application page the pytest session already serves: the production lightbox
is opened through ``_openImgLightboxWithNav`` and driven through the listeners
the application itself registered — real ``PointerEvent`` / ``WheelEvent`` /
``TouchEvent`` objects at the gesture viewport, a real ``click`` on the Fit
button, and real ``KeyboardEvent``s on ``document`` for the keydown handler.

Covers the six review-required points:

1. extreme drag clamping & undersized-axis centring via the pointer path
2. wheel and two-finger pinch anchoring via the production handlers
3. left-edge touch inside the lightbox never arms the sidebar swipe
   recogniser, with a positive control proving the recogniser is live
4. Fit click + F, +/=, -/_ change and reset production state
5. selected non-English locale renders button text/title/aria-label
6. measured 44x44 touch target and Fit/close non-overlap, on desktop and
   mobile viewports, from the browser's own layout engine

plus the four 2026-10-05 greptile follow-ups (second-pointer pan guard,
editable-target shortcut guard, focus into the dialog on open, stale
geometry dropped — zoom level preserved — on a failed load).

The 2026-10-15T17:54:15Z maintainer re-gate (review 5537610966) added seven
silent gesture edge cases — gestures overwriting a fresh fit, a wheel zoom
mid-pan snapping the next move, a same-source navigation skipping its
re-centre, a resize keeping an obsolete zoom-out limit, a replaced finger
keeping a dead pinch baseline, a reversed drag waiting out its overshoot, and
line/page wheel deltas treated as pixels. Each has its own ``gate1015_*``
check below, asserted by ``TestComposedMaintainerRegate20261015``.

The 2026-10-15T19:09:46Z greptile P2 ("Zoom keys make gestures jump", reported
against the merge-only head 860c0e85) added two more: a keyboard zoom during a
held pan must not snap the next pointermove, and one during an active pinch must
not be undone by the next touchmove. Both are ``gate1015_keyboard_*`` checks,
asserted by ``TestComposedGreptileKeyboardZoom20261015``.

The 2026-10-15T22:36:41Z greptile P2 ("Wheel zoom gets undone", reported against
the merge-only head 25684d89) added one more: a wheel tick during an active
pinch must not be undone by the next touchmove either —
``gate1015_wheel_zoom_mid_pinch_is_not_undone``, asserted by
``TestComposedGreptileWheelZoomPinch20261015``.

The 2026-10-16T01:39:15Z greptile P2 ("Dragging stops after pinching", reported
against the merge-only head 6f462d82) added one more: lifting one finger of a
pinch must leave the contact still down able to pan —
``gate1015_pinch_end_keeps_the_remaining_finger_panning``, asserted by
``TestComposedGreptilePinchEndDrag20261016``. It is the only check that drives
BOTH event streams of a touch gesture (pointer events carry the pointerId, touch
events carry the contacts), because the re-armed pan looks the contact up by id.

The per-check script lives in ``_img_lightbox_composed_checks.js`` and is
injected into the page; there is no DOM emulation layer and no third-party JS
package involved. Chromium is provisioned by the CI test job
(``python -m playwright install chromium`` in ``.github/workflows/tests.yml``)
alongside the other browser-backed regressions, so these checks run in the
matrix like every other test. Only a local checkout that never installed
Playwright at all skips them, which is the repository's standing convention
for browser-backed tests.
"""

import base64
from pathlib import Path

import pytest

from tests._pytest_port import BASE

CHECKS_JS = Path(__file__).with_name("_img_lightbox_composed_checks.js")
_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

DESKTOP_VIEWPORT = {"width": 1200, "height": 900}
MOBILE_VIEWPORT = {"width": 390, "height": 844}


def _svg(w: int, h: int) -> str:
    """A solid-colour SVG data URL of the requested intrinsic size."""
    markup = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
        f'<rect width="100%" height="100%" fill="#3a7"/></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(markup.encode()).decode()


class _ComposedRun:
    """Lazily runs (and caches) the per-viewport check suites.

    The desktop checks run on the first access and the mobile checks on their
    first access, so a single test item never pays for both viewports (keeps
    each item comfortably inside the suite's per-test timeout).
    """

    def __init__(self, browser, desktop_page):
        self._browser = browser
        self.desktop_page = desktop_page
        self._desktop = None
        self._mobile = None

    @property
    def desktop(self):
        if self._desktop is None:
            self._desktop = self.desktop_page.evaluate(
                "(mode) => window.__composedChecks(mode)", "desktop"
            )
        return self._desktop

    @property
    def mobile(self):
        if self._mobile is None:
            page = _load_page(self._browser, MOBILE_VIEWPORT)
            self._mobile = page.evaluate("(mode) => window.__composedChecks(mode)", "mobile")
        return self._mobile

    def fresh_page(self):
        """A brand-new page (own context) for a scenario that must not see state
        a sibling test left behind — a leaked dialog or an in-flight gesture on
        the module-wide desktop page. Used by the state-heavy pinch test, which
        needs a freshly fitted image to pinch from."""
        return _load_page(self._browser, DESKTOP_VIEWPORT)


def _load_page(browser, viewport):
    context = browser.new_context(
        viewport=viewport,
        has_touch=True,
        device_scale_factor=1,
    )
    page = context.new_page()
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof window._openImgLightboxWithNav === 'function'",
        timeout=20000,
    )
    page.add_script_tag(path=str(CHECKS_JS))
    page.wait_for_function(
        "() => typeof window.__composedChecks === 'function'",
        timeout=20000,
    )
    return page


@pytest.fixture(scope="module")
def composed():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            desktop_page = _load_page(browser, DESKTOP_VIEWPORT)
            yield _ComposedRun(browser, desktop_page)
        finally:
            browser.close()


def _check(results: dict, name: str) -> None:
    entry = results.get(name)
    assert entry is not None, f"check '{name}' was not produced; got {sorted(results)}"
    assert entry.get("ok") is True, (
        f"composed browser check '{name}' failed: {entry.get('error')}\n"
        f"{entry.get('stack', '')[:1200]}"
    )


class TestComposedPointerClamp:
    def test_extreme_negative_drag_is_clamped(self, composed):
        _check(composed.desktop, "pointer_extreme_negative_clamp")

    def test_extreme_positive_drag_is_clamped(self, composed):
        _check(composed.desktop, "pointer_extreme_positive_clamp")

    def test_undersized_axis_is_centred_via_pointer(self, composed):
        _check(composed.desktop, "pointer_undersized_centred")

    def test_zoom_out_centres_undersized_axis(self, composed):
        _check(composed.desktop, "pointer_zoom_out_centres")


class TestComposedAnchoring:
    def test_wheel_anchor_via_real_onwheel(self, composed):
        _check(composed.desktop, "wheel_anchor")

    def test_pinch_anchor_via_real_touch_handlers(self, composed):
        _check(composed.desktop, "pinch_anchor")


class TestComposedSidebarExclusion:
    def test_left_edge_touch_does_not_arm_sidebar(self, composed):
        _check(composed.mobile, "sidebar_swipe_excluded")


class TestComposedFitAndKeyboard:
    def test_fit_button_click_resets_to_fit(self, composed):
        _check(composed.desktop, "fit_click_resets")

    def test_F_key_resets_to_fit(self, composed):
        _check(composed.desktop, "keyboard_F_resets")

    def test_plus_minus_keys_zoom(self, composed):
        _check(composed.desktop, "keyboard_plus_minus")

    def test_viewport_click_suppresses_dragged_and_canvas_only(self, composed):
        _check(composed.desktop, "viewport_click_suppression")


class TestComposedReviewFollowups:
    """Regressions for the 2026-10-05 greptile review on this PR."""

    def test_second_pointer_cannot_hijack_the_pan(self, composed):
        _check(composed.desktop, "pointer_second_pointer_guard")

    def test_keyboard_shortcuts_ignore_an_editable_target(self, composed):
        _check(composed.desktop, "keyboard_ignores_editable_target")

    def test_focus_moves_into_the_dialog_on_open(self, composed):
        _check(composed.desktop, "focus_moves_into_dialog")

    def test_failed_load_drops_the_stale_geometry(self, composed):
        _check(composed.desktop, "img_error_keeps_zoom_drops_stale_geometry")

    def test_duplicate_load_does_not_drop_the_selected_zoom(self, composed):
        """re-gate 2026-10-08T19:21:38Z — a cached image initializes
        synchronously at mount and its queued `load` event re-fit the stage,
        silently discarding the zoom the user had selected."""
        _check(composed.desktop, "duplicate_load_keeps_zoom")

    def test_navigation_from_a_fitted_image_opens_the_new_image_at_fit(self, composed):
        """re-gate 2026-10-08T23:18:39Z, static/ui.js:2800 — navigation kept the
        raw scale, so a small image shown at its fit cropped a large successor
        (scale 1 on a 4000x3000 image) and the reverse navigation shrank the
        small one to ~25px."""
        _check(composed.desktop, "navigation_carries_fit_not_raw_scale")

    def test_navigation_from_a_fitted_image_opens_the_new_image_at_fit_mobile(self, composed):
        _check(composed.mobile, "navigation_carries_fit_not_raw_scale")

    def test_navigation_after_a_failed_load_opens_the_next_image_at_fit(self, composed):
        """greptile P1 (2026-10-08T23:43:03Z): _imgOnError reset fitScale to 1
        while preserving scale, so an image that WAS at fit looked user-zoomed
        and the next working image opened cropped/undersized instead of at its
        own fit."""
        _check(composed.desktop, "navigation_after_a_failed_load_carries_fit")

    def test_navigation_after_a_failed_load_opens_the_next_image_at_fit_mobile(self, composed):
        _check(composed.mobile, "navigation_after_a_failed_load_carries_fit")

    def test_a_deliberate_scale_survives_a_failed_load(self, composed):
        """Maintainer re-gate 2026-10-09T00:47:03Z, static/ui.js:2861 — opening a
        1350x800 image (fit 0.8), pressing '=' (x1.25 -> exactly 1) and then
        navigating through a broken image to a 1600x900 successor must keep the
        deliberate 1; HEAD reset it to the successor's 0.675 because the error
        path had rewritten fitScale to state.scale."""
        _check(composed.desktop, "navigation_after_a_failed_load_keeps_a_deliberate_scale")

    def test_a_deliberate_scale_survives_a_failed_load_mobile(self, composed):
        _check(composed.mobile, "navigation_after_a_failed_load_keeps_a_deliberate_scale")


class TestComposedMaintainerRegate20261015:
    """Maintainer re-gate 2026-10-15T17:54:15Z (review 5537610966): seven silent
    gesture edge cases driven on desktop and iPhone, one browser check each.
    Every check is asserted here — ``_check`` is what turns a red in-page check
    into a red CI run, so adding a check without its item leaves the suite green
    (greptile P2, 2026-10-15T18:33:30Z)."""

    def test_a_gesture_started_without_geometry_cannot_overwrite_the_fit(self, composed):
        """Item 1 (static/ui.js:2795): a pinch armed while the image was still
        decoding kept the previous frame's baseline and rescaled the next one
        (4000x3000: fitted 351px -> 4080px)."""
        _check(composed.desktop, "gate1015_fit_survives_a_gesture_started_without_geometry")

    def test_a_gesture_started_without_geometry_cannot_overwrite_the_fit_mobile(self, composed):
        _check(composed.mobile, "gate1015_fit_survives_a_gesture_started_without_geometry")

    def test_fit_ends_the_gesture_in_flight(self, composed):
        _check(composed.desktop, "gate1015_fit_resets_an_active_pinch")

    def test_fit_ends_the_gesture_in_flight_mobile(self, composed):
        _check(composed.mobile, "gate1015_fit_resets_an_active_pinch")

    def test_a_wheel_zoom_mid_pan_does_not_snap_the_next_move(self, composed):
        """Item 2 (static/ui.js:3134): 1px of pointer movement moved the image
        674px because the drag baseline stayed on the pre-zoom position."""
        _check(composed.desktop, "gate1015_wheel_zoom_mid_pan_does_not_snap")

    def test_same_source_navigation_still_recentres(self, composed):
        """Item 3 (static/ui.js:2843): two entries sharing a source were read as
        the previous frame's duplicate load, so init/recentring were skipped and
        pendingNav stayed armed."""
        _check(composed.desktop, "gate1015_same_source_navigation_recentres")

    def test_same_source_navigation_still_recentres_mobile(self, composed):
        _check(composed.mobile, "gate1015_same_source_navigation_recentres")

    def test_resize_recomputes_the_zoom_out_limit(self, composed):
        """Item 4 (static/ui.js:3145): the old fit baseline survived a resize,
        so zoom-out stopped at 0.25 while the new fit was 0.08775."""
        _check(composed.desktop, "gate1015_resize_recomputes_the_zoom_out_limit")

    def test_a_replaced_finger_reanchors_the_pinch(self, composed):
        """Item 5 (static/ui.js:3088): the pinch kept a baseline belonging to a
        contact that was no longer down (2.06x scale jump on a 1px move)."""
        _check(composed.desktop, "gate1015_replaced_finger_reanchors_the_pinch")

    def test_a_replaced_finger_reanchors_the_pinch_mobile(self, composed):
        _check(composed.mobile, "gate1015_replaced_finger_reanchors_the_pinch")

    def test_a_reversed_drag_moves_the_image_immediately(self, composed):
        """Item 6 (static/ui.js:2993): the baseline kept the clamped-away
        overshoot, so a reversed drag had to retrace it first."""
        _check(composed.desktop, "gate1015_overshoot_reversal_responds_immediately")

    def test_line_mode_wheel_deltas_zoom_like_pixels(self, composed):
        """Item 7 (static/ui.js:3133): a three-line tick zoomed by 0.45% because
        deltaMode was ignored."""
        _check(composed.desktop, "gate1015_line_mode_wheel_zooms")


class TestComposedGreptileKeyboardZoom20261015:
    """Greptile P2 on head 860c0e85 (2026-10-15T19:09:46Z, static/ui.js:3293,
    "Zoom keys make gestures jump"): pressing + / - moved the image through
    ``zoomBy`` but left the held pan's baseline (and the pinch's
    ``pinchStartScale``/anchor midpoint) on the pre-zoom geometry, so the next
    pointermove snapped the image back and the next touchmove undid the keyboard
    zoom. Both halves are driven here on the real handlers."""

    def test_a_keyboard_zoom_mid_pan_does_not_snap_the_next_move(self, composed):
        _check(composed.desktop, "gate1015_keyboard_zoom_mid_pan_does_not_snap")

    def test_a_keyboard_zoom_mid_pinch_is_not_undone(self, composed):
        _check(composed.desktop, "gate1015_keyboard_zoom_mid_pinch_is_not_undone")

    def test_a_keyboard_zoom_mid_pinch_is_not_undone_mobile(self, composed):
        _check(composed.mobile, "gate1015_keyboard_zoom_mid_pinch_is_not_undone")


class TestComposedGreptileWheelZoomPinch20261015:
    """Greptile P2 on the merge-only head 25684d89 (2026-10-15T22:36:41Z,
    static/ui.js:3275, "Wheel zoom gets undone"): ``_imgZoomFromWheel`` moved the
    image through ``_imgSetScale`` and re-anchored the held pan, but left the
    pinch's ``pinchStartScale``/anchor midpoint on the pre-tick geometry, so the
    next touchmove restored the old zoom. Driven on the real handlers in both
    buckets (the wheel is the desktop's zoom gesture; a phone may have a mouse).
    """

    def test_a_wheel_zoom_mid_pinch_is_not_undone(self, composed):
        _check(composed.desktop, "gate1015_wheel_zoom_mid_pinch_is_not_undone")

    def test_a_wheel_zoom_mid_pinch_is_not_undone_mobile(self, composed):
        _check(composed.mobile, "gate1015_wheel_zoom_mid_pinch_is_not_undone")


class TestComposedGreptilePinchReversal20261015:
    """Greptile P2 on the merge-only head c69de101 (2026-10-15T23:15:55Z,
    static/ui.js:3213, "Reversing fingers leaves images stuck"): the pinch kept
    the clamped-away overshoot inside its positional baseline, so a gesture
    reversed after pushing past an edge did nothing until the overshoot was
    retraced - the pan path already rebased onto the clamped position, the pinch
    path did not.
    """

    def test_a_reversed_pinch_moves_the_image_immediately(self, composed):
        _check(composed.desktop, "gate1015_pinch_reversal_responds_immediately")

    def test_a_reversed_pinch_moves_the_image_immediately_mobile(self, composed):
        _check(composed.mobile, "gate1015_pinch_reversal_responds_immediately")

    def test_a_spread_sequence_is_path_independent(self, composed):
        """Greptile P1 on 5e206e06 (2026-10-15T23:28:11Z, static/ui.js:3222):
        asked for several spread updates, because a constant-spread reversal
        cannot see drift. The same two fingers must always produce the same
        frame — true for the positional rebase shipped here, false for the
        whole-baseline re-anchor the finding proposed."""
        _check(composed.desktop, "gate1015_pinch_spread_is_path_independent")

    def test_a_spread_sequence_is_path_independent_mobile(self, composed):
        _check(composed.mobile, "gate1015_pinch_spread_is_path_independent")


class TestComposedGreptilePinchEndDrag20261016:
    """Greptile P2 on the merge-only head 6f462d82 (2026-10-16T01:39:15Z,
    static/ui.js:3235, "Dragging stops after pinching"): ``_imgOnTouchStart``
    ends the pointer drag when the pinch arms, and ``_imgOnTouchEnd`` only
    cleared ``pinching`` — the contact still down got no new ``pointerdown``,
    so every one of its moves was dropped until the user lifted and pressed
    again. The pan is now re-armed from that contact, anchored on its current
    geometry, and driven here on the real handlers with BOTH event streams (as
    a browser delivers them: the ids on the pointer events, the contacts on the
    touch events).
    """

    def test_the_remaining_finger_keeps_panning_after_a_pinch(self, composed):
        _check(
            composed.desktop,
            "gate1015_pinch_end_keeps_the_remaining_finger_panning",
        )

    def test_the_remaining_finger_keeps_panning_after_a_pinch_mobile(self, composed):
        _check(
            composed.mobile,
            "gate1015_pinch_end_keeps_the_remaining_finger_panning",
        )


# Trusted-input regressions for the 2026-10-06 maintainer gate certificate.
# These use Playwright's own input pipeline (real mouse/keyboard events), which
# is the only way to exercise the browser's click retargeting under pointer
# capture and the browser's own shortcut defaults -- dispatched synthetic
# events miss both (the gate note says synthetic clicks that explicitly choose
# a canvas target do not reproduce it).
_LB_RECTS_JS = """
() => {
  const lb = document.querySelector('.img-lightbox');
  if (!lb) return null;
  const vp = lb.querySelector('.img-lightbox-viewport');
  const cv = lb.querySelector('.img-lightbox-canvas');
  const v = vp.getBoundingClientRect();
  const c = cv.getBoundingClientRect();
  const vpBox = { left: v.left, top: v.top, width: v.width, height: v.height };
  const cvBox = { left: c.left, top: c.top, width: c.width, height: c.height };
  const inCanvas = (p) => !!p && p.x >= cvBox.left && p.x <= cvBox.left + cvBox.width
    && p.y >= cvBox.top && p.y <= cvBox.top + cvBox.height;
  const margin = 6;
  let letter = null;
  if (cvBox.top - vpBox.top > margin * 2) {
    letter = { x: vpBox.left + vpBox.width / 2, y: vpBox.top + margin };
  } else if ((vpBox.top + vpBox.height) - (cvBox.top + cvBox.height) > margin * 2) {
    letter = { x: vpBox.left + vpBox.width / 2, y: vpBox.top + vpBox.height - margin };
  } else if (cvBox.left - vpBox.left > margin * 2) {
    letter = { x: vpBox.left + margin, y: vpBox.top + vpBox.height / 2 };
  } else if ((vpBox.left + vpBox.width) - (cvBox.left + cvBox.width) > margin * 2) {
    letter = { x: vpBox.left + vpBox.width - margin, y: vpBox.top + vpBox.height / 2 };
  }
  const image = { x: cvBox.left + cvBox.width / 2, y: cvBox.top + cvBox.height / 2 };
  return {
    viewport: vpBox,
    canvas: cvBox,
    image,
    letterbox: letter,
    imageHitsCanvas: inCanvas(image),
    letterboxHitsCanvas: inCanvas(letter),
  };
}
"""

_THUMB_SVG = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI4MDAi"
    "IGhlaWdodD0iNDUwIj48cmVjdCB3aWR0aD0iMTAwJSIgaGVpZ2h0PSIxMDAlIiBmaWxs"
    "PSIjM2E3Ii8+PC9zdmc+"
)

# A point inside the lightbox overlay but OUTSIDE the gesture viewport and clear
# of every control, so a press/release there targets the backdrop itself.
_BACKDROP_JS = """
() => {
  const lb = document.querySelector('.img-lightbox');
  if (!lb) return null;
  const vp = lb.querySelector('.img-lightbox-viewport');
  const v = vp.getBoundingClientRect();
  const lbR = lb.getBoundingClientRect();
  const kids = Array.from(lb.children).filter((el) => el !== vp)
    .map((el) => el.getBoundingClientRect());
  const inside = (x, y, r) => x >= r.left && x <= r.left + r.width
    && y >= r.top && y <= r.top + r.height;
  for (let y = lbR.top + 6; y < lbR.top + lbR.height - 6; y += 6) {
    for (let x = lbR.left + 6; x < lbR.left + lbR.width - 6; x += 6) {
      if (inside(x, y, v)) continue;
      if (kids.some((r) => r.width > 0 && inside(x, y, r))) continue;
      if (document.elementFromPoint(x, y) !== lb) continue;
      return { x, y };
    }
  }
  return null;
}
"""


def _zoom_press_and_navigate(page):
    """Open a two-image gallery through the production opener, zoom it with real
    '=' presses, press the image with a real mouse, move 1px and then navigate
    with a real ArrowRight while the button is still held. Returns the image
    point and the selected zoom."""
    page.evaluate(
        """([wide, portrait]) => {
            const prev = document.querySelector('.img-lightbox');
            if (prev) {
                try { window._closeImgLightbox(prev); } catch (_) {}
                if (prev.parentNode) prev.parentNode.removeChild(prev);
            }
            window._openImgLightboxWithNav(wide, 'wide', [
                { src: wide, alt: 'wide' },
                { src: portrait, alt: 'portrait' },
            ], 0);
        }""",
        [_svg(1200, 300), _svg(300, 900)],
    )
    page.wait_for_function(
        "() => { const lb = document.querySelector('.img-lightbox'); "
        "return !!lb && !!lb._zoom && lb._zoom.boxW > 0; }",
        timeout=15000,
    )
    for _ in range(3):
        page.keyboard.press("Equal")  # three real '=' presses
    zoomed = page.evaluate("() => document.querySelector('.img-lightbox')._zoom.scale")
    data = page.evaluate(_LB_RECTS_JS)
    assert data is not None, "fixture: the lightbox must be open"
    img = data["image"]
    page.mouse.move(img["x"], img["y"])
    page.mouse.down()
    page.mouse.move(img["x"], img["y"] + 1)
    page.keyboard.press("ArrowRight")
    page.wait_for_function(
        "() => { const lb = document.querySelector('.img-lightbox'); "
        "const z = lb && lb._zoom; "
        "return !!z && lb._navIndex === 1 && z.pendingNav === false && z.boxW > 0; }",
        timeout=15000,
    )
    page.wait_for_timeout(60)  # let the re-centre settle
    return img, zoomed


def _cdp_touch(session, type_, points):
    """Dispatch one real multi-touch frame through Chromium's own input pipeline.

    Playwright's ``touchscreen`` API can only tap a single contact, so a genuine
    two-finger pinch has to go through CDP (``Input.dispatchTouchEvent``). A
    touchStart/touchMove carrying both points yields the matching TouchEvent
    whose ``touches`` holds both contacts (Chromium may split the frame across
    points, but the second event always carries both), and an empty touchEnd
    releases them all.
    """
    session.send(
        "Input.dispatchTouchEvent",
        {
            "type": type_,
            "touchPoints": [
                {"x": x, "y": y, "id": i + 1} for i, (x, y) in enumerate(points)
            ],
        },
    )


class TestComposedTrustedInput:
    """The two 2026-10-06 gate blockers, driven with real Chromium input."""

    @staticmethod
    def _open_from_thumbnail(page):
        """Open the production lightbox the way a user does: a real click on a
        rendered message thumbnail (the gate's own entrypoint)."""
        page.evaluate(
            """(src) => {
                const old = document.getElementById('gate-thumb');
                if (old) old.remove();
                // Tear the previous dialog down through its own close path so
                // its document keydown listener and window resize handler are
                // removed: removing the node directly would leave those
                // listeners live and make later tests order-dependent
                // (greptile review of #6896, 2026-10-05).
                const prev = document.querySelector('.img-lightbox');
                if (prev) {
                    try { window._closeImgLightbox(prev); } catch (_) {}
                    if (prev.parentNode) prev.parentNode.removeChild(prev);
                }
                window.__gateKeys = [];
                const im = document.createElement('img');
                im.id = 'gate-thumb';
                im.className = 'msg-media-img';
                im.src = src;
                im.style.cssText = 'position:fixed;left:8px;top:48px;width:160px;height:90px;z-index:9000';
                document.body.appendChild(im);
            }""",
            _THUMB_SVG,
        )
        page.click("#gate-thumb")
        page.wait_for_function(
            "() => { const lb = document.querySelector('.img-lightbox'); "
            "return !!lb && !!lb._zoom && lb._zoom.boxW > 0; }",
            timeout=15000,
        )
        data = page.evaluate(_LB_RECTS_JS)
        assert data is not None, "the thumbnail click must open the lightbox"
        return data

    @staticmethod
    def _is_open(page):
        return page.evaluate("() => document.querySelector('.img-lightbox') !== null")

    # `_closeImgLightbox` writes the inline reverse animation the moment a
    # dismissal starts but only removes the node 120ms later, so a presence
    # sample taken before that deadline can never observe a close and the two
    # "must survive" checks below went false-green (maintainer re-warmup of
    # #6896, 2026-10-06: a route-only controlled mutation restoring the
    # target-only dismissal arm still passed the 80ms presence assertion).
    # Assert the transition itself — only the close path ever writes it — and
    # then wait past the real removal deadline before sampling presence.
    _CLOSE_DEADLINE_MS = 400

    @staticmethod
    def _close_initiated(page):
        """True once the close transition has been started (or the node is gone)."""
        return page.evaluate(
            """() => {
                const lb = document.querySelector('.img-lightbox');
                if (!lb) return true;  // already removed => the close ran
                const inline = lb.style.animation || '';
                const direction = window.getComputedStyle(lb).animationDirection || '';
                return /reverse/.test(inline) || /reverse/.test(direction);
            }"""
        )

    def test_trusted_click_on_image_pixels_keeps_the_lightbox_open(self, composed):
        page = composed.desktop_page
        data = self._open_from_thumbnail(page)
        assert data["imageHitsCanvas"] is True, "fixture: the image centre is off the canvas"
        img = data["image"]
        page.mouse.click(img["x"], img["y"])
        page.wait_for_timeout(self._CLOSE_DEADLINE_MS)
        assert not self._close_initiated(page), (
            "a trusted mouse click on the rendered image started the close "
            "transition (pointer capture retargets the click to the viewport)"
        )
        assert self._is_open(page), (
            "a trusted mouse click on the rendered image dismissed the lightbox "
            "(pointer capture retargets the click to the viewport)"
        )

    def test_trusted_letterbox_click_still_dismisses(self, composed):
        page = composed.desktop_page
        data = self._open_from_thumbnail(page)
        assert data["letterbox"] is not None, "fixture: no letterbox area around the image"
        assert data["letterboxHitsCanvas"] is False, "fixture: the letterbox point is on the image"
        box = data["letterbox"]
        page.mouse.click(box["x"], box["y"])
        page.wait_for_timeout(self._CLOSE_DEADLINE_MS)
        assert not self._is_open(page), "a trusted click on the letterbox must still dismiss the lightbox"

    def test_trusted_drag_then_click_keeps_the_lightbox_open(self, composed):
        page = composed.desktop_page
        data = self._open_from_thumbnail(page)
        img = data["image"]
        page.mouse.move(img["x"], img["y"])
        page.mouse.down()
        page.mouse.move(img["x"] + 60, img["y"] + 30, steps=6)
        page.mouse.up()  # the browser emits a real click after the drag
        page.wait_for_timeout(self._CLOSE_DEADLINE_MS)
        assert not self._close_initiated(page), (
            "the post-drag click started the close transition"
        )
        assert self._is_open(page), "the post-drag click must not dismiss the lightbox"

    def test_trusted_browser_zoom_shortcuts_are_not_hijacked(self, composed):
        page = composed.desktop_page
        data = self._open_from_thumbnail(page)
        assert data["imageHitsCanvas"] is True
        page.evaluate(
            """() => {
                if (window.__gateKeyRec) document.removeEventListener('keydown', window.__gateKeyRec);
                window.__gateKeys = [];
                window.__gateKeyRec = (e) => {
                    window.__gateKeys.push({
                        key: e.key, ctrl: e.ctrlKey, meta: e.metaKey, alt: e.altKey,
                        prevented: e.defaultPrevented,
                    });
                };
                document.addEventListener('keydown', window.__gateKeyRec);
            }"""
        )
        scale_of = (
            "() => { const lb = document.querySelector('.img-lightbox'); "
            "return (lb._zoom && lb._zoom.scale) || 0; }"
        )
        try:
            # Re-establish the precondition the open path guarantees: the
            # document-level handler deliberately ignores keys whose target is
            # an editable field, and this scenario shares the module page with
            # earlier ones, so an ambient input/textarea left focused would
            # silently swallow the unmodified '=' below. Verified root cause of
            # the 2026-10-08 red on the 3.11/shard-3 job (which otherwise
            # passed the identical shard order on the sibling head): with an
            # editable target, '=' left the scale at 1; after re-focusing the
            # dialog it zooms to 1.25.
            page.evaluate(
                "() => { const lb = document.querySelector('.img-lightbox'); "
                "if (lb && typeof lb.focus === 'function') lb.focus({ preventScroll: true }); }"
            )
            before = page.evaluate(scale_of)
            page.keyboard.press("Control+Equal")
            page.keyboard.press("Control+Minus")
            after = page.evaluate(scale_of)
            assert after == before, (
                f"Ctrl+Equal / Ctrl+Minus must not change the lightbox zoom "
                f"(scale {before} -> {after}); those belong to the browser"
            )
            recorded = page.evaluate("() => window.__gateKeys || []")
            modified = [r for r in recorded if r["ctrl"] or r["meta"]]
            assert modified, "the trusted Ctrl shortcuts never reached the document"
            assert all(r["prevented"] is False for r in modified), (
                "a browser zoom shortcut must stay unprevented: " + repr(modified)
            )
            # Control: the unmodified shortcuts still drive the image.
            page.keyboard.press("Equal")
            assert page.evaluate(scale_of) > before, "an unmodified '=' must still zoom in"
            page.keyboard.press("Minus")
            assert page.evaluate(scale_of) == before, "an unmodified '-' must still zoom out"
        finally:
            page.evaluate(
                "() => { if (window.__gateKeyRec) document.removeEventListener('keydown', window.__gateKeyRec); }"
            )

    def test_trusted_pan_across_navigation_does_not_teleport_the_new_image(self, composed):
        """Real mouse-down -> navigation -> small move (maintainer re-warmup).

        The pan in flight when the image changes belongs to the old image's
        coordinate space. Re-applying its baseline to the freshly-centred new
        image threw it ~83px up (2026-10-06 re-warmup of #6896). The image
        change must cancel the gesture and keep the selected zoom.
        """
        page = composed.desktop_page
        img, zoomed = _zoom_press_and_navigate(page)
        before = page.evaluate(
            "() => { const z = document.querySelector('.img-lightbox')._zoom; "
            "return { x: z.x, y: z.y, scale: z.scale, dragging: z.dragging }; }"
        )
        assert before["dragging"] is False, (
            "changing the image must cancel the in-flight pan"
        )
        # Small move while still held: the new image must not jump.
        page.mouse.move(img["x"], img["y"] + 6, steps=2)
        page.wait_for_timeout(60)
        after = page.evaluate(
            "() => { const z = document.querySelector('.img-lightbox')._zoom; "
            "return { x: z.x, y: z.y, scale: z.scale }; }"
        )
        page.mouse.up()
        assert abs(after["y"] - before["y"]) <= 2, (
            "a move after navigation must not teleport the new image: "
            f"y {before['y']} -> {after['y']}"
        )
        assert abs(after["x"] - before["x"]) <= 2, (
            "a move after navigation must not teleport the new image: "
            f"x {before['x']} -> {after['x']}"
        )
        assert abs(after["scale"] - zoomed) < 1e-6, (
            f"the selected zoom must survive the navigation: {zoomed} -> {after['scale']}"
        )
        assert self._is_open(page), "navigating during a pan must not dismiss the lightbox"

    def test_trusted_navigation_release_outside_viewport_keeps_the_dialog(self, composed):
        """Releasing the held button outside the viewport must not dismiss.

        Cancelling the pan on an image change must KEEP the pointer capture:
        with the capture held, the release is retargeted to the viewport and the
        recorded press origin suppresses the follow-up click. Releasing the
        capture (the first version of the fix) let the release land on the
        backdrop and close the dialog (greptile review of #6896, 2026-10-06).
        """
        page = composed.desktop_page
        _zoom_press_and_navigate(page)
        backdrop = page.evaluate(_BACKDROP_JS)
        assert backdrop is not None, "fixture: no backdrop point outside the viewport"
        page.mouse.move(backdrop["x"], backdrop["y"], steps=2)
        page.mouse.up()
        page.wait_for_timeout(self._CLOSE_DEADLINE_MS)
        assert not self._close_initiated(page), (
            "releasing a held pointer outside the viewport started the close transition"
        )
        assert self._is_open(page), (
            "releasing a held pointer outside the viewport dismissed the lightbox; "
            "cancelling the pan must keep the pointer capture"
        )

    def test_trusted_pinch_across_navigation_drops_the_stale_baseline(self, composed):
        """A two-finger pinch held across a navigation must not throw the new image.

        Real CDP touch (two simultaneous contacts): zoom the wide image, put two
        fingers down, spread, press ArrowRight with both contacts still down,
        wait for the 300x900 replacement to decode, then spread a little more.
        Before the fix the next touchmove re-applied the previous image's
        pinchStart baselines and jumped the new image (544px on the reviewer's
        fixture); now the image change ends the pinch, so the extra spread is
        ignored and the pinched zoom survives (maintainer review of #6896,
        2026-10-06).
        """
        # A fresh page: the scenario needs a freshly fitted image to pinch from,
        # and the module-wide desktop page is shared with sibling tests — a
        # leaked dialog or a lingering gesture there would leave the pinch on
        # the wrong baseline.
        page = composed.fresh_page()
        page.evaluate(
            """([wide, portrait]) => {
                const prev = document.querySelector('.img-lightbox');
                if (prev) {
                    try { window._closeImgLightbox(prev); } catch (_) {}
                    if (prev.parentNode) prev.parentNode.removeChild(prev);
                }
                window._openImgLightboxWithNav(wide, 'wide', [
                    { src: wide, alt: 'wide' },
                    { src: portrait, alt: 'portrait' },
                ], 0);
            }""",
            [_svg(1200, 300), _svg(300, 900)],
        )
        page.wait_for_function(
            "() => { const lb = document.querySelector('.img-lightbox'); "
            "return !!lb && !!lb._zoom && lb._zoom.boxW > 0; }",
            timeout=15000,
        )
        assert page.evaluate(
            "() => document.querySelectorAll('.img-lightbox').length"
        ) == 1, "fixture: the fresh page must hold exactly one dialog"
        for _ in range(3):
            page.keyboard.press("Equal")  # three real '=' presses
        data = page.evaluate(_LB_RECTS_JS)
        assert data is not None, "fixture: the lightbox must be open"
        vp = data["viewport"]
        cx = vp["left"] + vp["width"] / 2
        cy = vp["top"] + vp["height"] / 2
        session = page.context.new_cdp_session(page)
        try:
            _cdp_touch(session, "touchStart", [(cx - 50, cy), (cx + 50, cy)])
            _cdp_touch(session, "touchMove", [(cx - 60, cy), (cx + 60, cy)])
            armed = page.evaluate(
                "() => { const z = document.querySelector('.img-lightbox')._zoom; "
                "return { pinching: z.pinching, scale: z.scale, fitScale: z.fitScale }; }"
            )
            # Only the production two-contact touch handlers can raise the scale,
            # so this also proves the CDP contacts really armed the pinch.
            assert armed["pinching"] is True, "fixture: two real contacts must arm the pinch"
            assert armed["scale"] > armed["fitScale"], (
                "fixture: the two real contacts must zoom the image in: "
                f"{armed['fitScale']} -> {armed['scale']}"
            )
            pinched = armed["scale"]
            page.keyboard.press("ArrowRight")
            page.wait_for_function(
                "() => { const lb = document.querySelector('.img-lightbox'); "
                "const z = lb && lb._zoom; "
                "return !!z && lb._navIndex === 1 && z.pendingNav === false && z.boxW > 0; }",
                timeout=15000,
            )
            page.wait_for_timeout(60)  # let the re-centre settle
            before = page.evaluate(
                "() => { const z = document.querySelector('.img-lightbox')._zoom; "
                "return { x: z.x, y: z.y, scale: z.scale, pinching: z.pinching, "
                "fitScale: z.fitScale }; }"
            )
            assert before["pinching"] is False, (
                "changing the image must end the in-flight pinch"
            )
            assert abs(before["scale"] - pinched) < 1e-6, (
                "the navigation must keep the pinched zoom: "
                f"{pinched} -> {before['scale']}"
            )
            assert before["scale"] > before["fitScale"], (
                "fixture: the pinched-in zoom must survive the navigation"
            )
            # Both contacts stay down and spread further; the pinch is over, so
            # this move must not touch the freshly-centred image.
            _cdp_touch(session, "touchMove", [(cx - 61, cy + 40), (cx + 61, cy + 40)])
            page.wait_for_timeout(60)
            after = page.evaluate(
                "() => { const z = document.querySelector('.img-lightbox')._zoom; "
                "return { x: z.x, y: z.y, scale: z.scale }; }"
            )
            assert abs(after["y"] - before["y"]) <= 2, (
                "a pinch move after navigation must not teleport the new image: "
                f"y {before['y']} -> {after['y']}"
            )
            assert abs(after["x"] - before["x"]) <= 2, (
                "a pinch move after navigation must not teleport the new image: "
                f"x {before['x']} -> {after['x']}"
            )
            assert abs(after["scale"] - before["scale"]) < 1e-6, (
                "a pinch move after navigation must not change the scale: "
                f"{before['scale']} -> {after['scale']}"
            )
            assert self._is_open(page), (
                "the pinch across navigation must not dismiss the lightbox"
            )
        finally:
            _cdp_touch(session, "touchEnd", [])
            session.detach()


class TestComposedGate20261006:
    """The in-page synthetic halves of the same two gate blockers."""

    def test_retargeted_click_from_an_image_press_does_not_dismiss(self, composed):
        _check(composed.desktop, "viewport_click_retargeted_image_press")

    def test_second_pointer_cannot_block_a_letterbox_dismissal(self, composed):
        _check(composed.desktop, "second_pointer_cannot_block_dismissal")

    def test_browser_shortcut_modifiers_are_left_alone(self, composed):
        _check(composed.desktop, "keyboard_modifier_shortcuts_untouched")


class TestComposedReworkup20261006:
    """The in-page synthetic half of the 2026-10-06 re-warmup finding."""

    def test_navigation_during_a_pan_drops_the_stale_baseline(self, composed):
        _check(composed.desktop, "navigation_during_pan_drops_the_stale_baseline")

    def test_navigation_during_a_pinch_drops_the_stale_baseline(self, composed):
        _check(composed.desktop, "navigation_during_pinch_drops_the_stale_baseline")


class TestComposedI18n:
    def test_zh_locale_localizes_fit_title_and_renders_icon(self, composed):
        _check(composed.desktop, "locale_zh_renders")

    def test_ja_locale_localizes_fit_title_and_renders_icon(self, composed):
        _check(composed.desktop, "locale_ja_renders")


class TestComposedGeometry:
    def test_fit_button_matches_close_circle_on_desktop(self, composed):
        _check(composed.desktop, "geometry_circle_size")

    def test_fit_button_matches_close_circle_on_mobile(self, composed):
        _check(composed.mobile, "geometry_circle_size")

    def test_fit_and_close_do_not_overlap_on_desktop(self, composed):
        _check(composed.desktop, "geometry_no_overlap")

    def test_fit_and_close_do_not_overlap_on_mobile(self, composed):
        _check(composed.mobile, "geometry_no_overlap")

    def test_fit_button_is_keyboard_reachable_with_focus_visible(self, composed):
        page = composed.desktop_page
        assert page.evaluate("() => window.__composedOpenFit()") is True
        # Park focus on the sibling control, then move with a real keyboard press so
        # Chromium applies the keyboard-modality :focus-visible heuristic. Fit sits
        # left of close and precedes it in the DOM (maintainer fold: Tab order
        # follows the visual order), so Shift+Tab from close reaches Fit.
        page.evaluate("() => document.querySelector('.img-lightbox-close').focus()")
        page.keyboard.press("Shift+Tab")
        measured = page.evaluate(
            """() => {
                const fit = document.querySelector('.img-lightbox-fit');
                if (!fit) throw new Error('fit button missing');
                const style = getComputedStyle(fit);
                const box = fit.getBoundingClientRect().toJSON();
                return {
                    active: document.activeElement === fit,
                    focusVisible: fit.matches(':focus-visible'),
                    outlineWidth: parseFloat(style.outlineWidth) || 0,
                    outlineStyle: style.outlineStyle,
                    width: box.width,
                    height: box.height,
                    coarse: window.matchMedia('(pointer:coarse)').matches,
                };
            }"""
        )
        assert measured["active"] is True, "Shift+Tab from the close button must reach the Fit button"
        assert measured["focusVisible"] is True, "keyboard focus must produce a :focus-visible state"
        assert measured["outlineStyle"] != "none", "focus-visible must render an outline"
        assert measured["outlineWidth"] >= 2, f"focus outline too thin: {measured['outlineWidth']}"
        want = 44 if measured["coarse"] else 36  # 44px tap target on a coarse pointer (maintainer fold)
        assert abs(measured["width"] - want) <= 0.5, f"focused Fit circle width {measured['width']} != {want}"
        assert abs(measured["height"] - want) <= 0.5, f"focused Fit circle height {measured['height']} != {want}"
