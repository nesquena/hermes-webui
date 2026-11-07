"""Full WebUI + real preferences API + Chromium, no live LLM requests."""
import time
import pytest
from tests._pytest_port import BASE
from tests.test_models_preferences_backend import (_append_journal, _wipe_state,
                                                  _write_index, _write_sidecar,
                                                  open_composer_picker)


@pytest.mark.parametrize("mobile", [False, True])
def test_real_page_pin_refresh_search_unpin_and_provider_selection(mobile):
    from playwright.sync_api import sync_playwright
    _wipe_state()
    _write_sidecar('frequent-e2e')
    _write_index([{'session_id':'frequent-e2e','profile':'default'}])
    _append_journal('frequent-e2e', [dict(event='submitted',stream_id=f'e{i}',model='model-b',
                                      model_provider='custom:one',created_at=time.time()-100) for i in range(3)])
    catalog={'active_provider':'custom:one','default_model':'model-a','configured_model_badges':{},'groups':[
        {'provider':'One','provider_id':'custom:one','models':[{'id':'@custom:one:model-a','label':'Model A'},
                                                                  {'id':'@custom:one:model-b','label':'Model B'}]},
        {'provider':'Two','provider_id':'custom:two','models':[{'id':'@custom:two:model-a','label':'Model A Two'}]}]}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(bypass_csp=True, viewport={"width":390 if mobile else 1280,"height":844 if mobile else 900}, is_mobile=mobile, has_touch=mobile)
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('**/api/models',lambda r:r.fulfill(json=catalog))
        page.route('**/api/models?*',lambda r:r.fulfill(json=catalog))
        page.route('**/api/models/live?*',lambda r:r.fulfill(json={'models':[]}))
        assert page.request.post(BASE+'/api/onboarding/complete',data={}).ok
        page.goto(BASE+'/',wait_until='domcontentloaded')
        page.wait_for_function("typeof renderModelDropdown==='function' && typeof api==='function'")
        # Catalog injected via the real public loader; preferences API remains real.
        page.evaluate('async()=>{await populateModelDropdown();await _loadModelFrequentPrefs();}')
        page.evaluate('async()=>{await newSession(false);}')
        open_composer_picker(page)
        dd=page.locator('#composerModelDropdown')
        freq=dd.locator('[data-group="__frequent__"]')
        assert freq.locator('.model-opt').count()==1
        assert 'model-b' in freq.inner_text()
        assert dd.locator('.model-group:not(.model-custom-sep)').first.text_content()=='Frequently used'
        assert dd.evaluate('el=>el.scrollTop')==0
        star=freq.locator('.model-pin-star')
        star.wait_for(state='visible',timeout=3000)
        rect=star.bounding_box()
        assert rect and rect['x']>=0 and rect['x']+rect['width']<=page.viewport_size['width']
        search=dd.locator('.model-search-input')
        search.fill('model-a')
        candidate=dd.locator('.model-opt').filter(has_text='@custom:two:model-a')
        assert candidate.count()==1
        previous=page.evaluate("document.getElementById('modelSelect').value")
        if mobile:
            candidate.locator('.model-pin-star').tap()
        else:
            candidate.locator('.model-pin-star').focus()
            page.keyboard.press('Enter')
        page.wait_for_function("_modelFrequentCache.pins.some(p=>p.provider==='custom:two')")
        assert page.evaluate("document.getElementById('modelSelect').value")==previous
        assert dd.evaluate("el=>el.classList.contains('open')")
        assert search.count()==1
        assert dd.locator('.model-search-input').input_value()=='model-a'
        data=page.request.get(BASE+'/api/models/preferences').json()
        assert data['pins']==[{'model':'model-a','provider':'custom:two'}]
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function("typeof renderModelDropdown==='function'")
        page.evaluate('async()=>{await populateModelDropdown();await _loadModelFrequentPrefs();}')
        open_composer_picker(page)
        freq=page.locator('#composerModelDropdown [data-group="__frequent__"]')
        assert freq.locator('.model-opt').count()==2
        assert 'custom:two:model-a' in freq.locator('.model-opt').first.inner_text()
        # Normal row click exercises exact real routing without running a model.
        page.evaluate("()=>{document.getElementById('modelSelect').onchange=()=>{window.selectedState=_modelStateForSelect(document.getElementById('modelSelect'),document.getElementById('modelSelect').value)}}")
        freq.locator('.model-opt').first.click()
        assert page.evaluate('window.selectedState')=={'model':'model-a','model_provider':'custom:two'}
        page.evaluate('()=>toggleModelDropdown()')
        page.locator('#composerModelDropdown [data-group="__frequent__"] .model-pin-star.pinned').click()
        page.wait_for_function('_modelFrequentCache.pins.length===0')
        assert page.request.get(BASE+'/api/models/preferences').json()['pins']==[]
        assert not errors, errors
        browser.close()
