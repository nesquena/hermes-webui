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


# ── Backend ────────────────────────────────────────────────────────────────────


def test_branch_endpoint_exists():
    """Verify the POST /api/session/branch route handler exists."""
    with open("api/routes.py") as f:
        src = f.read()
    assert '"POST /api/session/branch"' in src or '"/api/session/branch"' in src, (
        "Missing /api/session/branch route"
    )


def test_branch_endpoint_validates_session_id():
    """Verify the branch endpoint requires session_id."""
    with open("api/routes.py") as f:
        src = f.read()
    # Find the branch block
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src,
        re.DOTALL,
    )
    assert branch_match, "Could not find /api/session/branch handler block"
    block = branch_match.group(1)
    assert 'require(body, "session_id")' in block, (
        "Branch handler should validate session_id"
    )


def test_branch_endpoint_returns_new_session_id():
    """Verify the branch endpoint returns session_id and title."""
    with open("api/routes.py") as f:
        src = f.read()
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src,
        re.DOTALL,
    )
    assert branch_match
    block = branch_match.group(1)
    assert '"session_id"' in block, "Branch handler should return session_id"
    assert '"title"' in block, "Branch handler should return title"
    assert '"parent_session_id"' in block, (
        "Branch handler should return parent_session_id"
    )


def test_branch_creates_session_with_parent():
    """Verify the branch creates a Session with parent_session_id set."""
    with open("api/routes.py") as f:
        src = f.read()
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src,
        re.DOTALL,
    )
    assert branch_match
    block = branch_match.group(1)
    assert "parent_session_id=source.session_id" in block, (
        "Branch handler should set parent_session_id to source session"
    )


def test_branch_keep_count_support():
    """Verify the branch endpoint supports keep_count parameter."""
    with open("api/routes.py") as f:
        src = f.read()
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src,
        re.DOTALL,
    )
    assert branch_match
    block = branch_match.group(1)
    assert "keep_count" in block, "Branch handler should support keep_count"
    assert "forked_messages = source_messages[:keep_count]" in block, (
        "Branch handler should slice messages by keep_count"
    )


def test_branch_auto_title():
    """Verify fork title defaults to '<original> (fork)'."""
    with open("api/routes.py") as f:
        src = f.read()
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src,
        re.DOTALL,
    )
    assert branch_match
    block = branch_match.group(1)
    assert "(fork)" in block, "Branch handler should auto-title as '(fork)'"


# ── Session model ──────────────────────────────────────────────────────────────


def test_session_model_parent_session_id():
    """Verify Session model supports parent_session_id."""
    with open("api/models.py") as f:
        src = f.read()
    assert "parent_session_id" in src, "Session model should have parent_session_id"
    # Check __init__ parameter
    assert "parent_session_id: str=None" in src, (
        "Session.__init__ should accept parent_session_id parameter"
    )
    # Check it's set on self
    assert "self.parent_session_id = parent_session_id" in src, (
        "Session.__init__ should assign parent_session_id"
    )


def test_session_compact_includes_parent():
    """Verify compact() includes parent_session_id."""
    with open("api/models.py") as f:
        src = f.read()
    # Find the compact method and scan its full body for parent_session_id.
    # PR #1591 (May 2026) added a has_pending_user_message recompute block at
    # the top of compact() which pushed the parent_session_id field beyond a
    # 1500-char window — widen the scan to 3000 chars to cover the full
    # return-dict body without re-tightening every time compact() grows.
    compact_def_match = re.search(r"def compact\(self", src)
    assert compact_def_match, "Could not find compact() method"
    snippet = src[compact_def_match.start() : compact_def_match.start() + 3000]
    assert "'parent_session_id'" in snippet, (
        "compact() should include parent_session_id"
    )


def test_session_metadata_fields_includes_parent():
    """Verify parent_session_id is in METADATA_FIELDS for persistence."""
    with open("api/models.py") as f:
        src = f.read()
    assert "'parent_session_id'" in src, (
        "METADATA_FIELDS should include parent_session_id"
    )


# ── Frontend: slash command ────────────────────────────────────────────────────


def test_branch_slash_command_registered():
    """Verify /branch is registered as a slash command."""
    with open("static/commands.js") as f:
        src = f.read()
    assert "name:'branch'" in src, "/branch should be registered as a command"
    assert "cmdBranch" in src, "cmdBranch handler should be defined"


def test_cmdBranch_function_exists():
    """Verify cmdBranch function is defined."""
    with open("static/commands.js") as f:
        src = f.read()
    assert "async function cmdBranch(" in src, "cmdBranch should be an async function"


def test_cmdBranch_calls_branch_endpoint():
    """Verify cmdBranch calls the /api/session/branch endpoint."""
    with open("static/commands.js") as f:
        src = f.read()
    branch_fn = re.search(r"async function cmdBranch\(.*?\n\}", src, re.DOTALL)
    assert branch_fn, "Could not find cmdBranch function"
    block = branch_fn.group(0)
    assert "'/api/session/branch'" in block, "cmdBranch should call /api/session/branch"


