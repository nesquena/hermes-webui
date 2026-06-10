from pathlib import Path

from api.compression_anchor import visible_messages_for_anchor
from api.models import Session
from api.streaming import (
    _is_fallback_lifecycle_message,
    _prune_context_tool_results_after_compression,
)


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


def test_post_compression_context_prunes_tail_tool_results_with_active_compressor():
    class FakeCompressor:
        protect_last_n = 20
        tail_token_budget = 4096

        def __init__(self):
            self.calls = []

        def _prune_old_tool_results(self, messages, protect_tail_count, protect_tail_tokens=None):
            self.calls.append(
                {
                    "protect_tail_count": protect_tail_count,
                    "protect_tail_tokens": protect_tail_tokens,
                }
            )
            out = []
            pruned = 0
            for msg in messages:
                next_msg = dict(msg)
                if next_msg.get("role") == "tool" and len(str(next_msg.get("content") or "")) > 200:
                    next_msg["content"] = "[browser_navigate] opened page (large snapshot summarized)"
                    pruned += 1
                out.append(next_msg)
            return out, pruned

    compressor = FakeCompressor()
    agent = type("Agent", (), {"context_compressor": compressor})()
    context_messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_big"}]},
        {"role": "tool", "tool_call_id": "call_big", "content": "x" * 5000},
        {"role": "assistant", "content": "Final answer"},
    ]

    pruned = _prune_context_tool_results_after_compression(agent, context_messages)

    assert compressor.calls == [{"protect_tail_count": 20, "protect_tail_tokens": 4096}]
    assert pruned[1]["content"] == "[browser_navigate] opened page (large snapshot summarized)"
    assert context_messages[1]["content"] == "x" * 5000


def test_auto_compression_running_sse_uses_active_session_running_card():
    block = _compressing_listener_block()

    assert "if(!S.session||S.session.session_id!==activeSid) return;" in block
    assert "if(d.session_id&&d.session_id!==activeSid) return;" in block
    assert "try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }" in block
    assert "setCompressionUi" in block
    assert "phase:'running'" in block
    assert "automatic:true" in block
    assert "message:'Compressing context'" in block
    assert "message:d.message||'Compressing context'" not in block


def test_agent_status_callback_emits_compressing_and_warning_events():
    src = _read("api/streaming.py")
    start = src.find("def _agent_status_callback")
    assert start != -1, "agent status callback bridge not found"
    end = src.find("# Initialised here", start)
    assert end != -1, "status callback block end marker not found"
    block = src[start:end]

    # compressing events for compression lifecycle notices
    assert "put('compressing'" in block
    assert "'session_id': session_id" in block
    assert "'message': 'Compressing context'" in block
    assert "'preflight compression'" in block
    assert "'compressing'" in block
    assert "'compacting context'" in block
    assert "'context too large'" in block

    # warning events with type:fallback for rate-limit/fallback lifecycle notices
    assert "put('warning'" in block
    assert "'type': 'fallback'" in block
    assert "'rate limited'" in src
    assert "'switching to fallback'" in src
    assert "'falling back'" in src
    assert "'fallback activated'" in src
    assert "'trying fallback'" in src

    # Verify callback is wired to agent
    assert "'status_callback' in _agent_params" in src
    assert "_agent_kwargs['status_callback'] = _agent_status_callback" in src
    assert "agent.status_callback = _agent_kwargs.get('status_callback')" in src


def test_agent_status_callback_wiring():
    src = _read("api/streaming.py")
    assert "_agent_status_callback" in src
    assert "_agent_kwargs['status_callback'] = _agent_status_callback" in src


def test_fallback_lifecycle_message_predicate_matches_agent_emitters():
    assert _is_fallback_lifecycle_message(
        "lifecycle",
        "Rate limited — switching to fallback provider...",
    )
    assert _is_fallback_lifecycle_message(
        "lifecycle",
        "Non-retryable error (HTTP 500) — trying fallback...",
    )
    assert not _is_fallback_lifecycle_message(
        "tool",
        "Rate limited — switching to fallback provider...",
    )
    assert not _is_fallback_lifecycle_message(
        "lifecycle",
        "Compressing context",
    )


