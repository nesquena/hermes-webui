"""Regression tests for the server-side `_LOGIN_LOCALE` parity with `static/i18n.js`.

Issue #1442: when v0.50.264 added `ja` as the 8th built-in locale, Opus pre-release
advisor surfaced that `_LOGIN_LOCALE` (in `api/routes.py`) only contained
`en/es/de/ru/zh/zh-Hant`. `ja`, `pt`, and `ko` users would see the English login page
even after their UI language was set, because `_resolve_login_locale_key()` falls
through every check and returns "en" when the locale is missing.

These tests pin two invariants going forward:

1. Every locale key registered in `static/i18n.js` LOCALES (top-level) must also
   exist as a key in `_LOGIN_LOCALE` — so adding a new locale to i18n.js without
   updating `_LOGIN_LOCALE` is caught at test time.

2. Every entry in `_LOGIN_LOCALE` must carry the full required string set
   (`lang/title/subtitle/placeholder/btn/invalid_pw/conn_failed`) and every value
   must be a non-empty string.

The companion follow-up — closing the i18n.js login-flow English-leak gaps for
`ko` (10 keys) and `es` (3 keys), and adding the 3 missing `pt` keys — is verified
in `test_login_flow_translation_parity` below: every locale must have non-English
values for the user-facing login/sign-out/password keys.

See issue #1442.
"""

from __future__ import annotations

from pathlib import Path
import importlib
import re
import sys

import pytest


REPO = Path(__file__).resolve().parent.parent
I18N_PATH = REPO / "static" / "i18n.js"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _load_login_locale() -> dict:
    """Import `_LOGIN_LOCALE` from `api.routes` without booting the HTTP server."""
    sys.path.insert(0, str(REPO))
    try:
        routes = importlib.import_module("api.routes")
    finally:
        # Don't pop — leaving sys.path[0] is fine for the rest of the suite.
        pass
    return routes._LOGIN_LOCALE


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
            j = blen if nl < 0 else nl + 1
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


# Required sub-keys for every _LOGIN_LOCALE entry.
REQUIRED_LOGIN_KEYS = (
    "lang",
    "title",
    "subtitle",
    "placeholder",
    "btn",
    "invalid_pw",
    "conn_failed",
)

# Login-flow user-facing keys that must be translated (non-English) in every locale.
# Adding a new locale to i18n.js without these translated will leak English to the
# user during the very first run / login experience.
LOGIN_FLOW_TRANSLATED_KEYS = (
    "login_title",
    "login_subtitle",
    "login_placeholder",
    "login_btn",
    "login_invalid_pw",
    "login_conn_failed",
    "sign_out",
    "sign_out_failed",
    "password_placeholder",
    "settings_saved_pw",
    "settings_saved_pw_updated",
    "auth_disabled",
    "disable_auth_confirm_title",
)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_every_i18n_locale_has_login_locale_entry():
    """Every locale in static/i18n.js LOCALES must also exist as a key in _LOGIN_LOCALE."""
    i18n_keys = _i18n_top_level_locale_keys()
    login = _load_login_locale()
    missing = [k for k in i18n_keys if k not in login]
    assert not missing, (
        f"_LOGIN_LOCALE is missing entries for these i18n locales: {missing}. "
        f"Add the matching entry in api/routes.py to keep the login page localized "
        f"for every supported locale (issue #1442)."
    )


def test_login_locale_count_matches_or_exceeds_floor():
    """_LOGIN_LOCALE must contain at least the 10 launch locales (en, it, es, de, ru, zh, zh-Hant, ja, pt, ko)."""
    login = _load_login_locale()
    assert len(login) >= 10, f"_LOGIN_LOCALE shrank: only {len(login)} entries"
    for k in ("en", "it", "es", "de", "ru", "zh", "zh-Hant", "ja", "pt", "ko"):
        assert k in login, f"_LOGIN_LOCALE missing core locale {k!r}"


