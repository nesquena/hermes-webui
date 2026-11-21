from pathlib import Path
import pytest

UI = Path(__file__).parents[1] / 'static/ui.js'

@pytest.fixture
def browser_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox','--disable-dev-shm-usage'])
        page = browser.new_page()
        page.set_content('<div id="composerModelDropdown"></div>')
        helpers=UI.read_text().split('// ── Frequently-used models + server-backed pin state')[1].split('function renderModelDropdown(){')[0]
        page.add_script_tag(content="// extracted production helper block\nconst S={activeProfile:'default'}; const $=id=>document.getElementById(id); const t=k=>k; const showToast=()=>{};\n//"+helpers)
        yield page
        browser.close()


def test_exact_identity_ranking_and_profile_cache(browser_page):
    page=browser_page
    result=page.evaluate('''() => {
      const data=['a','b','c'].map(value=>({value,providerId:'p'}));
      const cache={pins:[],usage:[{model:'a',provider:'p',count:2,last_used:9},
       {model:'b',provider:'p',count:2,last_used:100},{model:'c',provider:'p',count:1,last_used:101}]};
      return {rows:_computeFrequentEntries(data,cache).rows.map(m=>m.value),
        distinct:_frequentModelKey('M','P')!==_frequentModelKey('M','p')};
    }''')
    assert result == {'rows':['b','a','c'],'distinct':True}
    assert page.evaluate("() => {_modelFrequentCache={profile:'default',pins:[{model:'a',provider:'p'}]};S.activeProfile='work';return _frequentIsPinned('a','p');}") is False


def test_failed_pin_never_marks_saved(browser_page):
    page=browser_page
    page.evaluate('''() => {
       _modelFrequentCache={profile:'default',pins:[],usage:[]};
       window.api=async()=>{throw Error('disk unavailable')};
       window.li=()=>'*'; window.esc=s=>s;
       const row=document.createElement('div'); const m={value:'a',providerId:'p'};
       row.innerHTML=_rowPinStarHtml(m);_bindRowStar(row,m);document.body.appendChild(row);
    }''')
    page.locator('.model-pin-star').click()
    page.wait_for_timeout(100)
    assert page.locator('.model-pin-star').get_attribute('aria-checked') == 'false'


def test_stale_profile_get_cannot_populate_new_profile(browser_page):
    page=browser_page
    assert page.evaluate('''async () => {
      window.api=()=>new Promise(resolve=>window.finish=resolve);
      const pending=_loadModelFrequentPrefs(); S.activeProfile='work';
      finish({profile:'default',pins:[{model:'secret',provider:'p'}],usage:[]}); await pending;
      return _modelFrequentCache;
    }''') is None
