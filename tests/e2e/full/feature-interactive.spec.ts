import {test,expect,open} from './fixtures';

// Composer prewarm imports modules only; it neither sends a model request nor writes a chat.
async function ready(page:any){
 await open(page);
 await page.waitForFunction(()=>typeof S!=='undefined'&&S._bootReady&&_loadingSessionId===null);
 await page.evaluate(()=>window.loadSynthPulseFeatureGuide());
}

test('optional full guide covers the complete chat and composer context without writing',async({page},info)=>{
 await ready(page);await page.locator('#msg').fill('Keep this unsent draft exactly.');
 const writes:string[]=[];page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method())&&!r.url().endsWith('/api/help/events')&&!r.url().endsWith('/api/chat/prewarm'))writes.push(r.url());});
 await page.locator('#btnPageHelp').focus();
 await page.evaluate(()=>window.SynthPulseHelp.startFeatureTour(window.SynthPulseFeatureGuide.featuresForPage('chat').map(f=>f.id),{trigger:document.getElementById('btnPageHelp')}));
 const tip=page.locator('.sp-feature-tour');await expect(tip).toBeVisible();
 const ids=await page.evaluate(()=>window.SynthPulseFeatureGuide.featuresForPage('chat').map(f=>f.id));
 expect(ids.length).toBeGreaterThan(32);
 for(const id of ['webui.chat.model','webui.chat.reasoning','webui.chat.attach','webui.chat.queue','webui.voice.dictation'])expect(ids).toContain(id);
 await expect(tip.locator('.sp-feature-tour-picker option')).toHaveCount(ids.length);
 await tip.locator('.sp-feature-tour-picker').selectOption(String(ids.indexOf('webui.chat.model')));
 await expect(tip).toHaveAttribute('data-feature','webui.chat.model');
 await expect(page.locator('#composerModelChip')).toHaveClass(/guida-highlight/);
 await expect(tip.locator('.sp-feature-tour-detail section')).toHaveCount(6);
 await tip.locator('.guida-next').click();await expect(tip).not.toHaveAttribute('data-feature','webui.chat.model');
 await tip.locator('.guida-prev').click();await expect(tip).toHaveAttribute('data-feature','webui.chat.model');
 for(const language of ['nl','en'])for(const width of [1440,900,390]){
  await page.setViewportSize({width,height:900});await tip.locator('.sp-feature-tour-language').selectOption(language);
  await expect(tip).toHaveAttribute('lang',language);
  await expect(tip.locator('.guida-close')).toHaveText(language==='nl'?'Sluiten':'Close');
  const bounds=await tip.evaluate(e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,scroll:e.scrollWidth,width:e.clientWidth};});
  expect(bounds.left).toBeGreaterThanOrEqual(0);expect(bounds.top).toBeGreaterThanOrEqual(0);expect(bounds.right).toBeLessThanOrEqual(width);expect(bounds.bottom).toBeLessThanOrEqual(900);expect(bounds.scroll).toBeLessThanOrEqual(bounds.width+1);
  await page.screenshot({path:info.outputPath(`interactive-${language}-${width}.png`)});
 }
 await page.keyboard.press('Escape');await expect(tip).toHaveCount(0);await expect(page.locator('#msg')).toHaveValue('Keep this unsent draft exactly.');await expect(page.locator('#btnPageHelp')).toBeFocused();
 expect(writes).toEqual([]);expect(await page.locator('[data-sp-help-step],.guida-highlight,#spFeatureTourAnchor').count()).toBe(0);
});

test('every catalogue feature has complete interactive NL/EN content, including MSP and planned topics',async({page})=>{
 test.setTimeout(120000);await ready(page);
 const report=await page.evaluate(()=>{
  const g=window.SynthPulseFeatureGuide,ids=g.featureIds(),failures=[];
  for(const language of ['en','nl']){
   window.SynthPulseHelp.startFeatureTour(ids,{locale:language});
   for(let i=0;i<ids.length;i++){
    const picker=document.querySelector('.sp-feature-tour-picker');picker.value=String(i);picker.dispatchEvent(new Event('change'));
    const tip=document.querySelector('.sp-feature-tour'),feature=g.getFeature(ids[i]),copy=feature.locales[language];
    if(tip?.getAttribute('data-feature')!==ids[i]||tip.querySelector('h3')?.textContent!==copy.title||tip.querySelectorAll('.sp-feature-tour-detail section').length!==6)failures.push(ids[i]);
    for(const value of [copy.purpose,copy.when,copy.permissions,copy.limits,copy.example,...copy.steps])if(!tip?.textContent?.includes(value))failures.push(ids[i]+':missing-content');
    if(feature.product==='msp'||feature.status==='planned'){
     if(document.querySelector('.guida-highlight')||!tip?.querySelector('.sp-feature-tour-note'))failures.push(ids[i]+':false-highlight');
    }
   }
   window.SynthPulseHelp.closeAll();
  }
  const inventory=[...window.SynthPulseFeatureCatalog.features,...window.SynthPulseMspFeatureCatalog.features].map(f=>f.id);
  return {ids:ids.sort(),inventory:inventory.sort(),failures,clean:!document.querySelector('.guida-tooltip,#spFeatureTourAnchor,[data-sp-help-step]')};
 });
 expect(report.inventory.length).toBeGreaterThanOrEqual(510);expect(report.ids).toEqual(report.inventory);expect(report.failures).toEqual([]);expect(report.clean).toBe(true);
});

