"""Issue #5671: workspace switcher and New Chat workspace announcements."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    assert start != -1, f"{start_marker!r} not found"
    end = source.find(end_marker, start)
    assert end != -1, f"{end_marker!r} not found after {start_marker!r}"
    return source[start:end]


def test_composer_workspace_switchers_expose_action_and_popup_state():
    assert 'id="composerWorkspaceChip"' in INDEX_HTML
    assert 'id="composerMobileWorkspaceAction"' in INDEX_HTML
    assert 'aria-label="Switch workspace"' in INDEX_HTML
    assert 'aria-haspopup="true"' in INDEX_HTML
    assert 'aria-expanded="false"' in INDEX_HTML
    assert 'aria-controls="composerWsDropdown"' in INDEX_HTML
    assert INDEX_HTML.count('aria-controls="composerWsDropdown"') == 2

    sync = _block(PANELS_JS, "function syncWorkspaceDisplays", "async function loadWorkspaceList")
    assert "const composerExpanded=!!(composerDropdown&&composerDropdown.classList.contains('open'))" in sync
    assert "composerChip.setAttribute('aria-label',hasWorkspace?t('workspace_switcher_aria',label):t('no_workspace'))" in sync
    assert "composerChip.setAttribute('aria-expanded',composerExpanded?'true':'false')" in sync
    assert "composerChip.classList.toggle('active',composerExpanded)" in sync
    assert "mobileAction.setAttribute('aria-label',hasWorkspace?t('workspace_switcher_aria',label):t('no_workspace'))" in sync
    assert "mobileAction.setAttribute('aria-expanded',composerExpanded?'true':'false')" in sync
    assert "mobileAction.classList.toggle('active',composerExpanded)" in sync


def test_composer_workspace_dropdown_keeps_aria_expanded_in_sync():
    toggle = _block(PANELS_JS, "function toggleComposerWsDropdown", "function closeWsDropdown")
    close = _block(PANELS_JS, "function closeWsDropdown", "document.addEventListener('click'")

    assert "chip.setAttribute('aria-expanded','true')" in toggle
    assert "mobileAction.setAttribute('aria-expanded','true')" in toggle
    assert "composerChip.setAttribute('aria-expanded','false')" in close
    assert "mobileAction.setAttribute('aria-expanded','false')" in close


def test_closed_composer_workspace_dropdown_is_removed_from_screen_reader_navigation():
    assert 'id="composerWsDropdown" hidden aria-hidden="true" inert' in INDEX_HTML

    helper = _block(PANELS_JS, "function _setWorkspaceDropdownOpenState", "function _getComposerWorkspaceFocusTarget")
    assert "dd.hidden=!open" in helper
    assert "dd.setAttribute('aria-hidden',open?'false':'true')" in helper
    assert "dd.removeAttribute('inert')" in helper
    assert "dd.setAttribute('inert','')" in helper

    toggle = _block(PANELS_JS, "function toggleComposerWsDropdown", "function closeWsDropdown")
    close = _block(PANELS_JS, "function closeWsDropdown", "document.addEventListener('click'")
    assert "_setWorkspaceDropdownOpenState(dd,true)" in toggle
    assert "_setWorkspaceDropdownOpenState(composerDd,false)" in close


def test_composer_workspace_cue_is_transient_not_a_persistent_editor_description():
    assert 'id="msg"' in INDEX_HTML
    assert 'aria-describedby="composerWorkspaceContext"' not in INDEX_HTML
    assert 'id="composerWorkspaceContext"' in INDEX_HTML
    assert 'class="sr-only"' in INDEX_HTML

    sync = _block(PANELS_JS, "function syncWorkspaceDisplays", "async function loadWorkspaceList")
    assert "composerWorkspaceContext" not in sync
    assert "workspace_context_aria" not in sync
    assert "workspace_context_none" not in sync


def test_workspace_selection_from_composer_dropdown_restores_focus_to_opening_trigger_once():
    focus_helpers = _block(PANELS_JS, "function _getComposerWorkspaceFocusTarget", "function _renderWorkspaceAction")
    switch = _block(PANELS_JS, "async function switchToWorkspace", "// ── Profile panel")

    assert "const mobileAction=(typeof $==='function')?$('composerMobileWorkspaceAction'):null" in focus_helpers
    assert "if(panel&&panel.classList.contains('open')&&mobileAction&&!mobileAction.disabled) return mobileAction" in focus_helpers
    assert "return (typeof $==='function')?$('composerWorkspaceChip'):null" in focus_helpers
    assert "function _shouldRestoreComposerWorkspaceFocus(dd)" in focus_helpers
    assert "return !!(dd&&dd.contains(active))" in focus_helpers

    assert "const restoreComposerFocusTarget=(composerDd&&composerDd.classList.contains('open')&&typeof _getComposerWorkspaceFocusTarget==='function')" in switch
    assert "_shouldRestoreComposerWorkspaceFocus(composerDd)" in switch
    assert "_focusComposerWorkspaceTarget(restoreComposerFocusTarget)" in switch
    assert switch.count("_focusComposerWorkspaceTarget(restoreComposerFocusTarget)") == 1
    assert switch.index("syncTopbar();") < switch.index("_focusComposerWorkspaceTarget(restoreComposerFocusTarget)") < switch.index("await loadDir('.')")
    assert "workspace_switched_to" in switch
    assert switch.index("_focusComposerWorkspaceTarget(restoreComposerFocusTarget)") < switch.index("workspace_switched_to")


def test_new_chat_has_screen_reader_only_workspace_announcer():
    assert 'id="a11yAnnouncer"' in INDEX_HTML
    assert 'class="sr-only"' in INDEX_HTML
    assert 'role="status"' in INDEX_HTML
    assert 'aria-live="polite"' in INDEX_HTML
    assert 'aria-atomic="true"' in INDEX_HTML
    assert 'id="a11yAnnouncer" hidden' not in INDEX_HTML
    assert 'id="a11yAnnouncer" aria-hidden="true"' not in INDEX_HTML

    assert ".sr-only{" in STYLE_CSS
    sr_only = _block(STYLE_CSS, ".sr-only{", "body{")
    assert "display:none" not in sr_only
    assert "visibility:hidden" not in sr_only
    assert "clip-path:inset(50%)" in sr_only


def test_new_session_announces_started_workspace_without_leaving_stale_browse_text():
    helper = _block(SESSIONS_JS, "function _setNewSessionWorkspaceCue", "function _setNewSessionPending")
    new_session = _block(SESSIONS_JS, "async function newSession", "/**\n * Self-heal")

    assert "const announcer=$('a11yAnnouncer')" in helper
    assert "const composerCue=$('composerWorkspaceContext')" in helper
    assert "composerCue.textContent=message" in helper
    assert "msg.setAttribute('aria-describedby',ids.join(' '))" in helper
    assert "announcer.textContent=message" in helper
    assert "setTimeout(clear,5000)" in helper
    assert "announcer.textContent=''" in helper
    assert "composerCue.textContent=''" in helper
    assert "msg.removeAttribute('aria-describedby')" in helper
    assert "getWorkspaceFriendlyName(session.workspace)" in helper
    assert "t('new_session_workspace_announce',name)" in helper
    assert "typeof requestAnimationFrame==='function'" in helper
    assert "if(typeof _announceNewSessionWorkspace==='function') _announceNewSessionWorkspace(S.session);" in new_session
    assert new_session.index("S.session=data.session") < new_session.index("_announceNewSessionWorkspace(S.session);")


def test_workspace_a11y_i18n_keys_exist_in_english_locale():
    assert "workspace_switcher_aria: 'Switch workspace. Current workspace: {0}.'" in I18N_JS
    assert "workspace_context_aria" not in I18N_JS
    assert "workspace_context_none" not in I18N_JS
    assert "new_session_workspace_announce: 'New chat started in workspace: {0}.'" in I18N_JS
