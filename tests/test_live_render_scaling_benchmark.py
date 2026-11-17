"""Deterministic regression tests for the live-turn render benchmark."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "bench" / "live_render_scaling.py"
SPEC = importlib.util.spec_from_file_location("live_render_scaling", SCRIPT)
assert SPEC is not None
benchmark = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)
NODE = shutil.which("node")


def _measure_event_costs(costs: list[int]) -> dict:
    """Run the production JS loop with synchronous, controlled browser costs."""
    if NODE is None:
        pytest.skip("node not on PATH")
    harness = """
const performance = {now: () => now};
let now = 0;
const costs = COSTS;
let eventIndex = 0;
let activeCost = 0;
const listeners = {};
const source = {
  url: '/api/chat/stream?fixture=1', readyState: 1,
  emit(type, data, id) {
    activeCost = costs[eventIndex++];
    window._renderLiveAnchorActivitySceneForStream();
  },
};
const fixtureSources = [source];
const window = globalThis;
window.__longtasks = [];
window.__longtaskStart = 0;
window._renderLiveAnchorActivitySceneForStream = () => { now += activeCost; };
const requestAnimationFrame = callback => { throw new Error('unexpected queued rAF'); };
const setTimeout = callback => { callback(); return 1; };
const document = {querySelectorAll: () => []};
const chatActivityMode = () => 'compact_worklog';
const loadSession = async () => {};
const run = RUN;
run({events: costs.length, checkpoints: COST_CHECKPOINTS})
  .then(result => console.log(JSON.stringify(result.rows)))
  .catch(error => { console.error(error); process.exitCode = 1; });
"""
    harness = (
        harness.replace("COSTS", json.dumps(costs))
        .replace("COST_CHECKPOINTS", json.dumps([4, 8]))
        .replace("RUN", benchmark.RUN, 1)
    )
    result = subprocess.run(
        [NODE, "-e", harness], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_latest_interval_uses_only_current_interval_samples():
    rows = _measure_event_costs([1, 1, 1, 1, 9, 9, 9, 9])

    assert rows[0]["latest_interval"]["median_per_render_ms"] == 1
    assert rows[0]["latest_interval"]["median_by_event_type_ms"] == {
        "reasoning": 1, "tool": 1, "tool_complete": 1, "token": 1,
    }
    latest = rows[1]["latest_interval"]
    assert latest["median_per_render_ms"] == 9
    assert latest["render_count"] == 4
    assert latest["median_by_event_type_ms"] == {
        "reasoning": 9, "tool": 9, "tool_complete": 9, "token": 9,
    }
    assert rows[1]["median_per_render_ms"] == 5
    assert rows[1]["cumulative_by_event_type_ms"] == {
        "reasoning": 5, "tool": 5, "tool_complete": 5, "token": 5,
    }


def test_uniform_interval_and_cumulative_samples_agree():
    rows = _measure_event_costs([9] * 8)
    latest = rows[1]["latest_interval"]
    assert latest["median_per_render_ms"] == 9
    assert latest["median_by_event_type_ms"] == {
        "reasoning": 9, "tool": 9, "tool_complete": 9, "token": 9,
    }
    assert rows[1]["median_per_render_ms"] == 9
    assert rows[1]["cumulative_by_event_type_ms"] == latest["median_by_event_type_ms"]


def test_explicit_chrome_path_precedes_discovery(monkeypatch, tmp_path):
    executable = tmp_path / "explicit-chrome"
    executable.touch()
    executable.chmod(0o755)
    monkeypatch.setattr(
        benchmark, "_chrome_candidates", lambda *args: pytest.fail("discovery was used")
    )

    assert benchmark.resolve_chrome_path(executable, platform="darwin") == str(executable)


@pytest.mark.parametrize("executable", [False, True])
def test_explicit_chrome_path_must_be_an_executable_file(
    monkeypatch, tmp_path, executable
):
    path = tmp_path / "chrome"
    path.touch()
    if executable:
        path.chmod(0o755)
        path.unlink()
    monkeypatch.setattr(
        benchmark.shutil, "which", lambda name: pytest.fail("PATH fallback was used")
    )

    with pytest.raises(ValueError, match="--chrome-path"):
        benchmark.resolve_chrome_path(path, platform="linux")


def test_macos_candidate_is_discovered(monkeypatch, tmp_path):
    executable = tmp_path / "Google Chrome"
    executable.touch()
    executable.chmod(0o755)
    monkeypatch.setattr(
        benchmark,
        "_chrome_candidates",
        lambda platform, release: [str(executable)],
    )
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: None)

    assert benchmark.resolve_chrome_path(platform="darwin") == str(executable)


def test_platform_chrome_candidates_cover_macos_linux_and_wsl():
    assert benchmark._chrome_candidates("darwin", "") == [benchmark.MACOS_CHROME]
    linux = benchmark._chrome_candidates("linux", "6.8-generic")
    assert "/usr/bin/google-chrome" in linux
    assert "/usr/bin/chromium" in linux
    wsl = benchmark._chrome_candidates("linux", "6.8-microsoft-standard-WSL2")
    assert wsl[0].endswith("/Google/Chrome/Application/chrome.exe")
    assert linux[0] in wsl


def test_linux_wsl_and_path_chrome_discovery(monkeypatch, tmp_path):
    wsl_executable = tmp_path / "chrome.exe"
    path_executable = tmp_path / "chromium"
    for executable in (wsl_executable, path_executable):
        executable.touch()
        executable.chmod(0o755)
    monkeypatch.setattr(
        benchmark, "_chrome_candidates", lambda platform, release: [str(wsl_executable)]
    )
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: None)
    assert benchmark.resolve_chrome_path(platform="linux") == str(wsl_executable)

    monkeypatch.setattr(benchmark, "_chrome_candidates", lambda platform, release: [])
    monkeypatch.setattr(
        benchmark.shutil,
        "which",
        lambda name: str(path_executable) if name == "chromium" else None,
    )
    assert benchmark.resolve_chrome_path(platform="linux") == str(path_executable)


def test_missing_discovered_chrome_has_clear_error(monkeypatch):
    monkeypatch.setattr(benchmark, "_chrome_candidates", lambda platform, release: [])
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: None)

    with pytest.raises(ValueError, match="--chrome-path"):
        benchmark.resolve_chrome_path(platform="linux")


@pytest.mark.parametrize("missing", [True, False])
def test_invalid_chrome_never_launches_server_or_browser(
    monkeypatch, tmp_path, missing
):
    chrome = tmp_path / "invalid-chrome"
    if missing:
        chrome.touch()
    launches = []
    monkeypatch.setattr(benchmark.subprocess, "Popen", lambda *a, **kw: launches.append(a))
    monkeypatch.setattr(
        sys, "argv", [str(SCRIPT), "--events", "4", "--checkpoints", "4", "--chrome-path", str(chrome)]
    )

    with pytest.raises(SystemExit) as exc_info:
        benchmark.main()

    assert exc_info.value.code == 2
    assert launches == []
