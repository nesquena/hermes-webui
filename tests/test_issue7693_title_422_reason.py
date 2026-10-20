"""Residual coverage for #7693 — the manual "regenerate title" 422 body.

The borrowed-Latin title-guard half of #7693 landed upstream in PR #7727
(`_script_drift` exempts Latin inside a title that keeps a CJK core). This
file pins the second half the maintainer asked for in the issue thread: the
`/api/session/title/regenerate` 422 body is rendered verbatim in the sidebar
toast (`static/sessions.js`), so it must never surface internal title-
generation status codes such as `llm_language_mismatch_aux`.

It also re-pins the #7693 guard behavior against the shared `_script_drift`
implementation so the borrowed-Latin exemption cannot silently regress.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── #7693 guard behavior (kept via upstream _script_drift) ──────────────────

def test_cjk_start_keeps_latin_product_name():
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch(
        "微信支付回调一直失败怎么办", "WeChat Pay 回调失败排查"
    ) is False


def test_cjk_start_keeps_latin_tech_term():
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch("如何修复这个错误问题", "Python 代码修复") is False


def test_cjk_start_pure_latin_title_still_rejected():
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch("如何修复这个错误问题", "Fixing the Bug") is True


def test_cjk_start_unrelated_script_still_rejected():
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch(
        "如何修复这个错误问题", "Исправление ошибки Python"
    ) is True


def test_latin_start_cjk_title_still_rejected():
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch(
        "How do I fix this Python bug in my code?", "修复 Python 代码错误"
    ) is True


# ── the 422 body must not leak internal status codes ────────────────────────

def test_regenerate_route_translates_internal_reason():
    routes_src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    start = routes_src.index('"/api/session/title/regenerate"')
    end = routes_src.index('"/api/personality/set"', start)
    block = routes_src[start:end]

    assert "Could not generate a better title" in block
    assert "_human_title_failure_reason" in block, (
        "the regenerate 422 message must run through a human-readable mapper"
    )
    assert "reason or 'empty'" not in block, (
        "the raw internal status must not be interpolated into the 422 body"
    )


def test_human_reason_covers_reachable_manual_statuses():
    """Every status generate_session_title_for_session can return when it
    yields no title must map to readable text."""
    from api.streaming import _human_title_failure_reason

    reachable = {
        "llm_language_mismatch_aux",
        "llm_invalid_aux",
        "llm_empty_aux",
        "llm_error_aux",
        "llm_empty_reasoning",
        "llm_empty_reasoning_aux",
        "llm_length",
        "llm_length_aux",
        "missing_exchange",
        "empty_user_message",
        "title_generation_disabled",
        "empty_title",
    }
    for status in reachable:
        out = _human_title_failure_reason(status)
        assert out, f"{status} must map to non-empty text"
        assert "llm_" not in out, f"{status} still leaks an internal prefix: {out!r}"
        assert "aux" not in out, f"{status} still leaks the aux suffix: {out!r}"


def test_human_reason_passes_unknown_status_through():
    from api.streaming import _human_title_failure_reason

    assert _human_title_failure_reason("some_future_status") == "some_future_status"
    assert _human_title_failure_reason("") == ""
    assert _human_title_failure_reason(None) == ""


# ── behavioural: the real route renders the mapped reason ────────────────────
def _fake_handler(body_bytes: bytes) -> object:
    """Minimal BaseHTTPRequestHandler stand-in, in the shape the routes in
    `api/routes.py` expect (the `_FakeHandler` pattern of
    `tests/test_1560_password_env_var_no_op.py`)."""
    import io as _io

    class _Handler:
        def __init__(self, raw: bytes):
            self.status = None
            self.sent_headers = []
            self.body = bytearray()
            self.wfile = self
            self.rfile = _io.BytesIO(raw)
            self.headers = {"Content-Length": str(len(raw))}
            self.request = None

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.sent_headers.append((name, value))

        def end_headers(self):
            pass

        def write(self, data):
            self.body.extend(data)

        def header(self, name):
            for key, value in self.sent_headers:
                if key.lower() == name.lower():
                    return value
            return None

        def json_body(self):
            return json.loads(bytes(self.body).decode("utf-8"))

    return _Handler(body_bytes)


def _post_regenerate(monkeypatch, reason, next_title=""):
    """Drive the real POST /api/session/title/regenerate route with the
    session/title/persistence collaborators stubbed, exactly as the gate
    review asked, and return (status, payload, persisted_titles)."""
    from api import routes

    class _Session:
        session_id = "sid-regenerate"
        title = "Existing title"
        profile = "default"

        def compact(self):
            return {"session_id": self.session_id, "title": self.title}

    session = _Session()
    persisted = []

    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda sid: session)
    monkeypatch.setattr(
        routes,
        "generate_session_title_for_session",
        lambda s, prefer_latest=False: (next_title, reason, "raw-model-output"),
    )

    def _persist(s, title, event_reason=None):
        persisted.append(title)
        s.title = title

    monkeypatch.setattr(routes, "_persist_generated_session_title", _persist)

    raw = json.dumps({"session_id": session.session_id}).encode("utf-8")
    handler = _fake_handler(raw)
    routes.handle_post(handler, urlparse("http://localhost/api/session/title/regenerate"))
    assert handler.status is not None, "the regenerate route must answer the request"
    return handler.status, handler.json_body(), persisted, session


def test_regenerate_route_behaviour_translates_headline_status(monkeypatch):
    """Red on master: the 422 body interpolated the raw status. Green here."""
    status, payload, persisted, session = _post_regenerate(
        monkeypatch, "missing_exchange"
    )
    assert status == 422
    error = payload.get("error", "")
    assert "missing_exchange" not in error, f"raw status leaked: {error!r}"
    assert "Could not generate a better title" in error
    assert error.split("(", 1)[-1].rstrip(")").strip()
    assert persisted == [], "a failed regenerate must persist nothing"
    assert session.title == "Existing title", "the existing title must be kept"


def test_regenerate_route_behaviour_translates_every_reachable_status(monkeypatch):
    """The five statuses reachable with no title must all come back readable
    through the real route, not only through the mapper in isolation."""
    leaked = []
    for reason in (
        "llm_empty_reasoning",
        "llm_empty_reasoning_aux",
        "llm_length",
        "llm_length_aux",
        "missing_exchange",
    ):
        status, payload, persisted, session = _post_regenerate(monkeypatch, reason)
        assert status == 422, f"{reason} must 422, got {status}"
        error = payload.get("error", "")
        if reason in error or error.endswith(f"({reason})"):
            leaked.append(reason)
        assert persisted == []
    assert leaked == [], f"route leaked raw statuses: {leaked}"


def test_regenerate_route_behaviour_success_persists(monkeypatch):
    """Control case: a produced title takes the 200 path and is persisted."""
    status, payload, persisted, session = _post_regenerate(
        monkeypatch, "ok", next_title="A better title"
    )
    assert status == 200
    assert persisted == ["A better title"]
    assert payload.get("title") == "A better title"