def test_auto_compression_completion_transition_is_preserved_after_running_listener():
    src = _read("static/messages.js")
    compressing_idx = src.find("source.addEventListener('compressing'")
    compressed_idx = src.find("source.addEventListener('compressed'")
    assert compressing_idx != -1 and compressed_idx != -1
    assert compressing_idx < compressed_idx
    assert "appendLiveCompressionCard({" in _compressed_listener_block()
    assert "phase:'done'" in _compressed_listener_block()
    assert "message:'Context auto-compressed'" in _compressed_listener_block()
    assert "clearCompressionUi()" in _compressed_listener_block()


def test_auto_compression_completion_ignores_legacy_payload_message():
    block = _compressed_listener_block()

    assert "d.message||'Compression finished'" not in block
    assert "setCompressionUi" not in block
    assert "message:'Context auto-compressed'" in block


def test_auto_compression_running_sse_stamps_elapsed_timer_start():
    block = _compressing_listener_block()

    assert "startedAt:Date.now()/1000" in block
    assert block.index("startedAt:Date.now()/1000") < block.index("setCompressionUi(state)")


def test_auto_compression_running_card_keeps_elapsed_timer_out_of_visible_copy():
    src = _read("static/ui.js")
    start = src.find("function _autoCompressionPreviewText")
    assert start != -1, "auto compression preview helper not found"
    end = src.find("function _compressionCardsNode", start)
    assert end != -1, "compression cards node helper not found after auto helper"
    helper = src[start:end]

    assert "const _COMPRESSION_ELAPSED_MAX_SECONDS=5*60;" in src
    assert "function _compressionElapsedLabel(state)" in src
    assert "_formatActiveElapsedTimer" in src
    assert "_compressionElapsedLabel(state)" not in helper
    assert "elapsedLabel" not in helper
    assert "`Elapsed: ${elapsedLabel}`" not in helper
    assert "_autoCompressionPreviewText(state)" in helper
    assert "_autoCompressionDetailText(state)" in helper
    assert "function _startCompressionElapsedTimer()" in src
    assert "function _clearCompressionElapsedTimer()" in src
    assert "function _updateCompressionElapsedCards(state)" in src
    assert "_startCompressionElapsedTimer();" in src
    assert "_clearCompressionElapsedTimer();" in src


def test_auto_compression_uses_command_action_copy():
    src = _read("static/ui.js")
    start = src.find("function _autoCompressionPreviewText")
    assert start != -1, "auto compression preview helper not found"
    end = src.find("function _autoCompressionDetailText", start)
    assert end != -1, "auto compression detail helper not found after preview helper"
    helper = src[start:end]

    assert "Compressing context" in helper
    assert "Context auto-compressed" in helper
    assert "Compression finished" not in helper
    assert "return running?'Running':'Done';" not in helper


def test_auto_compression_running_card_defaults_collapsed():
    src = _read("static/ui.js")
    start = src.find("function _autoCompressionCardsHtml")
    assert start != -1, "auto compression card helper not found"
    end = src.find("function _compressionCardsNode", start)
    assert end != -1, "compression cards node helper not found after auto helper"
    helper = src[start:end]

    assert "auto-compression-divider" in helper
    assert "open: false" not in helper
    assert "open: running" not in helper


def test_auto_compression_uses_centered_noninteractive_divider():
    src = _read("static/style.css")

    assert ".auto-compression-divider" in src
    assert "grid-template-columns:minmax(32px,1fr) auto minmax(32px,1fr)" in src
    assert "pointer-events:none" in src
    override = src.split(".auto-compression-divider{", 1)[1].split("}", 1)[0]
    assert "color:var(--muted)" in override
    assert "user-select:none" in override


def test_auto_compression_worklog_row_does_not_use_tool_card_affordances():
    src = _read("static/ui.js")
    start = src.find("function _autoCompressionWorklogNode")
    assert start != -1, "auto compression worklog node helper not found"
    end = src.find("function _compressionCardsNode", start)
    assert end != -1, "compression cards node helper not found after worklog helper"
    helper = src[start:end]

    assert "tool-card-running-dot" not in helper
    assert "auto_compress_label" not in helper
    assert "tool-card-header" not in helper
    assert "onclick" not in helper
    assert "tabindex" not in helper
    assert "tl-caret" not in helper
    assert "auto-compression-divider" in helper
    assert "auto-compression-divider-line" in helper
    assert "_autoCompressionPreviewText(state)" in helper


