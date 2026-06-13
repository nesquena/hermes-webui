"""Tests for issue #465 — session branching (/branch).

Verifies:
  1. Backend endpoint POST /api/session/branch exists in routes.py
  2. Session model supports parent_session_id field
  3. Frontend /branch slash command is registered
  4. forkFromMessage function exists in commands.js
  5. Fork button (git-branch icon) is rendered in ui.js message actions
  6. Parent session indicator uses a subtle git-branch icon in sessions.js sidebar
  7. i18n keys exist for all branch-related strings
  8. git-branch icon exists in icons.js
"""
import re
from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


# ── Backend ────────────────────────────────────────────────────────────────────

def test_branch_endpoint_exists():
    """Verify the POST /api/session/branch route handler exists."""
    src = _read('api/routes.py')
    assert '"POST /api/session/branch"' in src or '"/api/session/branch"' in src, \
        "Missing /api/session/branch route"


def test_branch_endpoint_validates_session_id():
    """Verify the branch endpoint requires session_id."""
    src = _read('api/routes.py')
    # Find the branch block
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match, "Could not find /api/session/branch handler block"
    block = branch_match.group(1)
    assert 'require(body, "session_id")' in block, \
        "Branch handler should validate session_id"


def test_branch_endpoint_returns_new_session_id():
    """Verify the branch endpoint returns session_id and title."""
    src = _read('api/routes.py')
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match
    block = branch_match.group(1)
    assert '"session_id"' in block, "Branch handler should return session_id"
    assert '"title"' in block, "Branch handler should return title"
    assert '"parent_session_id"' in block, \
        "Branch handler should return parent_session_id"


def test_branch_creates_session_with_parent():
    """Verify the branch creates a Session with parent_session_id set."""
    src = _read('api/routes.py')
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match
    block = branch_match.group(1)
    assert 'parent_session_id=source.session_id' in block, \
        "Branch handler should set parent_session_id to source session"


def test_branch_marks_explicit_forks_as_fork_sessions():
    """Explicit branches must not be mistaken for compression lineage rows."""
    src = _read('api/routes.py')
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match
    block = branch_match.group(1)
    assert 'session_source="fork"' in block, \
        "Branch handler should mark explicit forks with session_source='fork'"


def test_branch_fork_sessions_do_not_collapse_into_parent_lineage():
    """Fork sessions are not collapsed into compression-lineage; guard must remain in _sessionLineageKey."""
    src = _read('static/sessions.js')
    fn = re.search(r'function _sessionLineageKey\(.*?\n\}', src, re.DOTALL)
    assert fn, "Could not find _sessionLineageKey"
    block = fn.group(0)
    assert "if(s.session_source==='fork') return null;" in block, \
        "Fork guard must remain in _sessionLineageKey to prevent compression-lineage merging"
    assert block.index("if(s.session_source==='fork') return null;") < block.index('return s.parent_session_id || null')


def test_branch_fork_sessions_nest_under_parent():
    """Forks with a resolvable in-list parent are subgrouped via _isForkWithResolvableParent
    and fed into _attachChildSessionsToSidebarRows, not rendered as flat top-level rows."""
    src = _read('static/sessions.js')
    # Helper must exist
    assert 'function _isForkWithResolvableParent(' in src, \
        "Missing _isForkWithResolvableParent helper"
    # _attachChildSessionsToSidebarRows must check for fork children
    fn = re.search(r'function _attachChildSessionsToSidebarRows\(.*?\n\}', src, re.DOTALL)
    assert fn, "Could not find _attachChildSessionsToSidebarRows"
    block = fn.group(0)
    assert '_isForkWithResolvableParent' in block, \
        "_attachChildSessionsToSidebarRows must route fork children via _isForkWithResolvableParent"
    # _resolveSessionIdFromSidebarLineage must no longer skip fork rows wholesale
    resolve_fn = re.search(
        r'function _resolveSessionIdFromSidebarLineage\(.*?\n\}', src, re.DOTALL)
    assert resolve_fn, "Could not find _resolveSessionIdFromSidebarLineage"
    resolve_block = resolve_fn.group(0)
    assert "row.session_source==='fork'" not in resolve_block, \
        "_resolveSessionIdFromSidebarLineage must not skip fork rows; they may now be active nested children"
    assert "!_isChildSession(s)&&((s&&s.pinned)||!_isForkWithResolvableParent(s, sessionIdsInList))" in block, \
        "Only unpinned resolvable fork rows should be filtered out of the top-level rows array"


