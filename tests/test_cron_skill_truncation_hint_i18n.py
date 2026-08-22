"""Regression tests for cron/skill truncation hint i18n parity in static/i18n.js.

PR #6141 round r14 (copy-only i18n fix) added two new keys to the i18n file:
- `cron_output_truncated_hint`: conveys that output is large and only a bounded preview
  (front-matter + response) is shown, with the full file remaining on disk.
- `skill_file_truncated_hint`: conveys that a file is large and only the first 512 KiB is shown.

The maintainer's required repair was to translate both keys in the 14 maintained
non-English locale blocks, matching the terminology/tone of each locale's existing
`logs_truncated_hint` and surrounding cron/skill labels.

This test pins two invariants going forward:

1. Every locale block must define both keys (no fallback to English).
2. Every non-English locale must have non-English values for both keys (no silent
   reverts to the English source literal).

Adding a new locale that copies English values for these keys leaks English to
non-English users and defeats the purpose of the i18n system.

See PR #6141 r14 and the upstream gate certificate's i18n MUST-FIX.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest


REPO = Path(__file__).resolve().parent.parent
I18N_PATH = REPO / "static" / "i18n.js"


# ── Helpers (copied and adapted from test_login_locale_parity.py) ─────────────


def _i18n_top_level_locale_keys() -> list[str]:
    """Return the ordered list of top-level locale keys defined in static/i18n.js LOCALES."""
    src = I18N_PATH.read_text(encoding="utf-8")
    # Find `const LOCALES = {`
    m = re.search(r"const\s+LOCALES\s*=\s*\{", src)
    assert m, "LOCALES object not found in static/i18n.js"
    body_start = m.end()
    # Walk braces to find matching close, respecting strings/comments
    depth = 1
    i = body_start
    n = len(src)
    while i < n and depth > 0:
        ch = src[i]
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            nl = src.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch in ("'", '"'):
            q = ch
            i += 1
            while i < n and src[i] != q:
                i += 2 if src[i] == "\\" else 1
            i += 1
            continue
        if ch == "`":
            i += 1
            while i < n and src[i] != "`":
                i += 2 if src[i] == "\\" else 1
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                body_end = i
                break
            i += 1
            continue
        i += 1
    else:
        raise AssertionError("LOCALES object never closed in static/i18n.js")

    body = src[body_start:body_end]

    # Top-level locale keys are at 2-space indent: either `xx: {` or `'xx-Hant': {`.
    # Use brace-tracking so we only pick up *top-level* keys, not nested ones.
    keys: list[str] = []
    j = 0
    sub_depth = 0
    blen = len(body)
    while j < blen:
        ch = body[j]
        if ch == "/" and j + 1 < blen and body[j + 1] == "/":
            nl = body.find("\n", j)
            j = blen if nl < 0 else nl
            continue
        if ch == "/" and j + 1 < blen and body[j + 1] == "*":
            end = body.find("*/", j + 2)
            j = blen if end < 0 else end + 2
            continue
        if ch in ("'", '"'):
            q = ch
            j += 1
            while j < blen and body[j] != q:
                j += 2 if body[j] == "\\" else 1
            j += 1
            continue
        if ch == "`":
            j += 1
            while j < blen and body[j] != "`":
                j += 2 if body[j] == "\\" else 1
            j += 1
            continue
        if ch == "{":
            sub_depth += 1
            j += 1
            continue
        if ch == "}":
            sub_depth -= 1
            j += 1
            continue
        # Detect top-level key only when sub_depth is 0 and we're at the start
        # of a fresh line (after a newline) at column 2.
        if sub_depth == 0 and ch == "\n":
            # Look at the next characters: `  KEY: {` where KEY is identifier or 'identifier-with-dash'
            tail = body[j + 1 : j + 200]
            mk = re.match(
                r"  (?:'(?P<q>[A-Za-z][A-Za-z0-9_-]*)'|(?P<u>[A-Za-z][A-Za-z0-9_]*))\s*:\s*\{",
                tail,
            )
            if mk:
                keys.append(mk.group("q") or mk.group("u"))
        j += 1
    # Deduplicate while preserving order (LOCALES is a single object so no dups expected,
    # but be defensive in case the file ever picks them up).
    seen = set()
    ordered_unique = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            ordered_unique.append(k)
    return ordered_unique


def _i18n_locale_block(loc: str) -> str:
    """Return the body of a specific top-level locale block in i18n.js."""
    src = I18N_PATH.read_text(encoding="utf-8")
    if "-" in loc:
        head = re.compile(rf"^  '{re.escape(loc)}':\s*\{{", re.M)
    else:
        head = re.compile(rf"^  {re.escape(loc)}:\s*\{{", re.M)
    hm = head.search(src)
    assert hm, f"locale {loc!r} not found in i18n.js"
    body_start = hm.end()
    depth = 1
    i = body_start
    n = len(src)
    while i < n and depth > 0:
        ch = src[i]
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            nl = src.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch in ("'", '"'):
            q = ch
            i += 1
            while i < n and src[i] != q:
                i += 2 if src[i] == "\\" else 1
            i += 1
            continue
        if ch == "`":
            i += 1
            while i < n and src[i] != "`":
                i += 2 if src[i] == "\\" else 1
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return src[body_start:i]
            i += 1
            continue
        i += 1
    raise AssertionError(f"locale {loc!r} block never closed")


def _value_of(seg: str, key: str) -> str | None:
    m = re.search(rf"\b{re.escape(key)}:\s*'((?:\\.|[^'\\])*)'", seg)
    if m:
        return m.group(1)
    m = re.search(rf'\b{re.escape(key)}:\s*"((?:\\.|[^"\\])*)"', seg)
    if m:
        return m.group(1)
    return None


# ── Tests ─────────────────────────────────────────────────────────────────────


# Keys that must be translated in every locale block
TRUNCATION_HINT_KEYS = (
    "cron_output_truncated_hint",
    "skill_file_truncated_hint",
)


@pytest.mark.parametrize("loc_key", ["en"])
def test_english_keys_exist_and_nonempty(loc_key: str):
    """English locale must define both truncation hint keys with non-empty values."""
    seg = _i18n_locale_block(loc_key)
    for k in TRUNCATION_HINT_KEYS:
        val = _value_of(seg, k)
        assert val is not None, f"Locale {loc_key!r} is missing key {k!r}"
        assert val, f"Locale {loc_key!r} has empty value for key {k!r}"


@pytest.mark.parametrize("loc_key", _i18n_top_level_locale_keys())
def test_every_locale_has_truncation_keys(loc_key: str):
    """Every locale block must define both truncation hint keys (no fallback to English)."""
    # Skip en as it's tested separately
    if loc_key == "en":
        return
    seg = _i18n_locale_block(loc_key)
    missing = [k for k in TRUNCATION_HINT_KEYS if _value_of(seg, k) is None]
    assert not missing, (
        f"Locale {loc_key!r} is missing truncation hint keys: {missing}. "
        f"Add translations in static/i18n.js (PR #6141 r14)."
    )


@pytest.mark.parametrize("loc_key", _i18n_top_level_locale_keys())
def test_truncation_keys_are_translated(loc_key: str):
    """Truncation hint keys in static/i18n.js must NOT equal the English value.

    This guards against silent reverts to the English source literal in non-English
    locale blocks. Adding a new locale that copies English values for these keys
    leaks English to non-English users.
    """
    # Skip en as it's the reference locale
    if loc_key == "en":
        return

    en_seg = _i18n_locale_block("en")
    target_seg = _i18n_locale_block(loc_key)

    leaks = []
    for k in TRUNCATION_HINT_KEYS:
        en_val = _value_of(en_seg, k)
        loc_val = _value_of(target_seg, k)
        if en_val and loc_val is not None and loc_val == en_val:
            leaks.append(f"{k}={loc_val!r}")

    assert not leaks, (
        f"Locale {loc_key!r} leaks English for truncation hint keys: {leaks}. "
        f"Translate these in static/i18n.js (PR #6141 r14)."
    )


@pytest.mark.parametrize("loc_key", _i18n_top_level_locale_keys())
def test_truncation_keys_are_nonempty(loc_key: str):
    """Every locale block must have non-empty values for both truncation hint keys."""
    seg = _i18n_locale_block(loc_key)
    for k in TRUNCATION_HINT_KEYS:
        val = _value_of(seg, k)
        assert val is not None, f"Locale {loc_key!r} is missing key {k!r}"
        assert val, f"Locale {loc_key!r} has empty value for key {k!r}"


# ── Reviewed copy fixture (PR #6141 r15) ───────────────────────────────────────


REVIEWED_COPY = {
    "ko": {
        "cron_output_truncated_hint": "출력이 큽니다. 제한된 미리보기(front-matter + 응답)만 표시합니다. 전체 파일은 디스크에 있습니다.",
    },
    "pl": {
        "skill_file_truncated_hint": "Plik jest duży; pokazywanych jest tylko pierwszych 512 KiB.",
    },
}


@pytest.mark.parametrize("loc_key", sorted(REVIEWED_COPY))
def test_fable_reviewed_copy_exact(loc_key: str):
    """The two r15 corrections from the gate's UX advisor are pinned byte-for-byte.

    The all-locale parity tests above guard presence/non-emptiness/non-English;
    this fixture pins the exact approved wording so a paraphrase or partial
    revert of the reviewed copy fails CI. (#6141 r15)
    """
    seg = _i18n_locale_block(loc_key)
    for k, expected in REVIEWED_COPY[loc_key].items():
        assert _value_of(seg, k) == expected, (
            f"Locale {loc_key!r} key {k!r} must carry the Fable-reviewed copy exactly"
        )
