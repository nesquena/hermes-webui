from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]


def _write(path: Path, text: str = "# Synthetic\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_llm_wiki_status_reads_synthetic_fixture_without_exposing_content(tmp_path, monkeypatch):
    """The wiki status API should summarize counts/mtime without leaking page text."""
    import api.routes as routes

    wiki = tmp_path / "wiki"
    _write(wiki / "SCHEMA.md", "# Schema\n")
    _write(wiki / "index.md", "# Index\n")
    _write(wiki / "log.md", "# Log\n## [2026-05-04] update | Secret project name\n- Details stay private\n")
    _write(
        wiki / "entities" / "private-agent.md",
        "---\ntitle: Private Agent\nupdated: 2026-05-04\n---\nSensitive body text must not ship.\n",
    )
    _write(wiki / "concepts" / "safe-summary.md", "---\ntitle: Safe Summary\n---\nMore private text\n")
    _write(wiki / "raw" / "articles" / "source.md", "Raw source body should not count as wiki page\n")

    monkeypatch.setenv("WIKI_PATH", str(wiki))

    status = routes._build_llm_wiki_status()

    assert status["available"] is True
    assert status["enabled"] is True
    assert status["entry_count"] == 2
    assert status["page_count"] == 2
    assert status["raw_source_count"] == 1
    assert status["last_updated"] is not None
    # log.md in the fixture has a "## [2026-05-04] update | ..." heading,
    # so the new last-writer reader must surface that action verb.
    assert status["last_writer"] == "ai-agent (update)"
    assert status["toggle_available"] is False
    assert status["docs_url"].endswith("/research-llm-wiki")
    serialized = repr(status)
    assert "Sensitive body text" not in serialized
    assert "Secret project name" not in serialized
    assert str(wiki) not in serialized


def test_llm_wiki_status_reports_unavailable_when_path_missing(tmp_path, monkeypatch):
    import api.routes as routes

    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("WIKI_PATH", str(missing))

    status = routes._build_llm_wiki_status()

    assert status["available"] is False
    assert status["enabled"] is False
    assert status["entry_count"] == 0
    assert status["page_count"] == 0
    assert status["raw_source_count"] == 0
    assert status["last_updated"] is None
    assert status["status"] == "missing"


def test_api_wiki_status_route_is_registered(monkeypatch, tmp_path):
    import api.routes as routes

    wiki = tmp_path / "wiki"
    _write(wiki / "entities" / "one.md")
    monkeypatch.setenv("WIKI_PATH", str(wiki))

    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.j", side_effect=fake_j):
        handled = routes.handle_get(SimpleNamespace(), urlparse("/api/wiki/status"))

    assert handled is True
    assert captured["status"] == 200
    assert captured["payload"]["entry_count"] == 1


def test_insights_panel_fetches_and_renders_llm_wiki_status_card():
    panels_src = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    index_src = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    style_src = (REPO / "static" / "style.css").read_text(encoding="utf-8")

    assert "api('/api/wiki/status')" in panels_src
    assert "function _renderLlmWikiStatus" in panels_src
    assert "llmWikiStatusCard" in index_src
    assert "wiki-status-card" in style_src
    assert "raw/" in panels_src
    assert "recent_entries" not in panels_src


def test_last_writer_reads_frontmatter(tmp_path):
    """#3455 part 2: the Last writer field reads page frontmatter updated_by/writer/author."""
    import api.routes as routes

    wiki = tmp_path / "wiki"
    _write(wiki / "entities" / "a.md", "---\ntitle: A\nupdated_by: alice\n---\nbody\n")
    pages = routes._llm_wiki_page_files(wiki)
    assert routes._llm_wiki_last_writer(wiki, pages) == "alice"


def test_last_writer_rejects_symlink_outside_wiki(tmp_path):
    """#3455 review (Codex): a symlinked .md page resolving OUTSIDE the wiki must
    not be read — its frontmatter must never leak into the status card."""
    import api.routes as routes

    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    secret = outside / "private.md"
    secret.write_text("---\nauthor: outside-secret\n---\nprivate body\n", encoding="utf-8")

    wiki = tmp_path / "wiki" / "entities"
    wiki.mkdir(parents=True, exist_ok=True)
    link = wiki / "linked.md"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("symlinks not supported on this platform")

    wiki_root = tmp_path / "wiki"
    pages = routes._llm_wiki_page_files(wiki_root)
    writer = routes._llm_wiki_last_writer(wiki_root, pages)
    # The external symlink's frontmatter must NOT surface; falls back to ai-agent.
    assert writer != "outside-secret"
    assert "outside-secret" not in writer
