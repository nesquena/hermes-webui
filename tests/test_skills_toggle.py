"""Tests for skill toggle (enable/disable) API."""
import json
from pathlib import Path


PANELS_JS = (Path(__file__).resolve().parent.parent / "static" / "panels.js").read_text("utf-8")


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
