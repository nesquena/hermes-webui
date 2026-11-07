"""Real-browser regression: provider tag must sit immediately after the model
name in the frequent-models dropdown row, with the PRIMARY badge after it and
the pin star pinned far right.

The parent confirmed the root cause in static/ui.js (two row templates place
${providerChip} AFTER the badge markup) and static/style.css (.model-opt-provider
has margin-left:auto, which pushes it away from the name). This test renders the
REAL dropdown with production static/ui.js + static/style.css and asserts the
layout via actual DOM siblings and bounding boxes on desktop and mobile widths.

Run from the repo root (state dir must live outside the production Hermes home):

  env -u HERMES_WEBUI_TEST_STATE_DIR TMPDIR=/tmp \
      python -m pytest tests/test_frequent_provider_tag_layout.py -q --tb=short
"""
import time

import pytest

from tests._pytest_port import BASE
from tests.test_models_preferences_backend import (_append_journal, _wipe_state,
                                                   _write_index, _write_sidecar,
                                                   open_composer_picker)


def _frequent_model_row_layout(page):
    """Return geometry + DOM peek for the frequent group's first model row."""
    return page.evaluate("""() => {
      const dd = document.getElementById('composerModelDropdown');
      const row = dd.querySelector('[data-group="__frequent__"] .model-opt');
      if (!row) return { missing: true };
      const top = row.querySelector('.model-opt-top');
      const children = [...top.children];
      const box = el => {
        const r = el.getBoundingClientRect();
        return { left:r.left, top:r.top, right:r.right, bottom:r.bottom,
                 width:r.width, height:r.height };
      };
      return {
        missing: false,
        classes: children.map(c => c.className || c.tagName.toLowerCase()),
        texts: children.map(c => (c.textContent||'').trim()),
        nameIsProviderNextSibling:
          (function(){
            const nameIdx = children.findIndex(c => c.classList.contains('model-opt-name'));
            const nxt = children[nameIdx + 1];
            return !!nxt && nxt.classList.contains('model-opt-provider');
          })(),
        providerBeforeBadge:
          (function () {
            const p = children.findIndex(c => c.classList.contains('model-opt-provider'));
            const b = children.findIndex(c => c.classList.contains('model-opt-badge'));
            return p >= 0 && b >= 0 && p < b;
          })(),
        starLast:
          (function () {
            const s = children.findIndex(c => c.classList.contains('model-pin-star'));
            return s === children.length - 1;
          })(),
        nameBox: box(children.find(c => c.classList.contains('model-opt-name'))),
        providerBox: box(children.find(c => c.classList.contains('model-opt-provider'))),
        badgeBox: box(children.find(c => c.classList.contains('model-opt-badge'))),
        starBox: box(children.find(c => c.classList.contains('model-pin-star'))),
        badgeText: (children.find(c => c.classList.contains('model-opt-badge'))||{}).textContent||'',
        starMarginLeftAuto:
          getComputedStyle(children.find(c => c.classList.contains('model-pin-star'))).marginLeft,
        providerMarginLeft:
          getComputedStyle(children.find(c => c.classList.contains('model-opt-provider'))).marginLeft,
        viewportW: window.innerWidth,
      };
    }""")


