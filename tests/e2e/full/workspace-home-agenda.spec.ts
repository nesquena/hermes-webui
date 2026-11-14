import {test, expect, api} from './fixtures';

async function boot(page) {
  await page.goto('/');
  await page.waitForFunction(() => typeof S !== 'undefined' && S._bootReady && Array.isArray(window.__GOV_ME__?.hidden_nav));
  const tour = page.locator('#spHelpCard[data-kind="tour_offer"] .sp-help-btn').first();
  // The first-run offer is intentionally delayed until preferences have loaded.
  await page.addLocatorHandler(tour, async()=>{await tour.click();});
}

test('WORK VIEWS Overview opens on a plain visit and respects the personal Chat preference', async ({page}, info) => {
  await boot(page);
  await page.screenshot({path:info.outputPath('entry.png')});
  await expect(page.locator('#mainStudio .wp-hero')).toBeVisible();
  await expect(page.locator('[data-wp-go="conversations"]')).toBeVisible();
  await expect(page.locator('[data-wp-go="chart"]')).toBeVisible();
  const sections = page.locator('.wp-destinations');
  await sections.locator('summary').click();
  await expect(sections.locator('[data-wp-destination="settings"]')).toBeVisible();
  await expect(sections.locator('[data-wp-studio="me"]')).toBeVisible();
  await sections.locator('[data-wp-destination="projects"]').click();
  await expect(page.locator('#mainProjects')).toBeVisible();
  await page.locator('#workspaceHome').click();
  await expect(page.locator('#mainStudio .wp-hero')).toBeVisible();
  for (const width of [1440,900,390]) {
    await page.setViewportSize({width,height:900});
    await page.evaluate(()=>closeMobileSidebar());
    await expect(page.locator('.wp-hero')).toBeVisible();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath('overview-'+width+'.png')});
  }
  await page.setViewportSize({width:1440,height:900});
  await page.locator('[data-wp-go="chart"]').click();
  await expect(page.locator('.bot-org-view')).toBeVisible();
  await page.locator('.rail [data-panel="settings"]').click();
  await page.locator('#settingsStartPage').selectOption('chat');
  await boot(page);
  await expect(page.locator('#mainStudio .wp-hero')).toBeHidden();
  await expect(page.locator('#msg')).toBeVisible();
});

test('WORK VIEWS agenda can change period and open a repeating task', async ({page}, info) => {
  await boot(page);
  const made = await api(page,'/api/crons/create',{name:'Calendar review',prompt:'Synthetic calendar entry',schedule:'every 3h'});
  expect(made.status).toBe(200);
  await page.locator('.rail [data-panel="tasks"]').click();
  await page.screenshot({path:info.outputPath('tasks-entry.png')});
  const agenda = page.locator('#cronAgenda');
  await expect(agenda.locator('[data-agenda-view="week"]')).toHaveAttribute('aria-pressed','true');
  await agenda.locator('[data-agenda-view="month"]').click();
  await expect(agenda.locator('[data-agenda-day]').first()).toBeVisible();
  for (const width of [1440,900,390]) {
    await page.setViewportSize({width,height:900});
    await page.evaluate(()=>closeMobileSidebar());
    expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath('agenda-'+width+'.png')});
  }
  await agenda.locator('[data-agenda-day][data-populated="true"]').last().click();
  await expect(agenda.locator('[data-agenda-view="day"]')).toHaveAttribute('aria-pressed','true');
  const task = agenda.getByRole('button',{name:/Calendar review/}).first();
  const times = task.locator('xpath=following-sibling::details[1]');
  await times.locator('summary').click();
  await expect(times.locator('time')).toHaveCount(8);
  await task.click();
  await expect(page.locator('#taskDetailTitle')).toContainText('Calendar review');
  await page.locator('#cronAgendaBack').click();
  await expect(agenda).toBeVisible();
});

test('WORK VIEWS project sections reopen with the same disclosure state', async ({page}, info) => {
  await boot(page);
  const made=await api(page,'/api/projects/team',{name:'Calendar project',members:[],bot_participants:[],profile:'default'});
  expect(made.status).toBe(200);
  await page.locator('.rail [data-panel="projects"]').click();
  await page.locator('.proj-row').filter({hasText:'Calendar project'}).click();
  await expect(page.locator('#projStartChat')).toBeVisible();
  for (const width of [1440,900,390]) {
    await page.setViewportSize({width,height:900});await page.evaluate(()=>closeMobileSidebar());
    expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath('project-'+width+'.png')});
  }
  await page.setViewportSize({width:1440,height:900});
  const fold=page.locator('#projSections details[data-project-section="team"]');
  await expect(fold).not.toHaveAttribute('open','');
  await fold.locator('summary').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#projPeopleSearch')).toBeVisible();
  await page.locator('.proj-row').filter({hasText:'Calendar project'}).click();
  await expect(page.locator('#projPeopleSearch')).toBeVisible();
});

test('WORK VIEWS recent conversations and executed tasks open the original record',async({page})=>{
  test.setTimeout(100000);
  await boot(page);
  const task=await api(page,'/api/crons/create',{name:'Recent calendar run',prompt:'QA_DASHBOARD_RUN',schedule:'every 8h',deliver:'local'});
  expect(task.status).toBe(200);
  const id=task.body.job.id;
  expect((await api(page,'/api/crons/run',{job_id:id})).status).toBe(200);
  await expect.poll(async()=>(await api(page,'/api/crons')).body.jobs.find(j=>j.id===id)?.last_run_at,{timeout:45000}).toBeTruthy();
  const made=await api(page,'/api/session/new',{profile:'default',chat_mode:'normal',enabled_toolsets:['none']});
  const sid=made.body.session.session_id;
  await page.goto('/session/'+sid);
  await expect(page.locator('#msg')).toBeVisible();
  await page.locator('#msg').fill('QA_HOME_RECENT_CHAT');await page.locator('#btnSend').click();
  await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_HOME_RECENT_CHAT',{timeout:30000});
  await page.locator('#workspaceHome').click();
  await page.locator('[data-wp-open="conversation"][data-wp-id="'+sid+'"]').click();
  await expect(page.locator('#messages')).toBeVisible();
  await expect(page.locator('#messages')).toContainText('QA_HOME_RECENT_CHAT');
  await page.locator('#workspaceHome').click();
  await page.locator('[data-wp-open="run"][data-wp-id="'+id+'"]').click();
  await expect(page.locator('#taskDetailTitle')).toContainText('Recent calendar run');
});
