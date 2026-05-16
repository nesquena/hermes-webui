from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_session_menu_uses_viewport_height_not_fixed_scroll_cap():
    assert "max-height:calc(100vh - 16px)" in STYLE_CSS
    session_menu = STYLE_CSS[STYLE_CSS.find(".session-action-menu{"):STYLE_CSS.find(".session-action-menu.open")]
    assert "max-height:320px" not in session_menu


def test_session_menu_has_subtle_open_animation():
    session_menu = STYLE_CSS[STYLE_CSS.find(".session-action-menu{"):STYLE_CSS.find(".session-action-menu.open")]
    assert "will-change:opacity,transform" in session_menu
    assert "transform-origin:top right" in session_menu
    assert "function _playSessionActionMenuEntrance(menu){" in SESSIONS_JS
    assert "typeof menu.animate==='function'" in SESSIONS_JS
    assert "{opacity:0, transform:'translate3d(0,-4px,0) scale(.985)'}" in SESSIONS_JS
    assert "{duration:450, easing:'cubic-bezier(.2,.8,.2,1)'}" in SESSIONS_JS
    assert "menu.classList.add('open-animated')" in SESSIONS_JS
    assert ".session-action-menu.open-animated{animation:session-menu-in .45s cubic-bezier(.2,.8,.2,1);}" in STYLE_CSS
    assert "@keyframes session-menu-in" in STYLE_CSS
    assert "@media (prefers-reduced-motion:reduce)" in STYLE_CSS
    assert ".session-action-menu{animation:none;will-change:auto;}" in STYLE_CSS


def test_mobile_session_menu_opens_from_long_press_and_hides_dots():
    assert "_longPressDelay=400" in SESSIONS_JS
    assert "el.classList.add('long-pressing')" in SESSIONS_JS
    assert "if(!_longPressMenuOpened) el.classList.remove('long-pressing')" in SESSIONS_JS
    assert "row.classList.remove('menu-open','long-pressing')" in SESSIONS_JS
    assert "_openSessionActionMenu(s, el)" in SESSIONS_JS
    assert "@media (hover:none) and (pointer:coarse)" in STYLE_CSS
    assert ".session-actions{display:none;}" in STYLE_CSS
    assert "const _beginSessionGesture=(clientX,clientY)=>{" in SESSIONS_JS
    assert "const _scheduleSessionLongPressMenu=()=>{" in SESSIONS_JS
    mobile_touch = STYLE_CSS[STYLE_CSS.find("@media (hover:none) and (pointer:coarse)"):STYLE_CSS.find("@media (max-width: 340px)")]
    assert ".session-item{padding-right:6px;}" in mobile_touch
    assert ".session-item.streaming,.session-item.unread{padding-right:40px;}" in mobile_touch
    assert ".session-item:focus-within,.session-item.menu-open{padding-right:6px;}" in mobile_touch


def test_open_session_menu_consumes_next_row_activation():
    assert "if(_sessionActionMenu&&!_sessionActionMenu.contains(e.target)){" in SESSIONS_JS
    assert "closeSessionActionMenu();" in SESSIONS_JS
    assert "e.stopPropagation();" in SESSIONS_JS
    pointerup_idx = SESSIONS_JS.find("el.onpointerup=(e)=>{")
    dismiss_idx = SESSIONS_JS.find("if(_sessionActionMenu&&!_sessionActionMenu.contains(e.target)){", pointerup_idx)
    load_idx = SESSIONS_JS.find("await loadSession(s.session_id)", pointerup_idx)
    assert pointerup_idx > 0 and load_idx > pointerup_idx
    assert dismiss_idx > pointerup_idx and dismiss_idx < load_idx


def test_session_swipes_archive_right_and_delete_left():
    assert "_gesturePointerType!=='mouse'" in SESSIONS_JS
    assert "_swipeTracking=true" in SESSIONS_JS
    assert "const _trackHorizontalSwipe=(dx,dy)=>{" in SESSIONS_JS
    assert "_swipeActionThreshold=144" in SESSIONS_JS
    assert "_committedSwipeDuration=420" in SESSIONS_JS
    assert "const _handleSessionSwipe=(signedDx,signedDy)=>{" in SESSIONS_JS
    assert "if(_isSessionSwipeTarget()&&(_swipeTracking||Math.abs(signedDx)>Math.abs(signedDy))) _paintSessionSwipe(signedDx)" in SESSIONS_JS
    assert "if(_isSessionSwipeTarget()&&(_swipeTracking||dx>dy)) _paintSessionSwipe(signedDx)" in SESSIONS_JS
    assert "if(signedDx>0){" in SESSIONS_JS
    assert "_archiveSession(s,!s.archived)" in SESSIONS_JS
    assert "deleteSession(s.session_id,async()=>{" in SESSIONS_JS
    assert "showToast('Imported sessions cannot be deleted here.',3000);" in SESSIONS_JS
    assert "_swipeHandled=false;" in SESSIONS_JS
    assert "if(!_isDragging&&(dx>5||dy>5))" in SESSIONS_JS
    assert "const _commitSessionSwipe=()=>{" in SESSIONS_JS
    assert "_commitSessionSwipe();" in SESSIONS_JS


