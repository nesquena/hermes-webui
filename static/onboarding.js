const ONBOARDING={status:null,step:0,steps:['system','setup','workspace','password','finish'],form:{provider:'openrouter',workspace:'',model:'',password:'',apiKey:'',baseUrl:''},active:false};

function _getOnboardingSetupProviders(){
  return (((ONBOARDING.status||{}).setup||{}).providers)||[];
}

function _getOnboardingSetupProvider(id){
  return _getOnboardingSetupProviders().find(p=>p.id===id)||null;
}

function _getOnboardingCurrentSetup(){
  return (((ONBOARDING.status||{}).setup||{}).current)||{};
}

function _onboardingStepMeta(key){
  return ({
    system:{title:t('onboarding_step_system_title'),desc:t('onboarding_step_system_desc')},
    setup:{title:t('onboarding_step_setup_title'),desc:t('onboarding_step_setup_desc')},
    workspace:{title:t('onboarding_step_workspace_title'),desc:t('onboarding_step_workspace_desc')},
    password:{title:t('onboarding_step_password_title'),desc:t('onboarding_step_password_desc')},
    finish:{title:t('onboarding_step_finish_title'),desc:t('onboarding_step_finish_desc')}
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

function _getOnboardingProviderModelChoices(){
  const provider=_getOnboardingSetupProvider(ONBOARDING.form.provider);
  return provider?(provider.models||[]):[];
}

function _getOnboardingSelectedModel(){
  return ONBOARDING.form.model||'';
}

function _renderOnboardingModelField(){
  const choices=_getOnboardingProviderModelChoices();
  if(ONBOARDING.form.provider==='custom'){
    return `<label class="onboarding-field"><span>${t('onboarding_model_label')}</span><input id="onboardingModelInput" value="${esc(_getOnboardingSelectedModel())}" placeholder="${t('onboarding_custom_model_placeholder')}" oninput="ONBOARDING.form.model=this.value"></label><p class="onboarding-copy">${t('onboarding_custom_model_help')}</p>`;
  }
  const options=choices.map(m=>`<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('');
  return `<label class="onboarding-field"><span>${t('onboarding_model_label')}</span><select id="onboardingModelSelect" onchange="ONBOARDING.form.model=this.value">${options}</select></label><p class="onboarding-copy">${t('onboarding_workspace_help')}</p>`;
}

function _providerStatusLabel(system){
  if(system.chat_ready) return t('onboarding_check_provider_ready');
  if(system.provider_configured) return t('onboarding_check_provider_partial');
  return t('onboarding_check_provider_pending');
}

function _renderOnboardingBody(){
  const body=$('onboardingBody');
  if(!body||!ONBOARDING.status)return;
  const key=ONBOARDING.steps[ONBOARDING.step];
  const system=ONBOARDING.status.system||{};
  const settings=ONBOARDING.status.settings||{};
  const setup=ONBOARDING.status.setup||{};
  const nextBtn=$('onboardingNextBtn');
  const backBtn=$('onboardingBackBtn');
  if(backBtn) backBtn.style.display=ONBOARDING.step>0?'':'none';
  if(nextBtn) nextBtn.textContent=key==='finish'?t('onboarding_open'):t('onboarding_continue');

  if(key==='system'){
    const hermesOk=system.hermes_found&&system.imports_ok;
    const setupOk=!!system.chat_ready;
    _setOnboardingNotice(system.provider_note|| (setupOk?t('onboarding_notice_system_ready'):t('onboarding_notice_system_unavailable')),setupOk?'success':(hermesOk?'info':'warn'));
    body.innerHTML=`
      <div class="onboarding-panel-grid">
        <div class="onboarding-check ${hermesOk?'ok':'warn'}"><strong>${t('onboarding_check_agent')}</strong><span>${hermesOk?t('onboarding_check_agent_ready'):t('onboarding_check_agent_missing')}</span></div>
        <div class="onboarding-check ${(setupOk?'ok':system.provider_configured?'warn':'muted')}"><strong>${t('onboarding_check_provider')}</strong><span>${_providerStatusLabel(system)}</span></div>
        <div class="onboarding-check ${(settings.password_enabled?'ok':'muted')}"><strong>${t('onboarding_check_password')}</strong><span>${settings.password_enabled?t('onboarding_check_password_enabled'):t('onboarding_check_password_disabled')}</span></div>
      </div>
      <div class="onboarding-copy">
        <p><strong>${t('onboarding_config_file')}</strong> ${esc(system.config_path||t('onboarding_unknown'))}</p>
        <p><strong>${t('onboarding_env_file')}</strong> ${esc(system.env_path||t('onboarding_unknown'))}</p>
        <p>${esc(system.provider_note||'')}</p>
        ${system.current_provider?`<p><strong>${t('onboarding_current_provider')}</strong> ${esc(system.current_provider)}${system.current_model?` — ${esc(system.current_model)}`:''}</p>`:''}
        ${system.current_base_url?`<p><strong>${t('onboarding_base_url_label')}</strong> ${esc(system.current_base_url)}</p>`:''}
        ${system.missing_modules&&system.missing_modules.length?`<p><strong>${t('onboarding_missing_imports')}</strong> ${esc(system.missing_modules.join(', '))}</p>`:''}
      </div>`;
    return;
  }

  if(key==='setup'){
    const providers=_getOnboardingSetupProviders();
    const options=providers.map(p=>`<option value="${esc(p.id)}">${esc(p.label)}${p.quick?' — '+esc(t('onboarding_quick_setup_badge')):''}</option>`).join('');
    const provider=_getOnboardingSetupProvider(ONBOARDING.form.provider)||providers[0]||null;
    const showBaseUrl=provider&&provider.requires_base_url;
    const keyHelp=provider?`${t('onboarding_api_key_help_prefix')} ${esc(provider.env_var)}.`:'';

    // OAuth provider path: configured via CLI, no API key input needed.
    const currentIsOauth=!!(ONBOARDING.status.setup||{}).current_is_oauth;
    const currentProviderName=((ONBOARDING.status.setup||{}).current||{}).provider||'';
    if(currentIsOauth){
      const isReady=!!(ONBOARDING.status.system||{}).chat_ready;
      const providerLabel=esc(currentProviderName);
      if(isReady){
        _setOnboardingNotice(t('onboarding_notice_setup_already_ready'),'success');
        body.innerHTML=`
          <div class="onboarding-oauth-card onboarding-oauth-ready">
            <div class="onboarding-oauth-icon">✓</div>
            <div>
              <strong>${t('onboarding_oauth_provider_ready_title')}</strong>
              <p>${t('onboarding_oauth_provider_ready_body').replace('{provider}',providerLabel)}</p>
            </div>
          </div>
          <p class="onboarding-copy" style="margin-top:20px">${t('onboarding_oauth_switch_hint')}</p>
          <label class="onboarding-field">
            <span>${t('onboarding_provider_label')}</span>
            <select id="onboardingProviderSelect" onchange="syncOnboardingProvider(this.value)">${options}</select>
          </label>
          <label class="onboarding-field" id="onboardingApiKeyField">
            <span>${t('onboarding_api_key_label')}</span>
            <input id="onboardingApiKeyInput" type="password" value="${esc(ONBOARDING.form.apiKey||'')}" placeholder="${t('onboarding_api_key_placeholder')}" oninput="ONBOARDING.form.apiKey=this.value">
          </label>
          ${showBaseUrl?`<label class="onboarding-field"><span>${t('onboarding_base_url_label')}</span><input id="onboardingBaseUrlInput" value="${esc(ONBOARDING.form.baseUrl||'')}" placeholder="${t('onboarding_base_url_placeholder')}" oninput="ONBOARDING.form.baseUrl=this.value"></label>`:''}
          <p class="onboarding-copy">${keyHelp}</p>`;
      } else {
        _setOnboardingNotice(t('onboarding_notice_setup_required'),'warn');
        body.innerHTML=`
          <div class="onboarding-oauth-card onboarding-oauth-pending">
            <div class="onboarding-oauth-icon">⚠</div>
            <div>
              <strong>${t('onboarding_oauth_provider_not_ready_title')}</strong>
              <p>${t('onboarding_oauth_provider_not_ready_body').replace('{provider}',providerLabel)}</p>
            </div>
          </div>
          <p class="onboarding-copy" style="margin-top:20px">${t('onboarding_oauth_switch_hint')}</p>
          <label class="onboarding-field">
            <span>${t('onboarding_provider_label')}</span>
            <select id="onboardingProviderSelect" onchange="syncOnboardingProvider(this.value)">${options}</select>
          </label>
          <label class="onboarding-field" id="onboardingApiKeyField">
            <span>${t('onboarding_api_key_label')}</span>
            <input id="onboardingApiKeyInput" type="password" value="${esc(ONBOARDING.form.apiKey||'')}" placeholder="${t('onboarding_api_key_placeholder')}" oninput="ONBOARDING.form.apiKey=this.value">
          </label>
          ${showBaseUrl?`<label class="onboarding-field"><span>${t('onboarding_base_url_label')}</span><input id="onboardingBaseUrlInput" value="${esc(ONBOARDING.form.baseUrl||'')}" placeholder="${t('onboarding_base_url_placeholder')}" oninput="ONBOARDING.form.baseUrl=this.value"></label>`:''}
          <p class="onboarding-copy">${keyHelp}</p>`;
      }
      const providerSel=$('onboardingProviderSelect');
      if(providerSel) providerSel.value=ONBOARDING.form.provider;
      return;
    }

    _setOnboardingNotice(system.chat_ready?t('onboarding_notice_setup_already_ready'):t('onboarding_notice_setup_required'),system.chat_ready?'success':'info');
    body.innerHTML=`
      <label class="onboarding-field">
        <span>${t('onboarding_provider_label')}</span>
        <select id="onboardingProviderSelect" onchange="syncOnboardingProvider(this.value)">${options}</select>
      </label>
      <label class="onboarding-field">
        <span>${t('onboarding_api_key_label')}</span>
        <input id="onboardingApiKeyInput" type="password" value="${esc(ONBOARDING.form.apiKey||'')}" placeholder="${t('onboarding_api_key_placeholder')}" oninput="ONBOARDING.form.apiKey=this.value">
      </label>
      ${showBaseUrl?`<label class="onboarding-field"><span>${t('onboarding_base_url_label')}</span><input id="onboardingBaseUrlInput" value="${esc(ONBOARDING.form.baseUrl||'')}" placeholder="${t('onboarding_base_url_placeholder')}" oninput="ONBOARDING.form.baseUrl=this.value"></label>`:''}
      <p class="onboarding-copy">${keyHelp}</p>
      ${showBaseUrl?`<p class="onboarding-copy">${t('onboarding_base_url_help')}</p>`:''}
      <p class="onboarding-copy">${esc(setup.unsupported_note||'')||''}</p>`;
    const providerSel=$('onboardingProviderSelect');
    if(providerSel) providerSel.value=ONBOARDING.form.provider;
    return;
  }

  if(key==='workspace'){
    const workspaceOptions=_getOnboardingWorkspaceChoices().map(ws=>`<option value="${esc(ws.path)}">${esc(ws.name||ws.path)} — ${esc(ws.path)}</option>`).join('');
    _setOnboardingNotice(t('onboarding_notice_workspace'), 'info');
    body.innerHTML=`
      <label class="onboarding-field">
        <span>${t('onboarding_workspace_label')}</span>
        <select id="onboardingWorkspaceSelect" onchange="syncOnboardingWorkspaceSelect(this.value)">${workspaceOptions}</select>
      </label>
      <label class="onboarding-field">
        <span>${t('onboarding_workspace_or_path')}</span>
        <input id="onboardingWorkspaceInput" value="${esc(ONBOARDING.form.workspace||'')}" placeholder="${t('onboarding_workspace_placeholder')}" oninput="ONBOARDING.form.workspace=this.value">
      </label>
      ${_renderOnboardingModelField()}`;
    const wsSel=$('onboardingWorkspaceSelect');
    if(wsSel && ONBOARDING.form.workspace) wsSel.value=ONBOARDING.form.workspace;
    const modelSel=$('onboardingModelSelect');
    if(modelSel && ONBOARDING.form.model) modelSel.value=ONBOARDING.form.model;
    return;
  }

  if(key==='password'){
    _setOnboardingNotice(settings.password_enabled?t('onboarding_notice_password_enabled'):t('onboarding_notice_password_recommended'), settings.password_enabled?'success':'info');
    body.innerHTML=`
      <label class="onboarding-field">
        <span>${t('onboarding_password_label')}</span>
        <input id="onboardingPasswordInput" type="password" value="${esc(ONBOARDING.form.password||'')}" placeholder="${t('onboarding_password_placeholder')}" oninput="ONBOARDING.form.password=this.value">
      </label>
      <p class="onboarding-copy">${t('onboarding_password_help')}</p>`;
    return;
  }

  const provider=_getOnboardingSetupProvider(ONBOARDING.form.provider);
  _setOnboardingNotice(t('onboarding_notice_finish'), 'success');
  body.innerHTML=`
    <div class="onboarding-summary">
      <div><strong>${t('onboarding_provider_label')}</strong><span>${esc((provider&&provider.label)||ONBOARDING.form.provider||t('onboarding_not_set'))}</span></div>
      <div><strong>${t('onboarding_model_label')}</strong><span>${esc(_getOnboardingSelectedModel()||t('onboarding_not_set'))}</span></div>
      <div><strong>${t('onboarding_workspace_label')}</strong><span>${esc(ONBOARDING.form.workspace||t('onboarding_not_set'))}</span></div>
      <div><strong>${t('onboarding_check_password')}</strong><span>${t(_getOnboardingPasswordSummaryKey(settings))}</span></div>
    </div>
    ${ONBOARDING.form.baseUrl?`<p class="onboarding-copy"><strong>${t('onboarding_base_url_label')}</strong> ${esc(ONBOARDING.form.baseUrl)}</p>`:''}
    <p class="onboarding-copy">${t('onboarding_finish_help')}</p>`;
}

function _getOnboardingPasswordSummaryKey(settings){
  const hasExistingPassword=!!(settings&&settings.password_enabled);
  const hasNewPassword=!!((ONBOARDING.form.password||'').trim());
  if(hasNewPassword) return hasExistingPassword?'onboarding_password_will_replace':'onboarding_password_will_enable';
  return hasExistingPassword?'onboarding_password_keep_existing':'onboarding_password_remains_disabled';
}

function syncOnboardingWorkspaceSelect(value){
  ONBOARDING.form.workspace=value;
  const input=$('onboardingWorkspaceInput');
  if(input) input.value=value;
}

function syncOnboardingProvider(value){
  const provider=_getOnboardingSetupProvider(value);
  ONBOARDING.form.provider=value;
  if(provider){
    if(!ONBOARDING.form.model || !_getOnboardingProviderModelChoices().some(m=>m.id===ONBOARDING.form.model) || value==='custom'){
      ONBOARDING.form.model=provider.default_model||'';
    }
    if(provider.requires_base_url){
      ONBOARDING.form.baseUrl=ONBOARDING.form.baseUrl||provider.default_base_url||'';
    }else{
      ONBOARDING.form.baseUrl=provider.default_base_url||'';
    }
  }
  _renderOnboardingBody();
}

async function loadOnboardingWizard(){
  try{
    const status=await api('/api/onboarding/status');
    ONBOARDING.status=status;
    const current=((status.setup||{}).current)||{};
    ONBOARDING.form.provider=current.provider||'openrouter';
    ONBOARDING.form.workspace=(status.workspaces&&status.workspaces.last)||status.settings.default_workspace||'';
    ONBOARDING.form.model=status.settings.default_model||current.model||'openai/gpt-5.4-mini';
    ONBOARDING.form.password='';
    ONBOARDING.form.apiKey='';
    ONBOARDING.form.baseUrl=current.base_url||'';
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

async function _saveOnboardingProviderSetup(){
  const provider=(ONBOARDING.form.provider||'').trim();
  const model=(ONBOARDING.form.model||'').trim();
  const apiKey=(ONBOARDING.form.apiKey||'').trim();
  const baseUrl=(ONBOARDING.form.baseUrl||'').trim();
  const current=_getOnboardingCurrentSetup();
  const isUnchanged=current.provider===provider&&((current.model||'')===model)&&((current.base_url||'')===baseUrl);
  if(isUnchanged && !apiKey && (ONBOARDING.status.system||{}).chat_ready) return;
  const body={provider,model};
  if(apiKey) body.api_key=apiKey;
  if(baseUrl) body.base_url=baseUrl;
  const status=await api('/api/onboarding/setup',{method:'POST',body:JSON.stringify(body)});
  ONBOARDING.status=status;
}

async function _saveOnboardingDefaults(){
  const workspace=(ONBOARDING.form.workspace||'').trim();
  const model=(ONBOARDING.form.model||'').trim();
  const password=(ONBOARDING.form.password||'').trim();
  if(!workspace) throw new Error(t('onboarding_error_choose_workspace'));
  if(!model) throw new Error(t('onboarding_error_choose_model'));
  const known=_getOnboardingWorkspaceChoices().some(ws=>ws.path===workspace);
  if(!known){
    await api('/api/workspaces/add',{method:'POST',body:JSON.stringify({path:workspace})});
  }
  const body={default_workspace:workspace,default_model:model};
  if(password) body._set_password=password;
  const saved=await api('/api/settings',{method:'POST',body:JSON.stringify(body)});
  if(ONBOARDING.status){
    ONBOARDING.status.settings={...(ONBOARDING.status.settings||{}),password_enabled:!!saved.auth_enabled};
  }
  localStorage.setItem('hermes-webui-model',model);
  if($('modelSelect')) _applyModelToDropdown(model,$('modelSelect'));
}

async function _finishOnboarding(){
  await _saveOnboardingProviderSetup();
  await _saveOnboardingDefaults();
  const done=await api('/api/onboarding/complete',{method:'POST',body:'{}'});
  ONBOARDING.status=done;
  ONBOARDING.active=false;
  $('onboardingOverlay').style.display='none';
  showToast(t('onboarding_complete'));
  await loadWorkspaceList();
  if(typeof renderSessionList==='function') await renderSessionList();
  if(!S.session && typeof newSession==='function'){
    await newSession(true);
    await renderSessionList();
  }
}

async function nextOnboardingStep(){
  try{
    if(ONBOARDING.steps[ONBOARDING.step]==='setup'){
      ONBOARDING.form.provider=(($('onboardingProviderSelect')||{}).value||ONBOARDING.form.provider||'').trim();
      ONBOARDING.form.apiKey=(($('onboardingApiKeyInput')||{}).value||'').trim();
      ONBOARDING.form.baseUrl=(($('onboardingBaseUrlInput')||{}).value||ONBOARDING.form.baseUrl||'').trim();
      if(!ONBOARDING.form.provider) throw new Error(t('onboarding_error_provider_required'));
      if(ONBOARDING.form.provider==='custom' && !ONBOARDING.form.baseUrl) throw new Error(t('onboarding_error_base_url_required'));
    }
    if(ONBOARDING.steps[ONBOARDING.step]==='workspace'){
      ONBOARDING.form.workspace=(($('onboardingWorkspaceInput')||{}).value||ONBOARDING.form.workspace||'').trim();
      ONBOARDING.form.model=(($('onboardingModelInput')||{}).value||($('onboardingModelSelect')||{}).value||ONBOARDING.form.model||'').trim();
      if(!ONBOARDING.form.workspace) throw new Error(t('onboarding_error_workspace_required'));
      if(!ONBOARDING.form.model) throw new Error(t('onboarding_error_model_required'));
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
