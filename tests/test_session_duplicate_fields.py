"""Tests for session field propagation on duplicate and branch.

Verifies that critical Session fields are copied when duplicating or
branching a session, preventing data loss scenarios like:
  - truncation_watermark not carried → merge with state.db drops messages
  - context_messages not carried → agent sees different context
  - gateway_routing not carried → routing customization lost
  - enabled_toolsets not carried → toolset override lost
  - context_engine_state not carried → context engine state lost

These are static-analysis tests (source inspection) matching the
conventions of the existing test suite.
"""
import re


def _extract_duplicate_block():
    """Extract the duplicate handler block from routes.py."""
    with open('api/routes.py') as f:
        src = f.read()
    match = re.search(
        r'parsed\.path == "/api/session/duplicate"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert match, "Could not find /api/session/duplicate handler block"
    return match.group(1), src


def _extract_branch_block():
    """Extract the branch handler block from routes.py."""
    with open('api/routes.py') as f:
        src = f.read()
    match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert match, "Could not find /api/session/branch handler block"
    return match.group(1), src


def _find_session_ctor(block, var_name='copied_session'):
    """Find the Session() constructor call in a block and return its body."""
    # Match from "var_name = Session(" to the closing ")"
    pattern = rf'{var_name}\s*=\s*Session\((.*?)\)\s*$'
    match = re.search(pattern, block, re.DOTALL)
    assert match, f"Could not find {var_name} = Session() constructor"
    return match.group(1)


def _has_field(ctor_body, field_name):
    """Check if a field is present in the constructor body."""
    return f'{field_name}=' in ctor_body


def _has_deepcopy_for(ctor_body, field_name):
    """Check if a field uses deepcopy in the constructor."""
    # Find the line containing the field assignment
    lines = ctor_body.split('\n')
    for line in lines:
        if f'{field_name}=' in line:
            return 'deepcopy' in line
    return False


# ── Duplicate: critical fields MUST be copied ────────────────────────────────

def test_duplicate_copies_truncation_watermark():
    """truncation_watermark must be copied to prevent state.db merge from
    dropping messages after edit in a duplicated session (#2914)."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'truncation_watermark'), \
        "Duplicate must copy truncation_watermark"


def test_duplicate_copies_truncation_boundary():
    """truncation_boundary must be copied alongside truncation_watermark so
    empty-sidecar recovery in the duplicate can distinguish legitimate prefix
    from deleted suffix."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'truncation_boundary'), \
        "Duplicate must copy truncation_boundary"


def test_duplicate_copies_context_messages():
    """context_messages is the authoritative model-facing prefix. Without it,
    the duplicate's agent context diverges from what the user sees."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'context_messages'), \
        "Duplicate must copy context_messages"
    assert _has_deepcopy_for(ctor, 'context_messages'), \
        "context_messages must be deepcopied (mutable list)"


def test_duplicate_copies_gateway_routing():
    """gateway_routing customization must survive duplication."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'gateway_routing'), \
        "Duplicate must copy gateway_routing"
    assert _has_deepcopy_for(ctor, 'gateway_routing'), \
        "gateway_routing must be deepcopied (mutable dict)"


def test_duplicate_copies_gateway_routing_history():
    """gateway_routing_history must survive duplication."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'gateway_routing_history'), \
        "Duplicate must copy gateway_routing_history"
    assert _has_deepcopy_for(ctor, 'gateway_routing_history'), \
        "gateway_routing_history must be deepcopied (mutable list)"


def test_duplicate_copies_cache_tokens():
    """Cache token counters should be preserved for accurate usage tracking."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'cache_read_tokens'), \
        "Duplicate must copy cache_read_tokens"
    assert _has_field(ctor, 'cache_write_tokens'), \
        "Duplicate must copy cache_write_tokens"


def test_duplicate_copies_enabled_toolsets():
    """Per-session toolset override must survive duplication."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'enabled_toolsets'), \
        "Duplicate must copy enabled_toolsets"


def test_duplicate_copies_llm_title_generated():
    """llm_title_generated flag should be preserved to avoid regenerating title."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'llm_title_generated'), \
        "Duplicate must copy llm_title_generated"


def test_duplicate_copies_composer_draft():
    """Per-session composer draft should be preserved."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'composer_draft'), \
        "Duplicate must copy composer_draft"
    assert _has_deepcopy_for(ctor, 'composer_draft'), \
        "composer_draft must be deepcopied (mutable dict)"


def test_duplicate_copies_context_engine():
    """Context engine must be preserved so duplicate's context engine starts correctly."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'context_engine'), \
        "Duplicate must copy context_engine"
    assert _has_field(ctor, 'context_engine_state'), \
        "Duplicate must copy context_engine_state"
    assert _has_deepcopy_for(ctor, 'context_engine_state'), \
        "context_engine_state must be deepcopied (mutable dict)"


def test_duplicate_copies_model_provider():
    """model_provider must survive duplication."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'model_provider'), \
        "Duplicate must copy model_provider"


def test_duplicate_copies_personality():
    """personality must survive duplication."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'personality'), \
        "Duplicate must copy personality"


def test_duplicate_copies_project_id():
    """project_id must survive duplication."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'project_id'), \
        "Duplicate must copy project_id"


def test_duplicate_copies_profile():
    """profile must survive duplication."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'profile'), \
        "Duplicate must copy profile"


def test_duplicate_copies_context_length():
    """context_length must survive duplication."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'context_length'), \
        "Duplicate must copy context_length"


def test_duplicate_copies_threshold_tokens():
    """threshold_tokens must survive duplication."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'threshold_tokens'), \
        "Duplicate must copy threshold_tokens"


def test_duplicate_copies_workspace():
    """workspace must survive duplication."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'workspace'), \
        "Duplicate must copy workspace"


def test_duplicate_copies_model():
    """model must survive duplication."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'model'), \
        "Duplicate must copy model"


def test_duplicate_copies_usage_counters():
    """Token usage counters must be preserved for accurate billing tracking."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_field(ctor, 'input_tokens'), \
        "Duplicate must copy input_tokens"
    assert _has_field(ctor, 'output_tokens'), \
        "Duplicate must copy output_tokens"
    assert _has_field(ctor, 'estimated_cost'), \
        "Duplicate must copy estimated_cost"


# ── Duplicate: mutable fields MUST use deepcopy ──────────────────────────────

def test_duplicate_deepcopies_messages():
    """messages must be deepcopied to prevent shared list mutation."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_deepcopy_for(ctor, 'messages'), \
        "messages must be deepcopied (mutable list of dicts)"


def test_duplicate_deepcopies_tool_calls():
    """tool_calls must be deepcopied to prevent shared list mutation."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert _has_deepcopy_for(ctor, 'tool_calls'), \
        "tool_calls must be deepcopied (mutable list)"


# ── Duplicate: intentionally NOT copied (ephemeral / re-derived) ────────────

def test_duplicate_resets_pinned():
    """Pinned status should NOT transfer to duplicate."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert 'pinned=False' in ctor, \
        "Duplicate should reset pinned to False"


def test_duplicate_resets_archived():
    """Archived status should NOT transfer to duplicate."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert 'archived=False' in ctor, \
        "Duplicate should reset archived to False"


def test_duplicate_does_not_copy_compression_anchor():
    """Compression anchor fields are intentionally NOT carried — they
    re-derive on the next turn (per existing comment)."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert not _has_field(ctor, 'compression_anchor_visible_idx'), \
        "compression_anchor_visible_idx should NOT be copied (re-derives)"
    assert not _has_field(ctor, 'compression_anchor_message_key'), \
        "compression_anchor_message_key should NOT be copied (re-derives)"
    assert not _has_field(ctor, 'compression_anchor_summary'), \
        "compression_anchor_summary should NOT be copied (re-derives)"
    assert not _has_field(ctor, 'compression_anchor_details'), \
        "compression_anchor_details should NOT be copied (re-derives)"
    assert not _has_field(ctor, 'compression_anchor_mode'), \
        "compression_anchor_mode should NOT be copied (re-derives)"
    assert not _has_field(ctor, 'compression_anchor_engine'), \
        "compression_anchor_engine should NOT be copied (re-derives)"


def test_duplicate_does_not_copy_worktree():
    """Worktree fields must NOT transfer — they are per-session-instance."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert not _has_field(ctor, 'worktree_path'), \
        "worktree_path should NOT be copied (per-instance)"
    assert not _has_field(ctor, 'worktree_branch'), \
        "worktree_branch should NOT be copied (per-instance)"
    assert not _has_field(ctor, 'worktree_repo_root'), \
        "worktree_repo_root should NOT be copied (per-instance)"


def test_duplicate_does_not_copy_last_prompt_tokens():
    """last_prompt_tokens is intentionally NOT carried — re-derives on next turn."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert not _has_field(ctor, 'last_prompt_tokens'), \
        "last_prompt_tokens should NOT be copied (re-derives)"


def test_duplicate_does_not_copy_ephemeral():
    """Ephemeral fields (active_stream_id, pending_*) must NOT transfer."""
    block, _ = _extract_duplicate_block()
    ctor = _find_session_ctor(block)
    assert not _has_field(ctor, 'active_stream_id'), \
        "active_stream_id should NOT be copied (ephemeral)"
    assert not _has_field(ctor, 'pending_user_message'), \
        "pending_user_message should NOT be copied (ephemeral)"
    assert not _has_field(ctor, 'pending_attachments'), \
        "pending_attachments should NOT be copied (ephemeral)"
    assert not _has_field(ctor, 'pending_started_at'), \
        "pending_started_at should NOT be copied (ephemeral)"


# ── Branch: critical fields MUST be copied ──────────────────────────────────

def test_branch_copies_model_provider():
    """Branch must inherit model_provider from source."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert _has_field(ctor, 'model_provider'), \
        "Branch must copy model_provider"


def test_branch_copies_project_id():
    """Branch must inherit project_id from source."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert _has_field(ctor, 'project_id'), \
        "Branch must copy project_id"


def test_branch_copies_personality():
    """Branch must inherit personality from source."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert _has_field(ctor, 'personality'), \
        "Branch must copy personality"


def test_branch_copies_enabled_toolsets():
    """Branch must inherit enabled_toolsets from source."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert _has_field(ctor, 'enabled_toolsets'), \
        "Branch must copy enabled_toolsets"


def test_branch_copies_context_messages():
    """Branch must deep-copy context_messages for independent context."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert _has_field(ctor, 'context_messages'), \
        "Branch must copy context_messages"
    assert _has_deepcopy_for(ctor, 'context_messages'), \
        "context_messages must be deepcopied in branch"


def test_branch_copies_gateway_routing():
    """Branch must inherit gateway_routing from source."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert _has_field(ctor, 'gateway_routing'), \
        "Branch must copy gateway_routing"
    assert _has_deepcopy_for(ctor, 'gateway_routing'), \
        "gateway_routing must be deepcopied in branch"


def test_branch_copies_context_length():
    """Branch must inherit context_length from source."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert _has_field(ctor, 'context_length'), \
        "Branch must copy context_length"


def test_branch_copies_threshold_tokens():
    """Branch must inherit threshold_tokens from source."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert _has_field(ctor, 'threshold_tokens'), \
        "Branch must copy threshold_tokens"


def test_branch_copies_context_engine():
    """Branch must inherit context_engine state from source."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert _has_field(ctor, 'context_engine'), \
        "Branch must copy context_engine"
    assert _has_field(ctor, 'context_engine_state'), \
        "Branch must copy context_engine_state"
    assert _has_deepcopy_for(ctor, 'context_engine_state'), \
        "context_engine_state must be deepcopied in branch"


# ── Branch: intentionally NOT copied ────────────────────────────────────────

def test_branch_does_not_copy_compression_anchor():
    """Branch should not copy compression anchor fields (re-derive)."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert not _has_field(ctor, 'compression_anchor_visible_idx'), \
        "compression_anchor_visible_idx should NOT be copied in branch"


def test_branch_does_not_copy_truncation_watermark():
    """truncation_watermark must NOT be copied in branch — the branch starts
    with a fresh message slice and watermark from the original session would
    cause state.db merge to incorrectly filter messages in the branch."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert not _has_field(ctor, 'truncation_watermark'), \
        "truncation_watermark should NOT be copied in branch"


def test_branch_does_not_copy_truncation_boundary():
    """truncation_boundary must NOT be copied in branch — same reasoning as
    truncation_watermark: the branch is a fresh message slice with no truncate
    history of its own."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert not _has_field(ctor, 'truncation_boundary'), \
        "truncation_boundary should NOT be copied in branch"


def test_branch_does_not_copy_usage_counters():
    """Branch starts a new conversation — token counters must NOT carry over."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert not _has_field(ctor, 'input_tokens'), \
        "input_tokens should NOT be copied in branch"
    assert not _has_field(ctor, 'output_tokens'), \
        "output_tokens should NOT be copied in branch"
    assert not _has_field(ctor, 'estimated_cost'), \
        "estimated_cost should NOT be copied in branch"


def test_branch_does_not_copy_tool_calls():
    """Branch must NOT inherit tool_calls from source."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert not _has_field(ctor, 'tool_calls'), \
        "tool_calls should NOT be copied in branch"


def test_branch_does_not_copy_gateway_routing_history():
    """Branch starts its own routing history."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert not _has_field(ctor, 'gateway_routing_history'), \
        "gateway_routing_history should NOT be copied in branch"


def test_branch_sets_session_source():
    """Branch must set session_source='fork' for lineage tracking."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert 'session_source=' in ctor, \
        "Branch must set session_source"
    assert '"fork"' in ctor or "'fork'" in ctor, \
        "session_source must be 'fork'"


def test_branch_does_not_copy_ephemeral():
    """Branch should not copy ephemeral fields."""
    block, _ = _extract_branch_block()
    ctor = _find_session_ctor(block, 'branch')
    assert not _has_field(ctor, 'active_stream_id'), \
        "active_stream_id should NOT be copied in branch"
    assert not _has_field(ctor, 'pending_user_message'), \
        "pending_user_message should NOT be copied in branch"
