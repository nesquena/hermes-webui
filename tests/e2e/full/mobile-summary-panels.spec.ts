import {test,expect,capture} from './fixtures';

for(const [panel,control,endpoint,status] of [
 ['approvals','#btnMyApprovalsRefresh','/api/governance/approvals/mine',200],
 ['integrations','#mainIntegrations [onclick="loadIntegrations()"]','/api/integrations/catalog',502],
] as const){
 test('US-SP-UX-MOBILE '+panel+' hamburger navigation allows actual main refresh control',async({page},info)=>{
  await page.setViewportSize({width:390,height:844});await page.goto('/');await expect(page.locator('#mainStudio .wp-hero')).toBeVisible();
  await page.waitForFunction(()=>typeof S!=='undefined'&&S._bootReady&&_loadingSessionId===null);
  await page.locator('#spHelpCard[data-kind="tour_offer"] .sp-help-btn').first().click();
  await page.locator('#btnHamburger').click();await page.locator('.sidebar-nav [data-panel="'+panel+'"]').click();
  const response=page.waitForResponse(r=>r.url().includes(endpoint)&&r.request().method()==='GET');
  await page.locator(control).click();expect((await response).status()).toBe(status);
  if(panel==='integrations') await expect(page.locator('#intgGrid .intg-error')).toContainText('Some connections could not be loaded. Refresh to try again.');
  await expect(page.locator('.sidebar')).not.toHaveClass(/mobile-open/);await capture(page,'mobile-'+panel+'-refreshed',info);
 });
}
