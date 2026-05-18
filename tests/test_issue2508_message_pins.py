"""Regression coverage for per-message pinning (#2508)."""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODELS_PY = (REPO / "api" / "models.py").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_session_model_persists_pinned_messages_metadata():
    assert "pinned_messages=None" in MODELS_PY
    assert "self.pinned_messages" in MODELS_PY
    assert "'pinned', 'pinned_messages', 'archived'" in MODELS_PY
    assert "'pinned_messages': self.pinned_messages" in MODELS_PY


def test_session_pin_metadata_round_trips_through_disk():
    from api.models import Session

    pin = {
        "message_index": 0,
        "message_key": "msg:0:user:test",
        "role": "user",
        "preview": "Keep this visible",
        "pinned_at": 123.0,
    }
    s = Session(
        session_id="pinroundtrip",
        title="Pin round trip",
        messages=[{"role": "user", "content": "Keep this visible"}],
        pinned_messages=[pin],
    )
    s.save(touch_updated_at=False)

    loaded = Session.load("pinroundtrip")
    assert loaded.pinned_messages == [pin]
    compact = loaded.compact()
    assert compact["pinned_messages"] == [pin]


def test_message_pin_endpoint_is_bounded_and_session_scoped():
    assert 'parsed.path == "/api/session/message-pin"' in ROUTES_PY
    assert 'require(body, "session_id")' in ROUTES_PY
    assert "message_index < 0 or message_index >= len(messages)" in ROUTES_PY
    assert "Up to 3 messages can be pinned" in ROUTES_PY
    assert "s.save(touch_updated_at=False)" in ROUTES_PY


def test_transcript_actions_and_context_menu_can_pin_messages():
    assert "function toggleMessagePin" in UI_JS
    assert "_pinnedMessageButtonHtml(rawIdx,m)" in UI_JS
    assert "openMessagePinMenu(ev" in UI_JS
    assert "oncontextmenu=(ev)=>openMessagePinMenu" in UI_JS
    assert "data-full-msg-idx" in UI_JS


def test_right_panel_renders_pinned_message_section():
    assert 'id="pinnedMessagesPanel"' in INDEX_HTML
    assert "function renderPinnedMessages" in UI_JS
    assert "Pinned messages" in UI_JS
    assert "jumpToPinnedMessage" in UI_JS
    assert ".pinned-messages-panel" in CSS
    assert ".pinned-message-card" in CSS
