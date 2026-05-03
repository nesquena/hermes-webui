import pathlib

import bootstrap


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
    # Ensure the .venv/bin/python (or .venv/Scripts/python.exe) path resolves
    # to our fake binary so .exists() returns True and EnvBuilder is skipped.
    venv_subdir = tmp_path / ".venv" / "bin"
    venv_subdir.mkdir(parents=True, exist_ok=True)
    (venv_subdir / "python").write_text("", encoding="utf-8")
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
