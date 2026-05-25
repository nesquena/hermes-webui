import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def bytes_for(path: str) -> bytes:
    return (ROOT / path).read_bytes()


def _html_attr_values(source: str, attr: str) -> list[str]:
    values = []
    needle = f'{attr}="'
    start = 0
    while True:
        idx = source.find(needle, start)
        if idx < 0:
            return values
        value_start = idx + len(needle)
        value_end = source.find('"', value_start)
        assert value_end >= 0
        values.append(source[value_start:value_end])
        start = value_end + 1


def _assert_pet_url_resolves_to_root_path(base_url: str, value: str, expected_path: str):
    parsed = urlparse(urljoin(base_url, value))
    assert parsed.path == expected_path


def test_standalone_pet_page_assets_and_apis_are_wired():
    pet_html = read("static/desktop_pet/index.html")
    bubbles_html = read("static/desktop_pet/bubbles.html")
    pet_js = read("static/desktop_pet/pet.js")
    bubbles_js = read("static/desktop_pet/bubbles.js")
    css = read("static/desktop_pet/pet.css")
    sw = read("static/sw.js")
    routes = read("api/pet_routes.py")
    removed_pet_script = "static/" + "floating" + "_pet.js"

    assert "/static/desktop_pet/pet.css?v=__WEBUI_VERSION__&pet_asset=__DESKTOP_PET_ASSET_VERSION__" in pet_html
    assert "/static/desktop_pet/pet.js?v=__WEBUI_VERSION__&pet_asset=__DESKTOP_PET_ASSET_VERSION__" in pet_html
    assert "/static/i18n.js?v=__WEBUI_VERSION__" in pet_html
    assert 'body class="pet-body"' in pet_html
    assert 'id="petStage"' in pet_html
    assert 'id="petBadge"' in pet_html
    assert '<div class="pet-stage" id="petStage" role="button" tabindex="0"' in pet_html
    assert '<div class="pet-badge" id="petBadge" role="button" tabindex="0"' in pet_html
    assert 'id="petBubbles"' not in pet_html
    assert 'id="petInstall"' not in pet_html
    assert "__CSRF_TOKEN_JSON__" in pet_html
    assert "data-tauri-drag-region" not in pet_html

    assert "/static/desktop_pet/bubbles.js?v=__WEBUI_VERSION__&pet_asset=__DESKTOP_PET_ASSET_VERSION__" in bubbles_html
    assert 'body class="pet-bubbles-body"' in bubbles_html
    assert 'id="petBubbles"' in bubbles_html
    assert 'id="petInstall" aria-live="polite" data-tauri-drag-region hidden' in bubbles_html
    assert 'id="petReadyToast"' in bubbles_html
    assert 'id="petWelcome"' in bubbles_html
    assert 'id="petWelcomeAction"' in bubbles_html
    assert 'id="petWelcomeCountdown"' in bubbles_html
    assert '>Hello there<' in bubbles_html
    assert '>Got it<' in bubbles_html
    assert 'data-i18n="desktop_pet_welcome_action"' in bubbles_html
    assert 'id="petCollapse"' not in bubbles_html
    assert 'data-i18n-aria-label="desktop_pet_collapse_updates"' not in bubbles_html
    assert 'id="petStage"' not in bubbles_html

    assert "def _handle_pet_page(handler, template: str = \"index.html\")" in routes
    assert "def _desktop_pet_asset_version(base_version: str) -> str:" in routes
    assert ".replace(\"__DESKTOP_PET_ASSET_VERSION__\", pet_asset_version)" in routes
    assert 'for rel_path in ("pet.css", "pet.js", "bubbles.js"):' in routes
    assert 'if parsed.path == "/pet/bubbles":' in routes
    assert '_handle_pet_page(handler, "bubbles.html")' in routes
    assert 'return _handle_pet_page(handler, "bubbles.html")' not in routes
    assert '"started_at": float(session.get("started_at") or 0)' in routes
    assert 'row["started_at"] = float(run.get("started_at"))' in routes

    assert "const FRAME_MS=520" in pet_js
    assert "const PET_DISPLAY_SCALE=2/3" in pet_js
    assert "let currentPetDisplaySize={width:128,height:139}" in pet_js
    assert "let currentPetWindowSize={width:146,height:139}" in pet_js
    assert "const PET_BADGE_FIXED={right:16,top:14,size:26,gap:-24,hitPad:8}" in pet_js
    assert "PET_WINDOW_EXPANDED" not in pet_js
    assert "PET_WINDOW_COMPACT" not in pet_js
    assert "PET_ANCHOR" not in pet_js
    assert "PET_COMPACT_ANCHOR" not in pet_js
    assert "function _displaySizeForLayout(layout)" in pet_js
    assert "function _windowSizeForDisplaySize(display)" in pet_js
    assert "function _applyPetDisplaySize(layout)" in pet_js
    assert "win.setSize(logical)" in pet_js
    assert "fetch('/api/pet/attention'+_attentionQuery()" in pet_js
    assert "fetch('/api/pet/skins'" in pet_js
    assert "const DEFAULT_PET_LAYOUT={columns:8,rows:9,frameWidth:192,frameHeight:208" in pet_js
    assert "function _normalizeSkinLayout(layout)" in pet_js
    assert "sprite.style.backgroundSize=`${layout.columns*100}% ${layout.rows*100}%`;" in pet_js
    assert "sprite.style.width=`${currentPetDisplaySize.width}px`;" in pet_js
    assert "stage.style.height=`${currentPetDisplaySize.height}px`;" in pet_js
    assert "currentPetWindowSize=_windowSizeForDisplaySize(currentPetDisplaySize)" in pet_js
    assert "--pet-window-width" in pet_js
    assert "win.setSize(logical)" in pet_js
    assert "tauri.event.emit('pet-layout-update'" in pet_js
    assert "tauri.event.emit('pet-attention-update'" in pet_js
    assert "tauri.event.emit('pet-context-menu',{skins:petSkins,activeSkinId:" in pet_js
    assert "menuLabels:_menuLabels()" in pet_js
    assert "function _registerDesktopPetProcess()" in pet_js
    assert "desktop_pet_pid" in pet_js
    assert "fetch('/api/pet/register'" in pet_js
    assert "base_url:location.origin" in pet_js
    assert "badge.hidden=!count;" in pet_js
    assert "badge.classList.toggle('is-expanded',!!count&&!collapsed);" in pet_js
    assert "badge.innerHTML='<svg" in pet_js
    assert "badge.textContent=String(count);" in pet_js
    assert "badge.setAttribute('aria-label',_petT('desktop_pet_collapse_updates'))" in pet_js
    assert "badge.setAttribute('aria-label',_petT('desktop_pet_expand_updates'))" in pet_js
    assert "function _badgeGeometryForPet(pet)" in pet_js
    assert "const badgeGeo=_badgeGeometryForPet(nextGeo)" in pet_js
    assert "pet.width/currentPetWindowSize.width" in pet_js
    assert "currentPetDisplaySize.width+PET_BADGE_FIXED.gap" in pet_js
    assert "bubbleAnchor" not in pet_js
    assert "badge:badgeGeo,monitor:bounds" in pet_js
    pet_monitor_usable = pet_js[
        pet_js.index("function _monitorUsable(monitor)") : pet_js.index(
            "async function _windowGeometry", pet_js.index("function _monitorUsable(monitor)")
        )
    ]
    assert "_monitorBounds(monitor)" in pet_monitor_usable
    assert "bounds.width" in pet_monitor_usable
    assert "monitor&&monitor.width" not in pet_monitor_usable
    assert "function _emitPetLayoutBurst()" in pet_js
    assert "function _startDragLayoutTracking()" in pet_js
    assert "dragLayoutTrackUntil=Date.now()+12000" in pet_js
    assert "_startDragLayoutTracking();" in pet_js
    assert "window.addEventListener('mouseup',_stopDragLayoutTracking,{capture:true})" in pet_js
    assert "async function _listenPetWindowGeometry()" in pet_js
    assert "win.onMoved" in pet_js
    assert "win.onResized" in pet_js
    assert "localStorage.setItem(COLLAPSED_KEY,collapsed?'false':'true')" in pet_js
    assert "tauri.event.listen('pet-skin-change'" in pet_js
    assert "const RESTART_POSITION_KEY='hermes-pet-restart-position'" in pet_js
    assert "async function _savePetRestartPosition" in pet_js
    assert "async function _restorePetRestartPosition" in pet_js
    assert "async function _restartPetInPlace" in pet_js
    assert "PET_NATIVE_RESTART_REQUESTED_EVENT='pet-native-restart-requested'" in pet_js
    assert "localStorage.setItem(RESTART_POSITION_KEY,JSON.stringify" in pet_js
    assert "localStorage.removeItem(RESTART_POSITION_KEY)" in pet_js
    assert "location.reload()" in pet_js
    assert "tauri.event.listen('pet-restart-requested'" in pet_js
    assert "localStorage.getItem(SKIN_KEY)||'keeper'" in pet_js
    assert "let _isDragging=false;" in pet_js
    assert "let _dragPrevX=null;" in pet_js
    assert "if(!_isDragging){_setState(items.some(item=>item.status==='action_required')?'waiting':(items.some(item=>item.status==='ready')?'waving':(items.some(item=>item.status==='running')?'running':'idle')));}" in pet_js
    assert "_isDragging=true;_dragPrevX=null;" in pet_js
    assert "_isDragging=false;_dragPrevX=null;" in pet_js
    assert "_emitPetLayoutBurst();\n    render();" in pet_js
    assert "if(dragging&&nextGeo)" in pet_js
    assert "_setState(dx>_dragPrevX?'running-right':'running-left')" in pet_js
    assert "stage.addEventListener('click',_onStageClick)" in pet_js
    assert "stage.addEventListener('keydown',event=>_handleAccessibleKey(event,()=>_setState('jumping')))" in pet_js
    assert "stage.addEventListener('mousedown',_startTauriWindowDrag,{capture:true})" in pet_js
    assert "stage.addEventListener('pointerdown',_startTauriWindowDrag,{capture:true})" in pet_js
    assert "badge.addEventListener('click',_onBadgeActivate)" in pet_js
    assert "badge.addEventListener('keydown',event=>_handleAccessibleKey(event,_onBadgeActivate))" in pet_js
    assert "function _eventInsideBadge(event)" in pet_js
    assert "const pad=Number(PET_BADGE_FIXED.hitPad||0)" in pet_js
    assert "x>=rect.left-pad&&x<=rect.right+pad&&y>=rect.top-pad&&y<=rect.bottom+pad" in pet_js
    assert "if(_eventInsideBadge(event)) return;" in pet_js
    assert "function _onStageClick(event)" in pet_js
    assert "if(_eventInsideBadge(event)){" in pet_js
    assert "_onBadgeActivate();" in pet_js
    assert "_setState('jumping')" in pet_js
    assert "badge.addEventListener('mousedown'" not in pet_js
    assert "badge.addEventListener('pointerdown'" not in pet_js

    assert "const BUBBLE_WINDOW={width:320,height:300}" in bubbles_js
    assert "const INSTALL_WINDOW={width:320,height:300}" in bubbles_js
    assert "const TOAST_WINDOW={width:320,height:92}" in bubbles_js
    assert "const BUBBLE_MAX_VISIBLE_CARDS=2.7" in bubbles_js
    assert "const BUBBLE_SIDE_INSET=10" in bubbles_js
    assert "const BUBBLE_GAP=8" in bubbles_js
    assert "const BUBBLE_BOTTOM_INSET=0" in bubbles_js
    assert "PET_RAISE_REQUESTED_EVENT='pet-raise-requested'" in bubbles_js
    assert "function _requestPetRaise(visible,focus)" in bubbles_js
    assert "tauri.event.emit(PET_RAISE_REQUESTED_EVENT,payload)" in bubbles_js
    assert "visibleMode='hidden'" in bubbles_js
    assert "function _syncBubbleWindow" in bubbles_js
    assert ".setSize(" in bubbles_js
    assert ".setPosition(" in bubbles_js
    assert ".hide()" in bubbles_js
    assert ".show()" in bubbles_js
    assert "tauri.event.listen('pet-layout-update'" in bubbles_js
    assert "tauri.event.listen('pet-attention-update'" in bubbles_js
    assert "fetch('/api/pet/attention'+_attentionQuery()" in bubbles_js
    assert "fetch('/api/pet/skins'" in bubbles_js
    assert "fetch('/api/pet/open_session'" in bubbles_js
    assert "function _measureBubbleContentHeight" in bubbles_js
    assert "function _desiredWindowSize(mode)" in bubbles_js
    assert "function _applyWindowHeightLimit(mode,height)" in bubbles_js
    assert "function _coordinateScale(layout,monitor)" in bubbles_js
    assert "layout&&layout.monitor&&layout.monitor.scaleFactor" in bubbles_js
    assert "monitor&&monitor.scaleFactor" in bubbles_js
    bubbles_monitor_usable = bubbles_js[
        bubbles_js.index("function _monitorUsable(monitor)") : bubbles_js.index(
            "function _browserMonitorBounds", bubbles_js.index("function _monitorUsable(monitor)")
        )
    ]
    assert "_monitorBounds(monitor)" in bubbles_monitor_usable
    assert "bounds.width" in bubbles_monitor_usable
    assert "monitor&&monitor.width" not in bubbles_monitor_usable
    assert "function _positionWindowSize(size,scale)" in bubbles_js
    assert "function _availableVerticalSpace(pet,monitor,margin,placement,scale)" in bubbles_js
    assert "function _verticalPlacement(pet,monitor,desiredHeight,margin,preferredPlacement,scale)" in bubbles_js
    assert "function _horizontalPosition(pet,monitor,width,margin,scale)" in bubbles_js
    assert "const safeMonitor=_monitorUsable(monitor)?_monitorBounds(monitor):_browserMonitorBounds();" in bubbles_js
    assert "const monitor=_monitorUsable(layout&&layout.monitor)?_monitorBounds(layout&&layout.monitor):_browserMonitorBounds();" in bubbles_js
    assert "const desired=_positionWindowSize(windowSize,scale)" in bubbles_js
    assert "height:height/scale" in bubbles_js
    assert "const inset=BUBBLE_SIDE_INSET*scale" in bubbles_js
    assert "let x=pet.x+pet.width-(width-inset)" in bubbles_js
    assert "x=pet.x-inset" in bubbles_js
    assert "placement=belowSpace>aboveSpace?'below':'above'" in bubbles_js
    assert "const pet=layout&&layout.pet" in bubbles_js
    assert "bubbleAnchor" not in bubbles_js
    assert "placement:'right'" not in bubbles_js
    assert "placement:'left'" not in bubbles_js
    assert "layout.align" not in bubbles_js
    assert "layout.placement" not in bubbles_js
    assert "Promise.all(startupPromises)" in bubbles_js
    assert "Promise.allSettled(startupPromises)" not in bubbles_js
    assert "async function _openSession(sid,status)" in bubbles_js
    open_session_idx = bubbles_js.index("async function _openSession(sid,status)")
    open_session_block = bubbles_js[open_session_idx:bubbles_js.index("async function _reply", open_session_idx)]
    # Optimistic click feedback: mark the card as opening and render BEFORE the
    # browser-open round trip, so the bubble does not look like a dead click.
    assert "let openingSid='';" in bubbles_js
    assert open_session_block.index("openingSid=sid") < open_session_block.index("await _openSessionInBrowser(sid)")
    assert "item.session_id===openingSid" in bubbles_js
    assert 'class="pet-card-opening"' in bubbles_js
    assert "function _openSessionSucceeded(result)" in bubbles_js
    assert "result.consumed||result.opened||result.focused||result.reused" in bubbles_js
    assert open_session_block.index("_openSessionSucceeded(result)") < open_session_block.index("_dismissOpenedReadySession(sid)")
    assert "try{" in open_session_block
    assert "catch(err){" in open_session_block
    assert "console.warn('Failed to open session from pet',err)" in open_session_block
    assert "function _hideOpenedReadySession(sid)" in bubbles_js
    assert "_openSessionInBrowser(sid).catch(err=>console.warn('Failed to open session from pet',err))" not in bubbles_js
    assert "_openSession(card.dataset.sid,card.dataset.status);" in bubbles_js
    assert "{draft:text,autosend:true}" in bubbles_js
    assert "event.key==='Enter'&&!event.shiftKey&&!event.isComposing" in bubbles_js
    assert "const INSTALL_SEEN_KEY='hermes-pet-install-seen'" in bubbles_js
    assert "const WELCOME_SEEN_KEY='hermes-pet-welcome-seen'" in bubbles_js
    assert "const WELCOME_SECONDS=30" in bubbles_js
    assert "const WELCOME_IDLE_GRACE_MS=2600" in bubbles_js
    assert "const welcomeCountdown=document.getElementById('petWelcomeCountdown')" in bubbles_js
    assert "function _runFirstStartInstall" in bubbles_js
    assert "function _scheduleWelcomeBubble()" in bubbles_js
    assert "function _showWelcomeBubble()" in bubbles_js
    assert "function _hideWelcome(markSeen)" in bubbles_js
    assert "function _stopWelcomeDelay()" in bubbles_js
    assert "function _setWelcomeCountdown(seconds)" in bubbles_js
    assert "_scheduleWelcomeBubble();" in bubbles_js
    install_seen_idx = bubbles_js.index("if(localStorage.getItem(INSTALL_SEEN_KEY)==='1')")
    install_seen_block = bubbles_js[install_seen_idx:bubbles_js.index("setTimeout(()=>_setInstallStatus", install_seen_idx)]
    assert "_hideInstall();" in install_seen_block
    assert "_scheduleWelcomeBubble();" in install_seen_block
    assert "install.hidden=false;" not in bubbles_js
    assert "_scheduleBubbleSync('install');" not in bubbles_js
    assert "function _hasVisibleAttention()" in bubbles_js
    assert "function _hideStartupMessagesForAttention()" in bubbles_js
    assert "if(count&&!collapsed) _hideStartupMessagesForAttention();" in bubbles_js
    assert "if(welcome&&!welcome.hidden) _hideWelcome(false);" in bubbles_js
    assert "if(_hasVisibleAttention()){" in bubbles_js
    bubble_mode_idx = bubbles_js.index("function _bubbleMode()")
    bubble_mode_block = bubbles_js[bubble_mode_idx:bubbles_js.index("function _windowForMode", bubble_mode_idx)]
    assert bubble_mode_block.index("const count=_attentionItems().length") < bubble_mode_block.index("if(_isInstallVisible()) return 'install';")
    assert "if(count&&!collapsed) return 'bubbles';" in bubble_mode_block
    assert "if(_isWelcomeVisible()) return 'welcome';" in bubble_mode_block
    assert "welcomeAction.addEventListener('click'" in bubbles_js
    assert "welcomeTimer=setInterval(()=>{" in bubbles_js
    assert "welcomeDelayTimer=setTimeout(()=>{" in bubbles_js
    assert "welcomeSecondsRemaining-=1;" in bubbles_js
    assert "if(welcomeSecondsRemaining<=0){_hideWelcome(true);return;}" in bubbles_js
    assert 'if(installSprite){installSprite.style.backgroundImage=`url("${next.spritesheetUrl}")`;installSprite.style.backgroundSize=`${layout.columns*100}% ${layout.rows*100}%`;}' in bubbles_js
    assert "desktop_pet_ready_toast" in bubbles_js
    assert "_petT('desktop_pet_reply')" in bubbles_js
    assert "_petT('desktop_pet_action_required')" in bubbles_js
    assert "status==='action_required'" in bubbles_js
    assert "const actionType=_clean(row.action_required_type)" in bubbles_js
    assert "action_required_command:_clean(row.action_required_command)" in bubbles_js
    assert "action_required_choices:Array.isArray(row.action_required_choices)?row.action_required_choices:[]" in bubbles_js
    assert "action_required_approval_id:_clean(row.action_required_approval_id)" in bubbles_js
    assert "action_required_clarify_id:_clean(row.action_required_clarify_id)" in bubbles_js
    assert "data-action-type=" in bubbles_js
    assert "data-dismiss-key=" in bubbles_js
    assert "function _attentionQuery()" in bubbles_js
    assert "SESSION_VIEWED_COUNTS_KEY='hermes-session-viewed-counts'" in bubbles_js
    assert "SESSION_COMPLETION_UNREAD_KEY='hermes-session-completion-unread'" in bubbles_js
    assert "function _dismissKeyForRow(row,status)" in bubbles_js
    assert "function _timeAgo(epochSeconds)" in bubbles_js
    assert "function _readyMetaText(row)" in bubbles_js
    assert "function _formatElapsed(epochSeconds)" in bubbles_js
    assert "function _titleHtml(item)" in bubbles_js
    assert "function _actionRequiredText(row,actionType)" in bubbles_js
    assert "function _expandHtml(item)" in bubbles_js
    assert "function _actionPendingKey(item)" in bubbles_js
    assert "pendingActionResponses={}" in bubbles_js
    assert "function _scheduleExpandWindowSync()" in bubbles_js
    assert "expandedActionKey=''" in bubbles_js
    assert "function _setExpandedActionCard(card,expanded)" in bubbles_js
    assert "function _collapseExpandedActionCardSoon(card)" in bubbles_js
    assert "function _focusClarifyCustomInput()" in bubbles_js
    assert "_requestPetRaise(true,true)" in bubbles_js
    assert "document.activeElement.classList.contains('clarify-custom-input')" in bubbles_js
    assert "status==='ready'?_readyMetaText(row)" in bubbles_js
    assert "status==='action_required'?_actionRequiredText(row,actionType)" in bubbles_js
    assert "tooltip=status==='ready'?(_clean(row.process_text)||text):(status==='action_required'?(_clean(row.process_text)||text):text)" in bubbles_js
    assert "item.status==='running'&&startedAt>0" in bubbles_js
    assert 'class="pet-title-text"' in bubbles_js
    assert 'class="pet-elapsed" data-started-at="' in bubbles_js
    assert "document.querySelectorAll('.pet-elapsed[data-started-at]')" in bubbles_js
    assert "const expand=_expandHtml(item)" in bubbles_js
    assert "data-expanded=\"${isExpanded?'1':'0'}\"" in bubbles_js
    assert "pet-card${expand?' has-expand':''}" in bubbles_js
    assert "${expand}${item.status==='action_required'" in bubbles_js
    assert "class=\"pet-card-expand\"" in bubbles_js
    assert "class=\"expand-command\"" in bubbles_js
    assert "class=\"expand-actions\"" in bubbles_js
    assert "class=\"choice-chip\"" in bubbles_js
    assert "const longest=choices.reduce((max,choice)=>Math.max(max,_clean(choice).length),0);" in bubbles_js
    assert "const stacked=longest>16||choices.length>4;" in bubbles_js
    assert "expand-choices${stacked?' is-stacked':''}" in bubbles_js
    assert "class=\"clarify-custom\"" in bubbles_js
    assert "class=\"clarify-other\"" in bubbles_js
    assert "class=\"clarify-custom-input\"" in bubbles_js
    assert "class=\"clarify-custom-submit\"" in bubbles_js
    assert "_petT('clarify_other')" in bubbles_js
    assert "_petT('clarify_send')" in bubbles_js
    assert "_petT('clarify_input_placeholder')" in bubbles_js
    assert "_actionPendingKey(item)" in bubbles_js
    assert "clarifyDrafts={}" in bubbles_js
    assert "clarifyOtherKey=''" in bubbles_js
    assert "function _submitClarifyResponse" in bubbles_js
    assert "const otherSelected=clarifyOtherKey===pendingKey||!!clarifyDrafts[pendingKey];" in bubbles_js
    assert 'data-other-selected="${otherSelected?\'1\':\'0\'}"' in bubbles_js
    assert "${pending?'disabled':''}" in bubbles_js
    assert "if(!choices.length) return '';" in bubbles_js
    assert "desktop_pet_approve" in bubbles_js
    assert "desktop_pet_deny" in bubbles_js
    assert "desktop_pet_pick_one" in bubbles_js
    assert "target.closest('.btn-approve')" in bubbles_js
    assert "target.closest('.btn-deny')" in bubbles_js
    assert "target.closest('.choice-chip')" in bubbles_js
    assert "target.closest('.clarify-other')" in bubbles_js
    assert "clarifyOtherKey=form.dataset.pendingKey" in bubbles_js
    assert "document.querySelector('.clarify-custom[data-other-selected=\"1\"] .clarify-custom-input')" in bubbles_js
    assert "input.focus({preventScroll:true})" in bubbles_js
    assert "bubbles.addEventListener('pointerdown'" in bubbles_js
    assert "bubbles.addEventListener('mousedown'" in bubbles_js
    assert "event.target.closest('.clarify-custom-input')" in bubbles_js
    assert "if(!card.isConnected) return;" in bubbles_js
    assert "target.closest('.pet-card-expand')" in bubbles_js
    assert "bubbles.addEventListener('pointerenter'" in bubbles_js
    assert "bubbles.addEventListener('pointerleave'" in bubbles_js
    assert "bubbles.addEventListener('mouseover'" not in bubbles_js
    # Expanded action card must stay fully inside the resized native window so
    # every choice/button is hoverable and clickable, even when stacked above
    # other cards.
    assert "function _setViewportMax(px)" in bubbles_js
    assert "--pet-viewport-max" in bubbles_js
    assert "function _scrollExpandedCardIntoView()" in bubbles_js
    assert "if(expanded) _scrollExpandedCardIntoView();" in bubbles_js
    assert 'const expandedCard=list.querySelector(\'.pet-card[data-expanded="1"]\');' in bubbles_js
    assert "cap=Math.max(cap,expandedHeight+gap+Math.round(collapsedRef*0.6));" in bubbles_js
    assert "const signature=items.map(item=>`${item.session_id}~${item.status}~${item.dismissKey}`).join('|');" in bubbles_js
    assert "if(!force&&expandedActionKey&&signature===lastRenderedSignature&&typeof bubbles.matches==='function'&&bubbles.matches(':hover')) return;" in bubbles_js
    # Resolving an action must release the hover guard so the card returns to
    # running once the agent resumes (clarify/approval no longer pending).
    assert "delete pendingActionResponses[pendingKey];expandedActionKey='';refresh();" in bubbles_js
    assert "bubbles.addEventListener('focusin'" in bubbles_js
    assert "fetch('/api/approval/respond'" in bubbles_js
    assert "choice:'once'" in bubbles_js
    assert "choice:'deny'" in bubbles_js
    assert "fetch('/api/clarify/respond'" in bubbles_js
    assert "_submitClarifyResponse(chip.dataset.sid,chip.dataset.clarifyId||'',choice,pendingKey)" in bubbles_js
    assert "event.target.closest('.clarify-custom')" in bubbles_js
    assert "response:text" in bubbles_js
    assert "delete clarifyDrafts[key]" in bubbles_js
    assert "if(pendingActionResponses[key]) return;" in bubbles_js
    assert "delete pendingActionResponses[pendingKey]" in bubbles_js
    assert "function _attentionQuery()" in pet_js
    assert "SESSION_VIEWED_COUNTS_KEY='hermes-session-viewed-counts'" in pet_js
    assert "SESSION_COMPLETION_UNREAD_KEY='hermes-session-completion-unread'" in pet_js
    assert "function _dismissKeyForRow(row,status)" in pet_js
    assert "dismissed[item.dismissKey]!==true" in bubbles_js
    assert "dismissed[item.dismissKey]!==true" in pet_js
    assert "if(status==='ready') return `${sid}:ready:${Number(row&&row.message_count||0)}`;" in bubbles_js
    assert "if(status==='ready') return `${sid}:ready:${Number(row&&row.message_count||0)}`;" in pet_js
    assert "card.dataset.dismissKey" in bubbles_js
    assert "hermes-pet-viewed-counts" not in pet_js
    assert "hermes-pet-viewed-counts" not in bubbles_js
    assert "_seedViewedCounts" not in pet_js
    assert "_seedViewedCounts" not in bubbles_js
    assert "const symbol=type==='approval'?'!':'?';" in bubbles_js
    assert "_statusHtml(item)" in bubbles_js
    assert 'title="${_esc(item.tooltip||item.text)}"' in bubbles_js
    assert '.pet-card[data-status="running"] .pet-card-title{' in css
    assert ".pet-title-text{" in css
    assert ".pet-elapsed{" in css
    assert "font-variant-numeric:tabular-nums" in css
    assert '.pet-card[data-status="running"] .pet-card-text{' in css
    assert '.pet-card[data-status="ready"] .pet-card-text{' in css
    assert ".pet-card-expand{" in css
    assert '.pet-card.has-expand[data-expanded="1"]' in css
    assert "max-height:320px" in css
    assert "max-height:240px" in css
    assert "calc(100vh" not in css
    assert "overflow-y:auto" in css
    assert "overscroll-behavior:contain" in css
    assert ".pet-card.has-expand:hover" not in css
    assert ".pet-card.has-expand{max-height:74px" in css
    assert ".expand-command{" in css
    assert ".expand-actions{" in css
    assert ".btn-approve{" in css
    assert ".btn-deny{" in css
    assert ".expand-question{" in css
    assert ".expand-choices{" in css
    assert ".expand-choices.is-stacked{" in css
    assert "flex-direction:column" in css
    assert ".choice-chip{" in css
    assert "width:100%" in css
    assert "max-height:var(--pet-viewport-max,236px)" in css
    assert ".choice-chip:hover,.choice-chip:focus-visible{" in css
    assert ".clarify-custom{" in css
    assert ".clarify-other{" in css
    assert '.clarify-custom[data-other-selected="1"] .clarify-other{' in css
    assert ".clarify-custom-row{" in css
    assert ".clarify-custom-input{" in css
    assert ".clarify-custom-submit{" in css
    assert ".choice-chip:disabled,.expand-actions button:disabled,.clarify-other:disabled" in css
    assert "white-space:normal" in css
    assert "-webkit-line-clamp:1" in css
    assert "_petT('desktop_pet_sending')" in bubbles_js
    assert "_petT('desktop_pet_failed_to_send')" in bubbles_js
    assert "_petT('desktop_pet_latest')" in bubbles_js
    assert "'正在思考'" not in bubbles_js
    assert "'Ready for review'" not in bubbles_js
    assert "'Failed to send'" not in bubbles_js
    assert "请审批此会话" not in routes
    assert "需要批准" not in routes
    assert "请处理这个会话" not in routes
    assert "需要选择" not in routes
    assert "正在思考" not in routes

    assert "background:transparent" in css
    assert "--pet-width:128px;--pet-height:139px;--pet-window-width:146px;--pet-window-height:139px" in css
    assert "--pet-badge-hit-pad:8px" in css
    assert ".pet-body{width:var(--pet-window-width);height:var(--pet-window-height);cursor:grab;}" in css
    assert ".pet-shell{position:absolute;inset:0;width:var(--pet-window-width);height:var(--pet-window-height);margin:0;user-select:none;-webkit-user-select:none;pointer-events:none;}" in css
    assert ".pet-stage{position:absolute;left:0;bottom:0;width:var(--pet-width);height:var(--pet-height);border:0;padding:0;background:transparent;box-shadow:none;appearance:none;-webkit-appearance:none;cursor:grab;z-index:1;pointer-events:auto;}" in css
    assert ".pet-stage:active{cursor:grabbing;}" in css
    assert ".pet-badge:active{cursor:grabbing;}" not in css
    assert "left:calc(var(--pet-width) + var(--pet-badge-gap));top:var(--pet-badge-top);box-sizing:border-box;width:var(--pet-badge-size);min-width:var(--pet-badge-size);height:var(--pet-badge-size);padding:0" in css
    assert "z-index:10;cursor:pointer;pointer-events:auto" in css
    assert ".pet-badge::before{content:\"\";position:absolute;inset:calc(var(--pet-badge-hit-pad)*-1);border-radius:999px;pointer-events:auto;}" in css
    assert ".pet-badge.is-expanded{background:rgba(255,255,255,.96)!important;color:var(--pet-green)!important;border:1px solid rgba(8,177,83,.2);}" in css
    assert "appearance:none;-webkit-appearance:none" in css
    assert ".pet-badge svg{width:14px;height:14px;}" in css
    assert ".pet-bubbles-body{width:100%;height:100%;pointer-events:none;}" in css
    assert ".pet-install{position:absolute;inset:0;z-index:20;display:flex;align-items:center;justify-content:center;background:transparent;backdrop-filter:none;}" in css
    assert ".pet-bubbles{position:absolute;left:10px;right:10px;bottom:0;overflow:visible;z-index:3;pointer-events:auto;}" in css
    assert ".pet-bubbles-body,.pet-bubbles,.pet-viewport,.pet-list{cursor:pointer;}" in css
    assert ".pet-collapse" not in css
    assert ".pet-ready-toast" in css
    assert ".pet-welcome{position:absolute;left:10px;right:10px;bottom:0;z-index:4;pointer-events:auto;}" in css
    assert ".pet-welcome-head{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:start;}" in css
    assert ".pet-welcome-countdown{font-size:11px;line-height:1.2;font-weight:800;color:#858a9e;white-space:nowrap;}" in css
    assert ".pet-welcome-action{height:30px;border:0;border-radius:999px;background:var(--pet-green);color:#fff;font-size:13px;font-weight:800;cursor:pointer;}" in css
    assert ".pet-action-required" in css
    assert 'background:url("../pets/keeper/spritesheet.webp")' in css
    assert "-webkit-line-clamp:2" in css
    assert "max-height:34.8px" in css
    assert "overflow-wrap:anywhere" in css
    assert "word-break:break-word" in css
    assert ".pet-card{position:relative;box-sizing:border-box;width:100%;min-height:0;padding:8px 10px 9px 36px;" in css
    assert ".pet-card-main,.pet-card-title,.pet-card-text,.pet-card-status,.pet-ready,.pet-action-required,.pet-spinner{cursor:pointer;}" in css
    assert "margin:9px 1px 1px 0;cursor:default;}" in css
    assert ".pet-reply-input{min-width:0;height:32px;" in css
    assert "cursor:text;" in css
    assert ".pet-action-required.is-approval" in css
    assert ".pet-action-required.is-clarify" in css
    assert ".pet-viewport" in css
    assert "overflow-y:auto" in css
    assert ".pet-card[data-reply-open=\"1\"] .pet-reply-toggle{display:none;}" in css
    assert ".pet-card[data-reply-open=\"1\"] .pet-dismiss{display:none;}" in css
    assert ".pet-dismiss{position:absolute;left:3px;top:7px;width:24px;height:24px;" in css
    assert ".pet-dismiss svg{width:20px;height:20px;display:block;}" in css
    assert ".pet-card-opening{" in css
    assert '<button class="pet-dismiss" type="button"' in bubbles_js
    assert '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"' in bubbles_js
    assert '<line x1="20" y1="4" x2="4" y2="20"/><line x1="4" y1="4" x2="20" y2="20"/>' in bubbles_js
    assert "pet-window-resizing" not in css

    assert "./static/desktop_pet/pet.css" not in sw
    assert "./static/desktop_pet/pet.js" not in sw
    assert "./static/desktop_pet/bubbles.js" not in sw
    assert "./static/pets/courier/pet.json" not in sw
    assert "./static/pets/courier/spritesheet.webp" not in sw
    assert "./static/pets/keeper/pet.json" not in sw
    assert "./static/pets/keeper/spritesheet.webp" not in sw
    assert "./static/pets/shiba/pet.json" not in sw
    assert "./static/pets/shiba/spritesheet.webp" not in sw
    assert "./" + removed_pet_script not in sw
    assert "./static/pet_bridge.js" in sw


