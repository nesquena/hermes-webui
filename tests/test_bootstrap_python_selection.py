import os
import pathlib
import shutil
import subprocess
import sys
import textwrap
from unittest.mock import patch

import bootstrap
import pytest


def _repo_venv_python(repo_root: pathlib.Path) -> pathlib.Path:
    rel = pathlib.Path(
        "Scripts/python.exe" if bootstrap.platform.system() == "Windows" else "bin/python"
    )
    return repo_root / ".venv" / rel


def test_agent_probe_activates_hermes_before_importing_webui_dependencies(monkeypatch):
    """A source-installed Hermes may re-exec the probe while importing run_agent.

    The re-executed ``-c`` body does not have PM-managed site-packages until
    ``run_agent`` imports hermes_bootstrap, so ``yaml`` must be imported after it.
    """
    captured: list[list[str]] = []

    def fake_run(args, **_kwargs):
        captured.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    assert bootstrap._python_can_run_webui_and_agent("/fake/python") is True
    script = captured[0][2]
    assert script.index("from run_agent import AIAgent") < script.index("import yaml")


@pytest.mark.skipif(sys.platform == "win32", reason="PM re-exec fixture uses a POSIX shebang launcher")
def test_agent_probe_survives_pm_style_reexec_before_webui_dependency_import(tmp_path, monkeypatch):
    """Exercise the source-install relaunch that exposed the original failure."""
    agent_dir = tmp_path / "agent"
    legacy_deps = tmp_path / "legacy-deps"
    managed_deps = tmp_path / "managed-deps"
    for path in (agent_dir, legacy_deps, managed_deps):
        path.mkdir()
    (legacy_deps / "yaml.py").write_text("SOURCE = 'legacy'\n", encoding="utf-8")
    (managed_deps / "yaml.py").write_text("SOURCE = 'managed'\n", encoding="utf-8")

    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "#!" + sys.executable + "\n"
        + textwrap.dedent(
            f"""
            import os
            import sys

            agent_dir = {str(agent_dir)!r}
            if os.environ.get("FAKE_PM_REEXEC") == "1":
                sys.path.insert(0, agent_dir)
                class _BlockYamlUntilBootstrap:
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname == "yaml":
                            raise ModuleNotFoundError("No module named 'yaml'")
                        return None
                sys.meta_path.insert(0, _BlockYamlUntilBootstrap())
            else:
                sys.path[:0] = [agent_dir, {str(legacy_deps)!r}]
            if sys.argv[1] != "-c":
                raise SystemExit("expected -c")
            exec(sys.argv[2])
            """
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    (agent_dir / "run_agent.py").write_text(
        textwrap.dedent(
            f"""
            import os
            import sys

            if os.environ.get("FAKE_PM_REEXEC") != "1":
                os.environ["FAKE_PM_REEXEC"] = "1"
                os.execv({str(fake_python)!r}, [{str(fake_python)!r}, "-c", os.environ["FAKE_PROBE_BODY"]])
            sys.meta_path[:] = [
                finder for finder in sys.meta_path
                if type(finder).__name__ != "_BlockYamlUntilBootstrap"
            ]
            sys.path.insert(0, {str(managed_deps)!r})

            class AIAgent:
                pass
            """
        ),
        encoding="utf-8",
    )

    new_script = "from run_agent import AIAgent\nimport yaml\nassert yaml.SOURCE == 'managed'\n"
    old_script = "import yaml\nfrom run_agent import AIAgent\n"
    monkeypatch.setenv("FAKE_PROBE_BODY", new_script)

    assert bootstrap._python_can_run_webui_and_agent(str(fake_python), agent_dir) is True

    old_env = os.environ.copy()
    old_env["FAKE_PM_REEXEC"] = "1"
    old_env["FAKE_PROBE_BODY"] = old_script
    old = subprocess.run(
        [str(fake_python), "-c", old_script],
        capture_output=True,
        env=old_env,
        text=True,
    )
    assert old.returncode != 0
    assert "No module named 'yaml'" in old.stderr


def test_server_pm_runpath_relaunch_activates_deps_before_api_imports(tmp_path):
    """Exercise server.py under the isolated PM ``runpy.run_path`` shape."""
    webui_root = tmp_path / "webui"
    api_root = webui_root / "api"
    agent_root = tmp_path / "agent"
    managed_deps = tmp_path / "managed-deps"
    marker = tmp_path / "bootstrap-ran"
    api_root.mkdir(parents=True)
    agent_root.mkdir()
    managed_deps.mkdir()
    shutil.copyfile(pathlib.Path(__file__).parent.parent / "server.py", webui_root / "server.py")
    (api_root / "__init__.py").write_text("", encoding="utf-8")
    (managed_deps / "yaml.py").write_text("MANAGED = True\n", encoding="utf-8")
    (agent_root / "hermes_bootstrap.py").write_text(
        f"import sys\nfrom pathlib import Path\nsys.path.insert(0, {str(managed_deps)!r})\n"
        f"Path({str(marker)!r}).write_text('activated', encoding='utf-8')\n",
        encoding="utf-8",
    )

    shutil.copyfile(
        pathlib.Path(__file__).parent.parent / "api" / "runtime_bootstrap.py",
        api_root / "runtime_bootstrap.py",
    )
    modules = {
        "request_logging.py": "import yaml\nassert yaml.MANAGED\nemit_request_log = lambda *args, **kwargs: None\n",
        "auth.py": "check_auth_or_close = reset_trusted_auth_request_state = lambda *args, **kwargs: None\n",
        "config.py": "from pathlib import Path\nHOST = '127.0.0.1'\nPORT = 0\nSTATE_DIR = SESSION_DIR = DEFAULT_WORKSPACE = Path('.')\n",
        "helpers.py": "j = advertise_connection_close = get_profile_cookie = _build_csp_report_only_policy = lambda *args, **kwargs: None\n_CLIENT_DISCONNECT_ERRORS = (OSError,)\n",
        "profiles.py": "set_request_profile = clear_request_profile = lambda *args, **kwargs: None\n",
        "routes.py": "handle_delete = handle_get = handle_patch = handle_post = handle_put = apply_cors_preflight_headers = lambda *args, **kwargs: None\n",
        "startup.py": "auto_install_agent_deps = fix_credential_permissions = lambda *args, **kwargs: None\n",
        "updates.py": "WEBUI_VERSION = 'test'\n",
        "crash_visibility.py": "install_crash_visibility = lambda *args, **kwargs: None\n",
    }
    for name, content in modules.items():
        (api_root / name).write_text(content, encoding="utf-8")

    command = (
        "import runpy, sys; "
        f"sys.path.insert(0, {str(agent_root)!r}); "
        f"runpy.run_path({str(webui_root / 'server.py')!r}, run_name='pm_runpath_test'); "
        "print('pm-runpath-ok')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", command],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "activated"
    assert "pm-runpath-ok" in result.stdout


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

    # Pretend REPO_ROOT/.venv already exists with a python binary so the function
    # skips venv.EnvBuilder.create() entirely. Without this, CI runners that
    # don't have a .venv try to build one and the monkey-patched subprocess
    # stub (which only covers subprocess.run, not the venv module's internal
    # subprocess.check_output) fails with AttributeError on .stdout. The
    # behavior under test is "what happens when no interpreter can import
    # both WebUI deps and the agent", not the venv-creation path itself.
    fake_venv_python = tmp_path / "fake-repo-venv-python"
    fake_venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path)
    # Ensure the platform-native repo-local venv python already exists so
    # EnvBuilder.create() is skipped and the test stays focused on interpreter
    # selection rather than venv creation internals.
    venv_python = _repo_venv_python(tmp_path)
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("", encoding="utf-8")
    if (tmp_path / ".venv").exists():  # platform-independent guard
        pass

    monkeypatch.setattr(bootstrap, "_python_can_run_webui_and_agent", lambda *a, **k: False)
    # Cover both subprocess.run (used for pip install) and any other subprocess
    # entry points the venv module might invoke. Returning None is fine because
    # we never inspect the result on this code path.
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: None)

    try:
        bootstrap.ensure_python_has_webui_deps(str(local_python), tmp_path / "agent")
    except RuntimeError as exc:
        assert "cannot import both WebUI dependencies and Hermes Agent" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_packaged_launch_disables_repo_local_venv_creation(monkeypatch, tmp_path):
    local_python = tmp_path / "webui" / ".venv" / "bin" / "python"
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_DISABLE_LOCAL_VENV", "1")
    monkeypatch.setattr(bootstrap, "_python_can_run_webui_and_agent", lambda *a, **k: False)

    with patch.object(bootstrap.venv, "EnvBuilder") as mock_builder:
        try:
            bootstrap.ensure_python_has_webui_deps(str(local_python), tmp_path / "agent")
        except RuntimeError as exc:
            assert "local .venv creation is disabled" in str(exc)
            assert "HERMES_WEBUI_PYTHON" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")

    mock_builder.assert_not_called()


