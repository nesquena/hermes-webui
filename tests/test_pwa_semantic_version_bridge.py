"""Behavioral regression coverage for semantic bundle version vs asset fingerprint."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PWA_STARTUP = ROOT / "static" / "pwa-startup.js"


def _run_stamp(stamp: str) -> dict[str, str]:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for PWA startup behavior tests")
    source = PWA_STARTUP.read_text(encoding="utf-8")
    script = f"""
const vm = require('vm');
const source = {json.dumps(source)};
const classes = {{ toggle(){{}}, add(){{}}, remove(){{}} }};
global.document = {{
  documentElement: {{ classList: classes, dataset: {{}} }},
  addEventListener() {{}},
  visibilityState: 'visible',
}};
global.window = {{
  navigator: {{
    standalone: false,
    userAgent: '',
    platform: '',
    maxTouchPoints: 0,
    onLine: true,
  }},
  matchMedia() {{
    return {{ matches: false, addEventListener() {{}}, addListener() {{}} }};
  }},
  addEventListener() {{}},
  dispatchEvent() {{}},
  setTimeout() {{}},
}};
global.CustomEvent = function() {{}};
vm.runInThisContext(source);
window.__HERMES_WEBUI_BUNDLE_VERSION__ = {json.dumps(stamp)};
process.stdout.write(JSON.stringify({{
  bundle: String(window.__HERMES_WEBUI_BUNDLE_VERSION__ || ''),
  asset: String(window.__HERMES_WEBUI_ASSET_VERSION__ || ''),
}}));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_asset_fingerprint_does_not_change_semantic_bundle_version():
    result = _run_stamp("v0.52.294%2Ba0123456789")
    assert result == {
        "bundle": "v0.52.294",
        "asset": "v0.52.294%2Ba0123456789",
    }


def test_plain_release_version_keeps_existing_semantics():
    result = _run_stamp("v0.52.294")
    assert result == {
        "bundle": "v0.52.294",
        "asset": "v0.52.294",
    }


def test_unrelated_semver_build_metadata_is_not_stripped():
    result = _run_stamp("v0.52.294%2Blinux")
    assert result == {
        "bundle": "v0.52.294+linux",
        "asset": "v0.52.294%2Blinux",
    }
