// The server owns workplace policy. Browser state only controls presentation;
// changing it cannot grant access to protected API routes.
let _workplaceUnlockPromise=null;
let _workplaceExpiryTimer=null;

function workplaceLocked(){
  const state=window.__HERMES_WORKPLACE__;
  return !!(state&&state.enabled&&(!state.admin_unlocked||state.expires_at*1000<=Date.now()));
}

function _applyWorkplaceStatus(state){
  window.__HERMES_WORKPLACE__=state;
  clearTimeout(_workplaceExpiryTimer);
  const root=document.documentElement;
  if(!state.enabled){delete root.dataset.workplace;return;}
  const locked=workplaceLocked();
  root.dataset.workplace=locked?'locked':'admin';
  const welcome=document.querySelector('[data-i18n="empty_subtitle"],[data-i18n="workplace_welcome"]');
  if(welcome){welcome.dataset.i18n='workplace_welcome';welcome.textContent=t('workplace_welcome');}
  const bar=document.getElementById('workplaceAdminBar');
  if(bar)bar.hidden=locked;
  if(locked){
    // Expiry/revocation must not wait behind the unsaved-settings guard.
    if(typeof _currentPanel==='string'&&_currentPanel!=='chat'){
      if(typeof _settingsDirty!=='undefined')_settingsDirty=false;
      void switchPanel('chat',{bypassSettingsGuard:true});
    }
    if(typeof closeWorkspacePanel==='function') closeWorkspacePanel();
  }else{
    _workplaceExpiryTimer=setTimeout(()=>{
      _applyWorkplaceStatus({...state,admin_unlocked:false,expires_at:0});
    },Math.max(0,state.expires_at*1000-Date.now()));
  }
}

async function _requestWorkplaceUnlock(){
  try{
    const status=await api('/api/workplace/status');
    _applyWorkplaceStatus(status);
    if(!workplaceLocked())return true;
    if(!status.pin_configured){showToast(t('workplace_pin_missing'));return false;}
    const pin=await showPromptDialog({title:t('workplace_unlock'),
      message:t('workplace_pin_prompt'),inputType:'password',confirmLabel:t('workplace_unlock')});
    if(pin===null)return false;
    const unlocked=await api('/api/workplace/unlock',{
      method:'POST',body:JSON.stringify({pin}),retries:0});
    _applyWorkplaceStatus(unlocked);
    return !workplaceLocked();
  }catch(error){showToast(error.message||t('workplace_unlock_failed'));return false;}
}

async function workplaceAllowsPanel(name){
  if(!workplaceLocked()||name==='chat')return true;
  if(name!=='settings')return false;
  if(!_workplaceUnlockPromise){
    _workplaceUnlockPromise=_requestWorkplaceUnlock().finally(()=>{_workplaceUnlockPromise=null;});
  }
  return _workplaceUnlockPromise;
}

async function lockWorkplaceAdmin(){
  try{
    const state=await api('/api/workplace/lock',{method:'POST',body:'{}',retries:0});
    _applyWorkplaceStatus(state);
  }catch(error){showToast(error.message||t('workplace_unlock_failed'));}
}

if(window.__HERMES_WORKPLACE__&&window.__HERMES_WORKPLACE__.enabled){
  _applyWorkplaceStatus(window.__HERMES_WORKPLACE__);
  // A different tab may have locked/revoked the shared browser cookie. Never
  // cache authorization in localStorage; refresh on focus and at short intervals.
  const refresh=()=>api('/api/workplace/status',{retries:0}).then(_applyWorkplaceStatus).catch(()=>{
    _applyWorkplaceStatus({...window.__HERMES_WORKPLACE__,admin_unlocked:false,expires_at:0});
  });
  window.addEventListener('focus',refresh);
  setInterval(refresh,30000);
}
