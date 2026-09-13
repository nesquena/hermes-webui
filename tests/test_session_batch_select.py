"""Test: session batch select mode functions exist in sessions.js (#568)"""


def test_batch_select_state_variables():
    """Verify batch select state variables are declared."""
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    assert '_sessionSelectMode' in src, "Missing _sessionSelectMode variable"
    assert '_selectedSessions' in src, "Missing _selectedSessions variable"
    assert 'new Set()' in src, "Selected sessions should use Set"


def test_batch_select_functions_exist():
    """Verify all batch select functions are defined."""
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    required_funcs = [
        'toggleSessionSelectMode',
        'exitSessionSelectMode',
        'toggleSessionSelect',
        'selectAllSessions',
        'deselectAllSessions',
        '_updateBatchActionBar',
        '_renderBatchActionBar',
        '_showBatchProjectPicker',
    ]
    for fn in required_funcs:
        assert f'function {fn}(' in src, f"Missing function: {fn}"


def test_batch_select_checkbox_rendering():
    """Verify checkbox is rendered when in select mode."""
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    assert 'session-select-cb' in src, "Missing session-select-cb class"
    assert 'session-select-cb-wrapper' in src, "Missing session-select-cb-wrapper class"
    assert "cb.type='checkbox'" in src, "Checkbox should be type checkbox"


def test_batch_select_intercepts_navigation():
    """Verify select mode intercepts session navigation."""
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    assert "_sessionSelectMode" in src
    # Should have early return when in select mode
    assert 'toggleSessionSelect(s.session_id)' in src, \
        "Pointerup handler should call toggleSessionSelect in select mode"


def test_batch_checkbox_sets_selection_without_row_double_toggle():
    """Clicking the checkbox itself must not also trigger row-level toggling."""
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    assert 'function setSessionSelected(sid, selected)' in src, \
        "Checkbox changes should set explicit state instead of toggling blindly"
    assert 'cb.onchange=(e)=>{e.stopPropagation();setSessionSelected(s.session_id,cb.checked);};' in src, \
        "Checkbox change must use its checked state"
    assert 'cb.onpointerup=(e)=>{e.stopPropagation();};' in src, \
        "Checkbox pointerup must not bubble to the row pointerup handler"
    assert 'cbWrapper.onpointerup=(e)=>{e.stopPropagation();};' in src, \
        "Checkbox wrapper pointerup must not bubble to the row pointerup handler"


def test_batch_select_escape_handler():
    """Verify Escape key exits select mode."""
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    assert "e.key==='Escape'&&_sessionSelectMode" in src, \
        "Should have Escape key handler for select mode"


def test_batch_select_scope_changes_clear_stale_ids():
    """Profile/source replacements must not retain actionable IDs from another scope."""
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    assert "function _resetSessionSelectionForScopeChange()" in src
    assert "function _pruneSessionSelectionToCurrentScope(sessions,referenceSessions)" in src
    assert "_pruneSessionSelectionToCurrentScope(_allSessions,_sidebarReferenceSessions)" in src
    source_start = src.index("function _setSessionSourceFilter")
    source_filter = src[source_start:src.index("function _restoreSessionSourceFilter", source_start)]
    skeleton_start = src.index("function showSessionListSkeleton")
    profile_skeleton = src[skeleton_start:src.index("\nfunction ", skeleton_start + 1)]
    assert "_resetSessionSelectionForScopeChange()" in source_filter
    assert "_resetSessionSelectionForScopeChange()" in profile_skeleton


def test_batch_select_visible_ids_exclude_read_only_sessions():
    """Select All must not include rows that intentionally have no selection checkbox."""
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    assignment = src[src.index("_sessionVisibleSidebarIds=flatSessionRows"):]
    assignment = assignment[:assignment.index("for(const row of flatSessionRows)")]
    assert "!_isReadOnlySession(session)" in assignment


def test_batch_select_toggle_button():
    """Verify the persistent select-mode toggle is a semantic button."""
    with open('static/index.html', encoding="utf-8") as f:
        html = f.read()
    assert 'id="sessionSelectToggle"' in html, "Missing persistent session select toggle"
    assert 'class="session-select-toggle"' in html, "Missing session-select-toggle class"
    assert 'onclick="toggleSessionSelectMode()"' in html, "Toggle must enter select mode"


