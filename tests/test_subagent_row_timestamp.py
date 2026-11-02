"""Subagent sidebar rows keep their last-run time when the title is long (time is its own span)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "sessions.js").read_text()
CSS = (ROOT / "static" / "style.css").read_text()
LONG = "Rework unit 'toobit' " + "very long title " * 20

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node required")


@pytest.mark.parametrize("title", ["Short", LONG])
def test_label_splits_title_and_time(title):
    block = JS[JS.index("const childLabelFor=(child)=>{"):JS.index("const installForkChildSwipe")]
    js = f"""
    global.document={{createElement:()=>({{}})}};
    const _nestedChildTitle=c=>c.title, _sessionTimestampMs=c=>c.ts, _formatRelativeSessionTime=ms=>'T'+ms;
    {block}
    console.log(JSON.stringify(childLabelFor({{title:{json.dumps(title)},ts:5}}).map(e=>[e.className,e.textContent])));"""
    out = dict(json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout))
    assert out == {"session-child-label": f"-> {title}", "session-child-time": " - T5"}


def test_css_title_ellipsizes_and_time_never_shrinks():
    def rule(sel):
        return re.search(r"(?m)^" + re.escape(sel) + r"\{([^}]*)\}", CSS)[1]
    assert "text-overflow:ellipsis" in rule(".session-child-label") and "min-width:0" in rule(".session-child-label")
    assert "flex:none" in rule(".session-child-time")
    assert "display:flex" in rule(".session-child-session:not(.session-child-session-fork),.session-child-session-main")
