// Native Realtime conversation. The authenticated server sideband owns tools,
// transcript persistence and background results; the browser owns audio and UI.
class SynPulseVoice {
  constructor(deps) {
    this.d=deps;this.epoch=0;this.state='off';this.muted=true;
    this.transcripts=new Map();this.tasks=[];this.pendingApprovals=new Set();this.resolvedApprovals=new Set();
    this.userSpeaking=false;this.modelSpeaking=false;this.responseActive=false;
  }
  status(state) {this.state=state;this.d.status(state,this.muted);}
  emit(event) {if(this.dc?.readyState==='open')this.dc.send(JSON.stringify(event));}
  active(epoch=this.epoch) {return epoch===this.epoch&&!!this.sid&&this.d.current()===this.sid;}
  refreshState() {
    if(!this.pc)return;
    if(this.dc?.readyState!=='open'){this.status('connecting');return;}
    this.status(this.userSpeaking?'listening':this.modelSpeaking?'speaking':this.responseActive?'thinking':this.muted?'muted':'listening');
  }
  async start(sid) {
    this.stop();const epoch=++this.epoch;this.sid=sid;this.status('connecting');
    try {
      const media=await this.d.media();media.getTracks().forEach(t=>{t.enabled=false;});
      if(!this.active(epoch)){media.getTracks().forEach(t=>t.stop());if(epoch===this.epoch)this.stop();return;}
      this.media=media;const pc=this.d.peer();this.pc=pc;
      media.getTracks().forEach(t=>pc.addTrack(t,media));
      const active=()=>this.active(epoch)&&this.pc===pc;
      pc.ontrack=e=>{if(active())this.d.audio(e.streams[0]);};
      pc.onconnectionstatechange=()=>{
        if(active()&&['failed','closed','disconnected'].includes(pc.connectionState)){
          const failed=pc.connectionState!=='closed';this.stop();
          if(failed)this.d.error('Voice connection lost. Your background tasks continue in chat.');
        }
      };
      const dc=pc.createDataChannel('oai-events');this.dc=dc;
      dc.onmessage=e=>{if(!active()||this.dc!==dc)return;try{this.event(JSON.parse(e.data));}catch(_){this.d.error('Voice event could not be processed');}};
      dc.onopen=()=>{if(active()&&this.dc===dc)this.refreshState();};
      const offer=await pc.createOffer();if(!active())return;
      await pc.setLocalDescription(offer);if(!active())return;
      const answer=await this.d.connect(sid,offer.sdp);
      if(!active()){if(answer?.voice_id)this.endCall(answer.voice_id);return;}
      if(!answer?.voice_id)throw new Error('Voice service did not create a managed conversation.');
      this.voiceId=answer.voice_id;
      await pc.setRemoteDescription({type:'answer',sdp:answer.sdp});if(!active())return;
      this.refreshState();this.pollStatus(epoch);
      this.expiry=setTimeout(()=>{if(active()){this.stop();this.d.error('Voice session ended after 15 minutes. Start a new call to continue.');}},15*60*1000);
    } catch(e) {if(epoch===this.epoch){this.stop();this.d.error(e.message||'Voice connection failed');}}
  }
  endCall(voiceId) {
    if(!voiceId||!this.d.end)return;
    Promise.resolve().then(()=>this.d.end(voiceId)).catch(()=>{});
  }
  async pollStatus(epoch=this.epoch) {
    if(!this.active(epoch)||!this.voiceId||!this.d.snapshot)return;
    const voiceId=this.voiceId;
    try {
      const data=await this.d.snapshot(voiceId);
      if(!this.active(epoch)||this.voiceId!==voiceId)return;
      if((data.voice_id&&data.voice_id!==voiceId)||(data.session_id&&data.session_id!==this.sid)){
        this.stop({end:false});this.d.error('Voice status belongs to another conversation. Start a new call.');return;
      }
      this.applySnapshot(data);this.d.connectionWarning?.('');
      if(data.state==='closed'||data.state==='error'){
        this.stop({end:false});
        if(data.state==='error')this.d.error('Voice connection failed. Your background tasks continue in chat.');
        return;
      }
    } catch(e) {
      if(this.active(epoch)&&this.voiceId===voiceId){
        if([401,403,404].includes(e.status)){this.stop({end:false});this.d.error('Voice access ended or was revoked. Your background tasks remain in chat.');return;}
        this.d.connectionWarning?.('Task updates are temporarily unavailable. Voice may continue.');
      }
    }
    if(this.active(epoch)&&this.voiceId===voiceId)this.pollTimer=setTimeout(()=>this.pollStatus(epoch),1000);
  }
  applySnapshot(data) {
    for(const entry of Array.isArray(data.transcript)?data.transcript:[])this.transcript(entry.id,entry.role,entry.text);
    this.tasks=Array.isArray(data.tasks)?data.tasks.slice(-20):[];this.d.tasks?.(this.tasks,this);
  }
  transcript(id,role,text) {
    if(!id||!['user','assistant'].includes(role)||typeof text!=='string'||!text.trim())return;
    const key=String(id).startsWith(role+':')?String(id):role+':'+id;if(this.transcripts.get(key)?.text===text)return;
    this.transcripts.set(key,{id,role,text:text.slice(0,16000)});
    if(this.transcripts.size>100)this.transcripts.delete(this.transcripts.keys().next().value);
    this.d.transcript?.([...this.transcripts.values()]);
  }
  mic(on) {
    if(this.sid&&this.d.current()!==this.sid){this.stop();return;}
    if(!this.pc||this.dc?.readyState!=='open')return;
    this.muted=!on;this.media?.getTracks().forEach(t=>{t.enabled=on;});
    if(on)this.interrupt();if(!on)this.userSpeaking=false;this.refreshState();
  }
  interrupt() {
    this.emit({type:'response.cancel'});this.emit({type:'output_audio_buffer.clear'});
    this.modelSpeaking=false;this.responseActive=false;this.d.silence();this.refreshState();
  }
  event(e) {
    if(!this.active()){this.stop();return;}
    if(e.type==='conversation.item.input_audio_transcription.completed')this.transcript(e.item_id,'user',e.transcript);
    if(e.type==='response.output_audio_transcript.done')this.transcript(e.item_id||e.response_id,'assistant',e.transcript);
    if(e.type==='input_audio_buffer.speech_started')this.userSpeaking=true;
    if(e.type==='input_audio_buffer.speech_stopped')this.userSpeaking=false;
    if(e.type==='response.created')this.responseActive=true;
    if(e.type==='output_audio_buffer.started')this.modelSpeaking=true;
    if(e.type==='output_audio_buffer.stopped'||e.type==='output_audio_buffer.cleared')this.modelSpeaking=false;
    // Generation can finish before audio playback. Buffer events settle speech.
    if(e.type==='response.done'){
      this.responseActive=false;
      if(e.response?.status==='failed')this.d.error('The voice response failed. You can speak again.');
    }
    // The trusted sideband executes native function calls once. This client
    // must not submit transcripts or replay function calls through chat.
    if(e.type==='error'&&!['response_cancel_not_active','output_audio_buffer_clear_not_active'].includes(e.error?.code))this.d.error('Voice transport error; your chat remains available');
    this.refreshState();
  }
  async approveTask(taskId,approvalId,choice) {
    if(!['once','deny'].includes(choice)||!this.active())return false;
    const task=this.tasks.find(t=>t.task_id===taskId),approval=task?.approval;
    if(!task?.session_id||!approvalId||approval?.approval_id!==approvalId)return false;
    const key=task.session_id+':'+approvalId;
    if(this.pendingApprovals.has(key)||this.resolvedApprovals.has(key))return false;
    const epoch=this.epoch;this.pendingApprovals.add(key);this.d.tasks?.(this.tasks,this);
    try {
      const result=await this.d.approve({session_id:task.session_id,approval_id:approvalId,...(approval.request_id?{request_id:approval.request_id}:{}),choice});
      if(!result?.ok)throw new Error('Approval was not accepted. Refresh the task and try again.');
      if(this.active(epoch))this.resolvedApprovals.add(key);return true;
    } catch(e) {if(this.active(epoch))this.d.error(e.message||'Approval could not be saved.');return false;}
    finally {this.pendingApprovals.delete(key);if(this.active(epoch))this.d.tasks?.(this.tasks,this);}
  }
  stop({end=true}={}) {
    ++this.epoch;clearTimeout(this.expiry);clearTimeout(this.pollTimer);this.expiry=null;this.pollTimer=null;
    const voiceId=this.voiceId;this.voiceId=null;const pc=this.pc;this.pc=null;
    this.dc?.close();this.dc=null;this.media?.getTracks().forEach(t=>t.stop());this.media=null;
    if(pc){pc.onconnectionstatechange=null;pc.close();}
    this.d.silence();this.d.release?.();this.muted=true;this.sid=null;
    this.userSpeaking=false;this.modelSpeaking=false;this.responseActive=false;
    this.transcripts.clear();this.tasks=[];this.pendingApprovals.clear();this.resolvedApprovals.clear();
    this.d.transcript?.([]);this.d.tasks?.([],this);this.status('off');if(end)this.endCall(voiceId);
  }
}
if(typeof module!=='undefined')module.exports={SynPulseVoice};
if(typeof window!=='undefined')window.SynPulseVoice=SynPulseVoice;

