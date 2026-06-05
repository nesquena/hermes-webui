import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS_PATH = ROOT / "static" / "sessions.js"
SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_js_function(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    assert brace > start, f"{name} body not found"
    depth = 1
    i = brace + 1
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} body did not close"
    return src[start:i]


def _run_session_search_helper(cases):
    if NODE is None:
        pytest.skip("node not on PATH")
    driver = "\n".join(
        [
            "function _sessionDisplayTitle(s){return String((s&&s.title)||'');}",
            _extract_js_function(SESSIONS_JS, "_sessionSearchAddIdCandidate"),
            _extract_js_function(SESSIONS_JS, "_sessionSearchCleanUrlToken"),
            _extract_js_function(SESSIONS_JS, "_sessionSearchSessionIdCandidates"),
            _extract_js_function(SESSIONS_JS, "_sessionSearchDirectSessionMatches"),
            _extract_js_function(SESSIONS_JS, "_sessionSearchDirectAndTitleMatches"),
            _extract_js_function(SESSIONS_JS, "_sessionSearchMergeMatches"),
            "const cases = JSON.parse(process.argv[1]);",
            "const out = cases.map(c => ({candidates: _sessionSearchSessionIdCandidates(c.query), matches: _sessionSearchDirectSessionMatches(c.sessions, c.query).map(s => s.session_id), merged: _sessionSearchMergeMatches(c.sessions, c.query, c.content || []).map(s => s.session_id)}));",
            "process.stdout.write(JSON.stringify(out));",
        ]
    )
    result = subprocess.run(
        [NODE, "-e", driver, json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_session_action_menu_has_copy_link_action():
    assert "ICONS.link" in SESSIONS_JS
    assert "_appendSessionCopyLinkAction(menu, session);" in SESSIONS_JS
    assert "t('session_copy_link')" in SESSIONS_JS
    assert "t('session_copy_link_desc')" in SESSIONS_JS


def test_session_link_copies_internal_markdown_reference_not_external_url():
    assert "function _sessionInternalReferenceForSession" in SESSIONS_JS
    assert "session://${_sessionMarkdownUrlSid(sid)}" in SESSIONS_JS
    assert "_copyTextToClipboard(ref)" in SESSIONS_JS
    assert "function _sessionAbsoluteUrlForSid" not in SESSIONS_JS


def test_session_link_markdown_url_encodes_parentheses():
    assert "function _sessionMarkdownUrlSid" in SESSIONS_JS
    assert "encodeURIComponent(String(sid||''))" in SESSIONS_JS
    assert "replace(/[()]/g" in SESSIONS_JS
    assert "%28" in SESSIONS_JS
    assert "%29" in SESSIONS_JS


def test_session_link_label_collapses_multiline_titles():
    assert ".replace(/\\s+/g,' ')" in SESSIONS_JS


def test_copy_link_has_clipboard_fallback():
    assert "navigator.clipboard.writeText" in SESSIONS_JS
    assert "document.execCommand('copy')" in SESSIONS_JS
    assert "showToast(t('session_link_copied'))" in SESSIONS_JS
    assert "t('session_link_copy_failed')" in SESSIONS_JS


def test_read_only_sessions_can_still_open_actions_for_copy_link():
    start = SESSIONS_JS.index("function _openSessionActionMenu(session, anchorEl){")
    end = SESSIONS_JS.index("document.addEventListener('click'", start)
    open_menu_block = SESSIONS_JS[start:end]
    assert "Read-only imported sessions cannot be modified" not in open_menu_block
    assert "const isReadOnly = _isReadOnlySession(session);" in open_menu_block
    assert "if(isReadOnly){\n    _mountSessionActionMenu(menu, session, anchorEl);\n    return;\n  }" in open_menu_block


def test_copy_link_i18n_keys_have_english_and_german_labels():
    for key in [
        "session_copy_link",
        "session_copy_link_desc",
        "session_link_copied",
        "session_link_copy_failed",
    ]:
        assert I18N_JS.count(key) >= 2, f"{key} should be defined in English and German"
    assert "Copy conversation link" in I18N_JS
    assert "Unterhaltungslink kopieren" in I18N_JS


def test_rendered_session_reference_is_internal_link():
    assert "session:\\/\\/" in UI_JS
    assert "function _markdownAnchor" in UI_JS
    assert "class=\"session-link\"" in UI_JS
    assert "const sessionLink=e.target.closest('a.session-link[href]');" in UI_JS
    assert "loadSession(decodeURIComponent(m[1]))" in UI_JS


def test_streaming_markdown_keeps_session_refs_internal():
    assert "session:\\/\\/" in MESSAGES_JS
    assert "session-link" in MESSAGES_JS
    assert "_smdLinkHref" in MESSAGES_JS
    assert "^(file|workspace|session)" in MESSAGES_JS


def test_conversation_filter_extracts_session_ids_from_links_and_raw_ids():
    sessions = [
        {"session_id": "12f0ef3e1a62", "title": "Target"},
        {"session_id": "abc(def)", "title": "Paren"},
        {"session_id": "other", "title": "Other"},
    ]
    cases = [
        {"query": "12f0ef3e1a62", "sessions": sessions},
        {"query": "session://12f0ef3e1a62", "sessions": sessions},
        {"query": "https://example.test/session/12f0ef3e1a62", "sessions": sessions},
        {"query": "https://example.test/session/12f0ef3e1a62?foo=bar#chat", "sessions": sessions},
        {"query": "/session/12f0ef3e1a62", "sessions": sessions},
        {"query": "see https://example.test/session/12f0ef3e1a62).", "sessions": sessions},
        {"query": "[Target](session://12f0ef3e1a62)", "sessions": sessions},
        {"query": "[Target](https://example.test/session/12f0ef3e1a62)", "sessions": sessions},
        {"query": "?session_id=12f0ef3e1a62", "sessions": sessions},
        {"query": "https://example.test/session/abc%28def%29", "sessions": sessions},
        {"query": "session://abc%28def%29", "sessions": sessions},
    ]
    out = _run_session_search_helper(cases)
    assert [row["matches"] for row in out] == [
        ["12f0ef3e1a62"],
        ["12f0ef3e1a62"],
        ["12f0ef3e1a62"],
        ["12f0ef3e1a62"],
        ["12f0ef3e1a62"],
        ["12f0ef3e1a62"],
        ["12f0ef3e1a62"],
        ["12f0ef3e1a62"],
        ["12f0ef3e1a62"],
        ["abc(def)"],
        ["abc(def)"],
    ]


def test_conversation_filter_merges_direct_title_and_content_matches_without_dropping_posted_ids():
    sessions = [
        {"session_id": "target-123", "title": "Unrelated target title"},
        {"session_id": "title-hit", "title": "target-123 mentioned in title"},
        {"session_id": "other", "title": "Other"},
    ]
    content = [
        {"session_id": "target-123", "match_type": "content"},
        {"session_id": "posted-elsewhere", "match_type": "content"},
        {"session_id": "ignored-non-content", "match_type": "title"},
    ]
    out = _run_session_search_helper([
        {"query": "target-123", "sessions": sessions, "content": content},
    ])[0]
    assert out["merged"] == ["target-123", "title-hit", "posted-elsewhere"]


def test_conversation_filter_keeps_content_search_results_when_query_is_session_id():
    assert "function _sessionSearchMergeMatches" in SESSIONS_JS
    assert "function _sessionSearchDirectAndTitleMatches" in SESSIONS_JS
    assert "const sidebarRows=_sessionRowsWithActiveEphemeralSession(_allSessions);" in SESSIONS_JS
    assert "const searchMatches=_sessionSearchMergeMatches(sidebarRows,searchQueryRaw,_contentSearchResults);" in SESSIONS_JS
    assert "const allMatched=_ensureActiveSessionRowPresent(searchMatches,sidebarRows);" in SESSIONS_JS
    assert "const directAndTitleMatches=_sessionSearchDirectAndTitleMatches(_allSessions,currentQ);" in SESSIONS_JS
    assert "const directOrTitleIds=new Set(directAndTitleMatches.map(s=>s.session_id));" in SESSIONS_JS
    assert "!directOrTitleIds.has(s.session_id)" in SESSIONS_JS
    assert "api(`/api/sessions/search?q=${encodeURIComponent(requestedQ)}&content=1&depth=5`)" in SESSIONS_JS
