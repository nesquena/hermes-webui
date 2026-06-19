"""Static-analysis tests for the LLM Wiki browser feature (issue #2941).

Verifies that:
1. /api/wiki/browse and /api/wiki/page route patterns exist in routes.py.
2. _renderLlmWikiStatus in panels.js references a browse action.
3. Path-traversal rejection (the ".." check) is present in the wiki page handler.
4. The four i18n keys are present in every locale block.
"""
from __future__ import annotations

import json
import os
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
    # Traversal is rejected by a real `..` path SEGMENT check (not the bare
    # substring, which would also reject a legit filename like `v1..v2.md`).
    assert 'part == ".."' in src, "Segment-based path-traversal check not found in wiki page handler"
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


def test_wiki_page_vanished_between_check_and_read_returns_404_not_500(monkeypatch, tmp_path):
    """TOCTOU: a page that disappears between is_file() and read_text() must
    return a clean 404, not let OSError bubble to the generic 500 handler."""
    from api import routes

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()

    monkeypatch.setattr(routes, "_llm_wiki_resolve_path", lambda: (wiki_root, None, None))

    # Path resolves inside the root and passes the containment guard, but the
    # read itself raises FileNotFoundError (simulating a vanished/racing file).
    class _GonePath(type(wiki_root)):
        def is_file(self):  # noqa: D401 - test stub
            return True

        def read_text(self, *a, **k):
            raise FileNotFoundError("vanished between list and read")

    real_path_join = routes.os.path.join

    monkeypatch.setattr(routes, "_skill_path_within", lambda root, p: True)

    orig_Path = routes.Path

    def _fake_Path(arg):
        # Only wrap the wiki page target; leave the root resolution alone.
        if isinstance(arg, str) and arg == real_path_join(str(wiki_root), "gone.md"):
            return _GonePath(arg)
        return orig_Path(arg)

    monkeypatch.setattr(routes, "Path", _fake_Path)

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("http://example.com/api/wiki/page?path=gone.md"))

    assert handler.status == 404, f"expected clean 404 on vanished page, got {handler.status}"


def test_wiki_page_read_is_restricted_to_listed_pages(monkeypatch, tmp_path):
    """The read endpoint must only serve files the browse/list path surfaces.
    A secret file inside the wiki root (e.g. .env) that is NOT a listed page
    must NOT be readable, even though it passes path-containment."""
    from api import routes

    wiki_root = tmp_path / "wiki"
    (wiki_root / "concepts").mkdir(parents=True)
    listed = wiki_root / "concepts" / "real.md"
    listed.write_text("# real page\n", encoding="utf-8")
    secret = wiki_root / ".env"
    secret.write_text("DONOTLEAK=secretmarker_abc\n", encoding="utf-8")

    monkeypatch.setattr(routes, "_llm_wiki_resolve_path", lambda: (wiki_root, None, None))

    # Attempt to read the secret (contained within the root, not a listed page).
    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("http://example.com/api/wiki/page?path=.env"))
    assert handler.status == 404, f"secret .env must not be readable, got {handler.status}"
    assert b"secretmarker_abc" not in handler.body, "secret content leaked through /api/wiki/page"

    # The genuinely-listed page IS readable.
    handler2 = _FakeHandler()
    routes.handle_get(handler2, urlparse("http://example.com/api/wiki/page?path=concepts/real.md"))
    assert handler2.status == 200, f"listed page should be readable, got {handler2.status}"
    assert "real page" in handler2.get_json()["content"]


def test_wiki_symlink_page_cannot_escape_section_root(monkeypatch, tmp_path):
    """A listed-looking *.md symlink whose target resolves OUTSIDE its section
    dir (e.g. concepts/leak.md -> ../.env) must not be listed by browse nor
    readable by /api/wiki/page — the resolved real path is the security check."""
    import os as _os
    from api import routes

    wiki_root = tmp_path / "wiki"
    (wiki_root / "concepts").mkdir(parents=True)
    (wiki_root / "concepts" / "real.md").write_text("# real\n", encoding="utf-8")
    secret = wiki_root / ".env"
    secret.write_text("DONOTLEAK=leak_marker_xyz\n", encoding="utf-8")

    leak = wiki_root / "concepts" / "leak.md"
    try:
        leak.symlink_to(_os.path.join("..", ".env"))
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("symlinks not supported on this platform")

    monkeypatch.setattr(routes, "_llm_wiki_resolve_path", lambda: (wiki_root, None, None))

    # browse must NOT list the escaping symlink
    h_browse = _FakeHandler()
    routes.handle_get(h_browse, urlparse("http://example.com/api/wiki/browse"))
    listed_names = [p["path"] for p in h_browse.get_json()["pages"]]
    assert "concepts/leak.md" not in listed_names, "escaping symlink must not be listed"

    # page read of the symlink must 404 and must not leak the secret
    h_page = _FakeHandler()
    routes.handle_get(h_page, urlparse("http://example.com/api/wiki/page?path=concepts/leak.md"))
    assert h_page.status == 404, f"symlink-escape read must 404, got {h_page.status}"
    assert b"leak_marker" not in h_page.body and b"SECRET" not in h_page.body, "secret leaked via symlink page"


