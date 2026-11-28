"""Escape after clicking a pin star must close ONLY the model picker (#8622).

The pin star deliberately keeps focus on the (rebuilt) child button after a
mouse click.  The picker's keydown listener used to ignore every event whose
target was not the dropdown container itself, so Escape from the focused star
bubbled to the document-level handler: the composer picker stayed open while
the session filter was cleared, and in Settings the whole Settings panel closed
instead of just the picker.  The picker now consumes Escape at its own boundary
for descendants and routes it through the owning ``closeDropdown()``, which
returns focus to the chip.

These are full-app regressions: a real isolated WebUI server, the real page,
an ordinary mouse click on the star, a real preferences POST, and a real
Escape keypress.  The model catalog is the only mocked response; the
preferences API and its persistence stay real.
"""

from __future__ import annotations

import time

from tests._pytest_port import BASE
from tests.test_models_preferences_backend import (
    _append_journal,
    _wipe_state,
    _write_index,
    _write_sidecar,
    open_composer_picker,
)

CATALOG = {
    'active_provider': 'custom:one',
    'default_model': 'model-a',
    'configured_model_badges': {},
    'groups': [
        {
            'provider': 'One',
            'provider_id': 'custom:one',
            'models': [
                {'id': '@custom:one:model-a', 'label': 'Model A'},
                {'id': '@custom:one:model-b', 'label': 'Model B'},
            ],
        },
        {
            'provider': 'Two',
            'provider_id': 'custom:two',
            'models': [{'id': '@custom:two:model-a', 'label': 'Model A Two'}],
        },
    ],
}


def _seed(sid: str = 'frequent-escape') -> None:
    _wipe_state()
    _write_sidecar(sid)
    _write_index([{'session_id': sid, 'profile': 'default'}])
    # Usage history makes the frequent group render at the top of the picker,
    # so the star under test is reachable without expanding a provider group.
    _append_journal(sid, [
        dict(event='submitted', stream_id=f'e{i}', model='model-b',
             provider='custom:one', created_at=time.time() - 100)
        for i in range(3)
    ])


def _open_app(browser):
    page = browser.new_page(bypass_csp=True, viewport={'width': 1280, 'height': 900})
    errors: list[str] = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.route('**/api/models', lambda r: r.fulfill(json=CATALOG))
    page.route('**/api/models?*', lambda r: r.fulfill(json=CATALOG))
    page.route('**/api/models/live?*', lambda r: r.fulfill(json={'models': []}))
    assert page.request.post(BASE + '/api/onboarding/complete', data={}).ok
    page.goto(BASE + '/', wait_until='domcontentloaded')
    page.wait_for_function(
        "typeof renderModelDropdown==='function' && typeof api==='function'")
    page.evaluate(
        'async()=>{await populateModelDropdown();await _loadModelFrequentPrefs();}')
    return page, errors


def _click_star_and_wait_saved(page, star) -> tuple[str, str]:
    """Ordinary mouse click, wait for the real POST, require focus to follow it."""
    model = star.get_attribute('data-pin-model')
    provider = star.get_attribute('data-pin-provider')
    assert model and provider, 'the star must carry its model+provider identity'
    # The server stores the canonical bare model (the star's data attribute may
    # be the provider-qualified form), so compare on the normalized identity.
    prefix = f'@{provider}:'
    bare = model[len(prefix):] if model.startswith(prefix) else model
    star.click()
    page.wait_for_function(
        "([m,p]) => Array.isArray(_modelFrequentCache && _modelFrequentCache.pins)"
        " && _modelFrequentCache.pins.some(x => x.model===m && x.provider===p)",
        arg=[bare, provider],
        timeout=10000,
    )
    # The save rebuilt the picker; focus must have followed the SAME star.
    page.wait_for_function(
        "([m,p]) => {const a=document.activeElement;"
        " return !!a && a.classList.contains('model-pin-star')"
        " && a.getAttribute('data-pin-model')===m"
        " && a.getAttribute('data-pin-provider')===p;}",
        arg=[model, provider],
        timeout=10000,
    )
    return model, provider


def test_escape_on_a_clicked_star_closes_only_the_composer_picker():
    from playwright.sync_api import sync_playwright

    _seed()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page, errors = _open_app(browser)
            page.evaluate('async()=>{await newSession(false);}')
            open_composer_picker(page)
            # Opening the composer picker clears the session filter as part of its
            # own open path, so set it afterwards: the global Escape handler would
            # clear THIS value if the star let the key escape.
            page.fill('#sessionSearch', 'frequent-escape')
            assert page.evaluate(
                "()=>document.getElementById('sessionSearch').value") == 'frequent-escape'

            dd = page.locator('#composerModelDropdown')
            star = dd.locator('.model-pin-star').first
            star.wait_for(state='visible', timeout=5000)
            _click_star_and_wait_saved(page, star)

            assert dd.evaluate("el=>el.classList.contains('open')"), \
                'clicking a star must not close the picker'
            page.keyboard.press('Escape')
            page.wait_for_function(
                "()=>!document.getElementById('composerModelDropdown')"
                ".classList.contains('open')", timeout=5000)

            assert page.evaluate(
                "()=>document.getElementById('sessionSearch').value") == 'frequent-escape', \
                'Escape must be consumed by the picker, not clear the session filter'
            assert page.evaluate(
                "()=>document.activeElement && document.activeElement.id") == \
                'composerModelChip', 'Escape must return focus to the owning chip'
            assert not errors, errors
        finally:
            browser.close()


def test_escape_on_a_clicked_star_closes_only_the_settings_picker():
    from playwright.sync_api import sync_playwright

    _seed()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page, errors = _open_app(browser)
            page.evaluate('async()=>{await newSession(false);}')

            page.click(".rail-btn[data-panel='settings']")
            page.wait_for_function(
                "()=>typeof _currentPanel!=='undefined' && _currentPanel==='settings'")
            page.click("[data-settings-section='preferences']")
            page.wait_for_function(
                "()=>{const s=document.getElementById('settingsModel');"
                " return !!s && s.options.length>0;}")

            page.click('#settingsModelChip')
            page.wait_for_function(
                "()=>document.getElementById('settingsModelDropdown')"
                ".classList.contains('open')")
            star = page.locator('#settingsModelDropdown .model-pin-star').first
            star.wait_for(state='visible', timeout=5000)
            _click_star_and_wait_saved(page, star)

            assert page.evaluate(
                "()=>document.getElementById('settingsModelDropdown')"
                ".classList.contains('open')"), \
                'clicking a star must not close the Settings picker'
            page.keyboard.press('Escape')
            page.wait_for_function(
                "()=>!document.getElementById('settingsModelDropdown')"
                ".classList.contains('open')", timeout=5000)

            state = page.evaluate("""()=>({
              panel: (typeof _currentPanel==='undefined') ? null : _currentPanel,
              panelVisible: !!document.getElementById('panelSettings')
                && getComputedStyle(document.getElementById('panelSettings')).display!=='none',
              active: document.activeElement && document.activeElement.id,
            })""")
            assert state['panel'] == 'settings', \
                'Escape must close the picker, not the whole Settings panel'
            assert state['panelVisible'] is True, state
            assert state['active'] == 'settingsModelChip', \
                'Escape must return focus to the Settings model chip'
            assert not errors, errors
        finally:
            browser.close()
