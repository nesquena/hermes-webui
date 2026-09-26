"""Executed width/persistence/table contracts using real Chromium DOM and CSS.

No agent or user state: route the component shell and assets from this checkout.
Run with ./scripts/test.sh tests/test_content_width_browser.py.
"""
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def function(source, name):
    start = source.index('function ' + name + '(')
    end = source.index('\n}', start) + 2
    return source[start:end]


@pytest.fixture(scope='module')
def browser():
    pw = pytest.importorskip('playwright.sync_api')
    with pw.sync_playwright() as driver:
        instance = driver.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={'width': 1600, 'height': 1000})
    tab = context.new_page()
    html = (ROOT / 'static/index.html').read_text()
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)

    def route(request):
        path = request.request.url.split('width.test/', 1)[-1].split('?', 1)[0]
        if path == '':
            request.fulfill(body=html, content_type='text/html')
        elif path.startswith('static/') and (ROOT / path).is_file():
            request.fulfill(path=str(ROOT / path))
        else:
            request.fulfill(status=204)

    tab.route('**/*', route)
    tab.goto('http://width.test/')
    tab.evaluate("""() => {
      window.$ = id => document.getElementById(id);
      window._setButtonTooltip = (button,label) => button.dataset.tooltip=label;
      window._scheduleAppearanceAutosave = () => {};
      window._setAppearanceAutosaveStatus = state => window.saveStatus=state;
      window._ensureComposerControlVisibilityState = () => {};
      window._renderComposerControlChips = () => {};
      window._renderComposerSituationalControlChips = () => {};
      window._enqueueSettingsPost = async () => ({});
      window.api = async () => ({});
    }""")
    boot = (ROOT / 'static/boot.js').read_text()
    tab.add_script_tag(content=boot[boot.index('const _CONTENT_WIDTH_MODES='):boot.index('function _buildSkinPicker')])
    panels = (ROOT / 'static/panels.js').read_text()
    tab.add_script_tag(content='let _settingsThemeOnOpen, _settingsSkinOnOpen, _settingsFontSizeOnOpen, _settingsAppearanceAutosaveRetryPayload;\n' + function(panels, '_rememberAppearanceSaved') + '\nasync ' + function(panels, '_autosaveAppearanceSettings'))
    yield tab
    context.close()


def reconcile(page, server):
    boot = (ROOT / 'static/boot.js').read_text()
    block = boot[boot.index('    const serverContentWidth='):boot.index("    if(typeof setLocale", boot.index('    const serverContentWidth='))]
    page.evaluate('(s) => {' + block + '}', {'content_width': server})


@pytest.mark.parametrize('response', ['failure', 'partial'])
def test_explicit_default_survives_unsaved_reconciliation(page, response):
    page.evaluate("_pickContentWidth('wide')")
    page.evaluate("_pickContentWidth('default')")
    page.evaluate("""async response => {
      _enqueueSettingsPost = async () => {
        if(response==='failure') throw new Error('offline');
        return {theme:'dark'};
      };
      await _autosaveAppearanceSettings({content_width:'default'});
    }""", response)
    reconcile(page, 'wide')
    assert page.evaluate("localStorage.getItem('hermes-content-width')") == 'default'
    assert page.locator('html').get_attribute('data-content-width') is None
    assert page.locator('[data-content-width-value="default"]').get_attribute('aria-selected') == 'true'


def test_older_save_does_not_revert_newer_selection(page):
    page.evaluate("""() => {
      _pickContentWidth('wide');
      _enqueueSettingsPost = () => new Promise(resolve => window.finishSave=resolve);
      window.pendingSave = _autosaveAppearanceSettings({content_width:'wide'});
      _pickContentWidth('full');
      finishSave({content_width:'wide'});
    }""")
    page.evaluate('pendingSave')
    assert page.evaluate("localStorage.getItem('hermes-content-width')") == 'full'
    assert page.locator('html').get_attribute('data-content-width') == 'full'


@pytest.mark.parametrize(('local', 'server', 'expected'), [
    (None, 'wide', 'wide'), ('full', 'wide', 'full'),
    ('default', 'full', 'default'), ('invalid', 'wide', 'default'),
])
def test_boot_normalizes_missing_and_explicit_preferences(page, local, server, expected):
    if local is not None:
        page.evaluate("value => localStorage.setItem('hermes-content-width',value)", local)
    reconcile(page, server)
    assert page.evaluate("localStorage.getItem('hermes-content-width')") == expected


