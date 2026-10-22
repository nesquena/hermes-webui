"""Dependency bootstrap precedes WebUI imports; Agent application imports do not."""

import os
import subprocess
import sys

import pytest

import bootstrap


def test_server_keeps_api_importable_after_agent_hardens_sys_path(tmp_path):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "run_agent.py").write_text("class AIAgent: pass\n", encoding="utf-8")
    (agent_dir / "hermes_bootstrap.py").write_text(
        "import sys\n"
        "sys.path[:] = [path for path in sys.path if path != '']\n",
        encoding="utf-8",
    )
    webui_root = os.path.dirname(os.path.dirname(__file__))
    assert bootstrap._python_can_run_webui_and_agent(sys.executable, agent_dir)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)
    # A script launch starts in the Agent directory but puts the WebUI script
    # directory, not cwd, on sys.path. The probe's PYTHONPATH is not inherited.
    script = (
        "import importlib.util, sys\n"
        "sys.path[0] = " + repr(webui_root) + "\n"
        "spec = importlib.util.spec_from_file_location('server', "
        + repr(os.path.join(webui_root, "server.py")) + ")\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "assert 'hermes_bootstrap' in sys.modules\n"
        "import run_agent\n"
        "assert sys.modules['run_agent'].__file__ == "
        + repr(str(agent_dir / "run_agent.py")) + "\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=agent_dir, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("agent_source", [None, '"""No-Agent browser fixture."""\n'])
def test_server_starts_without_agent_class(tmp_path, agent_source):
    agent_dir = tmp_path / "no-agent"
    agent_dir.mkdir()
    if agent_source is not None:
        (agent_dir / "run_agent.py").write_text(agent_source, encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)
    webui_root = os.path.dirname(os.path.dirname(__file__))
    # Stop at the first WebUI import: this exercises the real startup seam
    # without involving unrelated imports from any Agent installed on the host.
    script = (
        "import builtins\n"
        "original = builtins.__import__\n"
        "def check(name, *args, **kwargs):\n"
        "    if name == 'api.request_logging':\n"
        "        raise SystemExit(0)\n"
        "    return original(name, *args, **kwargs)\n"
        "builtins.__import__ = check\n"
        "import server\n"
        "raise SystemExit('WebUI imports not reached')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=webui_root, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_server_warns_and_continues_on_bootstrap_internal_import_failure(tmp_path):
    """A broken Agent bootstrap must not abort WebUI startup (master's lazy import tolerated it)."""
    agent_dir = tmp_path / "broken-agent"
    agent_dir.mkdir()
    (agent_dir / "run_agent.py").write_text("class AIAgent: pass\n", encoding="utf-8")
    (agent_dir / "hermes_bootstrap.py").write_text(
        "import deliberately_missing_agent_dependency\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)
    result = subprocess.run(
        [sys.executable, "-c", "import server; print('SERVER_IMPORTED')"],
        cwd=os.path.dirname(os.path.dirname(__file__)), env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "SERVER_IMPORTED" in result.stdout
    assert "Hermes Agent dependency activation failed" in result.stderr
    assert "deliberately_missing_agent_dependency" in result.stderr


def test_legacy_agent_without_bootstrap_is_not_put_at_front_of_sys_path(tmp_path):
    """Older Agents keep api.config's append-at-END placement (pip -t . shadowing guard)."""
    agent_dir = tmp_path / "legacy-agent"
    agent_dir.mkdir()
    (agent_dir / "run_agent.py").write_text("class AIAgent: pass\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)
    script = (
        "import sys, managed_agent_startup as m\n"
        "m.activate_managed_agent()\n"
        "print('INDEX', sys.path.index(sys.argv[1]) if sys.argv[1] in sys.path else -1)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(agent_dir)],
        cwd=os.path.dirname(os.path.dirname(__file__)), env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "INDEX -1" in result.stdout


def test_server_propagates_bootstrap_system_exit(tmp_path):
    """The Agent's own relaunch/repair exit (SystemExit) is not swallowed."""
    agent_dir = tmp_path / "exiting-agent"
    agent_dir.mkdir()
    (agent_dir / "run_agent.py").write_text("class AIAgent: pass\n", encoding="utf-8")
    (agent_dir / "hermes_bootstrap.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)
    result = subprocess.run(
        [sys.executable, "-c", "import server; print('SERVER_IMPORTED')"],
        cwd=os.path.dirname(os.path.dirname(__file__)), env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 7
    assert "SERVER_IMPORTED" not in result.stdout


@pytest.mark.parametrize("with_bootstrap", [False, True])
def test_server_defers_application_import_until_profile_selection(tmp_path, with_bootstrap):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "run_agent.py").write_text(
        "raise AssertionError('premature application import')\n", encoding="utf-8",
    )
    if with_bootstrap:
        (agent_dir / "hermes_bootstrap.py").write_text(
            "import sys\nassert not any(n.startswith('api.') for n in sys.modules)\n",
            encoding="utf-8",
        )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)
    script = (
        "import builtins, sys\n"
        "original = builtins.__import__\n"
        "def check(name, *args, **kwargs):\n"
        "    if name == 'api.request_logging':\n"
        "        assert 'run_agent' not in sys.modules\n"
        f"        assert ('hermes_bootstrap' in sys.modules) is {with_bootstrap!r}\n"
        "        raise SystemExit(0)\n"
        "    return original(name, *args, **kwargs)\n"
        "builtins.__import__ = check\n"
        "import server\n"
        "raise SystemExit('WebUI imports not reached')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_bootstrap_reexec_keeps_webui_importable(tmp_path):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "run_agent.py").write_text(
        "raise AssertionError('premature application import')\n", encoding="utf-8",
    )
    webui_root = os.path.dirname(os.path.dirname(__file__))
    server = os.path.join(webui_root, "server.py")
    # Match Agent's script relaunch: isolated Python, only the Agent root added,
    # and run_path(server.py), which does not add the script's directory.
    check = (
        "import builtins, os, sys, runpy\n"
        f"sys.path.insert(0, {str(agent_dir)!r})\n"
        "original = builtins.__import__\n"
        "def checked(name, *args, **kwargs):\n"
        "    if name == 'api.request_logging':\n"
        "        assert os.environ['TEST_BOOTSTRAP_REEXEC'] == '1'\n"
        "        assert 'hermes_bootstrap' in sys.modules\n"
        "        assert 'run_agent' not in sys.modules\n"
        "        raise SystemExit(0)\n"
        "    return original(name, *args, **kwargs)\n"
        "builtins.__import__ = checked\n"
        f"sys.argv = [{server!r}]\n"
        f"runpy.run_path({server!r}, run_name='__main__')\n"
    )
    (agent_dir / "hermes_bootstrap.py").write_text(
        "import os, sys\n"
        "if not os.environ.get('TEST_BOOTSTRAP_REEXEC'):\n"
        "    os.environ['TEST_BOOTSTRAP_REEXEC'] = '1'\n"
        f"    os.execv(sys.executable, [sys.executable, '-I', '-c', {check!r}])\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("TEST_BOOTSTRAP_REEXEC", None)
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)
    result = subprocess.run(
        [sys.executable, server], cwd=agent_dir,
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
