const ONBOARDING={status:null,step:0,steps:['system','setup','workspace','password','finish'],form:{provider:'openrouter',workspace:'',model:'',password:'',apiKey:'',baseUrl:''},active:false,probe:{status:'idle',error:null,detail:'',models:null,probedKey:''}};

// ── Onboarding base-URL probe (#1499) ───────────────────────────────────────
// Probes <base_url>/models so the wizard can validate the configured endpoint
// before persisting AND populate the model dropdown from the live catalog.
// Probe state lives on ONBOARDING.probe; the dropdown render and the
// nextOnboardingStep gate both consult it.

let _onboardingProbeTimer=null;

function _onboardingProbeKey(provider,baseUrl,apiKey){
  return `${provider||''}|${(baseUrl||'').trim().replace(/\/+$/,'')}|${apiKey||''}`;
}

function _setOnboardingProbeState(patch){
  ONBOARDING.probe={...ONBOARDING.probe,...patch};
  // Re-render body so probe status / model dropdown reflect new state.
  _renderOnboardingBody();
}

async function _runOnboardingProbe({force=false}={}){
  const provider=ONBOARDING.form.provider;
  const cat=_getOnboardingSetupProvider(provider);
  if(!cat||!cat.requires_base_url){
    _setOnboardingProbeState({status:'idle',error:null,detail:'',models:null,probedKey:''});
    return ONBOARDING.probe;
  }
  const baseUrl=(ONBOARDING.form.baseUrl||'').trim();
  if(!baseUrl){
    _setOnboardingProbeState({status:'idle',error:null,detail:'',models:null,probedKey:''});
    return ONBOARDING.probe;
  }
  const apiKey=(ONBOARDING.form.apiKey||'').trim();
  const key=_onboardingProbeKey(provider,baseUrl,apiKey);
  if(!force&&ONBOARDING.probe.probedKey===key&&ONBOARDING.probe.status!=='probing'){
    return ONBOARDING.probe;
  }
  _setOnboardingProbeState({status:'probing',error:null,detail:'',probedKey:key});
  try{
    const res=await api('/api/onboarding/probe',{method:'POST',body:JSON.stringify({provider,base_url:baseUrl,api_key:apiKey||undefined})});
    if(res&&res.ok){
      _setOnboardingProbeState({status:'ok',error:null,detail:'',models:Array.isArray(res.models)?res.models:[],probedKey:key});
      // If the user hasn't picked a model yet (or their pick is no longer in
      // the list), default to the first probed model so Continue isn't blocked
      // on an empty selection.
      const stillPresent=ONBOARDING.form.model&&(res.models||[]).some(m=>m.id===ONBOARDING.form.model);
      if(!stillPresent&&(res.models||[]).length>0){
        ONBOARDING.form.model=res.models[0].id;
        _renderOnboardingBody();
      }
    }else{
      const err=(res&&res.error)||'unreachable';
      const detail=(res&&res.detail)||'';
      _setOnboardingProbeState({status:'error',error:err,detail,models:null,probedKey:key});
    }
  }catch(e){
    _setOnboardingProbeState({status:'error',error:'unreachable',detail:(e&&e.message)||String(e),models:null,probedKey:key});
  }
  return ONBOARDING.probe;
}

function _scheduleOnboardingProbe(){
  if(_onboardingProbeTimer)clearTimeout(_onboardingProbeTimer);
  _onboardingProbeTimer=setTimeout(()=>{_runOnboardingProbe();},400);
}

function _onboardingProbeMessage(probe){
  if(!probe||probe.status==='idle')return '';
  if(probe.status==='probing')return t('onboarding_probe_probing')||'Testing connection…';
  if(probe.status==='ok'){
    const n=(probe.models||[]).length;
    const tmpl=t('onboarding_probe_ok')||'Connected. {n} model(s) available.';
    return tmpl.replace('{n}',String(n));
  }
  // status === 'error'
  const errKey='onboarding_probe_error_'+probe.error;
  const localized=t(errKey);
  // i18n.js's `t()` returns the key itself when missing — fall back to a generic message.
  const heading=(localized&&localized!==errKey)?localized:(t('onboarding_probe_error_generic')||'Could not reach the configured base URL.');
  const detail=probe.detail?` (${probe.detail})`:'';
  return heading+detail;
}

