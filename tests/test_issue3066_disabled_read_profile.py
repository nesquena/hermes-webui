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


def test_disabled_read_unions_platform_and_global(tmp_path, monkeypatch):
    """platform_disabled.webui ADDS to the global disabled list, the way
    agent.skill_utils.get_disabled_skill_names does. Another platform's list
    is not read here."""
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
    assert result == {"global-disabled", "webui-disabled-a", "webui-disabled-b"}
    assert "telegram-disabled" not in result


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


def test_disabled_read_decodes_json_array_string(tmp_path, monkeypatch):
    """Issue #7120: skills.disabled stored as a JSON-array string (the shape
    produced by `hermes config set skills.disabled ...`) must decode to the
    real names, not one literal entry."""
    from api import routes

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {"skills": {"disabled": '["skill-x", "skill-y"]'}})
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)

    result = routes._get_disabled_skill_names_for_profile()
    assert result == {"skill-x", "skill-y"}


def test_disabled_read_platform_webui_decodes_json_array_string(tmp_path, monkeypatch):
    """Issue #7120: platform_disabled.webui stored as a JSON-array string is
    decoded the same way as the global disabled list, and both end up in the
    union."""
    from api import routes

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "skills": {
            "disabled": ["global-disabled"],
            "platform_disabled": {"webui": '["webui-disabled-a", "webui-disabled-b"]'},
        }
    })
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)

    result = routes._get_disabled_skill_names_for_profile()
    assert result == {"global-disabled", "webui-disabled-a", "webui-disabled-b"}


def test_disabled_read_subtracts_essential_skills(tmp_path, monkeypatch):
    """An essential skill is never reported disabled, whatever the config says:
    the agent loads it anyway, so showing it as disabled here would be a lie."""
    from api import routes

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "skills": {
            "disabled": ["hermes-agent", "skill-x"],
            "platform_disabled": {"webui": ["hermes-agent", "skill-y"]},
        }
    })
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)
    monkeypatch.setattr("api.routes._essential_skill_names", lambda: {"hermes-agent"})

    result = routes._get_disabled_skill_names_for_profile()
    assert result == {"skill-x", "skill-y"}


def test_essential_skill_names_falls_back_without_agent_modules(monkeypatch):
    """Not every deployment can import the agent package. The fallback keeps
    the panel usable and still protects hermes-agent."""
    import sys

    from api import routes

    monkeypatch.setitem(sys.modules, "agent.skill_utils", None)
    assert routes._essential_skill_names() == {"hermes-agent"}


@requires_agent_modules
def test_essential_skill_names_reads_the_agent_set():
    """When the agent package is importable, the set comes from it, so an
    upstream change to ESSENTIAL_SKILLS moves this UI with it."""
    from agent.skill_utils import ESSENTIAL_SKILLS

    from api import routes

    assert routes._essential_skill_names() == {str(n) for n in ESSENTIAL_SKILLS}


@requires_agent_modules
def test_skills_list_disabled_decodes_json_array_string(tmp_path, monkeypatch):
    """Issue #7120: the Skills panel (skills list endpoint) must report both
    skills as disabled when config.yaml stores disabled as a JSON-array string."""
    from api import routes

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "skill-a")
    _write_skill(skills_dir, "skill-b")

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {"skills": {"disabled": '["skill-a", "skill-b"]'}})
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)
    monkeypatch.setattr("api.routes._active_skills_dir", lambda: skills_dir)

    result = routes._skills_list_from_dir(skills_dir)
    skills = {s["name"]: s for s in result["skills"]}

    assert skills["skill-a"]["disabled"] is True
    assert skills["skill-b"]["disabled"] is True


