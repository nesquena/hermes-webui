"""Real installed Agent bootstrap must leave profile-sensitive imports deferred.

Uses an isolated PM selection, not the live install's facts or recovery state.
Run with HERMES_WEBUI_AGENT_DIR and HERMES_WEBUI_PYTHON for a managed install.
No provider calls or server sockets are needed.
"""

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
_MISSING_MODULE = re.compile(r"^ModuleNotFoundError: No module named '([^']+)'\s*$")
_TRACEBACK_FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+)')


def _agent_dependency_missing_from_interpreter(stderr, agent_dir):
    """``(module, "file:line")`` when the child died because the installed Agent's own
    source imports a third-party package this interpreter lacks, else ``None``.

    Only the final traceback counts: the failing import must sit in the Agent's own
    source (not WebUI's, not an installed package), and the missing top-level name must
    not be first-party to the Agent or WebUI. A broken WebUI import, a renamed Agent
    module or any other startup failure therefore still fails the test.
    """
    lines = (stderr or "").rstrip().splitlines()
    match = _MISSING_MODULE.match(lines[-1]) if lines else None
    frames = [frame for frame in map(_TRACEBACK_FRAME.match, lines) if frame]
    if not match or not frames:
        return None
    path, line = Path(frames[-1].group(1)).resolve(), frames[-1].group(2)
    agent = Path(agent_dir).resolve()
    if not path.is_relative_to(agent) or {"site-packages", "dist-packages"} & set(path.parts):
        return None
    top = match.group(1).split(".")[0]
    if any((root / top).exists() or (root / f"{top}.py").exists() for root in (agent, ROOT)):
        return None
    return match.group(1), f"{path.relative_to(agent)}:{line}"