function _getOnboardingSetupProviders(){
  return (((ONBOARDING.status||{}).setup||{}).providers)||[];
}

function _getOnboardingSetupProvider(id){
  return _getOnboardingSetupProviders().find(p=>p.id===id)||null;
}

function _getOnboardingSetupCategories(){
  return (((ONBOARDING.status||{}).setup||{}).categories)||[];
}

/** Render the provider <select> with <optgroup> per category. */
function _renderProviderSelectOptions(selectedId){
  const providers=_getOnboardingSetupProviders();
  const categories=_getOnboardingSetupCategories();
  const provMap={};
  providers.forEach(p=>{provMap[p.id]=p;});
  if(!categories.length){
    // Fallback: flat list when no categories are available.
    return providers.map(p=>`<option value="${esc(p.id)}">${esc(p.label)}${p.quick?' — '+esc(t('onboarding_quick_setup_badge')):''}</option>`).join('');
  }
  return categories.map(cat=>{
    const opts=cat.providers.map(pid=>{
      const p=provMap[pid];
      if(!p)return '';
      return `<option value="${esc(p.id)}"${p.id===selectedId?' selected':''}>${esc(p.label)}${p.quick?' — '+esc(t('onboarding_quick_setup_badge')):''}</option>`;
    }).join('');
    return `<optgroup label="${esc(t('provider_category_'+cat.id)||cat.label)}">${opts}</optgroup>`;
  }).join('');
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
  // Probe-discovered models (#1499) take precedence over the static catalog
  // for providers with requires_base_url=True.  The catalog ships an empty
  // list for self-hosted providers (lmstudio, ollama, custom) — without the
  // probe the user had nothing to pick from.
  if(provider&&provider.requires_base_url&&ONBOARDING.probe&&ONBOARDING.probe.status==='ok'&&Array.isArray(ONBOARDING.probe.models)&&ONBOARDING.probe.models.length){
    return ONBOARDING.probe.models;
  }
  return provider?(provider.models||[]):[];
}

function _renderOnboardingBaseUrlField(showBaseUrl){
  // Renders the base_url input PLUS the probe status banner / Test button
  // when the active provider has requires_base_url=True (#1499).  Returns
  // the empty string when the active provider does not require a base URL,
  // so the existing call sites can continue to template-interpolate this in
  // place of the previous inline `<label …>` snippet.
  if(!showBaseUrl)return '';
  const probe=ONBOARDING.probe||{status:'idle'};
  const msg=_onboardingProbeMessage(probe);
  let banner='';
  if(msg){
    const cls={ok:'onboarding-probe-ok',probing:'onboarding-probe-probing',error:'onboarding-probe-error'}[probe.status]||'';
    banner=`<p class="onboarding-copy onboarding-probe-banner ${cls}">${esc(msg)}</p>`;
  }
  const testBtnLabel=t('onboarding_probe_test_button')||'Test connection';
  const testBtnDisabled=(probe.status==='probing')?'disabled':'';
  return `<label class="onboarding-field"><span>${t('onboarding_base_url_label')}</span><input id="onboardingBaseUrlInput" value="${esc(ONBOARDING.form.baseUrl||'')}" placeholder="${t('onboarding_base_url_placeholder')}" oninput="ONBOARDING.form.baseUrl=this.value;_scheduleOnboardingProbe()" onblur="_runOnboardingProbe()"></label><div class="onboarding-probe-row"><button type="button" class="onboarding-probe-btn" ${testBtnDisabled} onclick="_runOnboardingProbe({force:true})">${esc(testBtnLabel)}</button></div>${banner}`;
}

