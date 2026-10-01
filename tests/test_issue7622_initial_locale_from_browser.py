"""Regression coverage for #7622 browser-based first-visit locale selection.

These tests execute the split production core, hold its requested script,
register the matching locale bundle, and await the active locale and storage.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
I18N_CORE_PATH = REPO_ROOT / "static" / "i18n-core.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _build_driver(stored_value, navigator_obj):
    if navigator_obj is None:
        nav_literal = "undefined"
    elif isinstance(navigator_obj, str):
        nav_literal = json.dumps({"language": navigator_obj})
    elif isinstance(navigator_obj, list):
        nav_literal = json.dumps({"languages": navigator_obj, "language": navigator_obj[0]})
    else:
        raise TypeError(navigator_obj)
    stored_seed = "" if stored_value is None else f"storage['hermes-lang'] = {json.dumps(stored_value)};"
    locale_dir = json.dumps(str(REPO_ROOT / "static" / "locales"))
    return f"""
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const src = {I18N_CORE_PATH.read_text(encoding="utf-8")!r};
const localeDir = {locale_dir};
const storage = {{}};
{stored_seed}
const scripts = [];
const ctx = {{
  URL,
  localStorage: {{
    getItem: (key) => Object.prototype.hasOwnProperty.call(storage, key) ? storage[key] : null,
    setItem: (key, value) => {{ storage[key] = String(value); }},
  }},
  document: {{
    baseURI: 'https://example.test/',
    currentScript: null,
    documentElement: {{ lang: '' }},
    querySelectorAll: () => [],
    createElement: () => ({{ async: false }}),
    head: {{ appendChild: (script) => scripts.push(script) }},
  }},
  navigator: {nav_literal},
}};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const started = vm.runInContext('loadLocale()', ctx);
const requested = storage['hermes-lang'] || null;
for (const script of scripts) {{
  const filename = script.src.split(/[?#]/, 1)[0].split('/').pop();
  vm.runInContext(fs.readFileSync(path.join(localeDir, filename), 'utf8'), ctx);
  script.onload();
}}
Promise.resolve(started).then((result) => {{
  process.stdout.write(JSON.stringify({{
    lang: vm.runInContext('getActiveLocale()', ctx),
    requested,
    result: result.status,
    stored: storage['hermes-lang'] || null,
    htmlLang: ctx.document.documentElement.lang,
    loaded: vm.runInContext('Object.keys(LOCALES)', ctx),
    requestCount: scripts.length,
  }}));
}});
"""


@pytest.fixture(scope="module")
def i18n_src() -> str:
    return I18N_CORE_PATH.read_text(encoding="utf-8")


def _run(driver_src: str) -> str:
    """Run a driver in a fresh node subprocess and return stdout."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(driver_src)
        path = f.name
    try:
        result = subprocess.run(
            [NODE, path], capture_output=True, text=True, timeout=30
        )
    finally:
        Path(path).unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"node driver failed: rc={result.returncode} stderr={result.stderr!r}"
        )
    return result.stdout.strip()


# ── 1. test_first_visit_zh_cn_falls_back_to_zh ────────────────────────────


class TestInitialLocaleFromBrowserHint:

    def test_first_visit_zh_cn_falls_back_to_zh(self, i18n_src):
        """#7622 repro: a fresh install on a Chinese browser must default
        to ``zh`` without the user having to open Settings.

        `navigator.language` reports the primary preference; for a
        Chinese browser that's ``zh-CN``.  `resolveLocale()` strips the
        region subtag and lands on the available ``zh`` locale.
        """
        driver = _build_driver(stored_value=None, navigator_obj="zh-CN")
        out = _run(driver)
        result = json.loads(out)
        assert result["lang"] == "zh", (
            f"first-visit zh-CN browser must default to 'zh' (the "
            f"available Chinese locale). Got: {out!r}"
        )
        assert result["stored"] == "zh"
        assert result["htmlLang"] == "zh-CN"
        assert result["loaded"] == ["en", "zh"]
        assert result["requestCount"] == 1

    def test_first_visit_en_us_falls_back_to_en(self, i18n_src):
        """First-visit en-US must still default to en (regression guard
        — the fix must not break the most common case)."""
        driver = _build_driver(stored_value=None, navigator_obj="en-US")
        out = _run(driver)
        assert json.loads(out)["lang"] == "en", (
            f"first-visit en-US browser must default to 'en'. "
            f"Got: {out!r}"
        )

    def test_first_visit_fr_fr_falls_back_to_fr(self, i18n_src):
        """First-visit fr-FR must default to fr (one of the 14
        supported locales)."""
        driver = _build_driver(stored_value=None, navigator_obj="fr-FR")
        out = _run(driver)
        result = json.loads(out)
        assert result["lang"] == "fr", (
            f"first-visit fr-FR browser must default to 'fr'. "
            f"Got: {out!r}"
        )
        assert result["stored"] == "fr"
        assert result["htmlLang"] == "fr-FR"
        assert result["loaded"] == ["en", "fr"]
        assert result["requestCount"] == 1

    def test_first_visit_unsupported_locale_falls_back_to_en(self, i18n_src):
        """First-visit on a browser that reports a locale with no
        translation available must still default to English (not crash
        or hang)."""
        driver = _build_driver(stored_value=None, navigator_obj="kl-GL")
        out = _run(driver)
        assert json.loads(out)["lang"] == "en", (
            f"unsupported browser locale must fall back to 'en'. "
            f"Got: {out!r}"
        )

    def test_stored_preference_overrides_browser_hint(self, i18n_src):
        """If the user has already chosen a language (or the server
        has persisted one), the stored value MUST win — the browser
        hint is only a first-visit fallback.  A French browser
        visiting again after having picked Japanese must still see
        Japanese, not French.
        """
        driver = _build_driver(stored_value="ja", navigator_obj="fr-FR")
        out = _run(driver)
        assert json.loads(out)["lang"] == "ja", (
            f"stored preference must override the browser hint. "
            f"Got: {out!r}"
        )

    def test_navigator_languages_takes_precedence_over_navigator_language(
        self, i18n_src
    ):
        """`navigator.languages[0]` (the ordered preference list) is
        preferred over `navigator.language` (the single primary) when
        the list is available.  Chrome and Firefox both expose the
        list.
        """
        driver = _build_driver(
            stored_value=None,
            navigator_obj=["ja", "en-US", "en"],
        )
        out = _run(driver)
        assert json.loads(out)["lang"] == "ja", (
            f"navigator.languages[0] must drive the first-visit hint. "
            f"Got: {out!r}"
        )

    def test_missing_navigator_falls_back_to_en(self, i18n_src):
        """Some embedded webviews (Tauri desktop shell, certain kiosk
        modes) report no `navigator` at all.  loadLocale() must
        not crash and must default to English.
        """
        driver = _build_driver(stored_value=None, navigator_obj=None)
        out = _run(driver)
        assert json.loads(out)["lang"] == "en", (
            f"missing navigator must default to 'en'. Got: {out!r}"
        )

    def test_browser_hint_never_consulted_when_stored_exists(
        self, i18n_src
    ):
        """Belt-and-braces: even with a stored English preference, a
        browser hint for, say, Japanese must not sneak through on a
        second visit.  This pins the 'stored wins' contract."""
        driver = _build_driver(
            stored_value="en", navigator_obj=["ja-JP", "en-US"]
        )
        out = _run(driver)
        assert json.loads(out)["lang"] == "en", (
            f"stored 'en' must win over a browser ja-JP hint. "
            f"Got: {out!r}"
        )


# ── 2. Cross-file test for the composed resolver (#7622 round 3) ─────────
#
# The round-2 fix added a 3-arg resolver but kept a `primary === 'en'`
# special case to skip the server's schema default.  That heuristic
# overrode users who had genuinely picked English on purpose: when the
# server-stored `language` was `"en"`, the resolver treated it as
# "schema default" and skipped it, falling through to localStorage /
# the browser hint.  The round-3 fix drops the schema default at the
# source (`api/config.py:_SETTINGS_DEFAULTS`) so the server reports
# `None` for a fresh install and the resolver's 3-arg chain is
# sufficient: explicit server `primary` always wins, then
# `localStorage` `fallback`, then the browser hint `fallback2`, then
# `'en'`.
#
# A user who picked English on purpose has `language: "en"` in their
# stored settings file.  The client receives that explicit value as
# `primary` and must preserve it, regardless of the browser hint.  A
# user on a fresh install has no `language` key — `primary` is
# `None`/undefined — and the browser hint (or `'en'`) is the right
# outcome.  These two cases must not be conflated.


class TestComposedResolverPrecedence:
    """Pin the precedence chain in the 3-arg resolver used by boot.js
    (settings hydration) and panels.js (settings modal).  Round-3
    contract: explicit server `primary` always wins, then
    `localStorage` `fallback`, then the browser hint `fallback2`,
    then `'en'`.  No `primary === 'en'` skip."""

    def _build_resolver_driver(self, i18n_src, primary, fallback, fallback2):
        arguments = ", ".join(
            "null" if value is None else json.dumps(value)
            for value in (primary, fallback, fallback2)
        )
        call = json.dumps(f"resolvePreferredLocale({arguments})")
        return f"""
const vm = require('vm');
const src = {i18n_src!r}.replace(/\\nloadLocale\\(\\);\\s*$/, '');
const ctx = {{localStorage: {{getItem: () => null, setItem: () => {{}}}}}};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const lang = vm.runInContext({call}, ctx);
process.stdout.write(JSON.stringify({{lang}}));
"""

    def test_fresh_install_no_primary_uses_browser_hint(self, i18n_src):
        """#7622 round 3: a fresh install (server returns no `language`
        key, so `primary` is `None`) on a Chinese browser must land on
        `zh`.  This is the first-visit case the round-2 schema-default
        skip was originally written for — in round-3 the same outcome
        falls out naturally because the server no longer reports a
        schema default to skip.
        """
        driver = self._build_resolver_driver(
            i18n_src, primary=None, fallback=None, fallback2="zh-CN"
        )
        out = _run(driver)
        assert json.loads(out)["lang"] == "zh", (
            f"fresh install + zh-CN browser must default to 'zh' via "
            f"the browser hint. Got: {out!r}"
        )

    def test_fresh_install_with_stored_ja_returns_ja(self, i18n_src):
        """Stored preference must always win over the browser hint."""
        driver = self._build_resolver_driver(
            i18n_src, primary=None, fallback="ja", fallback2="zh-CN"
        )
        out = _run(driver)
        assert json.loads(out)["lang"] == "ja", (
            f"a stored 'ja' must win over the browser hint 'zh-CN'. "
            f"Got: {out!r}"
        )

    def test_explicit_server_zh_with_browser_ja_returns_zh(self, i18n_src):
        """An explicit server language is treated as a real preference
        and must win over the browser hint, even with no stored
        choice."""
        driver = self._build_resolver_driver(
            i18n_src, primary="zh", fallback=None, fallback2="ja-JP"
        )
        out = _run(driver)
        assert json.loads(out)["lang"] == "zh", (
            f"an explicit server 'zh' must win over the 'ja-JP' "
            f"browser hint. Got: {out!r}"
        )

    def test_explicit_server_zh_with_stored_fr_returns_zh(
        self, i18n_src
    ):
        """Server explicit > stored > browser > 'en'."""
        driver = self._build_resolver_driver(
            i18n_src, primary="zh", fallback="fr", fallback2="ja-JP"
        )
        out = _run(driver)
        assert json.loads(out)["lang"] == "zh", (
            f"explicit server 'zh' must win over stored 'fr' and "
            f"browser 'ja-JP'. Got: {out!r}"
        )

    def test_no_preference_anywhere_falls_back_to_en(self, i18n_src):
        """No server, no stored, no browser hint -> 'en' (the safety
        net).  Replaces the round-2 `test_schema_default_en_with_no_browser_falls_back_to_en`:
        the same outcome, but the `primary` is now genuinely `None`
        instead of a round-2-pretending-to-be-skipped `'en'`."""
        driver = self._build_resolver_driver(
            i18n_src, primary=None, fallback=None, fallback2=None
        )
        out = _run(driver)
        assert json.loads(out)["lang"] == "en", (
            f"no preference at all must default to 'en'. Got: {out!r}"
        )

    def test_explicit_saved_english_wins_over_browser_zh(self, i18n_src):
        """#7622 round-3 BRICK regression: a user who explicitly saved
        English (server has `language: "en"`, localStorage may or may
        not have a value yet) on a non-English browser must still land
        on English.  This is the case the round-2
        `primary === 'en'` skip overrode.

        Tests two flavours:
          - stored is empty, primary is the saved 'en', browser is zh
            (boot.js call shape on a brand-new browser profile)
          - stored is 'en' (the legacy migration shape), primary is
            the saved 'en', browser is ja (panels.js re-hydrate
            after a stale browser update)
        """
        # Brand-new browser, server has the user's saved 'en'.
        driver = self._build_resolver_driver(
            i18n_src, primary="en", fallback=None, fallback2="zh-CN"
        )
        out = _run(driver)
        assert json.loads(out)["lang"] == "en", (
            f"server-saved 'en' must beat a zh-CN browser hint. "
            f"Got: {out!r}"
        )

        # Stale browser, server still has the user's saved 'en'.
        driver = self._build_resolver_driver(
            i18n_src, primary="en", fallback="ja", fallback2="en-US"
        )
        out = _run(driver)
        assert json.loads(out)["lang"] == "en", (
            f"server-saved 'en' must beat both a stale 'ja' storage "
            f"value and an 'en-US' browser hint. Got: {out!r}"
        )


# ── 3. Reviewer 4-row table regression (#7730 round 4) ──────────────────
#
# The maintainer reproduced the round-2 bug by loading the real
# `static/i18n-core.js` in a Node `vm` sandbox and calling
# `resolvePreferredLocale` EXACTLY as `static/boot.js` calls it:
#
#     resolvePreferredLocale(s.language,
#                            localStorage.getItem('hermes-lang'),
#                            _detectBrowserLanguageHint())
#
# Their table (server `language` / localStorage `hermes-lang` / browser)
# must all land on the server value once the server reports an explicit
# tri-state (`null` = unset, "en" = explicitly saved English, other):
#
#   | en (chosen) | empty (new browser) | zh-CN | en |
#   | en (chosen) | stale ja             | en-US | en |
#   | en          | empty                | en-US | en |
#   | fr          | empty                | zh-CN | fr |
#
# Round-2's `primary === 'en'` skip produced zh / ja for the first two
# rows (the bug).  Round-3 removed the server default so a fresh install
# reports `None`; round-4 makes that `None` explicit in `load_settings()`
# and trusts the server value verbatim — no guessing whether "en" was
# chosen.  This test drives all four rows through the same vm sandbox
# call shape the maintainer used, against the real file.


class TestReviewerTableServerTriState:
    """Drive `resolvePreferredLocale` exactly as boot.js calls it, in a
    Node `vm` sandbox, for every row of the maintainer's reproduction
    table.  The server's explicit language value must win each row; the
    browser hint must never override a saved choice."""

    def _build_boot_shape_driver(self, i18n_src, server_lang, stored, browser_langs):
        js_server = "null" if server_lang is None else json.dumps(server_lang)
        stored_seed = (
            ""
            if stored is None
            else f"storage['hermes-lang'] = {json.dumps(stored)};"
        )
        nav_literal = json.dumps({
            "languages": browser_langs,
            "language": browser_langs[0] if browser_langs else "",
        })
        call = (
            f"resolvePreferredLocale({js_server}, "
            "localStorage.getItem('hermes-lang'), _detectBrowserLanguageHint())"
        )
        return f"""
const vm = require('vm');
const src = {i18n_src!r}.replace(/\\nloadLocale\\(\\);\\s*$/, '');
const storage = {{}};
{stored_seed}
const ctx = {{
  localStorage: {{
    getItem: (key) => Object.prototype.hasOwnProperty.call(storage, key) ? storage[key] : null,
    setItem: (key, value) => {{ storage[key] = String(value); }},
  }},
  navigator: {nav_literal},
}};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const result = vm.runInContext({json.dumps(call)}, ctx);
process.stdout.write(JSON.stringify(result));
"""

    def test_four_row_table_server_tri_state_boot_shape(self, i18n_src):
        """#7730 reviewer table — all four rows, server value must win."""
        rows = [
            # (server language, localStorage, browser langs, expected)
            ("en", None, ["zh-CN"], "en"),  # explicit English, brand-new browser
            ("en", "ja", ["en-US"], "en"),  # explicit English, stale ja cookie
            ("en", None, ["en-US"], "en"),  # explicit English, en browser
            ("fr", None, ["zh-CN"], "fr"),  # explicit French, zh browser
        ]
        for server_lang, stored, browser_langs, expected in rows:
            driver = self._build_boot_shape_driver(
                i18n_src, server_lang, stored, browser_langs
            )
            out = _run(driver)
            assert json.loads(out) == expected, (
                f"server={server_lang!r} stored={stored!r} "
                f"browser={browser_langs!r} -> expected {expected!r}, "
                f"got {out!r}"
            )
