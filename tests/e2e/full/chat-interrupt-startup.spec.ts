import { test, expect, open, session, api, auth } from './fixtures';
import fs from 'node:fs';
import path from 'node:path';

const state = path.dirname(process.env.QA_SESSIONS!);
function evidence(sid: string) {
  const file = path.join(state, 'interrupt-evidence.jsonl');
  return fs.existsSync(file) ? fs.readFileSync(file, 'utf8').trim().split('\n')
    .filter(Boolean).map(line => JSON.parse(line)).filter(row => row.session_id === sid) : [];
}
function arm(sid: string, mode: string) {
  fs.writeFileSync(path.join(state, 'interrupt-control.json'), JSON.stringify({session_id:sid, mode}));
}
function providerTurns(marker: string) {
  return fs.readFileSync(path.join(state, 'provider-evidence.jsonl'), 'utf8').trim().split('\n')
    .filter(Boolean).map(line => JSON.parse(line))
    .filter(row => row.tools_offered?.length && row.last_user_text?.endsWith('\n' + marker));
}

test('US-SP-CHAT-008 startup interrupt leaves the cached successor able to answer', async ({page}, info) => {
  await open(page); const sid = await session(page);
  arm(sid, 'cancel_initialization');
  await page.locator('#msg').fill('QA_STARTUP_CANCEL_ORIGINAL');
  await page.locator('#btnSend').click();
  await expect.poll(() => evidence(sid).some(r => r.phase === 'initialization_held')).toBe(true);
  // A real cancel SSE may settle before the matching HTTP acknowledgement.
  // Hold only that response until the queued successor has had its first
  // opportunity to drain while the interrupt command still owns the composer.
  await page.route('**/api/chat/cancel?*', async route => {
    const response=await route.fetch();
    await page.waitForFunction(sid=>!S.busy&&_getSessionQueue(sid,false).some(entry=>entry._state==='queued'),sid);
    await route.fulfill({response});
  });
  await page.locator('#msg').fill('/interrupt QA_STARTUP_CANCEL_SUCCESSOR');
  await page.locator('#btnSend').click();
  await expect.poll(() => evidence(sid).some(r => r.phase === 'interrupt' && r.reason === 'Cancelled before start')).toBe(true);
  await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_STARTUP_CANCEL_SUCCESSOR', {timeout:30000});
  const stored = (await api(page, '/api/session?session_id=' + sid)).body.session;
  const answers = stored.messages.filter((m:any) => m.role === 'assistant').map((m:any) => m.content).join('\n');
  expect(answers).not.toContain('QA_REPLY: QA_STARTUP_CANCEL_ORIGINAL');
  expect(answers).toContain('QA_REPLY: QA_STARTUP_CANCEL_SUCCESSOR');
  expect(providerTurns('QA_STARTUP_CANCEL_ORIGINAL')).toHaveLength(0);
  expect(providerTurns('QA_STARTUP_CANCEL_SUCCESSOR')).toHaveLength(1);
  expect(evidence(sid).filter(r => r.phase === 'conversation_entered')).toEqual([
    {session_id:sid, phase:'conversation_entered', interrupted:false},
  ]);
  await info.attach('startup-interrupt-evidence', {body:JSON.stringify(evidence(sid)), contentType:'application/json'});
});