@pytest.mark.parametrize("mobile", [False, True])
def test_provider_tag_sits_directly_after_name_with_primary_badge_and_far_right_star(mobile):
    from playwright.sync_api import sync_playwright

    _wipe_state()
    _write_sidecar('freq-layout')
    _write_index([{'session_id': 'freq-layout', 'profile': 'default'}])
    # model-b used 3x -> lands in the frequent group.
    _append_journal('freq-layout', [
        dict(event='submitted', stream_id=f'e{i}', model='model-b',
             model_provider='custom:one', created_at=time.time() - 100)
        for i in range(3)
    ])
    catalog = {
        'active_provider': 'custom:one',
        'default_model': 'model-a',
        'configured_model_badges': {
            '@custom:one:model-b': {'role': 'primary', 'label': 'Primary',
                                    'provider': 'custom:one'},
        },
        'groups': [
            {'provider': 'One', 'provider_id': 'custom:one',
             'models': [{'id': '@custom:one:model-a', 'label': 'Model A'},
                        {'id': '@custom:one:model-b', 'label': 'Model B'}]},
            {'provider': 'Two', 'provider_id': 'custom:two',
             'models': [{'id': '@custom:two:model-a', 'label': 'Model A Two'}]},
        ],
    }
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
        page.route('**/api/models', lambda r: r.fulfill(json=catalog))
        page.route('**/api/models?*', lambda r: r.fulfill(json=catalog))
        page.route('**/api/models/live?*', lambda r: r.fulfill(json={'models': []}))
        assert page.request.post(BASE + '/api/onboarding/complete', data={}).ok
        page.goto(BASE + '/', wait_until='domcontentloaded')
        page.wait_for_function(
            "typeof renderModelDropdown==='function' && typeof api==='function'")
        page.evaluate(
            'async()=>{await populateModelDropdown();await _loadModelFrequentPrefs();}')
        page.evaluate('async()=>{await newSession(false);}')
        open_composer_picker(page)

        dd = page.locator('#composerModelDropdown')
        freq = dd.locator('[data-group="__frequent__"]')
        assert freq.locator('.model-opt').count() == 1, \
            'Expected exactly one frequent row (model-b used 3x).'
        star = freq.locator('.model-pin-star')
        star.wait_for(state='visible', timeout=3000)

        layout = _frequent_model_row_layout(page)
        assert not layout.get('missing'), 'Frequent row missing.'
        # ── Primary invariants ─────────────────────────────────────────────
        assert layout['nameIsProviderNextSibling'], (
            'provider tag must be the immediate nextElementSibling of the name. '
            f'row children (cls,text): {list(zip(layout["classes"], layout["texts"], strict=False))}'
        )
        assert layout['providerBeforeBadge'], (
            'PRIMARY badge must render AFTER the provider tag. '
            f'row children (cls,text): {list(zip(layout["classes"], layout["texts"], strict=False))}'
        )
        assert 'primary' in layout['badgeText'].lower(), (
            f'Expected a PRIMARY badge, got badge text={layout["badgeText"]!r}'
        )
        assert layout['starLast'], (
            'Pin star must remain the last element (far right). '
            f'row children: {layout["classes"]}'
        )

        # ── Geometry: provider adjacent to name (gap<=10px when same row) ──
        name_b, prov_b = layout['nameBox'], layout['providerBox']
        same_row = abs(name_b['top'] - prov_b['top']) < 2
        if same_row:
            gap = prov_b['left'] - name_b['right']
            assert 0 <= gap <= 10, (
                f'Provider tag gap from name is {gap}px (must be <=10px, i.e. the '
                f'flex 8px gap with no margin-left:auto). '
                f'name={name_b} provider={prov_b} providerMarginLeft={layout["providerMarginLeft"]}'
            )
            assert layout['providerMarginLeft'] in ('0px', '0'), (
                'providerChip must NOT carry margin-left:auto (that pushed it '
                f'far right in the screenshot). got margin-left={layout["providerMarginLeft"]}'
            )

        # ── Geometry: star stays far right ────────────────────────────────
        star_b, prov_b = layout['starBox'], layout['providerBox']
        assert star_b['right'] > prov_b['right'] + 40, (
            'Star must be clearly separated to the far right of the provider tag. '
            f'star.right={star_b["right"]} provider.right={prov_b["right"]}'
        )
        assert star_b['right'] <= layout['viewportW'] + 1, (
            f'star must stay inside the viewport got right={star_b["right"]} '
            f'viewport={layout["viewportW"]}'
        )

        assert not errors, errors
        browser.close()