function _renderOnboardingApiKeyField(){
  // Renders the API-key input.  For providers flagged `key_optional` in the
  // setup catalog (lmstudio, ollama, custom — typically self-hosted servers
  // that run keyless by default), the field shows an "(optional)" hint and
  // empty input is accepted on Continue.  Pre-#1499-third-sub-bug-fix the
  // wizard required a non-empty string here even for keyless installs, which
  // forced users to type random gibberish to clear onboarding.
  const provider=_getOnboardingSetupProvider(ONBOARDING.form.provider);
  const keyOptional=!!(provider&&provider.key_optional);
  const labelKey=keyOptional?'onboarding_api_key_label_optional':'onboarding_api_key_label';
  const placeholderKey=keyOptional?'onboarding_api_key_placeholder_optional':'onboarding_api_key_placeholder';
  const helpHtml=keyOptional?`<p class="onboarding-copy onboarding-api-key-help">${esc(t('onboarding_api_key_help_keyless')||'')}</p>`:'';
  return `<label class="onboarding-field" id="onboardingApiKeyField"><span>${t(labelKey)}</span><input id="onboardingApiKeyInput" type="password" value="${esc(ONBOARDING.form.apiKey||'')}" placeholder="${t(placeholderKey)}" oninput="ONBOARDING.form.apiKey=this.value" onblur="_runOnboardingProbe()"></label>${helpHtml}`;
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
    const selectedId=ONBOARDING.form.provider;
    const groupedOptions=_renderProviderSelectOptions(selectedId);
    const provider=_getOnboardingSetupProvider(selectedId)||_getOnboardingSetupProviders()[0]||null;
    const showBaseUrl=provider&&provider.requires_base_url;
    const keyHelp=provider?`${t('onboarding_api_key_help_prefix')} ${esc(provider.env_var)}.`:'';

    // OAuth provider path: configured via CLI, no API key input needed.
    const currentIsOauth=!!(ONBOARDING.status.setup||{}).current_is_oauth;
    const currentProviderName=((ONBOARDING.status.setup||{}).current||{}).provider||'';
    if(currentIsOauth){
      const isReady=!!(ONBOARDING.status.system||{}).chat_ready;
      const providerLabel=esc(currentProviderName);
      const codexOauthPendingBody=currentProviderName==='openai-codex'
        ? 'This instance is configured to use <strong>openai-codex</strong>, which uses OAuth rather than an API key. Use the button below to authenticate with ChatGPT, then continue once provider status refreshes.'
        : t('onboarding_oauth_provider_not_ready_body').replace('{provider}',providerLabel);
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
            <select id="onboardingProviderSelect" onchange="syncOnboardingProvider(this.value)">${groupedOptions}</select>
          </label>
          ${_renderOnboardingApiKeyField()}
          ${_renderOnboardingBaseUrlField(showBaseUrl)}
          <p class="onboarding-copy">${keyHelp}</p>`;
      } else {
        _setOnboardingNotice(t('onboarding_notice_setup_required'),'warn');
        body.innerHTML=`
          <div class="onboarding-oauth-card onboarding-oauth-pending">
            <div class="onboarding-oauth-icon">⚠</div>
            <div style="flex:1">
              <strong>${t('onboarding_oauth_provider_not_ready_title')}</strong>
              <p>${codexOauthPendingBody}</p>
              ${currentProviderName==='openai-codex'?`<div style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap"><button class="sm-btn" id="codexOAuthBtn" onclick="startCodexOAuth()" type="button">${t('oauth_login_codex')}</button></div><div id="codexOAuthFlow" style="display:none;margin-top:12px"></div>`:''}
            </div>
          </div>
          <p class="onboarding-copy" style="margin-top:20px">${t('onboarding_oauth_switch_hint')}</p>
          <label class="onboarding-field">
            <span>${t('onboarding_provider_label')}</span>
            <select id="onboardingProviderSelect" onchange="syncOnboardingProvider(this.value)">${groupedOptions}</select>
          </label>
          ${_renderOnboardingApiKeyField()}
          ${_renderOnboardingBaseUrlField(showBaseUrl)}
          <p class="onboarding-copy">${keyHelp}</p>`;
      }
      return;
    }

    _setOnboardingNotice(system.chat_ready?t('onboarding_notice_setup_already_ready'):t('onboarding_notice_setup_required'),system.chat_ready?'success':'info');
    body.innerHTML=`
      <label class="onboarding-field">
        <span>${t('onboarding_provider_label')}</span>
        <select id="onboardingProviderSelect" onchange="syncOnboardingProvider(this.value)">${groupedOptions}</select>
      </label>
      ${_renderOnboardingApiKeyField()}
      ${_renderOnboardingBaseUrlField(showBaseUrl)}
      <p class="onboarding-copy">${keyHelp}</p>
      ${showBaseUrl?`<p class="onboarding-copy">${t('onboarding_base_url_help')}</p>`:''}
      <p class="onboarding-copy">${esc(setup.unsupported_note||'')||''}</p>`;
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
    ONBOARDING.form.model=status.settings.default_model||current.model||'';
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
  // Skip the POST when nothing changed.  We also skip when the provider is
  // unsupported/OAuth-based and already working — chat_ready may be false for
  // providers not in the quick-setup list (e.g. minimax-cn) even though they are
  // fully configured.  Posting in that case would either be a no-op (the server
  // just marks complete for unsupported providers) or could silently overwrite
  // config.yaml if the user accidentally changed the provider dropdown.
  const currentIsOauth=!!(ONBOARDING.status&&ONBOARDING.status.setup&&ONBOARDING.status.setup.current_is_oauth);
  if(isUnchanged && !apiKey && ((ONBOARDING.status.system||{}).chat_ready || currentIsOauth)) return;
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
  // Model persisted by /api/onboarding/setup — no /api/default-model call needed here
  const body={default_workspace:workspace};
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

async function skipOnboarding(){
  try{
    // Mark onboarding completed server-side without changing any config
    await api('/api/onboarding/complete',{method:'POST',body:'{}'});
    ONBOARDING.active=false;
    $('onboardingOverlay').style.display='none';
    showToast(t('onboarding_skipped')||'Setup skipped');
  }catch(e){
    _setOnboardingNotice((e.message||String(e)),'warn');
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
      // For self-hosted providers (requires_base_url=True), gate Continue on a
      // successful probe of <base_url>/models — otherwise the wizard would
      // happily persist an unreachable URL and finish in 200ms with no
      // outbound HTTP, exactly the bug in #1499.  Run the probe synchronously
      // here, then check status; the probe is idempotent & cached on
      // (provider, baseUrl, apiKey) so this rarely triggers a second network
      // call when the user already saw a green banner.
      const cat=_getOnboardingSetupProvider(ONBOARDING.form.provider);
      if(cat&&cat.requires_base_url){
        if(!ONBOARDING.form.baseUrl) throw new Error(t('onboarding_error_base_url_required'));
        await _runOnboardingProbe();
        if(ONBOARDING.probe.status!=='ok'){
          // Surface the same localized error string the inline banner shows.
          const msg=_onboardingProbeMessage(ONBOARDING.probe)||t('onboarding_error_probe_failed')||'Could not reach the configured base URL.';
          throw new Error(msg);
        }
      }
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

/* ── Codex OAuth device-code flow ── */
let _codexOAuthPollTimer=null;
let _codexOAuthFlowId=null;

function _clearCodexOAuthPoll(){
  if(_codexOAuthPollTimer){clearTimeout(_codexOAuthPollTimer);_codexOAuthPollTimer=null;}
}

function _setCodexOAuthButton(enabled){
  const btn=$('codexOAuthBtn');
  if(btn){btn.disabled=!enabled;btn.textContent=enabled?t('oauth_login_codex'):'...';}
}

async function copyCodexOAuthCode(code){
  try{
    await navigator.clipboard.writeText(code||'');
    showToast('Code copied');
  }catch(e){
    showToast(code||'');
  }
}

async function cancelCodexOAuth(){
  const flowDiv=$('codexOAuthFlow');
  const flowId=_codexOAuthFlowId;
  _clearCodexOAuthPoll();
  _codexOAuthFlowId=null;
  if(flowId){
    try{await api('/api/onboarding/oauth/cancel',{method:'POST',body:JSON.stringify({flow_id:flowId})});}catch(e){}
  }
  _setCodexOAuthButton(true);
  if(flowDiv){
    flowDiv.innerHTML=`<div class="onboarding-oauth-card"><div class="onboarding-oauth-icon">⏹</div><div><strong>OAuth login cancelled</strong><p style="margin-top:6px;color:var(--muted);font-size:13px">Start again whenever you're ready.</p></div></div>`;
  }
}

