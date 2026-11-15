"""Read the i18n catalog from its split files.

Issue #6652 split static/i18n.js into an eager core (English + registry +
ensureLocale()) plus one bundle per language under static/locales/<code>.js.
Roughly 190 test modules read static/i18n.js directly and regex/BraceWalk the
"const LOCALES = {...}" literal out of it -- extract_locale_block(),
_i18n_top_level_locale_keys(), and their many copies.

Those tests assert real invariants (key coverage, identifier triples, login
parity). Their parsing strategy is what the split invalidated, not their intent.
So this module reconstructs the pre-split single-object text from the split
files, and those parsers keep working unmodified.

Three accessors:

* catalog_source() -- the pre-split i18n.js TEXT SHAPE: header, one
  "  <code>: { ... }," block per locale in the original order, closing "};",
  then the _i18n* helper declarations and the core (t(), setLocale(), ...).
  Use this when a test greps or brace-walks for catalog entries.
* executable_source() -- runnable JavaScript that evaluates in a bare JS context
  and still defines loadLocale()/setLocale()/resolvePreferredLocale(). Use this
  when a test actually EXECUTES i18n.js in node or a vm context.
* locale_block(code) -- one locale's inner body, without surrounding braces.

Nothing here reads the network or starts a server; it is pure filesystem work.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "static"
CORE_PATH = STATIC / "i18n.js"
LOCALES_DIR = STATIC / "locales"

_SQ = chr(39)
_DQQ = chr(34)
_BT = chr(96)
_QUOTES = (_SQ, _DQQ, _BT)

# Matches `key: {` where key is single-quoted, double-quoted, or a bare identifier.
_BS = chr(92)
_PAT = (
    _BS + 's*(?:'
    + chr(39) + '([^' + chr(39) + ']+)' + chr(39)
    + '|' + chr(34) + '([^' + chr(34) + ']+)' + chr(34)
    + '|([A-Za-z0-9_$]+))'
    + _BS + 's*:' + _BS + 's*' + _BS + '{'
)
_ENTRY_RE = re.compile(_PAT)



def _scan(src, start, stop_on_depth_zero=True):
    """Yield (key, body_start, body_end) for each top-level entry of an object.

    Token-aware: line/block comments and all three JS string forms are skipped,
    and inside a string only the quote that OPENED it closes it. A double quote
    inside a single-quoted translation therefore cannot be mistaken for code
    (greptile review: previously any quote char ended the string).
    """
    i = start
    depth = 0
    mode = ""
    quote = ""
    esc = False
    pending = None
    body_start = -1
    n = len(src)

    while i < n:
        ch = src[i]

        if mode == "line":
            if ch == "\n":
                mode = ""
            i += 1
            continue
        if mode == "block":
            if ch == "*" and src[i + 1 : i + 2] == "/":
                mode = ""
                i += 2
                continue
            i += 1
            continue
        if mode == "str":
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                mode = ""
                quote = ""
            i += 1
            continue

        if ch == "/" and src[i + 1 : i + 2] == "/":
            mode = "line"
            i += 2
            continue
        if ch == "/" and src[i + 1 : i + 2] == "*":
            mode = "block"
            i += 2
            continue
        if ch in (_SQ, _DQQ, _BT):
            mode = "str"
            quote = ch
            i += 1
            continue

        # Key detection only at object level; consumes the whole "key: {" prefix.
        if depth == 1 and pending is None:
            m = _ENTRY_RE.match(src, i)
            if m:
                pending = m.group(1) or m.group(2) or m.group(3)
                i = m.end() - 1
                continue

        if ch == "{":
            depth += 1
            if depth == 2 and pending is not None:
                body_start = i
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 1 and pending is not None:
                yield pending, body_start, i
                pending = None
            elif depth == 0:
                return
            i += 1
            continue

        i += 1


def _object_entries(src, anchor):
    """Map key -> inner body for the object literal following anchor."""
    start = src.find(anchor)
    assert start >= 0, "%r not found in source" % (anchor,)
    open_brace = src.find("{", start)
    assert open_brace >= 0, "%r has no object literal" % (anchor,)
    return {key: src[s + 1 : e] for key, s, e in _scan(src, open_brace)}


def _locale_codes(core_src):
    """Ordered locale codes from the eager LOCALE_META manifest."""
    entries = _object_entries(core_src, "const LOCALE_META = {")
    assert entries, "LOCALE_META listed no locales"
    return list(entries)


def _bundle_body(code):
    """Return the object body registered by static/locales/<code>.js."""
    path = LOCALES_DIR / (code + ".js")
    assert path.is_file(), "locale bundle missing: %s" % path
    src = path.read_text(encoding="utf-8")
    anchor = src.find("registerLocale(")
    assert anchor >= 0, "%s does not call registerLocale()" % path
    comma = src.find(",", anchor)
    assert comma >= 0, "%s registerLocale() has no bundle argument" % path
    open_brace = src.find("{", comma)
    assert open_brace >= 0, "%s registerLocale() has no bundle object" % path
    depth = 0
    i = open_brace
    mode = ""
    quote = ""
    esc = False
    while i < len(src):
        ch = src[i]
        if mode == "line":
            if ch == "\n":
                mode = ""
            i += 1
            continue
        if mode == "block":
            if ch == "*" and src[i + 1 : i + 2] == "/":
                mode = ""
                i += 2
                continue
            i += 1
            continue
        if mode == "str":
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                mode = ""
                quote = ""
            i += 1
            continue
        if ch == "/" and src[i + 1 : i + 2] == "/":
            mode = "line"
            i += 2
            continue
        if ch == "/" and src[i + 1 : i + 2] == "*":
            mode = "block"
            i += 2
            continue
        if ch in (_SQ, _DQQ, _BT):
            mode = "str"
            quote = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return src[open_brace + 1 : i]
            i += 1
            continue
        i += 1
    raise AssertionError("%s bundle braces are not balanced" % path)


def _header(core_src):
    return core_src[: core_src.find("// \u2500\u2500 Registry")]


def _declarations_and_core(core_src):
    """Helper declarations plus the core, with the registry manifest.

    Everything from LOCALE_META onward is kept. The English bundle references
    the _i18n* helpers (processed_elapsed: _i18nProcessedElapsedEn) and
    resolveLocale() references LOCALE_META; dropping either makes the
    reconstruction throw ReferenceError as soon as anything evaluates it.
    """
    idx = core_src.find('const LOCALE_META = {')
    assert idx >= 0, 'LOCALE_META not found in static/i18n.js'
    return core_src[idx:]


@lru_cache(maxsize=1)
def catalog_source():
    """Pre-split i18n.js text shape (what the older parsers expect)."""
    core_src = CORE_PATH.read_text(encoding="utf-8")
    locales = _object_entries(core_src, "const LOCALES = {")
    assert "en" in locales, "eager English bundle missing from static/i18n.js"

    parts = [_header(core_src), "const LOCALES = {\n"]
    for code in _locale_codes(core_src):
        body = locales["en"] if code == "en" else _bundle_body(code)
        quoted = code if re.fullmatch(r"[A-Za-z0-9_]+", code) else "'%s'" % code
        # Pre-split layout separated consecutive blocks with one blank line;
        # several tests anchor on "\n  },\n\n  <next>: {" exactly.
        parts.append("  %s: {%s},\n\n" % (quoted, body))
    parts.append("};\n\n")
    parts.append(_declarations_and_core(core_src))
    return "".join(parts)


@lru_cache(maxsize=1)
def executable_source():
    """Runnable i18n JavaScript assembled from the split files.

    A test that EXECUTES i18n.js needs the real core plus registered bundles,
    not the text-shape reconstruction: the reconstruction has no LOCALE_META, so
    resolveLocale() would throw, and it declares the registry functions it never
    defines.
    """
    parts = [CORE_PATH.read_text(encoding="utf-8")]
    for code in _locale_codes(CORE_PATH.read_text(encoding="utf-8")):
        if code == "en":
            continue
        parts.append((LOCALES_DIR / (code + ".js")).read_text(encoding="utf-8"))
    return "\n".join(parts)


@lru_cache(maxsize=32)
def locale_block(code):
    """Inner body of one locale's object, without the surrounding braces."""
    core_src = CORE_PATH.read_text(encoding="utf-8")
    if code == "en":
        return _object_entries(core_src, "const LOCALES = {")["en"]
    return _bundle_body(code)


@lru_cache(maxsize=1)
def locale_codes():
    """Every shipped locale code, in manifest order."""
    return tuple(_locale_codes(CORE_PATH.read_text(encoding="utf-8")))