def test_real_managed_bootstrap_preserves_named_profile_concurrency(tmp_path):
    agent_dir = os.environ.get("HERMES_WEBUI_AGENT_DIR")
    if not agent_dir:
        spec = importlib.util.find_spec("hermes_constants")
        if spec is None:
            pytest.skip("hermes-agent not installed")
        agent_dir = str(Path(spec.origin).parent)
    if not (Path(agent_dir) / "pm" / "environments.py").is_file():
        pytest.skip("requires a package-managed Hermes Agent")
    python = os.environ.get("HERMES_WEBUI_PYTHON", sys.executable)
    base = tmp_path / "base"
    home = base / "profiles" / "rocky"
    skill = home / "skills" / "named-only"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: named-only\ndescription: startup regression\n---\nIsolated skill.\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    (base / "active_profile").write_text("rocky", encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "HERMES_HOME": str(base),
        "HERMES_BASE_HOME": str(base),
        "HERMES_WEBUI_STATE_DIR": str(tmp_path / "webui"),
        "HERMES_WEBUI_AGENT_DIR": agent_dir,
        "HERMES_RUNTIME_DIR": str(tmp_path / "runtime"),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        # Agent's supported test guard prevents source-recovery writes.
        "PYTEST_CURRENT_TEST": "isolated managed startup",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join([str(ROOT), agent_dir]),
    }
    if "TMPDIR" in os.environ:
        env["TMPDIR"] = os.environ["TMPDIR"]
    result = subprocess.run(
        [python, "-c", textwrap.dedent('''
            import importlib, importlib.util, json, os, sys, sysconfig, threading, types
            from pathlib import Path

            def _read_only_git(command):
                # Read-only Agent/WebUI version metadata, never an installer.
                # hermes_cli/version_info.py also asks for the branch, commit date,
                # tags and distance, and runs `git --no-optional-locks status`.
                # Only the two global options those calls use: `-c key=value` can make a
                # read run a command (core.fsmonitor), and `--output` makes one write a file.
                rest = [str(part) for part in command[1:]]
                while rest and rest[0].startswith("-"):  # global options
                    option = rest.pop(0)
                    if option == "-C" and rest:
                        rest.pop(0)
                    elif option != "--no-optional-locks":
                        return False
                if not rest:
                    return False
                subcommand, options = rest[0], rest[1:]
                if any(o.startswith("--output") for o in options):
                    return False
                if subcommand == "branch":
                    return options == ["--show-current"]
                if subcommand == "tag":
                    return "--list" in options or "-l" in options
                return subcommand in {
                    "rev-parse", "describe", "status", "diff-index", "diff",
                    "log", "show", "rev-list"}

            def deny_external_effects(event, args):
                if event == "subprocess.Popen":
                    command = args[1]
                    if (isinstance(command, list) and Path(command[0]).name == "git"
                            and _read_only_git(command)):
                        return
                if event in {"socket.connect", "socket.bind", "subprocess.Popen", "os.system", "os.exec"}:
                    raise RuntimeError("startup probe forbids external effects: " + event)
            sys.addaudithook(deny_external_effects)
            base = Path(os.environ["HERMES_HOME"])
            home = base / "profiles" / "rocky"
            source = Path(os.environ["HERMES_WEBUI_AGENT_DIR"])
            # These PM layout APIs are stdlib-only and do not activate/install.
            from pm.environments import install_state_dir, runtime_facts_path, site_packages
            generation = install_state_dir(source) / "environments" / "test" / "venv"
            generation.mkdir(parents=True)
            (generation / "pyvenv.cfg").write_text(
                "version = " + sys.version.split()[0] + "\\n", encoding="utf-8")
            selected = site_packages(generation)
            selected.mkdir(parents=True)
            # Reuse installed packages read-only, plus WebUI's test dependencies
            # (notably PyYAML, whose ruamel fallback is a separate change).
            (selected / "dependencies.pth").write_text(
                sysconfig.get_path("purelib") + "\\n" + sys.argv[1] + "\\n", encoding="utf-8")
            (selected / "managed_startup_marker.py").write_text("activated = True\\n", encoding="utf-8")
            runtime_facts_path(source).write_text(json.dumps({
                "packages": {"venv": {"environment": str(generation)}}}), encoding="utf-8")
            assert importlib.util.find_spec("managed_startup_marker") is None

            # Do not read the checkout's project .env or invoke secret helpers.
            # Bootstrap/PM activation, config, Agent and skills are otherwise real.
            env_loader = types.ModuleType("hermes_cli.env_loader")
            env_loader.load_hermes_dotenv = lambda **kwargs: []
            sys.modules[env_loader.__name__] = env_loader
            # Importing the real server exercises its bootstrap/config ordering
            # without binding a socket (serve_forever is under the main guard).
            import server
            import managed_startup_marker
            assert managed_startup_marker.activated
            assert "hermes_bootstrap" in sys.modules
            assert str(selected) in sys.path

            import api.config as config
            from api import profiles
            from run_agent import AIAgent
            import hermes_constants as hc
            import tools.skills_tool as skills
            import tools.skill_manager_tool as manager
            assert AIAgent.__module__ == "run_agent"
            assert config._active_profile_home() == home
            assert os.environ["HERMES_HOME"] == str(home)
            assert skills.SKILLS_DIR == skills._SKILLS_DIR_AT_IMPORT == home / "skills", (
                "Agent skills were imported before WebUI selected the named profile")
            if hasattr(manager, "SKILLS_DIR"):
                assert manager.SKILLS_DIR == manager._SKILLS_DIR_AT_IMPORT == home / "skills"
                manager_shape = "own SKILLS_DIR"
            else:
                # Agents with 17b953d8b7cd: the manager resolves through skills_tool.
                assert manager._skills_dir is skills._skills_dir
                assert not any(hasattr(manager, a) for a in ("HERMES_HOME", "_SKILLS_DIR_AT_IMPORT"))
                manager_shape = "shared skills_tool resolver"
            assert profiles._skill_modules_support_profile_home(home), "startup disabled dynamic skill resolution"

            from api import streaming
            # Fail immediately if any same-profile scope chooses the legacy
            # whole-turn lock; this is a sentinel, not a replacement algorithm.
            class NoStaticWait:
                def acquire(self, *args, **kwargs):
                    raise AssertionError("entered static whole-turn wait")
                def release(self):
                    raise AssertionError("released unused static lock")
            profiles._SKILL_HOME_MODULE_PATCH_LOCK = NoStaticWait()
            barrier = threading.Barrier(3, timeout=10)
            errors = []
            def worker(purpose):
                try:
                    profiles.set_request_profile("rocky")
                    scope = (
                        profiles.profile_env_for_active_request("model catalog")
                        if purpose == "model catalog"
                        else profiles.profile_env_for_background_worker("rocky", purpose)
                    )
                    with scope:
                        # Exercise streaming's real context-home seam and its
                        # dynamic capability predicate without starting a turn.
                        ctx = streaming._set_streaming_hermes_home_override(str(home))
                        try:
                            assert ctx[2]
                            assert profiles._skill_modules_support_profile_home(home)
                            assert hc.get_hermes_home() == home
                            assert skills._skills_dir() == manager._skills_dir() == home / "skills"
                            payload = skills.skills_list()
                            payload = json.loads(payload) if isinstance(payload, str) else payload
                            assert "named-only" in {s["name"] for s in payload["skills"]}
                            barrier.wait()
                            barrier.wait()
                        finally:
                            streaming._reset_streaming_hermes_home_override(*ctx)
                except BaseException as exc:
                    errors.append(repr(exc))
                    barrier.abort()
                finally:
                    profiles.clear_request_profile()
            threads = [threading.Thread(target=worker, args=(purpose,), daemon=True)
                       for purpose in ("turn one", "turn two", "model catalog")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
            assert not any(thread.is_alive() for thread in threads), "same-profile scopes stalled"
            assert not errors, errors
            assert hc.get_hermes_home_override() is None
            assert hasattr(manager, "SKILLS_DIR") == (manager_shape == "own SKILLS_DIR"), "scopes changed manager globals"
            print("manager shape:", manager_shape)
            print("real PM activation; named-profile skills; two turn scopes + catalog progressed")
        '''), sysconfig.get_path("purelib")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        missing = _agent_dependency_missing_from_interpreter(result.stderr, agent_dir)
        if missing:
            # The installed Agent hard-imports a third-party package this interpreter
            # lacks (psutil, ruamel.yaml, ...), so it cannot import run_agent at all and
            # the scenario this test models, a working managed runtime, does not exist
            # here. The canonical suite runs with the Agent's own interpreter.
            pytest.skip(f"test interpreter lacks {missing[0]!r}, which the installed Agent "
                        f"imports at {missing[1]}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "two turn scopes + catalog progressed" in result.stdout
    print(result.stdout)


def _traceback(*frames, final):
    body = "".join(f'  File "{path}", line {line}, in <module>\n    import x\n' for path, line in frames)
    return "Traceback (most recent call last):\n" + body + final + "\n"


def test_dependency_skip_covers_only_a_third_party_import_in_agent_source(tmp_path):
    agent = tmp_path / "agent"
    (agent / "tools").mkdir(parents=True)
    (agent / "hermes_cli").mkdir()
    agent_file = str(agent / "tools" / "file_sync.py")
    webui_file = str(ROOT / "api" / "config.py")
    installed = str(agent / "venv" / "lib" / "python3.11" / "site-packages" / "pkg" / "__init__.py")
    missing = "ModuleNotFoundError: No module named 'psutil'"

    def check(stderr):
        return _agent_dependency_missing_from_interpreter(stderr, str(agent))

    # The Agent's own source hard-imports a package this interpreter lacks: skip.
    assert check(_traceback((webui_file, 3), (agent_file, 27), final=missing)) == (
        "psutil", "tools/file_sync.py:27")
    assert check(_traceback((agent_file, 9), final="ModuleNotFoundError: No module named 'ruamel.yaml'"))[0] == (
        "ruamel.yaml")
    # The failing import is WebUI's or an installed package's, not the Agent's: fail.
    assert check(_traceback((agent_file, 27), (webui_file, 3), final=missing)) is None
    assert check(_traceback((agent_file, 27), (installed, 1), final=missing)) is None
    # A first-party name (a renamed or deleted Agent/WebUI module) is a real break: fail.
    assert check(_traceback((agent_file, 2), final="ModuleNotFoundError: No module named 'hermes_cli.gone'")) is None
    assert check(_traceback((agent_file, 2), final="ModuleNotFoundError: No module named 'api'")) is None
    # Any other failure, or an earlier missing module that was handled, still fails.
    assert check(_traceback((agent_file, 27), final="ImportError: cannot import name 'x'")) is None
    handled = _traceback((agent_file, 27), final=missing) + "\nDuring handling of the above exception, another exception occurred:\n\n"
    assert check(handled + _traceback((agent_file, 30), final="AssertionError: scopes stalled")) is None
    assert check("") is None and check(None) is None
