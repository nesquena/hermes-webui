"""Source-level secondary guards for image-lightbox gestures.

The behavioral proof now lives in ``test_img_lightbox_gestures_composed.py``,
which mounts the real lightbox inside a JSDOM and drives its registered
listeners. This file keeps cheap source locks as a secondary regression net
so a marker regression fails fast without spinning the harness.

No ``eval(`` is used here — the threat scan flags that pattern as SUSPICIOUS.
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
        assert "img_lightbox_fit: '\u9002\u5e94'" in i18n
        assert "Reset zoom to fit (F)" in i18n

    def test_fit_button_geometry_and_focus_scoped_to_rule(self):
        rule = _fit_rule()
        assert "min-height:44px" in rule, "fit rule must have min-height:44px"
        assert "height:44px" in rule, "fit rule must have height:44px"
        css = STYLE.read_text(encoding="utf-8")
        assert ".img-lightbox-fit:focus-visible" in css
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
