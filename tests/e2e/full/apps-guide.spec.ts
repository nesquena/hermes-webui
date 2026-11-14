import {test,expect,open,api} from './fixtures';
import fs from 'node:fs';
import path from 'node:path';
const catalog=JSON.parse(fs.readFileSync(path.resolve('static/help-features.js'),'utf8').split(' = ')[1].trim().slice(0,-1));
const ids=catalog.features.filter((f:any)=>f.id.startsWith('webui.apps.')&&!['overview','search','categories','language'].includes(f.id.split('.').pop())).map((f:any)=>f.id);

for(const width of [1440,390]){
 test(`every app has a real bilingual interactive card at ${width}px`,async({page,context},info)=>{
  test.setTimeout(100_000);await page.setViewportSize({width,height:1000});await open(page);
  await page.waitForFunction(()=>typeof S!=='undefined'&&S._bootReady&&_loadingSessionId===null);
  await page.locator('#msg').fill('Apps guide draft remains unsent');
  const available=(await api(page,'/api/apps')).body.apps;
  expect(available.length).toBeGreaterThan(0);
  await page.evaluate(()=>switchPanel('apps'));await expect(page.locator('#mainApps [data-app-id]')).toHaveCount(available.length);
  await page.locator('#appsCategory').selectOption('creative');await page.locator('#appsSearch').fill('photo');
  const writes:string[]=[];page.on('request',request=>{const url=new URL(request.url());if(['POST','PUT','PATCH','DELETE'].includes(request.method())&&!['/api/help/events','/api/client-events/log'].includes(url.pathname))writes.push(url.pathname);});
  const pagesBefore=context.pages().length;
  for(const locale of ['nl','en']){
   await page.evaluate(({ids,locale})=>window.SynthPulseHelp.startFeatureTour(ids,{locale}),{ids,locale});
   const tip=page.locator('.sp-feature-tour');await expect(tip).toBeVisible();
   for(let index=0;index<ids.length;index++){
    if(index)await tip.locator('.sp-feature-tour-picker').selectOption(String(index));
    const appId=ids[index].slice('webui.apps.'.length);
    await expect(tip).toHaveAttribute('data-feature',ids[index]);
    await expect(page.locator(`#mainApps [data-app-id="${appId}"]`)).toHaveClass(/guida-highlight/);
    await expect(tip).toHaveAttribute('lang',locale);
    await expect(tip.locator('.sp-feature-tour-note')).toHaveCount(0);
    const box=await tip.boundingBox();expect(box!.x).toBeGreaterThanOrEqual(0);expect(box!.y).toBeGreaterThanOrEqual(0);expect(box!.x+box!.width).toBeLessThanOrEqual(width);expect(box!.y+box!.height).toBeLessThanOrEqual(1000);
   }
   await page.screenshot({path:info.outputPath(`apps-guide-${width}-${locale}.png`)});
   await page.keyboard.press('Escape');await expect(tip).toHaveCount(0);
   await expect(page.locator('#appsCategory')).toHaveValue('creative');await expect(page.locator('#appsSearch')).toHaveValue('photo');
   await expect(page.locator('.guida-highlight')).toHaveCount(0);
  }
  // The actual card button also starts its individual tour, without following
  // the app link or requiring the old popover launcher.
  await page.locator('[data-app-id="photocraft"] button').click();
  await expect(page.locator('.sp-feature-tour')).toHaveAttribute('data-feature','webui.apps.photocraft');
  await expect(page.locator('[data-app-id="photocraft"]')).toHaveClass(/guida-highlight/);
  await page.keyboard.press('Escape');await expect(page.locator('[data-app-id="photocraft"] button')).toBeFocused();expect(context.pages()).toHaveLength(pagesBefore);expect(writes).toEqual([]);
  await page.evaluate(()=>switchPanel('chat'));await expect(page.locator('#msg')).toHaveValue('Apps guide draft remains unsent');
 });
}
