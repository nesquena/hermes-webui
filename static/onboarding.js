const ONBOARDING={status:null,step:0,steps:['system','workspace','password','finish'],form:{workspace:'',model:'',password:''},active:false};

function _onboardingStepMeta(key){
  return ({
    system:{title:'System check',desc:'Verify Hermes Agent and config visibility.'},
    workspace:{title:'Workspace + model',desc:'Pick defaults for new sessions.'},
    password:{title:'Optional password',desc:'Protect the Web UI before sharing it.'},
    finish:{title:'Finish',desc:'Review and enter the app.'}
  })[key];
}

function _renderOnboardingSteps(){
  const wrap=$('onboardingSteps');
  if(!wrap)return;
  wrap.innerHTML='';
  ONBOARDING.steps.forEach((key,idx)=>{
    const meta=_onboardingStepMeta(key);
    const item=document.createElement('div');
    item.className='onboarding-step'+(idx===ONBOARDING.step?' active':idx<ONBOARDING.step?' done':'');
    item.innerHTML=`<div class="onboarding-step-index">${idx+1}</div><div><div class="onboarding-step-title">${meta.title}</div><div class="onboarding-step-desc">${meta.desc}</div></div>`;
    wrap.appendChild(item);
  });
}

function _setOnboardingNotice(msg,kind='info'){
  const el=$('onboardingNotice');
  if(!el)return;
  if(!msg){el.style.display='none';el.textContent='';el.className='onboarding-status';return;}
  el.style.display='block';
  el.className='onboarding-status '+kind;
  el.textContent=msg;
}

function _getOnboardingWorkspaceChoices(){
  const items=((ONBOARDING.status||{}).workspaces||{}).items||[];
  return items.length?items:[{name:'Home',path:ONBOARDING.form.workspace||''}];
}

function _getOnboardingModelChoices(){
  const groups=((ONBOARDING.status||{}).models||{}).groups||[];
  const flat=[];
  groups.forEach(g=>(g.models||[]).forEach(m=>flat.push({...m,provider:g.provider})));
  return flat;
}

