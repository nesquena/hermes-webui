"""Regression tests for per-turn response duration in WebUI.

The WebUI should expose how long an agent turn took, using backend timing so
reload/reconnect does not lose the measurement.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_streaming_done_payload_includes_backend_turn_duration():
    assert "duration_seconds" in STREAMING_PY, (
        "api/streaming.py should include a backend-measured duration_seconds "
        "field in the done usage payload."
    )
    assert "pending_started_at" in STREAMING_PY and "time.time()" in STREAMING_PY, (
        "Turn duration should be measured from the persisted pending_started_at "
        "start time, not only from browser-local state."
    )
    assert "recovered/legacy flows" in STREAMING_PY, (
        "The missing-start fallback should be documented so it is not mistaken "
        "for the primary timing path."
    )
    assert "_turnDuration" in STREAMING_PY, (
        "The measured duration should be persisted on the assistant message so "
        "it survives reload after the SSE stream settles."
    )


def test_done_handler_persists_duration_on_last_assistant_message():
    assert "d.usage.duration_seconds" in MESSAGES_JS, (
        "static/messages.js should read duration_seconds from the done usage payload."
    )
    assert "lastAsst._turnDuration" in MESSAGES_JS, (
        "The done handler should attach the duration to the last assistant message "
        "so renderMessages() can display it after the live stream settles."
    )


def test_ui_formats_and_renders_turn_duration_in_footer_and_activity_summary():
    assert "function _formatTurnDuration" in UI_JS, (
        "ui.js should centralize duration formatting for footer and compact activity display."
    )
    assert "msg-duration-inline" in UI_JS and "Done in" in UI_JS, (
        "Expanded/non-activity display should show a subtle footer chip like 'Done in 42s'."
    )
    assert "tool-call-group-duration" in UI_JS, (
        "Compact tool activity summary should have a dedicated duration span at the end of the line."
    )
    assert "data-turn-duration" in UI_JS, (
        "The spec Activity summary needs a stable data-turn-duration hook so settled duration can update its summary."
    )
    assert "turnDuration:includeTurnDuration?_turnDurationForAnchor(anchorRow):undefined" in UI_JS, (
        "Settled compact activity should put turn duration on the first spec Activity row, "
        "not resurrect the legacy top Run Activity."
    )
    assert "compactWorklogForMessage" in UI_JS, (
        "When folded Worklog detail is present, duration should live on the Worklog row "
        "instead of being duplicated in the assistant footer."
    )
    assert ".msg-duration-inline" in CSS and ".tool-call-group-duration" in CSS, (
        "Duration UI should have explicit CSS hooks for the footer chip and compact activity summary."
    )


def test_active_compact_activity_elapsed_timer_uses_persisted_start_time():
    assert '"pending_started_at": s.pending_started_at' in ROUTES_PY, (
        "/api/chat/start should return the persisted pending_started_at timestamp "
        "so the live timer starts from backend/session truth."
    )
    assert "startData.pending_started_at" in MESSAGES_JS, (
        "send() should copy chat-start pending_started_at into S.session before "
        "attaching the live stream."
    )
    assert "showLiveRunStatus(activeSid,{startedAt:_startedAt});" in MESSAGES_JS, (
        "The first chat-start path should show the bottom live footer timer as soon "
        "as stream_id and pending_started_at are known; reconnect should not be the "
        "only path that restores it."
    )
    assert "function _processedElapsedLabel" in UI_JS and "t('processed_elapsed',text)" in UI_JS, (
        "Compact Worklog should present the running timer as the stable processed-time anchor."
    )
    assert "data-turn-started-at" in UI_JS and "data-active-turn-elapsed" in UI_JS, (
        "Live compact Activity groups need stable start-time and active-elapsed "
        "hooks for browser QA and reconnect/rerender safety."
    )
    assert "_activityProcessedElapsedLabel(group)" in UI_JS, (
        "The in-progress Activity summary should own the live elapsed label "
        "instead of relying on the bottom live footer."
    )
    assert "setInterval" in UI_JS and "_clearActivityElapsedTimer" in UI_JS, (
        "The active elapsed label should tick while running and clear its interval "
        "on terminal/error/session-switch cleanup paths."
    )


def test_live_footer_timer_is_re_synced_after_message_rerender():
    assert "function _syncLiveRunStatusAfterRender()" in UI_JS, (
        "renderMessages() needs a dedicated helper so the live footer timer "
        "can be restored after DOM rebuilds."
    )
    assert "_syncLiveRunStatusAfterRender();" in UI_JS, (
        "renderMessages() should call the live-status sync helper after it "
        "rebuilds msgInner."
    )
    assert "showLiveRunStatus(sid,{startedAt,tokens:_liveRunStatusTokens});" in UI_JS, (
        "If the timer node was torn down during a rerender, the helper should "
        "recreate it for the active session."
    )


def test_compact_worklog_hides_bottom_live_footer_timer():
    show = UI_JS.split("function showLiveRunStatus", 1)[1].split("function _renderLiveRunStatusContent", 1)[0]
    sync = UI_JS.split("function _syncLiveRunStatusAfterRender", 1)[1].split("function hideLiveRunStatus", 1)[0]

    assert "isCompactWorklogMode" in show
    assert "if(el){el.hidden=true;el.innerHTML='';}" in show
    assert "isCompactWorklogMode" in sync
    assert "if(el){el.hidden=true;el.innerHTML='';}" in sync


def test_processed_elapsed_anchor_is_i18n_driven():
    assert "function _i18nProcessedElapsed(prefix, duration)" in I18N_JS
    assert "function _i18nProcessedElapsedEn(duration)" in I18N_JS
    assert "return _i18nProcessedElapsed('Processed', duration);" in I18N_JS
    assert "function _i18nProcessedElapsedZh(duration)" in I18N_JS
    assert "return _i18nProcessedElapsed('已处理', duration);" in I18N_JS
    assert "function _i18nProcessedElapsedZhHant(duration)" in I18N_JS
    assert "return _i18nProcessedElapsed('已處理', duration);" in I18N_JS
    assert "processed_elapsed: _i18nProcessedElapsedEn" in I18N_JS
    assert "processed_elapsed: _i18nProcessedElapsedZh" in I18N_JS
    assert "processed_elapsed: _i18nProcessedElapsedZhHant" in I18N_JS
    assert "t('processed_elapsed','')" in UI_JS
    assert "`已处理 ${" not in UI_JS
