import importlib
import os
import sys
from pathlib import Path


def test_profile_switch_clears_previous_profile_env_vars(monkeypatch, tmp_path):
    base = tmp_path / ".hermes"
    (base / "profiles" / "p1").mkdir(parents=True)
    (base / "profiles" / "p2").mkdir(parents=True)
    (base / "profiles" / "p1" / ".env").write_text(
        "OPENAI_API_KEY=secret-from-p1\nCUSTOM_TOKEN=token-from-p1\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_BASE_HOME", str(base))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_TOKEN", raising=False)

    # Use monkeypatch so sys.modules is restored after the test, preventing
    # api.profiles from being permanently removed and poisoning subsequent tests.
    monkeypatch.delitem(sys.modules, "api.profiles", raising=False)
    profiles = importlib.import_module("api.profiles")

    profiles.init_profile_state()
    profiles.switch_profile("p1")
    assert os.environ.get("OPENAI_API_KEY") == "secret-from-p1"
    assert os.environ.get("CUSTOM_TOKEN") == "token-from-p1"

    profiles.switch_profile("p2")
    assert os.environ.get("OPENAI_API_KEY") is None
    assert os.environ.get("CUSTOM_TOKEN") is None
    assert profiles.get_active_profile_name() == "p2"


def test_profile_switch_replaces_overlapping_keys(monkeypatch, tmp_path):
    base = tmp_path / ".hermes"
    (base / "profiles" / "p1").mkdir(parents=True)
    (base / "profiles" / "p2").mkdir(parents=True)
    (base / "profiles" / "p1" / ".env").write_text(
        "OPENAI_API_KEY=secret-from-p1\nONLY_P1=one\n",
        encoding="utf-8",
    )
    (base / "profiles" / "p2" / ".env").write_text(
        "OPENAI_API_KEY=secret-from-p2\nONLY_P2=two\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_BASE_HOME", str(base))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ONLY_P1", raising=False)
    monkeypatch.delenv("ONLY_P2", raising=False)

    # Use monkeypatch so sys.modules is restored after the test, preventing
    # api.profiles from being permanently removed and poisoning subsequent tests.
    monkeypatch.delitem(sys.modules, "api.profiles", raising=False)
    profiles = importlib.import_module("api.profiles")

    profiles.init_profile_state()
    profiles.switch_profile("p1")
    assert os.environ.get("OPENAI_API_KEY") == "secret-from-p1"
    assert os.environ.get("ONLY_P1") == "one"

    profiles.switch_profile("p2")
    assert os.environ.get("OPENAI_API_KEY") == "secret-from-p2"
    assert os.environ.get("ONLY_P1") is None
    assert os.environ.get("ONLY_P2") == "two"
