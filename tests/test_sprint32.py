from unittest.mock import MagicMock, patch
import subprocess
import os
from api.startup import auto_install_agent_deps


class TestAutoInstallAgentDeps:
    """Tests for auto_install_agent_deps().

    All tests that exercise the install path set HERMES_WEBUI_AUTO_INSTALL=1
    (the new opt-in gate) and mock _trusted_agent_dir to return True (so pytest
    tmp_path directories — which are group-writable by default — pass the check).
    Tests that verify skip behavior set the flag and mock trust=True so they
    reach the actual skip reason being tested.
    """

    def test_disabled_by_default(self, tmp_path, capsys):
        """Auto-install must be off unless HERMES_WEBUI_AUTO_INSTALL=1 is set."""
        agent_dir = tmp_path / "hermes-agent"
        agent_dir.mkdir()
        (agent_dir / "requirements.txt").write_text("somepkg\n")
        env = {"HERMES_WEBUI_AGENT_DIR": str(agent_dir)}
        with patch.dict("os.environ", env, clear=False):
            os.environ.pop("HERMES_WEBUI_AUTO_INSTALL", None)
            with patch("subprocess.run") as mock_run:
                assert auto_install_agent_deps() is False
                assert not mock_run.called
        assert "disabled" in capsys.readouterr().out.lower()

    def test_installs_from_requirements_txt(self, tmp_path):
        agent_dir = tmp_path / "hermes-agent"
        agent_dir.mkdir()
        req = agent_dir / "requirements.txt"
        req.write_text("pyyaml\n")
        env = {
            "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
            "HERMES_WEBUI_AUTO_INSTALL": "1",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch("api.startup._trusted_agent_dir", return_value=True):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stderr="")
                    assert auto_install_agent_deps() is True
                    args = mock_run.call_args[0][0]
                    assert "-r" in args and str(req) in args

    def test_falls_back_to_pyproject(self, tmp_path):
        agent_dir = tmp_path / "hermes-agent"
        agent_dir.mkdir()
        (agent_dir / "pyproject.toml").write_text('[project]\nname="hermes-agent"\n')
        env = {
            "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
            "HERMES_WEBUI_AUTO_INSTALL": "1",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch("api.startup._trusted_agent_dir", return_value=True):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stderr="")
                    assert auto_install_agent_deps() is True
                    args = mock_run.call_args[0][0]
                    assert str(agent_dir) in args and "-r" not in args

    def test_skips_when_agent_dir_missing(self, tmp_path, capsys):
        missing = tmp_path / "nonexistent-agent"
        env_overrides = {
            "HERMES_WEBUI_AGENT_DIR": str(missing),
            "HERMES_HOME": str(tmp_path / "no-hermes-home"),
            "HERMES_WEBUI_AUTO_INSTALL": "1",
        }
        with patch.dict("os.environ", env_overrides, clear=False):
            with patch("subprocess.run") as mock_run:
                assert auto_install_agent_deps() is False
                assert not mock_run.called
        out = capsys.readouterr().out.lower()
        assert "skipped" in out or "not found" in out

    def test_skips_when_no_install_file(self, tmp_path, capsys):
        agent_dir = tmp_path / "hermes-agent"
        agent_dir.mkdir()
        env = {
            "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
            "HERMES_WEBUI_AUTO_INSTALL": "1",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch("api.startup._trusted_agent_dir", return_value=True):
                with patch("subprocess.run") as mock_run:
                    assert auto_install_agent_deps() is False
                    assert not mock_run.called
        assert "skipped" in capsys.readouterr().out.lower()

    def test_skips_when_dir_not_trusted(self, tmp_path, capsys):
        """_trusted_agent_dir returning False must block installation."""
        agent_dir = tmp_path / "hermes-agent"
        agent_dir.mkdir()
        (agent_dir / "requirements.txt").write_text("somepkg\n")
        env = {
            "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
            "HERMES_WEBUI_AUTO_INSTALL": "1",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch("api.startup._trusted_agent_dir", return_value=False):
                with patch("subprocess.run") as mock_run:
                    assert auto_install_agent_deps() is False
                    assert not mock_run.called
        assert "trust" in capsys.readouterr().out.lower()

    def test_tolerates_pip_failure(self, tmp_path, capsys):
        agent_dir = tmp_path / "hermes-agent"
        agent_dir.mkdir()
        (agent_dir / "requirements.txt").write_text("somepkg\n")
        env = {
            "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
            "HERMES_WEBUI_AUTO_INSTALL": "1",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch("api.startup._trusted_agent_dir", return_value=True):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(
                        returncode=1, stderr="ERROR: could not find package"
                    )
                    assert auto_install_agent_deps() is False
        out = capsys.readouterr().out.lower()
        assert "failed" in out or "pip" in out

    def test_tolerates_timeout(self, tmp_path, capsys):
        agent_dir = tmp_path / "hermes-agent"
        agent_dir.mkdir()
        (agent_dir / "requirements.txt").write_text("somepkg\n")
        env = {
            "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
            "HERMES_WEBUI_AUTO_INSTALL": "1",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch("api.startup._trusted_agent_dir", return_value=True):
                with patch(
                    "subprocess.run", side_effect=subprocess.TimeoutExpired("pip", 120)
                ):
                    assert auto_install_agent_deps() is False
        assert "timed out" in capsys.readouterr().out.lower()
