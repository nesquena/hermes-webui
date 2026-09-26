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


# ── configured title language (auxiliary.title_generation.language) ─────────
#
# The cross-script guard above only rejects drift it can *see*. A Latin-script
# start whose title comes back in another Latin-script language is allowed on
# purpose (see test_english_start_spanish_title_allowed), so English → Spanish,
# Portuguese, Italian and friends still persist. #3293 listed "Chinese or
# Spanish"; in practice the Latin-script half is the common case and the script
# check cannot reach it.
# Hermes Agent already exposes `auxiliary.title_generation.language`, which its
# own generator applies as a hard pin. These cover the WebUI honouring it, so a
# user who has pinned a language gets it on every title path rather than only on
# native surfaces.


def test_configured_language_pins_every_title_prompt(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "English"})

    _, prompts = streaming._title_prompts("¿Cómo arreglo este error?", "Así se arregla.")

    assert prompts, "expected at least one title prompt"
    for prompt in prompts:
        assert "Write the title in English." in prompt


def test_configured_language_is_not_hardcoded_to_english(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "Deutsch"})

    _, prompts = streaming._title_prompts("How do I fix this bug", "Like this")

    for prompt in prompts:
        assert "Write the title in Deutsch." in prompt


def test_unset_language_keeps_match_user_default(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {})

    _, prompts = streaming._title_prompts("How do I fix this bug", "Like this")

    assert prompts
    for prompt in prompts:
        assert "Match the language of the user question." in prompt
        assert "Write the title in" not in prompt


def test_blank_language_is_treated_as_unset(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "   "})

    _, prompts = streaming._title_prompts("How do I fix this bug", "Like this")

    for prompt in prompts:
        assert "Match the language of the user question." in prompt
        assert "Write the title in" not in prompt


def test_unreadable_config_falls_back_to_default(monkeypatch):
    """A config read that raises must not break title generation."""
    from api import streaming

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(streaming, "_get_aux_title_config", _boom)

    _, prompts = streaming._title_prompts("How do I fix this bug", "Like this")

    for prompt in prompts:
        assert "Match the language of the user question." in prompt


# ── pinned language must survive output validation ──────────────────────────
#
# The prompt pin above is only half the contract. Both title wrappers run the
# generated title through _title_language_mismatch(user_text, title), which
# derives the expected script from the conversation start -- so a compliant
# Japanese title for an English conversation would be generated as requested
# and then discarded as llm_language_mismatch / llm_language_mismatch_aux.
# A nonblank pin is snapshotted once per attempt and is authoritative for both
# the prompt and validation; absent/blank/unreadable pins keep the #3293
# rejection behavior unchanged.


def _fake_transport(response, calls):
    def fake(*args, **kwargs):
        calls.append(kwargs)
        return response, "llm_stub"
    return fake


def test_agent_route_accepts_pinned_cross_script_title(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "Japanese"})
    calls = []
    monkeypatch.setattr(streaming, "generate_title_raw_via_agent", _fake_transport("修正方法", calls))

    title, status, _ = streaming._generate_llm_session_title_for_agent(
        object(), "How do I fix this error?", "Do it like this."
    )

    assert title == "修正方法"
    assert status == "llm_stub"
    assert calls and calls[0].get("pinned_language") == "Japanese"


def test_agent_route_still_rejects_cross_script_drift_without_pin(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {})
    monkeypatch.setattr(streaming, "generate_title_raw_via_agent", _fake_transport("修正方法", []))

    title, status, _ = streaming._generate_llm_session_title_for_agent(
        object(), "How do I fix this error?", "Do it like this."
    )

    assert title is None
    assert status == "llm_language_mismatch"


def test_aux_route_accepts_pinned_cross_script_title(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "Japanese"})
    calls = []
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("修正方法", calls))

    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )

    assert title == "修正方法"
    assert status == "llm_stub"
    assert calls and calls[0].get("pinned_language") == "Japanese"


