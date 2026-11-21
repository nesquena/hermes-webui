"""Deterministic regression tests for the live-turn render benchmark."""
from __future__ import annotations

import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request

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


@pytest.mark.skipif(os.name == "nt", reason="POSIX PATH lookup regression")
@pytest.mark.parametrize("path_form", ["absolute", "relative"])
def test_explicit_chrome_path_launches_the_checked_file(monkeypatch, tmp_path, path_form):
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\nprintf 'checked executable launched\\n'\n", encoding="utf-8")
    executable.chmod(0o755)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.chdir(tmp_path)

    argument = str(executable) if path_form == "absolute" else "./chrome"
    resolved = benchmark.resolve_chrome_path(argument, platform="linux")
    with subprocess.Popen(
        [resolved, "-c", "print('checked executable launched')"],
        cwd=tmp_path,
        env={"PATH": str(empty_path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        stdout, stderr = process.communicate(timeout=30)

    assert process.returncode == 0, stderr
    assert stdout.strip() == "checked executable launched"


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


# ── Readiness must bypass an inherited proxy (2026-11-20 Gate RED) ────────────
# The Gate reproduced a healthy local benchmark server reported unready for
# the full 30s deadline because wait_url() probed through the parent's
# ambient HTTP proxy (no loopback exemption). These tests mirror that
# differential with a controlled local 502 proxy and a REAL owned child.


class _Always502Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


class _Health200Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


def _serve(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _inherit_proxy_env(monkeypatch, proxy_url):
    """Model the parent process behind an inherited HTTP proxy with NO
    loopback exemption (the Gate's reproduced startup condition)."""
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.setenv(var, proxy_url)
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def refusing_proxy():
    server = _serve(_Always502Handler)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_ensure_loopback_proxy_bypass_merges_and_is_idempotent(monkeypatch):
    # No exemption present → both loopback hosts added to both variables.
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    benchmark.ensure_loopback_proxy_bypass()
    for var in ("NO_PROXY", "no_proxy"):
        entries = os.environ[var].split(",")
        assert "127.0.0.1" in entries and "localhost" in entries, os.environ[var]
    # Existing operator entries are preserved and duplicates never accrete.
    monkeypatch.setenv("NO_PROXY", "internal.example,10.0.0.0/8")
    monkeypatch.setenv("no_proxy", "internal.example")
    benchmark.ensure_loopback_proxy_bypass()
    first = os.environ["NO_PROXY"]
    assert first.startswith("internal.example,10.0.0.0/8,"), first
    benchmark.ensure_loopback_proxy_bypass()
    assert os.environ["NO_PROXY"] == first, "bypass must be idempotent"
    assert "127.0.0.1" in os.environ["no_proxy"].split(",")


def test_readiness_ignores_inherited_proxy_and_reaches_real_child(
    monkeypatch, refusing_proxy
):
    _inherit_proxy_env(monkeypatch, refusing_proxy)
    server = _serve(_Health200Handler)
    try:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            health = f"http://127.0.0.1:{server.server_address[1]}/health"
            # Run the REAL wait_url in a fresh interpreter with the inherited
            # proxy env — the same conditions the Gate reproduced. A fresh
            # interpreter matters: urllib caches its module-global opener on
            # first use, so an in-process call may never see the env change.
            # Ambient urlopen first proves the failure mode (healthy child,
            # 502 through the proxy); wait_url then must reach the child
            # anyway. Pre-fix this probe times out; post-fix it prints READY.
            probe = subprocess.run(
                [sys.executable, "-c",
                 "import importlib.util, sys, urllib.request\n"
                 "spec = importlib.util.spec_from_file_location('lrs', sys.argv[1])\n"
                 "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                 "class _P:\n"
                 "    def poll(self):\n"
                 "        return None\n"
                 "try:\n"
                 "    urllib.request.urlopen(sys.argv[2], timeout=2)\n"
                 "    raise SystemExit('precondition failed: ambient urlopen succeeded')\n"
                 "except Exception as err:\n"
                 "    if '502' not in str(err):\n"
                 "        raise\n"
                 "m.wait_url(sys.argv[2], _P(), timeout=5)\n"
                 "print('READY')\n",
                 str(SCRIPT), health],
                env={**os.environ, "http_proxy": refusing_proxy,
                     "HTTP_PROXY": refusing_proxy, "https_proxy": refusing_proxy,
                     "HTTPS_PROXY": refusing_proxy},
                capture_output=True, text=True, timeout=60,
            )
            assert probe.returncode == 0 and "READY" in probe.stdout, (
                f"readiness behind an inherited proxy must reach the healthy "
                f"child; rc={probe.returncode} stdout={probe.stdout[-200:]!r} "
                f"stderr={probe.stderr[-400:]!r}"
            )
            # Direct contract: wait_url also succeeds in-process with the
            # same inherited-proxy environment present.
            benchmark.wait_url(health, child, timeout=5)
        finally:
            child.terminate()
            child.wait(5)
    finally:
        server.shutdown()
        server.server_close()


def test_ambient_proxy_loopback_exemption_is_the_explicit_bypass_control(
    monkeypatch, refusing_proxy
):
    """The Gate's control: exempting loopback in the PARENT (NO_PROXY) makes
    ambient urlopen reach the local server — the fix instead needs no
    operator-side change, so wait_url succeeds with or without it."""
    _inherit_proxy_env(monkeypatch, refusing_proxy)
    server = _serve(_Health200Handler)
    try:
        health = f"http://127.0.0.1:{server.server_address[1]}/health"
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            benchmark.wait_url(health, child, timeout=5)
            # Control: with the loopback exemption restored, ambient urlopen
            # also reaches the local server (bypassing the 502 proxy).
            monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
            monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
            with urllib.request.urlopen(health, timeout=2) as response:
                assert response.status == 200
        finally:
            child.terminate()
            child.wait(5)
    finally:
        server.shutdown()
        server.server_close()
