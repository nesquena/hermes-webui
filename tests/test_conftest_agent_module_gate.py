"""The agent-module gate in conftest must agree with how the server imports the Agent.

``requires_agent_modules`` skips a test when ``conftest.AGENT_MODULES_AVAILABLE`` is False. The server runs with
the Agent checkout as its working directory (and ``api.config`` appends the checkout to ``sys.path``), so the
gate's answer has to match an import made that way. It once didn't: the check ran before the checkout was on
``sys.path``, the venv's editable-install finder knew only the top-level modules present when it was installed,
``cron.jobs`` imports a later one (``hermes_yaml``), and every agent-module test skipped on a host where the server
imported it fine.
"""
import subprocess
import sys

import pytest

import tests.conftest as conftest

_GATE_MODULES = ("cron.jobs", "tools.skills_tool")


def _server_view_imports(agent_dir):
    """Whether the gate's modules import in a fresh interpreter whose working directory is the checkout."""
    code = "import importlib\nfor m in %r:\n    importlib.import_module(m)\n" % (_GATE_MODULES,)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(agent_dir),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode == 0, result.stderr[-400:]


def test_agent_module_gate_agrees_with_the_servers_import_of_the_agent():
    if not conftest.HERMES_AGENT:
        pytest.skip("no hermes-agent checkout on this host")
    imports, stderr = _server_view_imports(conftest.HERMES_AGENT)
    assert conftest.AGENT_MODULES_AVAILABLE == imports, (
        f"conftest says agent modules importable={conftest.AGENT_MODULES_AVAILABLE}, but importing "
        f"{_GATE_MODULES} from the checkout gives {imports}: {stderr}"
    )
