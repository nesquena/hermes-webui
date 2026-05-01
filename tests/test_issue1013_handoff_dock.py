"""Regression guards for cross-channel handoff UI and summary generation."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
ROUTES = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def test_handoff_hint_is_docked_in_composer_flyout_not_transcript():
    """Handoff should use the Terminal-style composer dock, not transcript flow."""
    marker = '<div id="handoffHintContainer"'
    assert marker in INDEX
    msg_inner_idx = INDEX.index('<div class="messages-inner" id="msgInner">')
    composer_flyout_idx = INDEX.index('<div class="composer-flyout">')
    handoff_idx = INDEX.index(marker)
    assert handoff_idx > composer_flyout_idx
    assert not (msg_inner_idx < handoff_idx < composer_flyout_idx)


def test_handoff_dock_reserves_transcript_space_like_terminal_dock():
    assert ".messages.handoff-dock-visible" in STYLE_CSS
    assert ".handoff-hint-container{position:absolute" in STYLE_CSS
    assert "_syncHandoffDockSpace(true)" in SESSIONS_JS
    assert "_syncHandoffDockSpace(false)" in SESSIONS_JS


def test_handoff_summary_renders_as_transcript_card_not_dock_card():
    assert "function setHandoffUi" in SESSIONS_JS or "function setHandoffUi" in (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    ui_js = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert "_handoffCardsNode" in ui_js
    assert "data-handoff-card" in ui_js
    assert 'data-compression-card="1" data-handoff-card="1"' in ui_js
    assert 'class="tool-card-result handoff-summary-body"' in ui_js
    assert "renderMd(detail)" in ui_js
    assert "_insertCompressionLikeNode(handoffState?_handoffCardsNode" in ui_js
    assert "window._handoffUi&&(!window._handoffUi.sessionId||window._handoffUi.sessionId===sid)" in ui_js
    assert "!hasTransientTranscriptUi" in ui_js
    assert "handoff-summary-card" not in SESSIONS_JS
    assert "handoff-summary-card" not in STYLE_CSS


def test_handoff_summary_does_not_call_removed_agent_get_response():
    """Current Hermes Agent exposes run_conversation/private transports, not get_response."""
    handoff_start = ROUTES.index("def _handle_handoff_summary")
    next_handler = ROUTES.index("\ndef _handle_skill_save", handoff_start)
    handoff_body = ROUTES[handoff_start:next_handler]
    assert ".get_response(" not in handoff_body
    assert "_agent_text_completion" in handoff_body
    assert "_fallback_handoff_summary" in handoff_body


def test_generating_handoff_summary_does_not_dismiss_future_hints():
    """Summary generation is a read action; only explicit dismiss should suppress the dock."""
    generate_start = SESSIONS_JS.index("async function _generateHandoffSummary")
    resolve_start = SESSIONS_JS.index("function _resolveSessionModelForDisplaySoon", generate_start)
    generate_body = SESSIONS_JS[generate_start:resolve_start]

    dismiss_start = SESSIONS_JS.index("function _dismissHandoffHint")
    generate_start_after_dismiss = SESSIONS_JS.index("async function _generateHandoffSummary", dismiss_start)
    dismiss_body = SESSIONS_JS[dismiss_start:generate_start_after_dismiss]

    assert "_setHandoffDismissedAt(" not in generate_body
    assert "_setHandoffDismissedAt(" in dismiss_body
    assert "setHandoffUi({" in generate_body
    assert ":dismissed_at'" in SESSIONS_JS
    assert ":seen_at'" not in SESSIONS_JS