def test_pet_pages_use_root_absolute_assets_and_apis():
    pet_html = read("static/desktop_pet/index.html")
    bubbles_html = read("static/desktop_pet/bubbles.html")
    pet_js = read("static/desktop_pet/pet.js")
    bubbles_js = read("static/desktop_pet/bubbles.js")

    for value in _html_attr_values(pet_html, "href") + _html_attr_values(pet_html, "src"):
        if value.startswith("/static/"):
            _assert_pet_url_resolves_to_root_path("http://127.0.0.1:8787/pet", value, urlparse(value).path)
    for value in _html_attr_values(bubbles_html, "href") + _html_attr_values(bubbles_html, "src"):
        if value.startswith("/static/"):
            _assert_pet_url_resolves_to_root_path("http://127.0.0.1:8787/pet/bubbles", value, urlparse(value).path)

    assert "fetch('api/" not in pet_js
    assert "fetch('api/" not in bubbles_js
    assert "fetch('/api/pet/attention'+_attentionQuery()" in pet_js
    assert "fetch('/api/pet/attention'+_attentionQuery()" in bubbles_js
    assert "const DEFAULT_PET_LAYOUT={columns:8,rows:9,frameWidth:192,frameHeight:208" in bubbles_js
    assert "function _normalizeSkinLayout(layout)" in bubbles_js
    assert "spritesheetUrl:'/static/pets/keeper/spritesheet.webp'" in pet_js
    assert "spritesheetUrl:'/static/pets/keeper/spritesheet.webp'" in bubbles_js