def test_branch_nested_fork_rows_keep_session_actions():
    """Nested fork rows should keep the standard session action menu path."""
    src = _read('static/sessions.js')
    assert 'session-child-session-fork' in src, \
        "Missing fork-specific nested child row path"
    assert '_openSessionActionMenu(child, menuBtn)' in src, \
        "Nested fork rows should route the standard session action menu"
    assert 'row._startRename=_buildSessionRenameStarter(child, mainBtn' in src, \
        "Nested fork rows should expose the same rename entry point as top-level rows"


def test_branch_nested_fork_search_results_auto_expand():
    """Nested fork hits should stay visible while sidebar search is active."""
    src = _read('static/sessions.js')
    assert "(_expandedChildSessionKeys.has(lineageKey)||!!searchQueryRaw)" in src, \
        "Search-active fork matches should auto-expand their nested child group"


def test_branch_nested_fork_rows_render_their_own_state_indicator():
    """Expanded fork rows should keep unread/streaming/attention affordances."""
    src = _read('static/sessions.js')
    css = _read('static/style.css')
    assert "session-state-indicator session-child-session-state" in src, \
        "Nested fork rows should render a per-row state indicator"
    assert "session-child-session-fork.streaming" in css, \
        "Nested fork rows should expose row-level streaming styling"


def test_branch_keep_count_support():
    """Verify the branch endpoint supports keep_count parameter."""
    src = _read('api/routes.py')
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match
    block = branch_match.group(1)
    assert 'keep_count' in block, "Branch handler should support keep_count"
    assert 'forked_messages = source_messages[:keep_count]' in block, \
        "Branch handler should slice messages by keep_count"


def test_branch_auto_title():
    """Verify fork title defaults to '<original> (fork)'."""
    src = _read('api/routes.py')
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match
    block = branch_match.group(1)
    assert '(fork)' in block, "Branch handler should auto-title as '(fork)'"


# ── Session model ──────────────────────────────────────────────────────────────

def test_session_model_parent_session_id():
    """Verify Session model supports parent_session_id."""
    src = _read('api/models.py')
    assert 'parent_session_id' in src, "Session model should have parent_session_id"
    # Check __init__ parameter
    assert 'parent_session_id: str=None' in src, \
        "Session.__init__ should accept parent_session_id parameter"
    # Check it's set on self
    assert 'self.parent_session_id = parent_session_id' in src, \
        "Session.__init__ should assign parent_session_id"


def test_session_compact_includes_parent():
    """Verify compact() includes parent_session_id."""
    src = _read('api/models.py')
    # Find the compact method and scan its full body for parent_session_id.
    # PR #1591 (May 2026) added a has_pending_user_message recompute block at
    # the top of compact() which pushed the parent_session_id field beyond a
    # 1500-char window — widen the scan to 3000 chars to cover the full
    # return-dict body without re-tightening every time compact() grows.
    compact_def_match = re.search(r"def compact\(self", src)
    assert compact_def_match, "Could not find compact() method"
    snippet = src[compact_def_match.start():compact_def_match.start() + 3000]
    assert "'parent_session_id'" in snippet, \
        "compact() should include parent_session_id"


def test_session_metadata_fields_includes_parent():
    """Verify parent_session_id is in METADATA_FIELDS for persistence."""
    src = _read('api/models.py')
    assert "'parent_session_id'" in src, \
        "METADATA_FIELDS should include parent_session_id"


# ── Frontend: slash command ────────────────────────────────────────────────────

def test_branch_slash_command_registered():
    """Verify /branch is registered as a slash command."""
    src = _read('static/commands.js')
    assert "name:'branch'" in src, "/branch should be registered as a command"
    assert 'cmdBranch' in src, "cmdBranch handler should be defined"


def test_cmdBranch_function_exists():
    """Verify cmdBranch function is defined."""
    src = _read('static/commands.js')
    assert 'async function cmdBranch(' in src, \
        "cmdBranch should be an async function"


def test_cmdBranch_calls_branch_endpoint():
    """Verify cmdBranch calls the /api/session/branch endpoint."""
    src = _read('static/commands.js')
    branch_fn = re.search(r'async function cmdBranch\(.*?\n\}', src, re.DOTALL)
    assert branch_fn, "Could not find cmdBranch function"
    block = branch_fn.group(0)
    assert "'/api/session/branch'" in block, \
        "cmdBranch should call /api/session/branch"


def test_cmdBranch_switches_session():
    """Verify cmdBranch calls loadSession after branching."""
    src = _read('static/commands.js')
    branch_fn = re.search(r'async function cmdBranch\(.*?\n\}', src, re.DOTALL)
    assert branch_fn
    block = branch_fn.group(0)
    assert 'loadSession(' in block, \
        "cmdBranch should switch to the new session via loadSession"


