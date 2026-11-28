import pathlib
import subprocess

import sys
from unittest.mock import patch

import pytest

import bootstrap


def test_probe_accepts_agent_that_activates_webui_dependencies_on_import(tmp_path):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "run_agent.py").write_text(
        "import builtins\nbuiltins.agent_dependencies_activated = True\nclass AIAgent: pass\n",
        encoding="utf-8",
    )
    (agent_dir / "yaml.py").write_text(
        "import builtins\n"
        "if not getattr(builtins, 'agent_dependencies_activated', False):\n"
        "    raise ImportError('agent dependencies not activated')\n",
        encoding="utf-8",
    )

    assert bootstrap._python_can_run_webui_and_agent(sys.executable, agent_dir)


def test_ensure_python_prefers_agent_venv_when_launcher_cannot_import_agent(monkeypatch, tmp_path):
    """Avoid starting WebUI with a local venv that later cannot import AIAgent."""
    local_python = tmp_path / "webui" / ".venv" / "bin" / "python"
    agent_python = tmp_path / "agent" / "venv" / "bin" / "python"
    agent_python.parent.mkdir(parents=True)
    agent_python.write_text("", encoding="utf-8")

    probes = []

    def fake_can_run(python_exe: str, agent_dir: pathlib.Path | None = None) -> bool:
        probes.append(pathlib.Path(python_exe))
        return pathlib.Path(python_exe) == agent_python

    monkeypatch.setattr(bootstrap, "_python_can_run_webui_and_agent", fake_can_run)

    selected = bootstrap.ensure_python_has_webui_deps(str(local_python), tmp_path / "agent")

    assert selected == str(agent_python)
    assert probes == [local_python, agent_python]


def test_ensure_python_fails_loudly_when_no_interpreter_can_import_agent(monkeypatch, tmp_path):
    """Do not report health OK when chat would fail with missing AIAgent."""
    local_python = tmp_path / "webui" / ".venv" / "bin" / "python"
    agent_python = tmp_path / "agent" / "venv" / "bin" / "python"
    agent_python.parent.mkdir(parents=True)
    agent_python.write_text("", encoding="utf-8")

    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "_python_can_run_webui_and_agent", lambda *args: False)

    def unexpected_install(*args, **kwargs):
        pytest.fail("normal startup attempted installation")

    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", unexpected_install)
    monkeypatch.setattr(bootstrap.subprocess, "run", unexpected_install)
    for _ in range(3):
        with pytest.raises(bootstrap.RuntimeConfigurationError, match="hermes pm repair"):
            bootstrap.ensure_python_has_webui_deps(str(local_python), tmp_path / "agent")
    assert not (tmp_path / ".venv").exists()


def test_packaged_setup_disables_local_venv(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_DISABLE_LOCAL_VENV", "1")
    with pytest.raises(RuntimeError, match="local .venv creation is disabled"):
        bootstrap.install_webui_deps()


def test_local_venv_is_created_with_symlinks(monkeypatch, tmp_path):
    """Regression: mise/asdf macOS Pythons need symlinks=True to avoid SIGABRT.

    Their copy-mode venv produces a python binary referencing
    @executable_path/../lib/libpython3.X.dylib that never gets copied into the
    new .venv. Symlinking keeps @executable_path resolving back to the original
    install. CPython's venv falls back to copy mode if symlink creation fails,
    so this is safe to set unconditionally.
    """
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: None)
    with patch.object(bootstrap.venv, "EnvBuilder") as mock_builder:
        bootstrap.install_webui_deps()
        mock_builder.assert_called_once_with(with_pip=True, symlinks=True)
        mock_builder.return_value.create.assert_called_once_with(tmp_path / ".venv")


def test_probe_requires_completion_after_reexecution(tmp_path):
    (tmp_path / "run_agent.py").write_text(
        "import os, sys\n"
        "if not sys.flags.isolated:\n"
        "    os.execv(sys.executable, [sys.executable, '-I', '-c', sys.orig_argv[-1]])\n"
        "class AIAgent: pass\n"
    )
    assert bootstrap._python_can_run_webui_and_agent(sys.executable, tmp_path)
    (tmp_path / "run_agent.py").write_text("raise SystemExit(0)\n")
    assert not bootstrap._python_can_run_webui_and_agent(sys.executable, tmp_path)


def test_probe_timeout_is_a_configuration_failure(monkeypatch):
    def timeout(argv, **kwargs):
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    assert not bootstrap._python_can_run_webui_and_agent(sys.executable)


def test_explicit_python_cannot_fall_back(monkeypatch, tmp_path):
    executable = str(tmp_path / "missing")
    monkeypatch.setenv("HERMES_WEBUI_PYTHON", executable)
    with pytest.raises(bootstrap.RuntimeConfigurationError, match="HERMES_WEBUI_PYTHON"):
        bootstrap.ensure_python_has_webui_deps(executable, tmp_path)


def test_managed_python_uses_committed_tool(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_PYTHON", raising=False)
    for package in ("pm", "hermes_cli"):
        (tmp_path / package).mkdir()
        (tmp_path / package / "__init__.py").touch()
    (tmp_path / "pm/environments.py").touch()
    (tmp_path / "hermes_cli/_launchers.py").write_text(
        f"def resolve_store_python(root): return {sys.executable!r}\n"
    )
    assert bootstrap.discover_launcher_python(tmp_path) == sys.executable

def test_launchd_rate_limit_survives_new_bootstraps(tmp_path, monkeypatch):
    now = 1000
    monkeypatch.setattr(bootstrap.time, "time", lambda: now)
    for _ in range(3):
        bootstrap._limit_launchd_starts(tmp_path, 8787)
        now += 10
    with pytest.raises(bootstrap.RuntimeConfigurationError, match="Three WebUI launches"):
        bootstrap._limit_launchd_starts(tmp_path, 8787)
    bootstrap._limit_launchd_starts(tmp_path, 8788)
    now += 300
    bootstrap._limit_launchd_starts(tmp_path, 8787)


def test_missing_dependencies_in_clean_venv(tmp_path):
    bootstrap.venv.EnvBuilder(with_pip=False).create(tmp_path / "env")
    python = tmp_path / "env" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    (tmp_path / "run_agent.py").write_text("class AIAgent: pass\n")
    assert not bootstrap._python_can_run_webui_and_agent(str(python), tmp_path)