def test_main_webui_pet_bridge_is_narrow():
    index = read("static/index.html")
    panels = read("static/panels.js")
    css = read("static/style.css")
    bridge = read("static/pet_bridge.js")
    sessions = read("static/sessions.js")
    config = read("api/config.py")
    routes = read("api/pet_routes.py")
    ui = read("static/ui.js")
    removed_pet_script = "static/" + "floating" + "_pet.js"
    removed_pet_setting = "floating" + "_pet_enabled"

    assert 'static/pet_bridge.js?v=__WEBUI_VERSION__' in index
    assert 'id="settingsDesktopPetEnabled"' in index
    assert 'class="settings-switch-input"' in index
    assert 'role="switch"' in index
    assert 'class="settings-switch-ui" aria-hidden="true"' in index
    assert 'onchange="toggleDesktopPetFromAppearance(this.checked)"' in index
    assert 'id="btnOpenDesktopPet"' not in index
    assert 'data-i18n="settings_label_desktop_pet_autostart"' in index
    assert 'Desktop Pet autostart' not in index
    assert 'Desktop Pet (Beta)' in index
    assert 'Show a small native companion that stays outside the browser' in index
    assert 'settings' + 'FloatingPetEnabled' not in index
    assert removed_pet_script not in index
    assert 'onclick="startDesktopPet()"' not in index
    assert 'id="desktopPetInlineStatus"' in index
    assert 'id="desktopPetSetup"' not in index
    assert 'id="desktopPetSetupLaunch"' not in index
    assert "closeDesktopPetSetup" not in index
    assert "desktop-pet-setup-overlay" not in css
    assert 'data-i18n="settings_desc_desktop_pet"' in index
    assert ".settings-switch-row{position:relative;display:flex;align-items:center;gap:10px;cursor:pointer;}" in css
    assert ".settings-switch-input{position:absolute;left:0;top:50%;transform:translateY(-50%);opacity:0;width:34px;height:18px;" in css
    assert "-webkit-appearance:none;appearance:none;border:0;background:transparent;box-shadow:none;clip-path:inset(50%);" in css
    assert ".settings-switch-ui{display:inline-block;vertical-align:middle;flex-shrink:0;width:34px;height:18px;" in css
    assert ".settings-switch-input:checked+.settings-switch-ui{background:var(--accent-bg-strong);border-color:var(--accent);}" in css
    assert ".settings-switch-input:checked+.settings-switch-ui::after{transform:translateX(16px);background:var(--accent-text);}" in css
    generic_settings_label = "#mainSettings .settings-field > label{display:block"
    pet_switch_override = "#mainSettings .settings-field > label.settings-switch-row{position:relative"
    assert generic_settings_label in css
    assert pet_switch_override in css
    assert css.index(generic_settings_label) < css.index(pet_switch_override)
    assert "async function startDesktopPet(options={})" in panels
    assert "async function launchDesktopPet(options={})" in panels
    assert "async function closeDesktopPet()" in panels
    assert "async function toggleDesktopPetFromAppearance(enabled, options={})" in panels
    assert "async function _waitForDesktopPetRunning" in panels
    assert "async function prepareDesktopPetInline(options={})" in panels
    assert "function _notifyDesktopPetSetup(options, key, duration=12000, args=[])" in panels
    assert "const DESKTOP_PET_SETUP_PROGRESS_KEYS=[" in panels
    assert "'settings_desktop_pet_setup_build_wait'" in panels
    assert "'settings_desktop_pet_setup_build_continue'" in panels
    assert "'settings_desktop_pet_setup_pick_spot'" in panels
    assert "'settings_desktop_pet_setup_link_sessions'" in panels
    assert "'settings_desktop_pet_setup_pack_bubbles'" in panels
    assert "'settings_desktop_pet_setup_polish'" in panels
    assert "'settings_desktop_pet_setup_build_finish'" in panels
    assert "key='settings_desktop_pet_setup_build_elapsed';" in panels
    assert "Math.floor((Date.now()-_desktopPetSetupProgressStartedAt)/1000)" in panels
    assert "function _startDesktopPetSetupProgress(options={})" in panels
    assert "function _stopDesktopPetSetupProgress()" in panels
    assert "_startDesktopPetSetupProgress(options);" in panels
    assert "_desktopPetSetupProgressTimer=setInterval(update,8500);" in panels
    assert "showToast(t(key,...args), duration, 'info');" in panels
    assert "api('/api/pet/status',{method:'POST',body:'{}'})" in panels
    assert "openDesktopPetSetup" not in panels
    assert "api('/api/pet/install',{method:'POST',body:'{}',timeoutMs:600000})" in panels
    assert "api('/api/pet/launch',{method:'POST',body:'{}'})" in panels
    assert "api('/api/pet/close',{method:'POST',body:'{}'})" in panels
    assert "_notifyDesktopPetSetup(options,'settings_desktop_pet_setup_load',12000);" in panels
    assert "_notifyDesktopPetSetup(options,'settings_desktop_pet_setup_starting',12000);" in panels
    assert "return await startDesktopPet(options);" in panels
    assert "return await closeDesktopPet();" in panels
    assert "api('/api/pet/preference')" in panels
    assert "api('/api/pet/preference',{method:'POST',body:JSON.stringify({enabled:!!enabled})})" in panels
    assert "window.addEventListener('storage',event=>{" in panels
    assert "if(event.key!==DESKTOP_PET_ENABLED_KEY) return;" in panels
    assert "_cacheDesktopPetPreference(event.newValue==='true')" in panels
    assert "_setDesktopPetSwitch(_desktopPetPreferenceCache,false)" in panels
    assert "let _desktopPetPreferenceRefreshAt=0" in panels
    assert "async function _refreshDesktopPetPreferenceFromServer()" in panels
    assert "if(result&&result.ok&&result.configured!==false)" in panels
    assert "_setDesktopPetSwitch(!!result.enabled,false)" in panels
    assert "window.addEventListener('focus',_scheduleDesktopPetPreferenceRefresh)" in panels
    assert "document.addEventListener('visibilitychange',()=>{if(!document.hidden)_scheduleDesktopPetPreferenceRefresh();})" in panels

    assert "def _desktop_pet_shell_source_mtime()" in routes
    assert "def _desktop_pet_candidate_is_current" in routes
    assert "_desktop_pet_launch_candidates(include_stale=True)" in routes
    assert '"stale": bool(stale_candidates) and not bool(candidates),' in routes
    assert '"source_mtime": _desktop_pet_shell_source_mtime(),' in routes
    assert '"artifact_mtime":' in routes
    assert 'return _handle_pet_page(handler)' not in routes
    assert 'return _handle_pet_attention(handler, parsed)' not in routes
    assert 'return _handle_pet_status(handler, body)' not in routes
    assert 'return True' in routes[routes.index('def handle_get'):]
    assert 'return True' in routes[routes.index('def handle_post'):]
    assert "const DESKTOP_PET_ENABLED_KEY='hermes-desktop-pet-enabled'" in panels
    assert "async function _desktopPetPreferenceEnabled()" in panels
    assert "async function _setDesktopPetPreferenceEnabled(enabled)" in panels
    assert "async function _loadDesktopPetPreference()" in panels
    assert "desktop_pet_enabled" in config
    assert 'if parsed.path == "/api/pet/preference":' in routes
    assert "settings_desktop_pet_started" in panels
    assert "settings_desktop_pet_already_running" not in panels
    assert "settings_desktop_pet_start_failed" in panels
    assert "settings_desktop_pet_setup_starting" in panels
    assert "async function maybeAutoStartDesktopPetFromPreference()" in panels
    assert "const available=_syncDesktopPetAvailabilityUi();\n  if(!available) return;" in panels
    assert "window.addEventListener('load',()=>setTimeout(()=>{maybeAutoStartDesktopPetFromPreference().catch(()=>{});},400));" in panels
    assert "function isDesktopPetAvailableOnThisDevice()" in panels
    assert 'id="settingsDesktopPetField"' in index
    assert "window.open(" not in panels
    assert "def _queue_pet_session_navigation" in routes
    assert "def _queue_and_focus_pet_session_navigation" in routes
    assert "_reuse_existing_pet_browser_tab(command.get(\"url\", \"\"))" in routes
    # Tab reuse/focus tries the recently-seen WebUI browser first instead of
    # spawning an osascript probe for every installed browser.
    assert "def _ordered_pet_browser_apps()" in routes
    assert "_ordered_pet_browser_apps()" in routes
    # A reused tab is already navigated + frontmost, so the open-session handler
    # returns without waiting on the bridge ack (avoids a ~1.6s spinner tail).
    assert 'if command.get("reused"):' in routes
    # Cold start (no live bridge polling) must not burn the ack timeout.
    assert "def _pet_bridge_recently_polled()" in routes
    assert "elif _pet_bridge_recently_polled():" in routes
    assert "command[\"reused\"] = bool(reused)" in routes
    assert 'opened = False if (consumed or command.get("reused")) else _fallback_open_pet_browser_url(str(command.get("url") or ""))' in routes
    assert "_queue_and_open_pet_session_navigation" not in routes
    assert "_open_pet_session_in_existing_browser_window" not in routes
    assert "def _open_pet_session_url" not in routes
    assert 'subprocess.Popen(["open", url])' not in routes
    assert 'subprocess.Popen(["open", "--", url]' not in routes
    assert '["osascript", "-e", script, url]' in routes
    assert "Get-CimInstance Win32_Process" in routes
    assert '["taskkill", "/PID", str(pid), "/F"]' in routes
    assert '["taskkill", "/IM", "hermes-desktop-pet.exe", "/F"]' not in routes
    assert "PET_NAVIGATION_LAST_KEY" in bridge
    assert "'/api/pet/navigation?since='" in bridge
    assert "window.__hermesApplyPetNavigationCommand(command)" in bridge
    assert "async function _ackPetNavigation(command)" in bridge
    assert "'/api/pet/navigation_ack'" in bridge
    assert "const acked=await _ackPetNavigation(command)" in bridge
    assert bridge.index("const acked=await _ackPetNavigation(command)") < bridge.index("window.__hermesApplyPetNavigationCommand(command)")
    assert "if(!acked) return;" in bridge
    assert "try{window.focus();}catch(_){}" in bridge
    assert "localStorage.setItem(PET_NAVIGATION_LAST_KEY,String(command.id))" in bridge
    assert "window.__hermesApplyPetNavigationCommand=async function(command)" in sessions
    assert "await loadSession(sid)" in sessions
    assert "await _applyExternalComposerDraft(sid, command.draft, !!command.autosend)" in sessions
    assert "url.searchParams.get('autosend')" not in sessions
    assert "void _applyExternalComposerDraft(targetSid||pathSid,draft,false)" in sessions
    assert "PET_NAVIGATION_LAST_KEY" not in sessions
    assert "function _pollPetNavigation" not in sessions
    assert removed_pet_setting not in panels
    assert "_set" + "FloatingPetEnabled" not in panels
    assert removed_pet_setting not in config
    assert "_sync" + "FloatingPetState" not in ui


