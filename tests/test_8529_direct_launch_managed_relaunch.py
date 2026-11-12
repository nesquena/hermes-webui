"""#8529: direct launches must survive a PM-managed Agent's isolated relaunch.

A PM-managed Agent (a `.git` checkout whose install stamp says
`updateMechanism: self`, Agent >= v0.21.6) re-execs any process that imports
`hermes_bootstrap` from a foreign interpreter into its store Python with
`python -I -c "...runpy/exec(<caller>)"`. The child re-runs the caller from the
top with no third-party packages until `hermes_bootstrap` is imported again.

`managed_agent_startup.activate_managed_agent()` makes that first import happen
before WebUI imports YAML, but only did so when `HERMES_WEBUI_AGENT_DIR` was
exported (start.sh/bootstrap.py do; a systemd unit with only PYTHONPATH, the
README's manual launch and `pip install -e` do not). Without it the first
import came from `api.streaming`, after `api.profiles` had imported YAML in the
parent, so the relaunched child died on `import yaml`.

The fake Agent below reproduces the mechanism: its bootstrap re-execs the caller
with `-I -S` (no site-packages, like the store Python) and only "activates" the
dependency path when it is imported again in the child.

Every launch goes through tests/_hermetic_agent_launch.py: a copy of this WebUI
with no Agent beside it, a sanitized environment, a pre-flight that resolves the
Agent before anything imports it, and a guard against any other bootstrap.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests._hermetic_agent_launch import copy_webui, hermetic_env, run_guarded

WEBUI_ROOT = Path(__file__).resolve().parent.parent
HANDOFF = "_HERMES_WEBUI_RELAUNCH_AGENT_DIR"

_FAKE_BOOTSTRAP = textwrap.dedent(
    '''
    """Test double: PM-managed hermes_bootstrap + venv_sync.relaunch_command."""
    import json, os, sys
    _root = os.path.dirname(os.path.abspath(__file__))
    if os.environ.get("FAKE_PM_RELAUNCHED") != "1":
        os.environ["FAKE_PM_RELAUNCHED"] = "1"
        os.environ["FAKE_PM_SITE"] = json.dumps(
            [p for p in sys.path if p.rstrip(os.sep).endswith(("site-packages", "dist-packages"))]
        )
        body = sys.orig_argv[sys.orig_argv.index("-c") + 1]
        prefix = f"import sys, runpy; sys.path.insert(0, {_root!r}); sys.argv = {sys.argv!r}; "
        child_env = dict(os.environ)
        if os.environ.get("FAKE_PM_CHILD_DROPS_PYTHONPATH") == "1":
            # The store Python cannot see what the launching interpreter's own
            # site-packages (.pth, `pip install -e`) put on its path.
            child_env.pop("PYTHONPATH", None)
            child_env["HERMES_WEBUI_PYTHON"] = os.environ["FAKE_PM_MISSING_PYTHON"]
        os.execve(sys.executable, [sys.executable, "-I", "-S", "-c", prefix + f"exec({body!r})"], child_env)
    for _p in json.loads(os.environ["FAKE_PM_SITE"]):
        if _p not in sys.path:
            sys.path.append(_p)
    os.environ["FAKE_PM_ACTIVATED"] = "1"
    '''
)

# An Agent whose bootstrap activates in place (no relaunch: already the store
# Python, an older PM, HERMES_DISABLE_LAZY_INSTALLS). It records what it saw.
_FAKE_PLAIN_BOOTSTRAP = textwrap.dedent(
    f'''
    import os
    os.environ["FAKE_PM_SEEN_HANDOFF"] = os.environ.get({HANDOFF!r}, "<unset>")
    os.environ["FAKE_PM_SEEN_AGENT_DIR"] = os.environ.get("HERMES_WEBUI_AGENT_DIR", "<unset>")
    os.environ["FAKE_PM_ACTIVATED"] = "1"
    '''
)


def _fake_managed_agent(agent_dir: Path, bootstrap: str = _FAKE_BOOTSTRAP) -> Path:
    agent_dir.mkdir(parents=True)
    (agent_dir / "hermes_bootstrap.py").write_text(bootstrap, encoding="utf-8")
    (agent_dir / "run_agent.py").write_text(
        "import hermes_bootstrap\nclass AIAgent:\n    pass\n", encoding="utf-8"
    )
    (agent_dir / "pyproject.toml").write_text("[project]\nname = 'hermes-agent'\n", encoding="utf-8")
    _add_pm_launch_files(agent_dir)
    return agent_dir


def _add_pm_launch_files(agent_dir: Path) -> None:
    """What makes an Agent a PM launcher (v0.21.6+), beside hermes_bootstrap.py."""
    for rel in ("hermes_cli/venv_sync.py", "pm/environments.py"):
        (agent_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (agent_dir / rel).write_text("", encoding="utf-8")


@pytest.fixture(scope="module")
def webui(tmp_path_factory):
    """This WebUI, copied where neither `<repo>/..` nor `<repo>/../hermes-agent` is an Agent."""
    return copy_webui(tmp_path_factory.mktemp("isolated-webui"))


def _isolated_env(tmp_path: Path, **extra: str) -> dict:
    return hermetic_env(tmp_path, FAKE_PM_MISSING_PYTHON=str(tmp_path / "no-python"), **extra)


def _import_server(webui: Path) -> str:
    return (
        "import os, sys\n"
        f"sys.path.insert(0, {str(webui)!r})\n"
        "import server\n"
        "print('SERVER_IMPORTED relaunched=%s activated=%s' % (\n"
        "    os.environ.get('FAKE_PM_RELAUNCHED'), os.environ.get('FAKE_PM_ACTIVATED')))\n"
        f"print('AFTER handoff=%s agent_dir=%r' % (os.environ.get({HANDOFF!r}), os.environ.get('HERMES_WEBUI_AGENT_DIR')))\n"
    )


@pytest.mark.parametrize(
    "shape", ["discovered", "discovered-cwd-agent", "pythonpath-only", "blank-agent-dir-env"]
)
def test_direct_launch_survives_isolated_relaunch_without_agent_dir_env(tmp_path, webui, shape):
    agent_dir_env = None
    if shape == "pythonpath-only":
        # systemd unit with Environment=PYTHONPATH=<agent> at a non-standard path (#8529 A)
        agent_dir = _fake_managed_agent(tmp_path / "opt" / "hermes-agent")
        # PYTHONPATH precedes site-packages, so the interpreter finds the fake.
        env = _isolated_env(tmp_path, PYTHONPATH=str(agent_dir), HERMES_WEBUI_PYTHON=sys.executable)
        cwd = webui
    else:
        # Agent at $HERMES_HOME/hermes-agent, nothing exported (README manual
        # launch from the Agent checkout, a bare systemd unit). A blank
        # HERMES_WEBUI_AGENT_DIR (`Environment=HERMES_WEBUI_AGENT_DIR=`) is unset
        # for api.config, so it must be unset for activation too.
        agent_dir = _fake_managed_agent(tmp_path / "hermes-home" / "hermes-agent")
        if shape == "blank-agent-dir-env":
            agent_dir_env = "  "
        extra = {"HERMES_WEBUI_AGENT_DIR": agent_dir_env} if agent_dir_env else {}
        env = _isolated_env(tmp_path, **extra)
        cwd = agent_dir if shape == "discovered-cwd-agent" else webui
    result, resolved = run_guarded(_import_server(webui), webui=webui, env=env, cwd=cwd)
    assert resolved["ACTIVATES"] == resolved["RUNTIME"] == agent_dir.resolve(), resolved
    assert result.returncode == 0, (
        "WebUI died in the Agent's isolated relaunch: activation ran after a "
        f"third-party import.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr[-3000:]}"
    )
    assert "SERVER_IMPORTED relaunched=1 activated=1" in result.stdout, result.stdout
    # The relaunch handoff is private and one-shot, and the operator's variable
    # is never written: not even in the relaunched child.
    assert f"AFTER handoff=None agent_dir={agent_dir_env!r}" in result.stdout, result.stdout


def test_relaunched_child_activates_the_agent_the_parent_discovered(tmp_path, webui):
    """The launching interpreter found the Agent through its own import path; the
    Agent's store Python cannot. The child must get the parent's root handed over
    privately, not re-discover and come up empty (sandbox method H2: an Agent
    reachable only through the WebUI venv's .pth)."""
    agent_dir = _fake_managed_agent(tmp_path / "opt" / "hermes-agent")
    env = _isolated_env(
        tmp_path, PYTHONPATH=str(agent_dir), HERMES_WEBUI_PYTHON=sys.executable,
        FAKE_PM_CHILD_DROPS_PYTHONPATH="1",
    )
    result, resolved = run_guarded(_import_server(webui), webui=webui, env=env, cwd=webui)
    assert resolved["ACTIVATES"] == agent_dir.resolve(), resolved
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr[-3000:]}"
    )
    assert "SERVER_IMPORTED relaunched=1 activated=1" in result.stdout, result.stdout
    assert "AFTER handoff=None agent_dir=None" in result.stdout, result.stdout


