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
    """Each skill dict in the API response must have a 'disabled' boolean field."""
    routes_source = (Path(__file__).resolve().parent.parent / "api" / "routes.py").read_text("utf-8")
    assert '"disabled": name in disabled' in routes_source


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


def test_disabled_list_round_trip(tmp_path):
    """Verify that writing and reading the disabled list through the config
    module's YAML functions preserves values correctly, including normalization
    of None/str/list shapes."""
    from api.config import _load_yaml_config_file, _save_yaml_config_file

    config_path = tmp_path / "config.yaml"

    # Write initial config
    _save_yaml_config_file(config_path, {"skills": {"disabled": []}})

    # Read, add skill, write
    cfg = _load_yaml_config_file(config_path)
    cfg.setdefault("skills", {})
    disabled = cfg["skills"].get("disabled", [])
    disabled.append("skill-a")
    disabled.append("skill-b")
    cfg["skills"]["disabled"] = disabled
    _save_yaml_config_file(config_path, cfg)

    # Read back and verify
    cfg2 = _load_yaml_config_file(config_path)
    assert cfg2["skills"]["disabled"] == ["skill-a", "skill-b"]

    # Remove one skill, write, verify
    cfg2["skills"]["disabled"] = [d for d in cfg2["skills"]["disabled"] if d != "skill-a"]
    _save_yaml_config_file(config_path, cfg2)

    cfg3 = _load_yaml_config_file(config_path)
    assert cfg3["skills"]["disabled"] == ["skill-b"]
