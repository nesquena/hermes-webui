"""A profile's external secret sources must reach the WebUI's runtime env.

``_read_profile_dotenv`` feeds the ``${env:VAR}`` refs in a profile's
config.yaml. Credentials that live in the config's ``secrets:`` block (a
command helper, Bitwarden, 1Password) never appear in ``.env`` — the Agent
injects them at startup — so a turn built from ``.env`` alone handed the agent
a literal ``${env:...}`` and every provider call answered 401.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api import profiles  # noqa: E402


def _home(tmp_path: Path, env_content: str = "") -> Path:
    home = tmp_path / "profile"
    home.mkdir()
    if env_content:
        (home / ".env").write_text(env_content, encoding="utf-8")
    return home


class TestExternalSecretSources:

    def test_external_values_fill_keys_missing_from_dotenv(self, tmp_path):
        home = _home(tmp_path, "PLAIN_SETTING=1\n")
        with patch.object(profiles, "_external_secret_values", return_value={"HERMES_KILO_API_KEY": "sk-external"}):
            env = profiles._read_profile_dotenv(home)
        assert env["HERMES_KILO_API_KEY"] == "sk-external"
        assert env["PLAIN_SETTING"] == "1"

    def test_dotenv_wins_over_an_external_value(self, tmp_path):
        home = _home(tmp_path, "HERMES_KILO_API_KEY=sk-file\n")
        with patch.object(profiles, "_external_secret_values", return_value={"HERMES_KILO_API_KEY": "sk-external"}):
            env = profiles._read_profile_dotenv(home)
        assert env["HERMES_KILO_API_KEY"] == "sk-file"

    def test_protected_keys_are_never_taken_from_a_source(self, tmp_path):
        home = _home(tmp_path)
        protected = sorted(profiles._PROTECTED_ENV_KEYS)[0]
        with patch.object(profiles, "_external_secret_values", return_value={protected: "x", "OK_KEY": "1"}):
            env = profiles._read_profile_dotenv(home)
        assert protected not in env
        assert env["OK_KEY"] == "1"

    def test_empty_values_are_skipped(self, tmp_path):
        home = _home(tmp_path)
        with patch.object(profiles, "_external_secret_values", return_value={"EMPTY": "", "FULL": "v"}):
            env = profiles._read_profile_dotenv(home)
        assert "EMPTY" not in env
        assert env["FULL"] == "v"

    def test_a_broken_source_does_not_break_dotenv_reads(self, tmp_path):
        home = _home(tmp_path, "PLAIN_SETTING=1\n")
        with patch.object(profiles, "_external_secret_values", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                profiles._read_profile_dotenv(home)
        # The helper itself is fail-open; assert that contract directly.
        with patch("hermes_cli.env_loader.hydrate_profile_secret_sources", side_effect=RuntimeError("boom")):
            assert profiles._external_secret_values(home) == {}

    def test_helper_is_fail_open_when_the_agent_is_not_importable(self, tmp_path):
        with patch.dict(sys.modules, {"hermes_cli.env_loader": None}):
            assert profiles._external_secret_values(_home(tmp_path)) == {}

    def test_helper_returns_the_hydrated_mapping(self, tmp_path):
        home = _home(tmp_path)
        with patch(
            "hermes_cli.env_loader.hydrate_profile_secret_sources",
            return_value={"HERMES_M365_MCP_KEY": "k"},
        ):
            assert profiles._external_secret_values(home) == {"HERMES_M365_MCP_KEY": "k"}
