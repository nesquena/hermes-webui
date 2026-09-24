"""Regression coverage for #3293 — auto-generated WebUI titles drift into the
wrong language.

`_title_language_mismatch` previously only rejected English titles for *German*
conversation starts (`_detect_title_language` returns 'de' or ''). An English
start whose LLM-generated title came back in Chinese / Spanish / Russian sailed
through and persisted with a mismatched language.

The fix generalizes from a German-specific binary to a language-agnostic
cross-script check: when the conversation start has a clear dominant writing
script and the title introduces a substantial amount of a different script, the
title is rejected (and generation falls back to the deterministic topic title).
The legacy German→English same-script heuristic is preserved.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── _dominant_script ────────────────────────────────────────────────────────

def test_dominant_script_basic_buckets():
    from api.streaming import _dominant_script

    assert _dominant_script("How do I fix this bug") == "latin"
    assert _dominant_script("如何修复这个错误问题") == "cjk"
    assert _dominant_script("Привет как дела сегодня") == "cyrillic"
    assert _dominant_script("日本語のテキストです") == "cjk"  # JP folds into cjk


def test_dominant_script_undecidable_returns_empty():
    from api.streaming import _dominant_script

    # No meaningful alphabetic signal.
    assert _dominant_script("") == ""
    assert _dominant_script("12345 !@#") == ""
    assert _dominant_script("a") == ""  # below the 2-char floor
    # Evenly mixed text has no clear majority (2 latin / 2 cjk = 0.5 < 0.6).
    assert _dominant_script("ab字漢") == ""


# ── _title_language_mismatch: the #3293 cross-script drift ──────────────────

def test_english_start_chinese_title_is_rejected():
    """The reporter's exact class: English conversation, Chinese title (even
    with a borrowed Latin technical term embedded)."""
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch(
        "How do I fix this Python bug in my code?", "修复 Python 代码错误"
    ) is True


def test_english_start_cyrillic_title_is_rejected():
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch(
        "What time does the meeting start tomorrow?", "Встреча Завтра Утром"
    ) is True


def test_cjk_start_english_title_is_rejected():
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch("如何修复这个错误问题", "Fixing the Bug") is True


def test_cjk_start_mixed_cjk_latin_title_is_allowed():
    """CJK conversations frequently embed English product/technical terms in
    titles (e.g. 'WeChat Pay 回调失败排查').  As long as the title also
    contains CJK characters, Latin borrowed terms should not trigger rejection.
    Regression test for #7693."""
    from api.streaming import _title_language_mismatch

    # Pure CJK user, CJK title with English product name
    assert _title_language_mismatch(
        "微信支付回调一直失败怎么办", "WeChat Pay 回调失败排查"
    ) is False
    # CJK user, CJK title with English tech term
    assert _title_language_mismatch(
        "如何修复这个错误问题", "Python 代码修复"
    ) is False
    # Mixed CJK+Latin user, mixed title
    assert _title_language_mismatch(
        "prores raw是否能选择压缩？", "ProRes RAW 压缩选项与 BRAW 对比"
    ) is False


def test_cjk_start_pure_latin_title_still_rejected():
    """A purely Latin title for a CJK conversation is still a genuine drift."""
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch("如何修复这个错误问题", "Fixing the Bug") is True
    assert _title_language_mismatch("如何修复这个错误问题", "Python Error Guide") is True


# ── regression guards: legitimate same-script titles must NOT be rejected ───

def test_english_start_english_title_allowed():
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch(
        "Why are old images not displayed here?", "Old Image Display Issue"
    ) is False


def test_english_start_spanish_title_allowed():
    """Same (latin) script — language differs but the script check must not flag
    it; only a clearly different script is a mismatch signal."""
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch(
        "How do I fix this Python bug in my code?", "Arreglar error de Python"
    ) is False


def test_english_title_with_one_foreign_placename_allowed():
    """An otherwise-English title containing a single CJK place name stays below
    the proportion threshold and is not flagged."""
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch(
        "What is the best dataset for model training?", "Using 北京 Dataset Notes"
    ) is False


def test_same_cjk_script_title_allowed():
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch("如何修复这个错误问题", "代码错误修复") is False
    assert _title_language_mismatch("日本語で質問があります", "日本語のチャット") is False


def test_empty_title_is_not_a_mismatch():
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch("Hello there my friend", "") is False
    assert _title_language_mismatch("Hello there", "   ") is False


def test_tiny_start_without_script_signal_allows_title():
    """A start too short to establish a dominant script must not gate the title."""
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch("hi", "Quick Chat") is False


# ── legacy German→English heuristic preserved ───────────────────────────────

def test_legacy_german_start_english_title_still_rejected():
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch(
        "Warum werden alte Bilder hier nicht mehr angezeigt?",
        "Old Image Display Issue",
    ) is True


def test_legacy_german_start_german_title_allowed():
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch(
        "Warum werden alte Bilder angezeigt?", "Alte Bilder Anzeige"
    ) is False