@pytest.mark.parametrize("loc_key", ["en", "es", "de", "ru", "zh", "zh-Hant", "ja", "pt", "ko"])
def test_login_locale_entry_well_formed(loc_key: str):
    """Each _LOGIN_LOCALE entry must have all required sub-keys and non-empty string values."""
    login = _load_login_locale()
    entry = login[loc_key]
    assert set(entry.keys()) == set(REQUIRED_LOGIN_KEYS), (
        f"_LOGIN_LOCALE[{loc_key!r}] keys mismatch. "
        f"Expected {set(REQUIRED_LOGIN_KEYS)}, got {set(entry.keys())}."
    )
    for k, v in entry.items():
        assert isinstance(v, str) and v, f"_LOGIN_LOCALE[{loc_key!r}][{k!r}] is empty/non-str: {v!r}"


def test_login_locale_resolver_handles_new_locales():
    """_resolve_login_locale_key() must map ja/pt/ko (and common BCP-47 variants) to their entries."""
    sys.path.insert(0, str(REPO))
    from api.routes import _resolve_login_locale_key

    assert _resolve_login_locale_key("ja") == "ja"
    assert _resolve_login_locale_key("ja-JP") == "ja"
    assert _resolve_login_locale_key("ja_JP") == "ja"
    assert _resolve_login_locale_key("pt") == "pt"
    assert _resolve_login_locale_key("pt-BR") == "pt"
    assert _resolve_login_locale_key("pt-PT") == "pt"
    assert _resolve_login_locale_key("ko") == "ko"
    assert _resolve_login_locale_key("ko-KR") == "ko"
    assert _resolve_login_locale_key("fr") == "fr"
    assert _resolve_login_locale_key("fr-FR") == "fr"
    assert _resolve_login_locale_key("fr-CA") == "fr"
    # Unknown locale still falls back to en.
    assert _resolve_login_locale_key("xx-YY") == "en"


def _value_of(seg: str, key: str) -> str | None:
    m = re.search(rf"\b{re.escape(key)}:\s*'((?:\\.|[^'\\])*)'", seg)
    if m:
        return m.group(1)
    m = re.search(rf'\b{re.escape(key)}:\s*"((?:\\.|[^"\\])*)"', seg)
    if m:
        return m.group(1)
    return None


@pytest.mark.parametrize("loc_key", ["es", "de", "ru", "zh", "zh-Hant", "ja", "pt", "ko"])
def test_login_flow_keys_are_translated(loc_key: str):
    """Login/sign-out/password keys in static/i18n.js must NOT equal the English value.

    This guards the `ko` 10-key, `es` 3-key, and `pt` 3-key gaps closed in this PR.
    Adding a new locale that copies English values for these keys leaks English to
    the user during their very first interaction with the app.
    """
    en_seg = _i18n_locale_block("en")
    target_seg = _i18n_locale_block(loc_key)
    leaks = []
    for k in LOGIN_FLOW_TRANSLATED_KEYS:
        en_val = _value_of(en_seg, k)
        loc_val = _value_of(target_seg, k)
        if en_val and loc_val is not None and loc_val == en_val:
            leaks.append(f"{k}={loc_val!r}")
    assert not leaks, (
        f"Locale {loc_key!r} leaks English for login-flow keys: {leaks}. "
        f"Translate these in static/i18n.js (issue #1442)."
    )


# ── Session-management key parity ─────────────────────────────────────────────
#
# Keys added for session batch operations and multi-select (#2112).
# Every locale block must have these keys; missing them falls back to English
# which is a regression for non-English users.

SESSION_MANAGEMENT_KEYS = (
    "session_batch_delete_confirm",
    "session_batch_archive_confirm",
    "session_batch_delete_worktree_confirm",
    "session_batch_archive_worktree_confirm",
    "session_select_mode",
    "session_select_mode_desc",
    "session_select_all",
    "session_selected_count",
    "session_no_selection",
)


@pytest.mark.parametrize("loc_key", ["en", "es", "de", "ru", "zh", "zh-Hant", "ja", "pt", "ko"])
def test_session_management_keys_present(loc_key: str):
    """Every locale block must define all session-management keys (no fallback to English)."""
    seg = _i18n_locale_block(loc_key)
    missing = [k for k in SESSION_MANAGEMENT_KEYS if _value_of(seg, k) is None]
    assert not missing, (
        f"Locale {loc_key!r} is missing session-management keys: {missing}. "
        f"Add translations in static/i18n.js (issue #2112)."
    )
