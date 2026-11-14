import path from 'node:path';
import fs from 'node:fs';
import {test, expect, open, session, api, auth} from './fixtures';

async function ready(page:any){
  await open(page);
  await page.waitForFunction(()=>typeof S!=='undefined'&&S._bootReady&&_loadingSessionId===null);
  await page.locator('#spHelpCard[data-kind="tour_offer"] .sp-help-btn').first().click();
}
async function send(page:any, marker:string){
  const url='/api/session?session_id='+page.url().split('/session/')[1];
  const before=(await api(page,url)).body.session.messages.length;
  await page.locator('#msg').fill(marker); await page.locator('#btnSend').click();
  await expect.poll(async()=>(await api(page,url)).body.session.messages.length).toBeGreaterThanOrEqual(before+2);
  await expect.poll(async()=>(await api(page,'/health')).body.active_runs||0).toBe(0);
}

test('CONVERSATION WORKBENCH overview preserves drafts, filters persisted pins and opens the chosen conversation',async({page},info)=>{
  await ready(page);const first=await session(page);await send(page,'QA_BOARD_FIRST');
  expect((await api(page,'/api/session/rename',{session_id:first,title:'Launch plan · customer onboarding'})).status).toBe(200);
  expect((await api(page,'/api/session/pin',{session_id:first,pinned:true})).status).toBe(200);
  await page.locator('#btnNewChat').click();await expect(page).not.toHaveURL(new RegExp('/session/'+first+'$'));
  const second=page.url().split('/session/')[1];await send(page,'QA_BOARD_SECOND');
  expect((await api(page,'/api/session/rename',{session_id:second,title:'Website review · next steps'})).status).toBe(200);
  await page.evaluate(()=>renderSessionList());
  await page.locator('#msg').fill('Keep this unsent draft');
  await page.screenshot({path:info.outputPath('before-overview.png')});
  await expect(page.locator('#btnConversationBoard')).toBeVisible({timeout:3000});
  await page.locator('#btnConversationBoard').click();
  await expect(page.locator('#msg')).toBeHidden();
  await expect(page.locator('[data-conversation-open="'+first+'"]')).toBeVisible();
  await expect(page.locator('[data-conversation-open="'+second+'"]')).toBeVisible();
  await page.screenshot({path:info.outputPath('conversations-desktop.png')});
  await page.evaluate(()=>document.documentElement.classList.remove('dark'));
  await page.screenshot({path:info.outputPath('conversations-light.png')});
  await page.evaluate(()=>document.documentElement.classList.add('dark'));
  await page.setViewportSize({width:900,height:900});
  await expect(page.locator('#conversationBoardSearch')).toBeInViewport();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('conversations-narrow.png')});
  await page.setViewportSize({width:1440,height:1000});
  const writes:string[]=[];page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))writes.push(r.url());});
  await page.locator('#conversationBoardSearch').fill('Launch onboarding');
  await expect(page.locator('.conversation-board-card')).toHaveCount(1);
  await page.locator('[data-conversation-filter="pinned"]').click();
  await expect(page.locator('[data-conversation-open="'+first+'"]')).toBeVisible();
  await page.locator('#conversationBoardSearch').fill('no-such-session');
  await expect(page.locator('.conversation-board-card')).toHaveCount(0);
  await page.locator('#conversationBoardReset').click();
  await page.locator('#conversationBoardBack').click();
  await expect(page.locator('#mainConversations')).toBeHidden();
  await expect(page.locator('#msg')).toHaveValue('Keep this unsent draft');
  expect(writes).toEqual([]);
  await page.locator('#btnConversationBoard').click();
  await page.locator('[data-conversation-open="'+first+'"]').click();
  await expect(page).toHaveURL(new RegExp('/session/'+first+'$'));
  await expect(page.locator('#msg')).toBeVisible();
  await expect(page.locator('#messages')).toContainText('QA_BOARD_FIRST');
  await page.setViewportSize({width:390,height:844});await page.locator('#btnHamburger').click();
  await page.locator('#btnConversationBoard').click();
  await expect(page.locator('.sidebar')).not.toHaveClass(/mobile-open/);
  await expect(page.locator('#conversationBoardSearch')).toBeInViewport();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('conversations-mobile.png')});
  await page.locator('#conversationBoardNew').click();await expect(page.locator('#msg')).toBeVisible();
});

