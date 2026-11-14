import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {test,expect,open,session,auth,api,capture} from './fixtures';
const auditPath=path.join(path.dirname(path.resolve(process.env.QA_SESSIONS!)),'home','dashboard-governance-audit.jsonl');
function audits(){return fs.existsSync(auditPath)?fs.readFileSync(auditPath,'utf8').trim().split('\n').filter(Boolean).map(s=>JSON.parse(s)):[];}
async function audited(sid:string,user:string,decision:string,info:any){
 let row:any;
 await expect.poll(()=>{row=audits().find(r=>r.event==='action_approval'&&r.extra?.session_id===sid);return !!row;}).toBe(true);
 expect(row.extra).toMatchObject({decision,source:'manual',tool:'terminal',session_id:sid});expect(row.extra.policy_revision).toMatch(/^[a-f0-9]{64}$/);expect(row.extra.operation_id).toMatch(/^[a-f0-9]{32}$/);expect(row.reason.trim().length).toBeGreaterThan(0);expect(row.subject_email_hash).toBe(crypto.createHash('sha256').update(user+'@example.test').digest('hex').slice(0,24));
 await info.attach('persisted-governance-audit',{body:JSON.stringify(row,null,2),contentType:'application/json'});
}
for(const user of ['autoapprove','autodeny','automanual','manualapprove','manualdeny'] as const)test.describe(user,()=>{
 test.use({user});test(`US-SP-POL-RUNTIME-${user} access-request approval settings do not intercept an already permitted file write`,async({page},info)=>{
  await open(page);const sid=await session(page);const target=path.join(auth.workspace,'qa-governance-'+user+'-'+Date.now()+'.txt');
  expect(fs.existsSync(target)).toBe(false);await page.locator('#msg').fill('QA_GOV_WRITE|'+target);await page.locator('#btnSend').click();
  await expect(page.locator('#messages')).toContainText('QA_GOV_TOOL_RESULT:',{timeout:35000});
  expect(fs.readFileSync(target,'utf8')).toBe('QA_GOVERNANCE_EXECUTED');await expect(page.locator('#approvalCard')).toBeHidden();
  expect(audits().filter(r=>r.event==='action_approval'&&r.extra?.session_id===sid)).toEqual([]);await capture(page,user,info);
 });
});
for(const [user,choice] of [['clionce','once'],['clideny','deny'],['cliauto','once']] as const)test.describe(user,()=>{
 test.use({user});test(`US-SP-POL-RUNTIME-${user} mandatory CLI review waits for exact frontend ${choice} decision`,async({page},info)=>{
  await open(page);const sid=await session(page);const name='qa-cli-'+user+'-'+Date.now()+'.txt',target=path.join(auth.workspace,name);
  expect((await api(page,'/api/session/toolsets',{session_id:sid,toolsets:['terminal']})).status).toBe(200);
  await page.locator('#msg').fill('QA_SUPPLEMENT_CLI|'+name);await page.locator('#btnSend').click();
  await expect(page.locator('#approvalCard')).toBeVisible({timeout:30000});expect(fs.existsSync(target)).toBe(false);await expect(page.locator('#approvalCard')).toContainText('requires manual approval');
  await expect(page.locator('#approvalBtnSession')).toBeHidden();await expect(page.locator('#approvalBtnAlways')).toBeHidden();await expect(page.locator('#approvalSkipAll')).toBeHidden();await capture(page,'pending-action-'+user,info);
  const pending=(await api(page,'/api/approval/pending?session_id='+sid)).body.pending;expect(pending.approval_id).toBeTruthy();
  const responsePromise=page.waitForResponse(r=>r.url().endsWith('/api/approval/respond')&&r.request().method()==='POST');
  await page.locator(choice==='once'?'#approvalBtnOnce':'#approvalBtnDeny').click();const response=await responsePromise;
  expect(response.status(),await response.text()).toBe(200);expect(response.request().postDataJSON()).toMatchObject({session_id:sid,approval_id:pending.approval_id,choice});
  await expect(page.locator('#messages')).toContainText('QA_SUPPLEMENT_TOOL_RESULT:',{timeout:35000});expect(fs.existsSync(target)).toBe(choice==='once');if(choice==='once')expect(fs.statSync(target).size).toBe(0);await expect(page.locator('#approvalCard')).toBeHidden();
  await audited(sid,user,choice==='once'?'approve':'deny',info);
 });
});
