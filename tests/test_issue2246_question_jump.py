"""Regression coverage for #2246 per-turn jump-to-question buttons."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_assistant_footer_gets_completed_turn_question_jump_button():
    assert "function _questionJumpButtonHtml(questionRawIdx)" in UI_JS
    assert "function jumpToTurnQuestion(questionRawIdx)" in UI_JS
    assert "const questionRawIdxByAssistantRawIdx=new Map()" in UI_JS
    assert "questionRawIdxByAssistantRawIdx.set(entry.rawIdx,lastQuestionRawIdx)" in UI_JS
    assert "row.id=_userMessageDomId(rawIdx)" in UI_JS
    assert "const isTurnFinalAssistant=!isUser&&(!nextRendered||!nextRendered.m||nextRendered.m.role!=='assistant')" in UI_JS
    assert "(!isUser&&!m._live&&isTurnFinalAssistant)" in UI_JS
    assert "_questionJumpButtonHtml(questionRawIdxByAssistantRawIdx.get(rawIdx))" in UI_JS
    assert "msg-question-jump-btn" in UI_JS


def test_question_jump_expands_windowed_history_and_highlights_question():
    assert "_messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(),_messageRenderableMessageCount())" in UI_JS
    assert "renderMessages({ preserveScroll:true })" in UI_JS
    assert "row.scrollIntoView({block:'center',behavior:'smooth'})" in UI_JS
    assert "_highlightQuestionRow(row)" in UI_JS
    assert "msg-question-highlight" in UI_JS


def test_question_jump_button_is_quiet_and_hidden_on_mobile():
    assert ".msg-question-jump-btn" in STYLE_CSS
    assert "margin-left: auto;" in STYLE_CSS
    assert ".msg-question-highlight .msg-body" in STYLE_CSS
    assert "@keyframes question-highlight-pulse" in STYLE_CSS
    assert "@media (max-width: 600px)" in STYLE_CSS
    assert ".msg-question-jump-btn { display: none; }" in STYLE_CSS


def test_question_jump_text_is_localized():
    for key in ("jump_to_question", "jump_to_question_label"):
        assert I18N_JS.count(f"{key}:") >= 12
