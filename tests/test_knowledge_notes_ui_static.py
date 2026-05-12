import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
INDEX = (REPO / "static" / "index.html").read_text(encoding="utf-8")
PANELS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
I18N = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", src)
    assert match, f"{name}() not found"
    brace = src.find("{", match.end())
    assert brace != -1, f"{name}() has no body"
    depth = 1
    i = brace + 1
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    while i < len(src) and depth:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in "'\"`":
            in_string = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() body did not close"
    return src[brace + 1:i - 1]


def test_knowledge_tab_is_wired_between_memory_and_workspaces_in_rail_and_mobile_nav():
    rail = INDEX[INDEX.index('data-panel="memory"'):INDEX.index('data-panel="workspaces"')]
    assert 'data-panel="knowledge"' in rail

    mobile = INDEX[INDEX.index('data-panel="memory"', INDEX.index('class="sidebar-nav"')):INDEX.index('data-panel="workspaces"', INDEX.index('class="sidebar-nav"'))]
    assert 'data-panel="knowledge"' in mobile

    assert 'id="panelKnowledge"' in INDEX
    assert 'id="mainKnowledge"' in INDEX
    assert "tab_knowledge" in I18N


def test_knowledge_panel_fetches_local_api_and_renders_safe_obsidian_links():
    load_fn = _function_body(PANELS, "loadKnowledge")
    search_fn = _function_body(PANELS, "searchKnowledge")
    render_fn = _function_body(PANELS, "_renderKnowledgeResults")
    read_fn = _function_body(PANELS, "readKnowledgeSource")
    note_fn = _function_body(PANELS, "captureKnowledgeNote")

    assert "api('/api/knowledge/status" in load_fn or 'api("/api/knowledge/status' in load_fn
    assert "/api/knowledge/search" in search_fn
    assert "/api/knowledge/read" in read_fn
    assert "/api/knowledge/ask" in PANELS
    assert "askKnowledge" in PANELS
    assert "saveKnowledgeAnswer" in PANELS
    assert "id=\"knowledgeAsk\"" in INDEX
    assert "id=\"knowledgeAskResult\"" in INDEX
    assert "id=\"knowledgeSaveAnswerBtn\"" in INDEX
    assert "/api/notes/capture" in note_fn
    assert "esc(" in render_fn, "knowledge results must escape dynamic snippets/titles"
    assert "innerHTML" in render_fn and "snippet" in render_fn
    assert "obsidian://open" not in INDEX, "Obsidian URLs must come from API data, not baked fixtures"
    assert "target=\"_blank\"" in render_fn or "target='_blank'" in render_fn
    assert "rel=\"noopener noreferrer\"" in render_fn or "rel='noopener noreferrer'" in render_fn


def test_knowledge_css_hides_panel_by_default_and_shows_only_for_knowledge_class():
    css_min = re.sub(r"\s+", "", CSS)
    assert "main.main>#mainKnowledge" in css_min
    assert ":not(.showing-knowledge)" in css_min
    assert "main.main.showing-knowledge>#mainKnowledge{display:flex" in css_min
    for cls in ("knowledge-search-row", "knowledge-result", "knowledge-note-form"):
        assert f".{cls}" in css_min


def test_knowledge_ui_source_fixtures_do_not_bake_private_content():
    combined = "\n".join([INDEX, PANELS, CSS, I18N])
    assert "SECRET_VALUE_DO_NOT_LEAK" not in combined
    assert "/Users/bschmidy10/Documents/Obsidian Vault" not in combined
