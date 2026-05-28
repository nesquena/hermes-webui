"""Regression tests for WebUI notes source discovery."""
from __future__ import annotations


def test_notes_sources_identifies_note_or_knowledge_mcp_servers():
    from api.routes import _notes_sources_from_mcp_inventory

    servers = {
        "joplin": {"name": "joplin", "enabled": True, "active": True, "status": "healthy"},
        "filesystem": {"name": "filesystem", "enabled": True, "active": True, "status": "healthy"},
        "llm-wiki": {"name": "llm-wiki", "enabled": True, "active": False, "status": "configured"},
    }
    tools = [
        {"server": "joplin", "name": "search_notes", "description": "Search notes by keyword"},
        {"server": "joplin", "name": "get_note", "description": "Get full note content"},
        {"server": "filesystem", "name": "read_text_file", "description": "Read files"},
        {"server": "llm-wiki", "name": "query_knowledge_base", "description": "Search wiki knowledge"},
    ]

    sources = _notes_sources_from_mcp_inventory(servers, tools)

    assert [source["name"] for source in sources] == ["joplin", "llm-wiki"]
    assert sources[0]["label"] == "Joplin"
    assert sources[0]["tool_count"] == 2
    assert sources[0]["active"] is True
    assert sources[1]["active"] is False


def test_notes_sources_redacts_tool_descriptions_and_omits_plain_file_tools():
    from api.routes import _notes_sources_from_mcp_inventory

    servers = {"notion": {"name": "notion", "enabled": True, "active": True, "status": "healthy"}}
    tools = [
        {"server": "notion", "name": "search_pages", "description": "Search notes api_key=redaction-test-placeholder"},
    ]

    [source] = _notes_sources_from_mcp_inventory(servers, tools)

    assert source["name"] == "notion"
    assert "token" not in source["tools"][0]["description"].lower()
    assert "[REDACTED]" in source["tools"][0]["description"]


def test_notes_sources_shows_configured_third_party_note_servers_without_tool_inventory():
    from api.routes import _notes_sources_from_mcp_inventory

    servers = {
        "joplin": {"name": "joplin", "enabled": True, "active": False, "status": "configured"},
        "obsidian": {"name": "obsidian", "enabled": True, "active": False, "status": "configured"},
        "notion": {"name": "notion", "enabled": True, "active": False, "status": "configured"},
        "llm-wiki": {"name": "llm-wiki", "enabled": True, "active": False, "status": "configured"},
        "filesystem": {"name": "filesystem", "enabled": True, "active": True, "status": "healthy"},
    }

    sources = _notes_sources_from_mcp_inventory(servers, [])

    assert [source["name"] for source in sources] == ["joplin", "llm-wiki", "notion", "obsidian"]
    by_name = {source["name"]: source for source in sources}
    assert by_name["joplin"]["label"] == "Joplin"
    assert [tool["name"] for tool in by_name["joplin"]["tools"]] == ["search_notes", "list_notes", "get_note"]
    assert [tool["name"] for tool in by_name["obsidian"]["tools"]] == ["search_notes", "read_note"]
    assert [tool["name"] for tool in by_name["notion"]["tools"]] == ["search_pages", "get_page"]
    assert [tool["name"] for tool in by_name["llm-wiki"]["tools"]] == ["query_knowledge_base", "read_page"]
    assert all(source["tool_source"] == "configured_hint" for source in sources)
    assert all(tool.get("inferred") is True for source in sources for tool in source["tools"])
    assert all(source["status"] == "configured" for source in sources)


def test_external_notes_sources_drawer_is_default_off(monkeypatch):
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_EXTERNAL_NOTES_SOURCES", raising=False)

    assert routes._external_notes_sources_enabled({}) is False
    assert routes._external_notes_sources_enabled({"webui_external_notes_sources": False}) is False


def test_external_notes_sources_drawer_can_be_enabled_by_config_or_env(monkeypatch):
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_EXTERNAL_NOTES_SOURCES", raising=False)
    assert routes._external_notes_sources_enabled({"webui_external_notes_sources": True}) is True
    assert routes._external_notes_sources_enabled({"external_notes_sources": "yes"}) is True

    monkeypatch.setenv("HERMES_WEBUI_EXTERNAL_NOTES_SOURCES", "1")
    assert routes._external_notes_sources_enabled({}) is True


def test_joplin_search_notes_returns_safe_snippets(monkeypatch):
    from api import routes

    def fake_get(path, params=None):
        assert path == "/search"
        assert params["type"] == "note"
        return {"items": [{
            "id": "abc123def4567890",
            "title": "Hermes Context",
            "body": "This is a long Hermes context note with useful details.",
            "parent_id": "folder123",
            "updated_time": 123,
        }]}

    monkeypatch.setattr(routes, "_joplin_api_get", fake_get)

    results = routes._joplin_search_notes("Hermes")

    assert results == [{
        "id": "abc123def4567890",
        "title": "Hermes Context",
        "snippet": "This is a long Hermes context note with useful details.",
        "parent_id": "folder123",
        "updated_time": 123,
        "source": "joplin",
    }]


