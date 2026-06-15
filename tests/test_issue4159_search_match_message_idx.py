"""Backend regression for #4159: content-search results must surface the
matched message index so the frontend can jump straight to the hit.

The content scan in ``_handle_sessions_search`` already locates the exact
message that contains the query (it iterates ``sess.messages`` and ``break``s
on the first hit). Until now the loop index was discarded; this test pins the
new ``match_message_idx`` field on content-typed results, indexed against the
same raw ``sess.messages`` array the renderer stamps onto each row as
``data-msg-idx`` / ``msg-user-<rawIdx>``.

Title matches must NOT carry ``match_message_idx`` (there's no message-level
hit to jump to).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


def _run_search(query, *, session_messages, sessions_meta=None):
    import api.routes as routes

    meta = sessions_meta or [
        {"session_id": "s1", "title": "Untitled", "profile": "default"}
    ]
    session = SimpleNamespace(session_id="s1", messages=session_messages)
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(meta)), patch(
        "api.routes.get_session", return_value=session
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


def test_content_match_includes_message_index():
    """A content hit must carry match_message_idx pointing at the raw index
    inside sess.messages (so msg-user-<rawIdx> resolves on the client)."""
    msgs = [
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "second message — no hit"},
        {"role": "user", "content": "NEEDLE in the third message"},
        {"role": "assistant", "content": "fourth message"},
    ]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1&depth=10",
        session_messages=msgs,
    )
    assert captured["status"] == 200
    results = captured["payload"]["sessions"]
    assert len(results) == 1
    hit = results[0]
    assert hit["match_type"] == "content"
    assert hit["match_message_idx"] == 2, (
        "match_message_idx must be the raw enumerate index into sess.messages "
        "(0-based); the renderer stamps the same index onto msg-user-<rawIdx>"
    )


def test_content_match_returns_first_hit_index_not_last():
    """The scan break()s on the first hit (preserving existing behavior); the
    returned idx must reflect that first hit, not a later occurrence."""
    msgs = [
        {"role": "user", "content": "alpha NEEDLE first"},
        {"role": "user", "content": "beta NEEDLE second"},
    ]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1&depth=10",
        session_messages=msgs,
    )
    assert captured["payload"]["sessions"][0]["match_message_idx"] == 0


def test_title_match_does_not_include_message_index():
    """Title matches short-circuit before the content scan, so they must not
    grow a match_message_idx field (nothing to jump to)."""
    meta = [{"session_id": "s1", "title": "needle in the title", "profile": "default"}]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1",
        session_messages=[{"role": "user", "content": "no hit here"}],
        sessions_meta=meta,
    )
    hit = captured["payload"]["sessions"][0]
    assert hit["match_type"] == "title"
    assert "match_message_idx" not in hit


def test_no_match_returns_empty_results():
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1",
        session_messages=[{"role": "user", "content": "no hit here"}],
    )
    assert captured["payload"]["count"] == 0
