"""Wave 5 — auto-recover when /api/chat/start returns 404 Session not found.

Static assertions on static/messages.js. The runtime path is exercised
manually in the browser; here we only verify that the catch branch which
reacts to a missing session is in place so future refactors don't accidentally
revert to the dead-end behaviour the user hit on the Neo VPS post-deploy.
"""
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def test_chat_start_404_branch_exists():
    """The catch around /api/chat/start must branch on err.status === 404."""
    assert "e.status === 404" in MESSAGES_JS, (
        "messages.js must inspect err.status === 404 to detect a stale session "
        "id (Wave 5)."
    )


def test_chat_start_404_clears_local_storage_and_creates_session():
    """On 404 we must drop the orphaned id and mint a fresh session."""
    snippet_idx = MESSAGES_JS.find("e.status === 404")
    assert snippet_idx != -1
    snippet = MESSAGES_JS[snippet_idx:snippet_idx + 1500]
    assert "localStorage.removeItem('hermes-webui-session')" in snippet, (
        "the 404 recovery branch must clear the stale localStorage session id"
    )
    assert "newSession()" in snippet, (
        "the 404 recovery branch must call newSession() so the user is not stuck"
    )


def test_chat_start_404_restores_draft():
    """The recovery branch must put the typed text back in the composer so the
    user can simply press Enter again — automatically resending could surprise
    the user when the failure was a transient backend issue, but losing the
    draft entirely is what produced the original 'Erro: Session not found'
    dead-end report."""
    snippet_idx = MESSAGES_JS.find("e.status === 404")
    snippet = MESSAGES_JS[snippet_idx:snippet_idx + 1500]
    assert "_msgInput.value = msgText" in snippet, (
        "recovery branch must restore the user's draft to the composer"
    )
