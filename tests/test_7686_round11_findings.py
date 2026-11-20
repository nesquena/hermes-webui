"""Regression coverage for the 2026-11-18 #7686 review finding.

At desktop widths where Stop fits but Interrupt does not, the fitter collapses
the send button to its icon stage (``cf-action-icon``). Clearing the draft
changes the action back to Stop, but ``updateSendBtn()`` neither cleared that
class nor scheduled another fit — the 34 px button does not change
``.composer-left``'s width, so its ResizeObserver never rechecks and the label
stays hidden.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except Exception:  # pragma: no cover - optional in dev envs
    PlaywrightError = None
    sync_playwright = None


REPO = Path(__file__).resolve().parents[1]
_UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _extract_update_send_btn() -> str:
    start = _UI_JS.index("function updateSendBtn(){")
    end = _UI_JS.index("\nasync function handleComposerPrimaryAction", start)
    return _UI_JS[start:end]


_FIXTURE = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<button class="send-btn" id="btnSend" data-action="interrupt">
  <span class="send-btn-label" id="btnSendLabel">Interrupt</span>
</button>
<script>
window.__scheduled = 0;
window.__action = 'interrupt';
window.S = {};
window.$ = (id) => id === 'btnSend' ? document.getElementById('btnSend') : null;
window.t = (key) => key;
window.getComposerPrimaryAction = () => window.__action;
window._setComposerPrimaryButtonIcon = () => {};
window._applyBusyComposerPlaceholder = () => {};
window._scheduleComposerFit = () => { window.__scheduled += 1; };
window.requestAnimationFrame = (cb) => { cb(); };
__UPDATE_SEND_BTN__
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def page():
    if sync_playwright is None:
        pytest.skip("playwright is unavailable")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        except PlaywrightError as exc:
            if "Executable doesn't exist at" in str(exc):
                pytest.skip("playwright chromium executable is unavailable")
            raise
        ctx = browser.new_context(viewport={"width": 1280, "height": 600})
        pg = ctx.new_page()
        pg.set_content(_FIXTURE.replace("__UPDATE_SEND_BTN__", _extract_update_send_btn()))
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


_DRIVER = """() => {
  const btn = document.getElementById('btnSend');
  // Busy with Interrupt: simulate the fitter's overflow collapse.
  window.__action = 'interrupt';
  updateSendBtn();
  btn.classList.add('cf-action-icon');
  const collapsedBefore = btn.classList.contains('cf-action-icon');
  const scheduledBefore = window.__scheduled;
  // Clearing the draft switches the action to the shorter Stop label.
  window.__action = 'stop';
  updateSendBtn();
  const collapsedAfter = btn.classList.contains('cf-action-icon');
  const scheduledAfter = window.__scheduled;
  // Control: an unchanged action must not churn the collapse or re-fit.
  btn.classList.add('cf-action-icon');
  const scheduledStable = window.__scheduled;
  updateSendBtn();
  return {
    collapsedBefore,
    collapsedAfter,
    scheduled: scheduledAfter - scheduledBefore,
    stableCollapsed: btn.classList.contains('cf-action-icon'),
    stableScheduled: window.__scheduled - scheduledStable,
  };
}"""


def test_shorter_action_clears_the_icon_collapse_and_reschedules_the_fit(page):
    result = page.evaluate(_DRIVER)
    assert result["collapsedBefore"] is True
    assert result["collapsedAfter"] is False, (
        "switching Interrupt -> Stop must clear the icon-only collapse"
    )
    assert result["scheduled"] >= 1, (
        "the shorter label needs a fresh fit; the collapsed button never "
        "resizes .composer-left"
    )
    assert result["stableCollapsed"] is True
    assert result["stableScheduled"] == 0, (
        "an unchanged action must not clear the collapse or re-fit"
    )