def test_auto_compression_live_card_appends_to_worklog_timeline():
    src = _read("static/ui.js")
    start = src.find("function appendLiveCompressionCard")
    assert start != -1, "live compression card append helper not found"
    end = src.find("function _isHandoffSummaryToolPayload", start)
    assert end != -1, "handoff helper not found after live compression helper"
    helper = src[start:end]

    assert "ensureLiveWorklogContainer" in helper
    assert "_toolWorklogListEl(group)" in helper
    assert "_autoCompressionWorklogNode(state)" in helper
    automatic_branch = helper.split("if(state.automatic){", 1)[1].split("const node=_compressionCardsNode(state);", 1)[0]
    assert "inner.appendChild(node)" not in automatic_branch
    assert "list.appendChild(node)" in automatic_branch


def test_final_settle_removes_live_auto_compression_row():
    src = _read("static/ui.js")
    start = src.find("function clearLiveToolCards")
    assert start != -1, "live tool cleanup helper not found"
    end = src.find("function _removeEmptyLiveWorklogShells", start)
    assert end != -1, "next live worklog helper not found after cleanup helper"
    helper = src[start:end]

    assert ".live-worklog[data-live-worklog-shell]" in helper
    assert "data-live-compression-card" in src


def test_final_settle_drops_transient_automatic_compression_state():
    src = _read("static/ui.js")
    start = src.find("function renderMessages")
    assert start != -1, "renderMessages not found"
    end = src.find("function _toolDisplayName", start)
    assert end != -1, "renderMessages end marker not found"
    helper = src[start:end]

    assert "compressionState && compressionState.automatic" in helper
    assert "window._compressionUi=null;" in helper
    assert "compressionState=null;" in helper


def test_auto_compression_elapsed_cap_uses_non_frozen_label():
    src = _read("static/ui.js")
    start = src.find("function _compressionElapsedLabel")
    assert start != -1, "elapsed label helper not found"
    end = src.find("function _compressionElapsedExpired", start)
    assert end != -1, "elapsed expiry helper not found after label helper"
    helper = src[start:end]

    assert "'5+ min'" in helper
    assert "elapsed>=_COMPRESSION_ELAPSED_MAX_SECONDS" in helper
    assert "return '05:00'" not in helper


def test_auto_compression_running_detail_avoids_duplicate_message_text():
    src = _read("static/ui.js")
    start = src.find("function _autoCompressionDetailText")
    assert start != -1, "auto compression detail helper not found"
    end = src.find("function _autoCompressionCardsHtml", start)
    assert end != -1, "auto compression card helper not found after detail helper"
    helper = src[start:end]

    assert "if(running)return '';" in helper
    assert "`Elapsed: ${elapsedLabel}`" not in helper
    assert "${base}\\nElapsed:" not in helper


def test_auto_compression_done_detail_is_not_persisted_in_worklog():
    src = _read("static/ui.js")
    start = src.find("function _autoCompressionDetailText")
    assert start != -1, "auto compression detail helper not found"
    end = src.find("function _autoCompressionCardsHtml", start)
    assert end != -1, "auto compression card helper not found after detail helper"
    helper = src[start:end]

    assert "continuationSessionId" not in helper
    assert "Continued in compressed session" not in helper
    assert "return '';" in helper


def test_auto_compression_live_card_keeps_elapsed_state_for_timer_refresh():
    src = _read("static/ui.js")
    start = src.find("function appendLiveCompressionCard")
    assert start != -1, "live compression card append helper not found"
    end = src.find("function _isHandoffSummaryToolPayload", start)
    assert end != -1, "handoff helper not found after live compression helper"
    helper = src[start:end]

    assert "data-compression-started-at" in helper
    assert "data-compression-message" in helper
    assert "_compressionLiveCardState" in src