test('CONVERSATION WORKBENCH live activity follows the existing runtime and refresh keeps keyboard focus',async({page})=>{
  await ready(page);const sid=await session(page);
  await page.locator('#msg').fill('QA_SLOW_RESPONSE');await page.locator('#btnSend').click();
  await page.locator('#btnConversationBoard').click();
  await page.locator('[data-conversation-filter="running"]').click();
  await expect(page.locator('[data-conversation-open="'+sid+'"] [data-state="running"]')).toBeVisible();
  await expect(page.locator('[data-conversation-group="running"] [data-conversation-open="'+sid+'"]')).toBeVisible();
  await expect(page.locator('[data-conversation-filter="running"] .conversation-filter-count')).not.toHaveText('0');
  await page.locator('[data-conversation-filter="all"]').click();
  const card=page.locator('[data-conversation-open="'+sid+'"]');await card.focus();
  await page.evaluate(()=>renderSessionListFromCache());await expect(card).toBeFocused();
  await expect.poll(async()=>(await api(page,'/health')).body.active_runs||0).toBe(0);
  await page.evaluate(()=>renderSessionList());
  await expect(card.locator('[data-state="running"]')).toHaveCount(0);
  await expect(page.locator('[data-conversation-group="recent"] [data-conversation-open="'+sid+'"]')).toBeVisible();
  await card.press('Enter');await expect(page.locator('#messages')).toContainText('QA_SLOW_RESPONSE');
  await expect(page.locator('#mainConversations')).toBeHidden();
});

test('CONVERSATION WORKBENCH clears previous bot rows during switching and handles failed refresh',async({page})=>{
  await ready(page);const sid=await session(page);await send(page,'QA_BOARD_SCOPE');
  await page.locator('#btnConversationBoard').click();
  await expect(page.locator('[data-conversation-open="'+sid+'"]')).toBeVisible();
  let release!:()=>void,entered!:()=>void;
  const held=new Promise<void>(r=>{entered=r;}),gate=new Promise<void>(r=>{release=r;});
  await page.route('**/api/profile/switch',async route=>{entered();await gate;await route.continue();});
  try{
    await page.locator('#conversationBoardBack').click();
    await page.locator('#profileChip').click();
    await page.locator('#profileDropdown [data-profile-name="qa-research"]').click();await held;
    await page.locator('#btnConversationBoard').click();
    await expect(page.locator('.conversation-board-card')).toHaveCount(0);
    release();await expect.poll(()=>page.evaluate(()=>S.activeProfile)).toBe('qa-research');
    await expect(page.locator('#mainConversations')).toHaveAttribute('aria-busy','false');
    await expect(page.locator('[data-conversation-open="'+sid+'"]')).toHaveCount(0);
  }finally{release();await page.unroute('**/api/profile/switch');}
  await page.route('**/api/sessions?**',route=>route.fulfill({status:503,contentType:'application/json',body:'{"error":"QA unavailable"}'}));
  await page.evaluate(()=>renderSessionList());
  await expect(page.locator('#conversationBoardNotice')).toContainText('could not be refreshed');
  await page.unroute('**/api/sessions?**');await page.locator('#conversationBoardNotice button').click();
  await expect(page.locator('#conversationBoardNotice')).toBeHidden();
  await page.context().addCookies([{name:auth.cookie_name,value:auth.cookies.bob,url:auth.base_url,httpOnly:true,sameSite:'Lax'}]);
  await page.goto('/');
  await page.locator('.rail [data-panel="chat"]').click();
  await expect(page.locator('#conversationBoardSearch')).toBeVisible();
  await expect(page.locator('[data-conversation-open="'+sid+'"]')).toHaveCount(0);
});

