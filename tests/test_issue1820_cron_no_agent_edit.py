from pathlib import Path


PANELS_JS = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")


def test_cron_edit_plumbs_no_agent_and_script_into_form():
    """Editing no-agent jobs needs their mode metadata in the form renderer."""
    open_edit_start = PANELS_JS.index("function openCronEdit(job)")
    render_call = PANELS_JS.index("_renderCronForm({", open_edit_start)
    render_call_end = PANELS_JS.index("});", render_call)
    render_call_src = PANELS_JS[render_call:render_call_end]

    assert "noAgent:" in render_call_src
    assert "job.no_agent" in render_call_src
    assert "script:" in render_call_src
    assert "job.script" in render_call_src


def test_cron_prompt_required_only_for_agent_jobs():
    """No-agent cron jobs run scripts directly, so prompt validation must be skipped."""
    render_form_start = PANELS_JS.index("function _renderCronForm")
    save_start = PANELS_JS.index("async function saveCronForm()")
    render_form_src = PANELS_JS[render_form_start:save_start]
    save_src = PANELS_JS[save_start:PANELS_JS.index("// Back-compat aliases", save_start)]

    assert "noAgent" in render_form_src
    assert "cronFormPrompt" in render_form_src
    assert "promptRequired ? 'required' : ''" in render_form_src
    assert "const isNoAgent" in save_src
    assert "_editingCronId &&" in save_src
    assert "if(!isNoAgent&&!prompt)" in save_src.replace(" ", "")


def test_cron_update_does_not_overwrite_prompt_for_no_agent_jobs():
    """Editing schedule/name/profile for no-agent jobs should leave prompt untouched."""
    save_start = PANELS_JS.index("async function saveCronForm()")
    save_src = PANELS_JS[save_start:PANELS_JS.index("// Back-compat aliases", save_start)]

    assert "if(!isNoAgent)updates.prompt=prompt" in save_src.replace(" ", "")
    assert "isNoAgent" in save_src
