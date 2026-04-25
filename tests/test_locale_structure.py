"""Regression test: no orphaned keys at the top level of the LOCALES object.

Issue #1008 — keys placed outside any locale block became top-level LOCALES
properties and appeared as spurious language options in the dropdown.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The six canonical locale codes that LOCALES must contain — and nothing else.
KNOWN_LOCALES = {"en", "ru", "es", "de", "zh", "zh-Hant"}


def _read_i18n() -> str:
    return (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_locales_only_known_codes():
    """Object.keys(LOCALES) must contain exactly the 6 known locale codes."""
    src = _read_i18n()

    # Extract the LOCALES block: from "const LOCALES = {" to its matching "};"
    start = src.index("const LOCALES = {")
    # Find the opening brace on that line
    brace_pos = src.index("{", start)
    depth = 0
    end = brace_pos
    for i in range(brace_pos, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    block = src[brace_pos:end]

    # Now parse top-level keys from the LOCALES object body (between outer { }).
    # Strip the outer braces to get the body.
    body = block.strip()
    assert body.startswith("{") and body.endswith("}")
    body = body[1:body.rfind("}")].strip()

    # Find all top-level keys. A top-level entry is either:
    #   key: {        (locale block)
    #   key: 'value', (orphaned key — this is what we're catching)
    #
    # We use a simple state machine: track brace depth within the body.
    found: set[str] = set()
    orphans: list[str] = []
    depth = 0
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        # Count braces to determine depth relative to LOCALES body
        depth += stripped.count("{") - stripped.count("}")

        if depth == 0 and not stripped.endswith(",") and not stripped.endswith("{"):
            # Standalone line at top level that isn't an opener — likely orphaned
            orphans.append(stripped)
            continue

        if depth == 1 and stripped.endswith("{"):
            # This should be a locale block opener
            m = re.match(r"^'([^']+)'|(\w+)", stripped)
            if m:
                key = m.group(1) or m.group(2)
                found.add(key)

    assert found == KNOWN_LOCALES, (
        f"LOCALES top-level keys differ. Expected {sorted(KNOWN_LOCALES)}, "
        f"got {sorted(found)}. Extra: {sorted(found - KNOWN_LOCALES)}, "
        f"Missing: {sorted(KNOWN_LOCALES - found)}"
    )

    assert not orphans, (
        f"Found orphaned key-value pairs at top level of LOCALES:\n"
        + "\n".join(f"  {o}" for o in orphans)
    )


def test_no_orphaned_keys_between_locale_blocks():
    """No bare key-value pairs should appear between locale blocks.

    A locale block is identified by "  code: {" at the first nesting level.
    Any key: 'value', line at that same level is an orphan.
    """
    src = _read_i18n()

    start = src.index("const LOCALES = {")
    brace_pos = src.index("{", start)
    depth = 0
    end = brace_pos
    for i in range(brace_pos, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    block = src[brace_pos:end]

    # Walk through each character tracking depth
    lines = block.splitlines()
    depth = 0
    orphans: list[str] = []
    for idx, line in enumerate(lines):
        # Track depth *before* this line's content is evaluated
        line_depth = depth
        depth += line.count("{") - line.count("}")

        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue

        # At depth 1 (first nesting inside LOCALES), every line should be
        # either a locale block opener ("  code: {") or a comment.
        # A bare "  some_key: 'value'," here is an orphan.
        if line_depth == 1 and ":" in stripped and not stripped.endswith("{"):
            orphans.append(f"  line {idx}: {stripped}")

    assert not orphans, (
        "Found orphaned key-value pairs at the top level of LOCALES:\n"
        + "\n".join(orphans)
        + "\nThese will appear as spurious language options in the dropdown."
    )
