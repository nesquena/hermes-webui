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


# ── review round 2: a link with no word-like segment stays unsettled ───────
#
# The first round used any non-noise path segment, so common links were named
# after an opaque id or a scaffolding word (`dQw4w9WgXcQ`, `status`, `abs`,
# `item`, a 40-character document id). Only a segment the author wrote with
# separators reads like words; the GitHub repository and subreddit shapes keep
# their own rules. Everything else returns None — an unsettled title is better
# than a fragment.

LINK_ONLY_CASES = [
    ("https://youtu.be/dQw4w9WgXcQ", None),
    ("https://x.com/someuser/status/1862345678901234567", None),
    (
        "https://docs.google.com/document/d/"
        "1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz_-abcd/edit",
        None,
    ),
    ("https://arxiv.org/abs/2411.01234", None),
    ("https://news.ycombinator.com/item?id=41234567", None),
    (
        "https://notion.so/team/Q4-Roadmap-3f2a9c1b7d8e4f5a9b0c1d2e3f4a5b6c",
        "Q4 Roadmap",
    ),
    ("https://github.com/nesquena/hermes-webui/pull/9394", "hermes-webui"),
    (
        "https://stackoverflow.com/questions/1234567/how-to-merge-two-dicts",
        "how to merge two dicts",
    ),
]


@pytest.mark.parametrize("opener, expected", LINK_ONLY_CASES)
def test_link_only_fallback_uses_only_word_like_segments(opener, expected):
    from api.streaming import _fallback_title_from_exchange

    assert _fallback_title_from_exchange(opener, "") == expected


def test_bare_domain_with_a_generic_suffix_does_not_flip_a_korean_opener():
    """`cs.stanford.edu` is a host too: the fixed TLD list missed `.edu` and
    left the host's Latin letters voting, so a correct Korean title was
    rejected as a language mismatch."""
    from api.streaming import _title_language_mismatch

    opener = "cs.stanford.edu/projects/foo 이 프로젝트 어떻게 생각해?"
    assert _title_language_mismatch(opener, "스탠포드 프로젝트 검토") is False


# ── review round 2: the link stripper's ordering and boundaries ────────────


def test_email_is_removed_whole_before_the_domain_rule():
    from api.streaming import _strip_links_for_script_tally, _title_language_mismatch

    opener = "longaddress@example.com 이 메일 확인해줘"
    stripped = _strip_links_for_script_tally(opener)
    assert "longaddress" not in stripped, f"email local part survived: {stripped!r}"
    assert _title_language_mismatch(opener, "이 메일 확인") is False


def test_a_long_suffix_is_not_cut_into_a_domain_fragment():
    from api.streaming import _strip_links_for_script_tally, _fallback_title_from_exchange

    stripped = _strip_links_for_script_tally("example.company/project 이거 봐줘")
    assert "pany" not in stripped, f"partial host suffix survived: {stripped!r}"
    assert _fallback_title_from_exchange("example.company/project", "") is None


def test_markdown_link_does_not_eat_following_prose():
    from api.streaming import _strip_links_for_script_tally, _title_language_mismatch

    opener = "[link](https://example.com)请帮我分析这个问题"
    stripped = _strip_links_for_script_tally(opener)
    assert "请帮我分析这个问题" in stripped, f"the request was eaten: {stripped!r}"
    assert _title_language_mismatch(opener, "分析这个问题") is False


def test_agent_route_refuses_a_url_shaped_model_reply(monkeypatch):
    """The fallback was already refused; a model reply that is a URL must be
    refused on the generation path too, or 'Regenerate title' persists it."""
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {})
    monkeypatch.setattr(
        streaming,
        "generate_title_raw_via_agent",
        lambda *a, **k: ("https://www.example.com/x", "llm_stub"),
    )

    title, status, _ = streaming._generate_llm_session_title_for_agent(
        object(), "hello", "hi"
    )

    assert title is None
    assert status == "llm_invalid"


def test_aux_route_refuses_a_url_shaped_model_reply(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {})
    monkeypatch.setattr(
        streaming,
        "generate_title_raw_via_aux",
        lambda *a, **k: ("https://www.example.com/x", "llm_stub"),
    )

    title, status, _ = streaming._generate_llm_session_title_via_aux("hello", "hi")

    assert title is None
    assert status == "llm_invalid_aux"