def test_wiki_symlink_to_hidden_same_section_target_blocked(monkeypatch, tmp_path):
    """A *.md symlink whose target is a HIDDEN file in the same section
    (concepts/link.md -> .hidden/secret.md) must not be listed or readable —
    the dot-segment rule applies to the RESOLVED target, not just the link name."""
    import os as _os
    from api import routes

    wiki_root = tmp_path / "wiki"
    hidden_dir = wiki_root / "concepts" / ".hidden"
    hidden_dir.mkdir(parents=True)
    (hidden_dir / "secret.md").write_text("DONOTLEAK=hidden_marker_q\n", encoding="utf-8")
    (wiki_root / "concepts" / "real.md").write_text("# real\n", encoding="utf-8")
    link = wiki_root / "concepts" / "link.md"
    try:
        link.symlink_to(_os.path.join(".hidden", "secret.md"))
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("symlinks not supported on this platform")

    monkeypatch.setattr(routes, "_llm_wiki_resolve_path", lambda: (wiki_root, None, None))

    h_browse = _FakeHandler()
    routes.handle_get(h_browse, urlparse("http://example.com/api/wiki/browse"))
    listed = [p["path"] for p in h_browse.get_json()["pages"]]
    assert "concepts/link.md" not in listed, "symlink to hidden target must not be listed"

    h_page = _FakeHandler()
    routes.handle_get(h_page, urlparse("http://example.com/api/wiki/page?path=concepts/link.md"))
    assert h_page.status == 404, f"symlink to hidden target must 404, got {h_page.status}"
    assert b"hidden_marker_q" not in h_page.body, "hidden secret leaked via symlink"


def test_wiki_page_cached_nested_entry_rechecks_resolved_containment(monkeypatch, tmp_path):
    import os as _os
    from api import routes

    wiki_root = tmp_path / "wiki"
    section = wiki_root / "concepts"
    nested = section / "sub"
    nested.mkdir(parents=True)
    page = nested / "real.md"
    page.write_text("# real\n", encoding="utf-8")
    secret = wiki_root / ".env"
    secret.write_text("DONOTLEAK=stale_cache_marker\n", encoding="utf-8")

    try:
        page.unlink()
        page.symlink_to(_os.path.join("..", "..", ".env"))
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("symlinks not supported on this platform")

    routes._llm_wiki_clear_page_files_cache()
    monkeypatch.setattr(routes, "_WIKI_ALLOWLIST_TTL", 60.0)
    monkeypatch.setattr(routes, "_llm_wiki_resolve_path", lambda: (wiki_root, None, None))

    # Prime the cached name list while the nested page is still valid, then
    # swap only the nested entry so the top-level section signature stays stale.
    page.unlink()
    page.write_text("# real\n", encoding="utf-8")
    assert routes._llm_wiki_page_files(wiki_root) == [page]
    sig_before = routes._llm_wiki_page_files_cache_signature(wiki_root.resolve())

    page.unlink()
    page.symlink_to(_os.path.join("..", "..", ".env"))
    sig_after = routes._llm_wiki_page_files_cache_signature(wiki_root.resolve())
    assert sig_after == sig_before

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("http://example.com/api/wiki/page?path=concepts/sub/real.md"))

    assert handler.status == 404, f"stale cached nested symlink swap must 404, got {handler.status}"
    assert b"stale_cache_marker" not in handler.body, "stale cached nested symlink leaked hidden content"


def test_wiki_page_cached_entry_cannot_jump_sections(monkeypatch, tmp_path):
    import os as _os
    from api import routes

    wiki_root = tmp_path / "wiki"
    page = wiki_root / "concepts" / "sub" / "real.md"
    page.parent.mkdir(parents=True)
    page.write_text("# real\n", encoding="utf-8")
    external_doc = wiki_root / "drafts" / "page.md"
    external_doc.parent.mkdir(parents=True)
    external_doc.write_text("cross_section_marker", encoding="utf-8")

    try:
        page.unlink()
        page.symlink_to(_os.path.join("..", "..", "drafts", "page.md"))
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("symlinks not supported on this platform")

    routes._llm_wiki_clear_page_files_cache()
    monkeypatch.setattr(routes, "_WIKI_ALLOWLIST_TTL", 60.0)
    monkeypatch.setattr(routes, "_llm_wiki_resolve_path", lambda: (wiki_root, None, None))

    page.unlink()
    page.write_text("# real\n", encoding="utf-8")
    assert routes._llm_wiki_page_files(wiki_root) == [page]

    page.unlink()
    page.symlink_to(_os.path.join("..", "..", "drafts", "page.md"))

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("http://example.com/api/wiki/page?path=concepts/sub/real.md"))

    assert handler.status == 404, f"stale cached cross-section swap must 404, got {handler.status}"
    assert b"cross_section_marker" not in handler.body, "stale cached page leaked another section's content"


