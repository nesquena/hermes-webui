"""Coverage and consistency tests for the German locale.

German (`de`) had no dedicated locale test while most other locales do. These
tests cover the two failure modes seen in the existing block: keys missing from
``LOCALES.de`` (they silently ride the ``en`` fallback) and translated strings
that drifted from the glossary — mixed formal/informal address and one English
term rendered with several German words.

The glossary the ``de`` block is held to:

* address the reader informally (du/dein/dich) everywhere, never Sie/Ihr
* chat/conversation (the sidebar thread) -> "Chat"/"Chats"
* workspace -> "Arbeitsbereich", provider -> "Anbieter", memory -> "Gedächtnis"
* skill -> "Skill", prompt -> "Prompt", composer -> "Eingabeleiste"
* a chat turn is paraphrased, never "Zug"; runtime is "Laufzeit"
"""

import re
from pathlib import Path

from tests.test_issue2147_profile_concept_help import PROFILE_CONCEPT_KEYS
from tests.scripts_fallback_keys import scripts_fallback_keys

REPO = Path(__file__).resolve().parent.parent
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")

# Keys that are intentionally absent from non-English locales and ride the
# ``en`` fallback (docs/GUIDELINES.md section 8, see tests/scripts_fallback_keys.py).
DE_FALLBACK_KEYS = {
    *PROFILE_CONCEPT_KEYS,
    "workspace_artifact_source_session",
    *scripts_fallback_keys("de"),
}


def extract_locale_block(locale: str) -> str:
    start = I18N_JS.index(f"\n  {locale}: {{")
    end = I18N_JS.index("\n  },\n", start)
    return I18N_JS[start:end]


DE_BLOCK = extract_locale_block("de")


def _values(block: str) -> dict:
    """key -> raw value expression (string literal or arrow function)."""
    out = {}
    for match in re.finditer(r"^ {4,}([a-zA-Z0-9_]+):[ \t]*(.+?),?$", block, re.MULTILINE):
        out[match.group(1)] = match.group(2)
    return out


DE_VALUES = _values(DE_BLOCK)
EN_VALUES = _values(extract_locale_block("en"))


def test_german_locale_block_exists():
    assert "\n  de: {" in I18N_JS
    assert "_label: 'Deutsch'" in DE_BLOCK
    assert "_speech: 'de-DE'" in DE_BLOCK


def test_german_locale_covers_english_keys():
    missing = sorted(set(EN_VALUES) - set(DE_VALUES) - DE_FALLBACK_KEYS)
    assert not missing, f"German locale missing keys: {missing}"


def test_german_locale_addresses_the_reader_informally():
    formal = re.compile(r"\b(Sie|Ihnen|Ihr|Ihre|Ihrem|Ihren|Ihrer)\b")
    offenders = {k: v for k, v in DE_VALUES.items() if formal.search(v)}
    assert not offenders, (
        "German copy must address the reader informally (du); formal address "
        f"found in: {sorted(offenders)}"
    )


def test_german_locale_uses_one_term_per_concept():
    # Several German words for the same English term read as machine
    # translation; the block keeps one term per concept.
    forbidden = {
        "Gespräch": "chat/conversation -> Chat",
        "Konversation": "chat/conversation -> Chat",
        "Unterhaltung": "chat/conversation -> Chat",
        "Eingabeaufforderung": "prompt -> Prompt",
        "Fähigkeit": "skill -> Skill",
        "Provider": "provider -> Anbieter",
        "Workspace": "workspace -> Arbeitsbereich",
        "Chat-Zug": "a chat turn is paraphrased, never Zug",
    }
    offenders = {}
    for key, value in DE_VALUES.items():
        for term, rule in forbidden.items():
            if term in value:
                offenders.setdefault(term, []).append(key)
    assert not offenders, (
        "German glossary violations (term -> agreed wording): "
        f"{ {t: sorted(keys)[:5] for t, keys in offenders.items()} }"
    )


def test_german_glossary_wording_is_used():
    # Positive coverage: the agreed wording actually appears, so a future edit
    # cannot quietly swap it back.
    joined = " ".join(DE_VALUES.values())
    for term in ("Arbeitsbereich", "Anbieter", "Gedächtnis", "Skill", "Prompt", "Chat"):
        assert term in joined, f"expected German glossary term {term!r} in the de block"
