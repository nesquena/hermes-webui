"""Default-running regressions for the opt-in Agent integration harness."""
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parent.parent
MARKERLESS_TEST = (
    "tests/test_reset_lineage_agent_integration.py::"
    "test_real_agent_markerless_branch_survives_later_reset_boundary"
)


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """Harness outcome checks do not need the shared WebUI HTTP server."""


@pytest.mark.parametrize(
    "failure, diagnostic",
    [
        ("agent_dir", "hermes_state.py"),
        ("missing_python", "HERMES_WEBUI_PYTHON"),
        ("bad_python", "HERMES_WEBUI_PYTHON"),
        ("agent_import", "No module named 'missing_reset_lineage_dependency'"),
        ("probe_output", "invalid JSON"),
    ],
)
def test_opted_in_fixture_failure_is_error_not_xfail(tmp_path, failure, diagnostic):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    if failure != "agent_dir":
        # Deliberately broken imports/output test the harness only. No real
        # Agent checkout or Agent dependency installation is needed in CI.
        source = (
            "import os\n"
            "assert os.environ.get('HERMES_DISABLE_LAZY_INSTALLS') == '1', 'lazy installs not disabled'\n"
            "import missing_reset_lineage_dependency\n"
        )
        if failure == "probe_output":
            source = "print('not probe JSON')\nraise SystemExit(0)\n"
        (agent_dir / "hermes_state.py").write_text(source, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "HERMES_HOME": str(home),
        "HERMES_BASE_HOME": str(home),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_CONFIG_PATH": str(home / "config.yaml"),
        "HERMES_WEBUI_STATE_DIR": str(home / "webui"),
        "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
        "HERMES_WEBUI_PYTHON": sys.executable,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    if failure == "missing_python":
        env.pop("HERMES_WEBUI_PYTHON")
    elif failure == "bad_python":
        env["HERMES_WEBUI_PYTHON"] = str(tmp_path / "missing-python")
    report = tmp_path / "result.xml"
    # Reuse the running suite's supported interpreter. Calling test.sh inside
    # the test could create/install a second environment in CI.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", MARKERLESS_TEST,
         "-q", "--tb=short", f"--junitxml={report}"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    cases = ET.parse(report).findall(".//testcase")
    assert len(cases) == 1, output
    assert cases[0].find("error") is not None, output
    assert cases[0].find("skipped") is None, output
    assert diagnostic in output, output
