import fs from 'node:fs';
import path from 'node:path';
import {test,expect,open,session,api,auth,capture} from './fixtures';

// Only the browser media/WebRTC boundary is synthetic. All voice HTTP handlers,
// sideband WebSockets, child chat jobs, engine tools and approvals are real.
async function syntheticAudio(page:any, denied=false) {
  expect(auth.realtime_http_base).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
  expect(auth.realtime_ws_base).toMatch(/^ws:\/\/127\.0\.0\.1:\d+$/);
  await page.addInitScript(({wsBase,denied}:any)=>{
    const w=window as any;
    w.__qaVoiceTransport={tracks:[],peers:[],sent:[],playCount:0};
    HTMLMediaElement.prototype.play=function(){w.__qaVoiceTransport.playCount++;return Promise.resolve();};
    Object.defineProperty(navigator.mediaDevices,'getUserMedia',{configurable:true,value:async()=>{
      if(denied)throw new DOMException('Synthetic microphone permission denied','NotAllowedError');
      const track={enabled:true,stopped:false,stop(){this.stopped=true;}};
      w.__qaVoiceTransport.tracks.push(track);return {getTracks:()=>[track]};
    }});
    class Peer {
      dc:any;socket:any;connectionState='new';onconnectionstatechange:any;closed=false;
      constructor(){w.__qaVoiceTransport.peers.push(this);}
      addTrack(){}
      createDataChannel(){
        this.dc={readyState:'connecting',onmessage:null,onopen:null,
          send:(raw:string)=>{w.__qaVoiceTransport.sent.push(JSON.parse(raw));this.socket.send(raw);},
          close:()=>{this.dc.readyState='closed';this.socket?.close();}};
        return this.dc;
      }
      async createOffer(){return {type:'offer',sdp:'v=0\na=qa-synthetic-browser\n'};}
      async setLocalDescription(){}
      async setRemoteDescription(answer:any){
        const id=answer.sdp.match(/a=qa-call-id:([^\s]+)/)?.[1];
        if(!id)throw Error('Synthetic provider call ID missing');
        w.__qaVoiceTransport.callId=id;
        this.socket=new WebSocket(wsBase+'?call_id='+encodeURIComponent(id));
        this.socket.onmessage=(event:any)=>this.dc.onmessage?.(event);
        this.socket.onclose=()=>{this.connectionState='closed';this.onconnectionstatechange?.();};
        await new Promise<void>((resolve,reject)=>{
          this.socket.onopen=()=>{this.connectionState='connected';this.dc.readyState='open';this.dc.onopen?.();resolve();};
          this.socket.onerror=()=>reject(Error('Synthetic realtime WebSocket failed'));
        });
      }
      close(){this.closed=true;this.connectionState='closed';this.socket?.close();}
    }
    w.RTCPeerConnection=Peer;
  },{wsBase:auth.realtime_ws_base,denied});
}
async function begin(page:any,toolsets?:string[]){
  await syntheticAudio(page);await open(page);
  const mobile=page.viewportSize().width<768;
  if(mobile){await page.locator('#btnTitlebarNewChat').click();await expect.poll(()=>page.url()).toContain('/session/');}
  const sid=mobile?page.url().split('/session/')[1].split(/[?#]/)[0]:await session(page);
  if(toolsets)expect((await api(page,'/api/session/toolsets',{session_id:sid,toolsets})).status).toBe(200);
  const created=page.waitForResponse((r:any)=>r.url().endsWith('/api/voice/realtime/call')&&r.request().method()==='POST');
  await page.locator('#btnRealtimeVoice').click();const response=await created;
  expect(response.status(),await response.text()).toBe(200);
  const {voice_id}=await response.json();expect(voice_id).toMatch(/^[a-f0-9]{32}$/);
  await expect(page.locator('#realtimeVoiceStatus')).toHaveText('Microphone muted');
  const callId=await page.evaluate(()=>(window as any).__qaVoiceTransport.callId);
  expect(callId).toMatch(/^rtc_qa_/);return {sid,voiceId:voice_id,callId};
}
async function provider(page:any,callId:string){
  const r=await page.request.get(auth.realtime_http_base+'/qa/realtime/calls');expect(r.ok()).toBe(true);
  const call=(await r.json()).calls.find((c:any)=>c.call_id===callId);expect(call).toBeTruthy();return call;
}
async function events(page:any,callId:string,list:any[]){
  const r=await page.request.post(auth.realtime_http_base+'/qa/realtime/'+callId+'/events',{data:{events:list}});expect(r.ok()).toBe(true);
}
async function snapshot(page:any,voiceId:string){
  const r=await api(page,'/api/voice/realtime/status?voice_id='+voiceId);expect(r.status).toBe(200);return r.body;
}
function spoken(n:number,user:string,reply:string){
  return [
    {type:'input_audio_buffer.speech_started'},
    {type:'conversation.item.input_audio_transcription.completed',item_id:'user_'+n,transcript:user},
    {type:'input_audio_buffer.speech_stopped'},
    {type:'response.created',response:{id:'spoken_'+n}},
    {type:'output_audio_buffer.started',response_id:'spoken_'+n},
    {type:'response.output_audio_transcript.done',response_id:'spoken_'+n,item_id:'answer_'+n,transcript:reply},
    {type:'response.done',response:{id:'spoken_'+n,status:'completed',output:[]}},
    {type:'output_audio_buffer.stopped',response_id:'spoken_'+n},
  ];
}
function work(callId:string,request:string,mode='background'){
  return {type:'response.done',response:{id:'response_'+callId,status:'completed',output:[
    {type:'function_call',name:'dispatch_work',call_id:callId,arguments:JSON.stringify({request,mode})},
  ]}};
}
async function firstTask(page:any,voiceId:string){
  let task:any;await expect.poll(async()=>{task=(await snapshot(page,voiceId)).tasks[0];return !!task?.session_id;},{timeout:20000}).toBe(true);return task;
}
async function taskDone(page:any,voiceId:string){
  let task:any;await expect.poll(async()=>{task=(await snapshot(page,voiceId)).tasks[0];return task?.status;},{timeout:35000}).toBe('done');return task;
}
async function end(page:any,callId:string){
  await page.locator('#realtimeVoiceEnd').click();await expect(page.locator('#realtimeVoiceBar')).toBeHidden();
  await expect.poll(async()=>(await provider(page,callId)).ended).toBe(true);
  expect(await page.evaluate(()=>(window as any).__qaVoiceTransport.tracks.every((t:any)=>t.stopped))).toBe(true);
}

async function voiceContrast(page:any,info:any,selectors:string[]) {
  const evidence:any[]=[];
  for(const dark of [false,true]) {
    await page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
    await page.evaluate(()=>document.getAnimations().forEach(a=>{try{a.finish();}catch{}}));
    const readings=await page.evaluate(selectors=>{
      const canvas=document.createElement('canvas');canvas.width=canvas.height=1;const ctx=canvas.getContext('2d')!;
      const rgb=(value:string)=>{ctx.clearRect(0,0,1,1);ctx.fillStyle=value;ctx.fillRect(0,0,1,1);const c=[...ctx.getImageData(0,0,1,1).data];c[3]/=255;return c;};
      const lum=(c:number[])=>c.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
      return selectors.map(selector=>{
        const node=document.querySelector(selector)!,style=getComputedStyle(node,selector==='#msg'?'::placeholder':null);
        const parents:Element[]=[];let opacity=1,bg=[255,255,255];
        for(let p:Element|null=node;p;p=p.parentElement){parents.unshift(p);opacity*=Number(getComputedStyle(p).opacity);}
        for(const parent of parents){const c=rgb(getComputedStyle(parent).backgroundColor);bg=bg.map((v,i)=>c[i]*c[3]+v*(1-c[3]));}
        const ink=rgb(style.color),alpha=opacity*ink[3];const fg=ink.slice(0,3).map((v,i)=>v*alpha+bg[i]*(1-alpha));
        const a=lum(fg),b=lum(bg);return {selector,fg,bg,opacity,ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
      });
    },selectors);
    evidence.push({dark,readings});for(const reading of readings)expect(reading.ratio,JSON.stringify({dark,...reading})).toBeGreaterThanOrEqual(4.5);
  }
  await info.attach('voice-contrast',{body:JSON.stringify(evidence),contentType:'application/json'});
}

test.describe('native conversational voice',()=>{
 test.use({user:'alice'});
 test('US-SP-VOICE-REALTIME-TURNS native speech answers two turns without submitting chat or replacing draft',async({page},info)=>{
  const {sid,voiceId,callId}=await begin(page);const requests:string[]=[];
  page.on('request',r=>{if(r.method()==='POST')requests.push(new URL(r.url()).pathname);});
  await page.locator('#msg').fill('Unsent typed draft stays here');
  const config=(await provider(page,callId)).config;
  expect(config.audio.input.turn_detection).toMatchObject({create_response:true,interrupt_response:true});
  expect(config.tools.map((t:any)=>t.name)).toEqual(['dispatch_work','get_work_status']);
  await page.locator('#realtimeVoiceMic').click();
  await events(page,callId,spoken(1,'Hello, can we talk?','Yes. What would you like to discuss?'));
  await expect(page.locator('#realtimeVoiceTranscript')).toContainText('Yes. What would you like to discuss?');
  await events(page,callId,spoken(2,'Explain what background work means.','We can keep talking while a task runs in its own chat.'));
  await expect(page.locator('#realtimeVoiceTranscript')).toContainText('We can keep talking while a task runs in its own chat.');
  await expect.poll(async()=>(await snapshot(page,voiceId)).transcript.length).toBe(4);
  await expect(page.locator('#realtimeVoiceTranscript .realtime-voice-transcript-row')).toHaveCount(4);
  await expect(page.locator('#msg')).toHaveValue('Unsent typed draft stays here');
  expect((await api(page,'/api/session?session_id='+sid)).body.session.messages).toEqual([]);
  expect(requests.filter(p=>p==='/api/chat/start'||p==='/api/voice/speak')).toEqual([]);
  expect((await provider(page,callId)).client_events.filter((e:any)=>e.type==='response.create')).toEqual([]);
  const journal=path.join(path.dirname(process.env.QA_SESSIONS!),'state','voice');
  await expect.poll(()=>fs.existsSync(journal)).toBe(true);
  const files=fs.readdirSync(journal).flatMap(dir=>fs.readdirSync(path.join(journal,dir)).map(file=>path.join(journal,dir,file)));
  const persisted=JSON.parse(fs.readFileSync(files.find(f=>f.endsWith(voiceId+'.json'))!,'utf8'));
  expect(persisted.transcript.map((t:any)=>t.text)).toEqual(['Hello, can we talk?','Yes. What would you like to discuss?','Explain what background work means.','We can keep talking while a task runs in its own chat.']);
  await page.screenshot({path:info.outputPath('native-voice-conversation.png')});await capture(page,'voice-two-turns',info);await end(page,callId);
 });
 test('US-SP-VOICE-REALTIME-BACKGROUND native dispatch performs real file write once and preserves ongoing conversation',async({page},info)=>{
  const {sid,voiceId,callId}=await begin(page);const target=path.join(auth.workspace,'qa-voice-native-'+Date.now()+'.txt');
  await page.locator('#realtimeVoiceMic').click();
  const event=work('native_write','QA_SLOW_RESPONSE\nQA_GOV_WRITE|'+target);
  await events(page,callId,[event,event]);const task=await firstTask(page,voiceId);expect(task.session_id).not.toBe(sid);
  expect(fs.existsSync(target)).toBe(false);
  await events(page,callId,spoken(3,'While that runs, can you still hear me?','Yes, the task is running and our conversation can continue.'));
  await expect(page.locator('#realtimeVoiceTranscript')).toContainText('our conversation can continue');
  expect((await snapshot(page,voiceId)).tasks).toHaveLength(1);
  const result=await taskDone(page,voiceId);expect(result.result).toContain('QA_GOV_TOOL_RESULT:');
  expect(fs.readFileSync(target,'utf8')).toBe('QA_GOVERNANCE_EXECUTED');
  const call=await provider(page,callId);
  expect(call.client_events.filter((e:any)=>e.item?.type==='function_call_output'&&e.item.call_id==='native_write')).toHaveLength(1);
  expect(await page.evaluate(()=>(window as any).synPulseVoice.voiceId)).toBe(voiceId);
  await expect(page.locator('#realtimeVoiceTasks')).toContainText('done');
  const row=page.locator('.realtime-voice-task').first();await row.locator('summary').click();
  await expect(row.getByRole('link',{name:'Open task in a new tab'})).toHaveAttribute('href',new URL('/session/'+task.session_id,auth.base_url).href);
  const popupPromise=page.waitForEvent('popup');await row.getByRole('link').click();const popup=await popupPromise;
  await expect(popup.locator('#messages')).toContainText('QA_GOV_TOOL_RESULT:');await popup.close();
  expect(await page.evaluate(()=>(window as any).synPulseVoice.muted)).toBe(false);
  await capture(page,'voice-background-complete',info);await end(page,callId);
 });
 test('US-SP-VOICE-REALTIME-END ending call leaves a real engine task running to completion',async({page})=>{
  const {voiceId,callId}=await begin(page);await events(page,callId,[work('end_while_running','QA_SLOW_RESPONSE')]);
  const task=await firstTask(page,voiceId);expect(task.status).toBe('running');await end(page,callId);
  await expect.poll(async()=>(await api(page,'/api/session?session_id='+task.session_id)).body.session.messages.some((m:any)=>m.role==='assistant'&&String(m.content).includes('QA_REPLY: QA_SLOW_RESPONSE')),{timeout:20000}).toBe(true);
 });
});

for(const [user,choice] of [['clionce','once'],['clideny','deny'],['cliauto','once']] as const)test.describe('voice '+user,()=>{
 test.use({user,viewport:{width:390,height:844}});
 test(`US-SP-VOICE-REALTIME-${choice.toUpperCase()} mandatory CLI action waits for exact child approval while voice stays open`,async({page},info)=>{
  const {voiceId,callId}=await begin(page,['terminal']);const file='qa-cli-voice-'+user+'-'+Date.now()+'.txt',target=path.join(auth.workspace,file);
  await events(page,callId,[work('governed_'+choice,'QA_SUPPLEMENT_CLI|'+file)]);
  let task:any;await expect.poll(async()=>{task=(await snapshot(page,voiceId)).tasks[0];return task?.status;},{timeout:25000}).toBe('waiting_approval');
  expect(fs.existsSync(target)).toBe(false);expect(task.approval.approval_id).toBeTruthy();
  const row=page.locator('.realtime-voice-task').first();await expect(row.getByRole('button',{name:'Allow once',exact:true})).toBeVisible();
  await expect(row.getByRole('button')).toHaveCount(2);
  await events(page,callId,spoken(4,'Can we still talk while approval is pending?','Yes, use the approval controls when you are ready.'));
  await expect(page.locator('#realtimeVoiceTranscript')).toContainText('when you are ready');
  await page.screenshot({path:info.outputPath('voice-mobile-pending-'+choice+'.png')});await capture(page,'voice-mobile-pending-'+choice,info);
  const responsePromise=page.waitForResponse(r=>r.url().endsWith('/api/approval/respond'));
  await row.getByRole('button',{name:choice==='once'?'Allow once':'Deny',exact:true}).click();const response=await responsePromise;
  expect(response.status(),await response.text()).toBe(200);
  expect(response.request().postDataJSON()).toMatchObject({session_id:task.session_id,approval_id:task.approval.approval_id,choice});
  const result=await taskDone(page,voiceId);expect(result.result).toContain('QA_SUPPLEMENT_TOOL_RESULT:');expect(fs.existsSync(target)).toBe(choice==='once');
  if(choice==='once')expect(fs.statSync(target).size).toBe(0);
  expect(await page.evaluate(()=>(window as any).synPulseVoice.voiceId)).toBe(voiceId);
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('voice-mobile-'+choice+'.png')});await capture(page,'voice-mobile-'+choice,info);await end(page,callId);
 });
});

for(const user of ['autoapprove','autodeny','automanual','manualapprove'] as const)test.describe('voice automatic '+user,()=>{
 test.use({user});
 test(`US-SP-VOICE-REALTIME-${user.toUpperCase()} access-request reviewer does not intercept an already permitted native file write`,async({page},info)=>{
  const {voiceId,callId}=await begin(page);const target=path.join(auth.workspace,'qa-voice-'+user+'-'+Date.now()+'.txt');
  await events(page,callId,[work('voice_'+user,'QA_GOV_WRITE|'+target)]);
  const task=await taskDone(page,voiceId);expect(task.result).toContain('QA_GOV_TOOL_RESULT:');
  expect(fs.readFileSync(target,'utf8')).toBe('QA_GOVERNANCE_EXECUTED');
  await expect(page.locator('.realtime-voice-approval')).toHaveCount(0);
  const auditPath=path.join(path.dirname(process.env.QA_SESSIONS!),'home','dashboard-governance-audit.jsonl');
  const rows=fs.existsSync(auditPath)?fs.readFileSync(auditPath,'utf8').trim().split('\n').filter(Boolean).map(s=>JSON.parse(s)):[];
  const reviews=rows.filter(r=>r.event==='action_approval'&&r.extra?.session_id===task.session_id);
  expect(reviews).toEqual([]);await info.attach('voice-no-per-call-review',{body:JSON.stringify({session_id:task.session_id,reviews}),contentType:'application/json'});
  expect(await page.evaluate(()=>(window as any).synPulseVoice.voiceId)).toBe(voiceId);await end(page,callId);
 });
});

test.describe('voice lifecycle controls',()=>{
 test.use({user:'bob'});
 test('VOICE WORKSPACE stage keeps the call and draft while minimizing, translating and resizing',async({page},info)=>{
  test.setTimeout(60000);
  const {voiceId,callId}=await begin(page);
  await page.locator('#msg').fill('Mijn aantekeningen voor het project');
  await events(page,callId,spoken(21,'Kunnen we samen het project voorbereiden?','Ja. We kunnen verder praten terwijl ik de eerste taken uitwerk.'));
  await page.screenshot({path:info.outputPath('voice-desktop.png')});
  const toggle=page.locator('#realtimeVoiceView');
  await expect(toggle).toHaveAttribute('aria-expanded','true');
  await expect(page.locator('#realtimeVoiceHint')).toContainText('microphone is off');
  await page.locator('#realtimeVoiceMic').click();
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded','false');
  await expect(page.locator('#messages')).toBeVisible();
  await expect(page.locator('#realtimeVoiceEnd')).toBeInViewport();
  expect(await page.evaluate(()=>(window as any).__qaVoiceTransport.tracks[0].enabled)).toBe(true);
  await expect(page.locator('#msg')).toHaveValue('Mijn aantekeningen voor het project');
  await events(page,callId,[work('stage_task','Maak een korte projectintroductie')]);
  await taskDone(page,voiceId);
  await expect(page.locator('#realtimeVoiceTaskCount')).toContainText('1 of 1 done');
  await events(page,callId,[...spoken(22,'Hoe staat het met de introductie?','De projectintroductie is klaar. Je kunt de taak hieronder openen.'),...spoken(23,'Ik maak ondertussen aantekeningen.','Prima. Je kunt blijven typen terwijl we praten.')]);
  await toggle.click();
  await expect(page.locator('#realtimeVoiceTranscript')).toContainText('blijven typen');
  await page.evaluate(()=>setLocale('nl'));
  await expect(page.locator('#realtimeVoiceStatus')).toHaveText('Luisteren');
  await expect(page.locator('#realtimeVoiceMic')).toHaveText('Microfoon uit');
  for(const width of [1440,900,390]){
    await page.setViewportSize({width,height:width===390?844:1000});
    await expect(page.locator('#realtimeVoiceEnd')).toBeInViewport();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath('voice-dutch-'+width+'.png')});
  }
  await page.setViewportSize({width:1440,height:1000});
  await voiceContrast(page,info,['#realtimeVoiceStatus','#realtimeVoiceHint','#realtimeVoiceTaskCount','#realtimeVoiceMic','.realtime-voice-task-status']);
  await page.evaluate(()=>document.documentElement.classList.remove('dark'));
  await page.screenshot({path:info.outputPath('voice-light.png')});
  await page.emulateMedia({reducedMotion:'reduce'});
  expect(await page.locator('.realtime-voice-orb').evaluate(el=>getComputedStyle(el).animationName)).toBe('none');
  await page.setViewportSize({width:390,height:500});
  await expect(page.locator('#realtimeVoiceEnd')).toBeInViewport();
  await toggle.focus();await page.keyboard.press('Escape');
  await expect(toggle).toBeFocused();await expect(toggle).toHaveAttribute('aria-expanded','false');
  expect(await page.evaluate(()=>(window as any).synPulseVoice.voiceId)).toBe(voiceId);
  await expect(page.locator('#msg')).toHaveValue('Mijn aantekeningen voor het project');
  await end(page,callId);
  await expect(page.locator('#btnRealtimeVoice')).toBeFocused();
 });
 test('US-SP-VOICE-REALTIME-CONTROLS microphone, push to talk, interruption and session change release capture correctly',async({page})=>{
  const {callId}=await begin(page);const enabled=()=>page.evaluate(()=>(window as any).__qaVoiceTransport.tracks[0].enabled);
  expect(await enabled()).toBe(false);await page.locator('#realtimeVoiceMic').click();expect(await enabled()).toBe(true);
  await page.locator('#realtimeVoiceMic').click();expect(await enabled()).toBe(false);
  const hold=page.locator('#realtimeVoiceHold');await hold.focus();
  const plays=await page.evaluate(()=>(window as any).__qaVoiceTransport.playCount);
  await page.keyboard.down('Space');expect(await enabled()).toBe(true);
  expect(await page.evaluate(()=>(window as any).__qaVoiceTransport.playCount)).toBe(plays+1);
  await page.keyboard.up('Space');expect(await enabled()).toBe(false);
  await hold.hover();await page.mouse.down();expect(await enabled()).toBe(true);
  expect(await page.evaluate(()=>(window as any).__qaVoiceTransport.playCount)).toBe(plays+2);
  await page.mouse.up();expect(await enabled()).toBe(false);
  await events(page,callId,[{type:'response.created',response:{id:'buffered'}},{type:'output_audio_buffer.started',response_id:'buffered'},{type:'response.done',response:{id:'buffered',status:'completed',output:[]}}]);
  await expect(page.locator('#realtimeVoiceStatus')).toHaveText('Speaking');
  await page.locator('#realtimeVoiceInterrupt').click();await expect(page.locator('#realtimeVoiceStatus')).toHaveText('Microphone muted');
  expect((await provider(page,callId)).client_events.slice(-2).map((e:any)=>e.type)).toEqual(['response.cancel','output_audio_buffer.clear']);
  // An empty conversation is intentionally reused by New chat. Persist one
  // ordinary typed turn first so the next click changes the actual session.
  await page.locator('#msg').fill('QA_VOICE_TYPED_SETUP');await page.locator('#btnSend').click();
  await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_VOICE_TYPED_SETUP');
  await page.locator('#btnNewChat').click();await expect(page.locator('#realtimeVoiceBar')).toBeHidden();
  await expect.poll(async()=>(await provider(page,callId)).ended).toBe(true);expect(await page.evaluate(()=>(window as any).__qaVoiceTransport.tracks[0].stopped)).toBe(true);
 });
 test('US-SP-VOICE-REALTIME-MIC-DENIED microphone refusal fails without creating a provider call',async({page})=>{
  await syntheticAudio(page,true);await open(page);await session(page);
  const before=(await (await page.request.get(auth.realtime_http_base+'/qa/realtime/calls')).json()).calls.length;
  await page.locator('#btnRealtimeVoice').click();await expect(page.locator('#toast')).toContainText('Synthetic microphone permission denied');
  await expect(page.locator('#realtimeVoiceBar')).toBeHidden();
  expect((await (await page.request.get(auth.realtime_http_base+'/qa/realtime/calls')).json()).calls).toHaveLength(before);
 });
 test('US-SP-VOICE-REALTIME-REMOTE-END removed server call stops browser microphone capture',async({page})=>{
  const {voiceId,callId}=await begin(page);await page.locator('#realtimeVoiceMic').click();
  expect(await page.evaluate(()=>(window as any).__qaVoiceTransport.tracks[0].enabled)).toBe(true);
  const ended=await api(page,'/api/voice/realtime/end',{voice_id:voiceId});expect(ended.status).toBe(200);
  await expect(page.locator('#realtimeVoiceBar')).toBeHidden();
  expect(await page.evaluate(()=>(window as any).__qaVoiceTransport.tracks[0].stopped)).toBe(true);
  expect((await api(page,'/api/voice/realtime/status?voice_id='+voiceId)).status).toBe(404);
  expect((await provider(page,callId)).ended).toBe(true);
 });
 test('US-SP-VOICE-REALTIME-LAUNCH-RACE canceled coalesced capability reads cannot start microphone capture',async({page})=>{
  await syntheticAudio(page);await open(page);await session(page);
  const release:Array<()=>void>=[];let reads=0;
  await page.route('**/api/voice/realtime/capability',async route=>{
    const response=await route.fetch();reads++;
    await new Promise<void>(resolve=>release.push(resolve));await route.fulfill({response});
  });
  try{
    await page.locator('#btnRealtimeVoice').click();await expect.poll(()=>reads).toBe(1);
    await page.locator('#btnRealtimeVoice').click(); // cancel pending first launch
    await page.locator('#btnRealtimeVoice').click(); // new intent shares the in-flight GET
    await page.locator('#btnRealtimeVoice').click(); // cancel the newer intent too
    const response=page.waitForResponse(r=>r.url().endsWith('/api/voice/realtime/capability'));
    release[0]();await response;
    // api() coalesces identical concurrent reads. Both callbacks must respect
    // their canceled launch epoch; neither may request microphone capture.
    await page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,100)));
    await expect(page.locator('#realtimeVoiceBar')).toBeHidden();
    expect(reads).toBe(1);
    expect(await page.evaluate(()=>(window as any).__qaVoiceTransport.tracks)).toHaveLength(0);
    expect(await page.evaluate(()=>(window as any).__qaVoiceTransport.peers)).toHaveLength(0);
  }finally{for(const finish of release)finish();}
 });
});