def _essential_toggle_env(monkeypatch, config_path, skills_dir):
    """Point the toggle handler at a throwaway profile and make it return its payload.

    ``_find_skill_in_dirs`` is stubbed the way tests/test_skills_toggle.py stubs it, so the
    write path can be exercised without the agent package.
    """
    monkeypatch.setattr("api.routes._get_config_path", lambda: config_path)
    monkeypatch.setattr("api.routes._active_skills_dir", lambda: skills_dir)
    monkeypatch.setattr(
        "api.routes._find_skill_in_dirs",
        lambda n, dirs: (skills_dir / n, skills_dir / n / "SKILL.md"),
    )
    monkeypatch.setattr("api.routes._essential_skill_names", lambda: {"hermes-agent"})
    monkeypatch.setattr("api.routes.reload_config", lambda: None)
    monkeypatch.setattr("api.routes.j", lambda _handler, payload: payload)
    monkeypatch.setattr(
        "api.routes.bad",
        lambda _handler, message, status=400: {"error": message, "status": status},
    )


def test_toggle_cannot_persist_an_essential_skill_as_disabled(tmp_path, monkeypatch):
    """The write path honours the invariant the read path enforces. Disabling an essential
    skill used to be written to both lists and echoed back as disabled while the agent and
    the read path went on loading it, so the panel rendered a state nothing else believed."""
    from api import routes

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "hermes-agent")
    _write_skill(skills_dir, "skill-x")

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "skills": {"disabled": [], "platform_disabled": {"webui": []}},
    })
    _essential_toggle_env(monkeypatch, config_path, skills_dir)

    response = routes._handle_skill_toggle(None, {"name": "hermes-agent", "enabled": False})

    # the response carries the state that was written, not the state that was asked for
    assert response == {"ok": True, "name": "hermes-agent", "enabled": True}
    # nothing landed in either list
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert cfg["skills"]["disabled"] == []
    assert cfg["skills"]["platform_disabled"]["webui"] == []
    # and the effective read agrees with both
    assert "hermes-agent" not in routes._get_disabled_skill_names_for_profile()

    # an ordinary skill still toggles, so this is a guard and not a blanket refusal
    assert routes._handle_skill_toggle(None, {"name": "skill-x", "enabled": False}) == {
        "ok": True, "name": "skill-x", "enabled": False,
    }
    assert "skill-x" in routes._get_disabled_skill_names_for_profile()


def test_toggle_strips_an_essential_skill_an_earlier_write_left_behind(tmp_path, monkeypatch):
    """A config written before this guard existed can still name an essential skill. Clicking
    that row clears it from both lists instead of leaving it there to be ignored forever."""
    from api import routes

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "hermes-agent")

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "skills": {
            "disabled": ["hermes-agent", "skill-y"],
            "platform_disabled": {"webui": ["hermes-agent"], "telegram": ["skill-z"]},
        },
    })
    _essential_toggle_env(monkeypatch, config_path, skills_dir)

    response = routes._handle_skill_toggle(None, {"name": "hermes-agent", "enabled": False})

    assert response == {"ok": True, "name": "hermes-agent", "enabled": True}
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert cfg["skills"]["disabled"] == ["skill-y"]
    assert cfg["skills"]["platform_disabled"]["webui"] == []
    # another platform's list is not this endpoint's business
    assert cfg["skills"]["platform_disabled"]["telegram"] == ["skill-z"]


@requires_agent_modules
def test_skills_listing_agrees_after_an_essential_toggle(tmp_path, monkeypatch):
    """The last link in the chain: the listing the panel renders from reports the essential
    skill as enabled after the write, so persistence, response, read and panel all agree."""
    from api import routes

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "hermes-agent")
    _write_skill(skills_dir, "skill-x")

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "skills": {"disabled": ["hermes-agent"], "platform_disabled": {"webui": []}},
    })
    _essential_toggle_env(monkeypatch, config_path, skills_dir)

    routes._handle_skill_toggle(None, {"name": "hermes-agent", "enabled": False})

    listed = {s["name"]: s for s in routes._skills_list_from_dir(skills_dir)["skills"]}
    assert listed["hermes-agent"]["disabled"] is False
    assert listed["skill-x"]["disabled"] is False


def test_panel_toggle_renders_the_state_the_server_returned():
    """static/panels.js must read result.enabled instead of inverting the click, or the guard
    above is invisible in the UI: the row would still flip to disabled for one render."""
    source = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(
        encoding="utf-8"
    )
    idx = source.find("async function toggleSkill(")
    assert idx != -1
    body = source[idx:idx + 1200]
    assert "result.enabled" in body
    assert "skill.disabled = !newEnabled" not in body
