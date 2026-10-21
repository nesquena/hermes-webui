"""Real installed Agent bootstrap must leave profile-sensitive imports deferred.

Uses an isolated PM selection, not the live install's facts or recovery state.
Run with HERMES_WEBUI_AGENT_DIR and HERMES_WEBUI_PYTHON for a managed install.
No provider calls or server sockets are needed.
"""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


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
    # The installed Agent's first-party YAML (hermes_yaml) needs ruamel.yaml; an
    # interpreter without it cannot import run_agent at all, so the scenario this
    # test models (a working managed runtime) does not exist there.
    probe = subprocess.run([python, "-c", "import ruamel.yaml"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("test interpreter cannot import ruamel.yaml (required by the installed Agent)")
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

            def deny_external_effects(event, args):
                if event == "subprocess.Popen":
                    command = args[1]
                    if (isinstance(command, list) and Path(command[0]).name == "git"
                            and command[1] in {"rev-parse", "describe", "status", "diff-index", "diff"}):
                        return  # Agent/WebUI version metadata, never an installer.
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
            assert manager.SKILLS_DIR == manager._SKILLS_DIR_AT_IMPORT == home / "skills"
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
            print("real PM activation; named-profile skills; two turn scopes + catalog progressed")
        '''), sysconfig.get_path("purelib")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "two turn scopes + catalog progressed" in result.stdout