def test_auto_compression_does_not_rerender_over_live_worklog():
    block = _compressing_listener_block()
    src = _read("static/ui.js")

    assert "const liveAnswerStarted=" not in block
    assert "appendLiveCompressionCard(state)" in block
    assert "renderMessages({preserveScroll:true})" not in block
    assert "restoreLiveTurnHtmlForSession(activeSid)" not in block
    assert block.index("appendLiveCompressionCard(state)") < block.index("setCompressionUi(state)")
    assert "clearCompressionUi()" in block
    assert "function appendLiveCompressionCard(state)" in src
    assert 'data-live-compression-card' in src


def test_auto_compression_live_repeated_starts_keep_only_current_running_row():
    src = _read("static/ui.js")
    start = src.find("function appendLiveCompressionCard(state)")
    assert start != -1, "live compression card append helper not found"
    end = src.find("function _isHandoffSummaryToolPayload", start)
    assert end != -1, "handoff helper not found after live compression helper"
    helper = src[start:end]

    assert "node.setAttribute('data-compression-phase',String(state.phase||''));" in helper
    assert "const existingRunning=group.querySelector('[data-live-compression-card=\"1\"][data-compression-started-at]');" in helper
    assert 'const existing=state.phase===\'running\'?existingRunning:(existingRunning||existingDone);' in helper
    assert "if(existing) existing.replaceWith(node);" in helper
    assert "else list.appendChild(node);" in helper


def test_auto_compression_running_card_completes_on_followup_live_events():
    src = _read("static/messages.js")

    assert "function _completeAutomaticCompressionOnLiveProgress" in src
    helper = src.split("function _completeAutomaticCompressionOnLiveProgress", 1)[1].split("source.addEventListener('token'", 1)[0]
    assert "data-live-compression-card=\"1\"][data-compression-started-at]" in helper
    assert "window._compressionUi&&window._compressionUi.automatic&&window._compressionUi.phase==='running'" in helper
    assert "phase:'done'" in helper
    assert "message:'Context auto-compressed'" in helper
    assert "appendLiveCompressionCard({" in helper

    for event_name in ("token", "interim_assistant", "reasoning", "tool", "tool_complete"):
        start = src.find(f"source.addEventListener('{event_name}'")
        assert start != -1, f"{event_name} listener not found"
        end = src.find("source.addEventListener(", start + 1)
        assert end != -1, f"{event_name} listener end not found"
        block = src[start:end]
        assert "_completeAutomaticCompressionOnLiveProgress(activeSid)" in block
        assert "settleLiveCompressionCards" not in block
        assert "clearCompressionUi()" not in block


def test_auto_compression_elapsed_update_is_not_visible_detail_churn():
    src = _read("static/ui.js")
    start = src.find("function _updateCompressionElapsedCards")
    assert start != -1, "elapsed update helper not found"
    end = src.find("function _startCompressionElapsedTimer", start)
    assert end != -1, "timer helper not found after elapsed updater"
    helper = src[start:end]

    assert "return false;" in helper
    assert ".tool-card-compress-auto" not in helper
    assert "tool-card-preview" not in helper
    assert "tool-card-result" not in helper


def test_auto_compression_sse_uses_transient_card_not_fake_message():
    """Auto compression must not inject display-only text into S.messages."""
    src = _read("static/messages.js")
    block = _compressed_listener_block()

    assert "*[Context was auto-compressed to continue the conversation]*" not in src
    assert "S.messages.push" not in block
    assert "setCompressionUi" not in block
    assert "phase:'done'" in block
    assert "automatic:true" in block
    assert "appendLiveCompressionCard" in block
    assert "_setCompressionSessionLock" in block
    assert "clearCompressionUi()" in block


def test_auto_compression_sse_keeps_inactive_and_malformed_paths_safe():
    block = _compressed_listener_block()

    guard = "if(!S.session) return;"
    assert guard in block
    assert block.index(guard) < block.index("appendLiveCompressionCard")
    assert "try{ d=JSON.parse(e.data||'{}')||{}; }catch(_){ d={}; }" in block
    assert "const eventSid=d.old_session_id||d.session_id||activeSid;" in block
    assert "const eventMatchesCurrent=" in block
    event_guard = "if(!eventMatchesCurrent) return;"
    assert event_guard in block
    assert block.index("const eventMatchesCurrent=") < block.index(event_guard)


