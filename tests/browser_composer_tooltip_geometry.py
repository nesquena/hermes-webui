#!/usr/bin/env python3
"""Real-browser regression gate for composer-tooltip measurement lifecycle.

Binds the three regressions reproduced on PR #8766 (2026-11-05):

* with Dictate visible, enabled, idle, neither hovered nor focused, Chromium's
  accessibility tree must still name #btnMic "Dictate" (Attach is the positive
  control), and its generated tooltip text must not have been destroyed;
* with Attach's tooltip already visible, a real viewport resize in both
  directions must not hide and re-reveal it while hover remains active;
* in a populated long-name scene at 1120x900, the natural shrink-to-icons
  boundary must keep the enabled Attach control at its declared 34px width,
  demoting to icons when needed rather than squeezing its click target;
* with the supported control-order setting moved to Attach-last, the full-stage
  candidates at 1252px (ordinary labels) and 1464px (long labels) must preserve
  both Attach and Dictate at 34px, then recover at wide and phone widths.

The server and state are disposable and agent-free. No production app, browser,
credentials, or service is touched.

Usage: python tests/browser_composer_tooltip_geometry.py
Exit codes: 0 pass; 1 behavioral regression; 2 environment/setup failure.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(base: str, proc: subprocess.Popen, log_path: str, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    return False


def _wait_until(page, expression: str, timeout_ms: int = 3000) -> bool:
    """Poll without page.wait_for_function, whose polling needs CSP unsafe-eval."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression):
            return True
        page.wait_for_timeout(50)
    return False


def _ax_button_name(cdp, selector: str) -> str:
    document = cdp.send("DOM.getDocument", {"depth": 0})
    found = cdp.send(
        "DOM.querySelector",
        {"nodeId": document["root"]["nodeId"], "selector": selector},
    )
    if not found.get("nodeId"):
        raise AssertionError(f"missing DOM node {selector}")
    described = cdp.send("DOM.describeNode", {"nodeId": found["nodeId"]})
    backend_id = described["node"]["backendNodeId"]
    tree = cdp.send(
        "Accessibility.getPartialAXTree",
        {"backendNodeId": backend_id, "fetchRelatives": False},
    )
    buttons = [
        node for node in tree["nodes"]
        if node.get("role", {}).get("value") == "button" and not node.get("ignored")
    ]
    if len(buttons) != 1:
        raise AssertionError(f"expected one accessible button for {selector}: {tree['nodes']!r}")
    return str(buttons[0].get("name", {}).get("value", ""))


def _start_opacity_recording(page) -> None:
    page.evaluate(
        """() => {
          window.__composerTooltipOpacitySamples = [];
          const record = () => {
            const attach = document.querySelector('#btnAttach');
            const style = getComputedStyle(attach, '::after');
            window.__composerTooltipOpacitySamples.push({
              at: performance.now(),
              hovered: attach.matches(':hover'),
              opacity: Number(style.opacity),
              content: style.content,
              position: style.position,
            });
            if (window.__composerTooltipOpacitySamples.length < 24) {
              requestAnimationFrame(record);
            }
          };
          requestAnimationFrame(record);
        }"""
    )


def _collect_opacity_recording(page, width: int) -> list[dict]:
    if not _wait_until(page, "window.__composerTooltipOpacitySamples.length >= 24", 3000):
        return page.evaluate("window.__composerTooltipOpacitySamples")
    return page.evaluate("window.__composerTooltipOpacitySamples")


def _composer_geometry(page) -> dict:
    return page.evaluate(
        """() => {
          const footer = document.querySelector('.composer-footer');
          const left = document.querySelector('.composer-left');
          const attach = document.querySelector('#btnAttach');
          const mic = document.querySelector('#btnMic');
          const rect = attach.getBoundingClientRect();
          const micRect = mic.getBoundingClientRect();
          return {
            stage: footer.classList.contains('cf-burger') ? 'burger'
              : footer.classList.contains('cf-shrink') ? 'shrink'
              : footer.classList.contains('cf-icons') ? 'icons' : 'full',
            footerClasses: footer.className,
            attachEnabled: !attach.disabled,
            attachWidth: rect.width,
            attachHeight: rect.height,
            attachFlexShrink: getComputedStyle(attach).flexShrink,
            micWidth: micRect.width,
            micHeight: micRect.height,
            micFlexShrink: getComputedStyle(mic).flexShrink,
            leftClientWidth: left.clientWidth,
            leftScrollWidth: left.scrollWidth,
            controlOrder: Array.from(left.children)
              .filter((element) => !['fileInput', 'cameraFileInput'].includes(element.id) && !element.classList.contains('composer-divider'))
              .map((element) => element.id || element.className),
            children: Array.from(left.children).map((element) => ({
              id: element.id,
              className: element.className,
              display: getComputedStyle(element).display,
              flexShrink: getComputedStyle(element).flexShrink,
              width: element.getBoundingClientRect().width,
            })),
          };
        }"""
    )


