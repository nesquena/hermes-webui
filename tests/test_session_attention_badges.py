import io
import json
import pathlib
import sys
from urllib.parse import urlparse

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import api.profiles as profiles
import api.routes as routes


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _clear_attention_state(*session_ids):
    from api import clarify

    with routes._lock:
        for sid in session_ids:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)
    for sid in session_ids:
        clarify.clear_pending(sid)


def test_attention_summary_prefers_pending_approvals_over_clarify_questions():
    sid = "attention-both-session"
    _clear_attention_state(sid)
    try:
        routes.submit_pending(sid, {"command": "rm -rf /tmp/nope", "description": "Danger"})
        routes.submit_pending(sid, {"command": "touch /tmp/nope", "description": "Also danger"})
        routes.submit_clarify_pending(sid, {
            "question": "Which option?",
            "choices_offered": ["A", "B", "C"],
        })

        summary = routes._session_attention_summary(sid)

        assert summary == {
            "kind": "approval",
            "count": 2,
            "severity": "critical",
        }
    finally:
        _clear_attention_state(sid)


def test_attention_summary_reports_clarify_when_no_approval_is_pending():
    sid = "attention-clarify-session"
    _clear_attention_state(sid)
    try:
        routes.submit_clarify_pending(sid, {
            "question": "Pick deploy target",
            "choices_offered": ["staging", "prod", "cancel"],
        })
        routes.submit_clarify_pending(sid, {
            "question": "Pick rollout speed",
            "choices_offered": ["slow", "fast"],
        })

        summary = routes._session_attention_summary(sid)

        assert summary == {
            "kind": "clarify",
            "count": 2,
            "severity": "question",
        }
    finally:
        _clear_attention_state(sid)


def test_sessions_api_includes_attention_summary_for_sidebar_rows(monkeypatch):
    sid = "attention-api-session"
    _clear_attention_state(sid)
    try:
        routes.submit_pending(sid, {"command": "sudo service restart", "description": "Restart"})

        monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [{
            "session_id": sid,
            "title": "Needs approval",
            "profile": "default",
            "updated_at": 1,
            "last_message_at": 1,
        }])
        monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda rows: False)
        monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

        handler = _FakeHandler()
        routes.handle_get(handler, urlparse("http://example.com/api/sessions"))

        assert handler.status == 200
        sessions = handler.json_body()["sessions"]
        assert sessions[0]["attention"] == {
            "kind": "approval",
            "count": 1,
            "severity": "critical",
        }
    finally:
        _clear_attention_state(sid)


def test_session_sidebar_renders_attention_badge_and_semantic_classes():
    sessions_js = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    style_css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert "function _sessionAttentionState" in sessions_js
    assert "needs-attention" in sessions_js
    assert "attention-approval" in sessions_js
    assert "attention-clarify" in sessions_js
    # Attention is conveyed by the colored status dot (is-attention-*), not a
    # text badge — the badge was removed in favor of a color-coded dot + rail.
    assert "is-attention-approval" in sessions_js
    assert "is-attention-clarify" in sessions_js
    assert "session-attention-badge" not in sessions_js
    assert "session_attention_approval" in sessions_js
    assert "session_attention_clarify" in sessions_js
    assert "s.attention" in sessions_js
    assert "_sessionAttentionState(s) ||" in sessions_js

    i18n_js = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    assert "session_attention_approval" in i18n_js
    assert "session_attention_clarify" in i18n_js
    assert "session_attention_approval_title" in i18n_js
    assert "session_attention_clarify_title" in i18n_js

    assert ".session-item.needs-attention" in style_css
    assert ".session-item.attention-approval" in style_css
    assert ".session-item.attention-clarify" in style_css
    # The text-badge styles were removed; the dot now carries the color.
    assert ".session-attention-badge" not in style_css
    assert ".session-state-indicator.is-attention-approval" in style_css
    assert ".session-state-indicator.is-attention-clarify" in style_css
    assert "prefers-reduced-motion" in style_css
