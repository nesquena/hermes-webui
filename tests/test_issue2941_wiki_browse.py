"""Static-analysis tests for the LLM Wiki browser feature (issue #2941).

Verifies that:
1. /api/wiki/browse and /api/wiki/page route patterns exist in routes.py.
2. _renderLlmWikiStatus in panels.js references a browse action.
3. Path-traversal rejection (the ".." check) is present in the wiki page handler.
4. The four i18n keys are present in all 12 locale blocks.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_wiki_browse_route_exists_in_routes():
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    assert '"/api/wiki/browse"' in src, "GET /api/wiki/browse route not found in routes.py"


def test_wiki_page_route_exists_in_routes():
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    assert '"/api/wiki/page"' in src, "GET /api/wiki/page route not found in routes.py"


def test_wiki_page_path_traversal_rejection():
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    # The handler must reject paths containing ".." to prevent directory traversal.
    assert '".." in page_path' in src, "Path-traversal check (..) not found in wiki page handler"


def test_render_llm_wiki_status_references_browse():
    src = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    assert "_renderLlmWikiStatus" in src, "_renderLlmWikiStatus not found in panels.js"
    assert "_openWikiBrowser" in src, "_openWikiBrowser reference not found in panels.js"


def test_open_wiki_browser_function_exists():
    src = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    assert "async function _openWikiBrowser" in src, "_openWikiBrowser function not defined in panels.js"
    assert "/api/wiki/browse" in src, "/api/wiki/browse fetch not found in panels.js"
    assert "/api/wiki/page" in src, "/api/wiki/page fetch not found in panels.js"


def test_i18n_wiki_keys_in_all_locales():
    src = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    required_keys = [
        "wiki_browse",
        "wiki_search_placeholder",
        "wiki_no_pages",
        "wiki_not_configured",
    ]
    # Locate all locale block boundaries by finding "_lang:" occurrences,
    # then verify each key appears in every block.
    lang_positions = [m.start() for m in re.finditer(r"_lang:", src)]
    assert len(lang_positions) == 12, f"Expected 12 locale blocks, found {len(lang_positions)}"

    # Build per-locale slices: each block runs from just before "_lang:"
    # back to the previous locale start, forward to the next one.
    # Simpler: split on the top-level locale key pattern and check each chunk.
    # We split on the opening "  <key>: {" pattern used at the top level.
    chunks = re.split(r"\n  (?:'[^']+'|\w+): \{", src)
    # chunks[0] is the preamble before the first locale; chunks[1..12] are locale bodies.
    locale_chunks = chunks[1:]
    assert len(locale_chunks) == 12, f"Expected 12 locale chunks after split, got {len(locale_chunks)}"

    for i, chunk in enumerate(locale_chunks):
        for key in required_keys:
            assert key + ":" in chunk, (
                f"i18n key '{key}' missing from locale block {i + 1} "
                f"(position ~{lang_positions[i] if i < len(lang_positions) else '?'})"
            )