def test_aux_route_still_rejects_cross_script_drift_without_pin(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {})
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("修正方法", []))

    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )

    assert title is None
    assert status == "llm_language_mismatch_aux"


def test_blank_pin_keeps_rejection_on_both_routes(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "   "})
    monkeypatch.setattr(streaming, "generate_title_raw_via_agent", _fake_transport("修正方法", []))
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("修正方法", []))

    agent_title, agent_status, _ = streaming._generate_llm_session_title_for_agent(
        object(), "How do I fix this error?", "Do it like this."
    )
    aux_title, aux_status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )

    assert agent_title is None and agent_status == "llm_language_mismatch"
    assert aux_title is None and aux_status == "llm_language_mismatch_aux"


def test_unreadable_config_keeps_rejection(monkeypatch):
    from api import streaming

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(streaming, "_get_aux_title_config", _boom)
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("修正方法", []))

    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )

    assert title is None
    assert status == "llm_language_mismatch_aux"


# ── pin-aware validation: drift from the PINNED language is still drift ─────
#
# Re-gate finding on dcef44db: skipping validation whenever a language was
# pinned took drift protection away from pinned installs. An English pin
# accepted CJK output, and a Japanese pin accepted Cyrillic output. A pin
# that resolves to a `_script_counts` bucket now retargets the script check
# at the configured language instead of switching it off.
#
# The Japanese-pin/CJK-accepted half of the matrix is already covered by the
# two `accepts_pinned_cross_script_title` tests above.


def test_resolve_pinned_title_scripts_mapping():
    from api.streaming import _resolve_pinned_title_scripts

    assert _resolve_pinned_title_scripts("English") == ("latin",)
    assert _resolve_pinned_title_scripts("  japanese  ") == ("cjk",)
    assert _resolve_pinned_title_scripts("Deutsch") == ("latin",)
    assert _resolve_pinned_title_scripts("Brazilian Portuguese") == ("latin",)
    assert _resolve_pinned_title_scripts("ru") == ("cyrillic",)
    # Diacritics fold onto the ASCII keys.
    assert _resolve_pinned_title_scripts("Français") == ("latin",)
    assert _resolve_pinned_title_scripts("Español") == ("latin",)
    # Unknown names and blank stay unresolved -> conversation-based fallback.
    assert _resolve_pinned_title_scripts("Klingon") == ()
    assert _resolve_pinned_title_scripts("") == ()
    # Thai gained a bucket when classification went name-based (round 4), so
    # it resolves now. It was unresolvable while Thai text was invisible to
    # _script_counts.
    assert _resolve_pinned_title_scripts("Thai") == ("thai",)
    # Native-script endonyms are also unresolved by design: the mapping keys
    # must stay ASCII because api/streaming.py is English-only
    # (test_title_generation_source_has_no_cjk_literals). Such pins keep the
    # conversation-based fallback.
    assert _resolve_pinned_title_scripts("日本語") == ()  # "Japanese" written natively


def test_agent_route_rejects_cjk_under_english_pin(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "English"})
    monkeypatch.setattr(streaming, "generate_title_raw_via_agent", _fake_transport("修正方法", []))

    title, status, _ = streaming._generate_llm_session_title_for_agent(
        object(), "How do I fix this error?", "Do it like this."
    )

    assert title is None
    assert status == "llm_language_mismatch"


def test_aux_route_rejects_cjk_under_english_pin(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "English"})
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("修正方法", []))

    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )

    assert title is None
    assert status == "llm_language_mismatch_aux"


def test_agent_route_rejects_cyrillic_under_japanese_pin(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "Japanese"})
    monkeypatch.setattr(streaming, "generate_title_raw_via_agent", _fake_transport("Исправление ошибки", []))

    title, status, _ = streaming._generate_llm_session_title_for_agent(
        object(), "How do I fix this error?", "Do it like this."
    )

    assert title is None
    assert status == "llm_language_mismatch"


