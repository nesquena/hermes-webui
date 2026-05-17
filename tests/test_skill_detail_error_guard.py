from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def test_skill_detail_api_error_renders_in_detail_pane():
    assert "function _renderSkillError(name, message)" in PANELS_JS
    assert "data.success === false || data.error" in PANELS_JS
    assert "_renderSkillError(name, message);" in PANELS_JS
    assert "_currentSkillDetail = null;" in PANELS_JS
    assert "_setSkillHeaderButtons('empty');" in PANELS_JS


def test_skill_detail_no_longer_silently_falls_back_on_error_payload():
    success_guard = PANELS_JS.index("data.success === false || data.error")
    render_detail = PANELS_JS.index("_renderSkillDetail(name, data.content || ''")
    assert success_guard < render_detail


def test_skill_linked_file_api_error_renders_in_detail_pane():
    file_fetch = PANELS_JS.index("/api/skills/content?name=${encodeURIComponent(skillName)}&file=")
    file_error_guard = PANELS_JS.index("if (data && data.error)", file_fetch)
    file_render = PANELS_JS.index("_renderSkillError(skillName, data.error);", file_fetch)
    body_lookup = PANELS_JS.index("const body = $('skillDetailBody');", file_fetch)
    assert file_error_guard < body_lookup
    assert file_render < body_lookup
