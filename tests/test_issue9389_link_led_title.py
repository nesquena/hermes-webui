"""Regression coverage for #9389 — a link-led opener makes the language guard
read the link's Latin characters as conversation text, so a correct non-Latin
title is rejected and the local fallback names the chat after a URL fragment
(`https www`, `https github`).

Two independent mechanisms produce the same shape:

1. `_title_language_mismatch()` tallies the script of the *raw* opener. A single
   URL carries ~30 Latin characters, which can push a CJK/Hangul opener past the
   60% majority the classifier needs, so the title is rejected as
   `llm_language_mismatch_aux`.
2. `_fallback_title_from_exchange()` splits the opener on the first `.`, which
   is the dot in `https://www.example.com`, so the "sentence" becomes
   `https://www` and the Latin token scan yields `https www`.

The fix strips links (and bare domains/paths) before the script tally — never
from the title side — makes the fallback builder URL aware, and refuses to
persist a URL-shaped candidate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── mechanism 1: the script tally must ignore link characters ───────────────

KOREAN_WITH_LINKS = (
    "https://www.reddit.com/r/LocalLLaMA/comments/abc123/model_runs_local "
    "https://github.com/searxng/searxng/issues/6819 "
    "모델 데스크탑 실행 검토 중인데 어떻게 생각해?"
)


def test_link_heavy_korean_opener_is_cjk_dominant():
    """Links must not vote on the conversation's script."""
    from api.streaming import _dominant_script, _strip_links_for_script_tally

    assert _dominant_script(_strip_links_for_script_tally(KOREAN_WITH_LINKS)) == "cjk", (
        "the raw opener's Latin characters come from URLs the user never typed "
        "as words; after stripping links the Hangul text is the only signal"
    )


def test_correct_korean_title_is_accepted_for_a_link_led_opener():
    """The reporter's exact class: a correct Hangul title rejected because the
    opener's links flipped the classification to latin."""
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch(
        KOREAN_WITH_LINKS, "Reddit LocalLLaMA 모델 데스크탑 실행 검토"
    ) is False, (
        "the model returned the correct script; rejecting it is what forced the "
        "URL-shaped fallback title"
    )


def test_link_only_latin_opener_still_rejects_a_korean_title():
    """The guard must not simply switch off: a genuinely Latin opener keeps
    rejecting a Korean title."""
    from api.streaming import _title_language_mismatch

    latin_opener = (
        "https://www.reddit.com/r/LocalLLaMA/comments/abc123/model_runs_local "
        "https://github.com/searxng/searxng/issues/6819"
    )
    assert _title_language_mismatch(
        latin_opener, "모델 데스크탑 실행 검토"
    ) is True


def test_mixed_link_opener_does_not_fall_back_to_the_raw_text():
    """A link plus prose is still prose: the strip must not hand the tally back
    to the raw text just because some Latin survives in the user's own words."""
    from api.streaming import _title_language_mismatch

    opener = (
        "https://github.com/owner/phonetrack "
        "갤럭시 삼성 메시지 PhoneTrack 수신 문제"
    )
    assert _title_language_mismatch(
        opener, "갤럭시 삼성 메시지 PhoneTrack 수신 문제"
    ) is False, (
        "the opener's own Hangul and its borrowed product name are the same "
        "balance as the title, so the title is on-script"
    )


# ── mechanism 2: the fallback builder must be URL aware ─────────────────────

@pytest.mark.parametrize(
    "opener, expected",
    [
        ("https://github.com/owner/phonetrack", "phonetrack"),
        ("https://github.com/searxng/searxng/issues/6819", "searxng"),
        ("https://www.reddit.com/r/SomeSub/comments/abc", "SomeSub"),
        (
            "https://www.reddit.com/r/SomeSub/comments/abc/s/xyz987randomid",
            "SomeSub",
        ),
        ("https://www.example.com/x", "example"),
    ],
)
def test_fallback_names_the_link_topic_not_a_url_fragment(opener, expected):
    from api.streaming import _fallback_title_from_exchange

    got = _fallback_title_from_exchange(opener, "")
    assert got is not None
    assert "https" not in got.lower(), f"fallback still URL shaped: {got!r}"
    assert "www" not in got.lower(), f"fallback still URL shaped: {got!r}"
    assert expected.lower() in got.lower(), (
        f"expected the link's topic segment {expected!r}, got {got!r}"
    )


def test_fallback_keeps_prose_when_the_opener_has_a_link_and_text():
    from api.streaming import _fallback_title_from_exchange

    got = _fallback_title_from_exchange(
        "https://github.com/searxng/searxng/issues/6819 check this compose setup",
        "",
    )
    assert got is not None
    assert "https" not in got.lower()
    assert "compose" in got.lower(), f"the prose was dropped: {got!r}"


# ── mechanism 3: a URL-shaped candidate is never persisted ─────────────────

@pytest.mark.parametrize(
    "candidate",
    [
        "https www",
        "https github",
        "https example",
        "https://www.example.com/x",
        "www.example.com",
        "https example.com",
    ],
)
def test_url_shaped_titles_are_refused(candidate):
    from api.streaming import _is_generic_fallback_title

    assert _is_generic_fallback_title(candidate) is True, (
        f"{candidate!r} is a URL fragment, not a title; leaving the title "
        "unsettled is better than locking in the fragment"
    )


@pytest.mark.parametrize(
    "candidate",
    [
        "SearXNG 이슈 6819 Compose 적용 가능성 확인",
        "GitHub Issue Triage",
        "phonetrack",
    ],
)
def test_real_titles_are_not_refused(candidate):
    from api.streaming import _is_generic_fallback_title

    assert _is_generic_fallback_title(candidate) is False