def test_aux_route_rejects_cyrillic_under_japanese_pin(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "Japanese"})
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("Исправление ошибки", []))

    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )

    assert title is None
    assert status == "llm_language_mismatch_aux"


def test_aux_route_rejects_latin_under_japanese_pin(monkeypatch):
    """A model that ignores the pin and titles in the conversation language is
    still drift; rejection falls back to the deterministic topic title."""
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "Japanese"})
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("Fix Method Guide", []))

    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )

    assert title is None
    assert status == "llm_language_mismatch_aux"


def test_pinned_mode_keeps_trivial_echo_rejection(monkeypatch):
    """The pin gates only language validation; the echo/CoT sanitizer still
    runs first on both transports (#6529)."""
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "English"})
    monkeypatch.setattr(streaming, "generate_title_raw_via_agent", _fake_transport("Done", []))
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("pong", []))

    agent_title, agent_status, _ = streaming._generate_llm_session_title_for_agent(
        object(), "How do I fix this error?", "Do it like this."
    )
    aux_title, aux_status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )

    assert agent_title is None and agent_status == "llm_invalid"
    assert aux_title is None and aux_status == "llm_invalid_aux"


def test_unresolvable_pin_keeps_the_conversation_guard_on_the_wrapper(monkeypatch):
    """A pin the script map does not know changes the prompt and nothing
    else: the #3293 conversation check still runs on what comes back.

    Trusting such a pin was tried, and it switched the guard off, because a
    single-script title always agrees with its own dominant script. The map
    is the only language knowledge this module has, so a language it should
    support gets a bucket in ``_TITLE_LANGUAGE_SCRIPTS`` instead."""
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "Klingon"})
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("修正方法", []))

    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )

    assert title is None
    assert "mismatch" in status


def test_english_pin_overrides_legacy_german_heuristic(monkeypatch):
    """With a resolvable pin the conversation-based check (including the
    legacy German→English marker heuristic) must not fire: an English title
    for a German conversation is exactly what an English pin requested."""
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "English"})
    monkeypatch.setattr(streaming, "generate_title_raw_via_agent", _fake_transport("Old Image Display Issue", []))

    title, status, _ = streaming._generate_llm_session_title_for_agent(
        object(), "Warum werden alte Bilder angezeigt?", "Weil der Cache veraltet ist."
    )

    assert title == "Old Image Display Issue"
    assert status == "llm_stub"


# ── unclassified alphabets are visible to drift detection ───────────────────
#
# Follow-up finding on ed88789d: `_script_counts` dropped every alphabetic
# character outside its seven ranges, so a title written wholly in an
# unclassified script (Thai, Georgian, Armenian, half-width forms, ...) had
# nothing in the denominator and passed any pin. Classification now falls
# back to the Unicode character name, Thai/Georgian/Armenian get buckets of
# their own (and become pinnable), and anything still unrecognized counts as
# a real `other` bucket instead of vanishing.
#
# The overreach guards matter as much as the rejections here: half-width
# katakana and full-width Latin are LEGITIMATE Japanese title characters and
# must classify as cjk/latin rather than `other`.


def test_agent_route_rejects_thai_under_english_pin(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "English"})
    monkeypatch.setattr(streaming, "generate_title_raw_via_agent", _fake_transport("วิธีแก้ไข", []))

    title, status, _ = streaming._generate_llm_session_title_for_agent(
        object(), "How do I fix this error?", "Do it like this."
    )

    assert title is None
    assert status == "llm_language_mismatch"


def test_aux_route_rejects_thai_under_english_pin(monkeypatch):
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "English"})
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("วิธีแก้ไข", []))

    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )

    assert title is None
    assert status == "llm_language_mismatch_aux"