# ── Frontend: forkFromMessage ─────────────────────────────────────────────────

def test_forkFromMessage_function_exists():
    """Verify forkFromMessage function exists."""
    src = _read('static/commands.js')
    assert 'async function forkFromMessage(' in src, \
        "forkFromMessage should be defined"


def test_forkFromMessage_passes_keep_count():
    """Verify forkFromMessage passes keep_count to the endpoint."""
    src = _read('static/commands.js')
    fn = re.search(r'async function forkFromMessage\(.*?\n\}', src, re.DOTALL)
    assert fn
    block = fn.group(0)
    assert 'keep_count' in block, \
        "forkFromMessage should pass keep_count to /api/session/branch"


# ── Frontend: fork button in messages ──────────────────────────────────────────

def test_fork_button_rendered_in_ui():
    """Verify fork button is rendered in message actions."""
    src = _read('static/ui.js')
    assert "forkBtn" in src, "forkBtn variable should exist in ui.js"
    assert "fork_from_here" in src, \
        "fork_from_here i18n key should be referenced for tooltip"
    assert "forkFromMessage(" in src, \
        "forkFromMessage should be called from the button"


def test_fork_button_in_message_actions():
    """Verify fork button is included in the msg-actions span."""
    src = _read('static/ui.js')
    # The footHtml template should include forkBtn
    assert '${forkBtn}' in src, \
        "forkBtn should be included in message actions template"


# ── Frontend: sidebar parent indicator ────────────────────────────────────────

def test_sidebar_parent_indicator():
    """Verify parent session indicator is rendered in session list."""
    src = _read('static/sessions.js')
    assert 'parent_session_id' in src, \
        "sessions.js should check parent_session_id"
    assert 'session-branch-indicator' in src, \
        "Should have session-branch-indicator class"
    assert "li('git-branch',12)" in src, \
        "Sidebar parent indicator should use the git-branch icon"
    assert '\\u2442' not in src, \
        "Sidebar parent indicator should not use the opaque OCR double-backslash glyph"


def test_parent_indicator_not_clickable():
    """Verify parent indicator is informational, not hidden navigation."""
    src = _read('static/sessions.js')
    # Find the parent indicator block
    parent_block = re.search(
        r'branch-indicator[\s\S]*?parent_session_id[\s\S]*?titleRow\.appendChild',
        src
    )
    assert parent_block, "Could not find parent indicator block"
    block = parent_block.group(0)
    assert 'loadSession(' not in block, \
        "Parent indicator should not navigate to the parent from the sidebar"
    assert 'onclick' not in block, \
        "Parent indicator should not register a hidden click target"


def test_parent_indicator_tooltip_uses_parent_title_fallback():
    """Tooltip should prefer a parent title and only fall back to a short id."""
    src = _read('static/sessions.js')
    assert 'function _sessionTitleForForkParent' in src, \
        "sessions.js should resolve a user-facing parent title"
    assert 'function _truncatedSessionId' in src, \
        "sessions.js should fall back to a truncated id, not raw session_id"
    assert "_sessionTitleForForkParent(s.parent_session_id)||_truncatedSessionId(s.parent_session_id)" in src, \
        "parent indicator tooltip must prefer title and fall back to truncated id"


def test_parent_indicator_hover_only_style():
    """The sidebar lineage indicator should be visually subdued until row hover/focus."""
    src = _read('static/style.css')
    assert '.session-branch-indicator' in src, \
        "Missing session branch indicator CSS"
    assert 'opacity:.35' in src, \
        "Fork lineage indicator should be subdued at rest"
    assert '.session-item:hover .session-branch-indicator' in src, \
        "Fork lineage indicator should become visible on row hover"


# ── Frontend: i18n keys ────────────────────────────────────────────────────────

def test_i18n_branch_keys():
    """Verify all branch-related i18n keys exist in English locale."""
    src = _read('static/i18n.js')
    required_keys = [
        'cmd_branch',
        'cmd_branch_usage',
        'branch_forked',
        'branch_failed',
        'fork_from_here',
        'forked_from',
    ]
    for key in required_keys:
        assert f"{key}:" in src or f"{key} :" in src, \
            f"Missing i18n key: {key}"


# ── Frontend: icon ─────────────────────────────────────────────────────────────

def test_git_branch_icon_exists():
    """Verify git-branch icon is defined in icons.js."""
    src = _read('static/icons.js')
    assert "'git-branch'" in src, \
        "git-branch icon should be defined in LI_PATHS"
