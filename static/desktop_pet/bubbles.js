(function(){
  const SESSION_VIEWED_COUNTS_KEY='hermes-session-viewed-counts';
  const SESSION_COMPLETION_UNREAD_KEY='hermes-session-completion-unread';
  const DISMISSED_KEY='hermes-pet-dismissed';
  const COLLAPSED_KEY='hermes-pet-collapsed';
  const COLLAPSE_EXPLICIT_KEY='hermes-pet-collapsed-explicit';
  const SKIN_KEY='hermes-pet-skin';
  const INSTALL_SEEN_KEY='hermes-pet-install-seen';
  const WELCOME_SEEN_KEY='hermes-pet-welcome-seen';
  const WELCOME_SECONDS=30;
  const WELCOME_IDLE_GRACE_MS=2600;
  const POLL_MS=2500;
  const BUBBLE_WINDOW={width:320,height:300};
  const INSTALL_WINDOW={width:320,height:300};
  const TOAST_WINDOW={width:320,height:92};
  const BUBBLE_SIDE_INSET=10;
  const BUBBLE_GAP=8;
  const BUBBLE_BOTTOM_INSET=0;
  const BUBBLE_MAX_VISIBLE_CARDS=2.7;
  const BUBBLE_MIN_HEIGHT=76;
  const PET_RAISE_REQUESTED_EVENT='pet-raise-requested';
  const DEFAULT_PET_LAYOUT={columns:8,rows:9,frameWidth:192,frameHeight:208,states:[{name:'idle',row:0,frames:6},{name:'running-right',row:1,frames:8},{name:'running-left',row:2,frames:8},{name:'waving',row:3,frames:4},{name:'jumping',row:4,frames:5},{name:'failed',row:5,frames:8},{name:'waiting',row:6,frames:6},{name:'running',row:7,frames:6},{name:'review',row:8,frames:6}]};
  const bubbles=document.getElementById('petBubbles');
  const install=document.getElementById('petInstall');
  const installSprite=document.getElementById('petInstallSprite');
  const installTitle=document.getElementById('petInstallTitle');
  const installStatus=document.getElementById('petInstallStatus');
  const readyToast=document.getElementById('petReadyToast');
  const welcome=document.getElementById('petWelcome');
  const welcomeCountdown=document.getElementById('petWelcomeCountdown');
  const welcomeAction=document.getElementById('petWelcomeAction');
  let sessions=[], dismissed=_readJson(DISMISSED_KEY,{}), replySid='', replyText='', replyPendingSid='', replyError='';
  let bubbleScrollTop=0, latestPetLayout=null, visibleMode='hidden', layoutSeq=0;
  let petSkins=[{id:'keeper',displayName:'May',spritesheetUrl:'/static/pets/keeper/spritesheet.webp',layout:DEFAULT_PET_LAYOUT}];
  let activeSkinId=localStorage.getItem(SKIN_KEY)||'keeper';
  let bubbleContentHeightCache=BUBBLE_WINDOW.height-BUBBLE_BOTTOM_INSET;
  let bubbleContentHeightDirty=true;
  let bubbleWindowSyncFrame=0;
  let bubbleWindowSyncMode=null;
  let bubbleWindowCache={mode:'',logicalWidth:0,logicalHeight:0,x:0,y:0};
  let pendingBubblePosition=null;
  let bubblePositionInFlight=false;
  let pendingActionResponses={}, clarifyDrafts={}, clarifyOtherKey='';
  let expandedActionKey='';
  let expandCollapseTimer=0;
  let lastRenderedSignature='';
  let openingSid='';
  let welcomeTimer=0;
  let welcomeDelayTimer=0;
  let welcomeSecondsRemaining=0;

  function _petT(key,...args){return typeof t==='function'?t(key,...args):key;}
  function _readJson(key,fallback){try{const parsed=JSON.parse(localStorage.getItem(key)||'null');return parsed&&typeof parsed==='object'?parsed:fallback;}catch(_){return fallback;}}
  function _writeJson(key,value){try{localStorage.setItem(key,JSON.stringify(value));}catch(_){}}
  function _esc(value){return String(value||'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));}
  function _clean(value){return String(value||'').replace(/\s+/g,' ').trim();}
  function _localizeStaticLabels(){
    if(typeof applyLocaleToDOM==='function') applyLocaleToDOM();
    document.title=_petT('desktop_pet_title');
  }
  function _normalizeSkinLayout(layout){
    if(!layout||typeof layout!=='object') return DEFAULT_PET_LAYOUT;
    const columns=Number(layout.columns),rows=Number(layout.rows),frameWidth=Number(layout.frameWidth),frameHeight=Number(layout.frameHeight);
    const states=Array.isArray(layout.states)?layout.states.map(item=>({name:String(item&&item.name||'').trim(),row:Number(item&&item.row),frames:Number(item&&item.frames)})).filter(item=>/^[A-Za-z0-9_-]+$/.test(item.name)&&Number.isInteger(item.row)&&Number.isInteger(item.frames)):[];
    const required=DEFAULT_PET_LAYOUT.states.map(item=>item.name);
    if(!Number.isInteger(columns)||!Number.isInteger(rows)||!Number.isInteger(frameWidth)||!Number.isInteger(frameHeight)||columns<1||rows<1||frameWidth<1||frameHeight<1) return DEFAULT_PET_LAYOUT;
    if(!required.every(name=>states.some(item=>item.name===name))) return DEFAULT_PET_LAYOUT;
    return {columns,rows,frameWidth,frameHeight,states};
  }
  function _safeSkin(skin){
    if(!skin||typeof skin!=='object') return null;
    const id=String(skin.id||'').trim();
    const displayName=String(skin.displayName||id).trim()||id;
    const spritesheetUrl=String(skin.spritesheetUrl||'').trim();
    if(!/^[A-Za-z0-9_-]+$/.test(id)||!spritesheetUrl) return null;
    return {id,displayName,spritesheetUrl,layout:_normalizeSkinLayout(skin.layout)};
  }
  function _applyPetSkin(skinId){
    const next=petSkins.find(skin=>skin.id===skinId)||petSkins[0];
    if(!next) return;
    activeSkinId=next.id;
    const layout=_normalizeSkinLayout(next.layout);
    if(installSprite){installSprite.style.backgroundImage=`url("${next.spritesheetUrl}")`;installSprite.style.backgroundSize=`${layout.columns*100}% ${layout.rows*100}%`;}
  }
  async function _loadPetSkins(){
    try{
      const data=await fetch('/api/pet/skins',{cache:'no-store'}).then(res=>{if(!res.ok) throw new Error(`Pet skins failed: ${res.status}`);return res.json();});
      const skins=(Array.isArray(data.skins)?data.skins:[]).map(_safeSkin).filter(Boolean);
      if(skins.length) petSkins=skins;
      _applyPetSkin(activeSkinId);
      return true;
    }catch(err){console.warn('Failed to load pet skins',err);_applyPetSkin(activeSkinId);return false;}
  }
  async function _listenPetSkinChanges(){
    const tauri=window.__TAURI__;
    if(!tauri||!tauri.event||typeof tauri.event.listen!=='function') return;
    try{await tauri.event.listen('pet-skin-change',event=>_applyPetSkin(String(event.payload||'')));}catch(err){console.warn('Failed to listen for pet skin changes',err);}
  }
  function _setInstallStatus(statusKey){
    if(installTitle) installTitle.textContent=_petT('desktop_pet_install_title');
    if(installStatus) installStatus.textContent=_petT(statusKey);
  }
  function _isInstallVisible(){return !!(install&&!install.hidden);}
  function _isToastVisible(){return !!(readyToast&&!readyToast.hidden);}
  function _isWelcomeVisible(){return !!(welcome&&!welcome.hidden);}
  function _hasVisibleAttention(){
    const count=_attentionItems().length;
    const collapsed=localStorage.getItem(COLLAPSED_KEY)==='true' && localStorage.getItem(COLLAPSE_EXPLICIT_KEY)==='1';
    return count&&!collapsed;
  }
  function _stopWelcomeTimer(){
    if(welcomeTimer){clearInterval(welcomeTimer);welcomeTimer=0;}
  }
  function _stopWelcomeDelay(){
    if(welcomeDelayTimer){clearTimeout(welcomeDelayTimer);welcomeDelayTimer=0;}
  }
  function _setWelcomeCountdown(seconds){
    if(welcomeCountdown) welcomeCountdown.textContent=_petT('desktop_pet_welcome_countdown',seconds);
  }
  function _hideStartupMessagesForAttention(){
    _stopWelcomeDelay();
    if(install&&!install.hidden) install.hidden=true;
    if(readyToast&&!readyToast.hidden) readyToast.hidden=true;
    if(welcome&&!welcome.hidden) _hideWelcome(false);
    else _stopWelcomeTimer();
  }
  function _hideWelcome(markSeen){
    if(welcome) welcome.hidden=true;
    _stopWelcomeDelay();
    _stopWelcomeTimer();
    if(markSeen) try{localStorage.setItem(WELCOME_SEEN_KEY,'1');}catch(_){}
    _scheduleBubbleSync();
  }
  function _scheduleWelcomeBubble(){
    if(!welcome||localStorage.getItem(WELCOME_SEEN_KEY)==='1'||_hasVisibleAttention()) return;
    _stopWelcomeDelay();
    welcomeDelayTimer=setTimeout(()=>{
      welcomeDelayTimer=0;
      _showWelcomeBubble();
    },WELCOME_IDLE_GRACE_MS);
  }
  function _showWelcomeBubble(){
    _stopWelcomeDelay();
    if(!welcome||localStorage.getItem(WELCOME_SEEN_KEY)==='1'||_hasVisibleAttention()) return;
    welcome.hidden=false;
    welcomeSecondsRemaining=WELCOME_SECONDS;
    _setWelcomeCountdown(welcomeSecondsRemaining);
    _scheduleBubbleSync('welcome');
    _stopWelcomeTimer();
    welcomeTimer=setInterval(()=>{
      welcomeSecondsRemaining-=1;
      if(welcomeSecondsRemaining<=0){_hideWelcome(true);return;}
      _setWelcomeCountdown(welcomeSecondsRemaining);
    },1000);
  }
  function _hideInstall(){if(install) install.hidden=true;render(true);}
  function _showReadyToast(){
    if(!readyToast) return;
    if(_hasVisibleAttention()){
      readyToast.hidden=true;
      _scheduleBubbleSync();
      return;
    }
    readyToast.textContent=_petT('desktop_pet_ready_toast');
    readyToast.hidden=false;
    _scheduleBubbleSync();
    setTimeout(()=>{readyToast.hidden=true;_scheduleBubbleSync();},5200);
  }
  function _runFirstStartInstall(startupPromises){
    if(!install) return;
    if(localStorage.getItem(INSTALL_SEEN_KEY)==='1'){
      _hideInstall();
      _scheduleWelcomeBubble();
      return;
    }
    _setInstallStatus('desktop_pet_install_check_webui');
    setTimeout(()=>_setInstallStatus('desktop_pet_install_load_skins'),520);
    Promise.all(startupPromises).then(()=>{
      _setInstallStatus('desktop_pet_install_ready');
      install.classList.add('is-ready');
      try{localStorage.setItem(INSTALL_SEEN_KEY,'1');}catch(_){}
      _hideInstall();
      _scheduleWelcomeBubble();
    }).catch(err=>{
      console.warn('Desktop pet startup check failed',err);
      _setInstallStatus('settings_desktop_pet_start_failed');
    });
  }
  function _attentionQuery(){
    const params=new URLSearchParams();
    try{params.set('viewed_counts',localStorage.getItem(SESSION_VIEWED_COUNTS_KEY)||'{}');}catch(_){params.set('viewed_counts','{}');}
    try{params.set('completion_unread',localStorage.getItem(SESSION_COMPLETION_UNREAD_KEY)||'{}');}catch(_){params.set('completion_unread','{}');}
    const query=params.toString();
    return query?`?${query}`:'';
  }
  function _dismissKeyForRow(row,status){
    const sid=String(row&&row.session_id||'');
    if(status==='action_required') return `action_required:${String(row&&row.action_required_key||sid).trim()}`;
    if(status==='ready') return `${sid}:ready:${Number(row&&row.message_count||0)}`;
    return `${sid}:${status}`;
  }
  function _formatElapsed(epochSeconds){
    const seconds=Math.max(0,Math.floor(Date.now()/1000-epochSeconds));
    if(seconds<60) return `${seconds}s`;
    const minutes=Math.floor(seconds/60);
    if(minutes<60) return `${minutes}m ${String(seconds%60).padStart(2,'0')}s`;
    const hours=Math.floor(minutes/60);
    return `${hours}h ${String(minutes%60).padStart(2,'0')}m`;
  }
  function _timeAgo(epochSeconds){
    const seconds=Math.max(0,Math.floor(Date.now()/1000-epochSeconds));
    if(seconds<60) return _petT('desktop_pet_time_just_now');
    const minutes=Math.floor(seconds/60);
    if(minutes<60) return _petT('desktop_pet_time_minutes_ago',minutes);
    const hours=Math.floor(minutes/60);
    if(hours<24) return _petT('desktop_pet_time_hours_ago',hours);
    const days=Math.floor(hours/24);
    return _petT('desktop_pet_time_days_ago',days);
  }
  function _readyMetaText(row){
    const count=Number(row&&row.message_count||0);
    const msgPart=count>0?_petT('desktop_pet_ready_meta_messages',count):'';
    const ts=Number(row&&row.last_message_at||row&&row.updated_at||0);
    const agoPart=ts>0?_timeAgo(ts):'';
    const parts=[_petT('desktop_pet_ready_meta_completed')];
    if(msgPart) parts.push(msgPart);
    if(agoPart) parts.push(agoPart);
    return parts.join(' · ');
  }
  function _titleHtml(item){
    const startedAt=Number(item&&item.started_at||0);
    if(item&&item.status==='running'&&startedAt>0){
      return `<span class="pet-title-text">${_esc(item.title)}</span><span class="pet-elapsed" data-started-at="${startedAt}">${_esc(_formatElapsed(startedAt))}</span>`;
    }
    return _esc(item&&item.title);
  }
  function _actionRequiredText(row,actionType){
    const raw=_clean(row&&row.process_text);
    if(!raw) return _petT('desktop_pet_action_required');
    return `${_petT('desktop_pet_action_required')}: ${raw}`;
  }
  function _attentionItems(){
    dismissed=_readJson(DISMISSED_KEY,{});
    return sessions.map(row=>{
      const status=String(row&&row.status||'idle');
      const dismissKey=_dismissKeyForRow(row,status);
      const actionType=_clean(row.action_required_type);
      const text=status==='ready'?_readyMetaText(row):(status==='action_required'?_actionRequiredText(row,actionType):(_clean(row.process_text)||_petT('desktop_pet_thinking')));
      const tooltip=status==='ready'?(_clean(row.process_text)||text):(status==='action_required'?(_clean(row.process_text)||text):text);
      return {...row,status,dismissKey,actionType,text,tooltip,action_required_command:_clean(row.action_required_command),action_required_description:_clean(row.action_required_description),action_required_approval_id:_clean(row.action_required_approval_id),action_required_choices:Array.isArray(row.action_required_choices)?row.action_required_choices:[],action_required_clarify_id:_clean(row.action_required_clarify_id)};
    }).filter(item=>item.status!=='idle'&&dismissed[item.dismissKey]!==true).sort((a,b)=>{
      const priority={action_required:3,ready:2,running:1};
      if(a.status!==b.status) return (priority[b.status]||0)-(priority[a.status]||0);
      return Number(b.last_message_at||0)-Number(a.last_message_at||0);
    });
  }
  function _statusHtml(item){
    const status=item&&item.status;
    if(status==='action_required'){
      const type=item&&item.actionType==='approval'?'approval':(item&&item.actionType==='clarify'?'clarify':'action');
      const symbol=type==='approval'?'!':'?';
      return `<span class="pet-action-required is-${type}" aria-label="${_esc(_petT('desktop_pet_action_required'))}">${symbol}</span>`;
    }
    if(status==='running') return `<span class="pet-spinner" aria-label="${_esc(_petT('desktop_pet_running'))}"></span>`;
    return `<span class="pet-ready" aria-label="${_esc(_petT('desktop_pet_ready'))}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg></span>`;
  }
  function _replyHtml(item){
    if(replySid!==item.session_id) return '';
    const pending=replyPendingSid===item.session_id;
    const replyLabel=_petT('desktop_pet_reply');
    const sendingLabel=_petT('desktop_pet_sending');
    const activeLabel=pending?sendingLabel:replyLabel;
    return `<form class="pet-reply" data-sid="${_esc(item.session_id)}"><input class="pet-reply-input" type="text" value="${_esc(replyText)}" placeholder="${_esc(activeLabel)}" aria-label="${_esc(replyLabel)}" autocomplete="off" ${pending?'disabled':''}><button class="pet-reply-submit" type="submit" ${pending?'disabled':''}>${_esc(activeLabel)}</button>${replyError?`<div class="pet-reply-error">${_esc(replyError)}</div>`:''}</form>`;
  }
  function _actionPendingKey(item){
    if(!item||item.status!=='action_required') return '';
    if(item.actionType==='approval') return `approval:${item.session_id}:${item.action_required_approval_id||item.dismissKey||''}`;
    if(item.actionType==='clarify') return `clarify:${item.session_id}:${item.action_required_clarify_id||item.dismissKey||''}`;
    return '';
  }
  function _submitClarifyResponse(sid,clarifyId,response,pendingKey,onError){
    const text=_clean(response);
    if(!sid||!text){if(onError) onError();return;}
    const key=pendingKey||`clarify:${sid}:${clarifyId||''}`;
    if(pendingActionResponses[key]) return;
    pendingActionResponses[key]=true;
    render(true);
    fetch('/api/clarify/respond',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json',..._csrfHeaders()},body:JSON.stringify({session_id:sid,response:text,clarify_id:clarifyId||''})}).then(res=>{if(!res.ok) throw new Error(`Clarify failed: ${res.status}`);return res.json();}).then(()=>{delete pendingActionResponses[key];delete clarifyDrafts[key];expandedActionKey='';refresh();}).catch(()=>{delete pendingActionResponses[key];render(true);if(onError) setTimeout(onError,0);});
  }
  function _expandHtml(item){
    if(item.status!=='action_required') return '';
    const pendingKey=_actionPendingKey(item);
    const pending=!!pendingActionResponses[pendingKey];
    if(item.actionType==='approval'){
      const command=_esc(item.action_required_command||'');
      const cmdBlock=command?`<div class="expand-command">${command}</div>`:'';
      const approveLabel=_esc(_petT('desktop_pet_approve'));
      const denyLabel=_esc(_petT('desktop_pet_deny'));
      return `<div class="pet-card-expand">${cmdBlock}<div class="expand-actions"><button class="btn-approve" type="button" data-sid="${_esc(item.session_id)}" data-approval-id="${_esc(item.action_required_approval_id||'')}" ${pending?'disabled':''}>${approveLabel}</button><button class="btn-deny" type="button" data-sid="${_esc(item.session_id)}" data-approval-id="${_esc(item.action_required_approval_id||'')}" ${pending?'disabled':''}>${denyLabel}</button></div></div>`;
    }
    if(item.actionType==='clarify'){
      const choices=Array.isArray(item.action_required_choices)?item.action_required_choices:[];
      if(!choices.length) return '';
      const qLabel=_esc(_petT('desktop_pet_pick_one'));
      const longest=choices.reduce((max,choice)=>Math.max(max,_clean(choice).length),0);
      const stacked=longest>16||choices.length>4;
      const chips=choices.map(choice=>`<button class="choice-chip" type="button" data-sid="${_esc(item.session_id)}" data-clarify-id="${_esc(item.action_required_clarify_id||'')}" data-choice="${_esc(choice)}" ${pending?'disabled':''}>${_esc(choice)}</button>`).join('');
      const otherLabel=_esc(_petT('clarify_other'));
      const sendLabel=_esc(_petT('clarify_send'));
      const placeholder=_esc(_petT('clarify_input_placeholder'));
      const draft=_esc(clarifyDrafts[pendingKey]||'');
      const otherSelected=clarifyOtherKey===pendingKey||!!clarifyDrafts[pendingKey];
      const customRow=otherSelected?`<div class="clarify-custom-row"><input class="clarify-custom-input" type="text" value="${draft}" placeholder="${placeholder}" aria-label="${placeholder}" autocomplete="off" ${pending?'disabled':''}><button class="clarify-custom-submit" type="submit" ${pending?'disabled':''}>${sendLabel}</button></div>`:'';
      return `<div class="pet-card-expand"><div class="expand-question">${qLabel}</div><div class="expand-choices${stacked?' is-stacked':''}">${chips}</div><form class="clarify-custom" data-other-selected="${otherSelected?'1':'0'}" data-sid="${_esc(item.session_id)}" data-clarify-id="${_esc(item.action_required_clarify_id||'')}" data-pending-key="${_esc(pendingKey)}"><button class="clarify-other" type="button" ${pending?'disabled':''}>${otherLabel}</button>${customRow}</form></div>`;
    }
    return '';
  }
  function _hiddenCardCount(scroller){
    if(!scroller) return 0;
    const bottom=scroller.scrollTop+scroller.clientHeight;
    return Array.from(scroller.querySelectorAll('.pet-card')).filter(card=>card.offsetTop+card.offsetHeight>bottom+2).length;
  }
  function _syncViewport(){
    const scroller=bubbles.querySelector('.pet-viewport');
    if(!scroller) return;
    const maxScroll=Math.max(0,scroller.scrollHeight-scroller.clientHeight);
    const topHidden=scroller.scrollTop>3;
    const bottomHidden=maxScroll-scroller.scrollTop>3;
    bubbles.classList.toggle('has-hidden-above',topHidden);
    bubbles.classList.toggle('has-overflow',bottomHidden);
    const latest=bubbles.querySelector('.pet-latest');
    const more=bubbles.querySelector('.pet-more');
    if(latest) latest.hidden=!topHidden;
    if(more){
      const count=Math.max(1,_hiddenCardCount(scroller));
      more.hidden=!bottomHidden;
      more.textContent=`+${count}`;
      more.setAttribute('aria-label',_petT('desktop_pet_more_sessions_below',count));
    }
  }
  function _restoreViewport(){
    const scroller=bubbles.querySelector('.pet-viewport');
    if(!scroller) return;
    const maxScroll=Math.max(0,scroller.scrollHeight-scroller.clientHeight);
    scroller.scrollTop=Math.min(maxScroll,Math.max(0,bubbleScrollTop));
    _syncViewport();
  }
  function _scheduleExpandWindowSync(){
    bubbleContentHeightDirty=true;
    _scheduleBubbleSync();
    setTimeout(()=>{bubbleContentHeightDirty=true;_scheduleBubbleSync();},280);
  }
  function _setExpandedActionCard(card,expanded){
    const key=card&&(card.dataset.dismissKey||card.dataset.sid||'');
    if(!key) return;
    if(expandCollapseTimer){
      clearTimeout(expandCollapseTimer);
      expandCollapseTimer=0;
    }
    const next=expanded?key:'';
    if(expandedActionKey===next) return;
    expandedActionKey=next;
    for(const item of bubbles.querySelectorAll('.pet-card.has-expand')){
      item.dataset.expanded=item.dataset.dismissKey===expandedActionKey?'1':'0';
    }
    _scheduleExpandWindowSync();
    if(expanded) _scrollExpandedCardIntoView();
  }
  function _scrollExpandedCardIntoView(){
    // Surface the expanded card after the grow transition settles so every
    // choice/button lands inside the resized native window (hover + click work).
    const settle=()=>{
      const scroller=bubbles.querySelector('.pet-viewport');
      const card=bubbles.querySelector('.pet-card[data-expanded="1"]');
      if(!scroller||!card) return;
      const target=Math.max(0,Math.min(card.offsetTop-2,scroller.scrollHeight-scroller.clientHeight));
      scroller.scrollTop=target;
      bubbleScrollTop=target;
      _syncViewport();
    };
    requestAnimationFrame(settle);
    setTimeout(settle,300);
  }
  function _collapseExpandedActionCardSoon(card){
    const key=card&&(card.dataset.dismissKey||card.dataset.sid||'');
    if(!key||expandedActionKey!==key) return;
    if(expandCollapseTimer) clearTimeout(expandCollapseTimer);
    expandCollapseTimer=setTimeout(()=>{
      expandCollapseTimer=0;
      if(!card.isConnected) return;
      if(card.matches(':hover')||card.contains(document.activeElement)) return;
      _setExpandedActionCard(card,false);
    },180);
  }
  function _focusClarifyCustomInput(){
    _requestPetRaise(true,true);
    const focus=()=>{
      const input=document.querySelector('.clarify-custom[data-other-selected="1"] .clarify-custom-input');
      if(input){
        input.focus({preventScroll:true});
        if(typeof input.select==='function') input.select();
      }
    };
    requestAnimationFrame(()=>setTimeout(focus,0));
  }
  function render(force){
    if(!force&&bubbles.contains(document.activeElement)&&(document.activeElement.classList.contains('pet-reply-input')||document.activeElement.classList.contains('clarify-custom-input'))) return;
    const items=_attentionItems();
    const count=items.length;
    // Preserve the chip's :hover highlight while the user is actively reading an
    // expanded action card, but ONLY when the attention state is unchanged. Any
    // status flip (e.g. clarify -> running after the user picks) changes the
    // signature and must always re-render so the card returns to running.
    const signature=items.map(item=>`${item.session_id}~${item.status}~${item.dismissKey}`).join('|');
    if(!force&&expandedActionKey&&signature===lastRenderedSignature&&typeof bubbles.matches==='function'&&bubbles.matches(':hover')) return;
    lastRenderedSignature=signature;
    const collapsed=localStorage.getItem(COLLAPSED_KEY)==='true' && localStorage.getItem(COLLAPSE_EXPLICIT_KEY)==='1';
    if(count&&!collapsed) _hideStartupMessagesForAttention();
    bubbles.hidden=!count||collapsed;
    if(!count){bubbles.innerHTML='';_scheduleBubbleSync();return;}
    bubbleContentHeightDirty=true;
    const visibleKeys=new Set(items.map(item=>item.dismissKey));
    if(expandedActionKey&&!visibleKeys.has(expandedActionKey)) expandedActionKey='';
    bubbles.innerHTML=`<div class="pet-viewport" tabindex="0"><div class="pet-list" role="list">${items.map(item=>{const expand=_expandHtml(item);const isExpanded=expand&&item.dismissKey===expandedActionKey;return `<article class="pet-card${expand?' has-expand':''}" role="listitem" tabindex="0" data-sid="${_esc(item.session_id)}" data-status="${item.status}" data-dismiss-key="${_esc(item.dismissKey)}" data-action-type="${_esc(item.actionType||'')}" data-reply-open="${replySid===item.session_id?'1':'0'}"${expand?` data-expanded="${isExpanded?'1':'0'}"`:''}><button class="pet-dismiss" type="button" aria-label="${_esc(_petT('desktop_pet_dismiss_update'))}"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="20" y1="4" x2="4" y2="20"/><line x1="4" y1="4" x2="20" y2="20"/></svg></button><div class="pet-card-main"><div><div class="pet-card-title" title="${_esc(item.title)}">${_titleHtml(item)}</div><div class="pet-card-text" title="${_esc(item.tooltip||item.text)}">${_esc(item.text)}</div></div><div class="pet-card-status">${_statusHtml(item)}</div></div>${expand}${item.status==='action_required'||replySid===item.session_id?'':`<button class="pet-reply-toggle" type="button">${_esc(_petT('desktop_pet_reply'))}</button>`}${_replyHtml(item)}${item.session_id===openingSid?'<div class="pet-card-opening" aria-hidden="true"><span class="pet-spinner"></span></div>':''}</article>`;}).join('')}</div></div><button class="pet-latest" type="button" hidden>${_esc(_petT('desktop_pet_latest'))}</button><button class="pet-more" type="button" hidden>+1</button>`;
    requestAnimationFrame(_restoreViewport);
    _scheduleBubbleSync();
  }
  async function refresh(){
    try{
      const data=await fetch('/api/pet/attention'+_attentionQuery(),{cache:'no-store'}).then(res=>{if(!res.ok) throw new Error(`Pet attention failed: ${res.status}`);return res.json();});
      sessions=Array.isArray(data.sessions)?data.sessions:[];
      render();
      return true;
    }catch(_){return false;}
  }
  function _markViewed(sid){
    const row=sessions.find(item=>item.session_id===sid);
    if(!row) return;
    const viewed=_readJson(SESSION_VIEWED_COUNTS_KEY,{});
    viewed[sid]=Number(row.message_count||0);
    _writeJson(SESSION_VIEWED_COUNTS_KEY,viewed);
    const unread=_readJson(SESSION_COMPLETION_UNREAD_KEY,{});
    if(Object.prototype.hasOwnProperty.call(unread,sid)){
      delete unread[sid];
      _writeJson(SESSION_COMPLETION_UNREAD_KEY,unread);
    }
  }
  function _dismissOpenedReadySession(sid){
    const row=sessions.find(item=>item.session_id===sid&&item.status==='ready');
    if(!row) return;
    dismissed[_dismissKeyForRow(row,'ready')]=true;
    _writeJson(DISMISSED_KEY,dismissed);
  }
  function _hideOpenedReadySession(sid){
    sessions=sessions.filter(item=>!(item.session_id===sid&&item.status==='ready'));
    if(replySid===sid) replySid='';
  }
  function _currentTauriWindow(){
    const tauri=window.__TAURI__;
    if(!tauri) return null;
    if(tauri.webviewWindow&&typeof tauri.webviewWindow.getCurrentWebviewWindow==='function') return tauri.webviewWindow.getCurrentWebviewWindow();
    if(tauri.window&&typeof tauri.window.getCurrent==='function') return tauri.window.getCurrent();
    return null;
  }
  function _monitorBounds(monitor){
    const pos=(monitor&&monitor.position)||monitor||{};
    const size=(monitor&&monitor.size)||monitor||{};
    return {x:Number(pos.x||0),y:Number(pos.y||0),width:Number(size.width||0),height:Number(size.height||0),scale:Number(monitor&&(monitor.scale||monitor.scaleFactor)||1)||1};
  }
  function _monitorUsable(monitor){
    const bounds=_monitorBounds(monitor);
    return Number.isFinite(bounds.width)&&Number.isFinite(bounds.height)&&bounds.width>0&&bounds.height>0;
  }
  function _browserMonitorBounds(){
    const screenObj=window.screen||{};
    const scale=Number(window.devicePixelRatio||1)||1;
    const x=Number(screenObj.availLeft||0)*scale,y=Number(screenObj.availTop||0)*scale;
    const width=Number(screenObj.availWidth||screenObj.width||0)*scale,height=Number(screenObj.availHeight||screenObj.height||0)*scale;
    return {x,y,width,height,scale,coordinateSpace:'physical'};
  }
  function _tauriDpiCtor(name){
    const tauri=window.__TAURI__;
    return (tauri&&tauri.dpi&&tauri.dpi[name])||(tauri&&tauri.window&&tauri.window[name])||null;
  }
  function _logicalSize(width,height){const Ctor=_tauriDpiCtor('LogicalSize');return Ctor?new Ctor(width,height):null;}
  function _physicalPosition(x,y){const Ctor=_tauriDpiCtor('PhysicalPosition');return Ctor?new Ctor(Math.round(x),Math.round(y)):null;}
  function _logicalPosition(x,y){const Ctor=_tauriDpiCtor('LogicalPosition');return Ctor?new Ctor(x,y):null;}
  function _bubblePositionArg(pos){
    // macOS global window coordinates are logical points; that is the ONLY space
    // consistent across monitors with different scale factors. Pet
    // outerPosition()/currentMonitor() are reported as logical*ownScale, so the
    // physical-space result divided by that scale yields the correct global
    // logical point. Passing setPosition a PhysicalPosition lets winit
    // reinterpret it with the wrong (current) monitor scale, which strands the
    // bubble on the built-in display when the pet sits on a differently-scaled
    // external monitor. Fall back to physical only when LogicalPosition is
    // unavailable (single-monitor safe).
    const scale=Number(pos&&pos.scale)>0?Number(pos.scale):1;
    return _logicalPosition(pos.x/scale,pos.y/scale)||_physicalPosition(pos.x,pos.y);
  }
  function _bubbleMode(){
    const count=_attentionItems().length;
    const collapsed=localStorage.getItem(COLLAPSED_KEY)==='true' && localStorage.getItem(COLLAPSE_EXPLICIT_KEY)==='1';
    if(count&&!collapsed) return 'bubbles';
    if(_isWelcomeVisible()) return 'welcome';
    if(_isInstallVisible()) return 'install';
    if(_isToastVisible()) return 'toast';
    return 'hidden';
  }
  function _windowForMode(mode){return mode==='install'?INSTALL_WINDOW:(mode==='toast'?TOAST_WINDOW:BUBBLE_WINDOW);}
  function _clamp(value,min,max){return Math.min(max,Math.max(min,value));}
  function _modePreferredPlacement(mode){return mode==='install'||mode==='toast'?'above':'';}
  // Pet layout events use physical window coordinates; WebView content sizes
  // are logical CSS pixels. Convert sizes and gaps before positioning.
  function _setViewportMax(px){
    document.documentElement.style.setProperty('--pet-viewport-max',`${Math.max(0,Math.round(px))}px`);
  }
  function _measureBubbleContentHeight(){
    if(!bubbleContentHeightDirty) return bubbleContentHeightCache;
    const viewport=bubbles.querySelector('.pet-viewport');
    const list=bubbles.querySelector('.pet-list');
    if(!viewport||!list){_setViewportMax(BUBBLE_WINDOW.height-BUBBLE_BOTTOM_INSET);return BUBBLE_WINDOW.height-BUBBLE_BOTTOM_INSET;}
    const cards=Array.from(list.querySelectorAll('.pet-card'));
    if(!cards.length){_setViewportMax(0);return 0;}
    const gap=parseFloat(getComputedStyle(list).gap||'0')||0;
    // Reference a collapsed card for the normal "2.7 visible cards" cap so an
    // expanded card sitting first in the list does not inflate the budget.
    const collapsed=cards.find(card=>card.dataset.expanded!=='1');
    const collapsedRef=(collapsed?collapsed.getBoundingClientRect().height:0)||74;
    let cap=Math.ceil(collapsedRef*BUBBLE_MAX_VISIBLE_CARDS+gap*Math.max(0,BUBBLE_MAX_VISIBLE_CARDS-1));
    // When a card is expanded it must be fully reachable (hover/click), so the
    // window grows to fit it plus a hint of the next card; the rest scrolls.
    const expandedCard=list.querySelector('.pet-card[data-expanded="1"]');
    if(expandedCard){
      const expandedHeight=Math.ceil(expandedCard.getBoundingClientRect().height);
      cap=Math.max(cap,expandedHeight+gap+Math.round(collapsedRef*0.6));
    }
    const fullHeight=Math.ceil(list.scrollHeight||collapsedRef);
    const content=Math.max(BUBBLE_MIN_HEIGHT-BUBBLE_BOTTOM_INSET,Math.min(fullHeight,cap));
    _setViewportMax(content);
    bubbleContentHeightCache=content;
    bubbleContentHeightDirty=false;
    return content;
  }
  function _desiredWindowSize(mode){
    const base=_windowForMode(mode);
    if(mode==='bubbles') return {width:base.width,height:Math.max(BUBBLE_MIN_HEIGHT,_measureBubbleContentHeight()+BUBBLE_BOTTOM_INSET)};
    if(mode==='toast'){
      const rect=readyToast&&!readyToast.hidden?readyToast.getBoundingClientRect():null;
      const measured=rect&&rect.height?Math.ceil(rect.height+28):base.height;
      return {width:base.width,height:Math.max(44,measured)};
    }
    if(mode==='welcome'){
      const card=welcome&&welcome.querySelector('.pet-welcome-card');
      const rect=card&&!welcome.hidden?card.getBoundingClientRect():null;
      const measured=rect&&rect.height?Math.ceil(rect.height+16):BUBBLE_MIN_HEIGHT;
      return {width:base.width,height:Math.max(110,measured)};
    }
    if(mode==='install'){
      const card=install&&install.querySelector('.pet-install-card');
      const rect=card&&!install.hidden?card.getBoundingClientRect():null;
      const measured=rect&&rect.height?Math.ceil(rect.height+32):base.height;
      return {width:base.width,height:Math.max(220,measured)};
    }
    return base;
  }
  function _scheduleBubbleSync(forcedMode){
    if(forcedMode) bubbleWindowSyncMode=forcedMode;
    if(bubbleWindowSyncFrame) return;
    const run=()=>{
      bubbleWindowSyncFrame=0;
      const mode=bubbleWindowSyncMode;
      bubbleWindowSyncMode=null;
      _syncBubbleWindow(mode).catch(()=>{});
    };
    if(typeof requestAnimationFrame!=='function'||visibleMode==='hidden'||bubbleWindowSyncMode||(latestPetLayout&&latestPetLayout.dragging)){
      bubbleWindowSyncFrame=setTimeout(run,0);
      return;
    }
    bubbleWindowSyncFrame=requestAnimationFrame(run);
  }
  function _applyWindowHeightLimit(mode,height){
    if(mode!=='bubbles') return;
    const viewport=bubbles.querySelector('.pet-viewport');
    if(!viewport) return;
    const contentHeight=Math.max(0,Math.floor(height-BUBBLE_BOTTOM_INSET));
    if(contentHeight===Math.floor(bubbleWindowCache.logicalHeight)) return;
    viewport.style.maxHeight=`${contentHeight}px`;
    requestAnimationFrame(_syncViewport);
  }
  function _coordinateScale(layout,monitor){
    const scale=Number(layout&&layout.monitor&&layout.monitor.scale)||Number(layout&&layout.monitor&&layout.monitor.scaleFactor)||Number(layout&&layout.pet&&layout.pet.scale)||Number(monitor&&monitor.scale)||Number(monitor&&monitor.scaleFactor)||Number(window.devicePixelRatio||1)||1;
    return Number.isFinite(scale)&&scale>0?scale:1;
  }
  function _positionWindowSize(size,scale){
    return {width:Number(size&&size.width||0)*scale,height:Number(size&&size.height||0)*scale};
  }
  function _availableVerticalSpace(pet,monitor,margin,placement,scale){
    const gap=BUBBLE_GAP*scale;
    if(!monitor||!monitor.width||!monitor.height) return Infinity;
    if(placement==='below') return Math.max(0,monitor.y+monitor.height-margin-(pet.y+pet.height+gap));
    return Math.max(0,pet.y-gap-(monitor.y+margin));
  }
  function _verticalPlacement(pet,monitor,desiredHeight,margin,preferredPlacement,scale){
    const aboveSpace=_availableVerticalSpace(pet,monitor,margin,'above',scale);
    const belowSpace=_availableVerticalSpace(pet,monitor,margin,'below',scale);
    const aboveFits=aboveSpace>=desiredHeight;
    const belowFits=belowSpace>=desiredHeight;
    let placement=preferredPlacement||'above';
    if(placement==='above'&&!aboveFits&&belowFits) placement='below';
    if(placement==='below'&&!belowFits&&aboveFits) placement='above';
    if(!aboveFits&&!belowFits) placement=belowSpace>aboveSpace?'below':'above';
    return {placement,available:placement==='below'?belowSpace:aboveSpace};
  }
  function _horizontalPosition(pet,monitor,width,margin,scale){
    const inset=BUBBLE_SIDE_INSET*scale;
    const safeMonitor=_monitorUsable(monitor)?_monitorBounds(monitor):_browserMonitorBounds();
    const bounds=safeMonitor;
    let x=pet.x+pet.width-(width-inset);
    if(bounds.width&&x+inset<bounds.x+margin) x=pet.x-inset;
    if(bounds.width) x=_clamp(x,bounds.x+margin-inset,bounds.x+bounds.width-width-margin+inset);
    return x;
  }
  function _bubblePosition(layout,windowSize,mode){
    const pet=layout&&layout.pet;
    const monitor=_monitorUsable(layout&&layout.monitor)?_monitorBounds(layout&&layout.monitor):_browserMonitorBounds();
    const scale=_coordinateScale(layout,monitor);
    const margin=8*scale;
    const desired=_positionWindowSize(windowSize,scale);
    if(!desired.width||!desired.height) return null;
    if(!monitor.width||!monitor.height){
      return null;
    }
    if(!pet){
      const inset=BUBBLE_SIDE_INSET*scale;
      const x=_clamp(monitor.x+monitor.width-desired.width-inset,monitor.x+margin-inset,monitor.x+monitor.width-desired.width-margin);
      return {x,y:monitor.y+margin,width:Number(windowSize&&windowSize.width||0),height:Number(windowSize&&windowSize.height||0),placement:'below',scale};
    }
    const vertical=_verticalPlacement(pet,monitor,desired.height,margin,_modePreferredPlacement(mode),scale);
    const height=Math.max(1,Math.min(desired.height,Number.isFinite(vertical.available)?vertical.available:desired.height));
    const gap=BUBBLE_GAP*scale;
    const x=_horizontalPosition(pet,monitor,desired.width,margin,scale);
    let y=vertical.placement==='below'?pet.y+pet.height+gap:pet.y-gap-height;
    if(monitor&&monitor.width&&monitor.height) y=_clamp(y,monitor.y+margin,monitor.y+monitor.height-height-margin);
    return {x,y,width:Number(windowSize&&windowSize.width||0),height:height/scale,placement:vertical.placement,scale};
  }
  function _requestPetRaise(visible,focus){
    const tauri=window.__TAURI__;
    if(!tauri||!tauri.event||typeof tauri.event.emit!=='function') return;
    const payload=typeof visible==='boolean' ? {visible} : {visible:false};
    if(focus===true) payload.focus=true;
    tauri.event.emit(PET_RAISE_REQUESTED_EVENT,payload).catch(()=>{});
  }
  function _nativeDragFollowEnabled(){
    const tauri=window.__TAURI__;
    return !!(tauri&&tauri.event&&typeof tauri.event.emit==='function');
  }
  async function _applyDraggingBubblePosition(win,mode){
    if(!win||typeof win.setPosition!=='function') return false;
    if(bubbleWindowCache.mode!==mode||!bubbleWindowCache.logicalWidth||!bubbleWindowCache.logicalHeight) return false;
    const pos=_bubblePosition(latestPetLayout,{width:bubbleWindowCache.logicalWidth,height:bubbleWindowCache.logicalHeight},mode);
    if(!pos) return false;
    pendingBubblePosition=pos;
    if(bubblePositionInFlight) return true;
    bubblePositionInFlight=true;
    try{
      while(pendingBubblePosition){
        const next=pendingBubblePosition;
        pendingBubblePosition=null;
        const arg=_bubblePositionArg(next);
        if(arg) await win.setPosition(arg);
        bubbleWindowCache={...bubbleWindowCache,x:next.x,y:next.y};
      }
    }catch(err){
      console.warn('Failed to drag-sync pet bubbles window',err);
    }finally{
      bubblePositionInFlight=false;
    }
    return true;
  }
  async function _syncBubbleWindow(forcedMode){
    const seq=++layoutSeq;
    const win=_currentTauriWindow();
    if(!win) return;
    const mode=forcedMode||_bubbleMode();
    try{
      if(mode==='hidden'){
        visibleMode='hidden';
        bubbleWindowCache={mode:'hidden',logicalWidth:0,logicalHeight:0,x:0,y:0};
        if(typeof win.hide==='function') await win.hide();
        _requestPetRaise(false);
        return;
      }
      if(latestPetLayout&&latestPetLayout.dragging&&visibleMode!=='hidden'){
        if(_nativeDragFollowEnabled()) return;
        if(bubbleWindowCache.mode===mode&&bubbleWindowCache.logicalWidth&&bubbleWindowCache.logicalHeight) return _applyDraggingBubblePosition(win,mode);
      }
      const desired=_desiredWindowSize(mode);
      const pos=_bubblePosition(latestPetLayout,desired,mode);
      if(seq!==layoutSeq) return;
      if(!pos){
        visibleMode='hidden';
        bubbleWindowCache={mode:'hidden',logicalWidth:0,logicalHeight:0,x:0,y:0};
        if(typeof win.hide==='function') await win.hide();
        return;
      }
      const logicalHeight=pos.height;
      const needResize=mode!==bubbleWindowCache.mode||desired.width!==bubbleWindowCache.logicalWidth||logicalHeight!==bubbleWindowCache.logicalHeight;
      const needReposition=needResize||bubbleWindowCache.x!==pos.x||bubbleWindowCache.y!==pos.y;
      if(needResize){
        _applyWindowHeightLimit(mode,logicalHeight);
        const logical=_logicalSize(desired.width,logicalHeight);
        if(logical&&typeof win.setSize==='function') await win.setSize(logical);
      }
      if(needReposition&&typeof win.setPosition==='function'){
        const arg=_bubblePositionArg(pos);
        if(arg) await win.setPosition(arg);
      }
      const shouldFocusBubble=visibleMode==='hidden';
      bubbleWindowCache={mode,logicalWidth:desired.width,logicalHeight,x:pos.x,y:pos.y};
      visibleMode=mode;
      // Only (re)show and re-raise the native window when it actually becomes
      // visible or its size/position changed. The pet emits a 1Hz
      // pet-layout-update; re-asserting show()+always-on-top+window-level every
      // tick re-orders the NSWindow and disturbs a focused text field, which
      // made the clarify "Other" input flicker/shudder while typing.
      if(needReposition||shouldFocusBubble){
        if(typeof win.show==='function') await win.show();
        _requestPetRaise(true,shouldFocusBubble);
      }
    }catch(err){console.warn('Failed to sync pet bubbles window',err);}
  }
  function _csrfHeaders(){
    const token=window.__HERMES_CONFIG__&&window.__HERMES_CONFIG__.csrfToken;
    return token?{'X-Hermes-CSRF-Token':token}:{};
  }
  async function _openSessionInBrowser(sid, params){
    const res=await fetch('/api/pet/open_session',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json',..._csrfHeaders()},body:JSON.stringify({session_id:sid,...(params||{})})});
    if(!res.ok) throw new Error(await res.text());
    return res.json();
  }
  function _openSessionSucceeded(result){
    return !!(result&&(result.consumed||result.opened||result.focused||result.reused));
  }
  async function _openSession(sid,status){
    if(openingSid===sid) return null;
    // Give the click instant feedback: the browser-open round trip can take a
    // couple of seconds (AppleScript focus + background-tab ack), so mark the
    // card as opening BEFORE awaiting so it no longer looks like a dead click.
    openingSid=sid;
    render(true);
    try{
      const result=await _openSessionInBrowser(sid);
      if(!_openSessionSucceeded(result)) throw new Error('Desktop pet session navigation was not confirmed');
      if(status==='ready') _dismissOpenedReadySession(sid);
      if(status!=='action_required') _markViewed(sid);
      if(status==='ready') _hideOpenedReadySession(sid);
      openingSid='';
      render(true);
      return result;
    }catch(err){
      console.warn('Failed to open session from pet',err);
      openingSid='';
      render(true);
      return null;
    }
  }
  async function _reply(card){
    const sid=card&&card.dataset.sid;
    const input=card&&card.querySelector('.pet-reply-input');
    const text=_clean(input&&input.value);
    if(!sid||!text){if(input) input.focus();return;}
    replyPendingSid=sid;
    replyError='';
    render(true);
    try{
      await _openSessionInBrowser(sid,{draft:text,autosend:true});
      _markViewed(sid);
      replySid='';
      replyText='';
      replyPendingSid='';
      replyError='';
      render(true);
    }catch(err){
      console.warn('Failed to reply from pet',err);
      replyPendingSid='';
      replyError=_petT('desktop_pet_failed_to_send');
      render(true);
      setTimeout(()=>document.querySelector('.pet-reply-input')?.focus(),0);
    }
  }
  if(welcomeAction){
    welcomeAction.addEventListener('click',event=>{
      event.preventDefault();
      event.stopPropagation();
      _hideWelcome(true);
    });
  }
  bubbles.addEventListener('click',event=>{
    const target=event.target;
    if(target.closest('.pet-latest')){event.preventDefault();event.stopPropagation();const scroller=bubbles.querySelector('.pet-viewport');if(scroller){scroller.scrollTop=0;bubbleScrollTop=0;_syncViewport();}return;}
    if(target.closest('.pet-more')){event.preventDefault();event.stopPropagation();const scroller=bubbles.querySelector('.pet-viewport');if(scroller){scroller.scrollTop=Math.min(scroller.scrollHeight,scroller.scrollTop+scroller.clientHeight*.85);bubbleScrollTop=scroller.scrollTop;_syncViewport();}return;}
    const card=target.closest('.pet-card');
    if(!card) return;
    if(target.closest('.pet-dismiss')){dismissed[card.dataset.dismissKey||`${card.dataset.sid}:${card.dataset.status}`]=true;_writeJson(DISMISSED_KEY,dismissed);render();return;}
    if(target.closest('.pet-reply-toggle')){replySid=replySid===card.dataset.sid?'':card.dataset.sid;replyText='';replyError='';render(true);setTimeout(()=>document.querySelector('.pet-reply-input')?.focus(),0);return;}
    const approveBtn=target.closest('.btn-approve');
    if(approveBtn){
      event.preventDefault();
      event.stopPropagation();
      const pendingKey=`approval:${approveBtn.dataset.sid}:${approveBtn.dataset.approvalId||card.dataset.dismissKey||''}`;
      if(pendingActionResponses[pendingKey]) return;
      pendingActionResponses[pendingKey]=true;
      render(true);
      fetch('/api/approval/respond',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json',..._csrfHeaders()},body:JSON.stringify({session_id:approveBtn.dataset.sid,choice:'once',approval_id:approveBtn.dataset.approvalId||''})}).then(res=>{if(!res.ok) throw new Error(`Approval failed: ${res.status}`);return res.json();}).then(()=>{delete pendingActionResponses[pendingKey];expandedActionKey='';refresh();}).catch(()=>{delete pendingActionResponses[pendingKey];render(true);});
      return;
    }
    const denyBtn=target.closest('.btn-deny');
    if(denyBtn){
      event.preventDefault();
      event.stopPropagation();
      const pendingKey=`approval:${denyBtn.dataset.sid}:${denyBtn.dataset.approvalId||card.dataset.dismissKey||''}`;
      if(pendingActionResponses[pendingKey]) return;
      pendingActionResponses[pendingKey]=true;
      render(true);
      fetch('/api/approval/respond',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json',..._csrfHeaders()},body:JSON.stringify({session_id:denyBtn.dataset.sid,choice:'deny',approval_id:denyBtn.dataset.approvalId||''})}).then(res=>{if(!res.ok) throw new Error(`Approval failed: ${res.status}`);return res.json();}).then(()=>{delete pendingActionResponses[pendingKey];expandedActionKey='';refresh();}).catch(()=>{delete pendingActionResponses[pendingKey];render(true);});
      return;
    }
    const chip=target.closest('.choice-chip');
    if(chip){
      event.preventDefault();
      event.stopPropagation();
      const choice=chip.dataset.choice||'';
      const pendingKey=`clarify:${chip.dataset.sid}:${chip.dataset.clarifyId||card.dataset.dismissKey||''}`;
      _submitClarifyResponse(chip.dataset.sid,chip.dataset.clarifyId||'',choice,pendingKey);
      return;
    }
    const otherBtn=target.closest('.clarify-other');
    if(otherBtn){
      event.preventDefault();
      event.stopPropagation();
      const form=otherBtn.closest('.clarify-custom');
      if(!form) return;
      clarifyOtherKey=form.dataset.pendingKey||`clarify:${form.dataset.sid}:${form.dataset.clarifyId||''}`;
      render(true);
      _focusClarifyCustomInput();
      return;
    }
    if(target.closest('.pet-card-expand')) return;
    if(target.closest('.pet-reply')) return;
    _openSession(card.dataset.sid,card.dataset.status);
  });
  bubbles.addEventListener('pointerdown',event=>{
    if(event.target.closest('.clarify-custom-input')) event.stopPropagation();
  },true);
  bubbles.addEventListener('mousedown',event=>{
    if(event.target.closest('.clarify-custom-input')) event.stopPropagation();
  },true);
  bubbles.addEventListener('pointerenter',event=>{
    const card=event.target.closest('.pet-card.has-expand');
    if(!card) return;
    _setExpandedActionCard(card,true);
  },true);
  bubbles.addEventListener('pointerleave',event=>{
    const card=event.target.closest('.pet-card.has-expand');
    if(!card||card.contains(event.relatedTarget)) return;
    _collapseExpandedActionCardSoon(card);
  },true);
  bubbles.addEventListener('focusin',event=>{const card=event.target.closest('.pet-card.has-expand');if(card) _setExpandedActionCard(card,true);});
  bubbles.addEventListener('focusout',event=>{
    const card=event.target.closest('.pet-card.has-expand');
    if(!card) return;
    setTimeout(()=>{if(!card.isConnected) return;if(!card.contains(document.activeElement)) _collapseExpandedActionCardSoon(card);},0);
  });
  bubbles.addEventListener('submit',event=>{
    event.preventDefault();
    const custom=event.target.closest('.clarify-custom');
    if(custom){
      const input=custom.querySelector('.clarify-custom-input');
      const pendingKey=custom.dataset.pendingKey||`clarify:${custom.dataset.sid}:${custom.dataset.clarifyId||''}`;
      _submitClarifyResponse(custom.dataset.sid,custom.dataset.clarifyId||'',input&&input.value,pendingKey,()=>input&&input.focus());
      return;
    }
    _reply(event.target.closest('.pet-card'));
  });
  bubbles.addEventListener('input',event=>{
    if(event.target.classList.contains('pet-reply-input')) replyText=event.target.value;
    if(event.target.classList.contains('clarify-custom-input')){
      const form=event.target.closest('.clarify-custom');
      if(form) clarifyDrafts[form.dataset.pendingKey||`clarify:${form.dataset.sid}:${form.dataset.clarifyId||''}`]=event.target.value;
    }
  });
  bubbles.addEventListener('scroll',event=>{if(event.target.classList.contains('pet-viewport')){bubbleScrollTop=event.target.scrollTop;_syncViewport();}},true);
  bubbles.addEventListener('keydown',event=>{
    if(!event.target.classList||!event.target.classList.contains('pet-reply-input')) return;
    if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();_reply(event.target.closest('.pet-card'));}
  });
  window.addEventListener('storage',event=>{
    if(![COLLAPSED_KEY,DISMISSED_KEY,SKIN_KEY,COLLAPSE_EXPLICIT_KEY].includes(event.key)) return;
    if(event.key===SKIN_KEY){
      activeSkinId=localStorage.getItem(SKIN_KEY)||activeSkinId;
      _applyPetSkin(activeSkinId);
    }
    render(true);
  });
  async function _listenPetWindowEvents(){
    const tauri=window.__TAURI__;
    if(!tauri||!tauri.event||typeof tauri.event.listen!=='function') return;
    try{
      await tauri.event.listen('pet-layout-update',event=>{latestPetLayout=event.payload||latestPetLayout;_scheduleBubbleSync();});
      await tauri.event.listen('pet-attention-update',()=>{refresh().catch(()=>{});});
    }catch(err){console.warn('Failed to listen for pet layout events',err);}
  }
  setInterval(refresh,POLL_MS);
  setInterval(()=>{
    const els=document.querySelectorAll('.pet-elapsed[data-started-at]');
    for(let i=0;i<els.length;i++){
      const startedAt=Number(els[i].dataset.startedAt||0);
      if(startedAt>0) els[i].textContent=_formatElapsed(startedAt);
    }
  },1000);
  async function _bootBubbles(){
    _localizeStaticLabels();
    await _listenPetWindowEvents();
    const skinStartup=_loadPetSkins();
    const attentionStartup=refresh();
    _runFirstStartInstall([skinStartup,attentionStartup]);
    _listenPetSkinChanges();
  }
  _bootBubbles();
})();