function _renderCodexOAuthTerminal(status,message){
  const flowDiv=$('codexOAuthFlow');
  if(!flowDiv)return;
  const ok=status==='success';
  const icon=ok?'✅':status==='expired'?'⌛':status==='cancelled'?'⏹':'❌';
  const title=ok?t('oauth_codex_success'):(status==='expired'?t('oauth_codex_expired'):(status==='cancelled'?'OAuth login cancelled':t('oauth_codex_error')));
  flowDiv.innerHTML=`
    <div class="onboarding-oauth-card ${ok?'onboarding-oauth-ready':''}" ${ok?'':'style="border-color:var(--error,#e55)"'}>
      <div class="onboarding-oauth-icon">${icon}</div>
      <div><strong>${title}</strong><p style="margin-top:6px;color:var(--muted);font-size:13px">${esc(message||'')}</p></div>
    </div>`;
}

async function _pollCodexOAuth(){
  const flowId=_codexOAuthFlowId;
  if(!flowId)return;
  try{
    const resp=await api('/api/onboarding/oauth/poll?flow_id='+encodeURIComponent(flowId));
    const status=(resp&&resp.status)||'error';
    if(status==='pending'){
      _codexOAuthPollTimer=setTimeout(_pollCodexOAuth,3000);
      return;
    }
    _clearCodexOAuthPoll();
    _codexOAuthFlowId=null;
    _setCodexOAuthButton(true);
    if(status==='success'){
      _renderCodexOAuthTerminal('success','Credentials saved to the Hermes credential pool. Refreshing provider status…');
      showToast(t('oauth_codex_success'));
      try{await loadOnboardingWizard();}catch(e){}
    }else if(status==='expired'){
      _renderCodexOAuthTerminal('expired','The code expired. Start a new login flow to try again.');
    }else if(status==='cancelled'){
      _renderCodexOAuthTerminal('cancelled','The login flow was cancelled.');
    }else{
      _renderCodexOAuthTerminal('error',(resp&&resp.error)||'OAuth login failed. Please try again.');
    }
  }catch(e){
    _clearCodexOAuthPoll();
    _codexOAuthFlowId=null;
    _setCodexOAuthButton(true);
    _renderCodexOAuthTerminal('error',(e&&e.message)||String(e));
  }
}

