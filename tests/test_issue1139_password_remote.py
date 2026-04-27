"""Tests for issue #1139 — Password change/remove broken remotely.

Root cause: the System settings pane (settingsPaneSystem) had no Save Settings button.
Users in Settings > System could type a new password but had no way to submit it.
Disable Auth and Sign Out buttons existed but Save was missing.
"""

import pytest


def test_system_pane_has_save_button():
    """The System settings pane must include a Save Settings button."""
    with open('static/index.html') as f:
        html = f.read()

    # Find the System pane block (last pane, so search until parent close)
    start = html.index('id="settingsPaneSystem"')
    # The parent container closes after all panes
    end_marker = '</div>\n      </div>\n    </div>'  # pane + panes container + settings panel
    end = html.index(end_marker, start) + len(end_marker)
    pane_block = html[start:end]

    assert 'saveSettings()' in pane_block, \
        'System pane must contain a Save Settings button (onclick="saveSettings()")'


def test_system_pane_has_password_field_and_auth_buttons():
    """System pane should have password field, Disable Auth, and Sign Out buttons."""
    with open('static/index.html') as f:
        html = f.read()

    start = html.index('id="settingsPaneSystem"')
    end_marker = '</div>\n      </div>\n    </div>'
    end = html.index(end_marker, start) + len(end_marker)
    pane_block = html[start:end]

    assert 'settingsPassword' in pane_block, 'Password field missing from System pane'
    assert 'btnDisableAuth' in pane_block, 'Disable Auth button missing from System pane'
    assert 'btnSignOut' in pane_block, 'Sign Out button missing from System pane'


def test_save_settings_sends_password():
    """saveSettings() must read settingsPassword and send _set_password."""
    with open('static/panels.js') as f:
        src = f.read()

    assert 'settingsPassword' in src, 'saveSettings should read settingsPassword'
    assert '_set_password' in src, 'saveSettings should send _set_password key'


def test_disable_auth_sends_clear_password():
    """disableAuth() must send _clear_password: true."""
    with open('static/panels.js') as f:
        src = f.read()

    assert '_clear_password:true' in src, 'disableAuth should send _clear_password: true'


def test_all_settings_panes_have_save_button():
    """All settings panes that have user-editable fields should have a Save button."""
    with open('static/index.html') as f:
        html = f.read()

    import re
    # Find all settings panes
    panes = re.findall(
        r'<div class="settings-pane" id="(settingsPane\w+)"[^>]*>(.*?)</div>\s*</div>',
        html, re.DOTALL
    )

    for pane_id, pane_html in panes:
        # Providers pane doesn't need a Save button (has per-provider save)
        if pane_id == 'settingsPaneProviders':
            continue
        # Check if pane has any input fields (not just buttons)
        has_inputs = bool(re.search(r'<input|<select|<textarea', pane_html))
        if has_inputs:
            assert 'saveSettings()' in pane_html, \
                f'{pane_id} has input fields but no Save Settings button'