def test_desktop_pet_tauri_shell_has_dynamic_skin_menu():
    config = read("desktop-pet/src-tauri/tauri.conf.json")
    capability = read("desktop-pet/src-tauri/capabilities/pet-window-drag.json")
    package = read("desktop-pet/package.json")
    cargo = read("desktop-pet/src-tauri/Cargo.toml")
    main = read("desktop-pet/src-tauri/src/main.rs")
    readme = read("desktop-pet/README.md")

    assert '"devUrl": "http://127.0.0.1:8787/pet"' in config
    assert '"label": "pet"' in config
    assert '"url": "http://127.0.0.1:8787/pet"' in config
    assert '"width": 128' in config
    assert '"height": 139' in config
    assert '"label": "pet_bubbles"' in config
    assert '"url": "http://127.0.0.1:8787/pet/bubbles"' in config
    assert '"width": 320' in config
    assert '"height": 300' in config
    assert '"visible": false' in config
    assert '"decorations": false' in config
    assert '"transparent": true' in config
    assert '"alwaysOnTop": true' in config
    assert '"skipTaskbar": true' in config
    assert '"active": true' in config
    assert '"targets": ["app"]' in config
    assert '"icons/icon.icns"' in config
    assert '"icons/icon.ico"' in config
    assert '"withGlobalTauri": true' in config
    assert '"remote"' in capability
    assert '"pet"' in capability and '"pet_bubbles"' in capability
    assert '"http://127.0.0.1:*/*"' in capability
    assert '"http://localhost:*/*"' in capability
    assert '"core:window:allow-start-dragging"' in capability
    assert '"core:window:allow-current-monitor"' in capability
    assert '"core:window:allow-available-monitors"' in capability
    assert '"core:window:allow-outer-position"' in capability
    assert '"core:window:allow-outer-size"' in capability
    assert '"core:window:allow-set-size"' in capability
    assert '"core:window:allow-set-position"' in capability
    assert '"core:window:allow-show"' in capability
    assert '"core:window:allow-hide"' in capability
    assert '"core:event:allow-emit"' in capability
    assert '"core:event:allow-listen"' in capability
    assert 'serde = { version = "1", features = ["derive"] }' in cargo
    assert 'serde_json = "1"' in cargo
    assert 'const CLOSE_PET_MENU_ID: &str = "close_pet";' in main
    assert 'const RESTART_PET_MENU_ID: &str = "restart_pet";' in main
    assert 'const PET_CONTEXT_MENU_EVENT: &str = "pet-context-menu";' in main
    assert 'const PET_SKIN_CHANGE_EVENT: &str = "pet-skin-change";' in main
    assert 'HERMES_DESKTOP_PET_WEBUI_BASE' in main
    assert 'fn desktop_pet_webui_base()' in main
    assert 'fn navigate_window_to_webui' in main
    assert 'navigate_window_to_webui(app, "pet", "/pet")' in main
    assert 'navigate_window_to_webui(app, "pet_bubbles", "/pet/bubbles")' in main
    assert "struct PetContextMenuLabels" in main
    assert "fn valid_skin_id" in main
    assert "filter_map(sanitize_skin)" in main
    assert ".filter(|id| valid_skin_id(id))" in main
    assert "if !valid_skin_id(skin_id)" in main
    assert "labels.and_then(|item| item.switch_skin.as_ref())" in main
    assert "labels.and_then(|item| item.restart_pet.as_ref())" in main
    assert "labels.and_then(|item| item.close_pet.as_ref())" in main
    assert '"Switch skin"' in main
    assert '"Restart pet"' in main
    assert '"Close pet"' in main
    assert 'SubmenuBuilder::new(&menu_handle, switch_skin_label)' in main
    assert "lower_pet_windows_for_menu(&menu_handle)" in main
    assert ".text(RESTART_PET_MENU_ID, restart_pet_label)" in main
    assert ".text(CLOSE_PET_MENU_ID, close_pet_label)" in main
    assert "切换皮肤" not in main
    assert "重启宠物" not in main
    assert "关闭宠物" not in main
    assert "window.popup_menu(&menu)" in main
    assert "restore_pet_window_layers_later(menu_handle.clone(), Duration::from_secs(12))" in main
    assert 'const PET_RESTART_REQUESTED_EVENT: &str = "pet-restart-requested";' in main
    assert 'const PET_RAISE_REQUESTED_EVENT: &str = "pet-raise-requested";' in main
    assert "fn _persist_desktop_pet_preference" in main
    assert "/api/pet/preference" in main
    assert "headers['X-Hermes-CSRF-Token']=token" in main
    assert "localStorage.setItem(key,'{enabled_text}')" in main
    assert "new StorageEvent('storage'" in main
    assert "keepalive:true" in main
    assert "fn lower_pet_windows_for_menu" in main
    assert "fn restore_pet_window_layers" in main
    assert "fn restore_pet_window_layers_later" in main
    assert "fn apply_bubble_visibility" in main
    assert "set_bubble_window_level(&bubble_window)" in main
    assert "NSStatusWindowLevel + 1" in main
    assert "set_pet_window_level(&pet_window)" in main
    assert 'append_pair("desktop_pet_pid"' in main
    assert "app.emit_to(\"pet\", PET_RESTART_REQUESTED_EVENT, ());" in main
    assert "app.listen(PET_NATIVE_RESTART_REQUESTED_EVENT" in main
    assert "app.listen(PET_RAISE_REQUESTED_EVENT" in main
    assert "bubble_window.set_always_on_top(true)" in main
    assert "apply_bubble_visibility(&control_handle, &visible_state, visible, focus)" in main
    assert "window.set_always_on_top(false)" not in main[
        main.index("app.listen(PET_RAISE_REQUESTED_EVENT") : main.index(
            'app.listen("pet-attention-update"'
        )
    ]
    attention_slice = main[
        main.index('app.listen("pet-attention-update"') : main.index(
            "let restart_requested", main.index('app.listen("pet-attention-update"')
        )
    ]
    assert "let should_hide =" in attention_slice
    assert "!visible && visible_state.lock().map(|state| !*state).unwrap_or(true);" in attention_slice
    assert "if should_hide" in attention_slice
    assert "apply_bubble_visibility(&handle_for_window, &visible_state, false, false)" in attention_slice
    assert "pet_window.set_always_on_top(false)" not in main[
        main.index('app.listen("pet-attention-update"') : main.index(
            "let restart_requested", main.index('app.listen("pet-attention-update"')
        )
    ]
    assert "app.emit_to(\"pet\", PET_SKIN_CHANGE_EVENT, skin_id.clone())" in main
    assert "app.emit_to(\"pet_bubbles\", PET_SKIN_CHANGE_EVENT, skin_id)" in main
    assert "RESTART_PET_MENU_ID => {" in main
    assert "_ = app.emit_to(\"pet\", PET_RESTART_REQUESTED_EVENT, ());" in main
    assert "app.request_restart()" not in main
    assert "CLOSE_PET_MENU_ID => {" in main
    assert "thread::sleep(Duration::from_millis(220))" in main
    assert "exit_handle.exit(0)" in main
    assert "_persist_desktop_pet_preference(&app.clone(), false)" in main
    assert 'active_skin_id: Some("keeper".into())' in main
    assert 'unwrap_or("keeper")' in main
    assert '"@tauri-apps/cli"' in package
    assert "HERMES_WEBUI_PORT=8787 ./start.sh" in readme
    assert "python3 ../server.py" not in readme


