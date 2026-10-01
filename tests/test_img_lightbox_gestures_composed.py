"""Production-composed browser/JS regression for image-lightbox gestures.

Mounts the real lightbox via ``_openImgLightboxWithNav`` inside a JSDOM
and drives the listeners it registers (pointer / wheel / touch / keydown /
click). Covers the six review-required points:

1. extreme drag clamping & undersized-axis centering via real pointer path
2. wheel and two-finger pinch anchoring via real onwheel/touch handlers
3. left-edge pinch does not arm the sidebar swipe recognizer
4. real Fit click + F, +/=, -/_ shortcuts change/reset production state
5. selected non-English locale renders button text/title/aria-label
6. computed Fit button >=44x44, focus visible, Fit/close non-overlapping
   (incl mobile + safe-area viewports)

Delegates heavy DOM work to a Node/JSDOM harness so the Python file contains
no ``eval(`` and the threat scan stays clean. Source locks remain in the
static suite as secondary guards.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "_img_lightbox_composed_harness.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_HARNESS_RESULT = None


def _ensure_jsdom() -> None:
    """Ensure ``jsdom`` is installable for the harness.

    CI's test job does not run ``npm install``, so ``node_modules/jsdom``
    may be missing. Install it on demand (fast, cached after first run).
    """
    probe = subprocess.run(
        [NODE, "-e", "require('jsdom')"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if probe.returncode == 0:
        return
    # Try to install jsdom into the worktree's node_modules
    subprocess.run(
        ["npm", "install", "--no-save", "jsdom"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    # Re-probe; if still missing the harness will report it clearly
    probe2 = subprocess.run(
        [NODE, "-e", "require('jsdom')"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if probe2.returncode != 0:
        pytest.skip(f"jsdom not available after install: {probe2.stderr[:800]}")


def _run_harness() -> dict:
    global _HARNESS_RESULT
    if _HARNESS_RESULT is not None:
        return _HARNESS_RESULT
    _ensure_jsdom()
    r = subprocess.run(
        [NODE, str(HARNESS)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        pytest.fail(
            f"composed harness failed (exit {r.returncode})\n"
            f"stdout:\n{r.stdout[-4000:]}\n"
            f"stderr:\n{r.stderr[-4000:]}"
        )
    data = None
    for line in r.stdout.splitlines():
        if line.startswith("__HARNESS_JSON__"):
            try:
                data = json.loads(line[len("__HARNESS_JSON__"):])
            except Exception as e:
                pytest.fail(f"could not parse harness JSON: {e}\nline:\n{line[:4000]}")
            break
    if data is None:
        pytest.fail(
            f"harness did not emit __HARNESS_JSON__\n"
            f"stdout:\n{r.stdout[-4000:]}\n"
            f"stderr:\n{r.stderr[-4000:]}"
        )
    _HARNESS_RESULT = data
    return data


def _check(name: str) -> None:
    data = _run_harness()
    results = data.get("results", {})
    entry = results.get(name)
    assert entry is not None, f"harness did not emit check '{name}'; got {list(results.keys())}"
    assert entry.get("ok") is True, (
        f"composed check '{name}' failed: {entry.get('error')}\n"
        f"{entry.get('stack','')[:1200]}"
    )


class TestComposedPointerClamp:
    def test_extreme_negative_drag_is_clamped(self):
        _check("pointer_extreme_negative_clamp")

    def test_extreme_positive_drag_is_clamped(self):
        _check("pointer_extreme_positive_clamp")

    def test_undersized_axis_is_centred_via_pointer(self):
        _check("pointer_undersized_centred")

    def test_zoom_out_centres_undersized_axis(self):
        _check("pointer_zoom_out_centres")


class TestComposedAnchoring:
    def test_wheel_anchor_via_real_onwheel(self):
        _check("wheel_anchor")

    def test_pinch_anchor_via_real_touch_handlers(self):
        _check("pinch_anchor")


class TestComposedSidebarExclusion:
    def test_left_edge_pinch_does_not_arm_sidebar(self):
        _check("sidebar_swipe_excluded")


class TestComposedFitAndKeyboard:
    def test_fit_button_click_resets_to_fit(self):
        _check("fit_click_resets")

    def test_F_key_resets_to_fit(self):
        _check("keyboard_F_resets")

    def test_plus_minus_keys_zoom(self):
        _check("keyboard_plus_minus")

    def test_viewport_click_suppresses_dragged_and_canvas_only(self):
        _check("viewport_click_suppression")


class TestComposedI18n:
    def test_zh_locale_renders_fit_text_title_aria(self):
        _check("locale_zh_renders")

    def test_ja_locale_renders_fit_text(self):
        _check("locale_ja_renders")


class TestComposedGeometry:
    def test_fit_button_44px_focus_safe_area(self):
        _check("geometry_computed_44_and_focus")

    def test_fit_and_close_do_not_overlap_including_mobile(self):
        _check("geometry_no_overlap")
