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

import sys
from pathlib import Path

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