_ACTIVATE_AND_REPORT = """
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, {webui!r})
from api import startup
auth_before = startup._agent_dir()
import managed_agent_startup
managed_agent_startup.activate_managed_agent()
report = dict(
    activated=os.environ.get("FAKE_PM_ACTIVATED"),
    seen_handoff=os.environ.get("FAKE_PM_SEEN_HANDOFF"),
    seen_agent_dir=os.environ.get("FAKE_PM_SEEN_AGENT_DIR"),
    handoff_after=os.environ.get({handoff!r}),
    agent_dir_after=os.environ.get("HERMES_WEBUI_AGENT_DIR"),
    auth_before=str(auth_before) if auth_before else None,
    auth_after=str(startup._agent_dir()) if startup._agent_dir() else None,
)
def _no_pip(args, **kwargs):
    print("PIP_REACHED %r" % (args,), flush=True)
    raise SystemExit(87)
_run, subprocess.run = subprocess.run, _no_pip
os.environ["HERMES_WEBUI_AUTO_INSTALL"] = "1"
report["auto_install"] = startup.auto_install_agent_deps(expected_agent_dir=Path({agent!r}))
subprocess.run = _run
del os.environ["HERMES_WEBUI_AUTO_INSTALL"]
if {with_config!r}:
    import api.config as config
    report.update(
        runtime_agent=str(config._AGENT_DIR),
        proven_python=config._DISCOVERY.python_exe,
        python_exe=config.PYTHON_EXE,
    )
print("REPORT=" + json.dumps(report))
"""


def _activate_and_report(tmp_path, webui, agent_dir, env, *, with_config=False) -> dict:
    script = _ACTIVATE_AND_REPORT.format(
        webui=str(webui), handoff=HANDOFF, agent=str(agent_dir), with_config=with_config,
    )
    result, resolved = run_guarded(script, webui=webui, env=env, cwd=webui)
    assert resolved["ACTIVATES"] == agent_dir.resolve(), resolved
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr[-3000:]}"
    assert "PIP_REACHED" not in result.stdout, result.stdout
    line = next(x for x in result.stdout.splitlines() if x.startswith("REPORT="))
    return json.loads(line.removeprefix("REPORT="))


