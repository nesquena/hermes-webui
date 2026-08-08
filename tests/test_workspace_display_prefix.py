import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def _function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated {name}")


def _run_workspace_prefix_behavior(helper: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if not node:
        raise AssertionError("node is required for workspace-prefix behavior coverage")
    cases = [
        (r"[Workspace::v1: D:\\ai]" + "\nhello", "hello"),
        (r"[Workspace: D:\ai]" + "\nhello", "hello"),
        ("hello", "hello"),
        (r"prefix [Workspace::v1: D:\\ai]" + "\nhello", r"prefix [Workspace::v1: D:\\ai]" + "\nhello"),
        (r"[Workspace::v1: D:\ai", r"[Workspace::v1: D:\ai"),
    ]
    script = f"""
const assert=require('assert');
{helper}
const cases={json.dumps(cases)};
for(const [input,expected] of cases) assert.strictEqual(_stripWorkspaceDisplayPrefix(input),expected);
"""
    return subprocess.run([node, "-e", script], text=True, capture_output=True, check=False)


def test_workspace_display_prefix_helper_strips_leading_metadata_only():
    src = _read("static/ui.js")
    start = src.find("function _stripWorkspaceDisplayPrefix")
    assert start != -1, "workspace display prefix stripper not found"
    end = src.find("function _renderUserFencedBlocks", start)
    assert end != -1, "user fenced block renderer not found after prefix stripper"
    helper = src[start:end]

    # v1 sentinel regex must be present (matches `[Workspace::v1: <escaped path>]`).
    assert r"^\s*\[Workspace::v1:\s*(?:\\.|[^\]\\])+\]\s*" in helper
    # Legacy regex must ALSO be present as a fallback for transcripts saved
    # before the v1 migration (per Opus advisor on stage-322 — without this,
    # pre-upgrade sessions render the literal `[Workspace: /path]` prefix in
    # user bubbles after upgrade). Mirrors the Python `include_legacy=True`
    # branch in api/streaming.py:_strip_workspace_prefix().
    assert r"\[Workspace:[^\]]+\]" in helper
    assert ".trim()" in helper

    function_source = _function_source(src, "_stripWorkspaceDisplayPrefix")
    control = _run_workspace_prefix_behavior(function_source)
    assert control.returncode == 0, control.stderr

    target = "if(stripped !== value) return stripped.trim();"
    assert function_source.count(target) == 1
    mutant = _run_workspace_prefix_behavior(
        function_source.replace(target, "if(stripped !== value) return value.trim();", 1)
    )
    assert mutant.returncode != 0, "disabling v1 prefix stripping must RED"


def test_user_render_uses_stripped_display_content_without_preempting_context_cards():
    src = _read("static/ui.js")
    loop_start = src.find("for(let vi=0;vi<visWithIdx.length;vi++)")
    assert loop_start != -1, "message render loop not found"
    loop_end = src.find("if(!currentAssistantTurn)", loop_start)
    assert loop_end != -1, "assistant render branch not found after user branch"
    render_prefix = src[loop_start:loop_end]

    display_idx = render_prefix.find("const displayContent=isUser?_stripAttachedFilesMarkerForDisplay(_stripWorkspaceDisplayPrefix(content)):")
    context_idx = render_prefix.find("if(_isContextCompactionMessage(m))")
    user_idx = render_prefix.find("if(isUser)")
    assert display_idx != -1, "display content stripper not used in render loop"
    assert "_assistantDisplayContentFromMessage(m, content)" in render_prefix
    assert context_idx != -1, "context compaction branch not found"
    assert user_idx != -1, "user render branch not found"
    assert display_idx < context_idx < user_idx
    # The render call may be a direct _renderUserFencedBlocks call or go
    # through the cached wrapper _getCachedRender.  Both paths accept the
    # already-stripped displayContent, so the invariant holds either way.
    assert ("_renderUserFencedBlocks(displayContent)" in render_prefix or
            "_getCachedRender(displayContent, isUser)" in render_prefix)
    assert "const newRawText=String(displayContent).trim();" in render_prefix
    assert "row.dataset.rawText=newRawText;" in render_prefix