def _check(browser, base: str) -> tuple[list[str], dict]:
    failures: list[str] = []
    evidence: dict = {}
    context = browser.new_context(
        base_url=base,
        viewport={"width": 1440, "height": 900},
        reduced_motion="no-preference",
    )
    # Headless Chromium normally exposes MediaRecorder, but provide the standard
    # SpeechRecognition seam as well so Dictate is deterministically visible.
    context.add_init_script(
        """window.SpeechRecognition = window.SpeechRecognition || function () {
          this.start = function () {};
          this.stop = function () {};
        };"""
    )
    page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    cdp = context.new_cdp_session(page)
    try:
        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector("#btnMic", state="visible", timeout=15000)
        if not _wait_until(page, "typeof window._fitComposerFooter === 'function'", 15000):
            return ["app did not initialize the composer fit path"], evidence
        # Static HTML starts with "Attach files" before the later locale pass
        # stamps the current translation. Wait for that pass rather than letting
        # a mid-test string swap restart the tooltip reveal and masquerade as a
        # native-resize regression.
        if not _wait_until(
            page,
            "typeof t === 'function' && document.querySelector('#btnAttach').dataset.tooltip === t('composer_control_attach')",
            15000,
        ):
            return ["locale hydration did not settle the Attach tooltip"], evidence
        # Ensure this is the certifier's idle case: no hover and no focus.
        page.mouse.move(5, 5)
        page.evaluate("document.activeElement && document.activeElement.blur()")
        page.wait_for_timeout(400)
        mic_name = _ax_button_name(cdp, "#btnMic")
        attach_name = _ax_button_name(cdp, "#btnAttach")
        idle = page.evaluate(
            """() => {
              const mic = document.querySelector('#btnMic');
              const left = document.querySelector('.composer-left');
              return {
                hovered: mic.matches(':hover'),
                focused: document.activeElement === mic,
                disabled: mic.disabled,
                content: getComputedStyle(mic, '::after').content,
                position: getComputedStyle(mic, '::after').position,
                leftClientWidth: left.clientWidth,
                leftScrollWidth: left.scrollWidth,
              };
            }"""
        )
        evidence["idle"] = {"micName": mic_name, "attachName": attach_name, **idle}
        if mic_name != "Dictate":
            failures.append(f"idle Dictate accessible name is {mic_name!r}, expected 'Dictate'")
        if attach_name != "Attach":
            failures.append(f"positive-control Attach accessible name is {attach_name!r}, expected 'Attach'")
        if idle["content"] == "none":
            failures.append("idle Dictate generated tooltip content was destroyed")

        page.set_viewport_size({"width": 1920, "height": 900})
        page.hover("#btnAttach")
        if not _wait_until(
            page,
            "Number(getComputedStyle(document.querySelector('#btnAttach'), '::after').opacity) >= 0.999",
            3000,
        ):
            failures.append("Attach tooltip never became fully visible before resize")
            return failures, evidence
        evidence["resize"] = {}
        for width in (1921, 1920):
            if width == 1921:
                # Record the actual synchronous scrollWidth reads made by the
                # native-resize refit. This is the production geometry oracle:
                # generated text survives, but its box is fixed-position while
                # the fit ladder reads the strip.
                page.evaluate(
                    """() => {
                      const left = document.querySelector('.composer-left');
                      const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollWidth');
                      window.__composerFitScrollReads = [];
                      Object.defineProperty(left, 'scrollWidth', {
                        configurable: true,
                        get() {
                          const value = descriptor.get.call(this);
                          const attach = document.querySelector('#btnAttach');
                          const style = getComputedStyle(attach, '::after');
                          window.__composerFitScrollReads.push({
                            scrollWidth: value,
                            clientWidth: left.clientWidth,
                            measuring: document.querySelector('.composer-footer').classList.contains('cf-measuring'),
                            content: style.content,
                            position: style.position,
                          });
                          return value;
                        },
                      });
                    }"""
                )
            _start_opacity_recording(page)
            page.set_viewport_size({"width": width, "height": 900})
            samples = _collect_opacity_recording(page, width)
            evidence["resize"][str(width)] = samples
            if width == 1921:
                fit_reads = page.evaluate(
                    """() => {
                      const left = document.querySelector('.composer-left');
                      const values = window.__composerFitScrollReads;
                      delete left.scrollWidth;
                      return values;
                    }"""
                )
                evidence["nativeFitScrollReads"] = fit_reads
                measuring_reads = [read for read in fit_reads if read["measuring"]]
                if not measuring_reads:
                    failures.append("native resize produced no observable fit-guard scrollWidth reads")
                if any(read["content"] == "none" for read in measuring_reads):
                    failures.append("native fit destroyed generated tooltip content during measurement")
                if any(read["position"] != "fixed" for read in measuring_reads):
                    failures.append("native fit did not detach generated tooltip boxes from scroll overflow")
                if any(read["scrollWidth"] > read["clientWidth"] + 1 for read in measuring_reads):
                    failures.append(f"phantom tooltip overflow remained in native fit reads: {measuring_reads}")
            if len(samples) != 24:
                failures.append(f"resize to {width}: collected {len(samples)} frames, expected 24")
                continue
            lost_hover = [sample for sample in samples if not sample["hovered"]]
            if lost_hover:
                failures.append(f"resize to {width}: hover was not stable across all frames")
                continue
            blanked = [sample for sample in samples if sample["opacity"] < 0.999]
            if blanked:
                failures.append(
                    f"resize to {width}: visible tooltip reset opacity in "
                    f"{len(blanked)}/24 frames; "
                    f"first={blanked[0]}"
                )
        # R1 (2026-11-05): build a synthetic populated long-name scene near
        # the shrink-to-icons boundary. Only control content/presence and the
        # viewport change; the production resize handler chooses the stage.
        page.evaluate(
            """() => {
              const labels = {
                composerWorkspaceLabel: 'a-very-long-workspace-name-for-layout-regression',
                composerModelLabel: 'an-extremely-long-language-model-display-name-for-the-footer',
              };
              for (const [id, text] of Object.entries(labels)) {
                const element = document.getElementById(id);
                if (element) element.textContent = text;
              }
              const workspace = document.getElementById('composerWorkspaceChip');
              if (workspace) workspace.disabled = false;
              const quota = document.getElementById('providerQuotaChip');
              quota.hidden = false;
              quota.style.display = 'inline-flex';
              document.getElementById('providerQuotaChipLabel').textContent = '82% remaining';
              const reasoning = document.getElementById('composerReasoningWrap');
              reasoning.style.display = 'block';
              document.getElementById('composerReasoningLabel').textContent = 'XHIGH';
              document.getElementById('ctxIndicatorWrap').style.display = 'inline-flex';
              const status = document.getElementById('composerStatus');
              status.style.display = 'inline';
              status.textContent = 'Running';
            }"""
        )
        page.set_viewport_size({"width": 1120, "height": 900})
        page.wait_for_timeout(400)
        shrink = _composer_geometry(page)
        evidence["longName1120"] = shrink
        if shrink["stage"] not in {"shrink", "icons"}:
            failures.append(f"populated 1120px scene missed the natural shrink-to-icons boundary: {shrink}")
        if not shrink["attachEnabled"]:
            failures.append(f"Attach is unexpectedly disabled in the 1120px boundary scene: {shrink}")
        if abs(shrink["attachWidth"] - 34) > 0.5:
            failures.append(f"compact stage squeezed enabled Attach below its declared 34px width: {shrink}")

        # C51-F1 (2026-11-07): the full stage could satisfy the same overflow
        # predicate by squeezing Attach/Mic too. Reproduce through the SUPPORTED
        # reorder seam rather than forcing a stage class or writing inline style.
        # Move every left control ahead of Attach, just as the settings drag
        # chip does; the production apply path rebuilds order and refits.
        page.evaluate(
            """() => {
              const defs = window._COMPOSER_CONTROL_TOGGLE_DEFS.concat(
                window._COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS,
              );
              const order = defs
                .filter((def) => def.orderGroup === 'left' && def.key !== 'hide_composer_attach')
                .map((def) => def.key)
                .concat('hide_composer_attach');
              if (typeof window._applyComposerControlOrder !== 'function') {
                throw new Error('supported composer control-order API missing');
              }
              window._applyComposerControlOrder(order);
            }"""
        )
        scenarios = (
            (
                "ordinary1252",
                1252,
                "default",
                "GPT-5.4 Mini",
            ),
            (
                "long1464",
                1464,
                "a-very-long-workspace-name-for-layout-regression",
                "an-extremely-long-language-model-display-name-for-the-footer",
            ),
        )
        # Keep one independently measured natural-full boundary. This host's
        # width-sweep reproduces C51-F1 at 1590px: the candidate stays full while
        # Attach/Mic collapse to 17.046875px. The two certifier widths above are
        # neighboring controls, not substitutes for this biting case.
        scenarios += ((
            "fullSqueeze1590",
            1590,
            "a-very-long-workspace-name-for-layout-regression",
            "an-extremely-long-language-model-display-name-for-the-footer",
        ),)
        evidence["attachLast"] = {}
        for name, width, workspace_label, model_label in scenarios:
            page.evaluate(
                """({workspaceLabel, modelLabel}) => {
                  document.getElementById('composerWorkspaceLabel').textContent = workspaceLabel;
                  document.getElementById('composerModelLabel').textContent = modelLabel;
                }""",
                {"workspaceLabel": workspace_label, "modelLabel": model_label},
            )
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(400)
            geometry = _composer_geometry(page)
            evidence["attachLast"][name] = geometry
            if geometry["controlOrder"][-1] != "btnAttach":
                failures.append(f"{name}: supported reorder did not leave Attach-last: {geometry['controlOrder']}")
            if not geometry["attachEnabled"]:
                failures.append(f"{name}: Attach unexpectedly disabled: {geometry}")
            for control in ("Attach", "Mic"):
                measured = geometry[f"{control.lower()}Width"]
                if abs(measured - 34) > 0.5:
                    failures.append(
                        f"{name}: {control} measured {measured}px rather than its declared 34px "
                        f"at natural {geometry['stage']} stage: {geometry}"
                    )
        evidence["attachLast"]["recovery"] = {}
        for name, width in (("wide1920", 1920), ("phone390", 390)):
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(400)
            geometry = _composer_geometry(page)
            evidence["attachLast"]["recovery"][name] = geometry
            expected_width = 44 if width <= 640 else 34
            for control in ("Attach", "Mic"):
                measured = geometry[f"{control.lower()}Width"]
                if abs(measured - expected_width) > 0.5:
                    failures.append(
                        f"{name} recovery: {control} measured {measured}px, expected {expected_width}px: {geometry}"
                    )

        if page_errors:
            failures.append(f"uncaught browser errors: {page_errors}")
        return failures, evidence
    finally:
        context.close()


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    # Deliberately outside ~/.hermes even when the caller's TMPDIR points into it.
    with tempfile.TemporaryDirectory(prefix="hermes-composer-tooltip-", dir="/tmp") as state_dir:
        env = os.environ.copy()
        for key in list(env):
            if key.endswith("_API_KEY"):
                env.pop(key, None)
        for key in ("API_SERVER_KEY", "HERMES_WEBUI_PASSWORD", "HERMES_CONFIG_PATH"):
            env.pop(key, None)
        env.update(
            {
                "HERMES_WEBUI_PORT": str(port),
                "HERMES_WEBUI_HOST": "127.0.0.1",
                "HERMES_WEBUI_STATE_DIR": state_dir,
                "HERMES_HOME": state_dir,
                "HERMES_BASE_HOME": state_dir,
                "HERMES_CONFIG_PATH": os.path.join(state_dir, "config.yaml"),
                "HERMES_DISABLE_LAZY_INSTALLS": "1",
                "HERMES_WEBUI_SKIP_ONBOARDING": "1",
                "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
        )
        log_path = os.path.join(state_dir, "server.log")
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [sys.executable, server_py],
                cwd=repo_root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
            )
            try:
                if not _wait_for_health(base, proc, log_path):
                    print("SETUP FAIL: server did not become healthy", file=sys.stderr)
                    log.flush()
                    with open(log_path, encoding="utf-8") as handle:
                        print(handle.read()[-3000:], file=sys.stderr)
                    return 2
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(
                        headless=True,
                        args=["--no-sandbox", "--disable-dev-shm-usage"],
                    )
                    try:
                        failures, evidence = _check(browser, base)
                    finally:
                        browser.close()
                artifact = os.environ.get("COMPOSER_TOOLTIP_ARTIFACT")
                if artifact:
                    with open(artifact, "w", encoding="utf-8") as handle:
                        json.dump(evidence, handle, indent=2)
                if failures:
                    print("COMPOSER TOOLTIP GEOMETRY FAILED", file=sys.stderr)
                    for failure in failures:
                        print(" - " + failure, file=sys.stderr)
                    print(json.dumps(evidence, indent=2), file=sys.stderr)
                    return 1
                print("COMPOSER TOOLTIP GEOMETRY PASSED: idle AX + 2x24 resize frames + 1120px shrink target")
                return 0
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
