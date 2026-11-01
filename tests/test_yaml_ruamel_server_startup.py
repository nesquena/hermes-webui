"""server.py must activate the Agent before api.yaml_compat picks a YAML backend.

Combined contract (#7875 YAML fallback + #7876 activation order): the server imports
the Agent's ``hermes_bootstrap`` dependency layer before any WebUI module needs YAML,
and must NOT import ``run_agent`` at that point (#7886: importing Agent application
modules before the named profile is selected disables context-local skill homes).
"""

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent

# PyYAML is always blocked; ruamel.yaml stays invisible until the Agent's bootstrap layer
# is imported, mirroring a managed runtime that exposes its dependencies on activation.
_GATE = (
    "import builtins, sys\n"
    "_orig = builtins.__import__\n"
    "_seen = {}\n"
    "def _gate(name, *a, **k):\n"
    "    top = name.split('.')[0]\n"
    "    if top == 'yaml' or (top == 'ruamel' and 'hermes_bootstrap' not in sys.modules):\n"
    "        raise ImportError(f'{name} not activated')\n"
    "    if top == 'ruamel' and 'first' not in _seen:\n"
    "        _seen['first'] = 'run_agent' in sys.modules\n"
    "    return _orig(name, *a, **k)\n"
    "builtins.__import__ = _gate\n"
)


def _fixture_agent(tmp_path):
    agent_dir = tmp_path / "agent"
    ruamel_dir = agent_dir / "managed" / "ruamel"
    ruamel_dir.mkdir(parents=True)
    # Importing the fixture bootstrap layer exposes its managed dir, which ships a ruamel stub.
    (agent_dir / "hermes_bootstrap.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent / 'managed'))\n",
        encoding="utf-8",
    )
    (agent_dir / "run_agent.py").write_text("class AIAgent: pass\n", encoding="utf-8")
    (ruamel_dir / "__init__.py").write_text("", encoding="utf-8")
    (ruamel_dir / "yaml.py").write_text(
        "from types import SimpleNamespace\n"
        "class YAML:\n"
        "    def __init__(self, *a, **k): self.representer = SimpleNamespace()\n"
        "    def load(self, stream): return {'backend': 'managed-ruamel'}\n",
        encoding="utf-8",
    )
    ruamel_resolver = ruamel_dir / "resolver.py"
    ruamel_resolver.write_text("class VersionedResolver: pass\n", encoding="utf-8")
    return agent_dir


def _env(tmp_path, agent_dir):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HERMES_WEBUI_") and k != "PYTHONPATH"}
    env.update(
        HERMES_WEBUI_AGENT_DIR=str(agent_dir),
        HERMES_HOME=str(tmp_path / "home"),
        HERMES_BASE_HOME=str(tmp_path / "home"),
        HERMES_WEBUI_STATE_DIR=str(tmp_path / "state"),
    )
    return env


def test_server_import_activates_agent_before_ruamel_only_yaml(tmp_path):
    agent_dir = _fixture_agent(tmp_path)
    script = _GATE + (
        "import server\n"
        "from api import yaml_compat\n"
        "assert sys.modules['hermes_bootstrap'].__file__ == sys.argv[1], sys.modules['hermes_bootstrap'].__file__\n"
        "assert _seen.get('first') is False, 'run_agent was imported before the YAML backend (#7886)'\n"
        "assert yaml_compat.BACKEND == 'ruamel'\n"
        "assert yaml_compat.safe_load('a: 1') == {'backend': 'managed-ruamel'}\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(agent_dir / "hermes_bootstrap.py")],
        cwd=ROOT, env=_env(tmp_path, agent_dir), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_server_import_fails_without_activation(tmp_path):
    """Negative control: without the bootstrap layer, a ruamel-only runtime cannot start."""
    agent_dir = _fixture_agent(tmp_path)
    (agent_dir / "hermes_bootstrap.py").unlink()
    script = _GATE + "import server\n"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT, env=_env(tmp_path, agent_dir), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "not activated" in result.stderr
