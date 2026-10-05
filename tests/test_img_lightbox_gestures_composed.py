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