def test_auto_compression_done_accepts_rotated_continuation_session_event():
    block = _compressed_listener_block()

    # Auto-compression can rotate the backend session id before the 'compressed'
    # event is emitted. The browser stream still belongs to the pre-compression
    # activeSid, so the listener must correlate on old_session_id and keep the
    # continuation id as display metadata instead of dropping the event.
    assert "const eventSid=d.old_session_id||d.session_id||activeSid;" in block
    assert "const continuationSid=d.new_session_id||d.continuation_session_id||'';" in block
    event_guard = "if(!eventMatchesCurrent) return;"
    assert event_guard in block
    assert block.index("const eventSid=") < block.index("const eventMatchesCurrent=")
    assert "continuationSessionId:continuationSid" in block


def test_auto_compression_done_accepts_event_after_current_session_rotates():
    block = _compressed_listener_block()

    # The final compressed event can arrive/replay after another event has already
    # updated S.session to the continuation session id. Do not drop it just
    # because the active browser session no longer equals the original activeSid.
    strict_active_guard = "if(!S.session||S.session.session_id!==activeSid) return;"
    assert strict_active_guard not in block
    assert "if(!S.session) return;" in block
    assert "const currentSid=S.session.session_id;" in block
    assert "const eventMatchesCurrent=" in block
    assert "const displaySid=currentSid;" in block
    assert block.index("const eventSid=") < block.index("const eventMatchesCurrent=")
    assert block.index("const displaySid=") < block.index("appendLiveCompressionCard")


def test_auto_compression_done_sse_refreshes_context_indicator_usage():
    block = _compressed_listener_block()

    assert "if(d.usage&&typeof _syncCtxIndicator==='function')" in block
    assert "_mergeUsageForCtxIndicator(d.usage,S.lastUsage||{})" in block
    assert "_syncCtxIndicator(S.lastUsage);" in block
    assert block.index("_syncCtxIndicator(S.lastUsage);") < block.index("appendLiveCompressionCard")


def test_auto_compression_done_payload_includes_live_usage_snapshot():
    src = _read("api/streaming.py")
    start = src.find("put('compressed'")
    assert start != -1, "compressed SSE payload not found"
    end = src.find("})", start)
    assert end != -1, "compressed SSE payload end not found"
    block = src[start:end]

    assert "'session_id': _compression_origin_session_id" in block
    assert "'old_session_id': _compression_origin_session_id" in block
    assert "'new_session_id': _compression_continuation_session_id" in block
    assert "'continuation_session_id': _compression_continuation_session_id" in block
    assert "'message': 'Compression finished'" in block
    assert "'usage': _live_usage_snapshot()" in block


def test_auto_compression_rotation_tracks_origin_and_continuation_ids_for_sse():
    src = _read("api/streaming.py")
    rotate_start = src.find("# ── Handle context compression side effects ──")
    assert rotate_start != -1, "compression side-effect block not found"
    rotate_end = src.find("# Stamp 'timestamp'", rotate_start)
    assert rotate_end != -1, "compression side-effect block end not found"
    block = src[rotate_start:rotate_end]

    assert "_compression_origin_session_id = session_id" in block
    assert "_compression_continuation_session_id = None" in block
    assert "_compression_origin_session_id = old_sid" in block
    assert "_compression_continuation_session_id = new_sid" in block
    assert "'new_session_id': _compression_continuation_session_id" in block


def test_auto_compression_card_reuses_compression_card_renderer():
    src = _read("static/ui.js")
    start = src.find("function _autoCompressionCardsHtml")
    assert start != -1, "auto compression card helper not found"
    end = src.find("function _compressionCardsNode", start)
    assert end != -1, "compression cards node helper not found after auto helper"
    helper = src[start:end]

    assert "if(state.automatic) return _autoCompressionCardsHtml(state);" in src
    assert "tool-card-row compression-card-row auto-compression-divider-row" in helper
    assert "auto-compression-divider-line" in helper
    assert "variantClass: 'tool-card-compress-auto'" not in helper
    assert "statusLabel: preview" not in helper


def test_auto_compression_compressed_sse_does_not_show_persistent_completion_toast():
    block = _compressed_listener_block()

    assert 'showToast' not in block
    assert "Compression finished" not in block


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
    assert "_contextCompactionMessageHtml(m, tsTitle, preservedForThisCard)" not in render_prefix
    assert "continue;" in render_prefix[context_idx:user_idx]