test('catalogue opens an individual or filtered tour and preserves language and selection',async({page})=>{
 await ready(page);
 await page.evaluate(()=>window.SynthPulseFeatureGuide.open({feature:'webui.chat.model',locale:'nl'}));
 await page.getByRole('button',{name:'Interactieve uitleg starten',exact:true}).click();
 const tip=page.locator('.sp-feature-tour');await expect(tip).toHaveAttribute('data-feature','webui.chat.model');await expect(tip).toHaveAttribute('lang','nl');
 await expect(tip.locator('.sp-feature-tour-picker option')).toHaveCount(1);
 await tip.locator('.sp-feature-tour-language').selectOption('en');
 await tip.getByRole('button',{name:'Back to feature guide',exact:true}).click();
 await expect(page.locator('#spGuideLanguage')).toHaveValue('en');await expect(page.locator('.sp-guide-result[aria-current=true]')).toHaveAttribute('data-feature','webui.chat.model');
 await page.getByRole('button',{name:'Clear search and filters',exact:true}).click();await page.locator('#spGuideSearch').fill('reporter');
 const count=await page.locator('.sp-guide-result').count();expect(count).toBeGreaterThan(1);
 await page.getByRole('button',{name:'Guide through these results',exact:true}).click();await expect(tip.locator('.sp-feature-tour-picker option')).toHaveCount(count);
 await page.keyboard.press('Escape');await expect(tip).toHaveCount(0);
});

test('planned and cross-product topics never navigate, and late next does not reopen a closed tour',async({page})=>{
 await ready(page);const url=page.url();
 await page.evaluate(()=>window.SynthPulseHelp.startFeatureTour(['webui.planned.central_memory','msp.reporter-spool'],{locale:'en'}));
 const tip=page.locator('.sp-feature-tour');await expect(tip.locator('.sp-feature-tour-note')).toContainText('planned');await expect(page.locator('.guida-highlight')).toHaveCount(0);expect(page.url()).toBe(url);
 await tip.locator('.guida-next').click();await page.keyboard.press('Escape');await page.waitForTimeout(400);await expect(tip).toHaveCount(0);expect(page.url()).toBe(url);await expect(page.locator('#spFeatureTourAnchor')).toHaveCount(0);
});

test('missing spotlight library keeps the complete catalogue explanation available',async({page})=>{
 await ready(page);await page.evaluate(()=>{window.SynthPulseFeatureGuide.open({feature:'webui.chat.model',locale:'en'});window.SpotlightOnboarding=null;});
 await page.getByRole('button',{name:'Start interactive guide',exact:true}).click();await expect(page.locator('#spFeatureGuide')).toBeVisible();await expect(page.locator('.sp-guide-notice')).toContainText('could not start');
});

test('background controls stay inactive while the guide navigates to the real control',async({page})=>{
 await ready(page);await page.locator('#msg').fill('Do not send this draft.');
 const writes:string[]=[];page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method())&&!r.url().endsWith('/api/help/events')&&!r.url().endsWith('/api/chat/prewarm')&&!(r.url().endsWith('/api/auth/passkeys')&&r.postData()==='{}'))writes.push(r.url());});
 await page.evaluate(()=>window.SynthPulseHelp.startFeatureTour(['webui.chat.send'],{locale:'en'}));
 await page.locator('#btnSend').dispatchEvent('click');await expect(page.locator('#msg')).toHaveValue('Do not send this draft.');expect(writes).toEqual([]);
 await page.keyboard.press('Escape');
 await page.evaluate(()=>window.SynthPulseHelp.startFeatureTour(['webui.settings.theme'],{locale:'en'}));
 await expect(page.locator('.sp-feature-tour')).toBeVisible();await expect(page.locator('.guida-highlight')).toHaveCount(1);await expect(page.locator('.sp-feature-tour-note')).toHaveCount(0);
 expect(await page.evaluate(()=>_currentPanel)).toBe('settings');expect(writes).toEqual([]);
 await page.keyboard.press('Escape');await page.evaluate(()=>switchPanel('chat'));await expect(page.locator('#msg')).toHaveValue('Do not send this draft.');
});
