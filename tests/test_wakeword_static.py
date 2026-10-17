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
        "static/vendor/openwakeword/hey_jarvis_v0.1.onnx",
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
    js = _read("static/wakeword.js")
    assert "_startMicCaptureFromWake" in boot
    assert "_isMicCaptureStarting" in boot
    assert "window._toggleMicCapture()" not in boot[boot.index("_wakeTrigger"):]
    assert "_startMicCaptureFromWake()" in boot


def test_wakeword_e2e_is_hermetic():
    """Greptile #4/#5: the browser E2E must ship its fixtures in-repo and
    exercise the live mic path, not only direct injection."""
    e2e = _read("tests/browser_wakeword_e2e.py")
    assert "tests/fixtures/wakeword" in e2e
    assert "--use-file-for-fake-audio-capture" in e2e
    for name in ("positive_hey_jarvis.wav", "negative_speech_60s.wav"):
        p = REPO / "tests" / "fixtures" / "wakeword" / name
        assert p.is_file() and p.stat().st_size > 10000, name


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