def test_successful_save_projects_validated_width(page):
    page.evaluate("""async () => {
      _pickContentWidth('full');
      _enqueueSettingsPost = async () => ({content_width:'default'});
      await _autosaveAppearanceSettings({content_width:'full'});
    }""")
    assert page.evaluate("localStorage.getItem('hermes-content-width')") == 'default'
    assert page.locator('html').get_attribute('data-content-width') is None
    assert page.evaluate('saveStatus') == 'saved'


def test_reconciliation_rejection_is_handled(page):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.evaluate("() => {_pickContentWidth('default'); api = async () => {throw new Error('offline')};}")
    reconcile(page, 'wide')
    page.evaluate('() => new Promise(resolve => setTimeout(resolve, 20))')
    assert errors == []
    assert page.evaluate("localStorage.getItem('hermes-content-width')") == 'default'


@pytest.mark.parametrize('width', [1600, 900, 390])
def test_picker_pointer_keyboard_and_actual_geometry(page, width):
    page.set_viewport_size({'width': width, 'height': 1000})
    button = page.locator('#composerContentWidthBtn')
    # The shared icon-button transition animates dimensions after viewport resize.
    page.wait_for_function("""minimum =>
        document.getElementById('composerContentWidthBtn').getBoundingClientRect().width >= minimum
    """, arg=44 if width <= 640 else 34)
    box = button.bounding_box()
    assert box and box['width'] >= (44 if width <= 640 else 34)
    assert page.locator('#composerContentWidthBtn svg').bounding_box()['width'] > 0
    evidence = os.getenv('WIDTH_SCREENSHOT_DIR')
    if evidence:
        Path(evidence).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(evidence) / f'{width}-closed.png'))
    page.evaluate("""() => {
      document.getElementById('msgInner').innerHTML='<div class="msg assistant"><div class="msg-body"><p>'+('Readable prose with emoji 😀 and inline content. '.repeat(20))+'</p></div></div>';
    }""")
    widths = {}
    for mode in ['wide', 'full', 'default']:
        button.click()
        popup = page.locator('#composerContentWidthPopup')
        rect = popup.bounding_box()
        assert rect and 0 <= rect['x'] and rect['x'] + rect['width'] <= width
        assert rect['y'] + rect['height'] <= box['y']
        page.locator(f'[data-content-width-value="{mode}"]').click()
        assert page.evaluate("localStorage.getItem('hermes-content-width')") == mode
        assert not popup.is_visible()
        widths[mode] = page.locator('#msgInner').bounding_box()['width']
    assert widths['default'] <= widths['wide'] <= widths['full']
    if width == 1600:
        assert widths['default'] < widths['wide']
    button.click()
    if evidence:
        page.screenshot(path=str(Path(evidence) / f'{width}-open.png'))
    page.keyboard.press('ArrowRight')
    page.keyboard.press('Enter')
    assert page.locator('html').get_attribute('data-content-width') == 'wide'
    assert not page.locator('#composerContentWidthPopup').is_visible()
    assert button.evaluate('(el) => el===document.activeElement')
    button.click()
    page.keyboard.press('Escape')
    assert button.evaluate('(el) => el===document.activeElement')
    button.click()
    page.mouse.click(width / 2, 100)
    assert not page.locator('#composerContentWidthPopup').is_visible()


@pytest.mark.parametrize('width', [1600, 900, 390])
def test_unbroken_prose_wraps_without_changing_table_or_code_scroll(page, width):
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("""() => {
      const body=document.createElement('div');
      body.className='msg-body'; body.id='wrapFixture';
      body.innerHTML=['h1','h2','h3','h4','h5','h6','p','blockquote','li'].map(tag =>
        `<${tag}><a>${'abcdef'.repeat(65)}</a></${tag}>`).join('') +
        `<pre><code>${'abcdef'.repeat(65)}</code></pre>` +
        `<div class="md-table-scroll"><table><tr><th>${'abcdef'.repeat(65)}</th></tr></table></div>`;
      document.getElementById('msgInner').appendChild(body);
    }""")
    result = page.evaluate("""() => {
      const body=document.querySelector('#wrapFixture');
      return {
        prose:[...body.querySelectorAll('h1,h2,h3,h4,h5,h6,p,blockquote,li,a')].map(el => ({
          tag:el.tagName, width:el.scrollWidth, client:el.clientWidth})),
        table:body.querySelector('.md-table-scroll').scrollWidth > body.querySelector('.md-table-scroll').clientWidth,
        code:getComputedStyle(body.querySelector('pre')).overflowX
      };
    }""")
    assert all(el['width'] <= el['client'] + 2 for el in result['prose']), result
    assert result['table'] and result['code'] in ('auto', 'scroll', 'hidden'), result


