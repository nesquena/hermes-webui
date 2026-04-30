"""Test: session batch select mode functions exist in sessions.js (#568)"""
import re


def test_batch_select_state_variables():
    """Verify batch select state variables are declared."""
    with open('static/sessions.js') as f:
        src = f.read()
    assert '_sessionSelectMode' in src, "Missing _sessionSelectMode variable"
    assert '_selectedSessions' in src, "Missing _selectedSessions variable"
    assert 'new Set()' in src, "Selected sessions should use Set"


def test_batch_select_functions_exist():
    """Verify all batch select functions are defined."""
    with open('static/sessions.js') as f:
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
    with open('static/sessions.js') as f:
        src = f.read()
    assert 'session-select-cb' in src, "Missing session-select-cb class"
    assert 'session-select-cb-wrapper' in src, "Missing session-select-cb-wrapper class"
    assert "cb.type='checkbox'" in src, "Checkbox should be type checkbox"


def test_batch_select_intercepts_navigation():
    """Verify select mode intercepts session navigation."""
    with open('static/sessions.js') as f:
        src = f.read()
    assert "_sessionSelectMode" in src
    # Should have early return when in select mode
    assert 'toggleSessionSelect(s.session_id)' in src, \
        "Pointerup handler should call toggleSessionSelect in select mode"


def test_batch_select_escape_handler():
    """Verify Escape key exits select mode."""
    with open('static/sessions.js') as f:
        src = f.read()
    assert "e.key==='Escape'&&_sessionSelectMode" in src, \
        "Should have Escape key handler for select mode"


def test_batch_select_toggle_button():
    """Verify select mode toggle button is rendered."""
    with open('static/sessions.js') as f:
        src = f.read()
    assert 'session-select-toggle' in src, "Missing session-select-toggle class"
    assert 'toggleSessionSelectMode' in src, "Missing toggleSessionSelectMode call"


def test_batch_select_bar_element():
    """Verify batch action bar DOM element is created."""
    with open('static/sessions.js') as f:
        src = f.read()
    assert 'batchActionBar' in src, "Missing batchActionBar element"
    assert 'batch-action-bar' in src, "Missing batch-action-bar CSS class"
    assert 'batch-action-btn' in src, "Missing batch-action-btn class"


def test_batch_select_i18n_keys():
    """Verify all batch select i18n keys exist in all locales."""
    with open('static/i18n.js') as f:
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
    locales = ['en', 'ru', 'es', 'de', 'zh', 'zh-Hant', 'ko']
    for key in required_keys:
        for locale in locales:
            # Check if the key exists in the locale block
            if locale == 'zh-Hant':
                pattern = rf"'{locale}'\s*:.*?{key}"
            else:
                pattern = rf"{locale}\s*:.*?{key}"
            # Simpler check: just verify the key string with colon exists
            assert f"{key}:" in src, f"Missing i18n key '{key}' in i18n.js"
    # Count occurrences - each key should appear in all 7 locales
    for key in required_keys:
        count = src.count(f"{key}:")
        assert count == 7, f"Key '{key}' found {count} times, expected 7 (one per locale)"


def test_batch_select_css_exists():
    """Verify batch select CSS classes are defined."""
    with open('static/style.css') as f:
        src = f.read()
    required_classes = [
        'session-select-toggle',
        'session-select-bar',
        'batch-exit-btn',
        'batch-select-all-btn',
        'session-select-cb-wrapper',
        'session-select-cb',
        'session-item.selected',
        'batch-action-bar',
        'batch-count',
        'batch-action-btn',
        'batch-action-btn-danger',
    ]
    for cls in required_classes:
        assert cls in src, f"Missing CSS class: .{cls}"


def test_batch_select_mode_flags():
    """Verify select mode properly toggles state."""
    with open('static/sessions.js') as f:
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
    with open('static/sessions.js') as f:
        src = f.read()
    # The delete handler should call showConfirmDialog with batch message
    assert "session_batch_delete_confirm" in src, \
        "Batch delete should use session_batch_delete_confirm i18n key"
    assert "showConfirmDialog" in src, \
        "Should use showConfirmDialog for batch operations"
