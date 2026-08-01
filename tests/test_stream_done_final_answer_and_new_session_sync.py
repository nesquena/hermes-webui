"""Regression coverage for streamed final-answer settlement and new-session UI sync."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_done_settle_preserves_streamed_tail_when_payload_has_no_assistant_text():
    src = _source("static/messages.js")
    assert "const _streamedTail=_stripXmlToolCalls" in src
    assert "slice(Math.max(0,segmentStart||0))" in src
    assert "const _lastSettledAsst=[...S.messages].reverse().find" in src
    assert "if(!_hasSettledText)" in src
    assert "_lastSettledAsst.content=_streamedTail" in src


def test_done_settle_never_overwrites_a_non_empty_persisted_answer():
    src = _source("static/messages.js")
    start = src.index("// Settle guard (text side of the #4539 class)")
    end = src.index("if(typeof _hydrateTodosFromSession", start)
    guard = src[start:end]
    assert "typeof _sc==='string'&&!!_sc.trim()" in guard
    assert "Array.isArray(_sc)&&_sc.some" in guard
    assert guard.index("if(!_hasSettledText)") < guard.index("_lastSettledAsst.content=_streamedTail")


def test_new_session_repaints_main_pane_immediately_after_state_switch():
    src = _source("static/sessions.js")
    state = src.index("S.session=data.session;S.messages=data.session.messages||[];")
    render = src.index("renderMessages();", state)
    hydrate = src.index("_hydrateTodosFromSession", render)
    workspace = src.index("_announceNewSessionWorkspace", hydrate)
    assert state < render < hydrate < workspace


def test_new_conversation_button_handles_rejected_creation():
    src = _source("static/boot.js")
    start = src.index("$('btnNewChat').onclick=async()=>")
    end = src.index("$('btnDownload').onclick", start)
    handler = src[start:end]
    assert "try{" in handler
    assert "await newSession();" in handler
    assert "catch(_newSessionErr)" in handler
    assert "showToast" in handler