def test_locale_switch_updates_width_control_text_and_accessible_names(page):
    page.add_script_tag(content=(ROOT / 'static/i18n.js').read_text())
    page.evaluate("_pickContentWidth('wide')")
    for locale in ('en', 'fr', 'de', 'ja', 'en'):
        page.evaluate("lang => { setLocale(lang); applyLocaleToDOM(); }", locale)
        expected = page.evaluate("""() => ({
          button:t('content_width_current',t('content_width_wide')),
          list:t('content_width_label'), modes:['default','wide','full'].map(mode =>
            [t('content_width_'+mode),t('content_width_'+mode+'_aria')])
        })""")
        assert page.locator('#composerContentWidthBtn').get_attribute('aria-label') == expected['button']
        assert page.locator('#composerContentWidthBtn').get_attribute('data-tooltip') == expected['button']
        assert page.locator('#composerContentWidthPopup').get_attribute('aria-label') == expected['list']
        for mode, (text, label) in zip(('default', 'wide', 'full'), expected['modes'], strict=True):
            option = page.locator(f'[data-content-width-value="{mode}"]')
            assert option.locator('span').inner_text() == text
            assert option.get_attribute('aria-label') == label
    assert page.evaluate("LOCALES.en.content_width_default !== LOCALES.fr.content_width_default")
    assert page.evaluate("LOCALES.en.content_width_label !== LOCALES.de.content_width_label")
    assert page.evaluate("LOCALES.en.content_width_default_aria !== LOCALES.de.content_width_default_aria")


def test_width_locales_have_complete_independent_labels(page):
    page.add_script_tag(content=(ROOT / 'static/i18n.js').read_text())
    assert page.evaluate("""() => Object.values(LOCALES).every(locale =>
      ['content_width_label','content_width_current',...['default','wide','full'].flatMap(mode =>
        ['content_width_'+mode,'content_width_'+mode+'_aria'])].every(key =>
          typeof locale[key]==='string' && locale[key].length>0))""")


def test_tables_enhance_idempotently_sort_filter_and_scroll(page):
    source = (ROOT / 'static/messages.js').read_text()
    page.add_script_tag(content='\n'.join(function(source, name) for name in ['enhanceMarkdownTables', '_markdownTableText', '_markdownTableCellText']))
    page.evaluate("""() => {
      const root=document.createElement('div'); root.id='tableFixture';
      root.className='msg-body'; root.style='position:fixed;top:20px;left:20px;width:300px';
      root.innerHTML='<table><thead><tr><th>Name</th><th>Value</th></tr></thead><tbody>'+[4,2,3,1].map(n=>'<tr><td>Item '+n+'</td><td>'+('long value '.repeat(30))+'</td></tr>').join('')+'</tbody></table>';
      document.body.appendChild(root);
      window.originalTable=root.querySelector('table');
      enhanceMarkdownTables(document); enhanceMarkdownTables(document);
    }""")
    assert page.locator('#tableFixture .md-table-scroll').count() == 1
    assert page.locator('#tableFixture .markdown-table-filter').count() == 1
    assert page.evaluate("originalTable===document.querySelector('#tableFixture table')")
    page.locator('#tableFixture .markdown-table-sort').first.click()
    assert page.locator('#tableFixture tbody tr').first.inner_text().startswith('Item 1')
    page.locator('#tableFixture .markdown-table-filter').fill('Item 3')
    assert page.locator('#tableFixture tbody tr:visible').count() == 1
    assert page.locator('#tableFixture .md-table-scroll').evaluate('(el) => el.scrollWidth>el.clientWidth')
    assert page.locator('#tableFixture .md-table-scroll').bounding_box()['width'] <= 300
    assert 'Noto Color Emoji' in page.locator('#tableFixture').evaluate('(el) => getComputedStyle(el).fontFamily')
    assert page.evaluate("async () => (await document.fonts.load('16px \"Noto Color Emoji\"', '😀')).length > 0")
