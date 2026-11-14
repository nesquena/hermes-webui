import {test, expect, open, api, session} from './fixtures';

async function preferences(page:any, section='preferences') {
  await page.locator('.rail-btn[data-panel="chat"]').click();
  await page.locator('.rail-btn[data-panel="settings"]').click();
  await page.locator(`[data-settings-section="${section}"]`).click();
}

async function toggle(page:any,id:string,key:string,wanted:boolean) {
  const field=page.locator('#'+id);
  if(await field.isChecked()===wanted){
    await field.setChecked(!wanted);
    await expect.poll(async()=>(await api(page,'/api/settings')).body[key]).toBe(!wanted);
  }
  await field.setChecked(wanted);
  await expect.poll(async()=>(await api(page,'/api/settings')).body[key]).toBe(wanted);
}

test('US-SP-PREF-BEHAVIOR-SEARCH search selects actual setting and clears stale results',async({page})=>{
  await open(page,'settings');
  await page.locator('#settingsSearch').fill('Hide new-chat suggestions');
  const result=page.locator('#settingsSearchResults .settings-search-result').filter({hasText:'Hide new-chat suggestions'});
  await expect(result).toHaveCount(1);await result.click();
  await expect(page.locator('#settingsHideSuggestions')).toBeVisible();
  await expect(page.locator('#settingsSearch')).toHaveValue('');
  await expect(page.locator('#settingsSearchResults')).toBeHidden();
  const original=await page.locator('#settingsHideSuggestions').isChecked();
  await toggle(page,'settingsHideSuggestions','hide_empty_state_suggestions',!original);
  await toggle(page,'settingsHideSuggestions','hide_empty_state_suggestions',original);
  await page.locator('#settingsSearch').fill('QA_SETTING_DOES_NOT_EXIST');
  await expect(page.locator('#settingsSearchResults .settings-search-result')).toHaveCount(0);
  await page.locator('#settingsSearch').fill('');
  await expect(page.locator('#settingsSearchResults')).toBeHidden();
});

test('US-SP-PREF-BEHAVIOR-LANGUAGE German selection translates visible interface then restores English',async({page})=>{
  await open(page,'settings');await page.locator('[data-settings-section="preferences"]').click();
  const field=page.locator('#settingsLanguage');
  await expect(field.locator('option[value="de"]')).toHaveCount(1);
  await field.selectOption('de');
  await expect.poll(async()=>(await api(page,'/api/settings')).body.language).toBe('de');
  await expect(page.locator('#panelSettings .panel-head')).toContainText('Einstellungen');
  await expect(page.locator('label[for="settingsLanguage"]')).toHaveText('Sprache');
  await field.selectOption('en');
  await expect.poll(async()=>(await api(page,'/api/settings')).body.language).toBe('en');
  await expect(page.locator('#panelSettings .panel-head')).toContainText('Settings');
  await expect(page.locator('label[for="settingsLanguage"]')).toHaveText('Language');
});

test('US-SP-PREF-BEHAVIOR-EMPTY suggestions and entire welcome panel hide independently and survive reload',async({page})=>{
  await open(page);await session(page);
  await preferences(page);
  await toggle(page,'settingsHideEmptyStatePanel','hide_empty_state_panel',false);
  for(const hidden of [true,false]){
    await toggle(page,'settingsHideSuggestions','hide_empty_state_suggestions',hidden);
    await page.locator('.rail-btn[data-panel="chat"]').click();
    await expect(page.locator('#emptyState')).toBeVisible();
    if(hidden) await expect(page.locator('#emptyState .suggestion-grid')).toBeHidden();
    else await expect(page.locator('#emptyState .suggestion-grid')).toBeVisible();
    await preferences(page);
  }
  await toggle(page,'settingsHideEmptyStatePanel','hide_empty_state_panel',true);
  await page.locator('.rail-btn[data-panel="chat"]').click();
  await expect(page.locator('#emptyState')).toBeHidden();
  await page.reload();await expect(page.locator('#msg')).toBeVisible();
  await expect(page.locator('#emptyState')).toBeHidden();
  await preferences(page);
  await toggle(page,'settingsHideEmptyStatePanel','hide_empty_state_panel',false);
  await page.locator('.rail-btn[data-panel="chat"]').click();
  await expect(page.locator('#emptyState')).toBeVisible();
  await expect(page.locator('#emptyState .suggestion-grid')).toBeVisible();
});

test('US-SP-PREF-BEHAVIOR-TODOS workspace preference controls the contextual task list',async({page})=>{
  await open(page);await session(page);await preferences(page,'appearance');
  for(const enabled of [true,false]){
    await toggle(page,'settingsWorkspaceTodosTab','workspace_todos_tab',enabled);
    await page.locator('.rail-btn[data-panel="chat"]').click();
    if(await page.locator('html').getAttribute('data-workspace-panel')==='closed') await page.locator('#btnWorkspacePanelToggle').click();
    await expect(page.locator('html')).not.toHaveAttribute('data-workspace-panel','closed');
    if(enabled){
      await expect(page.locator('#workspaceTodosTab')).toBeVisible();
      await page.locator('#workspaceTodosTab').click();
      await expect(page.locator('#workspaceTodosPanel')).toBeVisible();
      await expect(page.locator('#workspaceTodosTab')).toHaveAttribute('aria-selected','true');
      await page.locator('#workspaceFilesTab').click();
    }else await expect(page.locator('#workspaceTodosTab')).toBeHidden();
    await expect(page.locator('.rail-btn[data-panel="todos"]')).toHaveCount(0);
    await preferences(page,'appearance');
  }
});