def test_desktop_pet_uses_webui_app_icon_assets():
    source_hash = hashlib.sha256(bytes_for("static/favicon-512.png")).hexdigest()
    pet_hash = hashlib.sha256(bytes_for("desktop-pet/src-tauri/icons/icon.png")).hexdigest()

    assert pet_hash == source_hash
    assert len(bytes_for("desktop-pet/src-tauri/icons/icon.icns")) > 100_000
    assert len(bytes_for("desktop-pet/src-tauri/icons/32x32.png")) > 1_000
    assert len(bytes_for("desktop-pet/src-tauri/icons/128x128.png")) > 5_000
    assert len(bytes_for("desktop-pet/src-tauri/icons/128x128@2x.png")) > 10_000


def test_desktop_pet_i18n_keys_exist_in_all_locales():
    i18n = read("static/i18n.js")
    keys = [
        "desktop_pet_title:",
        "desktop_pet_shell_label:",
        "desktop_pet_collapse_updates:",
        "desktop_pet_expand_updates:",
        "desktop_pet_thinking:",
        "desktop_pet_ready_for_review:",
        "desktop_pet_running:",
        "desktop_pet_ready:",
        "desktop_pet_action_required:",
        "desktop_pet_reply:",
        "desktop_pet_sending:",
        "desktop_pet_failed_to_send:",
        "desktop_pet_dismiss_update:",
        "desktop_pet_ready_meta_completed:",
        "desktop_pet_ready_meta_messages:",
        "desktop_pet_time_just_now:",
        "desktop_pet_time_minutes_ago:",
        "desktop_pet_time_hours_ago:",
        "desktop_pet_time_days_ago:",
        "desktop_pet_latest:",
        "desktop_pet_approve:",
        "desktop_pet_deny:",
        "desktop_pet_pick_one:",
        "desktop_pet_more_sessions_below:",
        "desktop_pet_switch_skin:",
        "desktop_pet_restart:",
        "desktop_pet_close:",
        "settings_label_desktop_pet_autostart:",
        "settings_desc_desktop_pet:",
        "settings_open_desktop_pet:",
        "settings_desktop_pet_started:",
        "settings_desktop_pet_already_running:",
        "settings_desktop_pet_start_failed:",
        "settings_desktop_pet_setup_title:",
        "settings_desktop_pet_setup_prepare:",
        "settings_desktop_pet_setup_build_wait:",
        "settings_desktop_pet_setup_build_continue:",
        "settings_desktop_pet_setup_pick_spot:",
        "settings_desktop_pet_setup_link_sessions:",
        "settings_desktop_pet_setup_pack_bubbles:",
        "settings_desktop_pet_setup_polish:",
        "settings_desktop_pet_setup_build_finish:",
        "settings_desktop_pet_setup_build_elapsed:",
        "settings_desktop_pet_setup_load:",
        "settings_desktop_pet_setup_ready:",
        "settings_desktop_pet_setup_starting:",
        "settings_desktop_pet_step_install:",
        "settings_desktop_pet_step_load:",
        "settings_desktop_pet_step_ready:",
        "settings_desktop_pet_launch_ready:",
        "desktop_pet_install_title:",
        "desktop_pet_install_check_webui:",
        "desktop_pet_install_load_skins:",
        "desktop_pet_install_ready:",
        "desktop_pet_ready_toast:",
        "desktop_pet_welcome_title:",
        "desktop_pet_welcome_countdown:",
        "desktop_pet_welcome_copy:",
        "desktop_pet_welcome_action:",
    ]
    for key in keys:
        assert i18n.count(key) == 11
    assert "desktop_pet_install_title: '正在启动桌面宠物'" in i18n
    assert "desktop_pet_install_title: '正在啟動桌面寵物'" in i18n
    assert "desktop_pet_switch_skin: '切换皮肤'" in i18n
    assert "desktop_pet_switch_skin: '切換皮膚'" in i18n
    assert "desktop_pet_approve: '✓ 批准'" in i18n
    assert "desktop_pet_deny: '✕ 拒绝'" in i18n
    assert "desktop_pet_pick_one: '请选择：'" in i18n
