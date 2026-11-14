import { test, expect, open, session, api, auth } from './fixtures';
test.use({user:'alice'});
test('US-SP-CHAT-001 real composer dispatches through real engine and deterministic provider, persists reload',async({page})=>{
 await open(page);const sid=await session(page);await page.locator('#msg').fill('QA_FRONTEND_CHAT');await page.locator('#btnSend').click();
 await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_FRONTEND_CHAT',{timeout:30000});
 const read=await api(page,`/api/session?session_id=${sid}`);expect(read.status).toBe(200);
 expect(read.body.session.messages.some((m:any)=>m.role==='assistant'&&String(m.content).includes('QA_REPLY: QA_FRONTEND_CHAT'))).toBe(true);
 await page.reload();await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_FRONTEND_CHAT');
});
test('US-SP-CHAT-002 modes change through header and survive reload',async({page})=>{
 await open(page);const sid=await session(page);await page.locator('#chatModeNormal').click();await expect(page.locator('#chatModeNormal')).toHaveAttribute('aria-pressed','true');
 await expect.poll(async()=>(await api(page,`/api/session?session_id=${sid}`)).body.session.chat_mode).toBe('normal');
 await page.reload();await expect(page.locator('#chatModeNormal')).toHaveAttribute('aria-pressed','true');await page.locator('#chatModeSuper').click();
 await expect.poll(async()=>(await api(page,`/api/session?session_id=${sid}`)).body.session.chat_mode).toBe('super');
});
test('US-SP-CHAT-004 Shift Enter inserts newline without sending',async({page})=>{
 await open(page);const sid=await session(page);await page.locator('#msg').fill('Line one');await page.locator('#msg').press('Shift+Enter');await page.locator('#msg').pressSequentially('Line two');
 await expect(page.locator('#msg')).toHaveValue('Line one\nLine two');expect((await api(page,`/api/session?session_id=${sid}`)).body.session.messages).toEqual([]);
});
test('US-SP-GROUP-001 selecting named colleague stages participants and persists actual group',async({page})=>{
 await open(page);await page.locator('#composerPeopleChip').click();await expect(page.locator('#groupPeopleModal')).toBeVisible();
 await page.locator('#groupPeopleList label').filter({hasText:'bob@example.test'}).locator('input').check();await page.locator('#groupPeopleModalSubmit').click();
 await expect(page.locator('#composerPeopleLabel')).not.toHaveText('Just me');
 await page.locator('#msg').fill('QA_GROUP_FRONTEND');await page.locator('#btnSend').click();
 await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_GROUP_FRONTEND',{timeout:30000});
 const sid=page.url().split('/session/')[1];expect((await api(page,`/api/session?session_id=${sid}`)).body.session.participants).toContain('bob@example.test');
});
test.describe('voice policy prerequisite',()=>{
test.use({user:'voicedenied'});
test('US-SP-VOICE-001 voice policy denial explains prerequisite without hanging',async({page})=>{
 await open(page); const sid=await session(page);
 const capability=await api(page,'/api/voice/realtime/capability');
 expect(capability.status).toBe(200); expect(capability.body.available).toBe(false);
 expect(capability.body.help).toMatch(/administrator.*speech/i);
 const calls:string[]=[]; page.on('request',r=>{if(r.url().endsWith('/api/voice/realtime/call')) calls.push(r.method());});
 await page.locator('#btnRealtimeVoice').click();
 await expect(page.getByText(capability.body.help,{exact:true})).toBeVisible();
 expect(calls).toEqual([]);
 expect((await api(page,'/api/voice/realtime/call',{session_id:sid,sdp:'v=0'})).status).toBe(403);
});
});
test('US-SP-CHAT-ROSTER bot roster hides and shows through its real toggle',async({page})=>{
 await open(page);await expect(page.locator('#chatBotRoster button[data-bot="default"]')).toBeVisible();await expect(page.locator('#chatBotRoster button[data-bot="qa-research"]')).toBeVisible();await page.locator('#chatBotRosterToggle').click();
 await expect(page.locator('#chatBotRoster button[data-bot]').first()).toBeHidden();await page.locator('#chatBotRosterToggle').click();await expect(page.locator('#chatBotRoster button[data-bot]').first()).toBeVisible();
});