def test_unpinned_conversation_check_sees_thai_drift(monkeypatch):
    """The same invisible-alphabet hole existed with no pin at all. An
    English conversation start must reject an all-Thai title."""
    from api.streaming import _title_language_mismatch

    assert _title_language_mismatch("How do I fix this error?", "วิธีแก้ไข") is True


def test_thai_pin_is_now_resolvable_and_validates(monkeypatch):
    """Making Thai visible must not recreate the round-1 break for Thai pins:
    a Thai pin accepts compliant Thai output and rejects Latin output."""
    from api import streaming

    assert streaming._resolve_pinned_title_scripts("Thai") == ("thai",)

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "Thai"})
    calls = []
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("วิธีแก้ไข", calls))
    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )
    assert title == "วิธีแก้ไข"
    assert status == "llm_stub"

    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("Fix Method Guide", []))
    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )
    assert title is None
    assert status == "llm_language_mismatch_aux"


def test_georgian_and_armenian_resolve():
    from api.streaming import _resolve_pinned_title_scripts, _script_drift

    assert _resolve_pinned_title_scripts("Georgian") == ("georgian",)
    assert _resolve_pinned_title_scripts("Armenian") == ("armenian",)
    # Georgian output under an English pin is drift.
    assert _script_drift("გამოსწორება", "latin") is True
    # Georgian output under a Georgian pin is not.
    assert _script_drift("გამოსწორება", "georgian") is False


def test_halfwidth_and_fullwidth_forms_classify_correctly():
    """Overreach guard: half-width katakana is katakana and full-width Latin
    is Latin. Neither may land in `other` and poison a legitimate title."""
    from api.streaming import _script_counts

    counts = _script_counts("ﾒﾓ帳アプリ")  # 2 half-width, 3 regular katakana, 1 Han
    assert counts.get("cjk", 0) == 6
    assert "other" not in counts

    counts = _script_counts("Ａpp")  # full-width A + ASCII
    assert counts.get("latin", 0) == 3
    assert "other" not in counts


def test_japanese_pin_accepts_halfwidth_katakana_title(monkeypatch):
    """The overreach test for this round, end to end: a Japanese pin must
    accept a Japanese title that uses half-width forms."""
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "Japanese"})
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("ﾒﾓ帳アプリの設定", []))

    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I configure the memo app?", "Like this."
    )

    assert title == "ﾒﾓ帳アプリの設定"
    assert status == "llm_stub"


def test_mixed_script_borrowed_term_still_accepted(monkeypatch):
    """Borrowed-term control: an English-pinned title carrying one short
    foreign word under the 35% threshold must survive."""
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "English"})
    monkeypatch.setattr(
        streaming, "generate_title_raw_via_aux",
        _fake_transport("Fixing the Sawasdee ครับ Greeting Bug", []),
    )

    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )

    assert title == "Fixing the Sawasdee ครับ Greeting Bug"
    assert status == "llm_stub"


def _math_bold(text: str) -> str:
    """Map ASCII letters onto the MATHEMATICAL BOLD alphabet (U+1D400..)."""
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D400 + ord(ch) - ord("A")))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D41A + ord(ch) - ord("a")))
        else:
            out.append(ch)
    return "".join(out)


def test_mathematical_latin_counts_as_latin():
    """A styled Latin title is Latin, not an unknown script.

    "MATHEMATICAL BOLD CAPITAL E" carries no script keyword in its name, so
    name-based bucketing alone filed it under ``other`` and an English
    conversation rejected its own styled title as drift.
    """
    from api.streaming import _script_counts, _generated_title_language_mismatch

    styled = _math_bold("Error Troubleshooting Guide")
    assert _script_counts(styled) == {"latin": 25}
    assert _generated_title_language_mismatch("How do I fix this error?", styled, "") is False
    assert _generated_title_language_mismatch("How do I fix this error?", styled, "English") is False


