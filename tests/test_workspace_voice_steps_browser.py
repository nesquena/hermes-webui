"""Real DOM regressions for voice fallback and tool-detail keyboard handling."""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path


def test_workspace_voice_and_steps_in_real_browser():
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    explicit_module = env.get("PLAYWRIGHT_MODULE")
    if explicit_module is not None:
        assert explicit_module.strip(), "PLAYWRIGHT_MODULE must not be empty"
    node = shutil.which("node")
    if explicit_module is None or node is None:
        # CI installs Python Playwright, whose bundled driver needs no npm install.
        spec = importlib.util.find_spec("playwright")
        assert spec and spec.origin, "Python Playwright is required without an explicit Node driver"
        driver = Path(spec.origin).parent / "driver"
        if explicit_module is None:
            package = driver / "package"
            assert (package / "package.json").is_file(), "Bundled Playwright package was not found"
            env["PLAYWRIGHT_MODULE"] = str(package)
        if node is None:
            bundled_node = driver / ("node.exe" if os.name == "nt" else "node")
            assert bundled_node.is_file(), "Bundled Node executable was not found"
            node = str(bundled_node)
    result = subprocess.run(
        [node, "--test", "tests/workspace_voice_steps_browser.test.cjs"],
        cwd=root, env=env, capture_output=True, text=True, timeout=90,
    )
    print(result.stdout, end="")
    assert result.returncode == 0, result.stdout + result.stderr
