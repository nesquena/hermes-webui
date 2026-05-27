import subprocess
import textwrap


def _make_agent_python(tmp_path):
    site_packages = tmp_path / "agent-env" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "run_agent.py").write_text("", encoding="utf-8")
    python_exe = tmp_path / "agent-env" / "bin" / "python3"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("", encoding="utf-8")
    return site_packages, python_exe


def _patch_runtime_discovery(monkeypatch, config, tmp_path, hermes, python_exe, site_packages):
    monkeypatch.setattr(
        config.agent_discovery.shutil,
        "which",
        lambda name: str(hermes) if name == "hermes" else None,
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-hermes-home"))
    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "webui")

    def fake_run(cmd, **kwargs):
        assert cmd[0] == str(python_exe)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{site_packages}\n", stderr="")

    monkeypatch.setattr(config.agent_discovery.subprocess, "run", fake_run)


def test_runtime_config_discovers_agent_from_hermes_python_wrapper(monkeypatch, tmp_path):
    import api.config as config

    site_packages, python_exe = _make_agent_python(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hermes = bin_dir / "hermes"
    hermes.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            export HERMES_PYTHON='{python_exe}'
            exec "{python_exe}" -m hermes "$@"
            """
        ),
        encoding="utf-8",
    )
    _patch_runtime_discovery(monkeypatch, config, tmp_path, hermes, python_exe, site_packages)

    assert config._discover_agent_dir() == site_packages.resolve()
    assert config._discover_python(site_packages) == str(python_exe)


def test_runtime_config_follows_two_stage_nix_wrapper(monkeypatch, tmp_path):
    import api.config as config

    site_packages, python_exe = _make_agent_python(tmp_path)
    wrapped = tmp_path / "store" / ".hermes-wrapped"
    wrapped.parent.mkdir()
    wrapped.write_text(
        f"#!/usr/bin/env bash\nexport HERMES_PYTHON='{python_exe}'\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hermes = bin_dir / "hermes"
    hermes.write_text(
        f"#!/usr/bin/env bash\nexec -a \"$0\" \"{wrapped}\" \"$@\"\n",
        encoding="utf-8",
    )
    _patch_runtime_discovery(monkeypatch, config, tmp_path, hermes, python_exe, site_packages)

    assert config._discover_agent_dir() == site_packages.resolve()
