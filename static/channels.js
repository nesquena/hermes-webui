/* Profile-scoped Matrix channel settings. Secrets are write-only. */
let _channelsLoadPromise=null;
let _channelsFormProfile=null;
let _channelsGeneration=0;
function invalidateChannelsForProfileSwitch(){
  _channelsGeneration++;
  _channelsLoadPromise=null;
  _channelsFormProfile=null;
  ['matrixHomeserver','matrixUserId','matrixAccessToken','matrixPassword','matrixAllowedUsers','matrixAllowedRooms'].forEach(id=>{const e=$(id);if(e)e.value='';});
  _setMatrixMessage('Loading channel settings for the selected profile...');
}
function _matrixLines(id){const e=$(id);return e?String(e.value||'').split(/\r?\n/).map(v=>v.trim()).filter(Boolean):[];}
function _setMatrixMessage(message,error){const e=$('matrixChannelMessage');if(e){e.textContent=message||'';e.style.color=error?'var(--error,#e05)':'var(--muted)';}}
function syncMatrixAuthFields(){const password=$('matrixAuthMethod')?.value==='password';if($('matrixAccessTokenField'))$('matrixAccessTokenField').style.display=password?'none':'';if($('matrixPasswordField'))$('matrixPasswordField').style.display=password?'':'none';}
function _renderMatrixChannel(data){
  if(!data)return;
  if(data.profile!==(S.activeProfile||'default'))return;
  _channelsFormProfile=data.profile;
  if($('matrixProfileName'))$('matrixProfileName').textContent=data.profile||'default';
  if($('matrixHomeserver'))$('matrixHomeserver').value=data.homeserver||'';
  if($('matrixUserId'))$('matrixUserId').value=data.user_id||'';
  if($('matrixAuthMethod'))$('matrixAuthMethod').value=data.auth_method||'access_token';
  const matrixAccessToken=$('matrixAccessToken'),matrixPassword=$('matrixPassword');
  if(matrixAccessToken){matrixAccessToken.value = '';matrixAccessToken.placeholder=data.has_access_token?'Saved token (leave blank to keep)':'Enter access token';}
  if(matrixPassword){matrixPassword.value = '';matrixPassword.placeholder=data.has_password?'Saved password (leave blank to keep)':'Enter password';}
  if($('matrixAllowedUsers'))$('matrixAllowedUsers').value=(data.allowed_users||[]).join('\n');
  if($('matrixAllowedRooms'))$('matrixAllowedRooms').value=(data.allowed_rooms||[]).join('\n');
  if($('matrixRequireMention'))$('matrixRequireMention').checked=data.require_mention!==false;
  if($('matrixSessionScope'))$('matrixSessionScope').value=data.session_scope||'room';
  if($('matrixAutoThread'))$('matrixAutoThread').checked=!!data.auto_thread;
  if($('matrixE2eeMode'))$('matrixE2eeMode').value=data.e2ee_mode||'required';
  const status=$('matrixChannelStatus');if(status){const running=data.gateway_status==='running';status.textContent=running?'Running':(data.configured?'Configured · stopped':'Not configured');status.className='detail-badge '+(running?'ok':'warn');}
  if($('matrixRestartBtn'))$('matrixRestartBtn').disabled=!data.configured;
  if($('matrixClearBtn'))$('matrixClearBtn').disabled=!data.configured;
  syncMatrixAuthFields();
}
async function loadChannelsPanel(){
  if(_channelsLoadPromise)return _channelsLoadPromise;
  const generation=_channelsGeneration;
  const requestedProfile=S.activeProfile||'default';
  _channelsLoadPromise=(async()=>{try{const data=await api('/api/channels/matrix');if(generation!==_channelsGeneration||requestedProfile!==(S.activeProfile||'default')||data.profile!==requestedProfile)return null;_renderMatrixChannel(data);_setMatrixMessage('');return data;}catch(e){if(generation===_channelsGeneration)_setMatrixMessage('Could not load Matrix settings: '+(e?.message||'request failed'),true);return null;}finally{if(generation===_channelsGeneration)_channelsLoadPromise=null;}})();
  return _channelsLoadPromise;
}
function _matrixPayload(){return {
  homeserver:($('matrixHomeserver')?.value||'').trim(),user_id:($('matrixUserId')?.value||'').trim(),auth_method:$('matrixAuthMethod')?.value||'access_token',
  access_token:$('matrixAccessToken')?.value||'',password:$('matrixPassword')?.value||'',allowed_users:_matrixLines('matrixAllowedUsers'),allowed_rooms:_matrixLines('matrixAllowedRooms'),
  require_mention:!!$('matrixRequireMention')?.checked,session_scope:$('matrixSessionScope')?.value||'room',auto_thread:!!$('matrixAutoThread')?.checked,e2ee_mode:$('matrixE2eeMode')?.value||'required'};}
async function saveMatrixChannel(event,quiet){
  if(!_channelsFormProfile||_channelsFormProfile!==(S.activeProfile||'default')){invalidateChannelsForProfileSwitch();await loadChannelsPanel();throw new Error('Profile changed; Matrix settings were reloaded.');}
  event?.preventDefault();const btn=$('matrixSaveBtn');if(btn)btn.disabled=true;_setMatrixMessage('Saving Matrix settings...');
  try{const data=await api('/api/channels/matrix',{method:'POST',body:JSON.stringify(_matrixPayload())});_renderMatrixChannel(data);_setMatrixMessage('Saved for '+(data.profile||'active profile')+'. Restart the gateway to apply changes.');if(!quiet)showToast('Matrix channel saved');return data;}
  catch(e){_setMatrixMessage('Save failed: '+(e?.message||'request failed'),true);if(!quiet)showToast('Matrix save failed');throw e;}finally{if(btn)btn.disabled=false;}
}
async function restartMatrixGateway(){
  if(!_channelsFormProfile||_channelsFormProfile!==(S.activeProfile||'default')){invalidateChannelsForProfileSwitch();await loadChannelsPanel();throw new Error('Profile changed; Matrix settings were reloaded.');}
  const btn=$('matrixRestartBtn');if(btn)btn.disabled=true;_setMatrixMessage("Restarting this profile's gateway...");
  try{const r=await api('/api/channels/matrix/restart',{method:'POST',body:'{}'});_setMatrixMessage(r?.message||'Gateway restart requested.');showToast('Gateway restart requested');return r;}
  catch(e){_setMatrixMessage('Gateway restart failed: '+(e?.message||'request failed'),true);throw e;}finally{if(btn)btn.disabled=false;}
}
async function saveAndRestartMatrixGateway(){try{await saveMatrixChannel(null,true);await restartMatrixGateway();}catch(_e){}}
async function clearMatrixChannel(){
  if(!_channelsFormProfile||_channelsFormProfile!==(S.activeProfile||'default')){invalidateChannelsForProfileSwitch();await loadChannelsPanel();return;}
  if(!window.confirm('Disconnect Matrix from the active Hermes profile? Other profiles are not affected.'))return;
  const btn=$('matrixClearBtn');if(btn)btn.disabled=true;
  try{const data=await api('/api/channels/matrix/clear',{method:'POST',body:'{}'});_renderMatrixChannel(data);_setMatrixMessage('Matrix disconnected from '+(data.profile||'active profile')+'.');showToast('Matrix channel disconnected');}
  catch(e){_setMatrixMessage('Disconnect failed: '+(e?.message||'request failed'),true);}finally{if(btn)btn.disabled=false;}
}
