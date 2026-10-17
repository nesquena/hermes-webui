#!/usr/bin/env python3
"""E2E: wake word triggers dictation in a real browser (hermetic).

Boots the real server.py on an EPHEMERAL port with a fully isolated
environment (all ambient HERMES_* config scrubbed, isolated state dir and
HERMES_HOME, no agent — same convention as tests/browser_smoke.py) and
serves the page in headless Chromium. Fixtures live in
tests/fixtures/wakeword/ (16 kHz mono int16 WAV): a 12 s positive
("hey jarvis" in babble at 20 dB SNR, peak model score 0.996 on the eval
corpus) and a 60 s negative (other real speech).

Phase A — LIVE MICROPHONE PATH (finds wiring bugs the injection path can't):
  Chromium is launched with --use-file-for-fake-audio-capture=<positive wav>,
  so getUserMedia yields a fake mic that actually plays the fixture. The
  detector must receive samples through the real AudioWorklet tap, detect
  the phrase, and start dictation (window._micActive flips true) with NO
  direct calls into the detector.

Phase B — MODEL FALSE-POSITIVE CONTROL (isolated injection):
  The live mic feed is DETACHED (beginInjection: worklet disconnected,
  stream stopped, audio history reset) before 60 s of negative real speech
  is pushed straight into _onAudio; the full mel→embedding→wake pipeline
  must produce ZERO triggers. The detector's inference queue is awaited
  (settle) before reading the trigger count, so slow runs cannot pass
  before all audio has been checked.

Phase C — TERMINAL PROCESSING-FAILURE LIFECYCLE (review finding #2):
  A running detector whose inference starts throwing must stop listening,
  release its resources, leave state='error' (never a zombie 'running'),
  and notify the owner (preference flipped off, checkbox unchecked).

Exit 0 = pass.
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "wakeword"
POS_FILE = FIXTURES / "positive_hey_jarvis.wav"
NEG_FILE = FIXTURES / "negative_speech_60s.wav"


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def boot_server(port):
    # Hermetic: no ambient HERMES_* config leaks into the test server
    # (review: "copying all ambient configuration" is not isolation).
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    state = tempfile.mkdtemp(prefix="ww-e2e-state-")
    env.update({
        "HERMES_WEBUI_STATE_DIR": state,
        "HERMES_WEBUI_PORT": str(port),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_HOME": tempfile.mkdtemp(prefix="ww-e2e-home-"),
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        # Agent pointed at a path that doesn't exist: the server boots and
        # serves the UI agent-free by design.
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state, "no-agent"),
    })
    proc = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
            return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("server did not boot")


def read_wav_f32(path):
    import wave
    import numpy as np
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return x


def wait_running(page, errors, timeout=120):
    deadline = time.time() + timeout
    state = None
    while time.time() < deadline:
        state = page.evaluate("() => window._wakeWordDetector ? window._wakeWordDetector.state : 'none'")
        if state == "running":
            return state
        time.sleep(1)
    raise AssertionError(f"detector did not reach running: {state}; console errors: {errors[:5]}")


def main():
    from playwright.sync_api import sync_playwright

    for f in (POS_FILE, NEG_FILE):
        if not f.exists():
            raise AssertionError(f"missing fixture {f} — tests/fixtures/wakeword/ must ship with the repo")

    neg = read_wav_f32(NEG_FILE)
    print(f"fixtures: pos={POS_FILE.name} neg={NEG_FILE.name} ({len(neg)/16000:.0f}s)")

    port = free_port()
    proc = boot_server(port)
    print("server up on", port)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    # Fake mic plays the positive fixture through the real
                    # getUserMedia → AudioWorklet → detector wiring.
                    "--use-fake-device-for-media-stream",
                    "--use-fake-ui-for-media-stream",
                    f"--use-file-for-fake-audio-capture={POS_FILE}",
                    "--allow-autoplay",
                    "--no-sandbox",
                ],
            )
            ctx = browser.new_context(permissions=["microphone"])
            page = ctx.new_page()
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

            # open settings, enable wake word checkbox (real UI path)
            page.evaluate("""() => {
                const cb = document.getElementById('settingsWakeWord');
                if (!cb) throw new Error('settingsWakeWord checkbox missing');
                cb.checked = true;
                cb.dispatchEvent(new Event('change'));
            }""")

            state = wait_running(page, errors)
            print("detector state:", state)
            trig_before = page.evaluate("() => window._wakeWordDetector.triggers")

            # ── Phase A: live fake-mic delivery ──────────────────────────
            # The fake mic loops the 12 s positive clip; the phrase should
            # trip the detector through the REAL mic path within ~2 loops.
            mic_ok = False
            st = {}
            deadline = time.time() + 45
            while time.time() < deadline:
                st = page.evaluate("""() => ({
                    triggers: window._wakeWordDetector.triggers,
                    micActive: !!window._micActive,
                    lastScore: window._wakeWordDetector.score,
                })""")
                if st["triggers"] > trig_before and st["micActive"] is True:
                    mic_ok = True
                    break
                time.sleep(1)
            print("phase A (live mic):", json.dumps(st), "ok=", mic_ok)

            # stop dictation so later phases start from a clean slate
            if st.get("micActive"):
                page.evaluate("() => { try { window._toggleMicCapture(); } catch(_){} }")
                page.wait_for_timeout(1000)

            # ── Phase B: isolated negative control ───────────────────────
            # DETACH the live feed first (no mixed positive loop), reset
            # audio history, then inject 60 s of negative speech and AWAIT
            # the inference queue before counting triggers.
            page.evaluate("() => window._wakeWordDetector.beginInjection()")
            page.wait_for_timeout(300)
            # Chunked delivery: a single ~1M-sample JSON payload can OOM the
            # renderer; 2 s blocks every 5 ms are well within budget.
            page.evaluate("""(neg) => {
                const d = window._wakeWordDetector;
                const CH = 1280 * 25;  // 2 s blocks
                return new Promise((resolve) => {
                    let i = 0;
                    function pump(){
                        const block = neg.slice(i, i + CH);
                        if (!block.length) return resolve();
                        d._onAudio(Float32Array.from(block));
                        i += CH;
                        setTimeout(pump, 5);
                    }
                    pump();
                });
            }""", [float(v) for v in neg])
            page.evaluate("() => window._wakeWordDetector.settle()")
            page.wait_for_timeout(500)
            trig_neg = page.evaluate("() => window._wakeWordDetector.triggers")
            print("phase B (negative control): triggers =", trig_neg)

            # ── Phase C: terminal processing-failure lifecycle ───────────
            # Make inference throw: replace the wake model's run with a
            # rejecting function, feed audio, and require the detector to
            # land in state='error' with resources released and the
            # preference flipped off by the owner.
            fail_ok = page.evaluate("""async () => {
                const d = window._wakeWordDetector;
                d.wakeModel.run = () => Promise.reject(new Error('synthetic inference failure'));
                d._onAudio(new Float32Array(1280 * 8));
                const t0 = performance.now();
                while (d.state !== 'error' && performance.now() - t0 < 10000) {
                    await new Promise(r => setTimeout(r, 100));
                }
                return {
                    state: d.state,
                    prefOff: localStorage.getItem('hermes-wake-word-enabled') !== 'true',
                    cbUnchecked: !(document.getElementById('settingsWakeWord') || {}).checked,
                    micReleased: !d.stream && !d.ctx,
                    modelsReleased: !d.melModel && !d.embModel && !d.wakeModel,
                };
            }""")
            print("phase C (failure lifecycle):", json.dumps(fail_ok))

            hard_errors = [e for e in errors if "wakeword" not in e.lower() or "failed" in e.lower()]
            ok = (
                state == "running"
                and mic_ok
                and trig_neg == 0
                and fail_ok
                and fail_ok.get("state") == "error"
                and fail_ok.get("prefOff") is True
                and fail_ok.get("cbUnchecked") is True
                and fail_ok.get("micReleased") is True
                and fail_ok.get("modelsReleased") is True
                and not hard_errors
            )
            print("console errors:", errors[:8])
            print("hard errors:", hard_errors[:8])
            print("RESULT:", "PASS" if ok else "FAIL")
            browser.close()
            return 0 if ok else 1
    finally:
        proc.kill()


if __name__ == "__main__":
    sys.exit(main())
