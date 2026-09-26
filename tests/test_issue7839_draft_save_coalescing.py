"""Regression coverage for large-session draft-save coalescing (#7839).

On very large sessions each debounced draft autosave rewrote the ENTIRE
session JSON behind the per-session agent lock. Queued autosaves all performed
their own full rewrite of the same payload, producing a lock convoy:
21-182 s lock waits stalled every endpoint for that session and the UI showed
"Request timed out. Please try again." while chats appeared to vanish until a
manual refresh.

The fix: POST /api/session/draft publishes the latest intent to a single
per-session worker (latest-wins coalescing, one save per distinct payload)
and answers with 503 if the worker cannot settle within the request wait.
The debounced autosave raises its client timeout and suppresses the generic
timeout toast so background saves cannot spam the UI.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT.joinpath("api", "routes.py").read_text(encoding="utf-8")
SESSIONS_JS = ROOT.joinpath("static", "sessions.js").read_text(encoding="utf-8")


def _block(source, start_marker, end_marker):
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_draft_handler_delegates_persistence_to_coalescing_worker():
    """The POST draft handler must not run Session.save() inline anymore."""
    handler_body = _block(
        ROUTES,
        'if parsed.path == "/api/session/draft":',
        'if parsed.path == "/api/session/update":',
    )
    assert "_DRAFT_COALESCE_LOCK" in handler_body, "draft POST must publish its intent via the coalescing registry"
    assert "worker_event.wait(" in handler_body, "draft POST must wait for the per-session worker result"
    assert "s.save(touch_updated_at=False" not in handler_body, "draft POST must not rewrite the session inline"
    assert 'return bad(handler, "Draft save still in progress; retry shortly", 503)' in handler_body, (
        "a still-running worker must answer 503 instead of stacking handlers behind the lock"
    )


def test_draft_worker_persists_exactly_like_the_old_inline_path():
    """The worker must keep the durable contract of the old inline save."""
    worker_body = _block(ROUTES, "def _draft_save_worker(", "def _session_is_subagent_view_only(")
    assert "_get_session_agent_lock(sid)" in worker_body, "worker saves must serialize on the per-session agent lock"
    assert "s.save(touch_updated_at=False, skip_index=True)" in worker_body, (
        "worker saves must preserve the no-updated_at-bump / no-index-churn contract"
    )
    # The contract comment must survive at the save site so the invariant stays documented.
    assert "Draft persistence is not conversation activity" in worker_body
    # A metadata-only stub must never be saved (#1558 P0: would wipe messages).
    assert "_loaded_metadata_only" in worker_body


def test_draft_worker_coalesces_to_latest_payload():
    worker_body = _block(ROUTES, "def _draft_save_worker(", "def _session_is_subagent_view_only(")
    assert 'state["pending"]' in worker_body and 'state["published"]' in worker_body, (
        "worker must move the queued latest intent into the published slot (latest-wins)"
    )
    assert "lock.acquire(timeout=_DRAFT_SAVE_LOCK_WAIT)" in worker_body, (
        "worker must use a bounded agent-lock wait so a heavy op cannot strand draft saves forever"
    )


def test_draft_404_contract_preserved_for_missing_sessions():
    handler_body = _block(
        ROUTES,
        'if parsed.path == "/api/session/draft":',
        'if parsed.path == "/api/session/update":',
    )
    assert "get_session(sid, metadata_only=True)" in handler_body, (
        "missing sessions must still 404 before publishing an intent (issue #4765)"
    )
    assert 'return bad(handler, "Session not found", 404)' in handler_body


def test_autosave_client_raises_timeout_and_suppresses_timeout_toast():
    save_body = _block(SESSIONS_JS, "function _saveComposerDraft(sid, text, files)", "function _composerDraftHasPayload")
    assert "timeoutMs: 120000" in save_body, "background autosave must tolerate long worker saves on huge sessions"
    assert "timeoutToast: false" in save_body, "background autosave must not spam the generic timeout toast"


def test_user_initiated_draft_flushes_keep_default_timeout():
    now_body = _block(SESSIONS_JS, "function _saveComposerDraftNow(sid, text, files)", "// Restore composer draft")
    assert "timeoutMs" not in now_body, "switch/send flushes are user-visible and keep the default timeout"