def test_styled_latin_user_text_accepts_plain_latin_title():
    from api.streaming import _dominant_script, _generated_title_language_mismatch

    styled_user = _math_bold("How do I fix this error") + " please help"
    assert _dominant_script(styled_user) == "latin"
    assert _generated_title_language_mismatch(styled_user, "Error Troubleshooting Guide", "") is False


def test_mathematical_greek_counts_as_greek():
    from api.streaming import _script_counts, _generated_title_language_mismatch

    # MATHEMATICAL BOLD CAPITAL ALPHA, BETA, GAMMA, DELTA, EPSILON
    styled = "".join(chr(0x1D6A8 + i) for i in range(5))
    assert _script_counts(styled) == {"greek": 5}
    # ... and it is still drift for an English conversation.
    assert _generated_title_language_mismatch("How do I fix this error?", styled, "") is True


def test_enclosed_latin_is_drift_under_a_conflicting_pin():
    """Enclosed letters are category So and fail isalpha() before NFKC, which
    used to leave them out of the count entirely: a zero denominator, and a
    Latin title passing a CJK pin."""
    from api.streaming import _script_counts, _script_drift, _generated_title_language_mismatch

    assert "\u24b6".isalpha() is False  # the trap this test pins
    assert _script_counts("\u24b6\u24b7") == {"latin": 2}
    assert _script_drift("\u24b6\u24b7", "cjk") is True
    assert _generated_title_language_mismatch("\u3053\u3093\u306b\u3061\u306f", "\u24b6\u24b7", "Japanese") is True


def test_compatibility_expansion_counts_every_codepoint():
    """A ligature is two letters. Collapsing the expansion to one count kept a
    title under the threshold that it crosses when counted per codepoint."""
    from api.streaming import _script_counts, _script_drift

    title = "\u041f\u0440\u0438\u0432\u0435" + "\ufb00\ufb01"   # 5 Cyrillic + ff, fi
    assert _script_counts(title) == {"cyrillic": 5, "latin": 4}
    assert _script_drift(title, "cyrillic") is True     # 4/9 = 44%; collapsed it was 2/7 = 29%


def test_foreign_scripts_aggregate_against_the_threshold():
    """Two foreign scripts each under 35% but 60% together are drift."""
    from api.streaming import _script_counts, _script_drift, _generated_title_language_mismatch

    title = "ABCD\u03b1\u03b2\u03b3\u0430\u0431\u0432"
    assert _script_counts(title) == {"latin": 4, "greek": 3, "cyrillic": 3}
    assert _script_drift(title, "latin") is True
    assert _generated_title_language_mismatch("How do I fix this?", title, "English") is True
    # The CJK borrowed-Latin policy still wins before aggregation.
    assert _script_drift("\u4fee\u6b63 Python \u6307\u5357", "cjk") is False


def test_truly_unclassified_letters_count_as_other():
    """Letters no keyword recognizes still land in a counted bucket, so an
    all-unknown-script title can no longer pass a pin by vanishing."""
    from api.streaming import _script_counts, _script_drift

    counts = _script_counts("ᬅᬓ᭄ᬱᬭ")  # Balinese
    assert sum(counts.values()) >= 2
    assert _script_drift("ᬅᬓ᭄ᬱᬭ", "latin") is True


def test_amharic_pin_keeps_a_compliant_title(monkeypatch):
    """A cross-script pin the map resolves must not have its compliant title
    discarded.

    The prompt takes the language verbatim ("Write the title in Amharic"),
    so the model answers in Amharic while the conversation is in English.
    Validating that against the conversation start is the #3293 check
    measuring the wrong thing, and it threw the title away; Amharic now
    resolves to ``ethiopic`` and the title is checked against that."""
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "Amharic"})
    monkeypatch.setattr(
        streaming, "generate_title_raw_via_aux",
        _fake_transport("የስህተት መላ ፍለጋ", []),
    )

    title, status, _ = streaming._generate_llm_session_title_via_aux(
        "How do I fix this error?", "Do it like this."
    )

    assert title == "የስህተት መላ ፍለጋ"
    assert status == "llm_stub"


