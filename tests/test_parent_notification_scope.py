"""A conversation's sidebar indicator must not inherit child notifications."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _render_indicator_script():
    source = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
    # Execute the real row-state/class calculation and indicator construction,
    # excluding unrelated title, menu and swipe DOM scaffolding in between.
    start = source.index("function _renderOneSession(")
    prefix_end = source.index("    const swipeReturnOffset=", start)
    dot_class = source.index("    const attentionDotClass=", prefix_end)
    dot_start = source.rindex("    const state=document.createElement('span');", prefix_end, dot_class)
    dot_end = source.index("    // Single trigger button", dot_start)
    return source[start:prefix_end] + source[dot_start:dot_end] + "return el; }"


@pytest.mark.parametrize("child_state", ["unread", "streaming", "approval", "clarify", "all"])
@pytest.mark.parametrize("own_state", ["idle", "unread", "streaming", "approval", "clarify"])
@pytest.mark.parametrize("active", [False, True])
def test_parent_indicator_uses_only_own_state(child_state, own_state, active):
    row = {
        "session_id": "parent",
        "has_unread": own_state == "unread",
        "is_streaming": own_state == "streaming",
        "_child_session_has_unread": child_state in ("unread", "all"),
        "_child_session_streaming": child_state in ("streaming", "all"),
    }
    if own_state in ("approval", "clarify"):
        row["attention"] = {"kind": own_state, "count": 1, "title": "Parent request"}
    if child_state in ("approval", "clarify", "all"):
        row["_child_session_attention"] = {
            "kind": "approval" if child_state == "all" else child_state,
            "count": 2,
            "title": "Child request",
        }
    script = f"""
const activeSidForSidebar = {json.dumps('parent' if active else 'other')};
const S = {{session:null}};
const document = {{createElement: () => ({{
  children:[], setAttribute(){{}}, appendChild(child){{this.children.push(child);}}
}})}};
function _sessionLineageContainsSession(s, sid) {{return s.session_id === sid;}}
function _isSessionEffectivelyStreaming(s) {{return !!s.is_streaming;}}
function _hasUnreadForSession(s) {{return !!s.has_unread;}}
function _sessionAttentionState(s) {{return s.attention || null;}}
function _isReadOnlySession() {{return false;}}
function _rememberRenderedStreamingState() {{}}
function _rememberRenderedSessionSnapshot() {{}}
function _sessionStateTooltip({{isStreaming,hasUnread}}) {{
  return isStreaming ? 'Conversation is running' : (hasUnread ? 'Unread completion' : '');
}}
{_render_indicator_script()}
const row = {json.dumps(row)};
const before = JSON.stringify(row);
const rendered = _renderOneSession(row);
console.log(JSON.stringify({{rendered, unchanged:before === JSON.stringify(row)}}));
"""
    assert NODE is not None
    result = subprocess.run([NODE], input=script, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    rendered = output["rendered"]
    dot = rendered["children"][0]
    assert (" is-unread" in dot["className"]) == (own_state == "unread" and not active)
    assert (" is-streaming" in dot["className"]) == (own_state == "streaming")
    for kind in ("approval", "clarify"):
        assert (f" is-attention-{kind}" in dot["className"]) == (own_state == kind)
    assert (" unread" in rendered["className"]) == (own_state == "unread" and not active)
    assert (" needs-attention" in rendered["className"]) == (own_state in ("approval", "clarify"))
    assert dot.get("title", "") != "Child request"
    assert output["unchanged"], "Rendering must not acknowledge or mutate child state"
