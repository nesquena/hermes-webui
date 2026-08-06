"""Extension TTS-engine registration capability (window.registerHermesTtsEngine).

Two layers:
  1. Structural — the public API + the two playback paths + the settings re-add
     hook exist.
  2. Behavioral — a Node harness extracts the registry from boot.js and exercises
     register / validation / reserved-key guard / synth type-coercion.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")


def test_public_api_present():
    assert "window.registerHermesTtsEngine=function" in BOOT_JS
    assert "window._hermesTtsSynth=function" in BOOT_JS
    assert "window._hermesTtsIsRegistered=function" in BOOT_JS
    assert "window._hermesTtsEngineOptions=function" in BOOT_JS


def test_reserved_builtins_guarded():
    assert "_HERMES_TTS_RESERVED" in BOOT_JS
    # browser/edge/elevenlabs/openai must be reserved so an extension can't shadow them
    assert "browser:1" in BOOT_JS
    assert "edge:1" in BOOT_JS
    assert "elevenlabs:1" in BOOT_JS
    assert "openai:1" in BOOT_JS


def test_both_playback_paths_check_registry():
    # voice-mode auto-read (boot.js _speakResponse) and the per-message Listen
    # button (ui.js speakMessage) must both route registered engines.
    assert "_hermesTtsIsRegistered(engine)" in BOOT_JS, "voice-mode path must check the registry"
    assert "_hermesTtsIsRegistered(engine)" in UI_JS, "per-message path must check the registry"


def test_settings_panel_readds_registered_options():
    assert "_hermesTtsEngineOptions" in PANELS_JS, (
        "settings panel must re-add registered engine options on render"
    )


def test_option_label_uses_textcontent_not_innerhtml():
    # the engine label goes into the dropdown via textContent (no HTML injection)
    assert "opt.textContent=label" in BOOT_JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_registration_behavior():
    """Drive the real registry logic from boot.js in a Node harness."""
    start = BOOT_JS.index("var _HERMES_TTS_ENGINES")
    end = BOOT_JS.index("window._hermesTtsSynth=function")
    end = BOOT_JS.index("};", BOOT_JS.index("throw new Error('TTS engine returned", end)) + 2
    region = BOOT_JS[start:end]

    harness = textwrap.dedent(
        """
        const window = {};
        const document = { getElementById: () => null };  // no <select> in harness
        %s
        const results = {};
        // valid registration
        results.validOk = window.registerHermesTtsEngine({
          id: 'voicevox', label: 'VOICEVOX', synthesize: () => new ArrayBuffer(4)
        });
        results.isRegistered = window._hermesTtsIsRegistered('voicevox');
        // reserved key rejected
        results.reservedRejected = (window.registerHermesTtsEngine({
          id: 'edge', label: 'x', synthesize: () => new ArrayBuffer(1) }) === false);
        results.openaiReservedRejected = (window.registerHermesTtsEngine({
          id: 'openai', label: 'x', synthesize: () => new ArrayBuffer(1) }) === false);
        // bad id rejected
        results.badIdRejected = (window.registerHermesTtsEngine({
          id: 'Bad Id!', label: 'x', synthesize: () => new ArrayBuffer(1) }) === false);
        // missing synthesize rejected
        results.noSynthRejected = (window.registerHermesTtsEngine({ id: 'nosynth', label: 'x' }) === false);
        // options list reflects the registered engine
        results.optionListed = window._hermesTtsEngineOptions().some(e => e.id === 'voicevox');
        // synth coerces ArrayBuffer through
        window._hermesTtsSynth('voicevox', 'hi', {}).then(buf => {
          results.synthReturnsArrayBuffer = (buf instanceof ArrayBuffer);
          // unregistered engine returns null
          results.unregisteredNull = (window._hermesTtsSynth('ghost', 'hi', {}) === null);
          console.log(JSON.stringify(results));
        });
        """
    ) % region

    out = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, f"harness failed: {out.stderr}"
    import json
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["validOk"] is True
    assert r["isRegistered"] is True
    assert r["reservedRejected"] is True
    assert r["openaiReservedRejected"] is True
    assert r["badIdRejected"] is True
    assert r["noSynthRejected"] is True
    assert r["optionListed"] is True
    assert r["synthReturnsArrayBuffer"] is True
    assert r["unregisteredNull"] is True