def test_amharic_pin_validates_against_ethiopic(monkeypatch):
    """Validation is retargeted at the pinned script: a title split between
    the requested language and the conversation's is drift, and so is a
    title that ignores the pin altogether."""
    from api.streaming import _generated_title_language_mismatch, _resolve_pinned_title_scripts

    assert _resolve_pinned_title_scripts("Amharic") == ("ethiopic",)
    assert _generated_title_language_mismatch(
        "How do I fix this error?", "የስህተት መላ ፍለጋ Error Guide", "Amharic"
    ) is True
    # An all-Latin title for an Amharic pin ignored the request. An earlier
    # head accepted it, because an unmapped pin validated the title against
    # its own script; with Amharic mapped it is drift like any other.
    assert _generated_title_language_mismatch(
        "How do I fix this error?", "Error Troubleshooting Guide", "Amharic"
    ) is True


def test_unmapped_pin_keeps_the_conversation_guard():
    """A pin the map cannot resolve falls back to the #3293 check. Self-
    validation was tried and it switched the guard off: an English question
    with a Russian title passed under an unmapped pin while an unpinned run
    rejected it."""
    from api.streaming import _generated_title_language_mismatch, _resolve_pinned_title_scripts

    assert _resolve_pinned_title_scripts("Klingon") == ()
    assert _generated_title_language_mismatch(
        "How do I fix the login button?", "Исправление кнопки входа", "Klingon"
    ) is True
    assert _generated_title_language_mismatch(
        "How do I fix the login button?", "Fix the login button", "Klingon"
    ) is False


def test_roman_numerals_stay_out_of_script_counts(monkeypatch):
    """NFKC expands Ⅲ to III. Counted, three Latin letters outvote the two
    ideographs in 第Ⅲ章 and a CJK chat about chapter three looks Latin, so its
    matching CJK title is rejected as drift. Number characters are excluded
    on their original category, before expansion; enclosed letters still
    expand."""
    from api import streaming
    from api.streaming import _dominant_script, _script_counts

    assert _script_counts("第Ⅲ章") == {"cjk": 2}
    assert _dominant_script("第Ⅲ章") == "cjk"
    assert _script_counts("①②") == {}
    assert _script_counts("ⒶⒷ") == {"latin": 2}

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": ""})
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport("第Ⅲ章概述", []))
    title, status, _ = streaming._generate_llm_session_title_via_aux("第Ⅲ章", "はい。")
    assert title == "第Ⅲ章概述"
    assert status == "llm_stub"


def test_amharic_pin_on_the_agent_route_too(monkeypatch):
    """Both transports share the one validator, so the agent route behaves
    identically."""
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "Amharic"})
    monkeypatch.setattr(
        streaming, "generate_title_raw_via_agent",
        lambda agent, user_text, assistant_text, pinned_language='': ("የስህተት መላ ፍለጋ", "llm_stub"),
    )

    title, _, _ = streaming._generate_llm_session_title_for_agent(
        object(), "How do I fix this error?", "Do it like this."
    )

    assert title == "የስህተት መላ ፍለጋ"


def test_blank_pin_keeps_the_conversation_check(monkeypatch):
    """The unpinned path is untouched: a CJK title on an English conversation
    is still drift."""
    from api.streaming import _generated_title_language_mismatch

    assert _generated_title_language_mismatch(
        "How do I fix this error?", "エラーの修正方法", ""
    ) is True
    assert _generated_title_language_mismatch(
        "How do I fix this error?", "Error Troubleshooting Guide", ""
    ) is False


GURMUKHI_TITLE = "ਗਲਤੀ ਠੀਕ ਕਰਨਾ"
SHAHMUKHI_TITLE = "غلطی ٹھیک کرنا"


