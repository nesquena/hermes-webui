from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def _compressed_listener_block() -> str:
    src = _read("static/messages.js")
    start = src.find("source.addEventListener('compressed'")
    assert start != -1, "compressed SSE listener not found"
    end = src.find("source.addEventListener('metering'", start)
    assert end != -1, "metering listener after compressed SSE listener not found"
    return src[start:end]


def _compressing_listener_block() -> str:
    src = _read("static/messages.js")
    start = src.find("source.addEventListener('compressing'")
    assert start != -1, "compressing SSE listener not found"
    end = src.find("source.addEventListener('compressed'", start)
    assert end != -1, "compressed listener after compressing SSE listener not found"
    return src[start:end]


def test_auto_compression_running_sse_uses_active_session_running_card():
    block = _compressing_listener_block()

    assert "if(!S.session||S.session.session_id!==activeSid) return;" in block
    assert "if(d.session_id&&d.session_id!==activeSid) return;" in block
    assert "try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }" in block
    assert "setCompressionUi" in block
    assert "phase:'running'" in block
    assert "automatic:true" in block
    assert "message:d.message||'Auto-compressing context...'" in block


def test_auto_compression_running_sse_is_emitted_from_agent_lifecycle_status():
    src = _read("api/streaming.py")
    start = src.find("def _agent_status_callback")
    assert start != -1, "agent status callback bridge not found"
    end = src.find("# Initialised here", start)
    assert end != -1, "status callback block end marker not found"
    block = src[start:end]

    assert "put('compressing'" in block
    assert "'session_id': session_id" in block
    assert "'message': 'Auto-compressing context to continue...'" in block
    assert "'preflight compression'" in block
    assert "'compressing'" in block
    assert "'compacting context'" in block
    assert "'context too large'" in block
    assert "'status_callback' in _agent_params" in src
    assert "_agent_kwargs['status_callback'] = _agent_status_callback" in src
    assert "agent.status_callback = _agent_kwargs.get('status_callback')" in src


def test_auto_compression_completion_transition_is_preserved_after_running_listener():
    src = _read("static/messages.js")
    compressing_idx = src.find("source.addEventListener('compressing'")
    compressed_idx = src.find("source.addEventListener('compressed'")
    assert compressing_idx != -1 and compressed_idx != -1
    assert compressing_idx < compressed_idx
    assert "phase:'done'" in _compressed_listener_block()


def test_auto_compression_sse_uses_transient_card_not_fake_message():
    """Auto compression must not inject display-only text into S.messages."""
    src = _read("static/messages.js")
    block = _compressed_listener_block()

    assert "*[Context was auto-compressed to continue the conversation]*" not in src
    assert "S.messages.push" not in block
    assert "setCompressionUi" in block
    assert "phase:'done'" in block
    assert "automatic:true" in block
    assert "_setCompressionSessionLock" in block


def test_auto_compression_sse_keeps_inactive_and_malformed_paths_safe():
    block = _compressed_listener_block()

    guard = "if(!S.session||S.session.session_id!==activeSid) return;"
    assert guard in block
    assert block.index(guard) < block.index("setCompressionUi")
    assert "try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }" in block


def test_auto_compression_card_reuses_compression_card_renderer():
    src = _read("static/ui.js")
    start = src.find("function _autoCompressionCardsHtml")
    assert start != -1, "auto compression card helper not found"
    end = src.find("function _compressionCardsNode", start)
    assert end != -1, "compression cards node helper not found after auto helper"
    helper = src[start:end]

    assert "if(state.automatic) return _autoCompressionCardsHtml(state);" in src
    assert "tool-card-row compression-card-row" in helper
    assert "tool-card-compress-complete tool-card-compress-auto" in helper
    assert "auto_compress_label" in helper


def test_auto_compression_card_survives_compression_session_rotation():
    src = _read("static/messages.js")

    assert "window._compressionUi.sessionId===activeSid" in src
    assert "sessionId:d.session.session_id" in src


def test_preserved_task_list_marker_is_detected_case_insensitively():
    src = _read("static/ui.js")
    marker = "[your active task list was preserved across context compression]"
    start = src.find("function _isPreservedCompressionTaskListMessage")
    assert start != -1, "preserved task list detector not found"
    end = src.find("function _preservedCompressionTaskListPreview", start)
    assert end != -1, "preserved task list preview helper not found after detector"
    detector = src[start:end]

    assert "m.role!=='user'" in detector
    assert marker.strip("[]") in detector.lower()
    assert ".test(text)" in detector
    assert "/i.test" in detector


def test_context_compaction_marker_is_detected_across_roles():
    src = _read("static/ui.js")
    start = src.find("function _isContextCompactionMessage")
    assert start != -1, "context compaction detector not found"
    end = src.find("function _isPreservedCompressionTaskListMessage", start)
    assert end != -1, "preserved task list detector not found after context detector"
    detector = src[start:end]

    assert "m.role==='tool'" in detector
    assert "m.role!=='assistant'" not in detector
    assert "[context compaction" in detector.lower()
    assert "context compaction" in detector.lower()


