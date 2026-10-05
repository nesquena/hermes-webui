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

The per-check script lives in ``_img_lightbox_composed_checks.js`` and is
injected into the page; there is no DOM emulation layer and no third-party JS
package involved. Chromium is provisioned by the CI test job
(``python -m playwright install chromium`` in ``.github/workflows/tests.yml``)
alongside the other browser-backed regressions, so these checks run in the
matrix like every other test. Only a local checkout that never installed
Playwright at all skips them, which is the repository's standing convention
for browser-backed tests.
"""

from pathlib import Path

import pytest

from tests._pytest_port import BASE

CHECKS_JS = Path(__file__).with_name("_img_lightbox_composed_checks.js")
_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

DESKTOP_VIEWPORT = {"width": 1200, "height": 900}
MOBILE_VIEWPORT = {"width": 390, "height": 844}


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
                const im = document.createElement('img');
                im.id = 'gate-thumb';
                im.className = 'msg-media-img';
                im.src = src;
                im.style.cssText = 'position:fixed;left:8px;top:48px;width:160px;height:90px;z-index:9000';
                document.body.appendChild(im);
                const old2 = document.querySelector('.img-lightbox');
                if (old2) old2.remove();
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

    def test_trusted_click_on_image_pixels_keeps_the_lightbox_open(self, composed):
        page = composed.desktop_page
        data = self._open_from_thumbnail(page)
        assert data["imageHitsCanvas"] is True, "fixture: the image centre is off the canvas"
        img = data["image"]
        page.mouse.click(img["x"], img["y"])
        page.wait_for_timeout(80)
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
        page.wait_for_timeout(300)
        assert not self._is_open(page), "a trusted click on the letterbox must still dismiss the lightbox"

    def test_trusted_drag_then_click_keeps_the_lightbox_open(self, composed):
        page = composed.desktop_page
        data = self._open_from_thumbnail(page)
        img = data["image"]
        page.mouse.move(img["x"], img["y"])
        page.mouse.down()
        page.mouse.move(img["x"] + 60, img["y"] + 30, steps=6)
        page.mouse.up()  # the browser emits a real click after the drag
        page.wait_for_timeout(80)
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


class TestComposedGate20261006:
    """The in-page synthetic halves of the same two gate blockers."""

    def test_retargeted_click_from_an_image_press_does_not_dismiss(self, composed):
        _check(composed.desktop, "viewport_click_retargeted_image_press")

    def test_browser_shortcut_modifiers_are_left_alone(self, composed):
        _check(composed.desktop, "keyboard_modifier_shortcuts_untouched")


class TestComposedI18n:
    def test_zh_locale_renders_fit_text_title_aria(self, composed):
        _check(composed.desktop, "locale_zh_renders")

    def test_ja_locale_renders_fit_text(self, composed):
        _check(composed.desktop, "locale_ja_renders")


class TestComposedGeometry:
    def test_fit_button_meets_44px_touch_target_on_desktop(self, composed):
        _check(composed.desktop, "geometry_min_touch_target")

    def test_fit_button_meets_44px_touch_target_on_mobile(self, composed):
        _check(composed.mobile, "geometry_min_touch_target")

    def test_fit_and_close_do_not_overlap_on_desktop(self, composed):
        _check(composed.desktop, "geometry_no_overlap")

    def test_fit_and_close_do_not_overlap_on_mobile(self, composed):
        _check(composed.mobile, "geometry_no_overlap")

    def test_fit_button_is_keyboard_reachable_with_focus_visible(self, composed):
        page = composed.desktop_page
        assert page.evaluate("() => window.__composedOpenFit()") is True
        # Park focus on the sibling control, then move with a real Tab press so
        # Chromium applies the keyboard-modality :focus-visible heuristic.
        page.evaluate("() => document.querySelector('.img-lightbox-close').focus()")
        page.keyboard.press("Tab")
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
                };
            }"""
        )
        assert measured["active"] is True, "Tab from the close button must reach the Fit button"
        assert measured["focusVisible"] is True, "keyboard focus must produce a :focus-visible state"
        assert measured["outlineStyle"] != "none", "focus-visible must render an outline"
        assert measured["outlineWidth"] >= 2, f"focus outline too thin: {measured['outlineWidth']}"
        assert measured["width"] >= 44 - 0.5, f"focused Fit width {measured['width']} < 44"
        assert measured["height"] >= 44 - 0.5, f"focused Fit height {measured['height']} < 44"
