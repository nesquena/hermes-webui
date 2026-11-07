"""Keep bounded scene navigation and the real SSE handoff in the CI gate."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.timeout(240)
@pytest.mark.parametrize("script", ["browser_transparent_scene_pages.py", "browser_scene_budget_ownership.py"])
def test_scene_pages_and_live_settle(script):
    pytest.importorskip("playwright.sync_api")
    env = dict(os.environ)
    # A diagnostic selector must never silently remove the lifecycle assertion
    # from the pytest/CI contract. BROWSERS selects installed engines in CI.
    for key in ("SCENE_SKIP_LIFECYCLE", "SCENE_ENGINE", "SCENE_VIEWPORT"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests" / script)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=220,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIPPED" not in result.stdout
    if script == "browser_transparent_scene_pages.py":
        assert "lifecycle PASS" in result.stdout
    else:
        assert "bounded remount/prepend" in result.stdout
