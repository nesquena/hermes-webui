"""Behavioral #8427 regressions against the actual shared helper and renderer.

Node-only helper cases need no dependencies. Optional jsdom checks exercise
render/collapse/re-render, not real-browser layout; the companion HTML fixture
loads the same production JS/CSS for separate native-browser verification.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "fixtures" / "issue8427_tool_anchor.cjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for #8427 behavior tests")


def _run(*args: str) -> dict:
    result = subprocess.run(
        [NODE, str(DRIVER), *args], cwd=ROOT, capture_output=True,
        text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("case", [
    "user-boundary", "user-boundary-through-tool", "nearest-user-boundary",
    "same-turn-through-tool", "same-turn-nearest-prose", "own-prose", "own-text-parts",
    "own-reasoning", "own-reasoning_content", "own-thinking", "own-_reasoning",
    "own-thinking-parts", "first-empty", "null-gap-within-turn",
])
def test_tool_anchor_is_user_bounded_and_preserves_existing_owners(case):
    assert _run("helpers", case) == {"result": "PASS", "checks": 1}


@pytest.fixture(scope="module")
def jsdom_available():
    result = subprocess.run(
        [NODE, "-e", "require.resolve('jsdom')"], cwd=ROOT,
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode:
        pytest.skip("jsdom is optional and is not installed; helper checks still run")


@pytest.mark.parametrize("mode", ["compact_worklog", "transparent_stream", "hide_all_activity"])
@pytest.mark.parametrize("show_thinking", [True, False])
def test_three_turn_final_answers_survive_disclosure_and_rerender(jsdom_available, mode, show_thinking):
    result = _run("renderer", mode, str(show_thinking).lower())
    assert result == {
        "result": "PASS", "mode": mode, "showThinking": show_thinking,
        "finalAnswers": 3, "stages": ["initial", "expanded", "collapsed", "rerender"],
        "layoutChecked": False,
    }
