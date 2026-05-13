import json
import re
import sqlite3
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
COMPACT_UI = re.sub(r"\s+", "", UI_JS)


def test_cli_tool_result_diff_snippet_is_not_cut_to_200_chars():
    """Diff-like CLI tool results should reach the existing tool-card expander."""
    assert "function _cliToolResultSnippet" in UI_JS
    assert "function _cliLooksLikePatchDiff" in UI_JS
    assert r"\*\*\* Begin Patch" in UI_JS
    assert "diff --git" in UI_JS
    assert (
        "if(_cliLooksLikePatchDiff(fullText))return_clipCliToolSnippet(fullText);"
        in COMPACT_UI
    )
    assert "returnString(fullText||'').slice(0,200);" in COMPACT_UI


def test_cli_tool_fallback_promotes_apply_patch_args_to_tool_card_snippet():
    """A successful apply_patch result may only say 'Success'; keep the patch visible."""
    assert "function _cliPatchSnippetFromArgs" in UI_JS
    assert "toolName==='apply_patch'" in COMPACT_UI
    assert "'old_string'" in UI_JS
    assert "'new_string'" in UI_JS
    assert "constpatchSnippet=_cliPatchSnippetFromArgs(name,args);" in COMPACT_UI
    assert "snippet:_cliToolCardSnippet(resultSnippet,patchSnippet)" in COMPACT_UI
    assert "is_diff:_cliToolCardHasDiffSnippet(resultSnippet,patchSnippet)" in COMPACT_UI


def test_diff_tool_cards_use_show_diff_expander_label():
    assert "const moreLabel=tc.is_diff?'Show diff':'Show more';" in UI_JS
    assert "const lessLabel=tc.is_diff?'Hide diff':'Show less';" in UI_JS
    assert 'data-more-label="${esc(moreLabel)}"' in UI_JS


def _function_source(src: str, name: str) -> str:
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
    return src[match.start() : i]


def test_rendered_apply_patch_tool_card_html_contains_diff_lines():
    """Drive the actual snippet helpers and buildToolCard() through Node."""
    function_names = [
        "_clipCliToolSnippet",
        "_cliToolResultText",
        "_cliLooksLikePatchDiff",
        "_cliToolResultSnippet",
        "_prefixedCliDiffLines",
        "_firstOwnedValue",
        "_cliPatchSnippetFromArgs",
        "_cliToolCardSnippet",
        "_cliToolCardHasDiffSnippet",
        "buildToolCard",
    ]
    functions = "\n".join(_function_source(UI_JS, name) for name in function_names)
    script = textwrap.dedent(
        f"""
        function esc(s){{return String(s||'').replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));}}
        function li(){{return '';}}
        function toolIcon(){{return '';}}
        function _toolDisplayName(tc){{return tc.name||'tool';}}
        const document={{
          createElement(){{return {{className:'', innerHTML:''}};}}
        }};
        {functions}

        const longPatch = [
          '*** Begin Patch',
          '*** Update File: app.py',
          '@@',
          '-old',
          '+new',
          ...Array.from({{length: 150}}, (_, i) => '+line ' + i),
          '*** End Patch'
        ].join('\\n');
        const resultSnippet = _cliToolResultSnippet(JSON.stringify({{output:'Success'}}));
        const patchSnippet = _cliPatchSnippetFromArgs('apply_patch', {{patch: longPatch}});
        const row = buildToolCard({{
          name: 'apply_patch',
          snippet: _cliToolCardSnippet(resultSnippet, patchSnippet),
          is_diff: _cliToolCardHasDiffSnippet(resultSnippet, patchSnippet),
          args: {{patch: '(shown in diff)'}},
          done: true
        }});
        const errorSnippet = _cliToolCardSnippet('Patch failed: context not found', patchSnippet);
        process.stdout.write(JSON.stringify({{html: row.innerHTML, errorSnippet}}));
        """
    )
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    payload = json.loads(proc.stdout)
    html = payload["html"]
    assert "-old" in html
    assert "+new" in html
    assert "Show diff" in html
    assert "Patch failed: context not found" in payload["errorSnippet"]
    assert "-old" in payload["errorSnippet"]


def _make_state_db(path: Path) -> None:
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: app.py",
            "@@",
            "-old",
            "+new",
            "*** End Patch",
        ]
    )
    tool_calls = [
        {
            "id": "call_patch",
            "type": "function",
            "function": {
                "name": "apply_patch",
                "arguments": json.dumps({"patch": patch}),
            },
        }
    ]
    conn = sqlite3.Connection(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO messages (session_id, role, content, timestamp, tool_calls)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("issue1824", "assistant", "", "2026-01-01T00:00:01Z", json.dumps(tool_calls)),
        )
        conn.execute(
            """
            INSERT INTO messages (session_id, role, content, timestamp, tool_call_id, tool_name)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "issue1824",
                "tool",
                json.dumps({"output": "Success"}),
                "2026-01-01T00:00:02Z",
                "call_patch",
                "apply_patch",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_cli_session_reader_preserves_apply_patch_metadata(tmp_path, monkeypatch):
    """The API payload should keep tool_calls/tool rows for the UI renderer."""
    _make_state_db(tmp_path / "state.db")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    import api.profiles
    from api.models import get_cli_session_messages

    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: str(tmp_path))

    messages = get_cli_session_messages("issue1824")
    assert [m["role"] for m in messages] == ["assistant", "tool"]

    assistant = messages[0]
    assert assistant["tool_calls"][0]["function"]["name"] == "apply_patch"
    args = json.loads(assistant["tool_calls"][0]["function"]["arguments"])
    assert "*** Begin Patch" in args["patch"]
    assert "-old" in args["patch"]
    assert "+new" in args["patch"]

    tool = messages[1]
    assert tool["tool_call_id"] == "call_patch"
    assert tool["tool_name"] == "apply_patch"
    assert tool["name"] == "apply_patch"
    assert json.loads(tool["content"])["output"] == "Success"
