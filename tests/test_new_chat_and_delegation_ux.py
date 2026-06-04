from pathlib import Path

from api.streaming import _tool_result_snippet


REPO = Path(__file__).resolve().parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")


def _function_block(source: str, marker: str, next_marker: str | None = None) -> str:
    start = source.find(marker)
    assert start != -1, f"{marker!r} not found"
    if next_marker:
        end = source.find(next_marker, start + len(marker))
        assert end != -1, f"{next_marker!r} not found after {marker!r}"
        return source[start:end]
    return source[start : start + 2500]


def test_new_chat_button_middle_click_uses_background_new_tab_helper():
    block = _function_block(BOOT_JS, "$('btnNewChat').onauxclick", "$('btnDownload').onclick")
    assert "e.button!==1" in block
    assert "e.preventDefault()" in block
    assert "e.stopPropagation()" in block
    assert "await openNewSessionInNewTab()" in block


def test_new_chat_button_cmd_ctrl_click_uses_background_new_tab_helper():
    block = _function_block(BOOT_JS, "$('btnNewChat').onclick", "$('btnNewChat').onauxclick")
    assert "e&&(e.ctrlKey||e.metaKey)" in block
    assert "await openNewSessionInNewTab()" in block
    assert block.find("await openNewSessionInNewTab()") < block.find("(S.session.message_count||0)===0"), (
        "modifier-click must bypass the empty-session focus guard and create a new tab session"
    )


def test_open_new_session_in_new_tab_does_not_steal_current_session():
    block = _function_block(SESSIONS_JS, "async function openNewSessionInNewTab", "async function newSession")
    assert "window.open('about:blank','_blank')" in block
    assert "newTab.location.href=url" in block
    assert "void renderSessionList({deferWhileInteracting:false})" in block
    assert "S.session=data.session" not in block
    assert "localStorage.setItem('hermes-webui-session'" not in block


def test_new_chat_tooltip_mentions_middle_click_new_tab():
    assert "Middle-click or Cmd/Ctrl-click to open in new tab" in INDEX_HTML
    assert 'data-i18n-title="new_conversation_new_tab"' in INDEX_HTML
    assert "new_conversation_new_tab: 'New conversation (Cmd+K) · Middle-click or Cmd/Ctrl-click to open in new tab'" in (
        REPO / "static" / "i18n.js"
    ).read_text(encoding="utf-8")


def test_subagent_progress_events_are_forwarded_to_tool_sse():
    block = _function_block(STREAMING_PY, "subagent_event_types = {", "# Modern Hermes Agent builds")
    assert "'subagent.start'" in block
    assert "'subagent.tool'" in block
    assert "'subagent.progress'" in block
    assert "'subagent.thinking'" in block
    assert "'subagent.complete'" in block
    assert "put('tool'" in block
    assert "'name': 'subagent_progress'" in block
    assert "'preview': preview or name" in block
    assert "subagent_args['__subagent_event'] = event_type" in block
    assert "'task_index', 'task_count', 'subagent_id', 'parent_id', 'depth'" in block


def test_subagent_progress_updates_live_delegation_card_in_place():
    block = _function_block(MESSAGES_JS, "source.addEventListener('tool'", "source.addEventListener('tool_complete'")
    assert "const isSubagentProgress=d.name==='subagent_progress'" in block
    assert "_liveDelegationApplyProgress(activeSid,d,inflight)" in block
    assert "if(!isSubagentProgress) INFLIGHT[activeSid].toolCalls.push(tc)" in block
    assert "done:isSubagentProgress" not in block
    assert "function _liveDelegationFindOrCreateToolCall" in MESSAGES_JS
    assert "function _liveDelegationApplyProgress" in MESSAGES_JS
    assert "tc.raw_result=JSON.stringify({results:rows" in MESSAGES_JS
    assert "name:'delegate_task'" in MESSAGES_JS
    assert "tid:`live-delegation-${sid||'session'}`" in MESSAGES_JS


def test_delegate_task_backend_snippet_is_human_readable():
    preview = _tool_result_snippet(
        {
            "results": [
                {
                    "status": "completed",
                    "summary": "Mapped the auth flow\nwith details",
                    "api_calls": 4,
                    "duration_seconds": 12.25,
                    "exit_reason": "completed",
                },
                {
                    "status": "failed",
                    "error": "Missing fixture",
                    "api_calls": 2,
                    "duration_seconds": 3,
                },
            ]
        }
    )
    assert preview.startswith("1/2 completed, 1 failed · 2 subagents · 12.2s · 6 API calls")
    assert "Mapped the auth flow" in preview
    assert "{'results'" not in preview


def test_delegate_task_frontend_has_specialized_summary_renderer():
    assert "function _delegateTaskCardSummary" in UI_JS
    assert "function _delegateTaskCardHtml" in UI_JS
    assert "function _delegateSummaryMarkup" in UI_JS
    assert "function _delegateFormatBytes" in UI_JS
    assert "function _subagentProgressCardHtml" in UI_JS
    assert "function _settledToolCallsForRender" in UI_JS
    assert "function _messageToolResultsByTid" in UI_JS
    assert "rawResultsByTid" in UI_JS
    assert "raw_result:name==='delegate_task'" in UI_JS
    assert "const candidates=[tc&&tc.raw_result,tc&&tc.snippet,tc&&tc.preview]" in UI_JS
    assert "tool_trace" in UI_JS
    assert "Tool trace ·" in UI_JS
    assert "hideDuplicateSingleResult" in UI_JS
    assert "summary.showResult&&summary.resultShort" in UI_JS
    assert "_deriveToolCallsFromMessages(messages)" in UI_JS
    assert "settledToolCallsForRender" in UI_JS
    assert "row.innerHTML=_delegateTaskCardHtml(tc,_delegateTaskCardSummary(tc))" in UI_JS
    assert "row.innerHTML=_subagentProgressCardHtml(tc)" in UI_JS
    assert "Delegation run" in UI_JS
    assert "Raw request / output" in UI_JS
    assert "delegation-child-row" in UI_JS
    assert ".delegation-run-card" in STYLE_CSS
    assert ".delegation-child-row" in STYLE_CSS
    assert ".delegation-tool-trace" in STYLE_CSS
    assert ".delegation-trace-row" in STYLE_CSS
    assert ".delegation-status-pill" in STYLE_CSS
    assert ".subagent-progress-card" in STYLE_CSS
