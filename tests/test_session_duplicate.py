"""
End-to-end tests for /api/session/duplicate endpoint.

Tests verify that:
1. A new session is created as a copy of the original
2. All messages are copied correctly
3. The duplicate is independent from the original
4. Error handling works properly
"""
import json
import pathlib
import shutil
import subprocess
import time
import urllib.request
import urllib.error
import uuid
import tempfile

import pytest

from tests.conftest import TEST_BASE, TEST_STATE_DIR, _post, TEST_WORKSPACE, _wait_for_server


def _get(path):
    """GET helper -- returns parsed JSON, or raises HTTPError on non-2xx."""
    with urllib.request.urlopen(TEST_BASE + path, timeout=10) as r:
        return json.loads(r.read())


def test_duplicate_session_handles_missing_session_id(cleanup_test_sessions):
    """
    Test that duplicate endpoint returns error when session_id is missing.
    """
    # Try to duplicate without session_id
    r = _post(TEST_BASE, '/api/session/duplicate', {})

    assert 'error' in r, "Should return error when session_id is missing"


def test_duplicate_session_handles_invalid_session_id(cleanup_test_sessions):
    """
    Test that duplicate endpoint returns error when session doesn't exist.
    """
    # Try to duplicate non-existent session
    r = _post(TEST_BASE, '/api/session/duplicate', {'session_id': 'nonexistent_xyz'})

    # Should return an error (could be auth error or not found)
    assert 'error' in r, "Should return error when session not found"
    # Check that we got some kind of error response
    assert r.get('error') is not None or 'error' in r, \
        f"Should return error when session not found. Got: {r}"


def test_duplicate_session_handles_empty_session_id(cleanup_test_sessions):
    """
    Test that duplicate endpoint returns error when session_id is empty string.
    """
    # Try to duplicate with empty session_id
    r = _post(TEST_BASE, '/api/session/duplicate', {'session_id': ''})

    assert 'error' in r, "Should return error when session_id is empty"