def test_settled_transcript_suppresses_context_compaction_reference_cards():
    src = _read("static/ui.js")

    assert "function _shouldShowSettledCompressionReference" in src
    assert "!_isContextCompactionText(referenceText)" in src

    visible_filter_start = src.find("const vis=S.messages.filter")
    assert visible_filter_start != -1, "visible message filter not found"
    visible_filter_end = src.find("$('emptyState')", visible_filter_start)
    visible_filter = src[visible_filter_start:visible_filter_end]
    assert "if(_isContextCompactionMessage(m)) return false;" in visible_filter

    vis_idx_start = src.find("for(const m of S.messages)", visible_filter_end)
    assert vis_idx_start != -1, "raw message index loop not found"
    vis_idx_end = src.find("let lastUserRawIdx", vis_idx_start)
    vis_idx_loop = src[vis_idx_start:vis_idx_end]
    assert "if(_isContextCompactionMessage(m)){ri++;continue;}" in vis_idx_loop


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
    assert "const preservedOnlyNode=" in src
    assert "_preservedCompressionTaskListCardsHtml(preservedCompressionTaskMessages)" in src


def test_context_anchor_reference_uses_session_summary_fallback():
    src = _read("static/ui.js")

    assert "sessionCompressionSummary" in src
    assert "const sessionCompressionSummary" in src
    assert "referenceText=referenceMessage" in src
    assert ": sessionCompressionSummary" in src
    assert "_shouldShowSettledCompressionReference(referenceText)" in src
    assert "!_isContextCompactionText(referenceText)" in src


def test_compression_anchor_matching_tolerates_legacy_missing_timestamp():
    src = _read("static/ui.js")
    start = src.find("function _compressionAnchorIndex")
    assert start != -1, "compression anchor matcher not found"
    end = src.find("function _compressionReferenceCardHtml", start)
    assert end != -1, "compression reference renderer not found after anchor matcher"
    helper = src[start:end]

    assert "const anchorTs=String(anchorKey.ts??'');" in helper
    assert "const candidateTs=String(candidate.ts??'');" in helper
    assert "(!anchorTs||!candidateTs||candidateTs===anchorTs)" in helper


def test_compression_anchor_index_is_translated_into_render_window():
    src = _read("static/ui.js")
    start = src.find("const insertionAnchorFull=_compressionAnchorIndex")
    assert start != -1, "full compression anchor lookup not found"
    end = src.find("let _prevSepKey=null", start)
    assert end != -1, "message render loop marker not found after anchor lookup"
    block = src[start:end]

    assert "_compressionAnchorIndex(\n    visWithIdx," in block
    assert "insertionAnchorFull<windowStart" in block
    assert "insertionAnchorFull-windowStart" in block
    assert "windowStart+renderVisWithIdx.length" in block


def test_reference_message_uses_raw_transcript_position_before_anchor_fallback():
    src = _read("static/ui.js")

    assert "const {message:referenceMessage, rawIdx:referenceMessageRawIdx}=_latestCompressionReferenceMessage(" in src
    assert "if(referenceNode&&referenceMessageRawIdx>=0) _insertCompressionLikeNodeByRawIdx(referenceNode, referenceMessageRawIdx);" in src
    assert "else _insertCompressionLikeNode(referenceNode);" in src


def test_reference_message_inserted_before_future_assistant_anchor():
    src = _read("static/ui.js")
    start = src.find("function _insertCompressionLikeNodeByRawIdx")
    assert start != -1, "raw-index insertion helper not found"
    end = src.find("const preservedOnlyNode", start)
    assert end != -1, "raw-index insertion helper end marker not found"
    helper = src[start:end]

    assert "const anchorSeg=assistantSegments.get(anchorRawIdx);" in helper
    assert "blocks.insertBefore(node, anchorSeg);" in helper
    assert helper.index("blocks.insertBefore(node, anchorSeg);") < helper.index("const userRow=userRows.get(anchorRawIdx);")