def test_discovered_agent_is_not_turned_into_the_operator_override(tmp_path, webui):
    """No relaunch: the activation must leave the process exactly as master's
    discovery would. A discovered-only Agent (here: XDG data dir) must not become
    HERMES_WEBUI_AGENT_DIR, which api.startup reads as auto-install authorization
    (#8529 gate: the public export let HERMES_WEBUI_AUTO_INSTALL reach pip)."""
    env = _isolated_env(tmp_path)
    agent_dir = _fake_managed_agent(
        Path(env["XDG_DATA_HOME"]) / "hermes-agent", bootstrap=_FAKE_PLAIN_BOOTSTRAP,
    )
    report = _activate_and_report(tmp_path, webui, agent_dir, env)
    assert report["activated"] == "1", report
    # Offered to a would-be relaunched child while the bootstrap ran...
    assert report["seen_handoff"] == str(agent_dir.resolve()), report
    assert report["seen_agent_dir"] == "<unset>", report
    # ...and gone once activation returned.
    assert report["handoff_after"] is None, report
    assert report["agent_dir_after"] is None, report
    # Auto-install authorization is what it was before activation: none.
    assert report["auth_before"] is None and report["auth_after"] is None, report
    assert report["auto_install"] is False, report


def test_discovery_keeps_the_interpreter_that_proved_the_agent(tmp_path, webui):
    """An Agent found through the launching interpreter keeps that interpreter as
    api.config's proven Python, ahead of WebUI's own .venv (#8529 gate: the
    public export reset `_DISCOVERY.python_exe` to None)."""
    agent_dir = _fake_managed_agent(
        tmp_path / "opt" / "hermes-agent", bootstrap=_FAKE_PLAIN_BOOTSTRAP,
    )
    env = _isolated_env(tmp_path, PYTHONPATH=str(agent_dir))
    # Unset, so discovery proves the Agent with the launching interpreter.
    env.pop("HERMES_WEBUI_PYTHON")
    report = _activate_and_report(tmp_path, webui, agent_dir, env, with_config=True)
    assert report["activated"] == "1", report
    assert report["runtime_agent"] == str(agent_dir.resolve()), report
    assert report["proven_python"] == sys.executable, report
    assert report["python_exe"] == sys.executable, report
    assert report["handoff_after"] is None and report["agent_dir_after"] is None, report


# A pre-PM Agent's bootstrap (the Windows UTF-8 shim, since 2026-05) moves its
# checkout to the FRONT of sys.path. Imported before WebUI's own dependencies,
# copies vendored into that checkout would shadow the working installed ones
# (#8529 gate: a vendored `cryptography` turned OIDC off). Such an Agent never
# relaunches, so a direct launch must keep master's behaviour: no early import.
_FAKE_PRE_PM_BOOTSTRAP = textwrap.dedent(
    """
    import os, sys
    _root = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p) != _root]
    sys.path.insert(0, _root)
    os.environ["FAKE_PM_ACTIVATED"] = "1"
    """
)

_PRE_PM_REPORT = """
import json, os, sys
sys.path.insert(0, {webui!r})
import managed_agent_startup
managed_agent_startup.activate_managed_agent()
print("REPORT=" + json.dumps(dict(
    activated=os.environ.get("FAKE_PM_ACTIVATED"),
    bootstrap_imported="hermes_bootstrap" in sys.modules,
    agent_on_path={agent!r} in sys.path,
)))
"""


def _pre_pm_agent(agent_dir: Path) -> Path:
    agent_dir = _fake_managed_agent(agent_dir, bootstrap=_FAKE_PRE_PM_BOOTSTRAP)
    (agent_dir / "hermes_cli" / "venv_sync.py").unlink()
    (agent_dir / "pm" / "environments.py").unlink()
    return agent_dir


def _pre_pm_report(webui, env, agent_dir) -> dict:
    script = _PRE_PM_REPORT.format(webui=str(webui), agent=str(agent_dir.resolve()))
    result, resolved = run_guarded(script, webui=webui, env=env, cwd=webui)
    assert resolved["ACTIVATES"] == agent_dir.resolve(), resolved
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr[-3000:]}"
    line = next(x for x in result.stdout.splitlines() if x.startswith("REPORT="))
    return json.loads(line.removeprefix("REPORT="))


def test_discovered_pre_pm_agent_keeps_masters_lazy_bootstrap(tmp_path, webui):
    agent_dir = _pre_pm_agent(tmp_path / "hermes-home" / "hermes-agent")
    report = _pre_pm_report(webui, _isolated_env(tmp_path), agent_dir)
    assert report == dict(activated=None, bootstrap_imported=False, agent_on_path=False), report


def test_explicit_pre_pm_agent_is_still_activated_as_on_master(tmp_path, webui):
    # start.sh / bootstrap.py export the variable; that path is unchanged.
    agent_dir = _pre_pm_agent(tmp_path / "hermes-home" / "hermes-agent")
    env = _isolated_env(tmp_path, HERMES_WEBUI_AGENT_DIR=str(agent_dir))
    report = _pre_pm_report(webui, env, agent_dir)
    assert report == dict(activated="1", bootstrap_imported=True, agent_on_path=True), report


def test_pm_launch_markers_match_the_docker_selection():
    import managed_agent_startup

    function = _select_staged_agent_function()
    for marker in managed_agent_startup._PM_LAUNCH_MARKERS:
        assert f'"$_src/{marker}"' in function, marker