def test_punjabi_pin_accepts_both_scripts():
    """Punjabi is written in Gurmukhi (India) and Shahmukhi, an Arabic
    script (Pakistan). An unqualified pin accepts either; an explicit script
    qualifier narrows it to one; an unrelated script is still drift."""
    from api.streaming import _generated_title_language_mismatch, _resolve_pinned_title_scripts

    for pin in ("Punjabi", "panjabi", "pa"):
        assert _resolve_pinned_title_scripts(pin) == ("gurmukhi", "arabic")
        assert _generated_title_language_mismatch(SHAHMUKHI_TITLE, SHAHMUKHI_TITLE, pin) is False
        assert _generated_title_language_mismatch(GURMUKHI_TITLE, GURMUKHI_TITLE, pin) is False
        assert _generated_title_language_mismatch(GURMUKHI_TITLE, "Исправление ошибки", pin) is True


def test_punjabi_script_qualifiers_narrow_the_pin():
    from api.streaming import _generated_title_language_mismatch, _resolve_pinned_title_scripts

    for pin in ("pa-Arab", "Punjabi (Arabic)", "Punjabi (Shahmukhi)", "pa_Arab"):
        assert _resolve_pinned_title_scripts(pin) == ("arabic",), pin
        assert _generated_title_language_mismatch(SHAHMUKHI_TITLE, SHAHMUKHI_TITLE, pin) is False
        assert _generated_title_language_mismatch(SHAHMUKHI_TITLE, GURMUKHI_TITLE, pin) is True
    for pin in ("pa-Guru", "Punjabi (Gurmukhi)"):
        assert _resolve_pinned_title_scripts(pin) == ("gurmukhi",), pin
        assert _generated_title_language_mismatch(GURMUKHI_TITLE, GURMUKHI_TITLE, pin) is False
        assert _generated_title_language_mismatch(GURMUKHI_TITLE, SHAHMUKHI_TITLE, pin) is True


def test_pa_arab_shahmukhi_title_survives_the_aux_wrapper(monkeypatch):
    """End to end: the Shahmukhi title the pin asked for is kept."""
    from api import streaming

    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: {"language": "pa-Arab"})
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", _fake_transport(SHAHMUKHI_TITLE, []))
    title, status, _ = streaming._generate_llm_session_title_via_aux(SHAHMUKHI_TITLE, "ٹھیک اے")
    assert title == SHAHMUKHI_TITLE
    assert status == "llm_stub"


def test_mongolian_pin_accepts_both_scripts_and_narrows_on_a_qualifier():
    """Mongolian is written in Cyrillic (Mongolia) and the traditional script
    (Inner Mongolia). "Traditional" names the script only beside Mongolian."""
    from api.streaming import _generated_title_language_mismatch, _resolve_pinned_title_scripts

    cyrillic, traditional = "Алдааг засах", "ᠮᠣᠩᠭᠣᠯ ᠪᠢᠴᠢᠭ"
    assert _resolve_pinned_title_scripts("Mongolian") == ("cyrillic", "mongolian")
    assert _generated_title_language_mismatch("x", cyrillic, "Mongolian") is False
    assert _generated_title_language_mismatch("x", traditional, "Mongolian") is False
    assert _generated_title_language_mismatch("x", "Error fix guide", "Mongolian") is True
    for pin in ("Mongolian (Traditional)", "Traditional Mongolian", "mn-Mong"):
        assert _resolve_pinned_title_scripts(pin) == ("mongolian",), pin
        assert _generated_title_language_mismatch("x", cyrillic, pin) is True
    assert _generated_title_language_mismatch("x", traditional, "mn-Cyrl") is True
    assert _resolve_pinned_title_scripts("Chinese (Traditional)") == ("cjk",)


