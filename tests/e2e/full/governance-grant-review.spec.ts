import fs from 'node:fs';
import path from 'node:path';
import {test,expect,open,auth,api,capture} from './fixtures';

// The model transport is deterministic; denial, spool ingestion, review,
// queue decision, policy mutation, audit and the next HTTP authorization are real.
for(const [user,decision,status] of [
 ['grantapprove','approve','approved'],['grantdeny','deny','rejected'],
 ['grantmanual','manual','pending'],['grantharddeny','manual','pending'],
] as const)test(`US-SP-GOV-GRANT-REVIEW-${user} real access request follows reviewer verdict and current policy bounds`,async({page,browser},info)=>{
 await open(page);const email=user+'@example.test',route='/api/skills',key=email+'|route|'+route;
 const requester=await browser.newContext({baseURL:auth.base_url});
 await requester.addCookies([{name:auth.cookie_name,value:auth.cookies[user],url:auth.base_url}]);
 try{
  const denied=await requester.request.get(route);expect(denied.status()).toBe(403);expect((await denied.json()).reason).toBe('route_not_allowed');
  const own=async()=>{const r=await requester.request.get('/api/governance/approvals/mine');expect(r.status()).toBe(200);const data=await r.json();expect(data.owner_email).toBe(email);return data.requests.find((r:any)=>r.key===key);};
  let row:any;await expect.poll(async()=>{row=await own();return row?.payload?.auto_review?.decision;},{timeout:35000}).toBe(decision);
  expect(row).toMatchObject({kind:'grant',key,owner_email:email,status,payload:{email,gkind:'route',value:route,auto_review:{decision,decided:status!=='pending'}}});
  // A subsequent requester refresh must retain the review and its reason.
  expect((await own()).payload.auto_review).toEqual(row.payload.auto_review);
  const policy=(await api(page,'/api/governance/users')).body.users[email];
  expect((policy.grants.routes||[]).includes(route)).toBe(status==='approved');
  expect((await requester.request.get(route)).status()).toBe(status==='approved'?200:403);
  // A grant to this one route does not silently widen the route allowlist.
  expect((await requester.request.get('/api/skills/usage')).status()).toBe(403);
  expect((await requester.request.post('/api/governance/approvals/decide',{data:{kind:'grant',key,decision:'approve'}})).status()).toBe(403);
  const providerFile=path.join(path.dirname(process.env.QA_SESSIONS!),'provider-evidence.jsonl');
  const reviews=fs.readFileSync(providerFile,'utf8').trim().split('\n').filter(Boolean).map(s=>JSON.parse(s)).filter(r=>r.review);
  expect(reviews.some(r=>r.review_decision===(user==='grantharddeny'?'approve':decision))).toBe(true);
  if(status!=='pending'){
   const audit=(await api(page,'/api/governance/audit?limit=100')).body.events;
   expect(audit.some((e:any)=>e.event==='approval_decision'&&e.extra?.key==='grant:'+key&&e.extra?.origin==='automatic'&&e.extra?.op==='approvals.'+(status==='approved'?'approve':'reject'))).toBe(true);
   expect((await api(page,'/api/governance/approvals/decide',{kind:'grant',key,decision:'approve'})).status).toBe(409);
  }else{
   if(user==='grantharddeny')expect(row.payload.auto_review.reason).toContain('not applied');
   await open(page,'governance');await page.locator('[data-gov-tab="approvals"]').click();
   const button=page.locator(`[data-gov-approval="${user==='grantharddeny'?'reject':'approve'}"][data-key=${JSON.stringify(key)}]`);
   await expect(button).toBeVisible();await capture(page,'manual-access-request-'+user,info);
   // Explicit denials also block a human granting through this queue.
   if(user==='grantharddeny')expect((await api(page,'/api/governance/approvals/decide',{kind:'grant',key,decision:'approve'})).status).toBe(403);
   const saved=page.waitForResponse(r=>r.url().endsWith('/api/governance/approvals/decide')&&r.request().method()==='POST');
   await button.click();expect((await saved).status()).toBe(200);
   expect((await own()).status).toBe(user==='grantharddeny'?'rejected':'approved');
   expect((await requester.request.get(route)).status()).toBe(user==='grantharddeny'?403:200);
  }
  await info.attach('real-access-request-review',{body:JSON.stringify(row),contentType:'application/json'});
 }finally{await requester.close();}
});
