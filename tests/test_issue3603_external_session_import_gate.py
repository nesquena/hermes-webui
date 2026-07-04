"""Regression test for _isExternalSession helper covering messaging open-path (#3603).

After #3603 reclassified messaging sessions (Discord/Telegram/Slack) as
non-CLI, the session open path only triggered import_cli for `is_cli_session`
sessions, making messaging sessions click-to-open-but-can't-send.

This test pins:
- `_isExternalSession` exists in sessions.js
- The shared sidebar open helper uses `_isExternalSession` before import_cli
- The gateway-refresh gate uses `_isExternalSession`
- Main, lineage-segment, and child-session opens route through that helper
"""

import re


def _read_js():
    with open('static/sessions.js', encoding='utf-8') as f:
        return f.read()


def test_is_external_session_function_exists():
    """sessions.js must define _isExternalSession helper."""
    js = _read_js()
    assert re.search(r'function\s+_isExternalSession\s*\(', js), (
        '_isExternalSession function not found in sessions.js'
    )


def test_is_external_session_covers_messaging():
    """_isExternalSession must check both is_cli_session and _isMessagingSession."""
    js = _read_js()
    m = re.search(
        r'function\s+_isExternalSession\s*\([^)]*\)\s*\{([^}]*)\}',
        js,
        re.DOTALL,
    )
    assert m, '_isExternalSession function body not found'
    body = m.group(1)
    assert 'is_cli_session' in body, (
        '_isExternalSession must reference is_cli_session'
    )
    assert '_isMessagingSession' in body, (
        '_isExternalSession must reference _isMessagingSession'
    )


def _open_sidebar_session_body(js):
    start = js.index('async function _openSidebarSession(session, loadOpts={})')
    end = js.index('function _isReadOnlySession', start)
    return js[start:end]


def test_open_path_uses_is_external_session():
    """Shared sidebar open helper must use _isExternalSession for import gate."""
    js = _read_js()
    body = _open_sidebar_session_body(js)
    assert re.search(
        r'if\s*\(\s*_isExternalSession\s*\(\s*session\s*\)\s*\)',
        body,
    ), 'Shared sidebar open helper must use _isExternalSession(session) for import gate'
    assert "JSON.stringify(_externalImportPayload(session))" in body


def test_gateway_refresh_uses_is_external_session():
    """Gateway SSE refresh must use _isExternalSession for active-session check."""
    js = _read_js()
    # Find the gateway SSE handler block (near line 3116)
    # It should have _isExternalSession(S.session)
    assert re.search(
        r'if\s*\(\s*S\.session\s*&&\s*!S\.busy\s*&&\s*_isExternalSession\s*\(\s*S\.session\s*\)\s*\)',
        js,
    ), 'Gateway SSE refresh must use _isExternalSession(S.session)'


def test_lineage_open_uses_is_external_session():
    """Lineage segment open handler must route through shared import/open helper."""
    js = _read_js()
    body = _open_sidebar_session_body(js)
    assert re.search(
        r'if\s*\(\s*_isExternalSession\s*\(\s*session\s*\)\s*\)',
        body,
    ), 'Shared sidebar helper must keep _isExternalSession(session) import gate'
    assert "await _openSidebarSession(seg, {skipLineageResolve:true});" in js


def test_child_session_open_uses_is_external_session():
    """Child session open handler must route through shared import/open helper."""
    js = _read_js()
    body = _open_sidebar_session_body(js)
    assert re.search(
        r'if\s*\(\s*_isExternalSession\s*\(\s*session\s*\)\s*\)',
        body,
    ), 'Shared sidebar helper must keep _isExternalSession(session) import gate'
    assert "await _openSidebarSession(childSession, {skipLineageResolve:true});" in js
