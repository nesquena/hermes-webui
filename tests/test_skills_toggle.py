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


def test_normalize_names_list():
    """_normalize_names_list handles None, str, list, and deduplicates."""
    from api.routes import _normalize_names_list

    assert _normalize_names_list(None) == []
    assert _normalize_names_list("foo") == ["foo"]
    assert _normalize_names_list(["a", "b"]) == ["a", "b"]
    assert _normalize_names_list(["a", "a"]) == ["a"]
    assert _normalize_names_list([" a ", "b"]) == ["a", "b"]
    assert _normalize_names_list("") == []
    assert _normalize_names_list([]) == []


def test_toggle_name_in_list():
    """_toggle_name_in_list adds when enabled=False, removes when enabled=True."""
    from api.routes import _toggle_name_in_list

    # Add to empty list
    assert _toggle_name_in_list([], "foo", enabled=False) == ["foo"]
    # Add to existing list
    assert _toggle_name_in_list(["bar"], "foo", enabled=False) == ["bar", "foo"]
    # Idempotent add
    assert _toggle_name_in_list(["foo"], "foo", enabled=False) == ["foo"]
    # Remove from list
    assert _toggle_name_in_list(["foo", "bar"], "foo", enabled=True) == ["bar"]
    # Remove non-existent is a no-op
    assert _toggle_name_in_list(["bar"], "foo", enabled=True) == ["bar"]
    # Remove from empty is a no-op
    assert _toggle_name_in_list([], "foo", enabled=True) == []
    # Handles str input (normalization)
    assert _toggle_name_in_list("foo", "bar", enabled=False) == ["foo", "bar"]
    assert _toggle_name_in_list("foo", "foo", enabled=True) == []


def test_platform_disabled_write_through(tmp_path, monkeypatch):
    """Toggle writes through to platform_disabled.webui when that key exists."""
    from unittest.mock import MagicMock
    from api.routes import _handle_skill_toggle
    from api.config import _load_yaml_config_file

    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)

    import yaml

    config = {
        "skills": {
            "disabled": ["skill-a"],
            "platform_disabled": {
                "webui": ["skill-a", "skill-b"],
                "telegram": ["skill-c"],
            },
        }
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(config), encoding="utf-8")

    # Mock _find_skill_in_dirs to avoid agent.skill_utils import in CI
    fake_dir = tmp_path / "skills" / "skill-a"
    fake_dir.mkdir(parents=True, exist_ok=True)
    fake_md = fake_dir / "SKILL.md"
    fake_md.write_text("---\nname: skill-a\n---\nA skill", encoding="utf-8")
    monkeypatch.setattr(
        "api.routes._find_skill_in_dirs",
        lambda name, dirs: (fake_dir, fake_md),
    )

    handler = MagicMock()

    # Toggle skill-a OFF — it's already disabled, verify idempotency
    _handle_skill_toggle(handler, {"name": "skill-a", "enabled": False})
    cfg_after = _load_yaml_config_file(config_path)
    assert "skill-a" in cfg_after["skills"]["disabled"]
    assert "skill-a" in cfg_after["skills"]["platform_disabled"]["webui"]

    # Toggle skill-a ON
    _handle_skill_toggle(handler, {"name": "skill-a", "enabled": True})
    cfg_after2 = _load_yaml_config_file(config_path)
    assert "skill-a" not in cfg_after2["skills"]["disabled"]
    assert "skill-a" not in cfg_after2["skills"]["platform_disabled"]["webui"]
    # Other platform keys are untouched
    assert cfg_after2["skills"]["platform_disabled"]["telegram"] == ["skill-c"]


def test_platform_disabled_no_write_through_when_key_absent(tmp_path, monkeypatch):
    """Toggle does NOT create platform_disabled.webui when it doesn't exist."""
    from unittest.mock import MagicMock
    from api.routes import _handle_skill_toggle
    from api.config import _load_yaml_config_file

    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)

    import yaml

    config = {"skills": {"disabled": []}}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(config), encoding="utf-8")

    # Mock _find_skill_in_dirs to avoid agent.skill_utils import in CI
    fake_dir = tmp_path / "skills" / "skill-b"
    fake_dir.mkdir(parents=True, exist_ok=True)
    fake_md = fake_dir / "SKILL.md"
    fake_md.write_text("---\nname: skill-b\n---\nB skill", encoding="utf-8")
    monkeypatch.setattr(
        "api.routes._find_skill_in_dirs",
        lambda name, dirs: (fake_dir, fake_md),
    )

    handler = MagicMock()
    _handle_skill_toggle(handler, {"name": "skill-b", "enabled": False})
    cfg_after = _load_yaml_config_file(config_path)
    assert "skill-b" in cfg_after["skills"]["disabled"]
    # platform_disabled was never created
    assert "platform_disabled" not in cfg_after.get("skills", {})