if(typeof document!=='undefined') (function(){
  const start=document.getElementById('btnRealtimeVoice'),bar=document.getElementById('realtimeVoiceBar');
  if(!start||!bar)return;
  const label=document.getElementById('realtimeVoiceStatus'),mic=document.getElementById('realtimeVoiceMic');
  const hold=document.getElementById('realtimeVoiceHold'),captions=document.getElementById('realtimeVoiceTranscript');
  const tasksBox=document.getElementById('realtimeVoiceTasks'),warning=document.getElementById('realtimeVoiceWarning');
  const view=document.getElementById('realtimeVoiceView'),stage=document.getElementById('realtimeVoiceStage');
  const hint=document.getElementById('realtimeVoiceHint'),work=document.getElementById('realtimeVoiceWork');
  const taskCount=document.getElementById('realtimeVoiceTaskCount');
  const text=(key,...args)=>t('voice_workspace_'+key,...args);
  const translated=(node,key)=>{node.dataset.i18n='voice_workspace_'+key;node.textContent=text(key);};
  const compact=value=>{
    bar.classList.toggle('is-compact',value);stage.hidden=value;
    view.setAttribute('aria-expanded',String(!value));translated(view,value?'expand':'minimize');
  };
  const audio=document.createElement('audio');audio.autoplay=true;
  const current=()=>typeof S!=='undefined'?S.session?.session_id:null;
  const taskRows=new Map();
  const voice=new SynPulseVoice({
    current,media:()=>navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}}),
    peer:()=>new RTCPeerConnection(),
    connect:(sid,sdp)=>api('/api/voice/realtime/call',{method:'POST',body:JSON.stringify({session_id:sid,sdp}),retries:0}),
    snapshot:voiceId=>api('/api/voice/realtime/status?voice_id='+encodeURIComponent(voiceId),{retries:0,timeoutToast:false}),
    end:voiceId=>api('/api/voice/realtime/end',{method:'POST',body:JSON.stringify({voice_id:voiceId}),keepalive:true,retries:0,timeoutToast:false}),
    approve:body=>api('/api/approval/respond',{method:'POST',body:JSON.stringify(body),retries:0}),
    audio:stream=>{audio.srcObject=stream;audio.play().catch(()=>showToast('Audio playback was blocked. Use the microphone control to resume.'));},
    release:()=>{audio.pause();audio.srcObject=null;},silence:()=>{},
    status:(state,muted)=>{
      const returnFocus=state==='off'&&bar.contains(document.activeElement);
      if(state==='connecting'&&bar.hidden){compact(false);if(warning){warning.hidden=true;warning.textContent='';}}
      bar.hidden=state==='off';start.setAttribute('aria-pressed',String(state!=='off'));bar.dataset.state=state;
      translated(label,state);
      translated(hint,'hint_'+state);
      translated(mic,muted?'unmute':'mute');mic.setAttribute('aria-pressed',String(!muted));
      mic.disabled=hold.disabled=state==='connecting';
      if(returnFocus)start.focus({preventScroll:true});
    },
    error:message=>showToast(message),
    connectionWarning:message=>{if(warning){warning.textContent=message;warning.hidden=!message;}},
    transcript:entries=>{
      if(!captions)return;
      const following=captions.scrollHeight-captions.scrollTop-captions.clientHeight<32;
      captions.replaceChildren();captions.hidden=!entries.length;
      for(const entry of entries.slice(-4)){
        const row=document.createElement('div');row.className='realtime-voice-transcript-row';row.dataset.role=entry.role;
        const speaker=document.createElement('strong');speaker.textContent=entry.role==='user'?text('you')+': ':'SynthPulse: ';
        row.append(speaker,document.createTextNode(entry.text));captions.append(row);
      }
      if(following)captions.scrollTop=captions.scrollHeight;
    },
    tasks:(tasks,controller)=>{
      if(!tasksBox)return;
      const activeIds=new Set(tasks.map(t=>t.task_id));
      for(const [id,row] of taskRows)if(!activeIds.has(id)){row.remove();taskRows.delete(id);}
      tasksBox.hidden=!tasks.length;
      work.hidden=!tasks.length;
      taskCount.textContent=tasks.length?text('task_count',tasks.length,tasks.filter(task=>task.status==='done').length):'';
      const waiting=tasks.filter(task=>task.status==='waiting_approval').length;
      if(waiting)taskCount.textContent+=' · '+text('task_waiting_count',waiting);
      for(const [index,task] of tasks.entries()){
        if(!task.task_id)continue;
        let row=taskRows.get(task.task_id);
        if(!row){row=document.createElement('details');row.className='realtime-voice-task';row.dataset.taskId=task.task_id;row.append(document.createElement('summary'),document.createElement('div'));taskRows.set(task.task_id,row);tasksBox.append(row);}
        const title=row.firstElementChild,body=row.lastElementChild;
        const approval=task.approval,key=task.session_id+':'+approval?.approval_id;
        const renderKey=JSON.stringify([task,index,controller.pendingApprovals.has(key),controller.resolvedApprovals.has(key)]);
        if(row.dataset.renderKey===renderKey)continue;
        row.dataset.renderKey=renderKey;
        const state=['starting','running','waiting_approval','done','error'].includes(task.status)?task.status:'unknown';
        row.dataset.state=state;title.replaceChildren();body.replaceChildren();
        const number=document.createElement('span');number.className='realtime-voice-step-number';number.textContent=String(index+1);number.setAttribute('aria-hidden','true');
        const name=document.createElement('span');name.className='realtime-voice-task-title';name.textContent=task.title||text('task');
        const status=document.createElement('span');status.className='realtime-voice-task-status';translated(status,'task_'+state);
        title.append(number,name,status);
        const result=document.createElement('p');result.textContent=String(task.error||task.result||'').slice(0,8000);body.append(result);
        if(task.session_id&&/^[A-Za-z0-9_-]+$/.test(task.session_id)){
          const link=document.createElement('a');translated(link,'open_task');link.href=new URL('session/'+encodeURIComponent(task.session_id),document.baseURI).href;link.target='_blank';link.rel='noopener';body.append(link);
        }
        if(approval?.approval_id&&!controller.resolvedApprovals.has(key)){
          row.open=true;const description=document.createElement('p');description.textContent=approval.description||approval.command||text('approval');body.append(description);
          const controls=document.createElement('div');controls.className='realtime-voice-approval';
          for(const choice of ['once','deny']){
            const btn=document.createElement('button');btn.type='button';translated(btn,choice);btn.dataset.approvalChoice=choice;btn.disabled=controller.pendingApprovals.has(key);
            btn.addEventListener('click',()=>controller.approveTask(task.task_id,approval.approval_id,choice));controls.append(btn);
          }
          body.append(controls);
        }
      }
    },
  });
  window.synPulseVoice=voice;
  window.syncVoiceWorkspaceLocale=()=>{
    voice.d.status(voice.state,voice.muted);
    compact(bar.classList.contains('is-compact'));
    voice.d.transcript([...voice.transcripts.values()]);
    for(const row of taskRows.values())delete row.dataset.renderKey;
    voice.d.tasks(voice.tasks,voice);
  };
  view.addEventListener('click',()=>{releaseHold();compact(!bar.classList.contains('is-compact'));});
  bar.addEventListener('keydown',event=>{
    if(event.key==='Escape'&&!bar.classList.contains('is-compact')){event.preventDefault();event.stopPropagation();releaseHold();compact(true);view.focus();}
  });
  let launchEpoch=0,launchPending=false;
  const cancelLaunch=()=>{++launchEpoch;launchPending=false;};
  start.addEventListener('click',async()=>{
    if(launchPending||voice.state!=='off'){cancelLaunch();voice.stop();return;}
    const sid=current();if(!sid){showToast('Open or create a chat before starting voice');return;}
    const intent=++launchEpoch;launchPending=true;
    if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia||typeof RTCPeerConnection==='undefined'){
      launchPending=false;showToast('Voice mode needs HTTPS and microphone support');return;
    }
    const cap=await api('/api/voice/realtime/capability',{retries:0}).catch(error=>({available:false,help:error?.status===409?(typeof t==='function'?t('voice_live_unavailable'):'Live voice is not available on this platform.'):null}));
    if(intent!==launchEpoch)return;
    if(current()!==sid){launchPending=false;return;}
    launchPending=false;
    if(!cap?.available){showToast(cap?.help||'Voice mode is not enabled. Ask an administrator to enable speech.');return;}
    if(typeof window._voiceModeActive==='function'&&window._voiceModeActive()){
      showToast('Turn off the existing voice mode first');return;
    }
    await voice.start(sid);
  });
  mic.addEventListener('click',()=>{audio.play().catch(()=>{});voice.mic(voice.muted);});
  const resumeAudio=()=>audio.play().catch(()=>{});
  let holding=false;
  const releaseHold=()=>{if(holding){holding=false;voice.mic(false);}};
  hold.addEventListener('pointerdown',e=>{e.preventDefault();hold.setPointerCapture(e.pointerId);holding=true;resumeAudio();voice.mic(true);});
  for(const event of ['pointerup','pointercancel','lostpointercapture'])hold.addEventListener(event,releaseHold);
  hold.addEventListener('keydown',e=>{if(e.code==='Space'&&!e.repeat){e.preventDefault();holding=true;resumeAudio();voice.mic(true);}});
  hold.addEventListener('keyup',e=>{if(e.code==='Space'){e.preventDefault();releaseHold();}});
  hold.addEventListener('blur',releaseHold);
  document.getElementById('realtimeVoiceInterrupt').addEventListener('click',()=>{voice.interrupt();voice.mic(false);});
  document.getElementById('realtimeVoiceEnd').addEventListener('click',()=>{cancelLaunch();voice.stop();});
  // Chat answers and tool progress never synthesize extra voice responses here.
  // The sideband returns native tool results when the voice conversation is idle.
  window.addEventListener('pagehide',()=>{cancelLaunch();voice.stop();});
  // A continuous call stays active while a background task opens in another
  // tab. Push-to-talk is released on focus loss so a missed keyup cannot leave
  // capture enabled. End, page exit and conversation changes still tear down.
  window.addEventListener('blur',releaseHold);
  setInterval(()=>{if(voice.sid&&current()!==voice.sid)voice.stop();},250);
})();
