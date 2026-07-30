"""Regression coverage for #4465 built-in personality visibility.

Hermes Agent CLI ships built-in personalities from its CLI config loader. WebUI
should expose the same defaults through config-derived personality paths so a
fresh profile is not empty.
"""

from api import config


BUILTIN_NAMES = {
    "helpful",
    "concise",
    "technical",
    "creative",
    "teacher",
    "kawaii",
    "catgirl",
    "pirate",
    "shakespeare",
    "surfer",
    "noir",
    "uwu",
    "philosopher",
    "hype",
}


def test_config_defaults_seed_agent_builtin_personalities_for_fresh_profile():
    cfg = {}

    config._apply_config_defaults(cfg)

    personalities = cfg["agent"]["personalities"]
    assert set(personalities) == BUILTIN_NAMES
    assert personalities["helpful"] == "You are a helpful, friendly AI assistant."
    assert "desu~!" in personalities["kawaii"]
    assert "William Shakespeare" in personalities["shakespeare"]
    assert personalities["hype"].startswith("YOOO LET'S GOOOO!!!")


def test_config_defaults_preserve_custom_personality_overrides():
    cfg = {
        "agent": {
            "personalities": {
                "helpful": "Custom helpful prompt.",
                "local": {
                    "description": "Local voice",
                    "prompt": "Use the local team voice.",
                },
            }
        }
    }

    config._apply_config_defaults(cfg)

    personalities = cfg["agent"]["personalities"]
    assert personalities["helpful"] == "Custom helpful prompt."
    assert personalities["local"]["prompt"] == "Use the local team voice."
    assert "kawaii" in personalities
    assert "shakespeare" in personalities


def test_config_defaults_replace_non_dict_personality_section_with_builtins():
    cfg = {"agent": {"personalities": []}}

    config._apply_config_defaults(cfg)

    assert set(cfg["agent"]["personalities"]) == BUILTIN_NAMES


def test_profile_home_config_read_hydrates_builtin_personalities_without_writing(tmp_path):
    profile_home = tmp_path / "profiles" / "research"
    profile_home.mkdir(parents=True)

    cfg = config.get_config_for_profile_home(profile_home)

    personalities = cfg["agent"]["personalities"]
    assert set(personalities) == BUILTIN_NAMES
    assert personalities["helpful"] == "You are a helpful, friendly AI assistant."
    assert not (profile_home / "config.yaml").exists()


def test_profile_home_config_read_merges_custom_personalities(tmp_path):
    profile_home = tmp_path / "profiles" / "research"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "\n".join(
            [
                "agent:",
                "  personalities:",
                "    helpful: Custom helpful prompt.",
                "    analyst:",
                "      system_prompt: Use careful evidence.",
            ]
        ),
        encoding="utf-8",
    )

    cfg = config.get_config_for_profile_home(profile_home)

    personalities = cfg["agent"]["personalities"]
    assert set(BUILTIN_NAMES).issubset(personalities)
    assert personalities["helpful"] == "Custom helpful prompt."
    assert personalities["analyst"]["system_prompt"] == "Use careful evidence."


def test_config_save_strips_generated_builtin_personalities(tmp_path):
    cfg = {}
    config._apply_config_defaults(cfg)
    cfg["webui"] = {"theme": "dark"}

    path = tmp_path / "config.yaml"
    config._save_yaml_config_file(path, cfg)

    text = path.read_text(encoding="utf-8")
    assert "webui:" in text
    assert "theme: dark" in text
    assert "personalities:" not in text
    assert "helpful:" not in text


def test_config_save_preserves_custom_personality_overrides(tmp_path):
    cfg = {
        "agent": {
            "personality": "local",
            "personalities": {
                "helpful": "Custom helpful prompt.",
                "local": "Use the local team voice.",
            },
        }
    }
    config._apply_config_defaults(cfg)

    path = tmp_path / "config.yaml"
    config._save_yaml_config_file(path, cfg)

    text = path.read_text(encoding="utf-8")
    assert "personalities:" in text
    assert "helpful: Custom helpful prompt." in text
    assert "local: Use the local team voice." in text
    assert "kawaii:" not in text
