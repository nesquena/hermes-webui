"""Static regression guards for read-only projected sessions."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_read_only_session_disables_primary_composer_action():
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert "_isReadOnlySession(S.session)) return 'disabled'" in src
    assert "Resume in WebUI before sending" in src


def test_send_refuses_read_only_projected_session_before_start():
    src = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    send_body = src.split("async function send(){", 1)[1].split("_sendInProgress = true;", 1)[0]
    assert "_isReadOnlySession(S.session)" in send_body
    assert "Resume in WebUI before sending" in send_body