"""The Agent's process-wide TLS trust store is installed before any WebUI import.

truststore patches ``ssl.SSLContext`` process-wide. A urllib3/botocore context built
before that patch recurses forever on its next use, which is how a long-running WebUI
lost its Bedrock model list ("maximum recursion depth exceeded") once the Agent
installed the trust store lazily on the first chat turn.
"""

import os
import subprocess
import sys

WEBUI_ROOT = os.path.dirname(os.path.dirname(__file__))

# Records whether any WebUI module was already imported when the Agent installed trust.
SSL_VERIFY = (
    "import sys\n"
    "def install_truststore():\n"
    "    early = not any(name == 'api' or name.startswith('api.') for name in sys.modules)\n"
    "    print('TRUST_INSTALLED', 'early' if early else 'late', flush=True)\n"
    "    return True\n"
)


def _agent(tmp_path, *, ssl_verify=SSL_VERIFY, bootstrap="pass\n"):
    agent_dir = tmp_path / "agent-checkout"
    (agent_dir / "agent").mkdir(parents=True)
    (agent_dir / "run_agent.py").write_text("class AIAgent: pass\n", encoding="utf-8")
    (agent_dir / "hermes_bootstrap.py").write_text(bootstrap, encoding="utf-8")
    (agent_dir / "agent" / "__init__.py").write_text("", encoding="utf-8")
    if ssl_verify is not None:
        (agent_dir / "agent" / "ssl_verify.py").write_text(ssl_verify, encoding="utf-8")
    return agent_dir


def _start_server(agent_dir):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)
    # Stop at the first WebUI import after activation: the seam under test.
    script = (
        "import builtins\n"
        "original = builtins.__import__\n"
        "def check(name, *args, **kwargs):\n"
        "    if name == 'api.request_logging':\n"
        "        print('WEBUI_IMPORTS_REACHED', flush=True)\n"
        "        raise SystemExit(0)\n"
        "    return original(name, *args, **kwargs)\n"
        "builtins.__import__ = check\n"
        "import server\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script], cwd=WEBUI_ROOT, env=env,
        capture_output=True, text=True, timeout=30,
    )


def test_agent_trust_store_is_installed_before_webui_imports(tmp_path):
    result = _start_server(_agent(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "TRUST_INSTALLED early" in result.stdout
    assert result.stdout.index("TRUST_INSTALLED") < result.stdout.index("WEBUI_IMPORTS_REACHED")


def test_agent_without_ssl_verify_still_starts(tmp_path):
    """Agents that predate agent.ssl_verify have no trust store to install."""
    result = _start_server(_agent(tmp_path, ssl_verify=None))
    assert result.returncode == 0, result.stderr
    assert "WEBUI_IMPORTS_REACHED" in result.stdout
    assert "TRUST_INSTALLED" not in result.stdout


def test_broken_ssl_verify_warns_and_continues(tmp_path):
    """A broken trust module must not stop WebUI from starting (same policy as bootstrap)."""
    result = _start_server(_agent(tmp_path, ssl_verify="import deliberately_missing_tls_dependency\n"))
    assert result.returncode == 0, result.stderr
    assert "WEBUI_IMPORTS_REACHED" in result.stdout
    assert "deliberately_missing_tls_dependency" in result.stderr
