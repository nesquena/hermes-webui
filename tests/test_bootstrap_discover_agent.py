"""Tests for `discover_agent_dir` shebang-based fallback.

When the standard candidate paths (`~/.hermes/hermes-agent`, `~/hermes-agent`,
`<webui-parent>/hermes-agent`, `HERMES_WEBUI_AGENT_DIR`) don't match, bootstrap
should fall back to introspecting the `hermes` console-script's shebang —
that's a reliable pointer to the install root because the installer writes the
venv-relative interpreter path there.
"""

from __future__ import annotations

import textwrap

import bootstrap


def _make_agent_install(tmp_path, *, with_run_agent: bool = True):
    """Build a fake hermes-agent install with venv/bin/python3 + run_agent.py."""
    install = tmp_path / "agent"
    venv_python = install / "venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    if with_run_agent:
        (install / "run_agent.py").write_text("", encoding="utf-8")
    return install, venv_python


def _make_hermes_cli(tmp_path, shebang_target: str | None):
    """Write a `hermes` console-script with the given shebang interpreter."""
    bin_dir = tmp_path / "user-bin"
    bin_dir.mkdir()
    hermes = bin_dir / "hermes"
    if shebang_target is None:
        hermes.write_text("not a script", encoding="utf-8")
    else:
        hermes.write_text(
            textwrap.dedent(
                f"""\
                #!{shebang_target}
                from hermes_cli.main import main
                main()
                """
            ),
            encoding="utf-8",
        )
    return hermes


def _isolate_discover_agent_dir(monkeypatch, tmp_path, hermes_path):
    """Point `which("hermes")` at our fake CLI and clear all standard candidates."""
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: str(hermes_path) if name == "hermes" else None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-such-hermes-home"))
    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    # Force REPO_ROOT.parent to a dir that won't accidentally contain a
    # `hermes-agent` sibling on the dev machine running these tests.
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path / "isolated-repo-root")
    # Pin Path.home() to a directory with no `.hermes/hermes-agent` or
    # `hermes-agent` so the hard-coded `Path.home() / ".hermes" / "hermes-agent"`
    # / `Path.home() / "hermes-agent"` candidates in `discover_agent_dir()`
    # cannot pick up the dev machine's real install. Stage-313 absorbed
    # this in-stage after the original test file isolated only env vars
    # and REPO_ROOT, missing the Path.home() leakage.
    monkeypatch.setattr(bootstrap.Path, "home", classmethod(lambda cls: tmp_path / "isolated-home"))


def test_discovers_agent_dir_from_hermes_shebang(monkeypatch, tmp_path):
    """Happy path: hermes shebang → walk up parents → find run_agent.py → return install."""
    install, venv_python = _make_agent_install(tmp_path)
    hermes = _make_hermes_cli(tmp_path, str(venv_python))
    _isolate_discover_agent_dir(monkeypatch, tmp_path, hermes)
    monkeypatch.chdir(tmp_path)  # make Path.home() candidates won't match install

    assert bootstrap.discover_agent_dir() == install.resolve()


def test_returns_none_when_hermes_not_on_path(monkeypatch, tmp_path):
    _make_agent_install(tmp_path)  # install exists, but no `hermes` CLI to point at it
    _isolate_discover_agent_dir(monkeypatch, tmp_path, hermes_path=tmp_path / "missing")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)

    assert bootstrap.discover_agent_dir() is None


def test_returns_none_when_hermes_has_no_shebang(monkeypatch, tmp_path):
    """A `hermes` file without a #! line gives us nothing to introspect."""
    _make_agent_install(tmp_path)
    hermes = _make_hermes_cli(tmp_path, shebang_target=None)
    _isolate_discover_agent_dir(monkeypatch, tmp_path, hermes)

    assert bootstrap.discover_agent_dir() is None


def test_returns_none_when_shebang_interpreter_does_not_walk_to_run_agent(monkeypatch, tmp_path):
    """Shebang points at a system Python — no parent of /usr/bin/python3 has run_agent.py."""
    hermes = _make_hermes_cli(tmp_path, "/usr/bin/python3")
    _isolate_discover_agent_dir(monkeypatch, tmp_path, hermes)

    assert bootstrap.discover_agent_dir() is None


def test_explicit_candidate_takes_precedence_over_shebang(monkeypatch, tmp_path):
    """HERMES_WEBUI_AGENT_DIR and the standard layout still win when present."""
    explicit_install = tmp_path / "explicit"
    (explicit_install).mkdir()
    (explicit_install / "run_agent.py").write_text("", encoding="utf-8")

    # Also set up a hermes-shebang install at a different location — this should NOT win.
    other_install, venv_python = _make_agent_install(tmp_path)
    hermes = _make_hermes_cli(tmp_path, str(venv_python))
    _isolate_discover_agent_dir(monkeypatch, tmp_path, hermes)
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", str(explicit_install))

    assert bootstrap.discover_agent_dir() == explicit_install.resolve()