def test_wiki_symlinked_section_cannot_expose_outside_tree(monkeypatch, tmp_path):
    """A symlinked SECTION dir (concepts -> /tmp/outside) must not expose files
    outside the real wiki root."""
    from api import routes

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.md").write_text("DONOTLEAK=outside_marker_z\n", encoding="utf-8")
    try:
        (wiki_root / "concepts").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("symlinks not supported on this platform")

    monkeypatch.setattr(routes, "_llm_wiki_resolve_path", lambda: (wiki_root, None, None))

    h_browse = _FakeHandler()
    routes.handle_get(h_browse, urlparse("http://example.com/api/wiki/browse"))
    listed = [p["path"] for p in h_browse.get_json()["pages"]]
    assert listed == [], f"symlinked section must expose nothing, listed {listed}"


def test_wiki_legit_filename_with_dotdot_substring_opens(monkeypatch, tmp_path):
    """A legit listed page whose filename merely CONTAINS '..' (e.g. v1..v2.md)
    must still open — traversal rejection is per-segment, not substring."""
    from api import routes

    wiki_root = tmp_path / "wiki"
    (wiki_root / "concepts").mkdir(parents=True)
    (wiki_root / "concepts" / "v1..v2.md").write_text("# diff notes\n", encoding="utf-8")

    monkeypatch.setattr(routes, "_llm_wiki_resolve_path", lambda: (wiki_root, None, None))

    h = _FakeHandler()
    routes.handle_get(h, urlparse("http://example.com/api/wiki/page?path=concepts/v1..v2.md"))
    assert h.status == 200, f"legit filename with '..' substring should open, got {h.status}"
    assert "diff notes" in h.get_json()["content"]


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


def test_wiki_page_files_documents_hardlink_trust_boundary():
    from api import routes

    doc = routes._llm_wiki_page_files.__doc__ or ""
    assert "hardlink" in doc.lower()
    assert "trusted" in doc.lower() or "operator-controlled" in doc.lower()


def test_wiki_page_files_reuses_cache_with_unchanged_section_mtime(monkeypatch, tmp_path):
    from api import routes

    wiki_root = tmp_path / "wiki"
    (wiki_root / "concepts").mkdir(parents=True)
    page = wiki_root / "concepts" / "one.md"
    page.write_text("# one\n", encoding="utf-8")

    routes._llm_wiki_clear_page_files_cache()
    monkeypatch.setattr(routes, "_WIKI_ALLOWLIST_TTL", 60.0)

    calls = []

    def fake_uncached(root):
        calls.append(root)
        return [page]

    monkeypatch.setattr(routes, "_llm_wiki_page_files_uncached", fake_uncached)

    assert routes._llm_wiki_page_files(wiki_root) == [page]
    assert routes._llm_wiki_page_files(wiki_root) == [page]
    assert len(calls) == 1


def test_wiki_page_files_cache_invalidates_when_section_mtime_changes(monkeypatch, tmp_path):
    import time as _time
    from api import routes

    wiki_root = tmp_path / "wiki"
    section = wiki_root / "concepts"
    section.mkdir(parents=True)
    first = section / "one.md"
    second = section / "two.md"
    first.write_text("# one\n", encoding="utf-8")

    routes._llm_wiki_clear_page_files_cache()
    monkeypatch.setattr(routes, "_WIKI_ALLOWLIST_TTL", 60.0)

    calls = []

    def fake_uncached(root):
        calls.append(root)
        return [first] if len(calls) == 1 else [first, second]

    monkeypatch.setattr(routes, "_llm_wiki_page_files_uncached", fake_uncached)

    assert routes._llm_wiki_page_files(wiki_root) == [first]
    second.write_text("# two\n", encoding="utf-8")
    _time.sleep(0.02)  # Ensure mtime granularity is sufficient on Windows
    os.utime(str(section), None)
    assert routes._llm_wiki_page_files(wiki_root) == [first, second]
    assert len(calls) == 2


def test_wiki_page_files_cache_expires_after_ttl(monkeypatch, tmp_path):
    from api import routes

    wiki_root = tmp_path / "wiki"
    (wiki_root / "concepts").mkdir(parents=True)
    page = wiki_root / "concepts" / "one.md"
    page.write_text("# one\n", encoding="utf-8")

    routes._llm_wiki_clear_page_files_cache()
    monkeypatch.setattr(routes, "_WIKI_ALLOWLIST_TTL", 1.0)

    calls = []
    ticks = iter([100.0, 100.5, 101.5])
    monkeypatch.setattr(routes.time, "monotonic", lambda: next(ticks))

    def fake_uncached(root):
        calls.append(root)
        return [page]

    monkeypatch.setattr(routes, "_llm_wiki_page_files_uncached", fake_uncached)

    routes._llm_wiki_page_files(wiki_root)   # miss → rebuild (t=100.0)
    routes._llm_wiki_page_files(wiki_root)   # hit (t=100.5 < 101.0)
    routes._llm_wiki_page_files(wiki_root)   # miss → rebuild (t=101.5 > 101.0)
    assert len(calls) == 2
