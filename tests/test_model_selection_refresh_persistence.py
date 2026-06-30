"""Regression coverage for model selection surviving hard refresh.

The frontend updates the visible model chip before the async
``/api/session/update`` request returns. A hard refresh can abort that request,
so the browser must remember the session-scoped selection and reapply it on the
next ``loadSession()`` before ``syncTopbar()`` projects server metadata.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text()
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text()
UI_JS = (ROOT / "static" / "ui.js").read_text()


def _body_between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def test_model_selection_records_pending_state_before_async_session_update():
    """A refresh during /api/session/update must not lose the selected model."""
    body = _body_between(BOOT_JS, "$('modelSelect').onchange=async()=>", "$('msg').addEventListener")

    pending_idx = body.index("_rememberPendingSessionModel")
    local_model_idx = body.index("S.session.model=modelState.model")
    update_idx = body.index("await api('/api/session/update'")

    assert pending_idx < update_idx
    assert local_model_idx < update_idx
    # onchange must NOT clear the pending marker after the session-update round-trip.
    # The marker has to survive until the next send() consumes it, otherwise the normal
    # pick→update→send flow loses the explicit-pick signal and the server re-reverts a
    # cross-family pick (#3737). The consume-clear now lives in send(), not here.
    assert "_clearPendingSessionModel" not in body, (
        "modelSelect.onchange must not clear the pending explicit-pick marker (#3737); "
        "send() consumes it instead"
    )


def test_load_session_applies_pending_model_before_first_topbar_sync():
    """Reload should project the pending selection before server old metadata wins."""
    body = _body_between(SESSIONS_JS, "async function loadSession", "activeStreamId=S.session.active_stream_id")

    apply_idx = body.index("_applyPendingSessionModelForSession")
    sync_idx = body.index("syncTopbar()")

    assert apply_idx < sync_idx


def test_pending_model_helpers_are_session_scoped_and_expire():
    assert "const PENDING_SESSION_MODEL_PREFIX" in UI_JS
    assert "function _pendingSessionModelKey" in UI_JS
    assert "function _rememberPendingSessionModel" in UI_JS
    assert "function _applyPendingSessionModelForSession" in UI_JS
    assert "propagateErrors:true" in UI_JS
    assert "sessionStorage" in UI_JS
    assert "10*60*1000" in UI_JS
