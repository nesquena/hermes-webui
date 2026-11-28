"""Selecting a model that is already pinned into the frequent group must NOT
auto-expand that model's original provider group.

The frequent group already surfaces the active model at the top, so expanding
its provider group again duplicates the row and pushes the rest of the list
down. Groups still auto-expand for a selection that is NOT in the frequent
group (upstream behaviour, guarded below so the fix cannot over-remove it).
"""
import time

import pytest

from tests.test_models_preferences_backend import open_composer_picker

from tests._pytest_port import BASE
from tests.test_models_preferences_backend import (_wipe_state, _write_sidecar,
                                                   _write_index, _append_journal)


def _group_state(page, group_key):
    return page.evaluate("""(key) => {
      const dd = document.getElementById('composerModelDropdown');
      const body = dd.querySelector(`[data-group="${key}"]`);
      if (!body) return { missing: true };
      const heading = body.previousElementSibling;
      return {
        missing: false,
        display: body.style.display || '',
        headingOpen: !!(heading && heading.classList.contains('open')),
      };
    }""", group_key)


def _open_picker(page, catalog, select_value):
    assert page.request.post(BASE + '/api/onboarding/complete', data={}).ok
    page.route('**/api/models', lambda r: r.fulfill(json=catalog))
    page.route('**/api/models?*', lambda r: r.fulfill(json=catalog))
    page.route('**/api/models/live?*', lambda r: r.fulfill(json={'models': []}))
    page.goto(BASE + '/', wait_until='domcontentloaded')
    page.wait_for_function(
        "typeof renderModelDropdown==='function' && typeof api==='function'")
    page.evaluate(
        'async()=>{await populateModelDropdown();await _loadModelFrequentPrefs();}')
    page.evaluate('async()=>{await newSession(false);}')
    # Let any pending model-sync settle: the app can rewrite the select value from
    # ambient config a moment after the session opens, which would otherwise race
    # the fixture selection pinned below.
    page.wait_for_timeout(1200)
    # Pin the active selection to the fixture's exact qualified id, then re-render,
    # so the expansion rule under test depends only on the code being tested and
    # not on whatever model the app happened to resolve from ambient state.
    page.evaluate('''(v)=>{
      const s=document.getElementById('modelSelect');
      s.value=v;
      // Align the ambient session with the fixture selection as well: a previous
      // test in the same process can leave S.session pointing at another model,
      // and the expansion rule consults both the select value and the session.
      if(window.S&&S.session){
        const m=/^@([^:]+):(.+)$/.exec(v);
        S.session.model=m?m[2]:v;
        S.session.model_provider=m?m[1]:null;
      }
      renderModelDropdown();
    }''', select_value)
    assert page.evaluate("document.getElementById('modelSelect').value") == select_value
    open_composer_picker(page)
    page.wait_for_function(
        "() => !!document.querySelector('#composerModelDropdown [data-group=\"__frequent__\"] .model-opt')")


def _catalog(selected_provider='custom:one', selected='model-b'):
    return {
        'active_provider': selected_provider,
        'default_model': selected,
        'configured_model_badges': {},
        'groups': [
            {'provider': 'One', 'provider_id': 'custom:one',
             'models': [{'id': '@custom:one:model-a', 'label': 'Model A'},
                        {'id': '@custom:one:model-b', 'label': 'Model B'}]},
            {'provider': 'Two', 'provider_id': 'custom:two',
             'models': [{'id': '@custom:two:model-c', 'label': 'Model C'}]},
        ],
    }


@pytest.mark.parametrize('mobile', [False, True])
def test_selected_frequent_model_does_not_expand_its_provider_group(mobile):
    from playwright.sync_api import sync_playwright

    _wipe_state()
    _write_sidecar('freq-nofolder')
    _write_index([{'session_id': 'freq-nofolder', 'profile': 'default'}])
    _append_journal('freq-nofolder', [
        dict(event='submitted', stream_id=f'e{i}', model='model-b',
             model_provider='custom:one', created_at=time.time() - 100)
        for i in range(3)
    ])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(
            bypass_csp=True,
            viewport={'width': 390 if mobile else 1280,
                      'height': 844 if mobile else 900},
            is_mobile=mobile, has_touch=mobile)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        _open_picker(page, _catalog(), '@custom:one:model-b')

        dd = page.locator('#composerModelDropdown')
        assert dd.locator('[data-group="__frequent__"] .model-opt').count() == 1

        state = _group_state(page, 'custom:one')
        assert not state.get('missing'), 'The custom:one group must be rendered.'
        assert state['display'] == 'none' and not state['headingOpen'], (
            'A selection that is already pinned in the frequent group must not '
            f'auto-expand its provider group. got {state}'
        )
        assert not errors, errors
        browser.close()


def test_selection_outside_frequent_group_still_expands_its_group():
    """Guard against over-removal: upstream behaviour must survive when the
    active model is not one of the frequent shortcuts."""
    from playwright.sync_api import sync_playwright

    _wipe_state()
    _write_sidecar('freq-ctrl')
    _write_index([{'session_id': 'freq-ctrl', 'profile': 'default'}])
    # Frequent group holds model-b, but model-c (custom:two) is selected.
    _append_journal('freq-ctrl', [
        dict(event='submitted', stream_id=f'e{i}', model='model-b',
             model_provider='custom:one', created_at=time.time() - 100)
        for i in range(4)
    ])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(bypass_csp=True,
                                viewport={'width': 1280, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        _open_picker(page, _catalog(selected_provider='custom:two',
                                    selected='@custom:two:model-c'),
                     '@custom:two:model-c')

        state = _group_state(page, 'custom:two')
        assert not state.get('missing')
        assert state['display'] != 'none' and state['headingOpen'], (
            'A selection NOT in the frequent group must still expand its own '
            f'group (upstream behaviour). got {state}'
        )
        assert not errors, errors
        browser.close()
