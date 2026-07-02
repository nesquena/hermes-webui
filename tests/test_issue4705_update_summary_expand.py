"""Static-analysis tests for #4705 (update summary panel expand control)."""
from pathlib import Path

ROOT = Path(__file__).parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


class TestIssue4705UpdateSummaryExpand:
    def test_summary_panel_has_expand_control(self):
        assert 'id="updateSummaryToolbar"' in INDEX_HTML
        assert 'id="btnUpdateSummaryExpand"' in INDEX_HTML
        assert 'id="updateSummaryScroll"' in INDEX_HTML
        assert 'onclick="toggleUpdateSummaryExpanded()"' in INDEX_HTML

    def test_toggle_function_toggles_expanded_class(self):
        assert "function toggleUpdateSummaryExpanded()" in UI_JS
        assert "update-summary-expanded" in UI_JS
        assert "_syncUpdateSummaryExpandButton" in UI_JS

    def test_render_shows_toolbar_and_resets_collapsed(self):
        idx = UI_JS.find("function _renderUpdateSummaryPanel(")
        assert idx >= 0
        body = UI_JS[idx:idx + 1200]
        assert "updateSummaryToolbar" in body
        assert "classList.remove('update-summary-expanded')" in body
        assert "_syncUpdateSummaryExpandButton(false)" in body

    def test_hide_clears_expanded_state(self):
        idx = UI_JS.find("function _hideUpdateSummaryPanel(")
        assert idx >= 0
        body = UI_JS[idx:idx + 700]
        assert "classList.remove('update-summary-expanded')" in body
        assert "_syncUpdateSummaryExpandButton(false)" in body

    def test_expanded_css_uses_larger_viewport_height(self):
        assert "#updateSummaryScroll{max-height:min(34vh,260px)" in STYLE_CSS
        assert "#updateSummaryPanel.update-summary-expanded #updateSummaryScroll{max-height:min(75vh,560px);}" in STYLE_CSS
        assert "@media (max-width:600px){#updateSummaryPanel.update-summary-expanded #updateSummaryScroll{max-height:min(82vh,640px);}}" in STYLE_CSS