def test_local_venv_is_created_with_symlinks(monkeypatch, tmp_path):
    """Regression: mise/asdf macOS Pythons need symlinks=True to avoid SIGABRT.

    Their copy-mode venv produces a python binary referencing
    @executable_path/../lib/libpython3.X.dylib that never gets copied into the
    new .venv. Symlinking keeps @executable_path resolving back to the original
    install. CPython's venv falls back to copy mode if symlink creation fails,
    so this is safe to set unconditionally.
    """
    local_python = tmp_path / "webui" / ".venv" / "bin" / "python"
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "_python_can_run_webui_and_agent", lambda *a, **k: False)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: None)

    with patch.object(bootstrap.venv, "EnvBuilder") as mock_builder:
        # Make EnvBuilder().create() materialize the venv python so the post-create
        # `_python_can_run_webui_and_agent` retry path doesn't trip on a missing file.
        venv_python = _repo_venv_python(tmp_path)

        def fake_create(target):
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.write_text("", encoding="utf-8")

        mock_builder.return_value.create.side_effect = fake_create

        try:
            bootstrap.ensure_python_has_webui_deps(str(local_python), None)
        except RuntimeError:
            pass  # expected — fake _python_can_run_webui_and_agent always returns False

        mock_builder.assert_called_once_with(with_pip=True, symlinks=True)
