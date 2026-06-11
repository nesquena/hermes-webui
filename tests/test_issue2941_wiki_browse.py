"""Static-analysis tests for the LLM Wiki browser feature (issue #2941).

Verifies that:
1. /api/wiki/browse and /api/wiki/page route patterns exist in routes.py.
2. _renderLlmWikiStatus in panels.js references a browse action.
3. Path-traversal rejection (the ".." check) is present in the wiki page handler.
4. The four i18n keys are present in every locale block.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8"))

    def get_json(self):
        return json.loads(self.body.decode("utf-8"))


def test_wiki_browse_route_exists_in_routes():
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    assert '"/api/wiki/browse"' in src, "GET /api/wiki/browse route not found in routes.py"


def test_wiki_page_route_exists_in_routes():
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    assert '"/api/wiki/page"' in src, "GET /api/wiki/page route not found in routes.py"


def test_wiki_page_path_traversal_rejection():
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    assert '".." in page_path' in src, "Path-traversal check (..) not found in wiki page handler"
    assert "_skill_path_within" in src.split("/api/wiki/page")[1].split("/api/")[0], (
        "Symlink-safe _skill_path_within guard not found in /api/wiki/page handler"
    )


def test_render_llm_wiki_status_references_browse():
    src = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    assert "_renderLlmWikiStatus" in src, "_renderLlmWikiStatus not found in panels.js"
    assert "_openWikiBrowser" in src, "_openWikiBrowser reference not found in panels.js"


def test_open_wiki_browser_function_exists():
    src = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    assert "async function _openWikiBrowser" in src, "_openWikiBrowser function not defined in panels.js"
    assert "/api/wiki/browse" in src, "/api/wiki/browse fetch not found in panels.js"
    assert "/api/wiki/page" in src, "/api/wiki/page fetch not found in panels.js"


def test_wiki_browse_skips_pages_that_disappear_during_listing(monkeypatch, tmp_path):
    from api import routes

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    ok = wiki_root / "ok.md"
    ok.write_text("# ok\n", encoding="utf-8")
    missing = wiki_root / "gone.md"

    monkeypatch.setattr(routes, "_llm_wiki_resolve_path", lambda: (wiki_root, None, None))
    monkeypatch.setattr(routes, "_llm_wiki_page_files", lambda root: [missing, ok])

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("http://example.com/api/wiki/browse"))

    assert handler.status == 200
    assert handler.get_json()["pages"] == [
        {
            "name": "ok.md",
            "path": "ok.md",
            "size": ok.stat().st_size,
            "mtime": int(ok.stat().st_mtime),
        }
    ]


def test_i18n_wiki_keys_in_all_locales():
    src = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    required_keys = [
        "wiki_browse",
        "wiki_search_placeholder",
        "wiki_no_pages",
        "wiki_not_configured",
    ]
    # Locate all locale block boundaries by finding "_lang:" occurrences,
    # then verify each required key appears in every locale block.
    lang_positions = [m.start() for m in re.finditer(r"_lang:", src)]
    assert lang_positions, "Could not find any locale blocks in i18n data"

    locale_chunks = []
    for idx, start in enumerate(lang_positions):
        end = lang_positions[idx + 1] if idx + 1 < len(lang_positions) else len(src)
        locale_chunks.append(src[start:end])

    for i, chunk in enumerate(locale_chunks):
        for key in required_keys:
            assert key + ":" in chunk, (
                f"i18n key '{key}' missing from locale block {i + 1} "
                f"(position ~{lang_positions[i]})"
            )