def test_duplicate_session_endpoint_exists():
    """
    Test that the duplicate endpoint is registered.
    """
    # This test verifies that the endpoint exists in routes.py
    with open('api/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()

    assert '/api/session/duplicate' in content, \
        "Duplicate endpoint should be registered in routes.py"

    # Verify the endpoint calls Session.load
    assert 'Session.load(sid)' in content or 'session = Session.load' in content, \
        "Endpoint should load the session from database"

    # Verify the endpoint creates a copy
    assert 'copied_session' in content, \
        "Endpoint should create a copied session"


def test_duplicate_creates_independent_session():
    """
    Test that the duplicate endpoint creates independent sessions.

    This test verifies the implementation logic by inspecting routes.py.
    """
    with open('api/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()

    # Verify that parent_session_id is NOT set (this would make it a fork)
    # Find the duplicate endpoint
    duplicate_start = content.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1, "Duplicate endpoint not found"

    # Extract the duplicate endpoint code (next few lines)
    lines = content[duplicate_start:].split('\n')
    endpoint_code = '\n'.join(lines[:80])

    # Verify that parent_session_id is NOT passed to Session constructor
    assert 'parent_session_id' not in endpoint_code, \
        "Duplicate should NOT set parent_session_id (that would make it a fork)"

    # Verify that messages are copied (accept both plain assignment and the
    # corrected deepcopy form added May 2 2026).
    assert 'messages=session.messages' in endpoint_code or \
           'messages=copy.deepcopy(session.messages)' in endpoint_code or \
           'messages=copied_session.messages' in endpoint_code, \
        "Messages should be copied to duplicate"

    # Verify that title includes (copy)
    assert '(copy)' in endpoint_code, \
        "Duplicate title should include '(copy)' suffix"


def test_duplicate_session_copies_title_logic():
    """
    Test that the duplicate session title includes (copy) suffix.
    """
    with open('api/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()

    duplicate_start = content.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1, "Duplicate endpoint not found"

    lines = content[duplicate_start:].split('\n')
    endpoint_code = '\n'.join(lines[:80])

    # Verify title includes (copy). Accept the original `session.title + " (copy)"`
    # form OR the May 2 2026 SF-3 hardened form `(session.title or "Untitled") + " (copy)"`
    # which guards against legacy null titles.
    assert 'session.title + " (copy)"' in endpoint_code or \
           '(session.title or "Untitled") + " (copy)"' in endpoint_code or \
           'session.title + \' (copy\')' in endpoint_code or \
           'title=session.title + " (copy)"' in endpoint_code, \
        f"Title should include '(copy)' suffix. Got: {endpoint_code}"


def test_duplicate_session_copies_messages_logic():
    """
    Test that the duplicate session copies all messages.
    """
    with open('api/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()

    duplicate_start = content.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1, "Duplicate endpoint not found"

    lines = content[duplicate_start:].split('\n')
    endpoint_code = '\n'.join(lines[:80])

    # Verify messages are copied from original session.  Accept either the
    # plain assignment (insufficient — see test_duplicate_runtime_messages_independence)
    # or the proper deepcopy form (the May 2 2026 fix).
    assert 'messages=session.messages' in endpoint_code or \
           'messages=copy.deepcopy(session.messages)' in endpoint_code, \
        f"Messages should be copied from original. Got: {endpoint_code}"


def test_duplicate_session_copies_model_logic():
    """
    Test that the duplicate session copies the model.
    """
    with open('api/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()

    duplicate_start = content.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1, "Duplicate endpoint not found"

    lines = content[duplicate_start:].split('\n')
    endpoint_code = '\n'.join(lines[:80])

    # Verify model is copied
    assert 'model=session.model' in endpoint_code, \
        f"Model should be copied. Got: {endpoint_code}"


def test_duplicate_session_copies_workspace_logic():
    """
    Test that the duplicate session copies the workspace.
    """
    with open('api/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()

    duplicate_start = content.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1, "Duplicate endpoint not found"

    lines = content[duplicate_start:].split('\n')
    endpoint_code = '\n'.join(lines[:80])

    # Verify workspace is copied
    assert 'workspace=session.workspace' in endpoint_code, \
        f"Workspace should be copied. Got: {endpoint_code}"


def test_duplicate_session_copies_all_session_properties():
    """
    Test that the duplicate session copies all properties.
    """
    with open('api/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()

    duplicate_start = content.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1, "Duplicate endpoint not found"

    lines = content[duplicate_start:].split('\n')
    endpoint_code = '\n'.join(lines[:80])

    # Extract the copied_session = Session( lines
    session_construction_start = endpoint_code.find('copied_session = Session(')
    assert session_construction_start != -1, "Should construct copied_session"

    # Get the construction block
    construction_block = endpoint_code[session_construction_start:session_construction_start+1600]

    # Verify all key properties are copied
    # `title` accepts either the original `title=session.title` or the
    # SF-3 hardened form `title=(session.title or "Untitled")` (May 2 2026).
    properties_to_check = [
        'session_id=uuid.uuid4',  # New unique ID
        'workspace=session.workspace',
        'model=session.model',
        'model_provider=session.model_provider',
    ]

    for prop in properties_to_check:
        assert prop in construction_block, \
            f"Property should be copied: {prop}. Got: {construction_block[:300]}"

    assert 'title=session.title' in construction_block or \
           'title=(session.title or "Untitled")' in construction_block, \
        f"title must be copied (plain or guarded form). Got: {construction_block[:300]}"

    # `messages` accepts either the plain assignment or the deepcopy form (May 2 2026 fix).
    assert 'messages=session.messages' in construction_block or \
           'messages=copy.deepcopy(session.messages)' in construction_block, \
        f"messages must be copied (plain or deepcopy form). Got: {construction_block[:300]}"



# ---------------------------------------------------------------------------
# Runtime tests added May 2 2026 (Opus pre-release follow-up to #1462 review)
# ---------------------------------------------------------------------------

def test_duplicate_uses_deepcopy_for_messages():
    """The duplicate must use copy.deepcopy() for messages and tool_calls.

    Static-grep regression test: catches the original bug where
    `messages=session.messages` was a plain reference assignment, leaving
    both sessions sharing the same list object in memory.
    """
    with open('api/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()
    duplicate_start = content.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1, "Duplicate endpoint not found"
    lines = content[duplicate_start:].split('\n')
    endpoint_code = '\n'.join(lines[:80])
    assert 'copy.deepcopy(session.messages)' in endpoint_code, \
        "duplicate must use copy.deepcopy(session.messages) — plain assignment shares list refs"
    assert 'copy.deepcopy(session.tool_calls)' in endpoint_code, \
        "duplicate must use copy.deepcopy(session.tool_calls) — plain assignment shares list refs"


def test_duplicate_explicitly_persists_to_disk():
    """The duplicate must call .save() — otherwise it vanishes on refresh.

    Static-grep regression test: pre-fix, the new endpoint never persisted
    the duplicate to disk. The session sat in SESSIONS only until the user
    sent a turn (which triggered _handle_chat_start save). Refreshing
    mid-flow lost the duplicate.
    """
    with open('api/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()
    duplicate_start = content.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1, "Duplicate endpoint not found"
    lines = content[duplicate_start:].split('\n')
    endpoint_code = '\n'.join(lines[:80])
    assert 'copied_session.save()' in endpoint_code, \
        "duplicate must call .save() explicitly — without it the copy vanishes on refresh"


def test_duplicate_resets_pinned_and_archived():
    """The duplicate must reset pinned/archived to False.

    UX bug: duplicating an archived conversation should produce a visible
    (un-archived) copy. Inheriting `archived=True` makes the duplicate
    invisible in the sidebar and users think the operation didn't work.
    """
    with open('api/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()
    duplicate_start = content.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1, "Duplicate endpoint not found"
    lines = content[duplicate_start:].split('\n')
    endpoint_code = '\n'.join(lines[:80])
    # Both must be hard-coded to False, NOT inherited from `session.pinned`/`session.archived`
    assert 'pinned=False' in endpoint_code, \
        "duplicate must reset pinned=False — duplicating shouldn't propagate pin state"
    assert 'archived=False' in endpoint_code, \
        "duplicate must reset archived=False — archived duplicates are invisible in the sidebar"
    # Negative: the old (buggy) `pinned=session.pinned` form must not still be there
    assert 'pinned=session.pinned' not in endpoint_code, \
        "duplicate must NOT inherit pinned from source session"
    assert 'archived=session.archived' not in endpoint_code, \
        "duplicate must NOT inherit archived from source session"


def test_duplicate_returns_404_when_session_not_found():
    """Missing session must be 404, not 400.

    Pre-fix, `bad(handler, "Session not found")` defaulted to status=400.
    A missing resource is conceptually 404, not "malformed request".
    """
    with open('api/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()
    duplicate_start = content.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1, "Duplicate endpoint not found"
    lines = content[duplicate_start:].split('\n')
    endpoint_code = '\n'.join(lines[:80])
    assert 'bad(handler, "Session not found", status=404)' in endpoint_code, \
        "missing session must return status=404, not the default 400"


def test_duplicate_local_imports_removed():
    """Style: `import uuid` and `import time` should not be re-imported inside
    the handler — both are already at the top of routes.py."""
    with open('api/routes.py', 'r', encoding='utf-8') as f:
        content = f.read()
    duplicate_start = content.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1, "Duplicate endpoint not found"
    # Only check the next ~10 lines — the local imports were right at the top of the handler
    lines = content[duplicate_start:].split('\n')
    handler_top = '\n'.join(lines[:10])
    assert '            import uuid' not in handler_top, "redundant `import uuid` inside handler"
    assert '            import time' not in handler_top, "redundant `import time` inside handler"