test('CONVERSATION WORKBENCH files filter locally, preview the selected file and reset on session change',async({page},info)=>{
  test.setTimeout(60000);await ready(page);const sid=await session(page);
  const doc=path.join(auth.workspace,'launch-summary.md'),data=path.join(auth.workspace,'launch-metrics.csv');
  await send(page,'QA_GOV_WRITE|launch-summary.md');await send(page,'QA_GOV_WRITE|launch-metrics.csv');
  expect(fs.readFileSync(doc,'utf8')).toContain('QA_GOVERNANCE_EXECUTED');
  expect(fs.readFileSync(data,'utf8')).toContain('QA_GOVERNANCE_EXECUTED');
  await page.locator('#btnWorkspacePanelEdgeToggle').click();await page.locator('#workspaceArtifactsTab').click();
  await expect(page.locator('.workspace-artifact-item')).toHaveCount(2);
  await page.locator('.rightpanel').screenshot({path:info.outputPath('artifacts-desktop.png')});
  const writes:string[]=[];page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))writes.push(r.url());});
  await page.locator('#workspaceArtifactType').selectOption('documents');
  await expect(page.locator('.workspace-artifact-item')).toHaveCount(1);
  await expect(page.locator('.workspace-artifact-item')).toContainText('launch-summary.md');
  await page.locator('#workspaceArtifactSearch').fill('no-match');
  await page.evaluate(()=>renderSessionArtifacts());
  await expect(page.locator('#workspaceArtifactSearch')).toHaveValue('no-match');
  await expect(page.locator('#workspaceArtifactSearch')).toBeFocused();
  await expect(page.locator('.workspace-artifact-empty')).toContainText('No files match');
  await page.locator('#workspaceArtifactReset').click();
  await page.locator('#workspaceArtifactSearch').fill('launch-metrics');
  await page.locator('.workspace-artifact-item').click();
  await expect(page.locator('#workspaceFilesTab')).toHaveAttribute('aria-selected','true');
  await expect.poll(()=>page.evaluate(()=>_previewCurrentPath)).toBe('launch-metrics.csv');
  expect(writes).toEqual([]);
  await session(page);await page.locator('#workspaceArtifactsTab').click();
  await expect(page.locator('#workspaceArtifactSearch')).toHaveValue('');
  await expect(page.locator('#workspaceArtifactType')).toHaveValue('all');
  await expect(page.locator('.workspace-artifact-item')).toHaveCount(0);
  await page.evaluate(sid=>loadSession(sid),sid);await page.locator('#workspaceArtifactsTab').click();
  await expect(page.locator('.workspace-artifact-item')).toHaveCount(2);
  await page.setViewportSize({width:390,height:844});
  await expect(page.locator('#workspaceArtifactSearch')).toBeInViewport();
  // Wait for the existing slide-over animation before measuring its controls.
  await expect.poll(async()=>{const box=await page.locator('.rightpanel').boundingBox();return box!.x+box!.width;}).toBeLessThanOrEqual(391);
  for(const selector of ['#workspaceArtifactSearch','#workspaceArtifactType','#workspaceArtifactReset']){
    const box=await page.locator(selector).boundingBox();expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);expect(box!.x+box!.width).toBeLessThanOrEqual(391);
  }
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.locator('.rightpanel').screenshot({path:info.outputPath('artifacts-mobile.png')});
});

