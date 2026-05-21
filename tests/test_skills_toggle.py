"""Tests for skill toggle (enable/disable) API and frontend."""
from pathlib import Path


PANELS_JS = (Path(__file__).resolve().parent.parent / "static" / "panels.js").read_text("utf-8")
I18N_JS = (Path(__file__).resolve().parent.parent / "static" / "i18n.js").read_text("utf-8")
STYLE_CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text("utf-8")


def test_toggle_endpoint_signature_in_routes():
    """Verify the toggle endpoint code exists in routes.py."""
    from api.routes import _handle_skill_toggle
    assert callable(_handle_skill_toggle)


def test_toggle_path_registered():
    """Verify /api/skills/toggle path is registered in POST routing."""
    routes_source = (Path(__file__).resolve().parent.parent / "api" / "routes.py").read_text("utf-8")
    assert '/api/skills/toggle' in routes_source


def test_skills_list_includes_disabled_flag():
    """Each skill in the API response must have a 'disabled' boolean."""
    from api.routes import _skills_list_from_dir, _active_skills_dir
    result = _skills_list_from_dir(_active_skills_dir())
    for skill in result.get("skills", []):
        assert "disabled" in skill, f"Skill {skill.get('name')} missing 'disabled' field"
        assert isinstance(skill["disabled"], bool), f"Skill {skill.get('name')} disabled must be bool"


def test_i18n_keys_added():
    """The three new i18n keys must exist in the English locale."""
    assert "skill_enabled" in I18N_JS
    assert "skill_disabled" in I18N_JS
    assert "skill_toggle_failed" in I18N_JS


def test_toggle_css_classes_exist():
    """Toggle switch CSS classes must be in style.css."""
    assert ".skill-toggle" in STYLE_CSS
    assert ".skill-toggle.enabled" in STYLE_CSS
    assert ".skill-item.disabled" in STYLE_CSS


def test_render_skills_produces_toggle_buttons():
    """renderSkills() must include toggleSkill and skill-toggle."""
    assert "toggleSkill(" in PANELS_JS
    assert "skill-toggle" in PANELS_JS


def test_toggle_skill_function_defined():
    """toggleSkill() async function must be defined."""
    assert "async function toggleSkill(" in PANELS_JS
