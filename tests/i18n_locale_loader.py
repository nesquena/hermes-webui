"""Shared source loader for the eager locale manifest and split bundles."""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
CORE_PATH = REPO / "static" / "i18n-core.js"
LOCALE_DIR = REPO / "static" / "locales"


def locale_manifest() -> dict[str, dict[str, str]]:
    source = CORE_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"const\s+LOCALE_REGISTRY\s*=\s*Object\.freeze\((\{.*?\})\);",
        source,
        re.DOTALL,
    )
    assert match, "LOCALE_REGISTRY manifest not found in i18n-core.js"
    return json.loads(match.group(1))


def locale_sources() -> dict[str, str]:
    sources = {"en": CORE_PATH.read_text(encoding="utf-8")}
    for path in sorted(LOCALE_DIR.glob("*.js")):
        sources[path.stem] = path.read_text(encoding="utf-8")
    return sources


def locale_source_text() -> str:
    """Return the split source corpus for tests that inspect translation text."""
    core = locale_sources()["en"]
    runtime = re.sub(
        r"const\s+LOCALE_REGISTRY\s*=\s*Object\.freeze\(\{.*?\}\);\s*const\s+LOCALE_METADATA\s*=\s*LOCALE_REGISTRY;",
        "",
        core,
        count=1,
        flags=re.DOTALL,
    )
    helper_sources = []
    for code in locale_codes():
        source = locale_sources()[code]
        if code == "en":
            continue
        register = re.search(r"registerLocale\(", source)
        assert register, f"locale {code!r} is not registered"
        helper_sources.append(source[: register.start()])

    blocks = []
    for code in locale_codes():
        key = f"'{code}'" if "-" in code else code
        blocks.append(f"  {key}: {{\n{locale_block(code)}\n  }},")
    return runtime + "\n" + "\n".join(helper_sources) + "\nconst TEST_LOCALES = {\n" + "\n\n".join(blocks) + "\n};\n"


def locale_codes() -> list[str]:
    return list(locale_manifest())


def locale_block(locale: str) -> str:
    source = locale_sources()[locale]
    match = re.search(
        rf"registerLocale\(\s*['\"]{re.escape(locale)}['\"]\s*,\s*\{{",
        source,
    )
    assert match, f"locale {locale!r} is not registered"
    body_start = source.find("{", match.start()) + 1
    depth = 1
    i = body_start
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    while i < len(source) and depth:
        char = source[i]
        next_char = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            i += 1
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            i += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            i += 2
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[body_start:i]
        i += 1
    raise AssertionError(f"locale {locale!r} block never closed")


def locale_key_names(locale: str) -> set[str]:
    return set(re.findall(r"^\s{2,}([A-Za-z_$][A-Za-z0-9_$]*):", locale_block(locale), re.MULTILINE))


def locale_string_value(locale: str, key: str) -> str | None:
    block = locale_block(locale)
    for quote in ("'", '"'):
        pattern = rf"\b{re.escape(key)}:\s*{quote}((?:\\.|[^{quote}\\])*){quote}"
        match = re.search(pattern, block)
        if match:
            return match.group(1)
    return None