test.describe('voice native work modes and result timing',()=>{
 test.use({user:'admin'});
 for(const [mode,request,expected] of [
  ['chat','QA_GOV_READ|'+path.join(auth.workspace,'qa-evidence.txt'),'Synthetic QA workspace evidence.'],
  ['subagents','QA_DELEGATE_PARENT','QA_CHILD_TWO_DONE'],
 ] as const)test(`US-SP-VOICE-REALTIME-${mode.toUpperCase()} native ${mode} mode executes actual engine work and status tool`,async({page},info)=>{
  test.setTimeout(60000);const {voiceId,callId}=await begin(page);
  await events(page,callId,[work('mode_'+mode,request,mode)]);
  const result=await taskDone(page,voiceId);expect(result.mode).toBe(mode);expect(result.result).toContain(expected);
  if(mode==='subagents'){
    const child=await api(page,'/api/session?session_id='+result.session_id);
    const completion=child.body.session.messages.find((m:any)=>m._source==='process_wakeup'&&String(m.content).includes('[ASYNC DELEGATION BATCH COMPLETE'));
    expect(completion,'engine completion must confirm both children, not merely requested goals').toBeTruthy();
    expect(completion.content).toContain('DEFAULT QA_REPLY: QA_CHILD_ONE_DONE');
    expect(completion.content).toContain('DEFAULT QA_REPLY: QA_CHILD_TWO_DONE');
    expect(completion.content.match(/status=completed/g)).toHaveLength(2);
    expect(JSON.stringify(child.body)).toContain('delegate_task');
    expect(JSON.stringify(child.body)).toContain('QA_CHILD_ONE_DONE');
    expect(JSON.stringify(child.body)).toContain('QA_CHILD_TWO_DONE');
  }
  await events(page,callId,[{type:'response.done',response:{id:'status_response',status:'completed',output:[
    {type:'function_call',name:'get_work_status',call_id:'status_'+mode,arguments:JSON.stringify({session_id:result.session_id})},
  ]}}]);
  let output:any;await expect.poll(async()=>{output=(await provider(page,callId)).client_events.find((e:any)=>e.item?.type==='function_call_output'&&e.item.call_id==='status_'+mode);return !!output;}).toBe(true);
  expect(JSON.parse(output.item.output).tasks[0]).toMatchObject({session_id:result.session_id,mode,status:'done'});
  await expect(page.locator('#realtimeVoiceTasks')).toContainText('done');await capture(page,'voice-native-'+mode,info);await end(page,callId);
 });
 test('US-SP-VOICE-REALTIME-IDLE result speech waits until native audio playback is finished',async({page})=>{
  const {voiceId,callId}=await begin(page);await events(page,callId,[work('deferred_result','QA_SLOW_RESPONSE')]);
  await firstTask(page,voiceId);
  await expect.poll(async()=>(await provider(page,callId)).client_events.filter((e:any)=>e.type==='response.create').length).toBe(1);
  await events(page,callId,[{type:'input_audio_buffer.speech_started'},
    {type:'input_audio_buffer.speech_stopped'},
    {type:'response.created',response:{id:'still_playing'}},
    {type:'output_audio_buffer.started',response_id:'still_playing'}]);
  await expect(page.locator('#realtimeVoiceStatus')).toHaveText('Speaking');await taskDone(page,voiceId);
  expect((await provider(page,callId)).client_events.filter((e:any)=>e.type==='response.create')).toHaveLength(1);
  await events(page,callId,[{type:'response.done',response:{id:'still_playing',status:'completed',output:[]}}]);
  await snapshot(page,voiceId);await expect(page.locator('#realtimeVoiceStatus')).toHaveText('Speaking');
  expect((await provider(page,callId)).client_events.filter((e:any)=>e.type==='response.create')).toHaveLength(1);
  await events(page,callId,[{type:'output_audio_buffer.stopped',response_id:'still_playing'}]);
  await expect.poll(async()=>(await provider(page,callId)).client_events.filter((e:any)=>e.type==='response.create').length).toBe(2);
  await end(page,callId);
 });
 test('US-SP-VOICE-REALTIME-SLOW-SUBAGENTS idle parent remains running until delayed actual children and their continuation finish',async({page},info)=>{
  const {voiceId,callId}=await begin(page);await events(page,callId,[work('delayed_children','QA_DELEGATE_SLOW_PARENT','subagents')]);
  const task=await firstTask(page,voiceId);let status:any;
  await expect.poll(async()=>{
    status=(await api(page,'/api/session/status?session_id='+task.session_id)).body;
    return status.agent_running===false&&status.delegation_pending===true;
  },{timeout:12000,intervals:[100,200,300]}).toBe(true);
  expect(status.delegation_pending_count).toBeGreaterThan(0);
  expect((await snapshot(page,voiceId)).tasks[0].status).toBe('running');
  await events(page,callId,spoken(9,'Keep talking while the subagents finish.','Yes, their work continues independently.'));
  await expect(page.locator('#realtimeVoiceTranscript')).toContainText('their work continues independently');
  await expect(page.locator('#realtimeVoiceTasks')).toContainText('running');
  await info.attach('voice-pending-delegation-status',{body:JSON.stringify(status),contentType:'application/json'});
  const result=await taskDone(page,voiceId);expect(result.result).toContain('QA_CHILD_TWO_DONE');
  const child=(await api(page,'/api/session?session_id='+task.session_id)).body.session;
  const completion=child.messages.find((m:any)=>m._source==='process_wakeup'&&String(m.content).includes('[ASYNC DELEGATION BATCH COMPLETE'));
  expect(completion).toBeTruthy();expect(completion.content.match(/status=completed/g)).toHaveLength(2);
  expect(completion.content).toContain('DEFAULT QA_REPLY: QA_CHILD_ONE_DONE');expect(completion.content).toContain('DEFAULT QA_REPLY: QA_CHILD_TWO_DONE');
  const finalStatus=(await api(page,'/api/session/status?session_id='+task.session_id)).body;
  expect(finalStatus.delegation_pending).toBe(false);expect(finalStatus.agent_running).toBe(false);
  await capture(page,'voice-children-finished',info);await end(page,callId);
 });
});

