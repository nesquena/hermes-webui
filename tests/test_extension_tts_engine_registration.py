"""Compatibility tombstone for retired WebUI extension TTS engines."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TTS_JS = (ROOT / "static" / "tts.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
ALL_PRODUCT_JS = "\n".join(
    path.read_text(encoding="utf-8") for path in (ROOT / "static").glob("*.js")
)
NODE = shutil.which("node")


def test_exact_public_tombstone_exists_once():
    exact = """window.registerHermesTtsEngine = function registerHermesTtsEngineTombstone(){
  return false;
};"""
    assert TTS_JS.count(exact) == 1
    assert ALL_PRODUCT_JS.count("window.registerHermesTtsEngine") == 1


def test_registry_runtime_and_private_facades_are_absent():
    forbidden = (
        "_HERMES_TTS_ENGINES",
        "_hermesTtsIsRegistered",
        "_hermesTtsEngineOptions",
        "_hermesTtsSynth",
        "_hermesAddTtsOption",
    )
    for symbol in forbidden:
        assert symbol not in ALL_PRODUCT_JS
    assert "registerHermesTtsEngine" not in BOOT_JS
    assert "registerHermesTtsEngine" not in UI_JS
    assert "registerHermesTtsEngine" not in PANELS_JS


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_every_old_call_returns_strict_false_without_retaining_or_calling():
    tombstone = TTS_JS[: TTS_JS.index("\n\n(function()")]
    script = f"""
const window={{}};
let callbacks=0;
{tombstone}
const values=[
  window.registerHermesTtsEngine(),
  window.registerHermesTtsEngine(null),
  window.registerHermesTtsEngine({{}}),
  window.registerHermesTtsEngine({{id:'voicevox',label:'Old',synthesize:()=>{{callbacks++;return new ArrayBuffer(1);}}}}),
  window.registerHermesTtsEngine({{id:'voicevox',synthesize:()=>{{callbacks++;}}}}),
];
process.stdout.write(JSON.stringify({{values,callbacks,keys:Object.keys(window)}}));
"""
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "values": [False, False, False, False, False],
        "callbacks": 0,
        "keys": ["registerHermesTtsEngine"],
    }
