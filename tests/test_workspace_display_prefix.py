from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_workspace_display_prefix_helper_strips_leading_metadata_only():
    src = _read("static/ui.js")
    start = src.find("function _stripWorkspaceDisplayPrefix")
    assert start != -1, "workspace display prefix stripper not found"
    end = src.find("function _renderUserFencedBlocks", start)
    assert end != -1, "user fenced block renderer not found after prefix stripper"
    helper = src[start:end]

    assert r"^\s*\[Workspace::v1:\s*(?:\\.|[^\]\\])+\]\s*" in helper
    assert "[Workspace:[^\\]]+" not in helper
    assert ".trim()" in helper


def test_user_render_uses_stripped_display_content_without_preempting_context_cards():
    src = _read("static/ui.js")
    loop_start = src.find("for(let vi=0;vi<visWithIdx.length;vi++)")
    assert loop_start != -1, "message render loop not found"
    loop_end = src.find("if(!currentAssistantTurn)", loop_start)
    assert loop_end != -1, "assistant render branch not found after user branch"
    render_prefix = src[loop_start:loop_end]

    display_idx = render_prefix.find("const displayContent=isUser?_stripWorkspaceDisplayPrefix(content):content;")
    context_idx = render_prefix.find("if(_isContextCompactionMessage(m))")
    user_idx = render_prefix.find("if(isUser)")
    assert display_idx != -1, "display content stripper not used in render loop"
    assert context_idx != -1, "context compaction branch not found"
    assert user_idx != -1, "user render branch not found"
    assert display_idx < context_idx < user_idx
    assert "_renderUserFencedBlocks(displayContent)" in render_prefix
    assert "row.dataset.rawText=String(displayContent).trim();" in render_prefix
