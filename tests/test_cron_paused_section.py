"""Regression coverage for paused cron jobs collapsing into their own sidebar section."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_load_crons_groups_paused_jobs_into_separate_section():
    assert "const visibleJobs = [];" in PANELS_JS
    assert "const pausedJobs = [];" in PANELS_JS
    assert "if (status.state === 'paused') pausedJobs.push(job);" in PANELS_JS
    assert "for (const job of visibleJobs) box.appendChild(_buildCronListItem(job));" in PANELS_JS
    assert "_appendCronPausedSection(box, pausedJobs);" in PANELS_JS


def test_paused_section_state_persists_and_selected_paused_job_forces_expand():
    assert "hermes-webui-cron-paused-collapsed" in PANELS_JS
    assert "function _cronPausedSectionCollapsed()" in PANELS_JS
    assert "function _setCronPausedSectionCollapsed(collapsed)" in PANELS_JS
    assert "const hasSelectedPausedJob = !!(_currentCronDetail && pausedJobs.some(job => String(job.id) === String(_currentCronDetail.id)));" in PANELS_JS
    assert "const collapsed = hasSelectedPausedJob ? false : _cronPausedSectionCollapsed();" in PANELS_JS


def test_paused_section_toggle_updates_dom_without_refetching_crons():
    assert "const nowCollapsed = !body.classList.contains('collapsed');" in PANELS_JS
    assert "body.classList.toggle('collapsed', nowCollapsed);" in PANELS_JS
    assert "header.setAttribute('aria-expanded', nowCollapsed ? 'false' : 'true');" in PANELS_JS
    assert "loadCrons(false);" not in PANELS_JS


def test_paused_section_has_copy_and_styles():
    assert "cron_paused_section: 'Paused'" in I18N_JS
    assert "cron_expand_section: 'Expand section'" in I18N_JS
    assert "cron_collapse_section: 'Collapse section'" in I18N_JS
    assert ".cron-group-toggle" in STYLE_CSS
    assert ".cron-group-body.collapsed" in STYLE_CSS
