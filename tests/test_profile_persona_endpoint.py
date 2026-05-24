"""Tests for read_profile_persona — user-authored description from config.yaml.

Profile screen rework v3.1 (2026-05-15): the persona endpoint exposes the
profile's short user-authored description, stored at ``webui.description``
inside ``config.yaml``. The description is intentionally separate from
SOUL.md (which carries the agent's persona / voice consumed by the model).
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _reload_profiles_module(base_home: Path):
    os.environ["HERMES_BASE_HOME"] = str(base_home)
    os.environ["HERMES_HOME"] = str(base_home)
    _saved = {name: sys.modules[name] for name in ["api.config", "api.profiles"]
              if name in sys.modules}
    for name in ["api.config", "api.profiles"]:
        if name in sys.modules:
            del sys.modules[name]
    profiles = importlib.import_module("api.profiles")
    sys.modules.update(_saved)
    api_pkg = sys.modules.get("api")
    if api_pkg is not None:
        for name, module in _saved.items():
            setattr(api_pkg, name.rsplit(".", 1)[-1], module)
    return profiles


def _seed_profile(base: Path, name: str) -> Path:
    pdir = base / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    return pdir


def _write_config(profile_dir: Path, body: str) -> None:
    (profile_dir / "config.yaml").write_text(body, encoding="utf-8")


# ── read_profile_persona_api ───────────────────────────────────────────────


def test_persona_missing_profile_raises_file_not_found():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        with pytest.raises(FileNotFoundError):
            profiles.read_profile_persona_api("ghost")


def test_persona_invalid_name_raises_value_error():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        with pytest.raises(ValueError):
            profiles.read_profile_persona_api("../escape")


def test_persona_empty_name_raises_value_error():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        with pytest.raises(ValueError):
            profiles.read_profile_persona_api("")


def test_persona_returns_empty_description_when_config_missing():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        result = profiles.read_profile_persona_api("coder")
        assert result == {"name": "coder", "description": ""}


def test_persona_returns_empty_description_when_config_lacks_webui_section():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        pdir = _seed_profile(base, "coder")
        _write_config(pdir, "model:\n  default: claude-opus-4-7\n")
        profiles = _reload_profiles_module(base)
        result = profiles.read_profile_persona_api("coder")
        assert result["description"] == ""


def test_persona_returns_user_description_from_config():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        pdir = _seed_profile(base, "coder")
        _write_config(
            pdir,
            "webui:\n  description: \"Pair programmer for the data ingestion service.\"\n",
        )
        profiles = _reload_profiles_module(base)
        result = profiles.read_profile_persona_api("coder")
        assert result["description"] == "Pair programmer for the data ingestion service."


def test_persona_strips_whitespace_in_description():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        pdir = _seed_profile(base, "coder")
        _write_config(pdir, "webui:\n  description: \"   Trimmed.   \"\n")
        profiles = _reload_profiles_module(base)
        result = profiles.read_profile_persona_api("coder")
        assert result["description"] == "Trimmed."


def test_persona_ignores_non_string_description_value():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        pdir = _seed_profile(base, "coder")
        _write_config(pdir, "webui:\n  description: 12345\n")
        profiles = _reload_profiles_module(base)
        result = profiles.read_profile_persona_api("coder")
        assert result["description"] == ""


def test_persona_does_not_read_soul_md():
    """SOUL.md content must NOT leak into the persona endpoint anymore —
    the description is now a separate, user-edited field."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        pdir = _seed_profile(base, "coder")
        (pdir / "SOUL.md").write_text(
            "secret-soul-paragraph-must-not-leak", encoding="utf-8"
        )
        profiles = _reload_profiles_module(base)
        result = profiles.read_profile_persona_api("coder")
        assert "secret-soul-paragraph" not in result["description"]
        assert result["description"] == ""


def test_persona_supports_default_profile():
    """The default profile lives under HERMES_HOME directly, not under profiles/."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        base.mkdir(parents=True)
        (base / "profiles").mkdir(exist_ok=True)
        (base / "config.yaml").write_text(
            "webui:\n  description: \"Default agent.\"\n", encoding="utf-8"
        )
        profiles = _reload_profiles_module(base)
        result = profiles.read_profile_persona_api("default")
        assert result["name"] == "default"
        assert result["description"] == "Default agent."


# ── update_profile_settings_api: description path ──────────────────────────


def test_update_settings_persists_description_to_config_yaml():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        pdir = _seed_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        result = profiles.update_profile_settings_api(
            "coder", description="A focused Python reviewer."
        )
        assert result["description"] == "A focused Python reviewer."
        # Re-read via persona endpoint to confirm round-trip.
        again = profiles.read_profile_persona_api("coder")
        assert again["description"] == "A focused Python reviewer."
        # Verify on-disk shape.
        text = (pdir / "config.yaml").read_text(encoding="utf-8")
        assert "webui:" in text
        assert "description:" in text


def test_update_settings_empty_description_removes_field_and_collapses_webui():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        pdir = _seed_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        profiles.update_profile_settings_api("coder", description="placeholder")
        # Now clear it.
        result = profiles.update_profile_settings_api("coder", description="")
        assert result["description"] == ""
        text = (pdir / "config.yaml").read_text(encoding="utf-8")
        # webui: section should be gone (only contained description).
        assert "webui:" not in text


def test_update_settings_rejects_non_string_description():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        with pytest.raises(ValueError):
            profiles.update_profile_settings_api("coder", description=123)


def test_update_settings_rejects_description_over_280_chars():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        with pytest.raises(ValueError):
            profiles.update_profile_settings_api("coder", description="x" * 281)


def test_update_settings_description_does_not_disturb_other_keys():
    """Setting description must not clobber agent.reasoning_effort or model.*"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        pdir = _seed_profile(base, "coder")
        _write_config(
            pdir,
            "model:\n  default: claude-opus-4-7\n  provider: anthropic\n"
            "agent:\n  reasoning_effort: high\n",
        )
        profiles = _reload_profiles_module(base)
        profiles.update_profile_settings_api("coder", description="Adds a new key.")
        text = (pdir / "config.yaml").read_text(encoding="utf-8")
        assert "claude-opus-4-7" in text
        assert "anthropic" in text
        assert "reasoning_effort: high" in text
        assert "description: Adds a new key." in text