def test_batch_select_bar_element():
    """Verify the persistent batch action toolbar exists in Chat panel markup."""
    with open('static/index.html', encoding="utf-8") as f:
        html = f.read()
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    assert 'id="batchActionBar"' in html, "Missing batchActionBar element"
    assert 'class="batch-action-bar"' in html, "Missing batch-action-bar CSS class"
    assert 'role="toolbar"' in html, "Batch actions need toolbar semantics"
    assert 'batch-action-btn' in src, "Missing batch-action-btn class"


def test_batch_action_bar_stays_visible_at_zero_selection():
    """Select mode must retain its controls while actions are inapplicable."""
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    assert "function _updateBatchActionBar(){\n  _renderBatchActionBar();\n}" in src, \
        "Every selection change must rerender the persistent toolbar"
    assert "bar.hidden=!_sessionSelectMode" in src, \
        "Toolbar visibility should follow select mode, not selected count"
    assert "archiveBtn.disabled=count===0" in src, \
        "Archive should be disabled, not hidden, when the selection is empty"
    assert "moveBtn.disabled=count===0" in src, \
        "Move should be disabled, not hidden, when the selection is empty"
    assert "deleteBtn.disabled=count===0" in src, \
        "Delete should be disabled, not hidden, when the selection is empty"
    assert "t('session_selected_count',count)" in src, \
        "Selected count must pass the selected session count to i18n"
    assert "t('session_batch_archive_confirm',ids.length)" in src, \
        "Batch archive confirmation must pass selected session count to i18n"
    assert "t('session_batch_delete_confirm',ids.length)" in src, \
        "Batch delete confirmation must pass selected session count to i18n"


def test_batch_action_bar_is_docked_below_the_scrolling_session_list():
    """Batch controls belong to Chat panel chrome, not the list or composer."""
    with open('static/index.html', encoding="utf-8") as f:
        html = f.read()
    with open('static/sessions.js', encoding="utf-8") as f:
        js = f.read()
    with open('static/style.css', encoding="utf-8") as f:
        css = f.read()
    list_pos = html.index('id="sessionList"')
    dock_pos = html.index('id="sessionBatchDock"')
    tasks_pos = html.index('<!-- Tasks (cron) panel -->')
    assert list_pos < dock_pos < tasks_pos, \
        "Session action dock should be the session list's sibling inside Chat panel"
    assert "list.appendChild(batchBar)" not in js, \
        "List rerenders must not move the persistent dock into scrolling content"
    assert "document.body.appendChild(batchBar)" not in js, \
        "Batch action bar must not be mounted as a global footer"
    dock_css = css[css.find(".session-batch-dock{"):css.find(".session-select-toggle{")]
    assert "flex:0 0 auto" in dock_css, "Dock should reserve non-scrolling flex space"
    assert "position:fixed" not in dock_css, "Dock must stay scoped to the Chat panel"


def test_batch_project_picker_is_anchored_to_batch_actions():
    """Batch move project picker should open inside the sidebar action bar."""
    with open('static/sessions.js', encoding="utf-8") as f:
        js = f.read()
    with open('static/style.css', encoding="utf-8") as f:
        css = f.read()
    assert "const bar=$('batchActionBar');if(!bar)return;" in js, \
        "Batch project picker should anchor to the batch action bar"
    assert "picker.className='project-picker batch-project-picker'" in js, \
        "Batch project picker needs its own inline styling hook"
    assert "bar.appendChild(picker)" in js, \
        "Batch project picker should render inside the batch action bar"
    assert "document.body.appendChild(picker);picker.style.cssText='position:fixed" not in js, \
        "Batch project picker must not use the old global fixed placement"
    assert ".batch-action-bar .batch-project-picker{position:static;" in css, \
        "Batch project picker should override the shared absolute project picker style"
    assert css.find(".project-picker{") < css.find(".batch-action-bar .batch-project-picker{"), \
        "Batch project picker override must come after the shared project picker rule"


