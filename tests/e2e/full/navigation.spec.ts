import { test, expect, open, capture } from './fixtures';
const panels: [string,string,string][] = [
 ['CHAT','chat','#chatModeHeader'], ['BOT','profiles','#botDirectory'], ['PROJ','projects','#projNewName'],
 ['CRON','tasks','#cronRefreshBtn'], ['SKILL','skills','#skillsSearch'], ['CONN','integrations','#integrationsRefreshBtn'],
 ['GOV','approvals','#btnMyApprovalsRefresh'], ['MEM','memory','#panelMemory'],
 ['TODO','todos','#panelTodos'], ['INS','insights','#insightsPeriod'],
 ['LOG','logs','#logsFile'], ['GOV','governance','#mainGovernance'], ['PREF','settings','#settingsSearch']
];
for (const [area,panel,selector] of panels) test(`US-SP-${area}-NAV ${panel} opens through visible navigation`, async ({page},info)=>{
 await open(page,panel); await expect(page.locator(selector)).toBeVisible();
 await capture(page,panel,info);
});
for (const tab of ['overview','users','groups','workspaces','approvals','integrations','preview','audit']) test(`US-SP-GOV-TAB-${tab} governance ${tab} tab opens and renders`,async({page},info)=>{
 await open(page,'governance'); await page.locator(`[data-gov-tab="${tab}"]`).click();
 const pane=page.locator('.gov-tab-pane.active');await expect(pane).toBeVisible();await expect(pane).not.toHaveText('');
 await expect(pane).not.toContainText('Loading...');await capture(page,tab,info);
});
for (const tab of ['appearance','preferences','providers','plugins','extensions','system','help']) test(`US-SP-PREF-TAB-${tab} settings ${tab} renders`,async({page},info)=>{
 await open(page,'settings');await page.locator(`[data-settings-section="${tab}"]`).click();
 const pane=page.locator('.settings-pane.active');await expect(pane).toBeVisible();await expect(pane).not.toHaveText('');await capture(page,tab,info);
});