async function startCodexOAuth(){
  const flowDiv=$('codexOAuthFlow');
  if(!flowDiv)return;
  _clearCodexOAuthPoll();
  _codexOAuthFlowId=null;
  _setCodexOAuthButton(false);
  flowDiv.style.display='block';
  flowDiv.innerHTML=`<div class="onboarding-oauth-card onboarding-oauth-pending"><div class="onboarding-oauth-icon">⏳</div><div><strong>${t('oauth_codex_polling')}</strong><p>Starting device-code flow…</p></div></div>`;
  try{
    const resp=await api('/api/onboarding/oauth/start',{method:'POST',body:JSON.stringify({provider:'openai-codex'})});
    if(resp.error) throw new Error(resp.error);
    const{flow_id,user_code,verification_uri}=resp;
    if(!flow_id||!user_code||!verification_uri) throw new Error('Invalid OAuth response');
    _codexOAuthFlowId=flow_id;
    flowDiv.innerHTML=`
      <div class="onboarding-oauth-card onboarding-oauth-pending">
        <div class="onboarding-oauth-icon">📋</div>
        <div style="flex:1">
          <strong>${t('oauth_codex_step1')}</strong>
          <p><a href="${esc(verification_uri)}" target="_blank" rel="noopener" style="color:var(--accent);word-break:break-all">${esc(verification_uri)}</a></p>
          <p style="margin-top:8px"><strong>${t('oauth_codex_step2')}</strong></p>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:4px">
            <code style="display:inline-block;font-size:18px;letter-spacing:0.1em;background:rgba(255,255,255,.08);padding:6px 14px;border-radius:8px;user-select:all">${esc(user_code)}</code>
            <button class="sm-btn" type="button" onclick="copyCodexOAuthCode('${esc(user_code)}')">Copy code</button>
            <button class="sm-btn" type="button" onclick="cancelCodexOAuth()">Cancel</button>
          </div>
          <p style="margin-top:8px;color:var(--muted);font-size:13px">${t('oauth_codex_polling')}</p>
        </div>
      </div>`;
    _codexOAuthPollTimer=setTimeout(_pollCodexOAuth,Math.max(1000,Number(resp.poll_interval_seconds||3)*1000));
  }catch(e){
    _clearCodexOAuthPoll();
    _codexOAuthFlowId=null;
    _renderCodexOAuthTerminal('error',(e&&e.message)||String(e));
    _setCodexOAuthButton(true);
  }
}
