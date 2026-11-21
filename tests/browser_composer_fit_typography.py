#!/usr/bin/env python3
"""Real-Chromium regression for inherited composer footer typography changes.

The scene follows the #9045 gate reproduction: start with a visible default-skin
composer, open Settings (which makes the left control group zero-width), select
Nous through the real skin picker, then return to Chat with identical labels and
available geometry.  The natural, observer-driven stage and overflow must match
an explicit forced fit.  A subsequent unchanged scheduled fit must not probe
scrollWidth.
"""
import json
import os
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.error
import urllib.request


WIDTHS = (702, 714, 1002, 1014)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(base, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.1)
    return False


def _wait_until(page, expression, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if page.evaluate(expression):
            return True
        page.wait_for_timeout(50)
    return False


_GEOMETRY = """() => {
  const footer = document.querySelector('.composer-footer');
  const left = footer.querySelector('.composer-left');
  const type = getComputedStyle(left);
  return {
    stage: footer.classList.contains('cf-burger') ? 'burger'
      : footer.classList.contains('cf-icons') ? 'icons' : 'full',
    footerClientWidth: footer.clientWidth,
    leftClientWidth: left.clientWidth,
    leftScrollWidth: left.scrollWidth,
    text: left.textContent,
    typography: [type.fontFamily, type.fontSize, type.fontWeight,
      type.fontStyle, type.lineHeight, type.letterSpacing].join('|'),
  };
}"""


def _check(browser, base, width):
    context = browser.new_context(
        base_url=base, viewport={"width": width, "height": 900},
    )
    # The isolated server retains appearance across scenes; keep each scene's
    # explicit local boot choice at default without changing server state.
    context.add_init_script(
        "try { localStorage.setItem('hermes-skin', 'default'); } catch (_) {}"
    )
    page = context.new_page()
    page.route("**/api/settings", lambda route: (
        route.fulfill(status=200, content_type="application/json", body="{}")
        if route.request.method == "POST" else route.continue_()
    ))
    errors = []
    failures = []
    observations = {}
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector(".composer-footer .composer-left", timeout=15000)
        if not _wait_until(page, "document.querySelector('.composer-left').clientWidth > 0"):
            failures.append("default composer never became visible")
            return failures, observations, errors
        page.wait_for_timeout(1000)
        baseline = page.evaluate(_GEOMETRY)
        observations["default"] = baseline
        if baseline["stage"] != "full":
            failures.append(f"precondition: default stage was {baseline['stage']}, not full")

        cache_key = page.evaluate("_composerFitLastKey")
        page.evaluate("""() => {
          window.__fitCalls = [];
          const original = _fitComposerFooter;
          _fitComposerFooter = function(force) {
            window.__fitCalls.push({forcedVisible: force !== false
              && document.querySelector('.composer-left').clientWidth > 0});
            return original.apply(this, arguments);
          };
        }""")
        page.evaluate("switchPanel('settings')")
        if not _wait_until(page, "document.querySelector('.composer-left').clientWidth === 0"):
            failures.append("Settings did not hide the composer to zero width")
        page.evaluate("switchSettingsSection('appearance')")
        page.locator('#skinPickerGrid [data-skin-val="nous"]').click()
        page.evaluate("_fitComposerFooter(false)")
        hidden = page.evaluate(_GEOMETRY)
        observations["hidden_nous"] = hidden
        if hidden["leftClientWidth"] != 0:
            failures.append("Nous selection did not preserve the hidden zero-width state")
        if page.evaluate("_composerFitLastKey") != cache_key:
            failures.append("hidden zero-width call overwrote the restored-layout memo key")

        page.evaluate("switchPanel('chat')")
        if not _wait_until(page, "document.querySelector('.composer-left').clientWidth > 0"):
            failures.append("Chat did not restore a visible composer")
        page.wait_for_timeout(500)
        natural = page.evaluate(_GEOMETRY)
        observations["natural_nous"] = natural
        observations["natural_fit_calls"] = page.evaluate("window.__fitCalls")
        if any(call["forcedVisible"] for call in observations["natural_fit_calls"]):
            failures.append("scene drift: a forced fit ran before the forced control")

        page.evaluate("_fitComposerFooter(true)")
        page.wait_for_timeout(100)
        forced = page.evaluate(_GEOMETRY)
        observations["forced_nous"] = forced

        if natural["text"] != baseline["text"]:
            failures.append("scene drift: label text changed during the skin round trip")
        if natural["typography"] == baseline["typography"]:
            failures.append("scene drift: inherited typography did not change")
        for key in ("footerClientWidth", "leftClientWidth"):
            if natural[key] != baseline[key]:
                failures.append(
                    f"scene drift: {key} changed {baseline[key]} -> {natural[key]}"
                )
        if natural["stage"] != forced["stage"]:
            failures.append(
                f"natural stage {natural['stage']} != forced stage {forced['stage']}"
            )
        for name, geometry in (("natural", natural), ("forced", forced)):
            if geometry["leftScrollWidth"] > geometry["leftClientWidth"] + 1:
                failures.append(
                    f"{name} overflow {geometry['leftScrollWidth']} > "
                    f"{geometry['leftClientWidth']} + 1"
                )

        memo_reads = page.evaluate(
            """() => {
              const left = document.querySelector('.composer-left');
              let reads = 0;
              Object.defineProperty(left, 'scrollWidth', {
                configurable: true, get() { reads++; return 0; },
              });
              _fitComposerFooter(false);
              _fitComposerFooter(false);
              return reads;
            }"""
        )
        observations["unchanged_memo_scroll_reads"] = memo_reads
        if memo_reads:
            failures.append(f"unchanged scheduled fit probed scrollWidth {memo_reads} times")
    except Exception as error:  # retain the other widths as evidence
        failures.append(f"driver error: {type(error).__name__}: {error}")
    finally:
        context.close()
    return failures, observations, errors


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SETUP FAIL: playwright not installed", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    state_dir = tempfile.mkdtemp(prefix="hermes-composer-fit-typography-")
    os.mkdir(os.path.join(state_dir, "no-agent"))
    open(os.path.join(state_dir, "no-agent", "run_agent.py"), "w").close()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY") or key in {
            "HERMES_WEBUI_PASSWORD", "HERMES_WEBUI_PASSKEY", "HERMES_WEBUI_PYTHON",
        }:
            env.pop(key, None)
    env.update({
        "HERMES_WEBUI_PORT": str(port),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_PYTHON": sys.executable,
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
    })
    dependency_path = os.pathsep.join(filter(None, (
        repo_root, sysconfig.get_paths()["purelib"], env.get("PYTHONPATH", ""),
    )))
    env["PYTHONPATH"] = dependency_path
    with open(os.path.join(state_dir, "server.log"), "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-S", os.path.join(repo_root, "server.py")],
            cwd=repo_root, env=env, stdout=log, stderr=subprocess.STDOUT,
            **({"creationflags": subprocess.CREATE_NO_WINDOW}
               if sys.platform == "win32" else {}),
        )
        try:
            if not _wait_for_health(base):
                print("SETUP FAIL: server did not become healthy", file=sys.stderr)
                return 2
            failures = []
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                for width in WIDTHS:
                    scene_failures, observations, errors = _check(browser, base, width)
                    print(f"SCENE {width}: {json.dumps(observations, sort_keys=True)}")
                    for error in errors:
                        scene_failures.append(f"pageerror: {error}")
                    for failure in scene_failures:
                        failures.append(f"[{width}] {failure}")
                    if scene_failures:
                        for failure in scene_failures:
                            print(f"FAIL [{width}] {failure}", file=sys.stderr)
                    else:
                        print(f"OK   [{width}] Settings -> Nous -> Chat natural == forced; memoized control")
                browser.close()
            if failures:
                print(f"\nCOMPOSER TYPOGRAPHY FAILED ({len(failures)} findings)", file=sys.stderr)
                return 1
            print("\nCOMPOSER TYPOGRAPHY PASSED — 4 real-Chromium scenes")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
