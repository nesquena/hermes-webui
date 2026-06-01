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
    assert status["last_writer"] is None
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


def test_llm_wiki_status_can_fall_back_to_joplin_metadata(monkeypatch, tmp_path):
    """When no filesystem wiki is configured, Joplin can be the knowledge source."""
    import api.routes as routes

    missing = tmp_path / "wiki"
    monkeypatch.delenv("WIKI_PATH", raising=False)
    monkeypatch.setattr(routes, "_llm_wiki_resolve_path", lambda: (missing, "default", False))
    monkeypatch.setattr(routes, "_llm_wiki_joplin_root_title", lambda: "Hermes 🤖")
    monkeypatch.setattr(routes, "_llm_wiki_notes_fallback_enabled", lambda: True)

    def fake_joplin_get(path, params=None):
        if path == "/folders":
            return {"items": [
                {"id": "root", "title": "Hermes 🤖", "parent_id": "", "updated_time": 1000},
                {"id": "knowledge", "title": "10 Knowledge", "parent_id": "root", "updated_time": 2000},
                {"id": "raw", "title": "Raw Captures", "parent_id": "knowledge", "updated_time": 3000},
            ]}
        if path == "/folders/root/notes":
            return {"items": [{"id": "n1", "title": "Index", "updated_time": 4000}]}
        if path == "/folders/knowledge/notes":
            return {"items": [{"id": "n2", "title": "Agent Memory", "updated_time": 5000}]}
        if path == "/folders/raw/notes":
            return {"items": [{"id": "n3", "title": "Capture", "updated_time": 6000}]}
        raise AssertionError(path)

    monkeypatch.setattr(routes, "_joplin_api_get", fake_joplin_get)

    status = routes._build_llm_wiki_status()

    assert status["available"] is True
    assert status["enabled"] is True
    assert status["status"] == "ready"
    assert status["path_source"] == "notes"
    assert status["source_kind"] == "joplin"
    assert status["source_label"] == "Joplin"
    assert status["source_count_available"] is True
    assert status["entry_count"] == 3
    assert status["page_count"] == 3
    assert status["raw_source_count"] == 1
    assert status["last_writer"] == "Joplin"
    serialized = repr(status)
    assert "Agent Memory" not in serialized
    assert "Capture" not in serialized


def test_llm_wiki_status_reports_generic_configured_notes_source(monkeypatch, tmp_path):
    """Non-Joplin note providers can still surface as configured knowledge sources."""
    import api.routes as routes

    missing = tmp_path / "wiki"
    monkeypatch.delenv("WIKI_PATH", raising=False)
    monkeypatch.setattr(routes, "_llm_wiki_resolve_path", lambda: (missing, "default", False))
    monkeypatch.setattr(routes, "_llm_wiki_notes_fallback_enabled", lambda: True)
    monkeypatch.setattr(routes, "_llm_wiki_joplin_status", lambda base: None)
    monkeypatch.setattr(routes, "_llm_wiki_preferred_notes_source", lambda: "obsidian")
    monkeypatch.setattr(routes, "_looks_like_notes_source", lambda name, tools: name == "obsidian")
    monkeypatch.setattr(routes, "_note_source_label", lambda name: "Obsidian")
    monkeypatch.setattr(routes, "get_config", lambda: {
        "mcp_servers": {
            "filesystem": {"enabled": True},
            "obsidian": {"enabled": True},
        }
    })

    status = routes._build_llm_wiki_status()

    assert status["available"] is True
    assert status["enabled"] is True
    assert status["status"] == "configured"
    assert status["path_source"] == "notes"
    assert status["source_kind"] == "obsidian"
    assert status["source_label"] == "Obsidian"
    assert status["source_count_available"] is False
    assert status["entry_count"] == 0
    assert status["last_writer"] == "Obsidian"


def test_insights_panel_fetches_and_renders_llm_wiki_status_card():
    panels_src = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    index_src = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    style_src = (REPO / "static" / "style.css").read_text(encoding="utf-8")

    assert "api('/api/wiki/status')" in panels_src
    assert "function _renderLlmWikiStatus" in panels_src
    assert "llmWikiStatusCard" in index_src
    assert "wiki-status-card" in style_src
    assert "raw/" in panels_src
    assert "third-party notes knowledge source" in panels_src
    assert "detailed counts depend on provider-specific support" in panels_src
    assert "Raw notes" in panels_src
    assert "recent_entries" not in panels_src
