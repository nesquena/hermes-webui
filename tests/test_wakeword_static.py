"""Static contract tests for the wake-word dictation feature (t_c70e5bf1).

The full audio-path E2E lives in tests/browser_wakeword_e2e.py (needs
playwright + chromium + the eval corpus). These tests gate the pieces CI
can always check: the static assets exist and are served, the i18n keys
exist in EVERY locale (locale-parity convention), the boot.js wiring is
present, the CSP allows the WASM the detector needs, and wakeword.js
passes a syntax check.
"""
import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[1]


def _read(p):
    return (REPO / p).read_text(encoding="utf-8")


def test_vendored_assets_exist():
    for p in (
        "static/vendor/onnxruntime-web/ort.min.js",
        "static/vendor/onnxruntime-web/ort-wasm-simd-threaded.wasm",
        "static/vendor/onnxruntime-web/ort-wasm-simd-threaded.mjs",
        "static/vendor/openwakeword/melspectrogram.onnx",
        "static/vendor/openwakeword/embedding_model.onnx",
        "static/vendor/openwakeword/hey_hermes_v0.1.onnx",
        "static/wakeword.js",
    ):
        assert (REPO / p).is_file() and (REPO / p).stat().st_size > 1000, p


def test_wakeword_js_syntax():
    r = subprocess.run(
        ["node", "--check", str(REPO / "static" / "wakeword.js")],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


def test_settings_ui_wired():
    html = _read("static/index.html")
    assert 'id="settingsWakeWord"' in html
    assert 'data-i18n="wake_word_label"' in html
    boot = _read("static/boot.js")
    assert "hermes-wake-word-enabled" in boot
    assert "_applyWakeWordPreference" in boot
    assert "static/wakeword.js" in boot          # lazy-load path
    assert "static/vendor/onnxruntime-web/ort.min.js" in boot
    assert "_toggleMicCapture" in boot            # triggers the existing mic path


def test_wakeword_detector_contract():
    js = _read("static/wakeword.js")
    # exposes the constructor + no auto-start on load
    assert "window.HermesWakeWord" in js
    assert "getUserMedia" not in js.split("async start(")[0]  # mic only on start()
    # trigger rule present
    assert "consecutive" in js and "refractoryMs" in js


def test_wakeword_start_is_cancellable_and_releases_resources():
    """Greptile #2/#3: stop() must cancel in-flight start() and release
    ONNX sessions so repeated toggles can't leak mic or models."""
    js = _read("static/wakeword.js")
    # generation token: stop() bumps it, start() checks after every await
    assert "this._gen = (this._gen || 0) + 1" in js or "this._gen = (this._gen||0)+1" in js
    assert "stale()" in js
    # sessions released on stop, serialized behind pending inference
    assert "_releaseSessions" in js
    assert ".release()" in js


def test_wakeword_trigger_is_start_only():
    """Greptile #1: a wake detection must never stop/cancel dictation via
    _toggleMicCapture's toggle semantics; it goes through a start-only
    entry point that also ignores requests while a capture is starting."""
    boot = _read("static/boot.js")
    assert "_startMicCaptureFromWake" in boot
    assert "_isMicCaptureStarting" in boot
    assert "window._toggleMicCapture()" not in boot[boot.index("_wakeTrigger"):]
    assert "_startMicCaptureFromWake()" in boot


def test_wakeword_e2e_is_hermetic():
    """Greptile #4/#5 + review: the browser E2E must ship its fixtures
    in-repo, exercise the live mic path, run on an ephemeral port with a
    scrubbed environment (no ambient HERMES_* config), and hard-fail on
    any console error."""
    e2e = _read("tests/browser_wakeword_e2e.py")
    assert "tests/fixtures/wakeword" in e2e
    assert "--use-file-for-fake-audio-capture" in e2e
    # ephemeral port: no fixed port constant anywhere
    assert "PORT = 22377" not in e2e and "PORT=22377" not in e2e
    assert "free_port()" in e2e
    # ambient config scrubbed before the server boots
    assert 'k.startswith("HERMES_")' in e2e
    # negative control must detach the live feed and await inference
    assert "beginInjection" in e2e
    assert "settle()" in e2e
    # console errors must be part of the pass condition
    assert "not hard_errors" in e2e
    for name in ("positive_hey_hermes.wav", "negative_speech_60s.wav"):
        p = REPO / "tests" / "fixtures" / "wakeword" / name
        assert p.is_file() and p.stat().st_size > 10000, name


def test_wakeword_stale_startup_does_not_stop_replacement():
    """Review finding #1: a stale _wakeStart() completion must stop only its
    OWN detector instance and clear the shared slot only under instance
    identity — never stop a replacement detector a newer enable created."""
    boot = _read("static/boot.js")
    # the detector is captured in a local before start() is awaited
    assert "const det=new window.HermesWakeWord.WakeWordDetector" in boot
    assert "await det.start()" in boot
    # stale settlement stops only its own instance …
    assert "try{ det.stop(); }catch(_){}" in boot
    # … and clears shared state only when this instance still owns it
    assert "if(_wakeDetector===det){ _wakeDetector=null; window._wakeWordDetector=null; }" in boot


def test_wakeword_processing_failure_is_terminal():
    """Review finding #2: a processing/inference error must route through an
    owner-aware failure path — resources released, state='error' (never a
    zombie 'running'), owner notified — instead of logging and retrying."""
    js = _read("static/wakeword.js")
    boot = _read("static/boot.js")
    # detector: terminal failure path exists and releases everything
    assert "_fail(err)" in js
    assert "this.onError(err)" in js
    # no swallow-and-continue: the old 'log and return' path is gone
    assert "if (this.melModel) log('frame error'" not in js
    # owner: failure flips the preference off and unchecks the box
    assert "function _wakeError(det,err)" in boot
    assert "_wakeReportError(err)" in boot
    # E2E exercises the failure lifecycle for real
    e2e = _read("tests/browser_wakeword_e2e.py")
    assert "synthetic inference failure" in e2e
    assert "modelsReleased" in e2e


def test_wakeword_documented():
    """Greptile #6: user/runtime docs required for new user-facing flows."""
    doc = REPO / "docs" / "wake-word.md"
    assert doc.is_file()
    text = doc.read_text(encoding="utf-8")
    for needle in ("Enable / disable", "localStorage", "0 FP", "start-only"):
        assert needle in text, needle
    readme = _read("README.md")
    assert "docs/wake-word.md" in readme


def test_i18n_locale_parity():
    src = _read("static/i18n.js")
    locales = re.findall(r"^  ('?[A-Za-z-]+'?): \{", src, re.M)
    assert locales, "no locales found"
    for key in ("wake_word_label", "wake_word_desc", "wake_word_toast"):
        n = len(re.findall(rf"\b{key}:", src))
        assert n == len(locales), f"{key}: {n} occurrences for {len(locales)} locales"


def test_csp_allows_wasm():
    src = _read("api/helpers.py")
    assert "'wasm-unsafe-eval'" in src
