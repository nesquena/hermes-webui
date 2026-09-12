"""Shared helper for reading i18n translation sources (issue #6652 split).

Translations moved from the monolithic static/i18n.js into per-language files
under static/locales/ (issue #6652). static/i18n.js now only contains the
runtime (LOCALES registry, t(), setLocale(), ...).

read_i18n_bundles() re-assembles the original single-file layout — the locale
blocks in their original declaration order, wrapped back into a `const LOCALES
= { ... }` object, followed by the i18n runtime — so tests that assert on
translation keys, locale blocks, block order, or key counts keep working.
"""
from __future__ import annotations

import re
from pathlib import Path

# Original declaration order of the locale blocks in static/i18n.js.
_LOCALE_ORDER = [
    "en", "it", "ja", "ru", "es", "de", "zh", "zh-Hant",
    "pt", "ko", "fr", "cs", "tr", "pl", "vi",
]


def _extract_block(text: str, lang: str) -> str:
    """Return the body of the `lang: { ... }` block from a locale bundle file."""
    key_re = re.compile(rf"^\s{{2}}'?{re.escape(lang)}'?\s*:\s*\{{", re.M)
    m = key_re.search(text)
    if not m:
        raise AssertionError(f"locale block {lang!r} not found in bundle file")
    start = m.end() - 1  # position of "{"
    depth = 0
    in_single = in_double = in_backtick = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if in_single:
            if ch == "\\":
                escape = True
            elif ch == "'":
                in_single = False
            continue
        if in_double:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            continue
        if in_backtick:
            if ch == "\\":
                escape = True
            elif ch == "`":
                in_backtick = False
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "`":
            in_backtick = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body = text[start + 1 : i].strip("\n")
                if body.endswith(","):
                    body = body[:-1].rstrip()
                return body
    raise AssertionError(f"locale block {lang!r} not closed in bundle file")


def _key_literal(lang: str) -> str:
    """Preserve quoted keys (e.g. 'zh-Hant') exactly as in the original file."""
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", lang):
        return lang
    return "'" + lang + "'"


def read_i18n_bundles(root: Path) -> str:
    """Return locale bundles re-assembled in the original layout, plus runtime."""
    locales_dir = root / "static" / "locales"
    blocks = []
    for lang in _LOCALE_ORDER:
        text = (locales_dir / f"{lang}.js").read_text(encoding="utf-8")
        body = _extract_block(text, lang)
        header = ""
        if lang == "zh-Hant":
            # Preserve the original inline comment before the zh-Hant block.
            header = "  // Traditional Chinese (zh-Hant)\n"
        blocks.append(f"{header}  {_key_literal(lang)}: {{\n{body}\n  }},")
    bundles = "\n\n".join(blocks)
    runtime = (root / "static" / "i18n.js").read_text(encoding="utf-8")
    return f"const LOCALES = {{\n{bundles}\n}};\n\n{runtime}"
