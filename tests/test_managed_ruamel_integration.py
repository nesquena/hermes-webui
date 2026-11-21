"""Real server startup activates Agent dependencies before WebUI YAML imports."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(
    not (ROOT / "api" / "yaml_compat.py").is_file(),
    reason="requires companion YAML compatibility change (#7875)",
)
def test_server_activates_agent_before_ruamel_only_webui_config(tmp_path):
    agent_dir = tmp_path / "agent"
    ruamel_dir = agent_dir / "managed" / "ruamel"
    ruamel_dir.mkdir(parents=True)
    (agent_dir / "run_agent.py").write_text("class AIAgent: pass\n", encoding="utf-8")
    (agent_dir / "hermes_bootstrap.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent / 'managed'))\n",
        encoding="utf-8",
    )
    (ruamel_dir / "__init__.py").write_text("", encoding="utf-8")
    (ruamel_dir / "yaml.py").write_text(
        "class YAML:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        from types import SimpleNamespace\n"
        "        self.representer = SimpleNamespace()\n"
        "    def load(self, stream): return {'integration_marker': 'ruamel'}\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text("integration_marker: ruamel\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)
    env["HERMES_HOME"] = str(tmp_path / "home")
    env["HERMES_BASE_HOME"] = str(tmp_path / "home")
    env["HERMES_CONFIG_PATH"] = str(config)
    env["HERMES_WEBUI_STATE_DIR"] = str(tmp_path / "state")
    script = (
        "import builtins, sys\n"
        "original = builtins.__import__\n"
        "def no_pyyaml(name, *args, **kwargs):\n"
        "    if name == 'yaml' or name.startswith('yaml.'):\n"
        "        raise ImportError('PyYAML disabled for integration test')\n"
        "    return original(name, *args, **kwargs)\n"
        "builtins.__import__ = no_pyyaml\n"
        "import server\n"
        "from api import yaml_compat\n"
        "from api.onboarding import _load_yaml_config\n"
        "from pathlib import Path\n"
        "assert 'hermes_bootstrap' in sys.modules\n"
        "import run_agent\n"
        "assert sys.modules['run_agent'].__file__ == sys.argv[2]\n"
        "assert yaml_compat.BACKEND == 'ruamel'\n"
        "assert _load_yaml_config(Path(sys.argv[1])) == {'integration_marker': 'ruamel'}\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(config), str(agent_dir / "run_agent.py")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
