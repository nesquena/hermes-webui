import {test,expect,open,api} from './fixtures';
async function ready(page:any){await page.waitForFunction(()=>typeof S!=='undefined'&&S._bootReady&&_loadingSessionId===null);await expect(page.locator('#btnApps')).toBeVisible();}
test.describe('Apps directory',()=>{
 test.use({user:'admin'});
 test('all stack apps, Craft editors, search, categories, layouts and preserved chat',async({page},info)=>{
  await open(page);await ready(page);await page.locator('#msg').fill('QA Apps draft stays here');
  await page.screenshot({path:info.outputPath('apps-before-chat.png'),fullPage:true});
  const available=(await api(page,'/api/apps')).body.apps;expect(available.length).toBeGreaterThan(0);
  await page.locator('#btnApps').click();await expect(page.locator('.app-card')).toHaveCount(available.length);await expect(page.locator('.app-open')).toHaveCount(available.length);
  for(const id of ['ragflow','langfuse','hermes_docs','hermes_sheets','hermes_slides','hermes_pdf','hermes_markdown','opendesign','craft_studio','n8n','omniroute','photocraft','filmcraft','effectcraft','vectorcraft','lightcraft','designcraft','pdfcraft','soundcraft'])await expect(page.locator(`[data-app-id="${id}"]`)).toBeVisible();
  await expect(page.locator('[data-app-id="openwebui"] a')).toHaveCount(0);await expect(page.locator('[data-app-id="silverbullet"] a')).toHaveCount(0);
  await expect(page.locator('[data-app-id="hermes_docs"] a')).toContainText('Open app');
  for(const id of ['pareltje','silverbullet','slibdetect','pixel_office','mnemosyne','strategy_gantt','opendesign_classic','vision_studio'])await expect(page.locator(`[data-app-id="${id}"]`)).toHaveCount(0);
  await page.evaluate(()=>_applyTabOrder(['apps','modules','tasks']));
  for(const selector of ['.rail','.sidebar-nav']){const order=await page.locator(selector+' .nav-tab[data-panel]').evaluateAll(nodes=>nodes.map(n=>n.getAttribute('data-panel')));expect(order.slice(-3)).toEqual(['apps','msp','settings']);}
  await page.locator('#appsSearch').fill('omnirouter');await expect(page.locator('.app-card')).toHaveCount(1);await expect(page.locator('.app-card')).toContainText('OmniRoute');
  await page.locator('#appsSearch').fill('no-such-app');await expect(page.locator('#appsList')).toContainText('No apps found');
  await page.locator('#appsSearch').fill('');await page.locator('#appsCategory').selectOption('creative');await expect(page.locator('.app-card')).toHaveCount(available.filter((app:any)=>app.category==='creative').length);
  await page.locator('#appsCategory').selectOption('all');
  for(const locale of ['nl','en'])for(const width of [390,900,1440]){
   await page.setViewportSize({width,height:1000});await page.locator('#appsLanguage').selectOption(locale);
   await expect(page.locator('#appsSearch')).toHaveAttribute('placeholder',locale==='nl'?'Zoek een app':'Search apps');
   for(const theme of ['dark','light']){await page.evaluate(t=>{_applySkin('default');_applyTheme(t);document.getElementById('mainApps')!.scrollTop=0;},theme);
    const dimensions=await page.locator('#mainApps').evaluate(e=>({scroll:e.scrollWidth,width:e.clientWidth,right:e.getBoundingClientRect().right}));expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width+1);expect(dimensions.right).toBeLessThanOrEqual(width+1);
    await page.screenshot({path:info.outputPath(`apps-${locale}-${width}-${theme}.png`),fullPage:true});
   }
  }
  await page.evaluate(()=>switchPanel('chat'));await expect(page.locator('#msg')).toHaveValue('QA Apps draft stays here');await expect(page.locator('#mainApps')).toBeHidden();
  const chat=await page.locator('#mainChat').boundingBox();expect(chat!.height).toBeGreaterThan(700);
 });
 test('mobile navbar opens Apps and closes drawer',async({page})=>{
  await open(page);await ready(page);await page.setViewportSize({width:390,height:900});
  await page.getByRole('button',{name:'Not now',exact:true}).click();
  await page.getByRole('button',{name:'Menu',exact:true}).click();
  await page.locator('.sidebar-nav [data-panel="apps"]').click();await expect(page.locator('#mainApps')).toBeVisible();await expect(page.locator('.sidebar')).not.toHaveClass(/mobile-open/);await expect(page.locator('.app-card')).toHaveCount((await api(page,'/api/apps')).body.apps.length);
 });
 test('directory retry, empty response and safe text rendering',async({page})=>{
  await open(page);await ready(page);await page.route('**/api/apps',r=>r.fulfill({status:503,json:{error:'offline'}}));await page.locator('#btnApps').click();await expect(page.locator('#appsList')).toContainText('Apps could not be loaded');
  await page.route('**/api/apps',r=>r.fulfill({json:{apps:[]}}));await page.getByRole('button',{name:'Try again',exact:true}).click();await expect(page.locator('#appsList')).toContainText('No apps have been linked');
  await page.route('**/api/apps',r=>r.fulfill({json:{apps:[{id:'opendesign',name:{en:'<img src=x onerror=alert(1)>'},description:{en:'<script>alert(1)</script>'},url:'javascript:alert(1)',category:'creative'}]}}));
  await page.evaluate(()=>SynthPulseApps.load());await expect(page.locator('.app-card img, .app-card script, .app-card a')).toHaveCount(0);await expect(page.locator('.app-card')).toContainText('<img');
 });
 test('permission refresh discards a late app response and blocks hidden navigation',async({page})=>{
  await open(page);await ready(page);let release:any;const gate=new Promise<void>(r=>release=r);
  await page.route('**/api/apps',async r=>{await gate;await r.fulfill({json:{apps:[{id:'omniroute',name:{en:'Private app'},url:'https://apps.example.test/omniroute/' }]}});});
  await page.locator('#btnApps').click();await expect(page.locator('#appsList')).toContainText('Loading apps');
  await page.evaluate(()=>SynthPulseModules.apply({enabled_modules:[],module_nav:{hidden_panels:[],modules_visible:false,apps_visible:false}}));release();
  await expect(page.locator('#btnApps')).toBeHidden();await expect(page.locator('.app-card')).toHaveCount(0);await page.evaluate(()=>switchPanel('apps'));expect(await page.evaluate(()=>_currentPanel)).toBe('chat');
 });
});