function _renderOnboardingBody(){
  const body=$('onboardingBody');
  if(!body||!ONBOARDING.status)return;
  const key=ONBOARDING.steps[ONBOARDING.step];
  const system=ONBOARDING.status.system||{};
  const settings=ONBOARDING.status.settings||{};
  const nextBtn=$('onboardingNextBtn');
  const backBtn=$('onboardingBackBtn');
  if(backBtn) backBtn.style.display=ONBOARDING.step>0?'':'none';
  if(nextBtn) nextBtn.textContent=key==='finish'?'Open Hermes':'Continue';

  if(key==='system'){
    const hermesOk=system.hermes_found&&system.imports_ok;
    _setOnboardingNotice(hermesOk?'Hermes Agent looks reachable from the Web UI.':'Hermes Agent is not fully available yet. Bootstrap can install it, but provider setup may still require a terminal.',hermesOk?'success':'warn');
    body.innerHTML=`
      <div class="onboarding-panel-grid">
        <div class="onboarding-check ${hermesOk?'ok':'warn'}"><strong>Hermes Agent</strong><span>${hermesOk?'Detected and importable':'Missing or partially importable'}</span></div>
        <div class="onboarding-check ${(settings.password_enabled?'ok':'muted')}"><strong>Password</strong><span>${settings.password_enabled?'Already enabled':'Not enabled yet'}</span></div>
        <div class="onboarding-check ${(system.provider_configured?'ok':'muted')}"><strong>Provider config</strong><span>${system.provider_configured?'Config detected':'Needs verification'}</span></div>
      </div>
      <div class="onboarding-copy">
        <p><strong>Config file:</strong> ${esc(system.config_path||'Unknown')}</p>
        <p>${esc(system.provider_note||'')}</p>
        ${system.missing_modules&&system.missing_modules.length?`<p><strong>Missing imports:</strong> ${esc(system.missing_modules.join(', '))}</p>`:''}
      </div>`;
    return;
  }

  if(key==='workspace'){
    const workspaceOptions=_getOnboardingWorkspaceChoices().map(ws=>`<option value="${esc(ws.path)}">${esc(ws.name||ws.path)} — ${esc(ws.path)}</option>`).join('');
    const modelOptions=((ONBOARDING.status.models||{}).groups||[]).map(g=>`<optgroup label="${esc(g.provider)}">${(g.models||[]).map(m=>`<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('')}</optgroup>`).join('');
    _setOnboardingNotice('These values reuse the same settings APIs as the normal app.', 'info');
    body.innerHTML=`
      <label class="onboarding-field">
        <span>Workspace</span>
        <select id="onboardingWorkspaceSelect" onchange="syncOnboardingWorkspaceSelect(this.value)">${workspaceOptions}</select>
      </label>
      <label class="onboarding-field">
        <span>Or enter a workspace path</span>
        <input id="onboardingWorkspaceInput" value="${esc(ONBOARDING.form.workspace||'')}" placeholder="/home/you/workspace" oninput="ONBOARDING.form.workspace=this.value">
      </label>
      <label class="onboarding-field">
        <span>Default model</span>
        <select id="onboardingModelSelect" onchange="ONBOARDING.form.model=this.value">${modelOptions}</select>
      </label>
      <p class="onboarding-copy">If provider readiness is uncertain, choose the model you expect to use and finish any remaining login/API-key setup later with <code>hermes model</code>.</p>`;
    const wsSel=$('onboardingWorkspaceSelect');
    if(wsSel && ONBOARDING.form.workspace) wsSel.value=ONBOARDING.form.workspace;
    const modelSel=$('onboardingModelSelect');
    if(modelSel && ONBOARDING.form.model) _applyModelToDropdown(ONBOARDING.form.model, modelSel);
    return;
  }

  if(key==='password'){
    _setOnboardingNotice(settings.password_enabled?'A password is already configured. Enter a new one only if you want to replace it.':'Optional but recommended if you will expose the UI beyond localhost.', settings.password_enabled?'success':'info');
    body.innerHTML=`
      <label class="onboarding-field">
        <span>Password (optional)</span>
        <input id="onboardingPasswordInput" type="password" value="${esc(ONBOARDING.form.password||'')}" placeholder="Leave blank to skip" oninput="ONBOARDING.form.password=this.value">
      </label>
      <p class="onboarding-copy">Passwords are stored through the existing settings API and hashed server-side.</p>`;
    return;
  }

  _setOnboardingNotice('You can reopen Settings later to change any of this.', 'success');
  body.innerHTML=`
    <div class="onboarding-summary">
      <div><strong>Workspace</strong><span>${esc(ONBOARDING.form.workspace||'Not set')}</span></div>
      <div><strong>Model</strong><span>${esc(getModelLabel(ONBOARDING.form.model)||ONBOARDING.form.model)}</span></div>
      <div><strong>Password</strong><span>${ONBOARDING.form.password?'Will be enabled':'Skipped for now'}</span></div>
    </div>
    <p class="onboarding-copy">Finishing stores <code>onboarding_completed</code> in settings and drops you into the normal app.</p>`;
}

function syncOnboardingWorkspaceSelect(value){
  ONBOARDING.form.workspace=value;
  const input=$('onboardingWorkspaceInput');
  if(input) input.value=value;
}

async function loadOnboardingWizard(){
  try{
    const status=await api('/api/onboarding/status');
    ONBOARDING.status=status;
    ONBOARDING.form.workspace=(status.workspaces&&status.workspaces.last)||status.settings.default_workspace||'';
    ONBOARDING.form.model=status.settings.default_model||((status.models||{}).default_model)||'';
    ONBOARDING.form.password='';
    ONBOARDING.active=!status.completed;
    if(!ONBOARDING.active) return false;
    $('onboardingOverlay').style.display='flex';
    _renderOnboardingSteps();
    _renderOnboardingBody();
    return true;
  }catch(e){
    console.warn('onboarding status failed',e);
    return false;
  }
}

function prevOnboardingStep(){
  if(ONBOARDING.step===0)return;
  ONBOARDING.step--;
  _renderOnboardingSteps();
  _renderOnboardingBody();
}

async function _saveOnboardingDefaults(){
  const workspace=(ONBOARDING.form.workspace||'').trim();
  const model=(ONBOARDING.form.model||'').trim();
  const password=(ONBOARDING.form.password||'').trim();
  if(!workspace) throw new Error('Choose a workspace before continuing.');
  if(!model) throw new Error('Choose a model before continuing.');
  const known=_getOnboardingWorkspaceChoices().some(ws=>ws.path===workspace);
  if(!known){
    await api('/api/workspaces/add',{method:'POST',body:JSON.stringify({path:workspace})});
  }
  const body={default_workspace:workspace,default_model:model};
  if(password) body._set_password=password;
  await api('/api/settings',{method:'POST',body:JSON.stringify(body)});
  localStorage.setItem('hermes-webui-model',model);
  if($('modelSelect')) _applyModelToDropdown(model,$('modelSelect'));
}

async function _finishOnboarding(){
  await _saveOnboardingDefaults();
  const done=await api('/api/onboarding/complete',{method:'POST',body:'{}'});
  ONBOARDING.status=done;
  ONBOARDING.active=false;
  $('onboardingOverlay').style.display='none';
  showToast('Onboarding complete');
  await loadWorkspaceList();
  if(typeof renderSessionList==='function') await renderSessionList();
  if(!S.session && typeof newSession==='function'){
    await newSession(true);
    await renderSessionList();
  }
}

async function nextOnboardingStep(){
  try{
    if(ONBOARDING.steps[ONBOARDING.step]==='workspace'){
      ONBOARDING.form.workspace=(($('onboardingWorkspaceInput')||{}).value||ONBOARDING.form.workspace||'').trim();
      ONBOARDING.form.model=(($('onboardingModelSelect')||{}).value||ONBOARDING.form.model||'').trim();
      if(!ONBOARDING.form.workspace) throw new Error('Workspace is required.');
      if(!ONBOARDING.form.model) throw new Error('Model is required.');
    }
    if(ONBOARDING.steps[ONBOARDING.step]==='password'){
      ONBOARDING.form.password=(($('onboardingPasswordInput')||{}).value||'').trim();
    }
    if(ONBOARDING.step===ONBOARDING.steps.length-1){
      await _finishOnboarding();
      return;
    }
    ONBOARDING.step++;
    _renderOnboardingSteps();
    _renderOnboardingBody();
  }catch(e){
    _setOnboardingNotice(e.message||String(e),'warn');
  }
}
