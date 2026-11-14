// Guided tours, Explain this page and What's new (plan 6.1, W4).
//
// This is the only code that touches Guida (static/vendor/guida/0.2.0). It
// builds steps at run time from what the person can see: panels hidden by
// governance, by the tab settings or by an inactive module are skipped, and
// help pages, sections and steps with a module, modules (all of) or audience
// key only show when they apply. Titles and texts come from
// static/help-content.js (English and Dutch) and are escaped before they
// reach Guida, which writes them with innerHTML. When Guida is missing or no
// target is visible, Explain this page opens a plain popover instead.
//
// Public API: window.SynthPulseHelp = {startTour(id), explainPage(id),
// hasSeen(id), showWhatsNew(opts), closeAll(), stepsFor(id), currentPage()}.
// Nothing starts on its own: a tour is only offered once after onboarding.
(function(){
  'use strict';
  if(window.SynthPulseHelp) return;

  const STORAGE_VERSION='v1';
  const EVENTS_URL='api/help/events';
  const TOURS=['first_run','org_admin'];
  const ID_RE=/^[a-z][a-z0-9_]{1,39}$/;
  const EN={
    help_page_button:'Explain this page',help_tour_first_run:'Getting started',
    help_tour_org_admin:'Tour for organisation admins',help_tour_next:'Next',help_tour_back:'Back',
    help_tour_close:'Close',help_tour_done:'Done',help_tour_step_of:'Step {0} of {1}',
    help_tour_completed:'Tour completed',help_tour_offer:'Want a short tour of the basics?',
    help_tour_offer_yes:'Show me',help_tour_offer_no:'Not now',help_explain_fallback_title:'About this page',
    help_still_stuck:'Still stuck? Ask support',help_did_not_help:'This did not help',
    help_whats_new_title:"What's new",help_whats_new_dismiss:'Got it',
  };

  let activeRun=null;
  let featureLoadGeneration=0;
  let popover=null;
  let settingsPromise=null;
  let pageBeforeSettings='';
  let uid=0;

  // ── Small helpers ──────────────────────────────────────────────────────────
  function tr(key,...args){
    try{
      if(typeof window.t==='function'){
        const value=window.t(key,...args);
        if(typeof value==='string'&&value&&value!==key)return value;
      }
    }catch(_){}
    let value=EN[key]||key;
    args.forEach((arg,i)=>{value=value.split('{'+i+'}').join(String(arg));});
    return value;
  }
  function escHtml(value){
    return String(value==null?'':value)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  function storeGet(key){try{return window.localStorage.getItem(key);}catch(_){return null;}}
  function storeSet(key,value){try{window.localStorage.setItem(key,value);return true;}catch(_){return false;}}
  function storeRemove(key){try{window.localStorage.removeItem(key);}catch(_){}}
  function me(){const value=window.__GOV_ME__;return value&&typeof value==='object'?value:null;}
  function userKey(){
    const email=me()&&typeof me().email==='string'?me().email.trim().toLowerCase():'';
    return email||'local';
  }
  function tourStorageKey(id){return 'synthpulse-tour:'+userKey()+':'+id+':'+STORAGE_VERSION;}
  function prefKey(name){return 'synthpulse-help:'+userKey()+':'+name+':'+STORAGE_VERSION;}
  function query(selector){
    if(typeof selector!=='string'||!selector)return null;
    try{return document.querySelector(selector);}catch(_){return null;}
  }
  function el(tag,className,text){
    const node=document.createElement(tag);
    if(className)node.className=className;
    if(text!=null)node.textContent=String(text);
    return node;
  }
  function button(className,text,onClick){
    const node=el('button',className,text);
    node.type='button';
    if(onClick)node.addEventListener('click',onClick);
    return node;
  }
  function focusable(root){
    return Array.from(root.querySelectorAll('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])'))
      .filter(node=>!node.disabled&&!node.hidden&&node.getClientRects().length);
  }
  function restoreFocus(target){
    try{if(target&&target.isConnected&&typeof target.focus==='function')target.focus({preventScroll:true});}catch(_){}
  }
  function reducedMotion(){
    try{return window.matchMedia('(prefers-reduced-motion: reduce)').matches;}catch(_){return false;}
  }
  function isDesktop(){
    try{
      if(typeof window._isDesktopWidth==='function')return !!window._isDesktopWidth();
      return window.matchMedia('(min-width:641px)').matches;
    }catch(_){return true;}
  }
  function toast(message){
    try{if(typeof window.showToast==='function')window.showToast(message,3000,'success');}catch(_){}
  }

  // An element counts as visible when it has a box on screen that no hidden,
  // transparent or zero-sized clipping ancestor takes away (a collapsed
  // sidebar or a closed mobile drawer, for example).
  function isVisible(node){
    if(!node||!node.isConnected||typeof node.getClientRects!=='function')return false;
    if(node.closest('[hidden]'))return false;
    if(!node.getClientRects().length)return false;
    const rect=node.getBoundingClientRect();
    if(rect.width<=0&&rect.height<=0)return false;
    const width=window.innerWidth||document.documentElement.clientWidth||0;
    if(width&&(rect.right<=0||rect.left>=width))return false;
    for(let cur=node;cur&&cur.nodeType===1&&cur!==document.body;cur=cur.parentElement){
      const style=window.getComputedStyle(cur);
      if(style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse')return false;
      if(Number(style.opacity)===0)return false;
      if(cur!==node&&style.overflow!=='visible'){
        const box=cur.getBoundingClientRect();
        if(box.width<=0||box.height<=0)return false;
      }
    }
    return true;
  }

  // ── Content and gates ──────────────────────────────────────────────────────
  function content(){
    const value=window.SynthPulseHelpContent;
    return value&&value.locales&&typeof value.locales==='object'&&value.locales.en?value:null;
  }
  function helpLocale(){
    const data=content();
    if(!data)return 'en';
    const has=code=>!!(data.locales[code]&&data.locales[code].pages);
    let ui='';
    try{ui=typeof _localeCode==='string'?_localeCode:'';}catch(_){ui='';}
    const base=String(ui||'').toLowerCase().split('-')[0];
    if(base&&base!=='en'&&has(base))return base;
    // Decision D18: help content is Dutch already while the UI has no Dutch
    // locale yet. Someone who did not pick English on purpose and whose
    // browser prefers Dutch gets the Dutch help.
    if((!base||base==='en')&&storeGet('hermes-lang')!=='en'){
      const langs=(navigator.languages&&navigator.languages.length)?navigator.languages:[navigator.language||''];
      for(const lang of langs){
        const code=String(lang||'').toLowerCase().split('-')[0];
        if(!code)continue;
        if(code==='en')break;
        if(has(code))return code;
      }
    }
    return 'en';
  }
  function localeData(){
    const data=content();
    if(!data)return null;
    return data.locales[helpLocale()]||data.locales.en;
  }
  function pick(kind,id){
    const data=content();
    if(!data||typeof id!=='string')return null;
    const own=localeData()||{};
    const value=(own[kind]&&own[kind][id])||(data.locales.en[kind]&&data.locales.en[kind][id]);
    return value&&typeof value==='object'?value:null;
  }
  function uiText(key){
    const own=localeData()||{},en=(content()||{locales:{en:{}}}).locales.en||{};
    return (own.ui&&own.ui[key])||(en.ui&&en.ui[key])||'';
  }

  function loadSettings(){
    if(!settingsPromise){
      settingsPromise=(async()=>{
        try{
          if(typeof window.api==='function')return await window.api('/api/settings',{timeoutToast:false,redirect401:false});
          const response=await fetch(new URL('api/settings',document.baseURI||location.href).href,{credentials:'same-origin'});
          return response.ok?await response.json():null;
        }catch(_){return null;}
      })();
      settingsPromise.then(value=>{if(!value)settingsPromise=null;});
    }
    return settingsPromise;
  }
  function isAdmin(who){
    if(!who)return false;
    if(who.is_bootstrap_admin)return true;
    const perms=Array.isArray(who.permissions)?who.permissions:[];
    return perms.includes('*')||perms.includes('governance:read')||perms.includes('governance:write');
  }
  // Fail safe: without a readable /api/settings answer no module counts as
  // enabled, so a help item for a module is hidden rather than shown.
  async function gateContext(){
    const settings=await loadSettings();
    const list=settings&&Array.isArray(settings.enabled_modules)?settings.enabled_modules:[];
    const who=me();
    return {
      modules:new Set(list.filter(id=>typeof id==='string')),
      admin:isAdmin(who),
      scope:who&&typeof who.delegated_scope==='string'?who.delegated_scope:'none',
    };
  }
  function allowed(item,ctx){
    if(!item||typeof item!=='object')return false;
    if(item.module!==undefined&&(typeof item.module!=='string'||!ctx.modules.has(item.module)))return false;
    if(item.modules!==undefined){
      if(!Array.isArray(item.modules)||!item.modules.length)return false;
      if(!item.modules.every(id=>typeof id==='string'&&ctx.modules.has(id)))return false;
    }
    if(item.audience!==undefined){
      if(item.audience==='admin'){if(!ctx.admin)return false;}
      else if(item.audience==='org'){if(ctx.scope!=='org')return false;}
      else return false;
    }
    if(item.enabled_if!==undefined&&!isVisible(query(item.enabled_if)))return false;
    return true;
  }
  // A panel this person cannot open: hidden by governance, by the tab
  // settings, by an inactive module, or never revealed (inline display none).
  function panelHidden(panel){
    if(typeof panel!=='string'||!panel)return true;
    if(panel==='chat'||panel==='settings')return false;
    for(const list of [window._govHiddenNav,window._moduleHiddenNav]){
      if(Array.isArray(list)&&list.indexOf(panel)!==-1)return true;
    }
    const navs=Array.from(document.querySelectorAll('.rail [data-panel], .sidebar-nav [data-panel]'))
      .filter(node=>node.dataset.panel===panel);
    if(!navs.length)return false;
    return navs.every(node=>node.hidden||node.classList.contains('nav-tab-hidden')||node.style.display==='none');
  }
  function navTarget(panel){
    const order=isDesktop()?['.rail','.sidebar-nav']:['.sidebar-nav','.rail'];
    for(const root of order){
      const node=Array.from(document.querySelectorAll(root+' [data-panel]')).find(item=>item.dataset.panel===panel);
      if(isVisible(node))return node;
    }
    return null;
  }
  function currentPanel(){
    try{return (typeof _currentPanel==='string'&&_currentPanel)||'chat';}catch(_){return 'chat';}
  }
  function panelView(panel){
    return document.getElementById('panel'+panel.charAt(0).toUpperCase()+panel.slice(1));
  }
  async function goToPanel(panel){
    if(!panel||panel===currentPanel())return true;
    if(panelHidden(panel)||!panelView(panel)||typeof window.switchPanel!=='function')return false;
    try{return (await window.switchPanel(panel))!==false&&currentPanel()===panel;}catch(_){return false;}
  }

  // The page the person is looking at: the active panel, refined into the
  // virtual pages that live on it (group chat, voice, files, bot builder).
  function detectPage(){
    const panel=currentPanel();
    if(panel==='settings'){
      let section='';
      try{section=typeof _currentSettingsSection==='string'?_currentSettingsSection:'';}catch(_){section='';}
      const entry=pick('pages',section);
      return entry&&entry.parent==='settings'?section:'settings';
    }
    if(panel==='profiles'){
      const builder=query('#profileDetailBody [data-builder-tab]');
      return builder&&isVisible(builder)&&pick('pages','bot_builder')?'bot_builder':'profiles';
    }
    if(panel==='chat'){
      if(isVisible(query("[data-tour='realtime-voice-bar']"))&&pick('pages','realtime_voice'))return 'realtime_voice';
      if(isVisible(query("[data-tour='voice-mode-bar']"))&&pick('pages','voice_mode'))return 'voice_mode';
      if(!isDesktop()&&document.documentElement.dataset.workspacePanel==='open'&&pick('pages','files'))return 'files';
      const people=query("[data-tour='chat-people']");
      if(people&&people.classList.contains('is-group')&&pick('pages','group_chat'))return 'group_chat';
      return 'chat';
    }
    return pick('pages',panel)?panel:'chat';
  }

  // Where the step card goes: below the title bar, beside the side columns,
  // above anything in the lower half (the message box) and below the rest.
  function autoPosition(node){
    try{
      const rect=node.getBoundingClientRect();
      const height=window.innerHeight||0;
      if(rect.top<80&&rect.bottom<=90)return 'bottom';
      if(isDesktop()){
        if(node.closest('.rail, .sidebar'))return 'right';
        if(node.closest('.rightpanel'))return 'left';
      }else if(node.closest('.sidebar-nav')){
        return 'right';
      }
      if(height&&rect.top>height*0.55)return 'top';
      return 'bottom';
    }catch(_){return 'bottom';}
  }

  // ── Usage counts (counts only; the server never learns who) ────────────────
  function recordEvent(event,fields){
    try{
      const body=JSON.stringify(Object.assign({event},fields||{}));
      const url=new URL(EVENTS_URL,document.baseURI||location.href).href;
      const pending=fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body,credentials:'same-origin',keepalive:true});
      if(pending&&typeof pending.catch==='function')pending.catch(()=>{});
    }catch(_){}
  }

  function supportAvailable(){
    const support=window.SynthPulseSupport;
    if(!support||typeof support.openTicket!=='function')return false;
    return !panelHidden('support');
  }
  function askSupport(pageId,title){
    closeAll();
    try{window.SynthPulseSupport.openTicket({page:pageId,title:String(title||''),source:'help'});}catch(_){}
  }
  function feedbackRow(pageId,title){
    const row=el('div','sp-help-feedback');
    const thanks=el('span','sp-help-feedback-thanks');
    thanks.setAttribute('role','status');
    const notHelpful=button('sp-help-link sp-help-did-not-help',tr('help_did_not_help'),()=>{
      recordEvent('did_not_help',{page:pageId});
      notHelpful.disabled=true;
      notHelpful.hidden=true;
      thanks.textContent=uiText('feedback_thanks');
    });
    row.appendChild(notHelpful);
    row.appendChild(thanks);
    if(supportAvailable()){
      row.appendChild(button('sp-help-link sp-help-support',tr('help_still_stuck'),()=>askSupport(pageId,title)));
    }
    return row;
  }

  // ── Tours on Guida ─────────────────────────────────────────────────────────
  function guidaClass(){
    const lib=window.SpotlightOnboarding;
    return lib&&typeof lib.Guida==='function'?lib.Guida:null;
  }
  function clearStepMarks(){
    document.querySelectorAll('[data-sp-help-step]').forEach(node=>node.removeAttribute('data-sp-help-step'));
    document.querySelectorAll('[data-sp-help-pos]').forEach(node=>{
      node.style.position=node.getAttribute('data-sp-help-pos')||'';
      node.removeAttribute('data-sp-help-pos');
    });
  }
  // Guida's highlight class makes its target position:relative. A control that
  // is absolute, fixed or sticky keeps its own position while it is lit.
  function keepPosition(node){
    if(!node||node.hasAttribute('data-sp-help-pos'))return;
    const had=node.classList.contains('guida-highlight');
    if(had)node.classList.remove('guida-highlight');
    let position='static';
    try{position=window.getComputedStyle(node).position;}catch(_){}
    if(had)node.classList.add('guida-highlight');
    node.setAttribute('data-sp-help-pos',node.style.position||'');
    if(position==='absolute'||position==='fixed'||position==='sticky')node.style.position=position;
  }

  async function buildTourSteps(id,ctx){
    const data=content();
    if(!data||TOURS.indexOf(id)===-1)return [];
    const own=localeData()||{};
    const ids=(own.tours&&own.tours[id])||(data.locales.en.tours&&data.locales.en.tours[id]);
    if(!Array.isArray(ids))return [];
    ctx=ctx||await gateContext();
    const here=currentPanel();
    const steps=[];
    for(const stepId of ids){
      const step=pick('tour_steps',stepId);
      if(!step||!allowed(step,ctx))continue;
      if(step.page&&panelHidden(step.page))continue;
      let node=null;
      if(typeof step.nav==='string'){
        if(panelHidden(step.nav))continue;
        node=navTarget(step.nav);
        const inMenu=Array.from(document.querySelectorAll('.sidebar-nav [data-panel]')).some(item=>item.dataset.panel===step.nav);
        if(!node&&inMenu){
          // The page is in a closed mobile menu: point at the menu button.
          const menu=query("[data-tour='menu-button']");
          if(isVisible(menu))node=menu;
        }
      }else if(typeof step.target==='string'){
        node=query(step.target);
        // A step on another page is checked again once that page is open.
        if(!node||(!(step.page&&step.page!==here)&&!isVisible(node)))continue;
      }
      if(!node)continue;
      steps.push({id:stepId,title:step.title,body:step.body,node,page:step.page||'',position:step.position||autoPosition(node)});
    }
    return steps;
  }

  function endRun(run){
    if(!run||run.ended)return;
    run.ended=true;
    restoreFeaturePresentation(run);
    if(activeRun===run)activeRun=null;
    window.removeEventListener('keydown',onTourKey,true);
    if(run.blockPointer){document.removeEventListener('pointerdown',run.blockPointer,true);document.removeEventListener('click',run.blockPointer,true);}
    try{run.cleanup.call(run.inst);}catch(_){}
    run.inst.cleanup=function(){};
    clearStepMarks();
    if(run.observer)run.observer.disconnect();
    if(run.frame)cancelAnimationFrame(run.frame);
    if(run.reconcile){document.removeEventListener('transitionend',run.reconcile,true);document.removeEventListener('animationend',run.reconcile,true);}
    if(run.anchor)run.anchor.remove();
    let focusTarget=run.trigger;
    if(run.mode==='feature'&&!focusTarget?.isConnected){
      const appCard=run.appTriggerId&&Array.from(document.querySelectorAll('#mainApps [data-app-id]'))
        .find(node=>node.dataset.appId===run.appTriggerId);
      focusTarget=appCard?.querySelector('button')||document.getElementById('btnPageHelp');
    }
    restoreFocus(focusTarget);
  }

  const FOCUS_KEYS=['sp-help-done','guida-next','guida-prev','guida-close','sp-help-did-not-help','sp-help-support','sp-feature-tour-picker','sp-feature-tour-language'];
  function focusKey(node){
    return FOCUS_KEYS.find(name=>node.classList&&node.classList.contains(name))||'';
  }
  function guideButton(pageId){
    const guide=window.SynthPulseFeatureGuide;
    if(!guide){
      if(typeof window.loadSynthPulseFeatureGuide!=='function')return null;
      return button('sp-help-chip',typeof t==='function'?t('help_browse_features'):'Browse all features',async()=>{
        try{const loaded=await window.loadSynthPulseFeatureGuide();loaded.open({area:pageId,trigger:document.getElementById('btnPageHelp')});}
        catch(_){if(typeof showToast==='function')showToast(t('help_guide_load_failed'));}
      });
    }
    return button('sp-help-chip',guide.contextLabel(),()=>guide.open({area:pageId,trigger:document.getElementById('btnPageHelp')}));
  }

  function decorate(run,keep){
    const inst=run.inst,tip=inst.state&&inst.state.tooltip;
    if(!tip)return;
    run.moving=false;
    const index=inst.state.currentStep,total=inst.config.steps.length,last=index===total-1;
    tip.setAttribute('role','dialog');
    tip.setAttribute('aria-modal','true');
    const heading=tip.querySelector('.guida-header h3');
    if(heading){heading.id=run.uid+'-title';tip.setAttribute('aria-labelledby',heading.id);}
    const body=tip.querySelector('.guida-tooltip-content > p');
    if(body){body.id=run.uid+'-body';tip.setAttribute('aria-describedby',body.id);}
    const progress=tip.querySelector('.guida-progress span');
    if(progress)progress.textContent=tr('help_tour_step_of',index+1,total);
    const prev=tip.querySelector('.guida-prev'),next=tip.querySelector('.guida-next'),close=tip.querySelector('.guida-close');
    if(prev){prev.type='button';prev.textContent=tr('help_tour_back');}
    if(next){next.type='button';next.textContent=tr('help_tour_next');}
    if(close){close.type='button';close.textContent=tr('help_tour_close');}
    let primary=next;
    const controls=tip.querySelector('.guida-controls');
    if(last&&controls){
      const done=button('guida-btn guida-next sp-help-done',tr('help_tour_done'),()=>finish(run));
      controls.appendChild(done);
      primary=done;
    }
    if(run.mode==='explain'&&!tip.querySelector('[data-guide-link]')){
      const link=guideButton(run.id);
      if(link){link.dataset.guideLink='';tip.querySelector('.guida-tooltip-content')?.appendChild(link);}
    }
    if(last&&run.mode==='explain'){
      const holder=tip.querySelector('.guida-tooltip-content');
      if(holder)holder.appendChild(feedbackRow(run.id,run.title));
    }
    if(run.mode==='feature')decorateFeature(run);
    if(run.focusedStep!==index){
      run.focusedStep=index;
      restoreFocus(primary||close);
    }else if(keep){
      restoreFocus(tip.querySelector('.'+keep)||primary||close);
    }
  }

  function finish(run){
    if(run.ended||run.moving)return;
    run.inst.complete();
  }
  function stepForward(run){
    if(run.ended||run.moving)return;
    const inst=run.inst;
    if(inst.state.currentStep>=inst.config.steps.length-1){finish(run);return;}
    inst.nextStep();
  }
  function stepBack(run){
    if(run.ended||run.moving)return;
    if(run.inst.state.currentStep>0)run.inst.previousStep();
  }
  function onTourKey(event){
    const run=activeRun;
    if(!run)return;
    const tip=run.inst.state&&run.inst.state.tooltip;
    const inside=!!(tip&&tip.contains(document.activeElement));
    const idle=!document.activeElement||document.activeElement===document.body;
    if(run.mode==='feature'){
      event.stopImmediatePropagation();
      if(!inside&&!idle&&event.key!=='Escape'){event.preventDefault();restoreFocus(tip?.querySelector('.guida-close'));return;}
    }
    if(event.key==='Escape'){
      event.preventDefault();event.stopImmediatePropagation();
      run.inst.close();
      return;
    }
    if(!inside&&!idle)return;
    if(/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName||'')&&event.key!=='Tab')return;
    if(event.key==='ArrowRight'){event.preventDefault();event.stopImmediatePropagation();stepForward(run);return;}
    if(event.key==='ArrowLeft'){event.preventDefault();event.stopImmediatePropagation();stepBack(run);return;}
    if(event.key==='Tab'&&tip){
      const items=focusable(tip);
      if(!items.length)return;
      const first=items[0],lastItem=items[items.length-1];
      if(event.shiftKey&&(document.activeElement===first||!inside)){event.preventDefault();lastItem.focus();}
      else if(!event.shiftKey&&(document.activeElement===lastItem||!inside)){event.preventDefault();first.focus();}
    }
  }

  // Starts one Guida run over resolved step elements. mode is 'tour' or
  // 'explain'; only tours remember that they were seen.
  function runGuida(mode,id,title,steps,trigger,options){
    options=options||{};
    const Guida=guidaClass();
    if(!Guida||!steps.length)return null;
    clearStepMarks();
    // One element can serve several steps (the mobile menu button), so each
    // step is a token in the attribute and matched with ~=.
    steps.forEach((step,i)=>{
      const before=step.node.getAttribute('data-sp-help-step');
      step.node.setAttribute('data-sp-help-step',before?before+' '+i:String(i));
    });
    let inst;
    try{
      inst=new Guida({
        steps:steps.map((step,i)=>({
          target:'[data-sp-help-step~="'+i+'"]',
          title:escHtml(step.title),
          description:escHtml(step.body),
          position:step.position,
          action:'observe',
          highlight:true,
          skipable:false,
          spotlight:{borderRadius:10,padding:6},
        })),
        storageKey:mode==='tour'?tourStorageKey(id):'synthpulse-explain:unused',
        autoStart:false,
        startDelay:0,
        spotlight:{borderRadius:10,padding:6,backdropOpacity:45},
        customClasses:{overlay:'sp-help-overlay',backdrop:'sp-help-backdrop',tooltip:'sp-help-tooltip',highlight:'sp-help-highlight'},
      });
    }catch(_){clearStepMarks();return null;}
    const run={mode,id,title,steps,inst,trigger,uid:'spHelpTour'+(++uid),focusedStep:-1,moving:false,ended:false,cleanup:inst.cleanup,locale:options.locale||'en',anchor:options.anchor||null};
    if(mode==='feature'){
      run.appTriggerId=trigger?.closest?.('#mainApps [data-app-id]')?.dataset.appId||'';
      const showStep=inst.showStep;
      inst.showStep=function(index){
        if(run.ended||!this.state.isActive)return;
        const step=steps[index];if(!step)return;
        if(run.preparedIndex!==index){
          restoreFeaturePresentation(run);
          run.preparedIndex=index;
          const ticket=run.prepareTicket=(run.prepareTicket||0)+1;
          run.preparing=true;run.navigationBlocked=false;
          // Let the card render first. A close/replacement in this turn cancels
          // preparation before any navigation can happen.
          Promise.resolve().then(()=>prepareFeature(run,index,ticket)).catch(()=>{console.warn('[help] could not reveal this feature');});
        }
        const target=resolveFeatureTarget(step.feature),node=target?.node;
        step.targetKind=target?.kind;step.node=node||run.anchor;step.highlight=!!node;
        clearStepMarks();step.node.setAttribute('data-sp-help-step',String(index));
        this.config.steps[index].title=escHtml(step.feature.locales[run.locale].title);
        this.config.steps[index].description=escHtml(step.feature.locales[run.locale].purpose);
        this.config.steps[index].highlight=!!node;
        this.config.steps[index].position=node?autoPosition(node):'bottom';
        const backdrop=this.state.overlay?.querySelector('.guida-backdrop');if(!node&&backdrop)backdrop.replaceChildren();
        showStep.call(this,index);
      };
      // Guida redraws on every scroll. Do not replace a scrollable explanation
      // while the person is reading it; only reposition the card/spotlight.
      inst.setupResizeHandler=function(){
        const update=event=>{
          if(run.ended||(event.target instanceof Node&&this.state.tooltip?.contains(event.target)))return;
          const step=steps[this.state.currentStep],node=resolveFeatureTarget(step.feature)?.node;
          if((node||null)!== (step.highlight?step.node:null)){this.showStep(this.state.currentStep);return;}
          const backdrop=this.state.overlay?.querySelector('.guida-backdrop');
          if(node&&backdrop)this.updateClipPath(node,backdrop,this.state.currentStepConfig);
          positionFeatureTip(run);
        };
        this.state.resizeHandler=update;this.state.scrollHandler=update;
        window.addEventListener('resize',update);document.addEventListener('scroll',update,true);
        // Panels and picker controls can appear after their async loader or
        // entrance animation. Re-resolve only when the current target changes.
        run.reconcile=()=>{if(!run.frame&&!run.ended)run.frame=requestAnimationFrame(()=>{run.frame=0;update({target:document});});};
        run.observer=new MutationObserver(run.reconcile);
        const layout=document.querySelector('.layout');
        if(layout)run.observer.observe(layout,{subtree:true,childList:true,attributes:true,attributeFilter:['class','style','hidden','aria-hidden','inert','aria-busy']});
        document.addEventListener('transitionend',run.reconcile,true);document.addEventListener('animationend',run.reconcile,true);
      };
    }
    // Storage never throws, and only tours are remembered.
    inst.markAsCompleted=function(){
      this.state.isCompleted=true;
      if(mode==='tour')storeSet(this.config.storageKey,'true');
    };
    inst.isCompleted=function(){return storeGet(this.config.storageKey)==='true';};
    inst.reset=function(){storeRemove(this.config.storageKey);this.state.isCompleted=false;};
    // The emoji completion card is replaced by a normal toast.
    inst.showCompletionMessage=function(){};
    // Guida redraws the card on every scroll and resize; the focused button
    // is found again after each redraw so the keyboard never loses its place.
    const show=inst.showTooltip;
    inst.showTooltip=function(node,step){
      const tip=this.state&&this.state.tooltip,active=document.activeElement;
      const keep=tip&&active&&tip.contains(active)?focusKey(active):'';
      show.call(this,node,step);
      try{decorate(run,keep);}catch(err){console.warn('[help] could not decorate the tour step',err);}
    };
    const highlight=inst.highlightElement;
    inst.highlightElement=function(node,on,step){
      keepPosition(node);
      if(node&&reducedMotion()){
        node.scrollIntoView=function(opts){
          delete node.scrollIntoView;
          Element.prototype.scrollIntoView.call(node,Object.assign({},opts,{behavior:'auto'}));
        };
      }
      try{return highlight.call(this,node,on,step);}
      finally{if(node&&Object.prototype.hasOwnProperty.call(node,'scrollIntoView'))delete node.scrollIntoView;}
    };
    inst.on('stepNavigation',info=>{
      run.moving=true;
      const next=steps[info&&info.toStep];
      if(next&&next.page&&next.page!==currentPanel())goToPanel(next.page);
    });
    inst.on('complete',()=>{
      endRun(run);
      if(mode==='tour'){
        recordEvent('tour_completed',{tour:id});
        toast(tr('help_tour_completed'));
      }
    });
    inst.on('close',()=>endRun(run));
    if(mode==='feature'){
      run.blockPointer=event=>{if(!(event.target instanceof Node)||!inst.state.tooltip?.contains(event.target)){event.preventDefault();event.stopImmediatePropagation();}};
      document.addEventListener('pointerdown',run.blockPointer,true);document.addEventListener('click',run.blockPointer,true);
    }
    activeRun=run;
    window.addEventListener('keydown',onTourKey,true);
    try{inst.start();}catch(err){
      console.warn('[help] the tour could not start',err);
      endRun(run);
      return null;
    }
    if(!inst.isActive()){endRun(run);return null;}
    return run;
  }

  // ── Popover (fallback and text-only pages) ─────────────────────────────────
  function setHelpExpanded(open){
    const anchor=document.getElementById('btnPageHelp');
    if(anchor)anchor.setAttribute('aria-expanded',open?'true':'false');
  }
  function closePopover(restore){
    if(!popover)return;
    const current=popover;
    popover=null;
    setHelpExpanded(false);
    document.removeEventListener('keydown',current.onKey,true);
    document.removeEventListener('mousedown',current.onOutside,true);
    window.removeEventListener('resize',current.onResize);
    current.node.remove();
    if(restore!==false)restoreFocus(current.trigger);
  }
  function placePopover(node){
    const width=window.innerWidth||document.documentElement.clientWidth||0;
    const height=window.innerHeight||document.documentElement.clientHeight||0;
    const anchor=document.getElementById('btnPageHelp');
    const rect=isVisible(anchor)?anchor.getBoundingClientRect():null;
    const top=Math.max(8,Math.round(rect?rect.bottom+8:56));
    node.style.top=top+'px';
    node.style.maxHeight=Math.max(160,height-top-12)+'px';
    if(width<=640){
      node.classList.add('is-narrow');
      node.style.left='8px';node.style.right='8px';
    }else{
      node.classList.remove('is-narrow');
      node.style.left='auto';
      node.style.right=Math.max(8,Math.round(rect?width-rect.right:16))+'px';
    }
  }
  function openPopover(model,trigger){
    closePopover(false);
    const node=el('div','sp-help-popover');
    const titleId='spHelpPopoverTitle'+(++uid);
    node.setAttribute('role','dialog');
    node.setAttribute('aria-modal','false');
    node.setAttribute('aria-labelledby',titleId);
    node.tabIndex=-1;
    node.dataset.helpPage=model.pageId||'';
    const head=el('div','sp-help-popover-head');
    const titles=el('div','sp-help-popover-titles');
    titles.appendChild(el('div','sp-help-popover-eyebrow',model.eyebrow));
    const heading=el('h2','sp-help-popover-title',model.title);
    heading.id=titleId;
    titles.appendChild(heading);
    head.appendChild(titles);
    const closeBtn=button('sp-help-popover-close','',()=>closePopover());
    closeBtn.setAttribute('aria-label',tr('help_tour_close'));
    closeBtn.innerHTML='<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    head.appendChild(closeBtn);
    node.appendChild(head);
    const scroll=el('div','sp-help-popover-body');
    if(model.summary)scroll.appendChild(el('p','sp-help-popover-summary',model.summary));
    if(model.sections&&model.sections.length){
      if(model.sectionsHeading)scroll.appendChild(el('h3','sp-help-popover-subhead',model.sectionsHeading));
      const list=el(model.ordered?'ol':'ul','sp-help-popover-sections');
      model.sections.forEach(section=>{
        const item=el('li','sp-help-popover-section');
        item.appendChild(el('div','sp-help-popover-section-title',section.title));
        item.appendChild(el('p','sp-help-popover-section-body',section.body));
        list.appendChild(item);
      });
      scroll.appendChild(list);
    }
    if(model.related&&model.related.length){
      scroll.appendChild(el('h3','sp-help-popover-subhead',uiText('related_heading')));
      const links=el('div','sp-help-popover-related');
      model.related.forEach(rel=>links.appendChild(button('sp-help-chip',rel.title,()=>explainPage(rel.id,{trigger:model.trigger||trigger}))));
      scroll.appendChild(links);
    }
    node.appendChild(scroll);
    if(model.pageId){
      const link=guideButton(model.pageId);if(link)scroll.appendChild(link);
      node.appendChild(feedbackRow(model.pageId,model.title));
    }
    document.body.appendChild(node);
    placePopover(node);
    const state={node,trigger};
    state.onKey=event=>{
      if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();closePopover();return;}
      if(event.key==='Tab'&&node.contains(document.activeElement)){
        const items=focusable(node);
        if(!items.length)return;
        if(event.shiftKey&&document.activeElement===items[0]){event.preventDefault();items[items.length-1].focus();}
        else if(!event.shiftKey&&document.activeElement===items[items.length-1]){event.preventDefault();items[0].focus();}
      }
    };
    state.onOutside=event=>{
      if(node.contains(event.target))return;
      if(event.target&&event.target.closest&&event.target.closest('#btnPageHelp'))return;
      closePopover(false);
    };
    state.onResize=()=>placePopover(node);
    document.addEventListener('keydown',state.onKey,true);
    document.addEventListener('mousedown',state.onOutside,true);
    window.addEventListener('resize',state.onResize);
    popover=state;
    setHelpExpanded(true);
    try{node.focus({preventScroll:true});}catch(_){}
    return node;
  }

  // Catalogue tours share Guida with the short onboarding tours. One step per
  // feature keeps the complete explanation together; the topic picker gives
  // direct access to every feature without forcing a long linear tour.
  const FEATURE_COPY={
    en:{next:'Next feature',back:'Previous',close:'Close',done:'Done',of:'Feature {0} of {1}',jump:'Choose a feature',language:'Guide language',guide:'Back to feature guide',open:'Open this section',purpose:'What it does',when:'When to use it',steps:'How to use it',permissions:'Access required',limits:'Conditions and limits',example:'Example',available:'Implemented',conditional:'Requires configuration or permissions',planned:'Planned',missing:'To reach this feature:',loading:'Opening the relevant section…',blocked:'Save or close your current editor first. The guide keeps your unsaved changes.',entry:'Start with the highlighted control. Close the guide to use it.',future:'This feature is planned. There is no working control to highlight yet.',msp:'This feature belongs to the MSP cockpit. Here you can follow its explanation; its controls are in that application.',nonvisual:'This is a background capability with no separate control to highlight.',tour:'Interactive feature guide'},
    nl:{next:'Volgende functie',back:'Vorige',close:'Sluiten',done:'Klaar',of:'Functie {0} van {1}',jump:'Kies een functie',language:'Taal van de gids',guide:'Terug naar functiegids',open:'Open dit onderdeel',purpose:'Wat het doet',when:'Wanneer je dit gebruikt',steps:'Zo gebruik je dit',permissions:'Benodigde toegang',limits:'Voorwaarden en grenzen',example:'Voorbeeld',available:'Geïmplementeerd',conditional:'Vereist configuratie of rechten',planned:'Gepland',missing:'Zo bereik je deze functie:',loading:'Het juiste onderdeel wordt geopend…',blocked:'Bewaar of sluit eerst je huidige bewerking. De gids bewaart je niet-opgeslagen wijzigingen.',entry:'Begin bij de gemarkeerde knop. Sluit de gids om die te gebruiken.',future:'Deze functie is gepland. Er is nog geen werkende bediening om aan te wijzen.',msp:'Deze functie hoort bij de MSP-cockpit. Je kunt hier de uitleg doorlopen; de bediening staat in die applicatie.',nonvisual:'Dit is een achtergrondfunctie zonder afzonderlijke bediening om aan te wijzen.',tour:'Interactieve functiegids'},
  };
  function featureText(run,key){return FEATURE_COPY[run.locale||'en'][key];}
  function featureNodes(selectors){
    const nodes=[];
    for(const selector of selectors||[]){try{nodes.push(...document.querySelectorAll(selector));}catch(_){}}
    return nodes;
  }
  function resolveFeatureTarget(feature){
    if(feature.product==='msp'||feature.status==='planned')return null;
    const mapping=(window.SynthPulseFeatureTargets||{})[feature.id];
    if(mapping&&(panelHidden(mapping.panel)||mapping.panel!==currentPanel()))return null;
    if(mapping?.section&&typeof _currentSettingsSection==='string'&&mapping.section!==_currentSettingsSection)return null;
    const targets=mapping?mapping.targets:(feature.anchors||[]).filter(s=>/^#[A-Za-z][\w-]*$/.test(s));
    for(const [kind,selectors] of [['control',targets],['entry',mapping?.entryTargets]]){
      for(const node of featureNodes(selectors)){
        if(isVisible(node)&&!node.closest('[inert]')&&node.type!=='hidden')return {node,kind};
      }
    }
    return null;
  }
  function restoreFeaturePresentation(run){
    for(const undo of (run.presentation||[]).reverse()){try{undo();}catch(_){}}
    run.presentation=[];
  }
  function revealFeatureClass(run,node,name,on){
    if(!node||node.classList.contains(name)===on)return;
    node.classList.toggle(name,on);
    run.presentation.push(()=>{if(node.classList.contains(name)===on)node.classList.toggle(name,!on);});
  }
  function featureNavigationBlocked(mapping){
    const panel=currentPanel(),changingPanel=mapping.panel!==panel;
    const changingSection=panel==='settings'&&mapping.section&&
      typeof _currentSettingsSection==='string'&&mapping.section!==_currentSettingsSection;
    if(!changingPanel&&!changingSection)return false;
    const visible=selector=>featureNodes([selector]).some(isVisible);
    const entered=selector=>featureNodes([selector]).some(node=>!!String(node.value||'').trim());
    if(panel==='settings'){
      if((typeof _settingsDirty!=='undefined'&&_settingsDirty)||(typeof _auxSettingsDirty!=='undefined'&&_auxSettingsDirty))return true;
      // Clarification choices have their own Save action and revision state;
      // entering Preferences again would replace them with server values.
      const saved=typeof _interactionPreferenceState!=='undefined'&&_interactionPreferenceState;
      if(saved&&[['#interactionUserMode',saved.user_mode],['#interactionTaskMode',saved.task_mode||'']]
        .some(([selector,value])=>{const node=query(selector);return node&&!node.disabled&&node.value!==value;}))return true;
    }
    if(!changingPanel)return false;
    // Existing panel loaders can rebuild editors. Do not invalidate a draft
    // just to demonstrate a feature; the person can save or cancel it first.
    if(visible('dialog[open], #profileDetailBody .bot-builder, #profileDetailBody .bot-editor, #profileDetailBody .detail-form, #taskDetailBody .detail-form, #workspaceDetailBody .detail-form, #skillDetailBody .detail-form, #memEditContent, #kanbanTaskModal, #kanbanBoardModal'))return true;
    if(panel==='projects'){
      if(entered('#projNewName, #projNewCategory'))return true;
      const project=typeof _projDetail!=='undefined'&&_projDetail?.project;
      if(project){
        const category=query('#projCategory');
        if(category&&!category.disabled&&category.value!==String(project.category||''))return true;
        for(const [selector,values] of [['#projHumanChoices input',project.members||[]],['#projBotChoices input',project.bot_participants||[]]]){
          if(featureNodes([selector]).some(node=>!node.disabled&&node.checked!==values.includes(node.value)))return true;
        }
      }
    }
    if(panel==='integrations'&&entered('#chAddUserId, #chAddPerson, #chMineOtherId, [data-mine-id], [data-mine-field]'))return true;
    return panel==='kanban'&&entered('#kanbanCommentInput, #kanbanDependencyInput');
  }
  async function prepareFeature(run,index,ticket){
    const current=()=>!run.ended&&run.prepareTicket===ticket&&run.inst.state.currentStep===index;
    if(!current())return;
    const feature=run.steps[index].feature,mapping=(window.SynthPulseFeatureTargets||{})[feature.id];
    try{
      if(feature.product==='msp'||feature.status==='planned'||!mapping||panelHidden(mapping.panel))return;
      if(!mapping.targets?.length&&!mapping.entryTargets?.length)return;
      if(featureNavigationBlocked(mapping)){run.navigationBlocked=true;return;}
      if(!(await goToPanel(mapping.panel))||!current())return;
      if(mapping.section&&typeof window.switchSettingsSection==='function'){
        if(typeof _currentSettingsSection!=='string'||_currentSettingsSection!==mapping.section)window.switchSettingsSection(mapping.section);
      }
      if(!current())return;
      if(mapping.panel==='apps'&&feature.id.startsWith('webui.apps.')&&typeof window.SynthPulseApps?.reveal==='function'){
        const restore=await window.SynthPulseApps.reveal(feature.id.slice('webui.apps.'.length));
        if(typeof restore==='function'){
          if(current())run.presentation.push(restore);else restore();
        }
        if(!current())return;
      }
      // Reveal only presentation containers, never execute feature buttons or
      // open forms. Remember our changes and restore them on step change/close.
      const candidates=featureNodes([...(mapping.targets||[]),...(mapping.entryTargets||[])]);
      for(const node of candidates){
        for(let parent=node.parentElement;parent;parent=parent.parentElement){
          if(parent.tagName==='DETAILS'&&!parent.open){parent.open=true;run.presentation.push(()=>{if(parent.isConnected)parent.open=false;});}
        }
      }
      if(candidates.some(node=>node.closest('.sidebar')&&!isVisible(node))){
        if(isDesktop())revealFeatureClass(run,document.querySelector('.layout'),'sidebar-collapsed',false);
        else{
          const sidebar=document.querySelector('.sidebar');
          revealFeatureClass(run,sidebar,'mobile-session-page',false);
          revealFeatureClass(run,sidebar,'mobile-panel-drawer',true);
          revealFeatureClass(run,sidebar,'mobile-open',true);
        }
      }
    }finally{
      if(current()){run.preparing=false;run.inst.showStep(index);}
    }
  }
  function positionFeatureTip(run){
    const tip=run.inst.state.tooltip;if(!tip)return;
    const width=window.innerWidth,height=window.innerHeight;
    tip.style.maxHeight=Math.max(100,height-24)+'px';
    const box=tip.getBoundingClientRect(),step=run.steps[run.inst.state.currentStep];
    const target=step&&step.node?.getBoundingClientRect();
    let left=(width-box.width)/2,top=(height-box.height)/2;
    if(step?.highlight&&target){
      left=target.left+target.width/2-box.width/2;
      top=target.top>height/2?target.top-box.height-16:target.bottom+16;
    }
    tip.style.left=Math.max(12,Math.min(left,width-box.width-12))+'px';
    tip.style.top=Math.max(12,Math.min(top,height-box.height-12))+'px';
  }
  function decorateFeature(run){
    const inst=run.inst,tip=inst.state.tooltip,step=run.steps[inst.state.currentStep];
    if(!tip||!step)return;
    const f=step.feature,copy=f.locales[run.locale];
    tip.classList.add('sp-feature-tour');tip.lang=run.locale;tip.dataset.feature=f.id;
    tip.querySelector('.guida-header h3').textContent=copy.title;
    tip.querySelector('.guida-progress span').textContent=featureText(run,'of').replace('{0}',inst.state.currentStep+1).replace('{1}',run.steps.length);
    const container=tip.querySelector('.guida-tooltip-content');
    const intro=container.querySelector(':scope > p');
    const article=el('div','sp-feature-tour-detail');article.id=run.uid+'-body';
    const status=el('p','sp-feature-tour-status',featureText(run,f.status));article.append(status);
    const mapping=(window.SynthPulseFeatureTargets||{})[f.id];
    if(step.targetKind==='entry'){
      article.append(el('p','sp-feature-tour-note',mapping?.entryHint?.[run.locale]||featureText(run,'entry')));
    }else if(!step.highlight){
      const reason=f.status==='planned'?'future':f.product==='msp'?'msp':run.navigationBlocked?'blocked':run.preparing?'loading':'missing';
      const message=featureText(run,reason)+(reason==='missing'?' '+copy.steps[0]:'');
      article.append(el('p','sp-feature-tour-note',message));
    }
    for(const field of ['purpose','when','steps','permissions','limits','example']){
      const section=el('section');section.append(el('h4','',featureText(run,field)));
      if(field==='steps'){const list=el('ol');copy.steps.forEach(value=>list.append(el('li','',value)));section.append(list);}
      else section.append(el('p','',copy[field]));
      article.append(section);
    }
    intro.replaceWith(article);tip.setAttribute('aria-describedby',article.id);
    const tools=el('div','sp-feature-tour-tools');
    const pickerLabel=el('label','',featureText(run,'jump')),picker=el('select','sp-feature-tour-picker');
    run.steps.forEach((entry,index)=>{const opt=el('option','',String(index+1)+'. '+entry.feature.locales[run.locale].title);opt.value=String(index);picker.append(opt);});
    picker.value=String(inst.state.currentStep);picker.setAttribute('aria-label',featureText(run,'jump'));
    picker.addEventListener('change',()=>{if(!run.ended){inst.state.currentStep=Number(picker.value);inst.showStep(inst.state.currentStep);}});
    pickerLabel.append(picker);tools.append(pickerLabel);
    const langLabel=el('label','',featureText(run,'language')),lang=el('select','sp-feature-tour-language');
    for(const [value,label] of [['en','English'],['nl','Nederlands']]){const opt=el('option','',label);opt.value=value;lang.append(opt);}
    lang.value=run.locale;lang.setAttribute('aria-label',featureText(run,'language'));
    lang.addEventListener('change',()=>{run.locale=lang.value;storeSet('sp-feature-guide-language',run.locale);window.SynthPulseFeatureGuide.setLanguage(run.locale);inst.showStep(inst.state.currentStep);restoreFocus(inst.state.tooltip.querySelector('.sp-feature-tour-language'));});
    langLabel.append(lang);tools.append(langLabel);container.insertBefore(tools,article);
    const next=tip.querySelector('.guida-next'),prev=tip.querySelector('.guida-prev'),close=tip.querySelector('.guida-close');
    if(next)next.textContent=featureText(run,next.classList.contains('sp-help-done')?'done':'next');
    if(prev)prev.textContent=featureText(run,'back');if(close)close.textContent=featureText(run,'close');
    const links=el('div','sp-feature-tour-links');
    links.append(button('sp-help-chip',featureText(run,'guide'),()=>{const locale=run.locale;closeAll();window.SynthPulseFeatureGuide.open({feature:f.id,locale});}));
    // Navigation now follows the selected topic. A blocked editor is left
    // intact; closing the guide lets the person finish or cancel that edit.
    container.append(links);positionFeatureTip(run);
  }
  function startFeatureTour(ids,opts){
    opts=opts||{};const guide=window.SynthPulseFeatureGuide;
    if(!Array.isArray(ids)||!ids.length||!guidaClass())return false;
    if(!guide){
      if(typeof window.loadSynthPulseFeatureGuide!=='function')return false;
      const generation=++featureLoadGeneration,panel=currentPanel();
      return window.loadSynthPulseFeatureGuide().then(()=>{
        if(generation!==featureLoadGeneration||currentPanel()!==panel||opts.trigger?.isConnected===false)return false;
        return startFeatureTour(ids,opts);
      }).catch(()=>{if(generation===featureLoadGeneration&&typeof showToast==='function')showToast(tr('help_guide_load_failed'));return false;});
    }
    const features=[...new Set(ids)].map(id=>guide.getFeature(id)).filter(Boolean);
    if(!features.length&&opts.fallbackId){const fallback=guide.getFeature(opts.fallbackId);if(fallback)features.push(fallback);}
    if(!features.length)return false;
    closeAll();guide.close();closeCard();
    const locale=opts.locale==='nl'||opts.locale==='en'?opts.locale:guide.language();
    const anchor=el('span');anchor.id='spFeatureTourAnchor';anchor.setAttribute('aria-hidden','true');
    anchor.style.cssText='position:fixed;top:45%;left:50%;width:1px;height:1px;pointer-events:none';document.body.append(anchor);
    const steps=features.map(f=>({id:f.id,title:f.locales[locale].title,body:f.locales[locale].purpose,node:anchor,position:'bottom',feature:f,highlight:false}));
    const run=runGuida('feature','features',FEATURE_COPY[locale].tour,steps,opts.trigger||document.getElementById('btnPageHelp'),{locale,anchor});
    if(!run)anchor.remove();
    return !!run;
  }

  // ── Public actions ─────────────────────────────────────────────────────────
  function closeAll(){
    ++featureLoadGeneration;
    if(activeRun){try{activeRun.inst.close();}catch(_){endRun(activeRun);}}
    closePopover(false);
  }
  function hasSeen(id){return typeof id==='string'&&storeGet(tourStorageKey(id))==='true';}

  async function stepsFor(id){
    const steps=await buildTourSteps(id);
    return steps.map(step=>step.id);
  }

  async function startTour(id,opts){
    opts=opts||{};
    id=typeof id==='string'&&id?id:'first_run';
    const trigger=opts.trigger||document.activeElement;
    closeAll();
    if(TOURS.indexOf(id)===-1||!content())return false;
    const ctx=await gateContext();
    // Tours start on the page most of their steps are on.
    const own=localeData()||{};
    const ids=(own.tours&&own.tours[id])||[];
    const pages=new Set(ids.map(stepId=>pick('tour_steps',stepId)).filter(step=>step&&step.page&&allowed(step,ctx)).map(step=>step.page));
    if(pages.size===1){
      const page=Array.from(pages)[0];
      if(page!==currentPanel()&&!panelHidden(page)&&panelView(page)&&!(await goToPanel(page)))return false;
    }
    const steps=await buildTourSteps(id,ctx);
    if(!steps.length)return false;
    const title=tr(id==='org_admin'?'help_tour_org_admin':'help_tour_first_run');
    recordEvent('tour_started',{tour:id});
    if(runGuida('tour',id,title,steps,trigger))return true;
    // Without Guida the steps are listed in a popover.
    openPopover({eyebrow:tr('help_explain_fallback_title'),title,summary:'',ordered:true,
      sections:steps.map(step=>({title:step.title,body:step.body})),related:[],pageId:''},trigger);
    storeSet(tourStorageKey(id),'true');
    return true;
  }

  async function explainPage(id,opts){
    opts=opts||{};
    const trigger=opts.trigger||document.activeElement;
    const asked=typeof id==='string'&&ID_RE.test(id)&&pick('pages',id)?id:'';
    closeAll();
    if(!content())return false;
    const ctx=await gateContext();
    let pageId=asked||detectPage();
    let entry=pick('pages',pageId);
    const parentOf=entryValue=>(entryValue&&typeof entryValue.parent==='string'&&entryValue.parent)||'';
    if(!entry||!allowed(entry,ctx)||panelHidden(parentOf(entry)||pageId)){
      pageId=detectPage();
      entry=pick('pages',pageId);
      if(!entry||!allowed(entry,ctx)){pageId='chat';entry=pick('pages','chat');}
    }
    if(!entry)return false;
    recordEvent('explain_clicked',{page:pageId});
    const panel=parentOf(entry)||pageId;
    if(asked&&panel!==currentPanel()&&panelView(panel))await goToPanel(panel);
    // Page help is an orientation, not the complete reference catalog. Keep the
    // catalog lazy; guideButton opens all topics when someone asks for depth.
    const sections=(Array.isArray(entry.sections)?entry.sections:[]).filter(section=>allowed(section,ctx));
    const visible=sections.filter(section=>typeof section.target==='string'&&isVisible(query(section.target))).slice(0,5);
    if(visible.length&&guidaClass()){
      const anchor=document.getElementById('btnPageHelp');
      const first=isVisible(anchor)?anchor:query(visible[0].target);
      const steps=[{id:'summary',title:entry.title,body:entry.summary,node:first,position:autoPosition(first)}]
        .concat(visible.map(section=>{
          const node=query(section.target);
          return {id:section.id,title:section.title,body:section.body,node,position:section.position||autoPosition(node)};
        }));
      if(runGuida('explain',pageId,entry.title,steps,trigger))return true;
    }
    const related=(Array.isArray(entry.related)?entry.related:[])
      .map(relId=>({id:relId,entry:pick('pages',relId)}))
      .filter(rel=>rel.entry&&allowed(rel.entry,ctx)&&!panelHidden(parentOf(rel.entry)||rel.id))
      .map(rel=>({id:rel.id,title:rel.entry.title}));
    openPopover({eyebrow:tr('help_explain_fallback_title'),title:entry.title,summary:entry.summary,
      sectionsHeading:sections.length?uiText('sections_heading'):'',
      sections:sections.map(section=>({title:section.title,body:section.body})),
      related,pageId,trigger},trigger);
    return true;
  }

  // ── Cards: What's new and the one-time tour offer ──────────────────────────
  function versionTuple(value){
    const match=/(\d+)\.(\d+)\.(\d+)/.exec(String(value||''));
    return match?[Number(match[1]),Number(match[2]),Number(match[3])]:null;
  }
  function compare(a,b){
    for(let i=0;i<3;i++){if(a[i]!==b[i])return a[i]<b[i]?-1:1;}
    return 0;
  }
  function currentVersion(){return versionTuple(window.__HERMES_WEBUI_BUNDLE_VERSION__);}
  function closeCard(){
    const card=document.getElementById('spHelpCard');
    if(card)card.remove();
  }
  document.addEventListener("input",event=>{
    const card=document.getElementById("spHelpCard");
    if(event.target&&card&&card.dataset.kind==="tour_offer"&&!card.contains(event.target))closeCard();
  });
  function showCard(kind,title,build){
    closeCard();
    const card=el('section','sp-help-card');
    card.id='spHelpCard';
    card.dataset.kind=kind;
    const titleId='spHelpCardTitle'+(++uid);
    card.setAttribute('role','region');
    card.setAttribute('aria-labelledby',titleId);
    const heading=el('h2','sp-help-card-title',title);
    heading.id=titleId;
    card.appendChild(heading);
    build(card);
    document.body.appendChild(card);
    return card;
  }
  function whatsNewEntries(since,current){
    const own=localeData()||{};
    const map=own.whats_new&&typeof own.whats_new==='object'?own.whats_new:{};
    return Object.keys(map)
      .map(key=>({key,version:versionTuple(key),items:Array.isArray(map[key])?map[key]:[]}))
      .filter(row=>row.version&&row.items.length&&compare(row.version,current)<=0&&(!since||compare(row.version,since)>0))
      .sort((a,b)=>compare(b.version,a.version));
  }
  // Shown once per person after an upgrade (the first visit only records the
  // version). opts.force shows the newest entry again, for the link in the
  // monthly report (#whats-new).
  function showWhatsNew(opts){
    opts=opts||{};
    const current=currentVersion();
    if(!current||!content())return false;
    const key=prefKey('whats_new');
    let rows;
    if(opts.force){
      rows=whatsNewEntries(null,current).slice(0,1);
    }else{
      const seen=versionTuple(storeGet(key));
      storeSet(key,current.join('.'));
      if(!seen||compare(current,seen)<=0)return false;
      rows=whatsNewEntries(seen,current).slice(0,3);
    }
    if(!rows.length)return false;
    showCard('whats_new',tr('help_whats_new_title'),card=>{
      const list=el('ul','sp-help-card-list');
      rows.forEach(row=>row.items.forEach(item=>{
        if(!item||typeof item!=='object')return;
        const li=el('li','sp-help-card-item');
        li.appendChild(el('div','sp-help-card-item-title',item.title));
        li.appendChild(el('p','sp-help-card-item-body',item.body));
        list.appendChild(li);
      }));
      card.appendChild(list);
      const actions=el('div','sp-help-card-actions');
      actions.appendChild(button('sp-help-btn sp-help-btn-primary',tr('help_whats_new_dismiss'),closeCard));
      card.appendChild(actions);
    });
    return true;
  }
  function offerTour(){
    const key=prefKey('tour_offer');
    if(storeGet(key)||hasSeen('first_run'))return false;
    if(!storeSet(key,'shown'))return false;
    showCard('tour_offer',tr('help_tour_first_run'),card=>{
      card.appendChild(el('p','sp-help-card-text',tr('help_tour_offer')));
      const actions=el('div','sp-help-card-actions');
      actions.appendChild(button('sp-help-btn',tr('help_tour_offer_no'),closeCard));
      actions.appendChild(button('sp-help-btn sp-help-btn-primary',tr('help_tour_offer_yes'),event=>{
        closeCard();
        startTour('first_run',{trigger:event.currentTarget});
      }));
      card.appendChild(actions);
    });
    return true;
  }
  function onboardingOpen(){
    const overlay=document.getElementById('onboardingOverlay');
    return !!(overlay&&overlay.style.display!=='none'&&isVisible(overlay));
  }
  async function waitForIdentity(limitMs){
    const until=Date.now()+limitMs;
    while(window.__GOV_ME__===undefined&&Date.now()<until){
      await new Promise(resolve=>setTimeout(resolve,150));
    }
  }
  async function afterBoot(){
    await waitForIdentity(3000);
    refreshSettingsCards();
    // A split view pane is a second copy of the app: cards show in the main one.
    if(document.documentElement.dataset.pane==='1')return;
    if((location.hash||'')==='#whats-new'){
      showWhatsNew({force:true});
      try{history.replaceState(history.state,'',location.pathname+location.search);}catch(_){}
      return;
    }
    if(onboardingOpen())return;
    if(showWhatsNew())return;
    const settings=await loadSettings();
    if(settings&&settings.onboarding_completed===true&&!onboardingOpen())offerTour();
  }

  async function refreshSettingsCards(){
    const orgButton=document.getElementById('helpCardStartOrgTour');
    if(!orgButton)return;
    let ids=[];
    try{ids=(me()&&me().delegated_scope==='org')?await stepsFor('org_admin'):[];}catch(_){ids=[];}
    orgButton.hidden=!ids.length;
  }

  // ── Wiring ─────────────────────────────────────────────────────────────────
  document.addEventListener('click',event=>{
    const target=event.target&&event.target.closest?event.target:null;
    if(!target)return;
    // Remember the page someone leaves for Settings, so Settings, Help can
    // explain that page instead of Settings itself.
    const nav=target.closest('[data-panel]');
    if(nav&&nav.dataset.panel==='settings'&&currentPanel()!=='settings')pageBeforeSettings=detectPage();
    if(target.closest('[data-settings-section="help"]'))refreshSettingsCards();
    const helpButton=target.closest('#btnPageHelp');
    if(helpButton){
      event.preventDefault();
      if(popover){closePopover();return;}
      explainPage(null,{trigger:helpButton});
      return;
    }
    const action=target.closest('[data-help-action]');
    if(!action)return;
    event.preventDefault();
    if(action.dataset.helpAction==='tour'){
      startTour(action.dataset.helpTour||'first_run',{trigger:action});
    }else if(action.dataset.helpAction==='explain'){
      const page=currentPanel()==='settings'&&pageBeforeSettings?pageBeforeSettings:null;
      explainPage(page,{trigger:action});
    }
  },true);

  window.SynthPulseHelp=Object.freeze({
    startTour,
    startFeatureTour,
    explainPage,
    hasSeen,
    showWhatsNew,
    closeAll,
    stepsFor,
    currentPage:detectPage,
  });

  if(typeof window.runAfterBootReady==='function'){
    window.runAfterBootReady(()=>{afterBoot();},8000);
  }else{
    window.addEventListener('synpulse:boot-ready',()=>{afterBoot();},{once:true});
  }
})();