def test_relaunch_handoff_is_private_to_managed_agent_startup():
    """Only the startup hook may read the handoff: api.config, auto-install, the
    gateway and every subprocess keep master's view of HERMES_WEBUI_AGENT_DIR."""
    skip_dirs = {".git", "node_modules", "tests", "__pycache__", ".venv", "venv"}
    readers = []
    for dirpath, dirnames, filenames in os.walk(WEBUI_ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix in {".py", ".sh", ".bash", ".ps1", ".js", ".nix"} and HANDOFF in path.read_text(
                encoding="utf-8", errors="ignore"
            ):
                readers.append(path.relative_to(WEBUI_ROOT).as_posix())
    assert readers == ["managed_agent_startup.py"], readers


def test_explicit_agent_dir_still_wins_over_discovery_and_the_handoff(tmp_path, monkeypatch):
    import managed_agent_startup

    _fake_managed_agent(tmp_path / "hermes-home" / "hermes-agent")
    explicit = tmp_path / "explicit-agent"
    explicit.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", f" {explicit} ")
    monkeypatch.setenv(HANDOFF, str(tmp_path / "handed-off"))
    assert managed_agent_startup._resolve_agent_dir(WEBUI_ROOT) == str(explicit)
    assert HANDOFF not in os.environ


def test_relaunch_handoff_wins_over_discovery_once(tmp_path, monkeypatch):
    import managed_agent_startup
    from api import startup

    handed = tmp_path / "handed-off"
    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    monkeypatch.setenv(HANDOFF, f" {handed} ")
    monkeypatch.setattr(
        startup, "discover_runtime_agent_identity",
        lambda **_kw: startup._AgentDiscovery(tmp_path / "discovered"),
    )
    assert managed_agent_startup._resolve_agent_dir(WEBUI_ROOT) == str(handed)
    assert HANDOFF not in os.environ
    # Consumed: the next resolution is discovery again.
    assert managed_agent_startup._resolve_agent_dir(WEBUI_ROOT) == str(tmp_path / "discovered")


def test_startup_activation_and_api_config_resolve_the_same_agent(tmp_path, webui):
    """Both a legacy ~/.hermes/hermes-agent and a different platform-default home
    (Windows: %LOCALAPPDATA%\\\\hermes) hold an Agent; HERMES_HOME is unset. The hook
    must activate the Agent api.config then runs against (#8355 review)."""
    legacy = _fake_managed_agent(tmp_path / "home" / ".hermes" / "hermes-agent")
    platform_default = tmp_path / "localappdata" / "hermes"
    current = _fake_managed_agent(platform_default / "hermes-agent")
    env = _isolated_env(tmp_path)
    env.pop("HERMES_HOME")
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(webui)!r})\n"
        "from pathlib import Path\n"
        "import api.paths as paths\n"
        f"paths._platform_default_hermes_home = lambda: Path({str(platform_default)!r})\n"
        "import managed_agent_startup as m\n"
        f"print('ACTIVATE', m._resolve_agent_dir(Path({str(webui)!r})))\n"
        "import api.config as config\n"
        "print('RUNTIME', config._AGENT_DIR)\n"
    )
    # Nothing here imports a bootstrap; still pre-flight + guard the launch.
    result, _resolved = run_guarded(script, webui=webui, env=env, cwd=webui, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    lines = dict(
        line.split(" ", 1) for line in result.stdout.splitlines()
        if line.startswith(("ACTIVATE", "RUNTIME"))
    )
    assert lines["ACTIVATE"] == lines["RUNTIME"] == str(current.resolve()), (lines, str(legacy))


def test_no_discoverable_agent_leaves_startup_untouched(tmp_path, monkeypatch):
    import managed_agent_startup
    from api import startup

    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    monkeypatch.setattr(
        startup, "discover_runtime_agent_identity",
        lambda **_kw: startup._AgentDiscovery(None),
    )
    before = list(sys.path)
    managed_agent_startup.activate_managed_agent()
    assert [p for p in sys.path if p not in before and p != str(WEBUI_ROOT)] == []
    assert HANDOFF not in os.environ


# ── Docker (docker_init.bash) ────────────────────────────────────────────────
# The single-container compose stack mounts the host's ~/.hermes, so discovery
# lands on the host's own Agent checkout. A git checkout there is a self-managed
# install to a PM-managed Agent, whose bootstrap would sync into the host's
# ~/.hermes/installs and re-exec into the host's store Python. docker_init must
# hand server.py the staged copy (no .git, the tree /app/venv was built from)
# in exactly that case, and leave the multi-container stacks alone.

_DOCKER_INIT = WEBUI_ROOT / "docker_init.bash"


def _docker_init_source() -> str:
    return _DOCKER_INIT.read_text(encoding="utf-8")


def _select_staged_agent_function() -> str:
    match = re.search(
        r"^select_staged_agent_for_git_checkout\(\) \{\n.*?^\}\n",
        _docker_init_source(), re.M | re.S,
    )
    assert match, "docker_init.bash must define select_staged_agent_for_git_checkout()"
    return match.group(0)


def _run_selection(tmp_path: Path, src: str, stage: str, agent_dir_env: str | None,
                   extra_env: dict | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k not in _DECISION_ENV}
    # A HERMES_HOME of its own, so no fixture is a Hermes home's hermes-agent by accident.
    env["HERMES_HOME"] = str(tmp_path / "unrelated-home")
    if agent_dir_env is not None:
        env["HERMES_WEBUI_AGENT_DIR"] = agent_dir_env
    for key, value in (extra_env or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    lazy_in = env.get("HERMES_DISABLE_LAZY_INSTALLS", "<unset>")
    script = (
        "set -eu\n"
        + _select_staged_agent_function()
        + 'select_staged_agent_for_git_checkout "$1" "$2"\n'
        # The selection never touches the Agent's lazy-install policy (#8594 gate).
        + 'bash -c \'printf "LAZY=%s\\n" "${HERMES_DISABLE_LAZY_INSTALLS-<unset>}"\'\n'
        + 'bash -c \'printf "RESULT=%s\\n" "${HERMES_WEBUI_AGENT_DIR-<unset>}"\'\n'
    )
    result = subprocess.run(
        ["bash", "-c", script, "docker_init", src, stage],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.rstrip("\n").splitlines()
    if "\n" not in lazy_in:
        assert lines[-2] == "LAZY=" + lazy_in, lines
    return lines[-1].removeprefix("RESULT=")


_DECISION_ENV = ("HERMES_WEBUI_AGENT_DIR", "HERMES_DISABLE_LAZY_INSTALLS", "HERMES_INSTALL_ROOT",
                 "HERMES_HOME", "HERMES_BASE_HOME", "HERMES_DATA_DIR_SUFFIX")


@pytest.fixture
def staged_agent(tmp_path):
    src = tmp_path / "mount" / "hermes-agent"
    src.mkdir(parents=True)
    (src / "run_agent.py").write_text("", encoding="utf-8")
    (src / "hermes_bootstrap.py").write_text("", encoding="utf-8")
    (src / "pyproject.toml").write_text('[project]\nname = "hermes-agent"\n', encoding="utf-8")
    _add_pm_launch_files(src)
    # Installed by the official installer or `hermes update`, which stamp it self-updating.
    (src / "install-stamp.json").write_text('{"updateMechanism": "self"}', encoding="utf-8")
    stage = tmp_path / "app" / "hermes-agent-src"
    stage.mkdir(parents=True)
    (stage / "run_agent.py").write_text("", encoding="utf-8")
    return src, stage


def test_docker_single_container_git_checkout_runs_the_staged_agent(tmp_path, staged_agent):
    src, stage = staged_agent
    (src / ".git").mkdir()
    assert _run_selection(tmp_path, str(src), str(stage), None) == str(stage)
    # A blank or whitespace-only value is unset everywhere else too
    # (api.config, activation), so it must not count as an operator override.
    for blank in ("", "  ", "\t", " \n "):
        assert _run_selection(tmp_path, str(src), str(stage), blank) == str(stage), repr(blank)


def test_docker_selection_leaves_every_other_topology_alone(tmp_path, staged_agent):
    src, stage = staged_agent
    # Two-/three-container: the agent image's source volume carries no .git.
    assert _run_selection(tmp_path, str(src), str(stage), None) == "<unset>"
    # No Agent mounted at all (browser-only container).
    assert _run_selection(tmp_path, "", str(stage), None) == "<unset>"
    (src / ".git").mkdir()
    # The operator's own choice always wins.
    assert _run_selection(tmp_path, str(src), str(stage), "/srv/agent") == "/srv/agent"
    assert _run_selection(tmp_path, str(src), str(stage), " /srv/agent ") == " /srv/agent "
    # A pre-PM git checkout never relaunches: keep master's behaviour (the
    # mounted checkout, in-app git update intact). Pre-PM Agents ship a
    # hermes_bootstrap.py too, so each PM launch file must be required.
    for marker in ("hermes_bootstrap.py", "hermes_cli/venv_sync.py", "pm/environments.py"):
        body = (src / marker).read_text(encoding="utf-8")
        (src / marker).unlink()
        assert _run_selection(tmp_path, str(src), str(stage), None) == "<unset>", marker
        (src / marker).write_text(body, encoding="utf-8")
    assert _run_selection(tmp_path, str(src), str(stage), None) == str(stage)
    # No usable stage (staging failed): keep today's behaviour.
    (stage / "run_agent.py").unlink()
    assert _run_selection(tmp_path, str(src), str(stage), None) == "<unset>"
    # A stage that still has .git would relaunch just the same: do not use it.
    (stage / "run_agent.py").write_text("", encoding="utf-8")
    (stage / ".git").mkdir()
    assert _run_selection(tmp_path, str(src), str(stage), None) == "<unset>"


def test_docker_init_selects_the_stage_before_launching_server():
    source = _docker_init_source()
    call = source.index('select_staged_agent_for_git_checkout "$_agent_src" "$_agent_stage"')
    launch = source.index("cd /app; python server.py")
    assert call < launch
    # Both names must refer to the tree the staging block writes, and that tree
    # must be copied without .git.
    assert re.search(r'^_agent_stage=/app/hermes-agent-src$', source, re.M)
    assert '_stage_src="/app/hermes-agent-src"' in source
    assert "--exclude='.git'" in source
    # The cp -a fallback (no rsync in the image) must drop .git as well, or its
    # stage would never be selected (exercised in the test below).
    assert 'rm -rf "$_stage_src/.git"' in source
    # The selection must not be skipped by the fast-restart path: it sits after
    # the dependency install / reuse decision, not inside install_venv_dependencies.
    assert call > source.index("ensure_hindsight_client_docker_dependency\n\n")


def test_cp_fallback_stage_drops_git_from_a_read_only_mount(tmp_path, staged_agent):
    """No rsync in the image: `cp -a` copies a :ro mount's .git with its 0555
    directories, so .git can only go after the chmod. The stage must end up
    without .git, i.e. selectable (#8529 gate r3)."""
    src, _ = staged_agent
    (src / ".git" / "objects").mkdir(parents=True)
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    source = _docker_init_source()
    start = source.index("    if command -v rsync >/dev/null 2>&1; then")
    # Through the chmod and the .git removal, up to the stage manifest.
    block = source[start:source.index("    # Never fatal: without the manifest", start)]
    assert 'rm -rf "$_stage_src/.git"' in block
    bindir = tmp_path / "bin-without-rsync"
    bindir.mkdir()
    for tool in ("cp", "rm", "chmod", "find", "mkdir"):
        real = shutil.which(tool)
        assert real, tool
        (bindir / tool).symlink_to(real)
    stage = tmp_path / "app2" / "hermes-agent-src"
    stage.mkdir(parents=True)
    for d in [src, *[p for p in src.rglob("*") if p.is_dir()]]:
        d.chmod(0o555)
    try:
        script = (
            "stage_it() {\n"
            f'  local _agent_src="{src}" _stage_src="{stage}"\n'
            + block
            + "}\nstage_it; echo rc=$?\n"
        )
        result = subprocess.run(
            [shutil.which("bash") or "/bin/bash", "-c", script], env={"PATH": str(bindir)},
            capture_output=True, text=True, timeout=30,
        )
    finally:
        for d in [src, *[p for p in src.rglob("*") if p.is_dir()]]:
            d.chmod(0o755)
    assert result.stdout.strip().endswith("rc=0"), result.stdout + result.stderr
    assert (stage / "run_agent.py").is_file()
    assert not (stage / ".git").exists(), result.stdout + result.stderr
    assert _run_selection(tmp_path, str(src), str(stage), None) == str(stage)


# --- #8581/#8593: the stage only when the mounted Agent's bootstrap would really relaunch ---
# Agent v0.22.5 hermes_cli/venv_sync.py prepare_launch() returns without relaunching when
# HERMES_DISABLE_LAZY_INSTALLS is 1/true/yes (lowercased, untrimmed), without pyproject.toml,
# or unless read_install_stamp() returns exactly updateMechanism == "self". It reads the whole
# stamp as utf-8-sig JSON (OSError, ValueError or a non-object reads as {}). With no stamp file,
# step_adopt_blessed_checkout() adopts the checkout as "self" only when it resolves to
# <home>/hermes-agent or /usr/local/lib/hermes-agent and is writable. On v0.52.544 every other
# setup ran the mounted checkout and kept its .git (in-app Agent update); exp-v0.52.560..571
# ran the stage for some of them (#8593).


def test_disabled_lazy_installs_keep_the_mounted_checkout(tmp_path, staged_agent):
    src, stage = staged_agent
    (src / ".git").mkdir()
    for value in ("1", "true", "TRUE", "yes", "Yes"):
        assert _run_selection(tmp_path, str(src), str(stage), None,
                              {"HERMES_DISABLE_LAZY_INSTALLS": value}) == "<unset>", value
    # Anything prepare_launch() does not read as disabled still relaunches → stage.
    for value in ("", "0", "false", "no", " 1", "1 ", "on", "1\n", "true\n"):
        assert _run_selection(tmp_path, str(src), str(stage), None,
                              {"HERMES_DISABLE_LAZY_INSTALLS": value}) == str(stage), repr(value)


def test_a_checkout_without_pyproject_keeps_the_mount(tmp_path, staged_agent):
    src, stage = staged_agent
    (src / ".git").mkdir()
    (src / "pyproject.toml").unlink()
    assert _run_selection(tmp_path, str(src), str(stage), None) == "<unset>"


def test_only_a_self_updating_stamp_selects_the_stage(tmp_path, staged_agent):
    src, stage = staged_agent
    (src / ".git").mkdir()
    stamp = src / "install-stamp.json"
    cases = {
        '{"updateMechanism": "self"}': str(stage),
        '\ufeff{"updateMechanism": "self", "source": "git"}': str(stage),     # BOM, as the Agent reads it
        '{"updateMechanism": "external"}': "<unset>",
        '{"updateMechanism": "app-installer"}': "<unset>",
        '{"source": "git"}': "<unset>",
        '{"updateMechanism": "self"': "<unset>",                              # malformed → {} → no relaunch
        '["self"]': "<unset>",
    }
    for text, want in cases.items():
        stamp.write_text(text, encoding="utf-8")
        assert _run_selection(tmp_path, str(src), str(stage), None) == want, text
    for text in ('{"updateMechanism": "self\\n"}', '{"updateMechanism": "Self"}', '{"updateMechanism": " self"}'):
        stamp.write_text(text, encoding="utf-8")                              # compared exactly (#8593)
        assert _run_selection(tmp_path, str(src), str(stage), None) == "<unset>", text
    stamp.unlink()
    stamp.mkdir()                                                             # a directory reads as {}
    assert _run_selection(tmp_path, str(src), str(stage), None) == "<unset>"
    stamp.rmdir()
    # Stampless and writable: the Agent may adopt it after a later HERMES_HOME change
    # (profile switch, then an os.execv restart without docker_init), wherever it is
    # mounted, so the stage, as on exp-v0.52.571 (#8594 gate).
    assert _run_selection(tmp_path, str(src), str(stage), None) == str(stage)


def test_an_unreadable_stamp_without_python3_prefers_the_stage(tmp_path, staged_agent):
    """Keeping a mount that does relaunch crash-loops (#8529); an unneeded stage only costs the
    in-app Agent update. With no python3 to read the stamp, the selection keeps the #8529 fix."""
    src, stage = staged_agent
    (src / ".git").mkdir()
    (src / "install-stamp.json").write_text('{"updateMechanism": "external"}', encoding="utf-8")
    bindir = tmp_path / "bin-no-python"
    bindir.mkdir()
    import shutil
    for tool in ("bash", "tr", "printf"):
        found = shutil.which(tool)
        if found:
            (bindir / tool).symlink_to(found)
    assert _run_selection(tmp_path, str(src), str(stage), None, {"PATH": str(bindir)}) == str(stage)


def test_the_operator_override_still_wins_over_the_relaunch_checks(tmp_path, staged_agent):
    src, stage = staged_agent
    (src / ".git").mkdir()
    assert _run_selection(tmp_path, str(src), str(stage), "/srv/agent",
                          {"HERMES_DISABLE_LAZY_INSTALLS": "1"}) == "/srv/agent"


def _tool_bin(tmp_path: Path, name: str, extra: dict | None = None) -> Path:
    """A PATH dir with the real shell tools the selector uses plus optional stub scripts."""
    bindir = tmp_path / name
    bindir.mkdir()
    for tool in ("bash", "tr", "printf", "wc", "timeout", "python3"):
        found = shutil.which(tool)
        if found and tool not in (extra or {}):
            (bindir / tool).symlink_to(found)
    for tool, body in (extra or {}).items():
        stub = bindir / tool
        stub.write_text("#!" + shutil.which("bash") + "\n" + body + "\n", encoding="utf-8")
        stub.chmod(0o755)
    return bindir


def test_the_stamp_is_read_where_the_agent_reads_it(tmp_path, staged_agent):
    """pm/paths.py install_root(): a non-empty HERMES_INSTALL_ROOT moves the executing tree's stamp."""
    src, stage = staged_agent
    (src / ".git").mkdir()
    (src / "install-stamp.json").write_text('{"updateMechanism": "external"}', encoding="utf-8")
    root = tmp_path / "install-root"
    root.mkdir()
    (root / "install-stamp.json").write_text('{"updateMechanism": "self"}', encoding="utf-8")
    # The Agent relaunches (it reads root's "self"): the stage, never the external stamp beside the checkout.
    assert _run_selection(tmp_path, str(src), str(stage), None, {"HERMES_INSTALL_ROOT": str(root)}) == str(stage)
    (root / "install-stamp.json").write_text('{"updateMechanism": "external"}', encoding="utf-8")
    (src / "install-stamp.json").write_text('{"updateMechanism": "self"}', encoding="utf-8")
    assert _run_selection(tmp_path, str(src), str(stage), None, {"HERMES_INSTALL_ROOT": str(root)}) == "<unset>"
    # An empty value is unset, as in the Agent (`if env`): the stamp beside the checkout decides.
    assert _run_selection(tmp_path, str(src), str(stage), None, {"HERMES_INSTALL_ROOT": ""}) == str(stage)


def test_a_fifo_or_oversized_stamp_prefers_the_stage_without_blocking(tmp_path, staged_agent):
    src, stage = staged_agent
    (src / ".git").mkdir()
    stamp = src / "install-stamp.json"
    stamp.unlink()
    os.mkfifo(stamp)                                      # open() on it would block startup forever
    assert _run_selection(tmp_path, str(src), str(stage), None) == str(stage)
    stamp.unlink()
    # The Agent reads the whole stamp, whatever its size (#8593): decide on what it says.
    stamp.write_text('{"updateMechanism": "external", "pad": "' + "x" * 70000 + '"}', encoding="utf-8")
    assert _run_selection(tmp_path, str(src), str(stage), None) == "<unset>"
    stamp.write_text('{"updateMechanism": "self", "pad": "' + "x" * 70000 + '"}', encoding="utf-8")
    assert _run_selection(tmp_path, str(src), str(stage), None) == str(stage)
    # Beyond what the reader will load: undecidable, so the stage.
    with open(stamp, "wb") as fh:
        fh.truncate(16 * 1024 * 1024 + 1)
    assert _run_selection(tmp_path, str(src), str(stage), None) == str(stage)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads mode-000 files")
def test_an_unreadable_stamp_prefers_the_stage(tmp_path, staged_agent):
    """Fail-closed: the Agent (the same user) would read an unreadable stamp as {} and not relaunch,
    but the selector cannot prove what the Agent will see, so it prefers the stage. A kept mount that
    does relaunch crash-loops (#8529); an unneeded stage only costs the in-app Agent update."""
    src, stage = staged_agent
    (src / ".git").mkdir()
    stamp = src / "install-stamp.json"
    stamp.write_text('{"updateMechanism": "self"}', encoding="utf-8")
    stamp.chmod(0)
    try:
        assert _run_selection(tmp_path, str(src), str(stage), None) == str(stage)
    finally:
        stamp.chmod(0o644)


def test_a_failing_or_hanging_stamp_reader_prefers_the_stage(tmp_path, staged_agent):
    src, stage = staged_agent
    (src / ".git").mkdir()
    (src / "install-stamp.json").write_text('{"updateMechanism": "external"}', encoding="utf-8")
    failing = _tool_bin(tmp_path, "bin-fail", {"python3": "exit 1"})
    assert _run_selection(tmp_path, str(src), str(stage), None, {"PATH": str(failing)}) == str(stage)
    if shutil.which("timeout"):
        import time
        hanging = _tool_bin(tmp_path, "bin-hang", {"python3": shutil.which("sleep") + " 30; echo external"})
        t0 = time.monotonic()
        assert _run_selection(tmp_path, str(src), str(stage), None, {"PATH": str(hanging)}) == str(stage)
        assert time.monotonic() - t0 < 20


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads mode-000 files")
def test_an_unreadable_stamp_is_quiet_and_explained(tmp_path, staged_agent):
    """No shell redirection error in the boot log; one line says why the stage was chosen."""
    src, stage = staged_agent
    (src / ".git").mkdir()
    stamp = src / "install-stamp.json"
    stamp.write_text('{"updateMechanism": "external"}', encoding="utf-8")
    stamp.chmod(0)
    try:
        env = {k: v for k, v in os.environ.items() if k not in _DECISION_ENV}
        env["HERMES_HOME"] = str(tmp_path / "unrelated-home")
        script = ("set -eu\n" + _select_staged_agent_function()
                  + 'select_staged_agent_for_git_checkout "$1" "$2"\n')
        proc = subprocess.run(["bash", "-c", script, "docker_init", str(src), str(stage)],
                              env=env, capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
    finally:
        stamp.chmod(0o644)
    assert "Permission denied" not in proc.stderr, proc.stderr
    assert "could not tell whether the Agent" in proc.stdout and "running the staged copy" in proc.stdout, proc.stdout


# --- #8593 / #8594 gate: a stampless checkout's adoption depends on HERMES_HOME at relaunch time ---
# WebUI rewrites HERMES_HOME after docker_init (init_profile_state(), profile switches) and
# its in-app update restarts with os.execv without docker_init, so the selection decides only
# on what no later HERMES_HOME can change: a writable stampless checkout always gets the stage.


def test_a_writable_stampless_checkout_runs_the_stage_wherever_it_is_mounted(tmp_path, staged_agent):
    src, stage = staged_agent
    (src / ".git").mkdir()
    (src / "install-stamp.json").unlink()
    base = tmp_path / "base" / ".hermes"
    (base / "profiles" / "coder").mkdir(parents=True)
    places = [base / "hermes-agent", base / "profiles" / "coder" / "hermes-agent", tmp_path / "opt" / "hermes"]
    for env in ({"HERMES_HOME": str(base)}, {"HERMES_HOME": str(base / "profiles" / "coder")},
                {"HERMES_HOME": None, "HOME": str(tmp_path / "user")}, {"HERMES_BASE_HOME": str(base)}):
        for place in places:
            place.parent.mkdir(parents=True, exist_ok=True)
            src.rename(place)
            assert _run_selection(tmp_path, str(place), str(stage), None, env) == str(stage), (env, place)
            src = place
    # HERMES_INSTALL_ROOT with no stamp there: undecidable, so the stage too.
    assert _run_selection(tmp_path, str(src), str(stage), None, {"HERMES_INSTALL_ROOT": str(tmp_path)}) == str(stage)
    # A dangling stamp symlink is a missing stamp to the Agent (adoption replaces it).
    (src / "install-stamp.json").symlink_to(tmp_path / "nowhere.json")
    assert _run_selection(tmp_path, str(src), str(stage), None) == str(stage)


def _execute_only_profiles(tmp_path, src):
    """<base>/profiles is mode 0111: WebUI still enters the active profile, a listing cannot."""
    base = tmp_path / "b" / ".hermes"
    (base / "profiles" / "coder").mkdir(parents=True)
    (base / "active_profile").write_text("coder", encoding="utf-8")
    mount = base / "profiles" / "coder" / "hermes-agent"
    src.rename(mount)
    (base / "profiles").chmod(0o111)
    return mount, {"HERMES_HOME": str(base)}, lambda: (base / "profiles").chmod(0o755)


def _symlinked_profiles(tmp_path, src):
    """<B>/profiles -> <C>/profiles: WebUI publishes the canonical <C>/profiles/coder, and a
    later restart unwraps that to the base <C>, whose hermes-agent is the mount."""
    b, c = tmp_path / "b" / ".hermes", tmp_path / "c" / ".hermes"
    (c / "profiles" / "coder").mkdir(parents=True)
    b.mkdir(parents=True)
    (b / "profiles").symlink_to(c / "profiles")
    (b / "active_profile").write_text("coder", encoding="utf-8")
    mount = c / "hermes-agent"
    src.rename(mount)
    return mount, {"HERMES_HOME": str(b)}, lambda: None


def _literal_variable_home(tmp_path, src):
    """HERMES_HOME holds a literal $DATA: WebUI publishes it unexpanded, the restarted Agent
    expands it, so its profile home is the expanded one, under which the mount sits."""
    literal, expanded = tmp_path / "t" / "$DATA", tmp_path / "t" / "actual"
    for home in (literal, expanded):
        (home / "profiles" / "coder").mkdir(parents=True)
        (home / "active_profile").write_text("coder", encoding="utf-8")
    mount = expanded / "profiles" / "coder" / "hermes-agent"
    src.rename(mount)
    return mount, {"HERMES_HOME": str(literal), "DATA": "actual"}, lambda: None


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root traverses 0111 directories")
@pytest.mark.parametrize("shape", [_execute_only_profiles, _symlinked_profiles, _literal_variable_home],
                         ids=["execute-only-profiles", "symlinked-profiles", "literal-variable-home"])
def test_homes_a_list_would_miss_still_get_the_stage(tmp_path, staged_agent, shape):
    """#8594 gate shapes where a list of reachable homes kept the mount and the Agent later
    adopted it after a profile switch plus an os.execv restart."""
    src, stage = staged_agent
    (src / ".git").mkdir()
    (src / "install-stamp.json").unlink()
    mount, env, cleanup = shape(tmp_path, src)
    try:
        assert _run_selection(tmp_path, str(mount), str(stage), None, env) == str(stage)
    finally:
        cleanup()


def test_a_profile_env_cannot_move_the_install_root(tmp_path, monkeypatch):
    """#8594 gate: the stamp the Agent reads must stay where docker_init read it, so a
    profile .env cannot set HERMES_INSTALL_ROOT before an in-app update restart."""
    import api.profiles as profiles
    assert "HERMES_INSTALL_ROOT" in profiles._PROTECTED_ENV_KEYS
    monkeypatch.delenv("HERMES_INSTALL_ROOT", raising=False)
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set(), raising=False)
    home = tmp_path / "profiles" / "coder"
    home.mkdir(parents=True)
    (home / ".env").write_text(f"HERMES_INSTALL_ROOT={tmp_path / 'elsewhere'}\nSOME_PROFILE_KEY=1\n", encoding="utf-8")
    profiles._reload_dotenv(home)
    try:
        assert "HERMES_INSTALL_ROOT" not in os.environ
        assert os.environ.get("SOME_PROFILE_KEY") == "1"
    finally:
        profiles._reload_dotenv(tmp_path / "nowhere")
        os.environ.pop("SOME_PROFILE_KEY", None)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes 0555 directories")
def test_a_read_only_stampless_mount_at_a_hermes_home_keeps_the_mount(tmp_path, staged_agent):
    """Adoption must write the stamp; a read-only checkout is never adopted (#8593)."""
    src, stage = staged_agent
    (src / ".git").mkdir()
    (src / "install-stamp.json").unlink()
    home = tmp_path / "hh"
    blessed = home / "hermes-agent"
    blessed.parent.mkdir(parents=True)
    src.rename(blessed)
    blessed.chmod(0o555)
    try:
        assert _run_selection(tmp_path, str(blessed), str(stage), None, {"HERMES_HOME": str(home)}) == "<unset>"
    finally:
        blessed.chmod(0o755)


def test_the_selection_never_sets_the_lazy_install_policy():
    """#8594 gate: HERMES_DISABLE_LAZY_INSTALLS also blocks PM-provisioned MCP launchers and
    post-update drift repair, and the Agent's own image does not set it."""
    function = _select_staged_agent_function()
    assert "export HERMES_DISABLE_LAZY_INSTALLS" not in function
    assert "export HERMES_DISABLE_LAZY_INSTALLS" not in _docker_init_source()
