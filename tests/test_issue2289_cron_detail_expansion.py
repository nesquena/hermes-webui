"""Static coverage for issue #2289 cron detail expansion controls."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_cron_prompt_and_run_expansion_helpers_are_persisted():
    assert "function _cronPanelExpandKey(jobId, suffix)" in PANELS_JS
    assert "function _cronRunExpandKey(jobId, filename)" in PANELS_JS
    assert "localStorage.setItem(key, expanded ? '1' : '0')" in PANELS_JS
    assert "toggleCronPromptExpanded" in PANELS_JS
    assert "toggleCronRunExpanded" in PANELS_JS


def test_cron_detail_renders_prompt_and_run_expand_buttons():
    assert "cron_expand_prompt" in PANELS_JS
    assert "cron_collapse_prompt" in PANELS_JS
    assert "class=\"detail-card-title detail-card-title-row\"" in PANELS_JS
    assert "class=\"detail-prompt ${promptExpanded ? 'expanded' : ''}\"" in PANELS_JS
    assert "class=\"detail-run-body ${runExpanded ? 'expanded' : ''}\"" in PANELS_JS
    assert "event.stopPropagation();toggleCronRunExpanded" in PANELS_JS


def test_cron_detail_expanded_state_removes_nested_scroll_caps():
    assert ".detail-prompt.expanded{max-height:none;overflow-y:visible;}" in STYLE_CSS
    assert ".detail-run-body.expanded{max-height:none;overflow-y:visible;}" in STYLE_CSS
    assert ".detail-expand-toggle" in STYLE_CSS


def test_cron_expansion_i18n_keys_exist_in_every_locale():
    locale_count = I18N_JS.count("cron_last_output:")
    assert locale_count >= 9
    for key in (
        "cron_expand_prompt",
        "cron_collapse_prompt",
        "cron_expand_output",
        "cron_collapse_output",
    ):
        assert I18N_JS.count(f"{key}:") >= locale_count
