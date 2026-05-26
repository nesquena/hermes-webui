from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PET_HTML = (ROOT / "static" / "desktop_pet" / "index.html").read_text(encoding="utf-8")
PET_JS = (ROOT / "static" / "desktop_pet" / "pet.js").read_text(encoding="utf-8")
BUBBLES_JS = (ROOT / "static" / "desktop_pet" / "bubbles.js").read_text(encoding="utf-8")
BUBBLES_CSS = (ROOT / "static" / "desktop_pet" / "pet.css").read_text(encoding="utf-8")
MAIN_RS = (ROOT / "desktop-pet" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")


def _matching_span(source: str, start: int, opener: str, closer: str) -> tuple[int, int]:
    depth = 0
    for idx in range(start, len(source)):
        ch = source[idx]
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return start, idx + 1
    raise AssertionError(f"unterminated span starting at {start}")


def _function_body(source: str, name: str) -> str:
    start = -1
    for prefix in (f"function {name}", f"async function {name}", f"fn {name}"):
        search_from = 0
        while True:
            candidate = source.find(prefix, search_from)
            if candidate < 0:
                break
            next_char_idx = candidate + len(prefix)
            if next_char_idx < len(source) and source[next_char_idx] == "(":
                start = candidate
                break
            search_from = candidate + 1
        if start >= 0:
            break
    if start < 0:
        raise AssertionError(f"missing function {name}")
    params_start = source.find("(", start)
    assert params_start >= 0, f"missing function params for {name}"
    _, params_end = _matching_span(source, params_start, "(", ")")
    brace = source.find("{", params_end)
    assert brace >= 0, f"missing function body for {name}"
    _, body_end = _matching_span(source, brace, "{", "}")
    return source[brace:body_end]


def _compact(source: str) -> str:
    return "".join(source.split())


def test_context_menu_source_contract_is_not_blocked_by_drag_regions_or_topmost_windows():
    assert "data-tauri-drag-region" not in PET_HTML
    assert "document.addEventListener('contextmenu',_openPetContextMenu)" in PET_JS

    context_menu = _function_body(PET_JS, "_openPetContextMenu")
    assert "event.preventDefault()" in context_menu
    assert "tauri.event.emit('pet-context-menu'" in context_menu

    lower = _function_body(MAIN_RS, "lower_pet_windows_for_menu")
    assert 'for label in ["pet", "pet_bubbles"]' in lower
    assert "set_always_on_top(false)" in lower

    restore = _function_body(MAIN_RS, "restore_pet_window_layers")
    assert 'get_webview_window("pet_bubbles")' in restore
    assert "bubble_window.set_always_on_top(true)" in restore
    assert "set_bubble_window_level(&bubble_window)" in restore
    assert 'get_webview_window("pet")' in restore
    assert "pet_window.set_always_on_top(false)" in restore
    assert "pet_window.set_always_on_top(true)" in restore
    assert "set_pet_window_level(&pet_window)" in restore

    lower_idx = MAIN_RS.index("lower_pet_windows_for_menu(&menu_handle)")
    popup_idx = MAIN_RS.index("window.popup_menu(&menu)")
    restore_idx = MAIN_RS.index("restore_pet_window_layers_later(menu_handle.clone(), Duration::from_secs(12))")
    assert lower_idx < popup_idx < restore_idx


def test_bubble_layer_source_contract_keeps_visible_bubble_above_pet():
    raise_listener_idx = MAIN_RS.index("app.listen(PET_RAISE_REQUESTED_EVENT")
    attention_listener_idx = MAIN_RS.index('app.listen("pet-attention-update"')
    setup_slice = MAIN_RS[MAIN_RS.index('get_webview_window("pet_bubbles")') : raise_listener_idx]
    raise_slice = MAIN_RS[raise_listener_idx:attention_listener_idx]
    attention_slice = MAIN_RS[attention_listener_idx : MAIN_RS.index("let restart_requested", attention_listener_idx)]

    assert "bubble_window.set_always_on_top(true)" in setup_slice
    assert "apply_bubble_visibility(&control_handle, &visible_state, visible, focus)" in raise_slice
    assert "set_bubble_window_level(&bubble_window)" in MAIN_RS
    assert "NSStatusWindowLevel + 1" in MAIN_RS
    assert "install_bubble_first_click_handler(&bubble_window)" in MAIN_RS
    assert "sel!(sendEvent:)" in MAIN_RS
    assert "sel!(canBecomeKeyWindow)" in MAIN_RS
    assert "sel!(canBecomeMainWindow)" in MAIN_RS
    assert "makeKeyWindow" in MAIN_RS
    assert "acceptsFirstMouse:" not in MAIN_RS
    assert "let _ = window.set_always_on_top(false)" not in raise_slice
    assert "let _ = window.set_always_on_top(true)" not in raise_slice
    assert "let should_hide =" in attention_slice
    assert "!visible && visible_state.lock().map(|state| !*state).unwrap_or(true);" in attention_slice
    assert "if should_hide" in attention_slice
    assert "apply_bubble_visibility(&handle_for_window, &visible_state, false, false)" in attention_slice
    assert "pet_window.set_always_on_top(false)" not in attention_slice
    assert "pet_window.set_always_on_top(true)" not in attention_slice


def test_bubble_window_source_contract_focuses_once_when_becoming_visible_for_first_click():
    request_body = _function_body(BUBBLES_JS, "_requestPetRaise")
    sync_body = _function_body(BUBBLES_JS, "_syncBubbleWindow")
    schedule_body = _function_body(BUBBLES_JS, "_scheduleBubbleSync")
    raise_listener_idx = MAIN_RS.index("app.listen(PET_RAISE_REQUESTED_EVENT")
    attention_listener_idx = MAIN_RS.index('app.listen("pet-attention-update"')
    raise_slice = MAIN_RS[raise_listener_idx:attention_listener_idx]

    assert "focus" in request_body
    assert "visibleMode==='hidden'||bubbleWindowSyncMode" in schedule_body
    assert "setTimeout(run,0)" in schedule_body
    assert "visibleMode==='hidden'" in sync_body
    assert "const shouldFocusBubble=visibleMode==='hidden'" in sync_body
    assert "_requestPetRaise(true,shouldFocusBubble)" in sync_body
    # Re-show/re-raise must be gated so a stationary 1Hz layout tick does not
    # re-order the native window and flicker a focused text input (clarify Other).
    assert "if(needReposition||shouldFocusBubble){" in _compact(sync_body)
    assert "apply_bubble_visibility(&control_handle, &visible_state, visible, focus)" in raise_slice
    apply_visibility = _function_body(MAIN_RS, "apply_bubble_visibility")
    assert "bubble_window.set_focus()" in apply_visibility
    assert "payload.focus" in raise_slice
    assert "bubble_window.set_focus()" not in MAIN_RS[
        attention_listener_idx : MAIN_RS.index("let restart_requested", attention_listener_idx)
    ]


def test_bubble_hidden_state_source_contract_removes_click_interception():
    assert ".pet-bubbles-body{width:100%;height:100%;pointer-events:none;}" in BUBBLES_CSS
    assert ".pet-bubbles{position:absolute;left:10px;right:10px;bottom:0;overflow:visible;z-index:3;pointer-events:auto;}" in BUBBLES_CSS

    sync_body = _function_body(BUBBLES_JS, "_syncBubbleWindow")
    hidden_branch = sync_body[sync_body.index("if(mode==='hidden')") : sync_body.index("const desired=_desiredWindowSize(mode)")]
    no_position_branch = sync_body[sync_body.index("if(!pos)") : sync_body.index("const logicalHeight=pos.height")]
    assert "win.hide()" in hidden_branch
    assert "_requestPetRaise(false)" in hidden_branch
    assert "win.hide()" in no_position_branch
    assert "win.show()" in sync_body
    assert "_requestPetRaise(true,shouldFocusBubble)" in sync_body

    attention_start = MAIN_RS.index('app.listen("pet-attention-update"')
    attention_end = MAIN_RS.index("let restart_requested", attention_start)
    attention_slice = MAIN_RS[attention_start:attention_end]
    assert "let should_hide =" in attention_slice
    assert "!visible && visible_state.lock().map(|state| !*state).unwrap_or(true);" in attention_slice
    assert "if should_hide" in attention_slice
    assert "apply_bubble_visibility(&handle_for_window, &visible_state, false, false)" in attention_slice
    apply_visibility = _function_body(MAIN_RS, "apply_bubble_visibility")
    assert "bubble_window.set_ignore_cursor_events(!visible)" in apply_visibility
    assert "bubble_window.hide()" in apply_visibility


def test_bubble_position_source_contract_uses_physical_coordinates_and_logical_window_size():
    position_body = _function_body(BUBBLES_JS, "_bubblePosition")
    sync_body = _function_body(BUBBLES_JS, "_syncBubbleWindow")

    assert "const scale=_coordinateScale(layout,monitor)" in position_body
    assert "const desired=_positionWindowSize(windowSize,scale)" in position_body
    assert "const inset=BUBBLE_SIDE_INSET*scale" in position_body
    horizontal_body = _function_body(BUBBLES_JS, "_horizontalPosition")
    assert "letx=pet.x+pet.width-(width-inset)" in _compact(horizontal_body)
    assert "constx=_horizontalPosition(pet,monitor,desired.width,margin,scale)" in _compact(position_body)
    assert "height:height/scale" in _compact(position_body)
    assert "new Ctor(Math.round(x),Math.round(y))" in BUBBLES_JS
    assert "_logicalSize(desired.width,logicalHeight)" in sync_body
    # Bubble positioning uses LOGICAL points (via _bubblePositionArg) so the
    # window lands on the correct monitor when the pet sits on an external
    # display whose scale factor differs from the built-in screen. Physical
    # coordinates are ambiguous across mixed-scale monitors.
    assert "const arg=_bubblePositionArg(pos)" in _compact(sync_body) or "constarg=_bubblePositionArg(pos)" in _compact(sync_body)
    arg_body = _function_body(BUBBLES_JS, "_bubblePositionArg")
    assert "_logicalPosition(pos.x/scale,pos.y/scale)" in _compact(arg_body)
    assert "_physicalPosition(pos.x,pos.y)" in arg_body


def test_drag_tracking_source_contract_keeps_bubble_following_pet_during_move():
    drag_body = _function_body(PET_JS, "_startTauriWindowDrag")
    start_tracking = _function_body(PET_JS, "_startDragLayoutTracking")
    stop_tracking = _function_body(PET_JS, "_stopDragLayoutTracking")
    emit_body = _function_body(PET_JS, "_emitPetLayout")
    drag_emit_body = _function_body(PET_JS, "_emitPetDragLayout")
    schedule_body = _function_body(BUBBLES_JS, "_scheduleBubbleSync")
    bubble_sync_body = _function_body(BUBBLES_JS, "_syncBubbleWindow")
    native_drag_body = _function_body(BUBBLES_JS, "_nativeDragFollowEnabled")
    bubble_drag_body = _function_body(BUBBLES_JS, "_applyDraggingBubblePosition")

    assert "_startDragLayoutTracking();" in drag_body
    assert "win.startDragging()" in drag_body
    assert "dragLayoutTrackUntil=Date.now()+12000" in start_tracking
    assert "_emitPetDragLayout();" in start_tracking
    assert "dragLayoutTrackDirty=true" in drag_emit_body
    assert "_emitPetLayout({dragging:true})" in drag_emit_body
    assert "const dragging=!!(options&&options.dragging)" in emit_body
    assert "dragging?latestPetMonitor:await _monitorForWindow(win,tauriGeo)" in emit_body
    assert "dragging?tauriGeo" in emit_body
    assert "_clampPetWindowToMonitor" not in emit_body[emit_body.index("const nextGeo=") :]
    assert "requestAnimationFrame(tick)" in start_tracking
    assert "cancelAnimationFrame(dragLayoutTrackFrame)" in stop_tracking
    assert "_emitPetLayoutBurst()" in stop_tracking
    assert "latestPetLayout&&latestPetLayout.dragging" in schedule_body
    assert "setTimeout(run,0)" in schedule_body
    assert "if(latestPetLayout&&latestPetLayout.dragging&&visibleMode!=='hidden')" in bubble_sync_body
    assert "if(_nativeDragFollowEnabled()) return" in bubble_sync_body
    assert "tauri&&tauri.event&&typeof tauri.event.emit==='function'" in native_drag_body
    assert "return _applyDraggingBubblePosition(win,mode)" in bubble_sync_body
    assert "bubblePositionInFlight=true" in bubble_drag_body
    assert "pendingBubblePosition" in bubble_drag_body
    # Drag-follow also positions via logical points (cross-monitor correctness).
    assert "win.setPosition(arg)" in bubble_drag_body
    assert "_bubblePositionArg(next)" in bubble_drag_body
    assert "win.show()" not in bubble_drag_body
    assert "_requestPetRaise" not in bubble_drag_body
    assert "attach_bubble_child_window(&pet_window, &bubble_window)" in MAIN_RS
    assert "addChildWindow_ordered" in MAIN_RS
    assert "NSWindowOrderingMode::Above" in MAIN_RS
    assert "WindowEvent::Moved" not in MAIN_RS
    assert "window.addEventListener('mouseup',_stopDragLayoutTracking,{capture:true})" in PET_JS
    assert "window.addEventListener('pointerup',_stopDragLayoutTracking,{capture:true})" in PET_JS
    assert "window.addEventListener('blur',_stopDragLayoutTracking)" in PET_JS


def test_monitor_selection_rejects_wrong_current_monitor_for_external_display():
    monitor_body = _function_body(PET_JS, "_monitorForWindow")
    current_slice = monitor_body[
        monitor_body.index("try{if(win&&typeof win.currentMonitor==='function')")
        : monitor_body.index("const monitors=await _availableMonitorsList(win)")
    ]

    assert "const cx=geo?geo.x+geo.width/2:null, cy=geo?geo.y+geo.height/2:null" in monitor_body
    assert "if(monitor&&(!geo||_monitorContainsPoint(monitor,cx,cy)))returnmonitor" in _compact(current_slice)
    assert "constcontains=monitors.find(item=>_monitorContainsPoint(item,cx,cy))" in _compact(monitor_body)


def test_completed_sessions_source_contract_remain_visible_until_user_opens_or_dismisses_them():
    # Completed work must not disappear just because the user is on another WebUI tab.
    # The pet sends WebUI's completion-unread state to the attention endpoint,
    # renders ready sessions with a green check, and only clears the unread marker
    # when the session is opened from the pet bubble.
    pet_query = _function_body(PET_JS, "_attentionQuery")
    bubble_query = _function_body(BUBBLES_JS, "_attentionQuery")
    assert "SESSION_COMPLETION_UNREAD_KEY" in pet_query
    assert "SESSION_COMPLETION_UNREAD_KEY" in bubble_query
    assert "completion_unread" in pet_query
    assert "completion_unread" in bubble_query

    status_body = _function_body(BUBBLES_JS, "_statusHtml")
    assert "status==='running'" in status_body
    assert "pet-ready" in status_body
    assert "polyline points=\"20 6 9 17 4 12\"" in status_body
    assert ".pet-ready{width:18px;height:18px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:var(--pet-green);color:#fff;}" in BUBBLES_CSS

    open_body = _function_body(BUBBLES_JS, "_openSession")
    mark_viewed_body = _function_body(BUBBLES_JS, "_markViewed")
    dismiss_opened_body = _function_body(BUBBLES_JS, "_dismissOpenedReadySession")
    assert "await _openSessionInBrowser(sid)" in open_body
    assert "_openSessionSucceeded(result)" in open_body
    assert open_body.index("await _openSessionInBrowser(sid)") < open_body.index("_dismissOpenedReadySession(sid)")
    assert "if(status!=='action_required') _markViewed(sid)" in open_body
    assert "if(status==='ready') _dismissOpenedReadySession(sid)" in open_body
    assert "if(status==='ready') _hideOpenedReadySession(sid)" in open_body
    assert "render(true)" in open_body
    assert "dismissed[_dismissKeyForRow(row,'ready')]=true" in dismiss_opened_body
    assert "_writeJson(DISMISSED_KEY,dismissed)" in dismiss_opened_body
    assert "sessions=sessions.filter(item=>!(item.session_id===sid&&item.status==='ready'))" in BUBBLES_JS
    assert "SESSION_COMPLETION_UNREAD_KEY" in mark_viewed_body
    assert "delete unread[sid]" in mark_viewed_body
    assert "_writeJson(SESSION_COMPLETION_UNREAD_KEY,unread)" in mark_viewed_body