def test_frontend_uses_context_engine_metadata_for_indexed_context_copy():
    src = _read("static/ui.js")
    i18n = _read("static/i18n.js")

    assert "function _compressionEngineForSession" in src
    assert "S.session.compression_anchor_engine" in src
    assert "S.session.context_engine" in src
    assert "function _compressionModeForSession" in src
    assert "S.session.compression_anchor_mode" in src
    assert "function _engineAwareCompressionCopy" in src
    assert "mode==='lossless_retrieval'" in src
    assert "t('retrieval_context_label')" in src
    assert "t('retrieval_context_preview')" in src
    assert "retrieval_context_label" in i18n
    assert "retrieval_context_preview" in i18n


def test_session_model_round_trips_context_engine_metadata(tmp_path, monkeypatch):
    import api.models as models

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")

    session = Session(
        session_id="lcm_metadata",
        workspace=str(tmp_path),
        context_engine="lcm",
        compression_anchor_engine="lcm",
        compression_anchor_mode="lossless_retrieval",
        compression_anchor_details={"retrieval_tools": ["lcm_grep"]},
        context_engine_state={"status": "indexed"},
    )
    session.save(touch_updated_at=False)

    loaded = Session.load("lcm_metadata")
    assert loaded.context_engine == "lcm"
    assert loaded.compression_anchor_engine == "lcm"
    assert loaded.compression_anchor_mode == "lossless_retrieval"
    assert loaded.compression_anchor_details == {"retrieval_tools": ["lcm_grep"]}
    assert loaded.context_engine_state == {"status": "indexed"}


def test_backend_auto_anchor_count_excludes_compaction_marker_cards():
    messages = [
        {"role": "user", "content": "before compression"},
        {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary"},
        {"role": "assistant", "content": "after compression"},
        {"role": "tool", "content": "hidden tool output"},
        {"role": "user", "content": "[Your active task list was preserved across context compression]"},
    ]

    visible = visible_messages_for_anchor(messages, auto_compression=True)

    assert [m["content"] for m in visible] == ["before compression", "after compression"]


def test_frontend_reference_insertion_skips_when_reference_is_before_render_window():
    src = _read("static/ui.js")
    start = src.find("function _insertCompressionLikeNodeByRawIdx")
    assert start != -1, "raw-index insertion helper not found"
    end = src.find("const preservedOnlyNode=", start)
    assert end != -1, "raw-index insertion helper end not found"
    helper = src[start:end]

    assert "if(rawIdx<firstRenderedRawIdx) return;" in helper


def test_reference_message_selection_prefers_latest_matching_marker():
    src = _read("static/ui.js")
    start = src.find("function _latestCompressionReferenceMessage")
    assert start != -1, "compression reference selection helper not found"
    end = src.find("function _compressionReferenceCardHtml", start)
    assert end != -1, "compression reference renderer not found after selection helper"
    helper = src[start:end]

    assert "for(let i=messages.length-1;i>=0;i--)" in helper
    assert "if(!summaryNorm) return {message:m, rawIdx:i};" in helper
    assert "if(contentNorm.includes(summaryNorm)) return {message:m, rawIdx:i};" in helper


def test_reference_message_falls_back_to_current_summary_when_only_stale_markers_exist():
    src = _read("static/ui.js")
    start = src.find("function _latestCompressionReferenceMessage")
    assert start != -1, "compression reference selection helper not found"
    end = src.find("function _compressionReferenceCardHtml", start)
    assert end != -1, "compression reference renderer not found after selection helper"
    helper = src[start:end]

    assert "const summaryNorm=String(summaryText||'').replace(/\\s+/g,' ').trim();" in helper
    assert "return {message:null, rawIdx:-1};" in helper


def test_preserved_task_list_attaches_once_per_render():
    src = _read("static/ui.js")

    assert "function _latestPreservedCompressionTaskListMessages" in src
    assert ".reverse().find(m=>_isPreservedCompressionTaskListMessage(m))" in src
    assert "const preservedCompressionTaskMessages=_latestPreservedCompressionTaskListMessages(S.messages);" in src
    assert "S.messages.filter(m=>_isPreservedCompressionTaskListMessage(m))" not in src
    assert "let preservedCompressionTaskCardsAttached=!!referenceNode;" in src
    assert "const preservedOnlyNode=" in src
    assert "(!preservedCompressionTaskCardsAttached&&(!referenceNode||compressionState)&&preservedCompressionTaskMessages.length)" in src


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
