import fs from 'node:fs';
import path from 'node:path';
import {test,expect,open,session,auth,api,capture,picker,selectPicker} from './fixtures';

const email='climcp@example.test';
const unique=(kind:string)=>'qa-cli-'+kind+'-'+Date.now()+'.txt';
async function identity(page:any,context:any,user:string){
 await context.addCookies([{name:auth.cookie_name,value:auth.cookies[user],url:auth.base_url,httpOnly:true,sameSite:'Lax'}]);await open(page);
}
async function chip(page:any,id:string,value:string){await selectPicker(page,id+'Input',value);}
async function entry(page:any){return (await api(page,'/api/governance/users')).body.users[email];}
async function seed(page:any){
 const snapshot=await api(page,'/api/governance/users');
 const policy={roles:['member','qa_cli_mcp'],access_level:'elevated',access_mode:'whitelist',approval:{mode:'manual',prompt:''},
  grants:{permissions:['*'],profiles:['*'],routes:['*'],workspaces:[auth.workspace],models:{models:['*'],providers:['*']},
   tools:{builtins:['terminal','read_file'],toolsets:['terminal','file']},
   files:{read_roots:[auth.workspace],write_roots:[auth.workspace]},
   cli:{workdir_roots:[auth.workspace]},mcp:{tools:{'qa-stdio':['*']}}}};
 const response=await page.evaluate(async({etag,email,policy})=>{const r=await fetch('/api/governance/users/update',{method:'POST',headers:{'Content-Type':'application/json','If-Match':etag},body:JSON.stringify({email,entry:policy})});return {status:r.status,body:await r.json()};},{etag:snapshot.body.etag,email,policy});expect(response.status,JSON.stringify(response.body)).toBe(200);
}
async function edit(page:any){await open(page,'governance');await page.locator('[data-gov-tab="users"]').click();await page.locator('#govPaneUsers tr').filter({hasText:email}).getByRole('button',{name:'Edit',exact:true}).click();}
async function save(page:any){const pending=page.waitForResponse((r:any)=>r.url().endsWith('/api/governance/users/update')&&r.request().method()==='POST');await page.locator('#govUserSave').click();expect((await pending).status()).toBe(200);await expect(page.locator('#govUserEmail')).toHaveValue('');}
async function invoke(page:any,marker:string,toolsets:string[]){
 const previous=page.url();await session(page);await expect.poll(()=>page.url()).not.toBe(previous);
 const sid=page.url().split('/session/')[1].split(/[?#]/)[0];expect((await api(page,'/api/session/toolsets',{session_id:sid,toolsets})).status).toBe(200);
 if(toolsets.some(name=>name.startsWith('mcp-'))){await page.locator('#chatModeSuper').click();await expect.poll(async()=>(await api(page,'/api/session?session_id='+sid)).body.session.chat_mode).toBe('super');}
 await page.locator('#msg').fill(marker);await page.locator('#btnSend').click();return sid;
}
async function complete(page:any){await expect(page.locator('#messages')).toContainText('QA_SUPPLEMENT_TOOL_RESULT:',{timeout:45000});await expect(page.locator('#approvalCard')).toBeHidden();}
function effects(){const file=path.join(auth.workspace,'qa-mcp-effects.jsonl');return fs.existsSync(file)?fs.readFileSync(file,'utf8').trim().split('\n').filter(Boolean).map(x=>JSON.parse(x)):[];}
async function pending(page:any,sid:string){await expect(page.locator('#approvalCard')).toBeVisible({timeout:30000});const p=await api(page,'/api/approval/pending?session_id='+sid);expect(p.status).toBe(200);await expect(page.locator('#approvalBtnSession')).toBeHidden();await expect(page.locator('#approvalBtnAlways')).toBeHidden();return p.body;}
async function mcpEnabled(page:any,enabled:boolean){
 await open(page,'settings');await page.locator('[data-settings-section="system"]').click();
 const row=page.locator('.mcp-server-row').filter({has:page.locator('.mcp-server-name',{hasText:'qa-stdio'})});await expect(row).toBeVisible();
 const status=(await api(page,'/api/mcp/servers')).body.servers.find((s:any)=>s.name==='qa-stdio');
 if(status.enabled!==enabled){const response=page.waitForResponse((r:any)=>r.url().endsWith('/api/mcp/servers/qa-stdio')&&r.request().method()==='PATCH');await row.locator('.mcp-toggle-btn').click();expect((await response).status()).toBe(200);}
 expect((await api(page,'/api/mcp/servers')).body.servers.find((s:any)=>s.name==='qa-stdio').enabled).toBe(enabled);
 await page.locator('.rail-btn[data-panel="chat"]').click();await session(page);await page.locator('#msg').fill('/reload-mcp');await page.locator('#btnSend').click();await expect(page.locator('#messages')).toContainText('Reloaded MCP servers from configuration.',{timeout:30000});
}

test('SUPPLEMENT CLI MCP command grants manual Once Deny and wildcard hard denial govern actual harmless process effects',async({page,context},info)=>{
 test.setTimeout(150000);await open(page);await seed(page);await edit(page);await chip(page,'govUserCliCommands','touch');await chip(page,'govUserCliApproval','touch');await save(page);await page.reload();
 expect((await entry(page)).grants.cli).toMatchObject({commands:['touch'],approval_commands:['touch'],workdir_roots:[auth.workspace]});
 for(const choice of ['once','deny']){
  await identity(page,context,'climcp');const file=unique(choice),target=path.join(auth.workspace,file);const sid=await invoke(page,'QA_SUPPLEMENT_CLI|'+file,['terminal']);
  await pending(page,sid);expect(fs.existsSync(target)).toBe(false);await capture(page,'cli-pending-'+choice,info);
  await page.locator(choice==='once'?'#approvalBtnOnce':'#approvalBtnDeny').click();await complete(page);expect(fs.existsSync(target)).toBe(choice==='once');if(choice==='once')expect(fs.statSync(target).size).toBe(0);
 }
 await identity(page,context,'admin');await edit(page);await picker(page,'govUserCliCommandsInput').locator('.sp-chip-x').click();await chip(page,'govUserCliCommands','*');await chip(page,'govUserDenyCli','touch');await save(page);await page.reload();
 expect((await entry(page)).grants.cli.commands).toEqual(['*']);expect((await entry(page)).deny.cli.commands).toEqual(['touch']);
 await page.locator('.rail-btn[data-panel="governance"]').click();await page.locator('[data-gov-tab="preview"]').click();await page.locator('#govPreviewEmail').fill(email);await page.locator('[onclick="_govRunPreview()"]').click();await expect(page.locator('#govPreviewResult')).toContainText('cli / commands: touch');
 await identity(page,context,'climcp');const file=unique('hard-deny');await invoke(page,'QA_SUPPLEMENT_CLI|'+file,['terminal']);await complete(page);expect(fs.existsSync(path.join(auth.workspace,file))).toBe(false);await expect(page.locator('#messages')).toContainText('cli_command_denied');
});

test('SUPPLEMENT CLI MCP real initialized stdio catalog has exact schemas search and next previous paging',async({page},info)=>{
 test.setTimeout(90000);await open(page);
 try{
  await mcpEnabled(page,true);const data=await api(page,'/api/mcp/tools');expect(data.status).toBe(200);const tools=data.body.tools.filter((t:any)=>t.server==='qa-stdio');expect(tools).toHaveLength(8);
  await open(page,'settings');await page.locator('[data-settings-section="system"]').click();const names=page.locator('#mcpToolList .mcp-tool-name');
  const sorted=tools.map((t:any)=>t.name).sort();await expect(names).toHaveText(sorted.slice(0,5));const buttons=page.locator('#mcpToolPager .mcp-tool-page-btn');await expect(buttons.first()).toBeDisabled();await buttons.last().click();await expect(names).toHaveText(sorted.slice(5));await expect(buttons.last()).toBeDisabled();await buttons.first().click();await expect(names).toHaveText(sorted.slice(0,5));
  await page.locator('#mcpToolSearch').fill('qa_mark');await expect(names).toHaveCount(1);await expect(page.locator('.mcp-tool-schema')).toContainText('marker*: string');await expect(page.locator('#mcpToolPager')).toBeEmpty();
  await page.locator('#mcpToolSearch').fill('QA_NO_SUCH_TOOL');await expect(names).toHaveCount(0);await page.locator('#mcpToolSearch').fill('');await page.locator('#mcpToolToolbar select').selectOption('10');await expect(names).toHaveText(sorted);await expect(page.locator('#mcpToolPager')).toBeEmpty();await capture(page,'populated-mcp-catalog',info);
 }finally{await mcpEnabled(page,false);}
});

test('SUPPLEMENT CLI MCP server grant permits real invocation while explicit server deny blocks dispatch and catalog disclosure',async({page,context},info)=>{
 test.setTimeout(150000);await open(page);await seed(page);
 try{
  await mcpEnabled(page,true);await edit(page);await chip(page,'govUserMcpServers','qa-stdio');await save(page);await page.reload();expect((await entry(page)).grants.mcp).toMatchObject({servers:['qa-stdio'],tools:{'qa-stdio':['*']}});
  await identity(page,context,'climcp');const allowed='QA_MCP_ALLOWED_'+Date.now();const sid=await invoke(page,'QA_SUPPLEMENT_MCP|'+allowed,['mcp-qa-stdio']);await complete(page);await expect(page.locator('#approvalCard')).toBeHidden();expect(effects().filter(r=>r.marker===allowed)).toHaveLength(1);await expect(page.locator('#messages')).toContainText('QA_MCP_EXECUTED:'+allowed);
  await identity(page,context,'admin');await edit(page);await picker(page,'govUserMcpServersInput').locator('.sp-chip-x').click();await chip(page,'govUserMcpServers','*');await chip(page,'govUserDenyMcp','qa-stdio');await save(page);await page.reload();expect((await entry(page)).deny.mcp.servers).toEqual(['qa-stdio']);
  await identity(page,context,'climcp');const denied='QA_MCP_DENIED_'+Date.now();await invoke(page,'QA_SUPPLEMENT_MCP|'+denied,['mcp-qa-stdio']);await complete(page);expect(effects().some(r=>r.marker===denied)).toBe(false);await expect(page.locator('#messages')).toContainText(/denied|not.allowed|not available|blocked/i);await capture(page,'mcp-explicit-denial',info);
  // A permitted catalog route must not disclose a specifically denied server.
  await open(page,'settings');await expect(page.locator('[data-settings-section="system"]')).toBeHidden();await capture(page,'restricted-system-navigation',info);
  // System navigation is admin-only; its hidden state does not secure the
  // separately permitted read endpoint against this signed elevated user.
  const catalog=await api(page,'/api/mcp/tools');expect(catalog.status).toBe(200);expect(catalog.body.tools.filter((t:any)=>t.server==='qa-stdio'),'explicitly denied MCP server schemas must be absent').toEqual([]);
 }finally{await identity(page,context,'admin');await mcpEnabled(page,false);}
});

test('SUPPLEMENT CLI MCP mandatory command review stays manual while an already permitted read needs no per-call review',async({page,context},info)=>{
 test.setTimeout(150000);await open(page);const original=await entry(page);
 const control=unique('automatic-control'),controlPath=path.join(auth.workspace,control);
 fs.writeFileSync(controlPath,'QA_CLI_AUTOMATIC_CONTROL');
 const reviews=()=>{
  const file=path.join(path.dirname(auth.workspace),'provider-evidence.jsonl');
  return fs.existsSync(file)?fs.readFileSync(file,'utf8').split('\n').filter(Boolean).map(line=>JSON.parse(line)).filter(row=>row.review):[];
 };
 try{
  await seed(page);await edit(page);await chip(page,'govUserCliCommands','touch');await chip(page,'govUserCliApproval','touch');
  await page.locator('#govUserApprovalMode').selectOption('automatic');
  await page.locator('#govUserApprovalPrompt').fill('QA_ALLOW_ONLY: approve harmless synthetic access requests.');
  await save(page);await page.reload();const policy=await entry(page);
  expect(policy.approval.mode).toBe('automatic');expect(policy.grants.cli.approval_commands).toEqual(['touch']);
  await identity(page,context,'climcp');const before=reviews().length;
  const readSid=await invoke(page,'QA_GOV_READ|'+controlPath,['file']);
  await expect(page.locator('#messages')).toContainText('QA_GOV_TOOL_RESULT:',{timeout:30000});
  await expect(page.locator('#messages')).toContainText('QA_CLI_AUTOMATIC_CONTROL');await expect(page.locator('#approvalCard')).toBeHidden();
  await expect.poll(async()=>(await api(page,'/api/session/status?session_id='+readSid)).body.agent_running).toBe(false);
  expect(reviews().slice(before)).toEqual([]);
  const afterRead=reviews().length;const decisions=[];
  for(const choice of ['once','deny']){
   const file=unique('mandatory-'+choice),target=path.join(auth.workspace,file);
   const sid=await invoke(page,'QA_SUPPLEMENT_CLI|'+file,['terminal']);
   await pending(page,sid);expect(fs.existsSync(target)).toBe(false);
   expect(reviews()).toHaveLength(afterRead);
   await expect(page.locator('#approvalCard')).toContainText('requires manual approval');
   const answered=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/approval/respond'&&r.request().method()==='POST');
   await page.locator(choice==='once'?'#approvalBtnOnce':'#approvalBtnDeny').click();const accepted=await answered;
   expect(accepted.status()).toBe(200);expect((await accepted.json()).ok).toBe(true);
   const answer=accepted.request().postDataJSON();expect(answer.approval_id).toBeTruthy();expect(answer.session_id).toBe(sid);expect(answer.choice).toBe(choice);await complete(page);
   await expect.poll(async()=>(await api(page,'/api/session/status?session_id='+sid)).body.agent_running).toBe(false);
   expect(fs.existsSync(target)).toBe(choice==='once');expect(reviews()).toHaveLength(afterRead);
   decisions.push({session_id:sid,choice,approval_id:answer.approval_id,file_effect:fs.existsSync(target)});
  }
  await info.attach('manual-cli-floor-with-allowed-read',{body:JSON.stringify({read_session:readSid,access_request_reviews:reviews().slice(before),decisions}),contentType:'application/json'});
  await capture(page,'mandatory-cli-denied-with-automatic-policy',info);
 }finally{
  await identity(page,context,'admin');const current=await api(page,'/api/governance/users');
  const status=await page.evaluate(async({etag,entry,email})=>{const r=await fetch('/api/governance/users/update',{method:'POST',headers:{'Content-Type':'application/json','If-Match':etag},body:JSON.stringify({email,entry})});return r.status;},{etag:current.body.etag,entry:original,email});expect(status).toBe(200);
  fs.rmSync(controlPath,{force:true});
 }
});
