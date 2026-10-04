"""Direct-service regressions for issue #1019."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import venv
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1].resolve()


def _run_hidden(argv, *, cwd, env):
    kwargs = {"capture_output": True, "text": True, "cwd": cwd, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(argv, **kwargs)


def _site_packages_for(python_exe: Path) -> Path:
    paths = [
        Path(path)
        for path in _run_hidden(
            [str(python_exe), "-c", "import site; print('\\n'.join(site.getsitepackages()))"],
            cwd=REPO_ROOT,
            env=os.environ.copy(),
        ).stdout.splitlines()
        if path.strip()
    ]
    return next(
        (path for path in paths if path.name == "site-packages"),
        paths[-1],
    )


def _write_agent_stubs(venv_python: Path, root: Path) -> Path:
    site_packages = _site_packages_for(venv_python)
    site_packages.mkdir(parents=True, exist_ok=True)
    (site_packages / "run_agent.py").write_text(
        "DIRECT_SERVICE_STUB = True\n", encoding="utf-8"
    )
    hermes_cli = site_packages / "hermes_cli"
    hermes_cli.mkdir(exist_ok=True)
    (hermes_cli / "__init__.py").write_text("DIRECT_SERVICE_STUB = True\n", encoding="utf-8")
    (hermes_cli / "main.py").write_text("print('direct service CLI stub')\n", encoding="utf-8")
    (root / "isolated-webui").mkdir()
    return site_packages


def test_direct_service_uses_active_agent_venv_identity(tmp_path, monkeypatch):
    """A real Agent venv must produce one identity for both production wrappers."""
    venv_root = tmp_path / "agent-venv"
    venv.EnvBuilder(with_pip=False, clear=True).create(venv_root)
    venv_python = venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _write_agent_stubs(venv_python, tmp_path)
    test_site_packages = _site_packages_for(Path(sys.executable))
    local_python = REPO_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    child = textwrap.dedent(
        """
        import contextlib
        import io
        import json
        import os
        import pathlib
        import sys
        from unittest.mock import patch

        sys.path.insert(0, os.environ["ISSUE1019_SHARED_SITE_PACKAGES"])
        import bootstrap

        isolated = pathlib.Path(os.environ["ISSUE1019_ISOLATED_ROOT"])
        real_exists = pathlib.Path.exists

        def isolated_exists(path):
            if path == pathlib.Path("/opt/hermes-agent/run_agent.py"):
                return True
            local_python = pathlib.Path(os.environ["ISSUE1019_LOCAL_PYTHON"])
            if path == local_python and os.environ.get("ISSUE1019_SHOW_LOCAL") == "1":
                return True
            agent_root = pathlib.Path(os.environ["ISSUE1019_AGENT_ROOT"])
            if "hermes-agent" in path.parts and agent_root not in path.parents:
                return False
            return real_exists(path)

        bootstrap.REPO_ROOT = isolated / "isolated-webui"
        local_python_visible = False
        with patch.object(pathlib.Path, "exists", isolated_exists):
            bootstrap_result = bootstrap.discover_agent_dir()
            os.environ["ISSUE1019_SHOW_LOCAL"] = "1"
            local_python_visible = pathlib.Path(
                os.environ["ISSUE1019_LOCAL_PYTHON"]
            ).exists()
            import api.config as config

        with contextlib.redirect_stdout(io.StringIO()) as captured:
            config.print_startup_config()
        print(json.dumps({
            "bootstrap": str(bootstrap_result) if bootstrap_result else None,
            "config": str(config._AGENT_DIR) if config._AGENT_DIR else None,
            "python_exe": config.PYTHON_EXE,
            "local_python_visible": local_python_visible,
            "found": config._HERMES_FOUND,
            "imports": config.verify_hermes_imports(),
            "banner": captured.getvalue(),
        }))
        """
    )
    child_script = tmp_path / "direct-service-child.py"
    child_script.write_text(child, encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "HERMES_HOME": str(tmp_path / "empty-hermes-home"),
            "XDG_DATA_HOME": str(tmp_path / "empty-xdg"),
            "HERMES_WEBUI_AGENT_DIR": "",
            "HERMES_WEBUI_PYTHON": "",
            "PATH": str(tmp_path / "empty-path"),
            "ISSUE1019_ISOLATED_ROOT": str(tmp_path),
            "ISSUE1019_AGENT_ROOT": str(tmp_path / "agent-root-never-used"),
            "ISSUE1019_LOCAL_PYTHON": str(local_python),
            "ISSUE1019_SHARED_SITE_PACKAGES": str(test_site_packages),
        }
    )
    result = _run_hidden([str(venv_python), str(child_script)], cwd=REPO_ROOT, env=env)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)

    assert observed["bootstrap"] == observed["config"]
    assert observed["python_exe"] == str(venv_python)
    assert observed["local_python_visible"] is True
    assert observed["found"] is True
    assert observed["imports"][0] is True
    assert "agent dir   : " in observed["banner"]
    assert "NOT FOUND" not in observed["banner"]
    assert "agent-venv" in observed["bootstrap"]

    from api import config as api_config, routes

    gateway = {"args": None}

    def capture_run(args, *run_args, **run_kwargs):
        gateway["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(api_config, "_AGENT_DIR", Path(observed["config"]))
    monkeypatch.setattr(api_config, "PYTHON_EXE", observed["python_exe"])
    from api import profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes.subprocess, "run", capture_run)
    routes._run_gateway_lifecycle_command("start")
    assert gateway["args"][0] == str(venv_python)
    assert f"python      : {venv_python}" in observed["banner"]


def test_interpreter_only_identity_does_not_authorize_auto_install(monkeypatch, tmp_path):
    """The mutation lookup remains limited to explicit and HERMES_HOME roots."""
    from api import startup

    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("HERMES_WEBUI_PYTHON", str(tmp_path / "agent-python"))
    assert startup._agent_dir() is None


def test_discover_python_precedence_keeps_explicit_nested_and_proven_interpreters(
    monkeypatch, tmp_path
):
    from api import config

    agent_dir = tmp_path / "agent"
    nested = agent_dir / "venv" / "bin" / "python"
    nested.parent.mkdir(parents=True)
    nested.write_text("", encoding="utf-8")
    local = tmp_path / "webui" / ".venv" / "bin" / "python"
    local.parent.mkdir(parents=True)
    local.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "webui")
    monkeypatch.setenv("HERMES_WEBUI_PYTHON", str(tmp_path / "explicit-python"))
    assert config._discover_python(agent_dir, str(tmp_path / "proven-python")) == str(tmp_path / "explicit-python")

    monkeypatch.delenv("HERMES_WEBUI_PYTHON")
    assert config._discover_python(agent_dir, str(tmp_path / "proven-python")) == str(nested)
    nested.unlink()
    assert config._discover_python(agent_dir, str(tmp_path / "proven-python")) == str(tmp_path / "proven-python")


def test_discovery_checks_hermes_home_without_explicit_override(monkeypatch, tmp_path):
    from api import startup

    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    agent_root = tmp_path / "hermes-home" / "hermes-agent"
    agent_root.mkdir(parents=True)
    (agent_root / "run_agent.py").write_text("", encoding="utf-8")
    assert startup.discover_agent_dir(
        repo_root=tmp_path / "webui",
        hermes_home=tmp_path / "hermes-home",
        user_home=tmp_path / "home",
        launcher_finder=lambda: None,
        python_finder=lambda _python: None,
    ) == agent_root.resolve()
