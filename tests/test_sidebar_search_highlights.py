import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = ROOT / "static" / "sessions.js"
STYLE_CSS = ROOT / "static" / "style.css"
INDEX_HTML = ROOT / "static" / "index.html"


def _run_js_ranges(cases):
    src = SESSIONS_JS.read_text(encoding="utf-8")
    start = src.index("function _sessionSearchRanges")
    end = src.index("function _appendHighlightedText", start)
    helper = src[start:end]
    script = helper + "\nconsole.log(JSON.stringify(cases.map(c => _sessionSearchRanges(c.text, c.query))));"
    completed = subprocess.run(
        ["node", "-e", "const cases = " + json.dumps(cases) + ";\n" + script],
        check=True,
        text=True,
        capture_output=True,
        cwd=ROOT,
    )
    return json.loads(completed.stdout)


def test_sidebar_search_ranges_are_case_insensitive_and_repeated():
    ranges = _run_js_ranges([
        {"text": "Psalm notes and psalm outline", "query": "PSALM"},
    ])[0]
    assert ranges == [{"start": 0, "end": 5}, {"start": 16, "end": 21}]


def test_sidebar_search_ranges_handle_regex_special_characters():
    ranges = _run_js_ranges([
        {"text": "Can we search for foo(bar)+baz? safely", "query": "foo(bar)+baz?"},
    ])[0]
    assert ranges == [{"start": 18, "end": 31}]


def test_sidebar_search_ranges_support_multi_word_exact_then_tokens():
    exact, tokenized = _run_js_ranges([
        {"text": "Parallel TTS Audio Generation Strategy", "query": "tts audio"},
        {"text": "Parallel TTS and Audio Strategy", "query": "tts audio"},
    ])
    assert exact == [{"start": 9, "end": 18}]
    assert tokenized == [{"start": 9, "end": 12}, {"start": 17, "end": 22}]


def test_sidebar_search_ranges_empty_query_returns_no_ranges():
    assert _run_js_ranges([{"text": "Psalm", "query": ""}])[0] == []


def test_session_search_preview_trims_long_body_with_ellipses():
    from api.routes import _session_search_preview

    body = "Intro " + ("before " * 20) + "generated audio for the Psalm study" + (" after" * 20)
    preview = _session_search_preview(body, "Psalm", max_len=92)
    assert preview.startswith("...")
    assert preview.endswith("...")
    assert "Psalm" in preview
    assert len(preview) <= 98


def test_session_search_preview_handles_empty_or_unavailable_body():
    from api.routes import _session_search_message_text, _session_search_preview

    assert _session_search_preview("", "psalm") == ""
    assert _session_search_preview(None, "psalm") == ""
    assert _session_search_preview("No matching body", "psalm") == ""
    assert _session_search_preview("Some body", "") == ""
    assert _session_search_message_text({}) == ""
    assert _session_search_message_text({"content": [{"type": "text", "text": "Psalm body"}]}) == "Psalm body"


def test_sidebar_search_rendering_uses_safe_dom_helpers():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert "function _appendHighlightedText" in src
    assert ".textContent=source.slice(r.start,r.end)" in src
    assert "dangerouslySetInnerHTML" not in src
    assert "displayTitle.toLowerCase().includes(searchQueryRaw.toLowerCase())" in src
    assert "contentPreview=titleMatched?'':_sessionSearchContentPreview" in src
    assert "if(($('sessionSearch').value||'').trim()) _hideSearchPreviewsAfterSelect=true;" in src
    assert ".session-search-preview" in css
    assert "-webkit-line-clamp:2" in css


def test_session_search_has_accessible_clear_button():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert '<div class="session-search sidebar-search"><div class="session-search-field">' in html
    clear = re.search(r'<button[^>]*id="sessionSearchClear"[^>]*>', html)
    assert clear, "#sessionSearchClear button not found beside #sessionSearch"
    tag = clear.group(0)
    assert 'type="button"' in tag
    assert 'hidden' in tag
    assert 'onclick="clearSessionSearch()"' in tag
    assert 'aria-label="Clear conversation filter"' in tag


def test_session_search_clear_button_styles_do_not_shift_input_width():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".session-search-field{position:relative;display:flex;align-items:center;width:100%;}" in css
    assert ".session-search input{padding-right:34px;}" in css
    assert ".sidebar-search-icon{position:absolute;left:10px;top:50%;transform:translateY(-50%);" in css
    assert ".session-search .session-search-clear{position:absolute;" in css
    assert "right:6px;top:50%;transform:translateY(-50%)" in css
    assert "z-index:1" in css
    assert ".session-search-clear[hidden]{display:none;}" in css
    assert ".session-search-clear:focus-visible" in css


def test_session_search_clear_sync_and_click_behaviour():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    start = src.index("function syncSessionSearchClear")
    end = src.index("function filterSessions", start)
    helper = src[start:end]
    script = helper + r"""
const input = { value: 'Psalm', focused: false, focus(){ this.focused = true; } };
const clear = { hidden: true };
let filtered = 0;
function $(id){ return id === 'sessionSearch' ? input : clear; }
function filterSessions(){ filtered += 1; syncSessionSearchClear(); }
syncSessionSearchClear();
const visibleAfterText = clear.hidden === false;
clearSessionSearch();
const clearedOnClick = input.value === '' && clear.hidden === true && filtered === 1 && input.focused === true;
input.value = 'again';
input.focused = false;
clearSessionSearch(false);
const preserveFocus = input.value === '' && clear.hidden === true && filtered === 2 && input.focused === false;
console.log(JSON.stringify({visibleAfterText, clearedOnClick, preserveFocus}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        text=True,
        capture_output=True,
        cwd=ROOT,
    )
    result = json.loads(completed.stdout)
    assert result == {
        "visibleAfterText": True,
        "clearedOnClick": True,
        "preserveFocus": True,
    }