test.describe('voice delegated continuation authority',()=>{
 test.use({user:'continuation'});
 test('US-SP-VOICE-REALTIME-CONTINUATION original signed SSO restriction survives actual child completion and parent wakeup',async({page},info)=>{
  test.setTimeout(65000);
  const {voiceId,callId}=await begin(page);
  const target=path.join(auth.workspace,'qa-continuation-protected-'+Date.now()+'.txt');
  await events(page,callId,[work('continuation_sso','QA_CONTINUATION_PARENT|'+target,'subagents')]);
  const task=await firstTask(page,voiceId);
  const result=await taskDone(page,voiceId);
  expect(result.result).toContain('QA_CONTINUATION_RESULT:');
  expect(result.result).toContain('file_denied_glob');
  expect(fs.existsSync(target),'original SSO restriction must prevent the continuation effect').toBe(false);
  const child=(await api(page,'/api/session?session_id='+task.session_id)).body.session;
  const completion=child.messages.find((m:any)=>m._source==='process_wakeup'&&String(m.content).includes('[ASYNC DELEGATION BATCH COMPLETE'));
  expect(completion).toBeTruthy();expect(completion.content.match(/status=completed/g)).toHaveLength(2);
  expect(completion.content).toContain('QA_CHILD_ONE_DONE');expect(completion.content).toContain('QA_CHILD_TWO_DONE');
  const privateRoot=path.join(path.dirname(process.env.QA_SESSIONS!),'state','continuation-authority');
  const records=fs.readdirSync(privateRoot).filter(name=>name.endsWith('.json')).map(name=>JSON.parse(fs.readFileSync(path.join(privateRoot,name),'utf8'))).filter(record=>record.session_id===task.session_id);
  expect(records).toHaveLength(1);expect(records[0].identity).toMatchObject({email:'continuation@example.test',groups:['qa-sso-restricted']});
  expect(records[0].active_profile).toBe('default');
  const original=JSON.parse(records[0].context);
  expect(original.access.grants.file_denied_globs).toContain(path.join(auth.workspace,'qa-continuation-protected-*'));
  await info.attach('continuation-authority-proof',{body:JSON.stringify({subject:'continuation@example.test',groups:records[0].identity.groups,profile:records[0].active_profile,actualChildren:2,blockedTool:'write_file',decision:'file_denied_glob',fileExists:false}),contentType:'application/json'});
  await expect(page.locator('#realtimeVoiceTasks')).toContainText('done');await capture(page,'voice-continuation-denied',info);await end(page,callId);
 });
});