test('BOARD SECTIONS filters existing chats and starts a fresh chat inside an empty subsection', async ({page}, info) => {
  await ready(page);
  const original = await session(page);
  await send(page, 'QA_SECTION_ORIGINAL');
  const endpoint = '/api/me/sidebar-organization';
  async function change(body: object) {
    const {revision} = (await api(page, endpoint)).body;
    const result = await api(page, endpoint, {revision, ...body});
    expect(result.status).toBe(200);
    return result.body;
  }
  const name = 'Delivery';
  const parent = (await change({action:'create', name})).sections.at(-1).id;
  const child = (await change({action:'create', parent, name:'Kick-off'})).sections.at(-1).id;
  await change({action:'assign', session_id:original, section:parent});
  await page.reload();
  await expect(page.locator('#msg')).toBeVisible();
  await page.locator('#msg').fill('Keep the delivery draft');
  await page.locator('#btnConversationBoard').click();
  const panel = page.locator('#mainConversations');
  const pick = (id:string) => panel.locator('#conversationBoardSectionPick').selectOption(id);
  const create = panel.getByRole('button', {name:'New conversation in Delivery / Kick-off', exact:true});
  for (const width of [1440,900,390]) {
    await page.setViewportSize({width, height:900});
    await expect(panel.locator('#conversationBoardSearch')).toBeVisible();
    await page.screenshot({path:info.outputPath('sections-'+width+'.png')});
  }
  await expect(create).toBeVisible({timeout:3000});
  await pick(parent);
  await expect(panel.locator('[data-section-branch="'+parent+'"] [data-conversation-open="'+original+'"]')).toBeVisible();
  await pick(child);
  await expect(panel.locator('.conversation-board-card')).toHaveCount(0);
  await expect(create).toBeInViewport();
  await create.focus();
  await page.evaluate(()=>renderSessionListFromCache());
  await expect(create).toBeFocused();
  // A concurrent personal edit forces a real revision conflict on assignment.
  await change({action:'create', name:'Personal'});
  let creations = 0;
  page.on('request', req=>{ if(req.method()==='POST' && new URL(req.url()).pathname==='/api/session/new') creations++; });
  await create.press('Enter');
  await expect(page).toHaveURL(/\/session\//);
  const fresh = page.url().split('/session/')[1];
  expect(fresh).not.toBe(original);
  await expect(page.locator('#msg')).toBeFocused();
  await expect(page.locator('#msg')).toHaveValue('');
  await expect.poll(async()=>(await api(page,endpoint)).body.assignments[fresh]).toBe(child);
  expect((await api(page,endpoint)).body.assignments[original]).toBe(parent);
  expect(creations).toBe(1);
  await send(page,'QA_SECTION_NEW');
  await page.goto('/?view=conversations');
  await pick(child);
  await expect(panel.locator('.conversation-board-card')).toHaveCount(1);
  await expect(panel.locator('[data-section-branch="'+child+'"] [data-conversation-open="'+fresh+'"]')).toBeVisible();
  await pick(parent);
  await panel.locator('[data-conversation-open="'+original+'"]').click();
  await expect(page.locator('#msg')).toHaveValue('Keep the delivery draft');
});

test('BOARD SECTIONS recovers the same new chat when the selected section was removed in another tab', async ({page}) => {
  await ready(page);
  const endpoint='/api/me/sidebar-organization';
  async function change(body:object){
    const {revision}=(await api(page,endpoint)).body;
    const result=await api(page,endpoint,{revision,...body});
    expect(result.status).toBe(200);
    return result.body;
  }
  const removed=(await change({action:'create',name:'Archive later'})).sections.at(-1).id;
  const fallback=(await change({action:'create',name:'Follow-up'})).sections.at(-1).id;
  await page.goto('/?view=conversations');
  await page.locator('#conversationBoardSectionPick').selectOption(removed);
  await change({action:'delete',id:removed});
  let creations=0;
  page.on('request', req=>{if(req.method()==='POST' && new URL(req.url()).pathname==='/api/session/new') creations++;});
  await page.locator('#conversationBoardNew').evaluate((el:HTMLButtonElement)=>{el.click();el.click();});
  await expect(page).toHaveURL(/\/session\//);
  const sid=page.url().split('/session/')[1];
  const recovery=page.locator('dialog.sidebar-org-dialog');
  await expect(recovery).toBeVisible();
  await recovery.locator('select').selectOption(fallback);
  await recovery.getByRole('button',{name:'Save'}).click();
  await expect(recovery).toHaveCount(0);
  expect((await api(page,endpoint)).body.assignments[sid]).toBe(fallback);
  expect(creations).toBe(1);
});

test('BOARD SECTIONS keeps shared project membership and leaves private draft attachments behind', async ({page}) => {
  await ready(page);
  const original=await session(page);
  const endpoint='/api/me/sidebar-organization';
  const {revision}=(await api(page,endpoint)).body;
  const org=await api(page,endpoint,{revision,action:'create',name:'Team handover'});
  expect(org.status).toBe(200);
  const id=org.body.sections.at(-1).id;
  const project=await api(page,'/api/projects/team',{name:'Support team',members:[],bot_participants:['default'],profile:'default'});
  expect(project.status).toBe(200);
  await page.reload();
  await expect(page.locator('#msg')).toBeVisible();
  await page.locator('#msg').fill('Private note for the previous conversation');
  await page.locator('#fileInput').setInputFiles({name:'personal-note.txt',mimeType:'text/plain',buffer:Buffer.from('Private QA example')});
  await expect.poll(()=>page.evaluate(()=>S.pendingFiles.length)).toBe(1);
  await page.locator('#btnConversationBoard').click();
  const chip=page.locator('.project-chip').filter({hasText:'Support team'});
  await chip.click();
  await expect(chip).toHaveClass(/active/);
  await page.locator('[data-create-in-section="'+id+'"]').click();
  await expect(page).toHaveURL(/\/session\//);
  const fresh=page.url().split('/session/')[1];
  expect(fresh).not.toBe(original);
  await expect.poll(async()=>(await api(page,endpoint)).body.assignments[fresh]).toBe(id);
  expect((await api(page,'/api/session?session_id='+fresh)).body.session.project_id).toBe(project.body.project.project_id);
  await expect(page.locator('#msg')).toHaveValue('');
  expect(await page.evaluate(()=>S.pendingFiles.length)).toBe(0);
  const draft=(await api(page,'/api/session?session_id='+original)).body.session.composer_draft;
  expect(draft.text).toBe('Private note for the previous conversation');
  expect(draft.files[0].name).toBe('personal-note.txt');
});

test('SECTION TREE shows each conversation in its own branch and remembers independent folds', async ({page}, info) => {
  test.setTimeout(90000);
  await ready(page);
  const endpoint = '/api/me/sidebar-organization';
  let org = (await api(page, endpoint)).body;
  async function change(body:object) {
    const update = await api(page, endpoint, {revision:org.revision, ...body});
    expect(update.status).toBe(200);
    org = update.body;
  }
  async function section(name:string, parent?:string) {
    await change({action:'create', name, parent});
    return org.sections.at(-1).id;
  }
  async function chat(title:string, section?:string) {
    const result = await api(page, '/api/session/import', {title, messages:[{role:'user',content:title}]});
    expect(result.status).toBe(200);
    const id = result.body.session.session_id;
    if (section) await change({action:'assign', session_id:id, section});
    return id;
  }
  const parent = await section('Client work');
  const child = await section('Launch', parent);
  const grandchild = await section('Design review', child);
  const sibling = await section('Operations');
  const meeting = await chat('Weekly client meeting', parent);
  const launch = await chat('Launch checklist', child);
  const design = await chat('Illustration feedback', grandchild);
  const ops = await chat('Office planning', sibling);
  const free = await chat('Loose notes');
  await page.goto('/?view=conversations');
  const board = page.locator('#mainConversations');
  const card = (id:string) => board.locator('[data-conversation-open="'+id+'"]');
  const branch = (id:string) => board.locator('[data-section-branch="'+id+'"]');
  const toggle = (id:string) => board.locator('[data-section-toggle="'+id+'"]');
  await expect(card(meeting)).toBeVisible();
  for (const width of [1440,900,390]) {
    await page.setViewportSize({width,height:920});
    if (width === 390) {
      await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(1);
      await board.locator('#conversationBoardCount').scrollIntoViewIfNeeded();
      await board.locator('#conversationBoardCount').evaluate(el=>el.scrollIntoView({block:'start'}));
    }
    await page.screenshot({path:info.outputPath('tree-'+width+'.png'),animations:'disabled'});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  }
  await expect(branch(parent)).toBeVisible({timeout:3000});
  for (const [sid,id] of [[meeting,parent],[launch,child],[design,grandchild],[ops,sibling],[free,'unassigned']]) {
    await expect(card(sid)).toHaveCount(1);
    expect(await card(sid).evaluate(el=>el.closest('[data-section-branch]')?.getAttribute('data-section-branch'))).toBe(id);
  }
  await toggle(child).press('Enter');
  await expect(card(launch)).toBeHidden();
  await expect(card(meeting)).toBeVisible();
  await toggle(parent).click();
  await expect(toggle(child)).toBeHidden();
  await expect(card(ops)).toBeVisible();
  await page.evaluate(()=>renderSessionListFromCache());
  await expect(toggle(parent)).toBeFocused();
  await page.reload();
  await expect(toggle(parent)).toHaveAttribute('aria-expanded','false');
  await board.locator('#conversationBoardSearch').fill('Illustration');
  await expect(card(design)).toBeVisible();
  await expect(card(ops)).toBeHidden();
  await board.locator('#conversationBoardSearch').fill('');
  await expect(toggle(parent)).toHaveAttribute('aria-expanded','false');
  await toggle(parent).click();
  await expect(toggle(child)).toHaveAttribute('aria-expanded','false');
  await toggle(child).click();
  await card(design).click();
  await expect(page).toHaveURL(new RegExp('/session/'+design+'$'));

  // A busy section must not consume another section's first page.
  for (let i=1; i<=7; i++) await chat('Facilities update ' + i, sibling);
  await page.goto('/?view=conversations');
  await expect(branch(sibling).locator('[data-conversation-open]')).toHaveCount(6);
  await expect(card(meeting)).toBeVisible();
  await branch(sibling).locator('[data-more-in-section]').click();
  await expect(branch(sibling).locator('[data-conversation-open]')).toHaveCount(8);
  const foldAll = board.locator('#conversationBoardFoldAll');
  await foldAll.click();
  await expect(board.locator('[data-conversation-open]')).toHaveCount(0);
  await foldAll.click();
  await expect(card(design)).toBeVisible();
  await expect(card(free)).toBeVisible();
});
