"""Native email-session continuation against the installed Agent, with SMTP intercepted."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_viewer_followup_delivers_to_original_email_sender(tmp_path, monkeypatch):
    pytest.importorskip("gateway.config", reason="Hermes Agent is not installed")
    pytest.importorskip("plugins.platforms.email.adapter", reason="Hermes Agent email adapter is not installed")
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.session import SessionSource, SessionStore
    from gateway.session_authority import LiveSession, initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from plugins.platforms.email.adapter import EmailAdapter

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("EMAIL_ADDRESS", "agent@example.invalid")
    monkeypatch.setenv("EMAIL_PASSWORD", "fictional-only")
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.example.invalid")
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.example.invalid")
    sender = "sender@example.invalid"
    adapter = EmailAdapter(PlatformConfig(enabled=True))
    smtp = MagicMock()
    monkeypatch.setattr(adapter, "_connect_smtp", lambda: smtp)
    adapter._thread_context[sender] = {
        "subject": "Synthetic question", "message_id": "<original@example.invalid>"}

    seen = []
    async def answer(event):
        seen.append((event.text, event.source.platform, event.source.chat_id))
        return "Synthetic response"

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner = SimpleNamespace(
        _session_db=store._db, session_store=store, _draining=False,
        _handle_message=answer, _adapter_for_source=lambda source: adapter)
    authority = await initialize_session_authority(runner, profile_id="p", instance_id="owner")
    store._db.create_session("email-session", source="email")
    authority.sessions["email-session"] = LiveSession(
        SessionSource(platform=Platform.EMAIL, chat_id=sender, user_id=sender), "email:" + sender)
    viewer = AuthorityConnection(authority, object(), {"user_id": "operator"})
    try:
        resumed = await viewer.dispatch({"id": 1, "method": "session.resume", "params": {
            "session_id": "email-session"}})
        assert resumed.get("result", {}).get("session_id") == "email-session", resumed
        submitted = await viewer.dispatch({"id": 2, "method": "prompt.submit", "params": {
            "session_id": "email-session", "submission_id": "webui:fixture", "text": "Synthetic follow-up"}})
        assert submitted.get("result", {}).get("status") == "queued", submitted
        await authority.sessions["email-session"].task
    finally:
        await viewer.close()
        store.close_all_db_handles()

    assert seen == [("Synthetic follow-up", Platform.EMAIL, sender)]
    smtp.send_message.assert_called_once()
    sent = smtp.send_message.call_args.args[0]
    assert sent["To"] == sender
    assert sent["Subject"] == "Re: Synthetic question"
    assert sent["In-Reply-To"] == "<original@example.invalid>"
    assert "Synthetic response" in sent.get_payload()[0].get_payload(decode=True).decode("utf-8")