def test_session_swipes_show_visual_feedback_and_touch_load_clears():
    assert "const _paintSessionSwipe=(signedDx)=>{" in SESSIONS_JS
    assert "el.style.setProperty('--session-swipe-offset',offset+'px')" in SESSIONS_JS
    assert "const progress=Math.min(1,Math.abs(offset)/72)" in SESSIONS_JS
    assert "el.style.setProperty('--session-swipe-progress',Math.pow(progress,1.5))" in SESSIONS_JS
    assert "const _clearSessionSwipePaint=()=>{" in SESSIONS_JS
    assert "const _settleSessionSwipePaint=()=>{" in SESSIONS_JS
    assert "const _completeSessionSwipePaint=(signedDx)=>{" in SESSIONS_JS
    assert "el.classList.add('swipe-committed')" in SESSIONS_JS
    assert "el.style.setProperty('--session-swipe-progress','0')" in SESSIONS_JS
    assert "deleteSession(s.session_id,async()=>{" in SESSIONS_JS
    assert "const archived=await _archiveSession(s,!s.archived);" in SESSIONS_JS
    assert "if(!archived) _settleSessionSwipePaint();" in SESSIONS_JS
    assert "await new Promise(resolve=>setTimeout(resolve,_committedSwipeDuration));" in SESSIONS_JS
    assert "async function deleteSession(sid, beforeDelete=null){" in SESSIONS_JS
    assert "requestAnimationFrame(()=>requestAnimationFrame(_clearSessionSwipePaint))" in SESSIONS_JS
    assert ".session-item.swiping-right" in STYLE_CSS
    assert ".session-item.swiping-left" in STYLE_CSS
    assert "opacity:var(--session-swipe-progress,0)" in STYLE_CSS
    assert "transform:translateX(calc(-1 * var(--session-swipe-offset,0px)))" in STYLE_CSS
    assert STYLE_CSS.count("background:transparent;") >= 3
    assert "transform .5s cubic-bezier(.2,.8,.2,1)" in STYLE_CSS
    assert ".session-item.dragging.swiping-right" in STYLE_CSS
    assert ".session-item.dragging.swiping-left" in STYLE_CSS
    assert ".session-item.active.swiping-right" in STYLE_CSS
    assert ".session-item.active.swiping-left" in STYLE_CSS
    assert 'content:"Archive"' in STYLE_CSS
    assert 'content:"Delete"' in STYLE_CSS
    assert ".session-item.dragging{transition:background .15s,color .15s,box-shadow .15s ease;}" in STYLE_CSS
    assert ".session-item.swipe-committed" in STYLE_CSS
    assert "transform .42s cubic-bezier(.2,.8,.2,1)" in STYLE_CSS
    assert ".session-item.swipe-committed::after{transition:opacity .18s ease;}" in STYLE_CSS
    assert ".session-item.long-pressing" in STYLE_CSS
    assert "@keyframes session-long-press" in STYLE_CSS
    assert "transform:translateX(var(--session-swipe-offset,0))" in STYLE_CSS
    assert "finally{" in SESSIONS_JS
    assert "el.classList.remove('loading');" in SESSIONS_JS


def test_ios_touch_events_drive_session_swipes():
    assert "el.addEventListener('touchstart'" in SESSIONS_JS
    assert "el.addEventListener('touchmove'" in SESSIONS_JS
    assert "el.addEventListener('touchend'" in SESSIONS_JS
    assert "{passive:false}" in SESSIONS_JS
    assert "e.preventDefault()" in SESSIONS_JS


def test_touch_session_rows_preserve_vertical_scroll():
    assert ".session-item{padding:8px 8px;" in STYLE_CSS
    item_rule = STYLE_CSS[STYLE_CSS.find(".session-item{padding:8px 8px;"):STYLE_CSS.find("}", STYLE_CSS.find(".session-item{padding:8px 8px;"))]
    assert "touch-action:pan-y" in item_rule
    assert "user-select:none" in item_rule
    assert "-webkit-user-select:none" in item_rule
    assert "-webkit-touch-callout:none" in item_rule