def test_streaming_zero_message_sessions_stay_visible_after_reload():
    """In-flight sessions may have zero saved messages during reload recovery."""
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    assert "_isSessionEffectivelyStreaming(s)" in src, \
        "Streaming sessions must bypass the zero-message sidebar filter"
    assert "!!s.active_stream_id" in src, \
        "Sessions with persisted active stream IDs must remain visible after reload"
    assert "!!s.pending_user_message" in src, \
        "Sessions with pending user turns must remain visible after reload"


def test_boot_does_not_drop_zero_message_inflight_session():
    """Reloading /session/<id> during a running turn must keep the session open."""
    with open('static/boot.js', encoding="utf-8") as f:
        src = f.read()
    assert "const _restoredInFlight = S.session && (" in src, \
        "Boot must detect restored in-flight sessions before ephemeral cleanup"
    assert "S.session.active_stream_id" in src, \
        "Boot must treat active stream IDs as real sessions"
    assert "S.session.pending_user_message" in src, \
        "Boot must treat pending user messages as real sessions"
    assert "&& !_restoredInFlight" in src, \
        "Zero-message cleanup must not run for in-flight sessions"


def test_batch_select_i18n_keys():
    """Verify all batch select i18n keys exist in all locales."""
    with open('static/i18n.js', encoding="utf-8") as f:
        src = f.read()
    required_keys = [
        'session_select_mode',
        'session_select_mode_desc',
        'session_select_all',
        'session_deselect_all',
        'session_selected_count',
        'session_batch_archive',
        'session_batch_delete',
        'session_batch_move',
        'session_batch_delete_confirm',
        'session_batch_archive_confirm',
        'session_no_selection',
    ]
    for key in required_keys:
        assert f"{key}:" in src, f"Missing i18n key '{key}' in i18n.js"
    # Count occurrences - each key should appear in all 7 locales
    for key in required_keys:
        count = src.count(f"{key}:")
        assert count >= 8, f"Key '{key}' found {count} times, expected >= 8 (one per locale) (one per locale)"


def test_i18n_string_placeholder_interpolation_supported():
    """String-valued translations with {0} placeholders should interpolate args."""
    with open('static/i18n.js', encoding="utf-8") as f:
        src = f.read()
    assert "String(val).replace(/\\{(\\d+)\\}/g" in src, \
        "t() must interpolate {0}-style placeholders for string-valued translations"
    assert "Object.prototype.hasOwnProperty.call(args, idx)" in src, \
        "t() must preserve unknown placeholders instead of replacing with undefined"


def test_batch_select_css_exists():
    """Verify batch select CSS classes are defined."""
    with open('static/style.css', encoding="utf-8") as f:
        src = f.read()
    required_classes = [
        'session-batch-dock',
        'session-select-toggle',
        'batch-selection-controls',
        'batch-exit-btn',
        'batch-select-all-btn',
        'session-select-cb-wrapper',
        'session-select-cb',
        'session-item.selected',
        'batch-action-bar',
        'batch-action-buttons',
        'batch-count',
        'batch-action-btn',
        'batch-action-btn-danger',
    ]
    for cls in required_classes:
        assert cls in src, f"Missing CSS class: .{cls}"


def test_batch_select_mode_flags():
    """Verify select mode properly toggles state."""
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    # toggleSessionSelectMode should flip the flag
    assert '_sessionSelectMode=!_sessionSelectMode' in src, \
        "toggleSessionSelectMode should flip _sessionSelectMode"
    # exitSessionSelectMode should clear state
    assert '_sessionSelectMode=false' in src, \
        "exitSessionSelectMode should set _sessionSelectMode=false"
    assert '_selectedSessions.clear()' in src, \
        "Exit should clear selected sessions"


def test_batch_delete_uses_confirm_dialog():
    """Verify batch delete shows confirmation dialog."""
    with open('static/sessions.js', encoding="utf-8") as f:
        src = f.read()
    # The delete handler should call showConfirmDialog with batch message
    assert "session_batch_delete_confirm" in src, \
        "Batch delete should use session_batch_delete_confirm i18n key"
    assert "showConfirmDialog" in src, \
        "Should use showConfirmDialog for batch operations"
