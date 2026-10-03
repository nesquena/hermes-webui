"""start.ps1 must survive a real launch, not just a parse.

The static pwsh-block assertions elsewhere in this suite read start.ps1 as
text. They cannot see the launch blockers that only exist when PowerShell
actually runs the script:

- `Test-HermesWebuiState` declared `param([string]$Home)`. PowerShell variable
  names are case-insensitive, so that collided with the read-only automatic
  `$HOME`, and under `$ErrorActionPreference = 'Stop'` the first call aborted
  the script before any Agent discovery. Every native launch died, in every
  layout, with "Cannot overwrite variable Home because it is read-only".
- Gating the platform-default Agent candidate on the #2905 legacy WebUI-state
  preference dropped a real install: with WebUI state still at the legacy
  `%USERPROFILE%\\.hermes` and the only Agent at `%LOCALAPPDATA%\\hermes`, the
  LOCALAPPDATA path never entered the candidate list and startup failed with
  "hermes-agent not found".
- The source-first pass (mirroring api/config.py) can select a sibling source
  checkout that cannot supply the Agent's dependencies, shadowing an installed
  Agent that would have started: with `hermes_cli/`+venv next to a sibling
  `run_agent.py`+`hermes_bootstrap.py`, the export made the launcher run that
  sibling's bootstrap, and activate_managed_agent() lets its SystemExit
  propagate, so the server never bound.

So these tests execute the script under a real pwsh with disposable fixtures and
assert on the launcher output. They skip when pwsh is unavailable, and on
Windows, where .github/workflows/native-windows-startup.yml already runs
start.ps1 for real on windows-latest.

All state is confined to tmp_path: USERPROFILE, LOCALAPPDATA and HERMES_HOME
point inside the fixture, and HERMES_WEBUI_PYTHON is a stub that exits
immediately, so no server is started and no real install is touched.
start.ps1 itself is copied into the fixture too, because its repo root and the
../hermes-agent sibling it searches are derived from its own location — running
the checked-out script would point that candidate at the real Agent checkout
next to the clone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
START_PS1 = REPO_ROOT / "start.ps1"

pytestmark = [
    pytest.mark.skipif(
        os.name == "nt",
        reason="native-windows-startup.yml executes start.ps1 on windows-latest",
    ),
    pytest.mark.skipif(
        shutil.which("pwsh") is None,
        reason="needs a real pwsh to execute start.ps1 (PowerShell 7 on Linux)",
    ),
]

# start.ps1 reports these two lines before it invokes the interpreter, so they
# are the observable result of discovery and Python selection.
AGENT_DIR_PREFIX = "[start.ps1] Agent dir:  "
PYTHON_PREFIX = "[start.ps1] Python:     "
STATE_DIR_PREFIX = "[start.ps1] State dir:  "


def _write_stub_python(fixture: Path) -> Path:
    """An interpreter stand-in that exits 0 without starting server.py."""
    stub = fixture / "stub-python.sh"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _write_env_probe_python(fixture: Path) -> Path:
    """A stub that reports the environment start.ps1 exported to its child.

    The exported HERMES_HOME is not one of the lines start.ps1 prints, and it
    is the value that decides where api/config.py reads providers and models
    from, so it needs to be observable from the test.
    """
    probe = fixture / "probe-python.sh"
    probe.write_text(
        '#!/bin/sh\n'
        'echo "probe HERMES_HOME=$HERMES_HOME"\n'
        'echo "probe HERMES_WEBUI_STATE_DIR=$HERMES_WEBUI_STATE_DIR"\n'
        'exit 0\n',
        encoding="utf-8",
    )
    probe.chmod(0o755)
    return probe


def _make_agent(root: Path, *, source: bool = False, venv: bool = False,
                bootstrap: bool = False) -> Path:
    """Build a hermes-agent-shaped directory.

    `source=True` means a bare source checkout: run_agent.py and no hermes_cli,
    which is what makes it eligible for the source-first pass. The default is
    the pip-style shape instead — hermes_cli and no run_agent.py — because that
    is what an installed Agent looks like, and keeping the two shapes disjoint
    is what lets the source-first pass be exercised at all.
    `venv` adds the Windows venv path start.ps1 looks for; `bootstrap` adds the
    file managed_agent_startup.activate_managed_agent() imports to supply deps.
    """
    root.mkdir(parents=True, exist_ok=True)
    if source:
        (root / "run_agent.py").write_text("", encoding="utf-8")
    else:
        (root / "hermes_cli").mkdir(parents=True, exist_ok=True)
        (root / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    if venv:
        venv_python = root / "venv" / "Scripts" / "python.exe"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.write_text("", encoding="utf-8")
    if bootstrap:
        (root / "hermes_bootstrap.py").write_text("", encoding="utf-8")
    return root


def _stage_repo(fixture: Path) -> Path:
    """Copy start.ps1 into an isolated repo root inside the fixture.

    start.ps1 derives $RepoRoot from its own path, and one of the layouts it
    searches is that root's sibling, ../hermes-agent. Running the checked-out
    script therefore points that candidate at the real checkout next to the
    clone — which, for anyone using the documented side-by-side layout
    (hermes-webui/ next to hermes-agent/), is their actual Agent working tree.
    Copying the script keeps the repo root, the sibling candidate and every
    Agent inside tmp_path, so a fixture can create and remove an Agent of its
    own without ever reaching the developer's tree.
    """
    repo = fixture / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    shutil.copy2(START_PS1, repo / "start.ps1")
    # start.ps1 refuses to launch when server.py is absent from its own root.
    (repo / "server.py").write_text("", encoding="utf-8")
    return repo


def _stage_repo_with_activation(fixture: Path) -> Path:
    """Stage the repo with a server.py that performs the real Agent activation.

    `server.py` calls activate_managed_agent() as its first Agent-related
    import, and that hook is what runs the selected root's hermes_bootstrap.py.
    With the default empty stub server.py that hook is never reached, so a
    bootstrap that exits would look like a successful launch. Copying the real
    module in and calling it the way server.py does is what makes the
    bootstrap-exit layout observable.
    """
    repo = _stage_repo(fixture)
    shutil.copy2(REPO_ROOT / "managed_agent_startup.py", repo / "managed_agent_startup.py")
    (repo / "server.py").write_text(
        "from managed_agent_startup import activate_managed_agent\n"
        "\n"
        "activate_managed_agent()\n"
        "print('SERVER_BOUND')\n",
        encoding="utf-8",
    )
    return repo


def _make_venv_interpreter(root: Path) -> Path:
    """A venv interpreter stand-in that really runs the server stub.

    _make_agent(venv=True) only writes the path start.ps1 looks for. When the
    test needs the selected interpreter to reach server.py, the file has to be
    an executable that forwards to the interpreter running the tests.
    """
    venv_python = root / "venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    venv_python.chmod(0o755)
    return venv_python


def _run_start_ps1(
    fixture: Path,
    extra_env: dict[str, str] | None = None,
    *,
    activate: bool = False,
) -> tuple[int, str]:
    """Execute start.ps1 against a disposable fixture tree."""
    repo = _stage_repo_with_activation(fixture) if activate else _stage_repo(fixture)
    start_ps1 = repo / "start.ps1"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(fixture / "user"),
        "USERPROFILE": str(fixture / "user"),
        "LOCALAPPDATA": str(fixture / "local"),
        "HERMES_WEBUI_PYTHON": str(_write_stub_python(fixture)),
    }
    env.update(extra_env or {})
    # A None value means "unset", so a layout that must fall through to the
    # Agent's own venv (or to PATH) can be expressed without touching the base
    # environment.
    for key in [k for k, v in env.items() if v is None]:
        del env[key]
    completed = subprocess.run(
        [shutil.which("pwsh"), "-NoProfile", "-File", str(start_ps1)],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    return completed.returncode, completed.stdout + completed.stderr


def _agent_dir(output: str) -> str:
    for line in output.splitlines():
        if line.startswith(AGENT_DIR_PREFIX):
            return line[len(AGENT_DIR_PREFIX):].strip()
    return ""


def _python(output: str) -> str:
    for line in output.splitlines():
        if line.startswith(PYTHON_PREFIX):
            return line[len(PYTHON_PREFIX):].strip()
    return ""


def _state_dir(output: str) -> str:
    for line in output.splitlines():
        if line.startswith(STATE_DIR_PREFIX):
            return line[len(STATE_DIR_PREFIX):].strip()
    return ""


def test_standard_layout_reaches_agent_discovery(tmp_path):
    """The $Home/read-only-$HOME collision killed this before discovery."""
    fixture = tmp_path / "fx"
    agent = _make_agent(fixture / "local" / "hermes" / "hermes-agent")
    (fixture / "state").mkdir(parents=True)

    code, output = _run_start_ps1(fixture)

    assert "read-only or constant" not in output, (
        "start.ps1 aborted on the read-only automatic $HOME; the "
        "Test-HermesWebuiState parameter must not be named $Home"
    )
    assert _agent_dir(output) == str(agent), (
        "the standard layout must resolve the LOCALAPPDATA Agent; got:\n" + output
    )
    assert code == 0, output


def test_legacy_webui_state_does_not_hide_the_localappdata_agent(tmp_path):
    """WebUI state in the legacy home must not move where the Agent is searched.

    This is the upgrade shape from #2905: the session data has not moved yet
    but the Agent was installed at the new location. The #2905 preference is
    about where HERMES_HOME points; it must not decide where the Agent is
    looked for, or the only install on the machine becomes unreachable.
    """
    fixture = tmp_path / "fx"
    (fixture / "user" / ".hermes" / "webui").mkdir(parents=True)
    agent = _make_agent(fixture / "local" / "hermes" / "hermes-agent")

    code, output = _run_start_ps1(fixture)

    assert _agent_dir(output) == str(agent), (
        "a populated legacy WebUI home must not push the LOCALAPPDATA Agent out "
        "of the candidate list; got:\n" + output
    )
    assert code == 0, output


def test_legacy_only_agent_is_still_found(tmp_path):
    """The legacy Agent stays reachable now that candidate 5 is the new home."""
    fixture = tmp_path / "fx"
    (fixture / "user" / ".hermes" / "webui").mkdir(parents=True)
    agent = _make_agent(fixture / "user" / ".hermes" / "hermes-agent")

    code, output = _run_start_ps1(fixture)

    assert _agent_dir(output) == str(agent), (
        "an Agent that only exists at the legacy location is still a valid "
        "install; got:\n" + output
    )
    assert code == 0, output


def test_explicit_agent_dir_override_wins(tmp_path):
    fixture = tmp_path / "fx"
    agent = _make_agent(fixture / "elsewhere" / "hermes-agent")

    env_agent = str(agent)
    code, output = _run_start_ps1(fixture, extra_env={"HERMES_WEBUI_AGENT_DIR": env_agent})

    assert _agent_dir(output) == env_agent, (
        "HERMES_WEBUI_AGENT_DIR is the documented override and must not be "
        "second-guessed; got:\n" + output
    )
    assert code == 0, output


def test_bare_source_checkout_yields_to_the_installed_agent(tmp_path):
    """A source checkout with no venv and no bootstrap is not a usable pick.

    The source-first pass reaches a sibling checkout before the platform
    default, which is what api/config.py does. But on its own that checkout
    cannot import the Agent's dependencies: activate_managed_agent() no-ops
    without hermes_bootstrap.py, and with the export in place the launcher's
    whole job is to point the server at an Agent that can. When an installed
    Agent with a venv is also on the candidate list, master's hermes_cli-only
    pass picked that one, so discovery does too — as $AgentDir, not only as the
    interpreter, so the exported HERMES_WEBUI_AGENT_DIR and $Python agree.
    """
    fixture = tmp_path / "fx"
    (fixture / "user" / ".hermes" / "webui").mkdir(parents=True)
    installed = _make_agent(
        fixture / "local" / "hermes" / "hermes-agent", venv=True
    )
    # The sibling of the staged repo root, i.e. inside the fixture. Never
    # REPO_ROOT.parent: that is the real checkout for a side-by-side developer.
    _make_agent(fixture / "hermes-agent", source=True)

    code, output = _run_start_ps1(fixture)

    assert _agent_dir(output) == str(installed), (
        "a bare source checkout cannot supply the Agent's dependencies, so "
        "discovery must prefer the installed Agent and export it; got:\n" + output
    )
    expected = installed / "venv" / "Scripts" / "python.exe"
    assert _python(output) == str(expected), (
        "$Python has to be the venv of the exported Agent, otherwise the server "
        "imports from one install and runs on another; got:\n" + output
    )
    assert code == 0, output


def test_selected_pip_install_without_a_venv_keeps_its_root(tmp_path):
    """A selected pip-style Agent is authoritative even with no venv of its own.

    The install at %LOCALAPPDATA% is what both the source-first and pip passes
    reach first, so it is what api/config.py selects and what master selected
    too. It has no venv because a pip-style Agent's packages are importable
    from the interpreter this script already runs, and managed activation
    returns for it without importing a bootstrap. A venv sitting on a LATER
    candidate - here %USERPROFILE%\\hermes-agent, the launcher's last server
    candidate, which master never searched at all - must not take the launch
    away from it: neither the root nor the interpreter may move.
    """
    fixture = tmp_path / "fx"
    selected = _make_agent(fixture / "local" / "hermes" / "hermes-agent")
    _make_agent(fixture / "user" / "hermes-agent", venv=True)

    code, output = _run_start_ps1(fixture)

    assert _agent_dir(output) == str(selected), (
        "the selected install is the one the server would use and the one this "
        "script used before discovery was aligned with the server; a later venv "
        "is not authority to move it. Got:\n" + output
    )
    assert _python(output) == str(_write_stub_python(fixture)), (
        "$Python has to stay the interpreter the selected install already works "
        "with, instead of a venv from a different Agent; got:\n" + output
    )
    assert code == 0, output


def test_bootstrap_managed_root_without_a_venv_keeps_its_root(tmp_path):
    """A source checkout whose deps the bootstrap manages keeps its root too.

    Same shape as the pip case above, but the selected root is a checkout with
    hermes_bootstrap.py and no hermes_cli, so it supplies its own dependencies
    through the managed hook and needs no venv. Master's hermes_cli-only pass
    never accepted such a root, so nothing it would have picked was displaced
    and there is nothing to repair toward: the checkout stays selected, and the
    later venv must not move either the root or the interpreter.
    """
    fixture = tmp_path / "fx"
    selected = _make_agent(
        fixture / "local" / "hermes" / "hermes-agent", source=True, bootstrap=True
    )
    _make_agent(fixture / "user" / "hermes-agent", venv=True)

    code, output = _run_start_ps1(fixture)

    assert _agent_dir(output) == str(selected), (
        "a bootstrap-managed checkout needs no venv, so a later one is no reason "
        "to displace it; got:\n" + output
    )
    assert _python(output) == str(_write_stub_python(fixture)), (
        "$Python has to stay the interpreter the selected checkout already works "
        "with; got:\n" + output
    )
    assert code == 0, output


def test_source_checkout_with_its_own_venv_still_wins(tmp_path):
    """Source-first must survive when the checkout can actually be launched."""
    fixture = tmp_path / "fx"
    (fixture / "user" / ".hermes" / "webui").mkdir(parents=True)
    _make_agent(fixture / "local" / "hermes" / "hermes-agent", venv=True)
    sibling = _make_agent(fixture / "hermes-agent", source=True, venv=True)

    code, output = _run_start_ps1(fixture)

    assert _agent_dir(output) == str(sibling), (
        "a source checkout with its own venv is launchable and is what "
        "api/config.py selects, so it keeps priority over the install; got:\n"
        + output
    )
    assert _python(output) == str(sibling / "venv" / "Scripts" / "python.exe"), output
    assert code == 0, output


@pytest.mark.parametrize("hermes_home_set", [True, False])
def test_sibling_bootstrap_exit_still_reaches_the_server(tmp_path, hermes_home_set):
    """The reported layout: pip-style install + sibling source checkout.

    A normal install is pip-style (hermes_cli/, no run_agent.py) and has its own
    venv. A developer who also keeps a source checkout next to the repo gets
    the sibling picked by the source-first pass, and the launcher then runs that
    checkout's hermes_bootstrap.py. activate_managed_agent() only catches
    Exception, so a bootstrap that exits takes the launch down with it and the
    server never binds — even though the installed Agent would have started.

    Both HERMES_HOME shapes from the report are covered, because that decides
    whether the install is candidate 1 or further down the list.
    """
    fixture = tmp_path / "fx"
    installed = _make_agent(fixture / "local" / "hermes" / "hermes-agent", venv=True)
    venv_python = _make_venv_interpreter(installed)
    sibling = _make_agent(fixture / "hermes-agent", source=True, bootstrap=True)
    (sibling / "hermes_bootstrap.py").write_text(
        "raise SystemExit(7)\n", encoding="utf-8"
    )

    extra_env: dict[str, str | None] = {
        # Unset, so $Python comes from the selected Agent's venv rather than
        # from a stub — that is what makes the sibling's SystemExit(7) visible
        # as the process exit code instead of being masked by a stub.
        "HERMES_WEBUI_PYTHON": None,
    }
    if hermes_home_set:
        (fixture / "local" / "hermes").mkdir(parents=True, exist_ok=True)
        extra_env["HERMES_HOME"] = str(fixture / "local" / "hermes")

    code, output = _run_start_ps1(fixture, extra_env=extra_env, activate=True)

    assert code == 0, (
        "the sibling checkout's bootstrap exits 7 and must not decide the "
        "launch; got:\n" + output
    )
    assert _agent_dir(output) == str(installed), (
        "the installed Agent is the one that can start, and the export has to "
        "name it; got:\n" + output
    )
    assert _python(output) == str(venv_python), output
    assert "SERVER_BOUND" in output, (
        "activate_managed_agent() returned early because the installed Agent is "
        "pip-style, so the server reached its own code; got:\n" + output
    )


def test_explicit_state_dir_keeps_the_legacy_agent_ahead_of_home(tmp_path):
    """Candidate 5 must be the SERVER's default home, not HOME\\hermes-agent.

    With the WebUI state still at the legacy %USERPROFILE%\\.hermes, the server
    defaults its own home there too, so %USERPROFILE%\\.hermes\\hermes-agent is
    the install it would use. %USERPROFILE%\\hermes-agent is a flat checkout the
    server never searches, so when both exist the launcher has to reach the
    legacy one first or it exports a different Agent than the server defaults
    to.
    """
    fixture = tmp_path / "fx"
    (fixture / "user" / ".hermes" / "webui").mkdir(parents=True)
    legacy = _make_agent(fixture / "user" / ".hermes" / "hermes-agent")
    _make_agent(fixture / "user" / "hermes-agent")

    code, output = _run_start_ps1(
        fixture, extra_env={"HERMES_WEBUI_STATE_DIR": str(fixture / "state")}
    )

    assert _agent_dir(output) == str(legacy), (
        "the server's own default home must be searched before the flat "
        "HOME\\hermes-agent checkout; got:\n" + output
    )
    assert code == 0, output


def test_legacy_webui_state_redirects_only_the_state_dir(tmp_path):
    """A legacy state dir must not drag the working config home along with it.

    api/config.py reads providers and models from HERMES_HOME, so a user whose
    config.yaml sits in %LOCALAPPDATA%\\hermes has to keep reading it there.
    Only the webui/ state location is affected by the #2905 migration.
    """
    fixture = tmp_path / "fx"
    legacy_state = fixture / "user" / ".hermes" / "webui"
    legacy_state.mkdir(parents=True)
    new_home = fixture / "local" / "hermes"
    new_home.mkdir(parents=True)
    (new_home / "config.yaml").write_text("provider: local\n", encoding="utf-8")
    agent = _make_agent(new_home / "hermes-agent")
    probe = _write_env_probe_python(fixture)

    code, output = _run_start_ps1(
        fixture, extra_env={"HERMES_WEBUI_PYTHON": str(probe)}
    )

    assert _agent_dir(output) == str(agent), (
        "the new home's Agent must still be found in a legacy-state layout; "
        "got:\n" + output
    )
    assert _state_dir(output) == str(legacy_state), (
        "the sessions still live in the legacy webui directory, so the state "
        "default has to follow them there; got:\n" + output
    )
    assert f"probe HERMES_HOME={new_home}" in output, (
        "HERMES_HOME is where api/config.py reads providers and models from, so "
        "it must stay on the platform default that holds config.yaml; got:\n"
        + output
    )
    assert code == 0, output