def test_joplin_get_note_validates_id_and_truncates_body(monkeypatch):
    from api import routes

    def fake_get(path, params=None):
        assert path == "/notes/abc123def4567890"
        return {
            "id": "abc123def4567890",
            "title": "Big Note",
            "body": "x" * 60000,
            "parent_id": "folder123",
            "updated_time": 456,
            "created_time": 123,
        }

    monkeypatch.setattr(routes, "_joplin_api_get", fake_get)

    note = routes._joplin_get_note("abc123def4567890")

    assert note["title"] == "Big Note"
    assert note["source"] == "joplin"
    assert len(note["body"]) < 51000
    assert "Preview truncated" in note["body"]


def test_joplin_api_get_uses_authorization_header(monkeypatch):
    from api import routes

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self, _limit):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(routes, "_joplin_connection_from_config", lambda: ("http://127.0.0.1:41184", "secret-token"))
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    data = routes._joplin_api_get("/notes", {"query": "hello world"})

    assert data == {"ok": True}
    assert captured["timeout"] == 8
    assert "token=" not in captured["url"]
    assert "query=hello+world" in captured["url"]
    assert captured["authorization"] == "token secret-token"


def test_joplin_recent_ai_notes_uses_configured_prefill_script(monkeypatch, tmp_path):
    from api import routes

    script = tmp_path / "joplin_context.py"
    script.write_text(
        '\n'.join([
            'CURRENT_CONTEXT_ID = "5ba9ab822c344115939205ca4e8eaec0"',
            'OPEN_ISSUES_ID = "623aeb6e55cb4aa39a0541f2ac09aa36"',
            'AGENT_MEMORY_ID = "0a7a232ea46b4b8bb0bbd4358f725a84"',
            'RAW_CAPTURES_ID = "cb1087795c7d4129a863ab0a5642233d"',
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(routes, "get_config", lambda: {"prefill_messages_script": str(script)})

    def fake_get(path, params=None):
        note_id = path.rsplit("/", 1)[-1]
        titles = {
            "5ba9ab822c344115939205ca4e8eaec0": "Current Context",
            "623aeb6e55cb4aa39a0541f2ac09aa36": "Open Issues",
            "0a7a232ea46b4b8bb0bbd4358f725a84": "Agent Memory",
        }
        assert note_id in titles
        return {"id": note_id, "title": titles[note_id], "updated_time": 123, "parent_id": "folder"}

    monkeypatch.setattr(routes, "_joplin_api_get", fake_get)

    notes = routes._joplin_recent_ai_notes(limit=3)

    assert [note["title"] for note in notes] == ["Current Context", "Open Issues", "Agent Memory"]
    assert all(note["source"] == "joplin" for note in notes)
    assert all(note["used_by"] == "ai_prefill" for note in notes)
    assert all(note["used_reason"] == "automatic_recall" for note in notes)


def test_joplin_recent_ai_notes_prefers_webui_prefill_script_hook(monkeypatch, tmp_path):
    from api import routes

    legacy_script = tmp_path / "legacy_context.py"
    legacy_script.write_text('CURRENT_CONTEXT_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n', encoding="utf-8")
    webui_script = tmp_path / "webui_context.py"
    webui_script.write_text(
        'CURRENT_CONTEXT_ID = "5ba9ab822c344115939205ca4e8eaec0"\n'
        'OPEN_ISSUES_ID = "623aeb6e55cb4aa39a0541f2ac09aa36"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(routes, "get_config", lambda: {
        "prefill_messages_script": str(legacy_script),
        "webui_prefill_messages_script": ["python3", str(webui_script)],
    })

    def fake_get(path, params=None):
        note_id = path.rsplit("/", 1)[-1]
        assert note_id != "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        titles = {
            "5ba9ab822c344115939205ca4e8eaec0": "Current Context",
            "623aeb6e55cb4aa39a0541f2ac09aa36": "Open Issues",
        }
        return {"id": note_id, "title": titles[note_id], "updated_time": 123, "parent_id": "folder"}

    monkeypatch.setattr(routes, "_joplin_api_get", fake_get)

    notes = routes._joplin_recent_ai_notes(limit=2)

    assert [note["title"] for note in notes] == ["Current Context", "Open Issues"]


def test_joplin_recent_ai_notes_mirrors_webui_prefill_env_hook(monkeypatch, tmp_path):
    from api import routes

    legacy_script = tmp_path / "legacy_context.py"
    legacy_script.write_text('CURRENT_CONTEXT_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n', encoding="utf-8")
    env_script = tmp_path / "env context.py"
    env_script.write_text('CURRENT_CONTEXT_ID = "5ba9ab822c344115939205ca4e8eaec0"\n', encoding="utf-8")
    monkeypatch.setattr(routes, "get_config", lambda: {"prefill_messages_script": str(legacy_script)})
    monkeypatch.setenv("HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT", f'python3 "{env_script}"')

    def fake_get(path, params=None):
        note_id = path.rsplit("/", 1)[-1]
        assert note_id != "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        return {"id": note_id, "title": "Current Context", "updated_time": 123, "parent_id": "folder"}

    monkeypatch.setattr(routes, "_joplin_api_get", fake_get)

    notes = routes._joplin_recent_ai_notes(limit=1)

    assert [note["title"] for note in notes] == ["Current Context"]


def test_prefill_script_path_keeps_plain_existing_paths_with_spaces(tmp_path):
    from api import routes

    script = tmp_path / "context scripts" / "recall.py"
    script.parent.mkdir()
    script.write_text('CURRENT_CONTEXT_ID = "5ba9ab822c344115939205ca4e8eaec0"\n', encoding="utf-8")

    assert routes._script_path_from_config_value(str(script)) == script



def test_external_notes_ui_uses_minimal_lucide_icons_for_ai_recent_notes():
    from pathlib import Path

    panels = Path("static/panels.js").read_text(encoding="utf-8")
    start = panels.index("function _renderExternalNotesSources()")
    end = panels.index("function _renderMemoryDetail", start)
    notes_block = panels[start:end]
    assert "notes-ai-recent-card" in notes_block
    assert "li('bot', 14)" in notes_block
    assert "li('clock', 14)" in notes_block
    assert "Recently used by AI" not in notes_block  # i18n key, not hard-coded UI copy
    assert "🤖" not in notes_block
    assert "📚" not in notes_block


def test_external_notes_menu_item_is_default_off_from_memory_payload():
    from pathlib import Path

    panels = Path("static/panels.js").read_text(encoding="utf-8")
    assert "external_notes_enabled" in panels
    assert "if (s.key === 'external_notes' && !_memoryData.external_notes_enabled) continue;" in panels


def test_external_notes_drawer_copy_is_localized_outside_english():
    from pathlib import Path

    i18n = Path("static/i18n.js").read_text(encoding="utf-8")

    assert i18n.count("external_notes_sources: 'Third-party notes'") == 1
    assert i18n.count("external_notes_recent_ai: 'Recently used by AI'") == 1
    assert i18n.count("external_notes_recent_ai_reason: 'Automatic recall'") == 1
    assert i18n.count("external_notes_search_placeholder: 'Search notes…'") == 1

    locale_sources = [
        ("  en: {", "  it: {", "external_notes_sources: 'Third-party notes'"),
        ("  it: {", "  ja: {", "external_notes_sources: 'Note di terze parti'"),
        ("  ja: {", "  ru: {", "external_notes_sources: 'サードパーティのノート'"),
        ("  ru: {", "  es: {", "external_notes_sources: 'Сторонние заметки'"),
        ("  es: {", "  de: {", "external_notes_sources: 'Notas de terceros'"),
        ("  de: {", "  zh: {", "external_notes_sources: 'Notizen von Drittanbietern'"),
        ("  zh: {", "  'zh-Hant': {", "external_notes_sources: '第三方笔记'"),
        ("  'zh-Hant': {", "  pt: {", "external_notes_sources: '第三方筆記'"),
        ("  pt: {", "  ko: {", "external_notes_sources: 'Notas de terceiros'"),
        ("  ko: {", "  fr: {", "external_notes_sources: '타사 노트'"),
        ("  fr: {", "  tr: {", "external_notes_sources: 'Notes tierces'"),
    ]
    for start_marker, end_marker, expected in locale_sources:
        start = i18n.index(start_marker)
        end = i18n.index(end_marker, start)
        assert expected in i18n[start:end]

    tr_start = i18n.index("  tr: {")
    tr_block = i18n[tr_start:]
    assert "external_notes_sources: 'Üçüncü taraf notlar'" in tr_block
    assert "external_notes_sources: 'Third-party notes'" not in tr_block


def test_external_notes_search_button_matches_minimal_dark_controls():
    from pathlib import Path

    css = Path("static/style.css").read_text(encoding="utf-8")
    assert ".notes-search-form button" in css
    button_block = css[css.index(".notes-search-form button"):css.index(".notes-search-form button:hover")]
    assert "background:var(--panel)" in button_block or "background:var(--surface)" in button_block
    assert "border:1px solid var(--border)" in button_block
    assert "color:var(--text)" in button_block
    assert "border-radius:10px" in button_block