test('US-SP-CHAT-009 Stop during cached initialization prevents provider admission and permits a later turn', async ({page}, info) => {
  await open(page); const sid = await session(page);
  await page.locator('#msg').fill('QA_STARTUP_WARM'); await page.locator('#btnSend').click();
  await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_STARTUP_WARM');
  await expect.poll(async () => (await api(page, '/api/session/status?session_id=' + sid)).body.agent_running).toBe(false);
  arm(sid, 'cancel_reuse');
  await page.locator('#msg').fill('QA_STOP_DURING_REUSE'); await page.locator('#btnSend').click();
  await expect.poll(() => evidence(sid).some(r => r.phase === 'initialization_held')).toBe(true);
  const cancelled = page.waitForResponse(r => r.url().includes('/api/chat/cancel?') && r.status() === 200);
  await page.locator('#btnSend').click();
  await cancelled;
  fs.writeFileSync(path.join(state, 'interrupt-release-' + sid), 'release after actual cancel response');
  await expect.poll(() => evidence(sid).some(r => r.phase === 'interrupt' && r.reason === 'Cancelled before start')).toBe(true);
  await expect.poll(async () => (await api(page, '/api/session/status?session_id=' + sid)).body.agent_running).toBe(false);
  expect(evidence(sid).filter(r => r.phase === 'conversation_entered')).toEqual([]);
  expect(evidence(sid).find(r => r.phase === 'reuse_released')?.cancel_flag_present).toBe(false);
  expect(providerTurns('QA_STOP_DURING_REUSE')).toHaveLength(0);
  await page.locator('#msg').fill('QA_AFTER_REUSE_STOP'); await page.locator('#btnSend').click();
  await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_AFTER_REUSE_STOP', {timeout:30000});
  const saved = (await api(page, '/api/session?session_id=' + sid)).body.session;
  expect(saved.messages.filter((m:any) => m.role === 'assistant').map((m:any) => m.content).join('\n'))
    .not.toContain('QA_REPLY: QA_STOP_DURING_REUSE');
  expect(evidence(sid).filter(r => r.phase === 'conversation_entered')).toEqual([
    {session_id:sid, phase:'conversation_entered', interrupted:false},
  ]);
  expect(providerTurns('QA_AFTER_REUSE_STOP')).toHaveLength(1);
  await info.attach('startup-stop-evidence', {body:JSON.stringify(evidence(sid)), contentType:'application/json'});
});

test('US-SP-CHAT-008 an aged cancelled worker retains its interrupt until its held reply settles', async ({page}, info) => {
  await open(page); const sid = await session(page);
  const target = path.join(auth.workspace, 'qa-cancelled-old-worker-' + sid + '.txt');
  arm(sid, 'cancelled_worker_response');
  await page.locator('#msg').fill('QA_GOV_WRITE|' + target); await page.locator('#btnSend').click();
  await expect.poll(() => evidence(sid).some(row => row.phase === 'provider_response_held')).toBe(true);
  const nextStart = page.waitForResponse(r => r.url().endsWith('/api/chat/start') &&
    r.request().postDataJSON()?.message === 'QA_AFTER_AGED_CANCEL');
  await page.locator('#msg').fill('/interrupt QA_AFTER_AGED_CANCEL'); await page.locator('#btnSend').click();
  try {
    const pending = await nextStart;
    if (pending.status() === 200) {
      // On the broken implementation, retain the response barrier long enough
      // to record the unsafe reset before reporting the admission failure.
      await expect.poll(() => evidence(sid).some(row => row.phase === 'interrupt_reset_while_response_held')).toBe(true);
    }
    expect(pending.status(), 'real worker lifetime must outrank the advisory180s registry ceiling').toBe(409);
    await expect.poll(() => evidence(sid).some(row => row.phase === 'cancelled_worker_aged')).toBe(true);
    expect(evidence(sid).filter(row => row.phase === 'interrupt_reset_while_response_held')).toEqual([]);
    expect(fs.existsSync(target)).toBe(false);
  } finally {
    fs.writeFileSync(path.join(state, 'interrupt-release-' + sid), 'release actual provider reply');
  }
  await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_AFTER_AGED_CANCEL', {timeout:30000});
  await expect.poll(async () => (await api(page, '/api/session/status?session_id=' + sid)).body.agent_running).toBe(false);
  expect(evidence(sid).find(row => row.phase === 'provider_response_released')?.interrupted).toBe(true);
  expect(evidence(sid).filter(row => row.phase === 'interrupt_reset_while_response_held')).toEqual([]);
  expect(fs.existsSync(target)).toBe(false);
  const saved = (await api(page, '/api/session?session_id=' + sid)).body.session;
  expect(saved.messages.filter((m:any) => m.role === 'user' && m.content === 'QA_AFTER_AGED_CANCEL')).toHaveLength(1);
  expect(saved.messages.filter((m:any) => m.role === 'assistant' && String(m.content).includes('QA_REPLY: QA_AFTER_AGED_CANCEL'))).toHaveLength(1);
  await info.attach('aged-worker-cancel-evidence', {body:JSON.stringify(evidence(sid)), contentType:'application/json'});
});
