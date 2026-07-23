"""Profile isolation contract for Agent TTS subprocess environments."""

from __future__ import annotations

import os

from api import profiles


def test_agent_tts_child_env_is_non_mutating_and_scrubs_other_profile_secrets(
    tmp_path, monkeypatch
):
    selected = tmp_path / "profiles" / "voice"
    other = tmp_path / "profiles" / "other"
    default = tmp_path / "default"
    for home in (selected, other, default):
        home.mkdir(parents=True)
    monkeypatch.setenv("DEFAULT_ONLY_SECRET", "default-secret")
    monkeypatch.setenv("OTHER_ONLY_SECRET", "other-secret")
    monkeypatch.setenv("OTHER_CUSTOM_COMMAND_VAR", "other-command-secret")
    monkeypatch.setenv("SELECTED_SECRET", "stale-selected")
    monkeypatch.setenv("UNRELATED_SAFE", "keep")
    before = dict(os.environ)
    other.joinpath(".env").write_text(
        "OTHER_CUSTOM_COMMAND_VAR=other-command-secret\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        profiles,
        "_all_profile_homes_for_env_scrub",
        lambda: [default, selected, other],
        raising=False,
    )
    monkeypatch.setattr(
        profiles,
        "_profile_secret_env_names",
        lambda home: {
            default: {"DEFAULT_ONLY_SECRET"},
            selected: {"SELECTED_SECRET"},
            other: {"OTHER_ONLY_SECRET"},
        }[home],
    )
    monkeypatch.setattr(
        profiles,
        "get_profile_runtime_env",
        lambda home: {"SELECTED_SECRET": "selected-value", "PROFILE_FLAG": "yes"},
    )

    child = profiles.build_profile_subprocess_env("voice", selected)

    assert os.environ == before
    assert child["UNRELATED_SAFE"] == "keep"
    assert child["SELECTED_SECRET"] == "selected-value"
    assert child["PROFILE_FLAG"] == "yes"
    assert "DEFAULT_ONLY_SECRET" not in child
    assert "OTHER_ONLY_SECRET" not in child
    assert "OTHER_CUSTOM_COMMAND_VAR" not in child
    assert child["HERMES_HOME"] == str(selected.resolve())
    assert child["HERMES_CONFIG_PATH"] == str((selected / "config.yaml").resolve())
    assert child["HERMES_SESSION_PLATFORM"] == "webui"


def test_agent_tts_child_env_excludes_webui_secrets_and_loader_injection(
    tmp_path, monkeypatch
):
    home = tmp_path / "profiles" / "voice"
    home.mkdir(parents=True)
    inherited = {
        "HERMES_WEBUI_PASSWORD": "webui-password",
        "HERMES_WEBUI_OIDC_CLIENT_SECRET": "oidc-secret",
        "HERMES_WEBUI_TTS_REQUEST_MAX_CHARS": "1234",
        "LD_LIBRARY_PATH": "/tmp/untrusted-libraries",
        "LD_PRELOAD": "/tmp/untrusted.so",
        "PYTHONHOME": "/tmp/untrusted-python",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        profiles,
        "get_profile_runtime_env",
        lambda _home: {
            **inherited,
            "SELECTED_TTS_KEY": "selected-secret",
        },
    )
    monkeypatch.setattr(profiles, "_all_profile_homes_for_env_scrub", lambda: [home])
    monkeypatch.setattr(profiles, "_profile_secret_env_names", lambda _home: set())

    child = profiles.build_profile_subprocess_env("voice", home)

    for key in inherited:
        if key == "HERMES_WEBUI_TTS_REQUEST_MAX_CHARS":
            continue
        assert key not in child
    assert child["HERMES_WEBUI_TTS_REQUEST_MAX_CHARS"] == "1234"
    assert child["SELECTED_TTS_KEY"] == "selected-secret"
    assert child["HERMES_HOME"] == str(home.resolve())


def test_agent_tts_child_env_filters_unsafe_runtime_keys(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(profiles, "_all_profile_homes_for_env_scrub", lambda: [home], raising=False)
    monkeypatch.setattr(profiles, "_profile_secret_env_names", lambda _home: set())
    monkeypatch.setattr(
        profiles,
        "get_profile_runtime_env",
        lambda _home: {
            "GOOD_KEY": "ok",
            "HERMES_HOME": "/attacker",
            "PYTHONPATH": "/attacker",
            "HERMES_CONFIG_PATH": "/attacker/config",
        },
    )

    child = profiles.build_profile_subprocess_env("voice", home)

    assert child["GOOD_KEY"] == "ok"
    assert child["HERMES_HOME"] == str(home.resolve())
    assert child["HERMES_CONFIG_PATH"] == str((home / "config.yaml").resolve())
    assert child.get("PYTHONPATH") != "/attacker"


def test_agent_tts_request_limit_passthrough_is_clamped_and_not_profile_owned(
    tmp_path, monkeypatch
):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_TTS_REQUEST_MAX_CHARS", "999999")
    monkeypatch.setattr(profiles, "_all_profile_homes_for_env_scrub", lambda: [home])
    monkeypatch.setattr(profiles, "_profile_secret_env_names", lambda _home: set())
    monkeypatch.setattr(
        profiles,
        "get_profile_runtime_env",
        lambda _home: {"HERMES_WEBUI_TTS_REQUEST_MAX_CHARS": "256"},
    )

    child = profiles.build_profile_subprocess_env("voice", home)

    assert child["HERMES_WEBUI_TTS_REQUEST_MAX_CHARS"] == "10000"