def test_minority_scripts_need_a_qualifier():
    """A bare pin accepts only the scripts in majority use, so the commonest
    drift (an English title) stays visible. A user who writes Kazakh in Latin
    or Malay in Jawi opts in with a qualifier."""
    from api.streaming import _generated_title_language_mismatch, _resolve_pinned_title_scripts

    assert _resolve_pinned_title_scripts("Kazakh") == ("cyrillic",)
    assert _generated_title_language_mismatch("x", "Error fix guide", "Kazakh") is True
    assert _generated_title_language_mismatch("x", "Қатені түзету", "Kazakh") is False
    assert _generated_title_language_mismatch("x", "Qatelikti tuzetu", "kk-Latn") is False
    assert _generated_title_language_mismatch("x", "Қатені түзету", "kk-Latn") is True
    assert _resolve_pinned_title_scripts("kk-Arab") == ("arabic",)
    assert _resolve_pinned_title_scripts("Malay (Jawi)") == ("arabic",)
    assert _resolve_pinned_title_scripts("Hindi (Roman)") == ("latin",)


def test_punjabi_pin_accepts_a_switch_between_its_scripts():
    """The pinned check replaces the conversation check: a Gurmukhi
    conversation may get a Shahmukhi title under a bare Punjabi pin."""
    from api.streaming import _generated_title_language_mismatch

    assert _generated_title_language_mismatch(GURMUKHI_TITLE, SHAHMUKHI_TITLE, "Punjabi") is False
    assert _generated_title_language_mismatch(SHAHMUKHI_TITLE, GURMUKHI_TITLE, "Punjabi") is False


def test_qualifier_beats_the_language_token_wherever_it_sits():
    from api.streaming import _resolve_pinned_title_scripts

    assert _resolve_pinned_title_scripts("Egyptian Arabic") == ("arabic",)
    assert _resolve_pinned_title_scripts("Egyptian Arabic (Latin)") == ("latin",)
    assert _resolve_pinned_title_scripts("Latin Egyptian Arabic") == ("latin",)
    # accented qualifier is folded before the lookup
    assert _resolve_pinned_title_scripts("Punjabi (Gurmukh\u012b)") == ("gurmukhi",)
    # a BCP 47 private-use section is not a script qualifier
    assert _resolve_pinned_title_scripts("zh-Hant-TW-x-latn") == ("cjk",)


def test_lone_characters_outside_a_tag_do_not_end_parsing():
    """Initials and punctuation between words are skipped, so the language
    and any qualifier after them still count."""
    from api.streaming import _resolve_pinned_title_scripts

    assert _resolve_pinned_title_scripts("U.S. English") == ("latin",)
    assert _resolve_pinned_title_scripts("S. Korean") == ("cjk",)
    assert _resolve_pinned_title_scripts("Punjabi \u2013 Shahmukhi") == ("arabic",)
    assert _resolve_pinned_title_scripts("Serbian \u2013 Latin") == ("latin",)
    assert _resolve_pinned_title_scripts("Punjabi + Shahmukhi") == ("arabic",)
    assert _resolve_pinned_title_scripts("zh-Hant-u-nu-hanidec") == ("cjk",)


def test_script_drift_with_no_expected_script_is_not_drift():
    from api.streaming import _script_drift

    assert _script_drift("Error fix guide", ()) is False


def test_serbian_resolves_only_with_a_qualifier():
    """Unqualified Serbian stays unmapped (no majority script) and keeps the
    conversation check; a script qualifier makes it resolvable."""
    from api.streaming import _resolve_pinned_title_scripts

    assert _resolve_pinned_title_scripts("Serbian") == ()
    assert _resolve_pinned_title_scripts("sr-Latn") == ("latin",)
    assert _resolve_pinned_title_scripts("Serbian (Cyrillic)") == ("cyrillic",)
    assert _resolve_pinned_title_scripts("zh-Hant-TW") == ("cjk",)
    assert _resolve_pinned_title_scripts("pt-BR") == ("latin",)
