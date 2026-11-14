import { test, expect, open, api } from './fixtures';
test.use({user:'alice'});
test('US-SP-BOT-HANDOFF delegates through real engine and replies in the same chat',async({page},testInfo)=>{
 test.setTimeout(90000);
 await open(page);
 const created=await api(page,'/api/session/new',{profile:'default',chat_mode:'super',bot_participants:['default','qa-research']});
 expect(created.status).toBe(200);
 const sid=created.body.session.session_id;
 await page.goto('/session/'+sid);
 // The composer mounts before the selected conversation finishes hydrating.
 await page.waitForFunction(id=>typeof S!=='undefined'&&S._bootReady&&_loadingSessionId===null&&S.session?.session_id===id,sid);
 await page.locator('#msg').fill('@default QA_BOT_HANDOFF_START');await page.locator('#btnSend').click();
 await expect(page.locator('#messages')).toContainText('QA_BOT_HANDOFF_DONE',{timeout:60000});
 const read=(await api(page,'/api/session?session_id='+sid)).body.session;
 const reply=read.messages.find((m:any)=>m.role==='assistant'&&String(m.content).includes('QA_BOT_HANDOFF_DONE'));
 expect(reply.bot_profile).toBe('qa-research');
 const task=read.messages.find((m:any)=>m.role==='user'&&m.bot_delegation);
 expect(task.bot_delegation.from).toBe('default');expect(task.bot_delegation.to).toBe('qa-research');
 expect(task.author_email).toBeUndefined();
 expect(read.session_id).toBe(sid);
 await page.reload();await expect(page.locator('#messages')).toContainText('QA_BOT_HANDOFF_DONE');
 await expect(page.locator('#messages')).not.toContainText('not a new human instruction');
 // The task row and its tooltip use display names; persisted metadata above keeps the exact profile IDs.
 const author=page.locator('.msg-author.sp-handoff-author').first();
 await expect(author).toBeVisible();
 await expect(author).toHaveAttribute('title','Standard bot ↪ Research');
 await expect(author).not.toContainText('qa-research');
 await expect(page.locator('details.sp-handoff-task').first()).toBeVisible();
 await page.screenshot({path:testInfo.outputPath('handoff-desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});
 await expect(author).toBeVisible();
 await page.screenshot({path:testInfo.outputPath('handoff-mobile.png'),fullPage:true});
});