def test_cmdBranch_switches_session():
    """Verify cmdBranch calls loadSession after branching."""
    with open("static/commands.js") as f:
        src = f.read()
    branch_fn = re.search(r"async function cmdBranch\(.*?\n\}", src, re.DOTALL)
    assert branch_fn
    block = branch_fn.group(0)
    assert "loadSession(" in block, (
        "cmdBranch should switch to the new session via loadSession"
    )


# ── Frontend: forkFromMessage ─────────────────────────────────────────────────


def test_forkFromMessage_function_exists():
    """Verify forkFromMessage function exists."""
    with open("static/commands.js") as f:
        src = f.read()
    assert "async function forkFromMessage(" in src, "forkFromMessage should be defined"


def test_forkFromMessage_passes_keep_count():
    """Verify forkFromMessage passes keep_count to the endpoint."""
    with open("static/commands.js") as f:
        src = f.read()
    fn = re.search(r"async function forkFromMessage\(.*?\n\}", src, re.DOTALL)
    assert fn
    block = fn.group(0)
    assert "keep_count" in block, (
        "forkFromMessage should pass keep_count to /api/session/branch"
    )


# ── Frontend: fork button in messages ──────────────────────────────────────────


def test_fork_button_rendered_in_ui():
    """Verify fork button is rendered in message actions."""
    with open("static/ui.js") as f:
        src = f.read()
    assert "forkBtn" in src, "forkBtn variable should exist in ui.js"
    assert "fork_from_here" in src, (
        "fork_from_here i18n key should be referenced for tooltip"
    )
    assert "forkFromMessage(" in src, "forkFromMessage should be called from the button"


def test_fork_button_in_message_actions():
    """Verify fork button is included in the msg-actions span."""
    with open("static/ui.js") as f:
        src = f.read()
    # The footHtml template should include forkBtn
    assert "${forkBtn}" in src, "forkBtn should be included in message actions template"


# ── Frontend: sidebar parent indicator ────────────────────────────────────────


def test_sidebar_parent_indicator():
    """Verify parent session indicator is rendered in session list."""
    with open("static/sessions.js") as f:
        src = f.read()
    assert "parent_session_id" in src, "sessions.js should check parent_session_id"
    assert "session-branch-indicator" in src, (
        "Should have session-branch-indicator class"
    )
    assert "li('git-branch',12)" in src, (
        "Sidebar parent indicator should use the git-branch icon"
    )
    assert "\\u2442" not in src, (
        "Sidebar parent indicator should not use the opaque OCR double-backslash glyph"
    )


def test_parent_indicator_not_clickable():
    """Verify parent indicator is informational, not hidden navigation."""
    with open("static/sessions.js") as f:
        src = f.read()
    # Find the parent indicator block
    parent_block = re.search(
        r"branch-indicator[\s\S]*?parent_session_id[\s\S]*?titleRow\.appendChild", src
    )
    assert parent_block, "Could not find parent indicator block"
    block = parent_block.group(0)
    assert "loadSession(" not in block, (
        "Parent indicator should not navigate to the parent from the sidebar"
    )
    assert "onclick" not in block, (
        "Parent indicator should not register a hidden click target"
    )


def test_parent_indicator_tooltip_uses_parent_title_fallback():
    """Tooltip should prefer a parent title and only fall back to a short id."""
    with open("static/sessions.js") as f:
        src = f.read()
    assert "function _sessionTitleForForkParent" in src, (
        "sessions.js should resolve a user-facing parent title"
    )
    assert "function _truncatedSessionId" in src, (
        "sessions.js should fall back to a truncated id, not raw session_id"
    )
    assert (
        "_sessionTitleForForkParent(s.parent_session_id)||_truncatedSessionId(s.parent_session_id)"
        in src
    ), "parent indicator tooltip must prefer title and fall back to truncated id"


def test_parent_indicator_hover_only_style():
    """The sidebar lineage indicator should be visually subdued until row hover/focus."""
    with open("static/style.css") as f:
        src = f.read()
    assert ".session-branch-indicator" in src, "Missing session branch indicator CSS"
    assert "opacity:.35" in src, "Fork lineage indicator should be subdued at rest"
    assert ".session-item:hover .session-branch-indicator" in src, (
        "Fork lineage indicator should become visible on row hover"
    )


# ── Frontend: i18n keys ────────────────────────────────────────────────────────


def test_i18n_branch_keys():
    """Verify all branch-related i18n keys exist in English locale."""
    with open("static/i18n.js") as f:
        src = f.read()
    required_keys = [
        "cmd_branch",
        "cmd_branch_usage",
        "branch_forked",
        "branch_failed",
        "fork_from_here",
        "forked_from",
    ]
    for key in required_keys:
        assert f"{key}:" in src or f"{key} :" in src, f"Missing i18n key: {key}"


# ── Frontend: icon ─────────────────────────────────────────────────────────────


def test_git_branch_icon_exists():
    """Verify git-branch icon is defined in icons.js."""
    with open("static/icons.js") as f:
        src = f.read()
    assert "'git-branch'" in src, "git-branch icon should be defined in LI_PATHS"
