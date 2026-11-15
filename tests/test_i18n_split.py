'''Regression tests for the i18n split (issue #6652).

static/i18n.js used to be one ~2MB file carrying every language. It is now an
eager core (English + registry) plus one bundle per language under
static/locales/. These tests pin the contract that split must keep:

1. English stays eager and complete -- it is the fallback for every key, so
   it can never be the thing that fails to load.
2. Every shipped language has a LOCALE_META row and a bundle file, so
   Settings can list all languages without downloading any of them.
3. resolveLocale() validates against LOCALE_META, not the loaded LOCALES
   registry. Checking the registry rejects every not-yet-fetched code and
   makes the lazy path unreachable (a real bug while landing the split).
4. Bundles register through registerLocale() rather than mutating LOCALES.
'''

from __future__ import annotations

import re
from pathlib import Path

from tests._i18n_source import (
    _object_entries,
    catalog_source,
    executable_source,
    locale_block,
    locale_codes,
)

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / 'static' / 'i18n.js'
LOCALES_DIR = REPO / 'static' / 'locales'


def _core() -> str:
    return CORE.read_text(encoding='utf-8')


def test_english_bundle_stays_eager():
    '''English must live in the core file, not in static/locales/.'''
    assert len(locale_block('en')) > 10000, (
        'the eager English bundle looks empty; every key falls back to it'
    )
    assert not (LOCALES_DIR / 'en.js').exists(), (
        'English must stay eager; a lazy English bundle would make the'
        ' fallback depend on a network fetch.'
    )


def test_every_locale_has_a_manifest_row_and_a_bundle():
    """LOCALE_META and static/locales/ must agree, or Settings breaks."""
    core = _core()
    meta_block = core[core.index("const LOCALE_META"):]
    for code in locale_codes():
        dq = chr(34)
        sq = chr(39)
        assert ((dq + code + dq) in meta_block) or ((sq + code + sq) in meta_block), (
            "locale %r has no LOCALE_META row; Settings would not list it" % code
        )
        if code == "en":
            continue
        bundle = LOCALES_DIR / (code + ".js")
        assert bundle.is_file(), "missing bundle for locale %r: %s" % (code, bundle)

def test_no_orphan_locale_bundles():
    '''Every file in static/locales/ must be a manifest-listed locale.'''
    codes = set(locale_codes())
    for bundle in sorted(LOCALES_DIR.glob('*.js')):
        assert bundle.stem in codes, (
            'static/locales/%s is not in LOCALE_META' % bundle.name
        )


def test_bundles_register_instead_of_mutating_the_registry():
    '''Bundles call registerLocale(); they must not assign into LOCALES.'''
    for bundle in sorted(LOCALES_DIR.glob('*.js')):
        src = bundle.read_text(encoding='utf-8')
        assert 'registerLocale(' in src, (
            '%s must register via registerLocale()' % bundle.name
        )
        assert 'LOCALES[' not in src, (
            '%s mutates LOCALES directly; a late bundle could half-update the UI'
            % bundle.name
        )


def test_resolve_locale_accepts_unfetched_codes():
    '''resolveLocale() must validate against the manifest, not the registry.'''
    core = _core()
    fn = core[core.index('function resolveLocale('):]
    fn = fn[: fn.index('\n}')]
    assert 'LOCALE_META' in fn, (
        'resolveLocale() must consult LOCALE_META so unfetched codes resolve'
    )
    for stale in ('LOCALES[raw]', 'Object.keys(LOCALES)'):
        assert stale not in fn, (
            'resolveLocale() still validates against the loaded registry via %r' % stale
        )


def test_set_locale_stays_synchronous():
    '''setLocale() must keep returning synchronously; call sites rely on it.'''
    core = _core()
    fn = core[core.index('function setLocale('):]
    fn = fn[: fn.index('\n}')]
    assert '_locale = LOCALES[resolved] || LOCALES.en' in fn, (
        'setLocale() must fall back to English when the bundle is not loaded yet'
    )
    assert 'ensureLocale(resolved)' in fn, (
        'setLocale() must trigger the lazy fetch, or switching languages does nothing'
    )
    assert 'await' not in fn, (
        'setLocale() must not become async; existing call sites do not await it'
    )


def test_history_parsers_still_see_every_locale():
    src = catalog_source()
    for code in locale_codes():
        quoted = code if all(c.isalnum() or c == "_" for c in code) else chr(39) + code + chr(39)
        assert ("  %s: {" % quoted) in src, (
            "locale %r missing from reconstructed catalog" % code
        )




def test_catalog_reconstruction_is_executable():
    """The rebuilt catalog must evaluate, not just parse.

    The English bundle references the _i18n* helper declarations and
    resolveLocale() references LOCALE_META, so a reconstruction that drops
    either throws ReferenceError as soon as anything executes it
    (greptile review: test_issue7622 evaluates exactly this source).
    """
    src = catalog_source()
    for needed in (
        "_i18nProcessedElapsedEn",
        "_i18nToolActionLabelEn",
        "const LOCALE_META",
    ):
        assert needed in src, "reconstruction is missing %r" % needed


def test_executable_source_has_registry_and_bundles():
    """Tests that RUN i18n.js need the real core plus registered bundles."""
    src = executable_source()
    assert "const LOCALE_META" in src, "executable source lost the registry"
    assert "function ensureLocale(" in src, "executable source lost ensureLocale()"
    for code in locale_codes():
        if code == "en":
            continue
        assert "registerLocale(" in src, "executable source has no registerLocale()"


def test_string_scanner_respects_the_opening_quote():
    """A double quote inside a single-quoted string must not end it.

    Both scanners used to treat any quote character as a terminator, so a
    translation containing the other quote kind could be truncated or shift
    brace tracking (greptile review).
    """
    sq = chr(39)
    dq = chr(34)
    src = chr(10).join([
        "const LOCALES = {",
        "  en: {",
        "    a: " + sq + "He said " + dq + "hi" + dq + " loudly" + sq + ",",
        "    b: {x: 1},",
        "    c: " + sq + "plain" + sq + ",",
        "  },",
        "  de: {",
        "    a: " + sq + "de" + sq + ",",
        "  },",
        "};",
    ])
    entries = _object_entries(src, "const LOCALES = {")
    assert list(entries) == ["en", "de"], "scanner lost track of locale entries"
    assert "b: {x: 1}" in entries["en"], "nested braces were miscounted"
    assert dq + "hi" + dq in entries["en"], "quoted text was truncated"

if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("i18n split contract checks passed")