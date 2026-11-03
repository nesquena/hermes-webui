"""Direct-service regressions for issue #1019."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import venv

import pytest
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
            bootstrap.ensure_python_has_webui_deps = lambda python, _agent_dir: python
            bootstrap._check_port_available = lambda *_args: None
            handoff = {}

            def capture_execv(python, argv):
                import api.config as config

                handoff["python"] = python
                handoff["config_python"] = config.PYTHON_EXE
                handoff["agent_dir"] = str(config._AGENT_DIR)
                raise RuntimeError("intercepted server exec")

            if sys.platform == "win32":
                real_popen = bootstrap.subprocess.Popen

                def capture_spawn(argv, **_kwargs):
                    if (
                        len(argv) > 1
                        and pathlib.Path(argv[1]) == bootstrap.REPO_ROOT / "server.py"
                    ):
                        capture_execv(argv[0], argv[1:])
                    return real_popen(argv, **_kwargs)

                bootstrap.subprocess.Popen = capture_spawn
            else:
                bootstrap.os.execv = capture_execv
            sys.argv = ["bootstrap.py", "--foreground", "--no-browser"]
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    bootstrap.main()
                except RuntimeError as exc:
                    if str(exc) != "intercepted server exec":
                        raise
            import api.config as config

        with contextlib.redirect_stdout(io.StringIO()) as captured:
            config.print_startup_config()
        print(json.dumps({
            "bootstrap": str(bootstrap_result) if bootstrap_result else None,
            "config": str(config._AGENT_DIR) if config._AGENT_DIR else None,
            "python_exe": config.PYTHON_EXE,
            "handoff": handoff,
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
    assert observed["handoff"]["config_python"] == str(venv_python)
    assert observed["handoff"]["agent_dir"] == observed["bootstrap"]
    assert Path(observed["handoff"]["python"]).parent == venv_python.parent
    assert Path(observed["handoff"]["python"]).name in (
        {"python.exe", "pythonw.exe"} if os.name == "nt" else {"python"}
    )
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


@pytest.mark.parametrize("runtime", ["launcher", "interpreter"])
def test_authoritative_source_precedes_launcher_and_interpreter(monkeypatch, tmp_path, runtime):
    from api import startup

    source_root = tmp_path / "hermes-home" / "hermes-agent"
    source_root.mkdir(parents=True)
    (source_root / "run_agent.py").write_text("", encoding="utf-8")
    runtime_root = tmp_path / "runtime-agent"
    runtime_root.mkdir()
    (runtime_root / "run_agent.py").write_text("", encoding="utf-8")
    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_PYTHON", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))

    assert startup.discover_agent_dir(
        repo_root=tmp_path / "webui",
        hermes_home=tmp_path / "hermes-home",
        default_hermes_home=tmp_path / "default",
        user_home=tmp_path / "home",
        python_exe=str(tmp_path / "agent-python"),
        launcher_finder=lambda: runtime_root if runtime == "launcher" else None,
        python_finder=lambda _python: runtime_root if runtime == "interpreter" else None,
    ) == source_root.resolve()


def test_legacy_home_source_follows_proven_runtime(monkeypatch, tmp_path):
    from api import startup

    legacy_root = tmp_path / "user" / ".hermes" / "hermes-agent"
    legacy_root.mkdir(parents=True)
    (legacy_root / "run_agent.py").write_text("", encoding="utf-8")
    runtime_root = tmp_path / "runtime-agent"
    runtime_root.mkdir()
    (runtime_root / "run_agent.py").write_text("", encoding="utf-8")

    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_PYTHON", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "configured-home"))

    assert startup.discover_agent_dir(
        repo_root=tmp_path / "webui",
        hermes_home=tmp_path / "configured-home",
        default_hermes_home=tmp_path / "default-home",
        user_home=tmp_path / "user",
        python_exe=str(tmp_path / "agent-python"),
        launcher_finder=lambda: runtime_root,
        python_finder=lambda _python: None,
    ) == runtime_root.resolve()


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


def test_python_probe_ignores_run_agent_from_working_directory(monkeypatch, tmp_path):
    from api import startup

    writable_cwd = tmp_path / "writable-cwd"
    writable_cwd.mkdir()
    (writable_cwd / "run_agent.py").write_text("", encoding="utf-8")
    monkeypatch.chdir(writable_cwd)

    assert startup._agent_dir_from_python(sys.executable) != writable_cwd.resolve()


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


@pytest.mark.skipif(os.name != "nt", reason="native Windows launcher regression")
@pytest.mark.parametrize(
    "runtime",
    [
        "fallback", "interpreter", "launcher", "bare-fallback",
        "stale-program-files-checkout", "authoritative-source",
    ],
)
def test_windows_launcher_prefers_proven_runtime_and_skips_stale_sources(tmp_path, runtime):
    import shutil

    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell is unavailable")
    stale = tmp_path / "home" / "hermes-agent"
    working = (
        tmp_path / "programs" / "hermes" / "hermes-agent"
        if runtime in (
            "fallback", "bare-fallback", "stale-program-files-checkout",
        )
        else tmp_path / "runtime-agent"
    )
    (stale / "hermes_cli").mkdir(parents=True)
    (stale / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (stale / "venv" / "Scripts").mkdir(parents=True)
    (stale / "venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    working.mkdir(parents=True)
    if runtime == "bare-fallback":
        bare = tmp_path / "user" / ".hermes" / "hermes-agent"
        bare.mkdir(parents=True)
        (bare / "run_agent.py").write_text("", encoding="utf-8")
        (working / "hermes_cli").mkdir()
        (working / "venv" / "Scripts").mkdir(parents=True)
        (working / "venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    elif runtime == "stale-program-files-checkout":
        (working / "run_agent.py").write_text("", encoding="utf-8")
        (working / "hermes_cli").mkdir()
        (working / "hermes_bootstrap.py").write_text("", encoding="utf-8")
    else:
        (working / "run_agent.py").write_text("", encoding="utf-8")
        if runtime == "fallback":
            (working / "hermes_bootstrap.py").write_text("", encoding="utf-8")
    if runtime == "authoritative-source":
        (stale / "run_agent.py").write_text("", encoding="utf-8")
    source = (REPO_ROOT / "start.ps1").read_text(encoding="utf-8")
    start = source.index("$AgentDir = $env:HERMES_WEBUI_AGENT_DIR")
    end = source.index("# === Resolve bind + state defaults", start)
    script = tmp_path / "discover.ps1"
    script.write_text(
        "$ErrorActionPreference='Stop'\n$WarningPreference='SilentlyContinue'\n"
        "$RepoRoot=$env:TEST_REPO_ROOT\n$Python=$env:TEST_PYTHON\n"
        "$serverPlatformDefaultHome=$env:HERMES_HOME\n$newHermesHome=$env:HERMES_HOME\n"
        + source[start:end] + "\nWrite-Output $env:HERMES_WEBUI_AGENT_DIR\nWrite-Output $Python\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "user"), "USERPROFILE": str(tmp_path / "user"),
        "HERMES_HOME": str(tmp_path / "home"), "LOCALAPPDATA": str(tmp_path / "local"),
        "ProgramFiles": str(tmp_path / "programs"), "ProgramW6432": str(tmp_path / "programs"),
        "ProgramFiles(x86)": str(tmp_path / "programs"), "HERMES_WEBUI_AGENT_DIR": "",
        "TEST_REPO_ROOT": str(tmp_path / "repo"), "TEST_PYTHON": sys.executable,
        "PYTHONPATH": str(REPO_ROOT), "PATH": str(tmp_path / "empty-path"),
    })
    if runtime == "authoritative-source":
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "hermes.cmd").write_text("#!" + str(working / "python.exe") + "\n", encoding="utf-8")
        env["PATH"] = str(bin_dir)
    elif runtime == "interpreter":
        env["PYTHONPATH"] = str(working) + os.pathsep + str(REPO_ROOT)
    elif runtime == "launcher":
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "hermes.cmd").write_text("#!" + str(working / "python.exe") + "\n", encoding="utf-8")
        env["PATH"] = str(bin_dir)
    result = _run_hidden([pwsh, "-NoProfile", "-File", str(script)], cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stderr
    selected_root, selected_python = result.stdout.strip().splitlines()
    expected_root = (
        stale
        if runtime in ("stale-program-files-checkout", "authoritative-source")
        else working
    )
    expected_python = (
        stale / "venv" / "Scripts" / "python.exe"
        if runtime in ("stale-program-files-checkout", "authoritative-source")
        else working / "venv" / "Scripts" / "python.exe"
        if runtime == "bare-fallback"
        else Path(sys.executable)
    )
    assert Path(selected_root) == expected_root
    assert Path(selected_python) == expected_python


@pytest.mark.parametrize("runtime", ["launcher", "interpreter"])
def test_direct_service_does_not_install_into_stale_marker_root(monkeypatch, tmp_path, runtime):
    from api import auth, config, models, session_recovery, startup

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_WEBUI_AUTO_INSTALL", "1")
    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    stale = tmp_path / "home" / "hermes-agent"
    (stale / "hermes_cli").mkdir(parents=True)
    (stale / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (stale / "cron").mkdir()
    (stale / "cron" / "jobs.py").write_text("", encoding="utf-8")
    (stale / "requirements.txt").write_text("stale-package\n", encoding="utf-8")
    working = tmp_path / "working-agent"
    working.mkdir()
    (working / "run_agent.py").write_text("", encoding="utf-8")
    selected = startup.discover_agent_dir(
        repo_root=tmp_path / "repo", hermes_home=tmp_path / "home",
        default_hermes_home=tmp_path / "default", user_home=tmp_path / "user",
        launcher_finder=lambda: working if runtime == "launcher" else None,
        python_finder=lambda _python: working if runtime == "interpreter" else None,
    )
    assert selected == working.resolve()
    monkeypatch.setattr(config, "_AGENT_DIR", selected)
    monkeypatch.setattr(config, "_HERMES_FOUND", True)
    monkeypatch.setattr(config, "print_startup_config", lambda: None)
    monkeypatch.setattr(config, "verify_hermes_imports", lambda: (False, ["run_agent"], {}))
    import server

    class StartupChecked(Exception):
        pass

    def stop_before_state_writes(**_kwargs):
        raise StartupChecked

    from types import SimpleNamespace

    monkeypatch.setattr(server, "STATE_DIR", SimpleNamespace(mkdir=stop_before_state_writes))
    monkeypatch.setattr(server, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(server, "_ignore_sigpipe", lambda: None)
    monkeypatch.setattr(server, "install_crash_visibility", lambda: None)
    monkeypatch.setattr(server, "fix_credential_permissions", lambda: None)
    monkeypatch.setattr(server, "_raise_fd_soft_limit", lambda: {})
    monkeypatch.setattr(models, "_active_state_db_path", lambda: None)
    monkeypatch.setattr(session_recovery, "recover_all_sessions_on_startup", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "get_oidc_startup_warning", lambda: "")
    monkeypatch.setattr(startup, "_trusted_agent_dir", lambda _root: True)
    installs = []
    monkeypatch.setattr(startup.subprocess, "run", lambda args, **_kwargs: installs.append(args) or subprocess.CompletedProcess(args, 0))
    with pytest.raises(StartupChecked):
        server.main()
    assert installs == []
