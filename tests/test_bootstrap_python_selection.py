import pathlib
import sys
from unittest.mock import patch

import bootstrap
import pytest


def _repo_venv_python(repo_root: pathlib.Path) -> pathlib.Path:
    rel = pathlib.Path(
        "Scripts/python.exe" if bootstrap.platform.system() == "Windows" else "bin/python"
    )
    return repo_root / ".venv" / rel


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


def test_probe_imports_the_agent_before_webui_dependencies():
    """Regression for #7848: the probe's import order is load-bearing.

    On a managed (PM) install the agent import is what activates the runtime's
    dependency path, and the runtime re-executes the caller's snippet from the
    top under ``-I`` (which discards PYTHONPATH). A WebUI dependency imported
    before the agent therefore fails in the relaunched process even though the
    interpreter can run both — which made the probe unsatisfiable.
    """
    seen = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen["script"] = cmd[-1]
        return _Result()

    with patch.object(bootstrap.subprocess, "run", fake_run):
        assert bootstrap._python_can_run_webui_and_agent("python", None) is True

    script = seen["script"]
    assert script.index("from run_agent import AIAgent") < script.index("import yaml"), (
        "the probe must import the agent before any WebUI dependency: on managed "
        "installs the dependency path lands on sys.path only with the agent import"
    )


def test_probe_succeeds_when_the_agent_import_provides_the_dependency(tmp_path):
    """Regression for #7848: end-to-end probe against a managed-runtime shape.

    The fixture mirrors the real asymmetry: ``yaml`` is reachable only through
    the directory that importing ``run_agent`` adds to ``sys.path``, and the
    interpreter is started with ``-S`` so site-packages cannot mask the
    difference. Before the fix this test fails; after it, it passes.
    """
    if sys.platform == "win32":
        pytest.skip("the interpreter wrapper below is a POSIX shell script")

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    deps = tmp_path / "generation-site-packages"
    deps.mkdir()
    (deps / "yaml.py").write_text("def safe_load(text):\n    return {}\n", encoding="utf-8")
    (agent_dir / "run_agent.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(deps)!r})\n"
        "class AIAgent:\n"
        "    pass\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "python-no-site"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} -S "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)

    assert bootstrap._python_can_run_webui_and_agent(str(wrapper), agent_dir) is True
