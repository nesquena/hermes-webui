"""Regression tests for issue #3066: skills panel disabled-state read path
must resolve against the active WebUI profile, not the process-global
HERMES_HOME.
"""
import yaml
from pathlib import Path

from tests.conftest import requires_agent_modules


def _write_config(config_path: Path, data: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(data), encoding="utf-8")


def _write_skill(skills_dir: Path, name: str) -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A test skill\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_disabled_read_uses_active_profile_config(tmp_path, monkeypatch):
    """_get_disabled_skill_names_for_profile reads from the active profile's
    config.yaml (via _get_config_path), not from HERMES_HOME."""
    from api import routes

    profile_home = tmp_path / "profiles" / "work"
    config_path = profile_home / "config.yaml"
    _write_config(config_path, {"skills": {"disabled": ["skill-x", "skill-y"]}})

    # Point _get_config_path at the profile config
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)

    result = routes._get_disabled_skill_names_for_profile()
    assert result == {"skill-x", "skill-y"}


def test_disabled_read_prefers_platform_disabled_webui(tmp_path, monkeypatch):
    """When platform_disabled.webui exists, it takes precedence over the
    global disabled list."""
    from api import routes

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "skills": {
            "disabled": ["global-disabled"],
            "platform_disabled": {
                "webui": ["webui-disabled-a", "webui-disabled-b"],
                "telegram": ["telegram-disabled"],
            },
        }
    })
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)

    result = routes._get_disabled_skill_names_for_profile()
    assert result == {"webui-disabled-a", "webui-disabled-b"}
    assert "global-disabled" not in result


def test_disabled_read_falls_back_to_global_disabled(tmp_path, monkeypatch):
    """When platform_disabled.webui is absent, falls back to skills.disabled."""
    from api import routes

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {"skills": {"disabled": ["fallback-skill"]}})
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)

    result = routes._get_disabled_skill_names_for_profile()
    assert result == {"fallback-skill"}


def test_disabled_read_empty_when_no_config(tmp_path, monkeypatch):
    """Returns empty set when config.yaml does not exist."""
    from api import routes

    config_path = tmp_path / "nonexistent" / "config.yaml"
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)

    result = routes._get_disabled_skill_names_for_profile()
    assert result == set()


def test_disabled_read_empty_when_no_skills_section(tmp_path, monkeypatch):
    """Returns empty set when config has no skills section."""
    from api import routes

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {"model": "gpt-4"})
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)

    result = routes._get_disabled_skill_names_for_profile()
    assert result == set()


@requires_agent_modules
def test_skills_list_disabled_reflects_active_profile(tmp_path, monkeypatch):
    """_skills_list_from_dir marks skills as disabled based on the active
    profile's config, not the process-global HERMES_HOME."""
    from api import routes

    # Set up skills directory with two skills
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "skill-a")
    _write_skill(skills_dir, "skill-b")

    # Profile config disables only skill-a
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {"skills": {"disabled": ["skill-a"]}})
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)
    monkeypatch.setattr("api.routes._active_skills_dir", lambda: skills_dir)

    result = routes._skills_list_from_dir(skills_dir)
    skills = {s["name"]: s for s in result["skills"]}

    assert skills["skill-a"]["disabled"] is True
    assert skills["skill-b"]["disabled"] is False


@requires_agent_modules
def test_profile_switch_changes_disabled_state(tmp_path, monkeypatch):
    """Simulates switching profiles: disabled state should follow the active
    profile's config, not remain stuck on the original profile."""
    from api import routes

    # Two profile configs with different disabled lists
    profile_a_config = tmp_path / "profile-a" / "config.yaml"
    profile_b_config = tmp_path / "profile-b" / "config.yaml"
    _write_config(profile_a_config, {"skills": {"disabled": ["skill-x"]}})
    _write_config(profile_b_config, {"skills": {"disabled": ["skill-y"]}})

    # Skills dir with both skills
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "skill-x")
    _write_skill(skills_dir, "skill-y")
    monkeypatch.setattr("api.routes._active_skills_dir", lambda: skills_dir)

    # "Active" profile A
    monkeypatch.setattr("api.routes._get_config_path", lambda: profile_a_config)
    result_a = routes._skills_list_from_dir(skills_dir)
    skills_a = {s["name"]: s for s in result_a["skills"]}
    assert skills_a["skill-x"]["disabled"] is True
    assert skills_a["skill-y"]["disabled"] is False

    # "Switch" to profile B
    monkeypatch.setattr("api.routes._get_config_path", lambda: profile_b_config)
    result_b = routes._skills_list_from_dir(skills_dir)
    skills_b = {s["name"]: s for s in result_b["skills"]}
    assert skills_b["skill-x"]["disabled"] is False
    assert skills_b["skill-y"]["disabled"] is True


def test_normalize_disabled_set_handles_edge_cases():
    """_normalize_disabled_set handles None, str, list, and whitespace."""
    from api.routes import _normalize_disabled_set

    assert _normalize_disabled_set(None) == set()
    assert _normalize_disabled_set("single") == {"single"}
    assert _normalize_disabled_set(["a", "b"]) == {"a", "b"}
    assert _normalize_disabled_set([" spaced ", "  "]) == {"spaced"}
    assert _normalize_disabled_set([]) == set()
