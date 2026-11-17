"""The quota chip window select follows the "Show provider quota chip" toggle.

The choice only matters while the chip is shown, so the select is disabled
while the toggle is off, using the same dependent-control pattern as the
other Settings toggles. The saved choice is kept either way.
"""

import time

import pytest

from tests._pytest_port import BASE


_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
_STATE = (
    "() => ({toggle: document.getElementById('settingsShowQuotaChip').checked,"
    " disabled: document.getElementById('settingsQuotaChipWindow').disabled,"
    " value: document.getElementById('settingsQuotaChipWindow').value})"
)


def _wait_js(page, expr, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.evaluate("() => (" + expr + ")"):
            return True
        page.wait_for_timeout(100)
    return False


def _set_toggle(page, checked):
    page.evaluate(
        "(checked) => { const cb = document.getElementById('settingsShowQuotaChip');"
        " cb.checked = checked; cb.dispatchEvent(new Event('change')); }",
        checked,
    )
    return page.evaluate(_STATE)


def test_quota_chip_window_select_is_disabled_while_the_chip_toggle_is_off():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            # Keep this page from saving preferences or fetching quota on the shared test server.
            page.route("**/api/settings", lambda route: route.fulfill(status=200, body="{}")
                       if route.request.method == "POST" else route.continue_())
            page.goto(BASE + "/", wait_until="domcontentloaded")
            assert _wait_js(page, "typeof loadSettingsPanel==='function' && typeof S!=='undefined' && S._bootReady")
            page.evaluate(
                "() => { window._schedulePreferencesAutosave = () => {};"
                " window.refreshProviderQuotaIndicator = () => Promise.resolve(); }"
            )
            page.evaluate("() => loadSettingsPanel()")
            assert _wait_js(page, "document.getElementById('settingsQuotaChipWindow').value!==''")

            initial = page.evaluate(_STATE)
            assert initial["disabled"] is (not initial["toggle"]), initial

            on = _set_toggle(page, True)
            assert on["toggle"] is True and on["disabled"] is False, on
            page.evaluate(
                "() => { const sel = document.getElementById('settingsQuotaChipWindow');"
                " sel.value = 'shortest'; sel.dispatchEvent(new Event('change')); }"
            )
            off = _set_toggle(page, False)
            assert off == {"toggle": False, "disabled": True, "value": "shortest"}, off
            again = _set_toggle(page, True)
            assert again == {"toggle": True, "disabled": False, "value": "shortest"}, again

            # Reopening Settings keeps following the toggle.
            page.evaluate("() => loadSettingsPanel()")
            page.wait_for_timeout(300)
            off_after_reopen = _set_toggle(page, False)
            assert off_after_reopen["disabled"] is True, off_after_reopen
            assert not errors, errors
        finally:
            browser.close()
