"""Regression coverage for issue #1617: TPS belongs on message headers.

Product decision:
- show live TPS in the assistant message header while streaming when real TPS is available;
- persist/show the final TPS at the end of the turn;
- do not show placeholder or estimated TPS when unavailable.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
MODELS_PY = (REPO / "api" / "models.py").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_tps_renders_in_message_header_not_global_titlebar():
    assert "msg-tps-inline" in UI_JS, "assistant message headers need a TPS chip hook"
    assert "msg-tps-inline" in CSS, "TPS header chip needs an explicit CSS hook"
    assert "_assistantRoleHtml(tsTitle='', tpsText='', toolText='')" in UI_JS, (
        "assistant role/header rendering should accept per-message TPS and tool-time text"
    )
    assert "_formatTurnTps" in UI_JS, "TPS formatting should be centralized"
    assert "_turnTps" in UI_JS, "settled assistant messages should render final TPS from message metadata"
    assert "tpsStat" not in MESSAGES_JS, "live TPS must not target the removed/global titlebar chip"


def test_live_metering_updates_only_real_tps_and_never_placeholders():
    listener_start = MESSAGES_JS.find("source.addEventListener('metering'")
    assert listener_start != -1, "messages.js should listen for metering SSE events"
    listener_end = MESSAGES_JS.find("source.addEventListener('apperror'", listener_start)
    assert listener_end != -1, "apperror listener should follow metering listener"
    listener = MESSAGES_JS[listener_start:listener_end]
    assert "_setLiveAssistantTps" in listener, "live metering should update the live assistant header"
    assert "tps_available" in listener and "estimated" in listener, (
        "live TPS display must check availability and reject estimated readings"
    )
    assert "0.0 t/s" not in listener, "unavailable TPS should render nothing, not a 0.0 placeholder"
    assert "'—'" not in listener and '"—"' not in listener, "unavailable TPS should render nothing, not a dash"
    assert "high" not in listener.lower() and "low" not in listener.lower(), (
        "message-header TPS should not carry global HIGH/LOW titlebar semantics"
    )


def test_live_metering_usage_is_provisional_until_done():
    listener_start = MESSAGES_JS.find("source.addEventListener('metering'")
    assert listener_start != -1, "messages.js should listen for metering SSE events"
    listener_end = MESSAGES_JS.find("source.addEventListener('apperror'", listener_start)
    assert listener_end != -1, "apperror listener should follow metering listener"
    listener = MESSAGES_JS[listener_start:listener_end]

    assert "_mergeUsageForCtxIndicator(d.usage,S.lastUsage||{})" in listener, (
        "live usage should merge through the context indicator usage guard"
    )
    assert ": {...(S.lastUsage||{}),...d.usage}" in listener, (
        "live usage should update the transient usage cache for the indicator"
    )
    assert "_syncCtxIndicator(S.lastUsage)" in listener, (
        "live usage should refresh the context indicator"
    )
    assert "S.session.input_tokens=d.usage.input_tokens" not in listener
    assert "S.session.last_prompt_tokens=d.usage.last_prompt_tokens" not in listener


def test_live_prompt_estimate_reanchors_to_fresh_exact_prompt_tokens():
    assert "_live_prompt_exact_tokens = [0]" in STREAMING_PY, (
        "live prompt estimates need a separate exact-token anchor"
    )
    assert "_real_prompt_tokens = int(_usage.get('last_prompt_tokens') or 0)" in STREAMING_PY
    assert "_real_prompt_tokens != _live_prompt_exact_tokens[0]" in STREAMING_PY
    assert "_live_prompt_estimate_tokens[0] = _real_prompt_tokens" in STREAMING_PY


def test_done_payload_persists_final_tps_when_exact_usage_available():
    assert "usage['tps']" in STREAMING_PY, "done usage payload should include final exact TPS when available"
    assert "output_tokens" in STREAMING_PY and "duration_seconds" in STREAMING_PY, (
        "final TPS should be based on exact completion tokens over measured turn duration"
    )
    assert "d.usage.tps" in MESSAGES_JS, "done handler should read final TPS from the usage payload"
    assert "lastAsst._turnTps" in MESSAGES_JS, "done handler should persist final TPS on the last assistant message"


def test_final_tps_uses_per_turn_completion_delta():
    """A cached agent accumulates session_completion_tokens across turns.

    Reusing that session total as the numerator while dividing by one turn's
    duration reports TPS far above the model's real throughput on every turn
    after the first. Final TPS must divide THIS turn's delta instead.
    """
    assert "turn_output_tokens = max(0, output_tokens - prev_output_tokens)" in STREAMING_PY, (
        "turn completion delta should be derived from the agent's cumulative counters"
    )
    assert "float(turn_output_tokens) / _turn_duration_seconds" in STREAMING_PY, (
        "final TPS numerator must be the per-turn completion delta"
    )
    assert "float(output_tokens) / _turn_duration_seconds" not in STREAMING_PY, (
        "session-cumulative completion tokens must not be divided by a single turn's duration"
    )


def test_final_tps_reports_pure_generation_speed():
    """The settled chip reports model generation speed, not whole-turn wall clock.

    The agent keeps one (completion_tokens, request_seconds) entry per provider
    call; this turn's slice of those histories is the pure generation rate.
    The whole-turn average stays in the payload as ``tps_turn`` so the header can
    show tool/wait time next to it instead of folding it into the model number.
    """
    assert "_api_output_history" in STREAMING_PY and "_api_latency_history" in STREAMING_PY, (
        "pure generation TPS must come from the agent's per-call histories"
    )
    assert "_turn_api_calls_base" in STREAMING_PY, (
        "the per-turn slice needs a call-count baseline so cached agents do not leak prior turns"
    )
    assert "usage['tps_turn']" in STREAMING_PY, "whole-turn average should stay in the usage payload"
    assert "usage['tool_seconds']" in STREAMING_PY, "tool wall time should be reported separately"
    assert "usage['api_seconds']" in STREAMING_PY, "provider-call time should be reported separately"
    assert "'_turnToolSeconds'" in STREAMING_PY, "tool time must persist on the assistant message"
    assert "_tool_seconds_total" in STREAMING_PY, "tool durations must be accumulated server-side"
    assert "msg-toolsec-inline" in UI_JS, "tool time needs its own header chip"
    assert "function _formatToolSeconds(" in UI_JS, "tool time formatting should be centralized"
    assert "d.usage.tool_seconds" in MESSAGES_JS, "done handler should read the tool-time field"
    assert "'_turnToolSeconds'" in MESSAGES_JS, (
        "ephemeral-field carry-forward must keep the tool chip across session reloads"
    )
    assert '"_turnToolSeconds"' in MODELS_PY, (
        "session display-metadata allowlist must pass the tool chip through the API"
    )


def test_backend_marks_streaming_metering_availability_explicitly():
    assert "tps_available" in STREAMING_PY, "metering SSE payloads must explicitly say whether TPS is displayable"
    assert "estimated" in STREAMING_PY, "metering SSE payloads must explicitly distinguish estimated readings"
    assert "record_token(stream_id, len(STREAM_PARTIAL_TEXT[stream_id]))" not in STREAMING_PY, (
        "live TPS must not be derived from streamed character count / byte-size estimates"
    )


def test_tps_display_setting_is_default_off_and_persisted():
    assert '"show_tps": False' in CONFIG_PY, "TPS display should be disabled by default"
    assert '"show_tps"' in CONFIG_PY and "_SETTINGS_BOOL_KEYS" in CONFIG_PY, (
        "TPS display should be a persisted boolean WebUI setting"
    )
    assert "settingsShowTps" in INDEX_HTML, "Preferences needs a user-facing TPS display toggle"
    assert "payload.show_tps=showTpsCb.checked" in PANELS_JS, (
        "Preferences autosave should persist the TPS display toggle through /api/settings"
    )
    assert "showTpsCb.checked=!!settings.show_tps" in PANELS_JS, (
        "Settings panel should hydrate the TPS toggle from persisted settings"
    )
    assert "window._showTps=!!s.show_tps" in BOOT_JS, (
        "Boot should hydrate show_tps into a runtime flag"
    )
    assert "window._showTps=false" in BOOT_JS, (
        "Boot fallback should keep TPS hidden when settings cannot load"
    )


def test_tps_display_hot_applies_when_preferences_autosave():
    fn_start = PANELS_JS.find("async function _autosavePreferencesSettings")
    assert fn_start != -1, "preferences autosave function should exist"
    fn_end = PANELS_JS.find("function _retryPreferencesAutosave", fn_start)
    assert fn_end != -1, "retry function should follow preferences autosave"
    fn = PANELS_JS[fn_start:fn_end]
    assert "payload&&payload.show_tps!==undefined" in fn, (
        "TPS preference autosave must detect the show_tps field specifically"
    )
    assert "window._showTps=!!(saved&&saved.show_tps)" in fn, (
        "TPS preference autosave should update the runtime flag from the saved response"
    )
    assert "clearMessageRenderCache" in fn and "renderMessages" in fn, (
        "TPS preference autosave should re-render the open transcript without refresh"
    )


def test_tps_header_rendering_respects_display_setting():
    assert "function isTpsDisplayEnabled()" in UI_JS, "TPS visibility should be centralized"
    assert "return window._showTps===true" in UI_JS, "TPS should only render when explicitly enabled"
    assert "const tps=(isTpsDisplayEnabled()&&tpsText)" in UI_JS, (
        "settled assistant headers must suppress TPS when the setting is off"
    )
    assert "isTpsDisplayEnabled()?_formatTurnTps(value):''" in UI_JS, (
        "live TPS updates must remove/suppress the chip when the setting is off"
    )
    assert "isTpsDisplayEnabled()?_formatTurnTps(m._turnTps):''" in UI_JS, (
        "reloaded assistant messages must not render persisted TPS while disabled"
    )
