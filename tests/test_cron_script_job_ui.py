"""Static coverage for script (no_agent) cron jobs in Tasks UI."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_cron_script_job_helpers_exist():
    assert "function _isCronScriptJob(job)" in PANELS_JS
    assert "function _cronModeLabel(job)" in PANELS_JS
    assert "function _cronOutputTitle(job)" in PANELS_JS
    assert "function _cronScriptJobBannerHtml()" in PANELS_JS
    assert "function _cronScriptCardHtml(job)" in PANELS_JS
    assert "function _cronAgentPromptCardHtml(job)" in PANELS_JS


def test_cron_detail_branches_on_no_agent():
    assert "const isNoAgent = _isCronScriptJob(job)" in PANELS_JS
    assert "isNoAgent ? _cronScriptCardHtml(job) : _cronAgentPromptCardHtml(job)" in PANELS_JS
    assert "isNoAgent ? _cronScriptJobBannerHtml() : ''" in PANELS_JS
    assert 'class="detail-script"' in PANELS_JS
    assert 'class="detail-alert cron-script-job-banner"' in PANELS_JS
    assert "cron-mode-badge ${isNoAgent ? 'script' : 'agent'}" in PANELS_JS


def test_cron_list_shows_script_badge():
    assert "cron-script-badge" in PANELS_JS
    assert "cron_script_badge_title" in PANELS_JS


def test_cron_form_hides_prompt_for_script_jobs():
    assert "const isNoAgent = !!no_agent" in PANELS_JS
    assert "const promptBlock = isNoAgent ? '' :" in PANELS_JS
    assert "const scriptBlock = isNoAgent ?" in PANELS_JS
    assert 'id="cronFormScript"' in PANELS_JS
    assert "if(!isNoAgent && !prompt)" in PANELS_JS


def test_cron_runs_skip_usage_strip_for_script_jobs():
    assert "const isScriptJob = _isCronScriptJob(_currentCronDetail)" in PANELS_JS
    assert "const usageStrip = isScriptJob ? '' : _formatCronRunUsageStrip(run.usage)" in PANELS_JS


def test_cron_script_job_styles_exist():
    assert ".cron-script-badge" in STYLE_CSS
    assert ".cron-script-job-banner" in STYLE_CSS
    assert ".detail-script{" in STYLE_CSS
    assert ".detail-badge.cron-mode-badge.script" in STYLE_CSS
    assert ".detail-badge.cron-mode-badge.agent" in STYLE_CSS


def test_cron_script_job_i18n_keys_exist_in_every_locale():
    locale_count = I18N_JS.count("cron_last_output:")
    assert locale_count >= 9
    for key in (
        "cron_mode_agent",
        "cron_mode_script",
        "cron_mode_label",
        "cron_script_job_banner",
        "cron_script_card_title",
        "cron_script_output",
        "cron_script_path_label",
        "cron_script_path_hint",
        "cron_script_badge_title",
        "cron_workdir_label",
    ):
        assert I18N_JS.count(f"{key}:") >= locale_count, key