def test_context_compaction_branch_precedes_user_bubble_branch():
    src = _read("static/ui.js")
    loop_start = src.find("for(let vi=0;vi<visWithIdx.length;vi++)")
    assert loop_start != -1, "message render loop not found"
    loop_end = src.find("if(!currentAssistantTurn)", loop_start)
    assert loop_end != -1, "assistant render branch not found after context branch"
    render_prefix = src[loop_start:loop_end]

    context_idx = render_prefix.find("if(_isContextCompactionMessage(m))")
    user_idx = render_prefix.find("if(isUser)")
    assert context_idx != -1, "context compaction render branch not found"
    assert user_idx != -1, "normal user bubble render branch not found"
    assert context_idx < user_idx
    assert "_contextCompactionMessageHtml(m, tsTitle, preservedForThisCard)" in render_prefix


def test_preserved_task_list_skips_normal_visible_message_path():
    src = _read("static/ui.js")

    visible_filter_start = src.find("const vis=S.messages.filter")
    assert visible_filter_start != -1, "visible message filter not found"
    visible_filter_end = src.find("$('emptyState')", visible_filter_start)
    assert visible_filter_end != -1, "empty state update after visible filter not found"
    visible_filter = src[visible_filter_start:visible_filter_end]
    assert "if(_isContextCompactionMessage(m)) return false;" in visible_filter
    assert "if(_isPreservedCompressionTaskListMessage(m)) return false;" in visible_filter

    vis_idx_start = src.find("for(const m of S.messages)", visible_filter_end)
    assert vis_idx_start != -1, "raw message index loop not found"
    vis_idx_end = src.find("let lastUserRawIdx", vis_idx_start)
    assert vis_idx_end != -1, "last user index lookup after raw message loop not found"
    vis_idx_loop = src[vis_idx_start:vis_idx_end]
    assert "if(_isPreservedCompressionTaskListMessage(m))" in vis_idx_loop
    assert "preservedCompressionRawIdxs.push(rawIdx)" in vis_idx_loop
    assert "continue;" in vis_idx_loop


def test_preserved_task_list_renders_through_compression_card_path():
    src = _read("static/ui.js")
    start = src.find("function _preservedCompressionTaskListCardHtml")
    assert start != -1, "preserved task list card helper not found"
    end = src.find("function _preservedCompressionTaskListCardsHtml", start)
    assert end != -1, "preserved task list card list helper not found"
    helper = src[start:end]

    assert "_compressionStatusCardHtml" in helper
    assert "preserved_task_list_label" in helper
    assert "tool-card-compress-reference" in helper
    assert "data-compression-card=\"1\"" in helper
    assert "li('list-todo',13)" in helper
    assert "_contextCompactionMessageHtml(m, tsTitle, preservedForThisCard)" in src


def test_preserved_task_list_attaches_once_per_render():
    src = _read("static/ui.js")

    assert "function _latestPreservedCompressionTaskListMessages" in src
    assert ".reverse().find(m=>_isPreservedCompressionTaskListMessage(m))" in src
    assert "const preservedCompressionTaskMessages=_latestPreservedCompressionTaskListMessages(S.messages);" in src
    assert "S.messages.filter(m=>_isPreservedCompressionTaskListMessage(m))" not in src
    assert "let preservedCompressionTaskCardsAttached=!!referenceNode;" in src
    assert "const preservedForThisCard=preservedCompressionTaskCardsAttached?[]:preservedCompressionTaskMessages;" in src
    assert "if(preservedForThisCard.length) preservedCompressionTaskCardsAttached=true;" in src
    assert "(!preservedCompressionTaskCardsAttached&&(!referenceMessage||compressionState)&&preservedCompressionTaskMessages.length)" in src


def test_preserved_task_list_is_suppressed_when_latest_todo_state_has_no_active_items():
    src = _read("static/ui.js")
    start = src.find("function _latestTodoToolItems")
    assert start != -1, "latest todo state helper not found"
    end = src.find("function _isSameLocalDay", start)
    assert end != -1, "preserved-task-list helper block end not found"
    helpers = src[start:end]

    assert "if(payload&&Array.isArray(payload.todos)) return payload.todos;" in helpers
    assert "function _hasActiveTodoItems" in helpers
    assert "status==='pending'||status==='in_progress'" in helpers
    assert "if(Array.isArray(latestTodos) && !_hasActiveTodoItems(latestTodos)) return [];" in helpers


def test_preserved_task_list_rendering_does_not_mutate_history():
    src = _read("static/ui.js")
    start = src.find("function _isPreservedCompressionTaskListMessage")
    assert start != -1, "preserved task list detector not found"
    end = src.find("function _isSameLocalDay", start)
    assert end != -1, "end of preserved task list render helpers not found"
    preserved_helpers = src[start:end]

    assert "S.messages" not in preserved_helpers
    assert ".splice(" not in preserved_helpers
    assert "delete " not in preserved_helpers
