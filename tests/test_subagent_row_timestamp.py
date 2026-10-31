"""Nested subagent sidebar rows must always keep their last-run timestamp.

The label used to be one text node (`-> title via X - 1h`) inside a
`white-space:nowrap; overflow:hidden; text-overflow:ellipsis` button, so a long
title pushed the trailing timestamp into the clipped overflow. The timestamp now
lives in its own non-shrinking span next to an ellipsizing title span.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

LONG = "Rework hx11 wave-6 unit 'toobit' on existing " + "very long title " * 20


def _extract(name: str) -> str:
    start = JS.index(f"function {name}(")
    i = JS.index("{", start) + 1
    depth = 1
    while depth:
        depth += {"{": 1, "}": -1}.get(JS[i], 0)
        i += 1
    return JS[start:i]


def _label_block() -> str:
    """The real childLabelFor/setChildLabel closure source from the row renderer."""
    start = JS.index("const childLabelFor=(child)=>{")
    end = JS.index("const installForkChildSwipe", start)
    return JS[start:end]


def _run(sessions, root_id="root"):
    # Rows are built through the real attach step and the real label closure.
    source = f"""
class El {{
  constructor(tag) {{ this.tag = tag; this.className = ''; this.children = []; this.textContent = ''; }}
  replaceChildren(...k) {{ this.children = k; }}
}}
global.document = {{ createElement: t => new El(t) }};
const src = {json.dumps(JS)};
function extractFunc(name) {{
  const start = src.indexOf('function ' + name + '(');
  let i = src.indexOf('{{', start) + 1, d = 1;
  while (d) {{ const c = src[i++]; if (c === '{{') d++; else if (c === '}}') d--; }}
  return src.slice(start, i);
}}
eval(src.match(/const _MESSAGING_RAW_SOURCES = [^;]*;/)[0].replace('const ', 'global.'));
for (const n of ['_isMessagingSession','_isWebUiSourceSession','_isExternalSession','_isCliSession',
  '_isChildSession','_isForkWithResolvableParent','_sessionLineageKey','_sidebarLineageKeyForRow',
  '_attachChildSessionsToSidebarRows','_isDelegatedSubagentRow','_nestedChildTitle']) eval(extractFunc(n));
global._isCliSession=_isCliSession; global._isExternalSession=_isExternalSession; global._isMessagingSession=_isMessagingSession;
global._hasUnreadForSession = () => false; global._isSessionEffectivelyStreaming = () => false;
global._sessionAttentionState = () => null;
global.INFLIGHT = {{}}; global.S = {{ session: null }};
global._showArchived = false;
function _sessionDisplayTitle(s) {{ return s.title || ''; }}
function _sessionTimestampMs(s) {{ return (s.last_message_at || s.updated_at) * 1000; }}
function _formatRelativeSessionTime(ms) {{ return 'T' + ms; }}
const sessions = {json.dumps(sessions)};
const rows = _attachChildSessionsToSidebarRows(sessions.filter(s => !s.parent_session_id), sessions);
const parent = rows.find(r => r.session_id === {json.dumps(root_id)});
{_label_block()}
const out = (parent._child_sessions || []).map(c => {{
  const el = new El('button'); setChildLabel(el, c);
  return {{ sid: c.session_id, kids: el.children.map(k => [k.className, k.textContent]) }};
}});
console.log(JSON.stringify(out));
"""
    res = subprocess.run([NODE], input=source, capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _sub(sid, parent, title, ts):
    return {"session_id": sid, "parent_session_id": parent, "title": title, "raw_source": "subagent",
            "source_tag": "subagent", "session_source": "other", "relationship_type": "child_session",
            "message_count": 3, "updated_at": ts, "last_message_at": ts}


def _tree(title):
    return [
        {"session_id": "root", "title": "Root", "raw_source": "webui", "source_tag": "webui",
         "session_source": "webui", "message_count": 5, "updated_at": 100, "last_message_at": 100},
        _sub("d1", "root", title, 101),
        _sub("d2", "d1", title, 102),
    ]


@pytest.mark.parametrize("title", ["Short", LONG])
def test_depth1_and_depth2_subagent_rows_render_timestamp(title):
    out = {r["sid"]: dict(r["kids"]) for r in _run(_tree(title))}
    assert set(out) == {"d1", "d2"}  # both depths render under the top-level row
    assert out["d1"]["session-child-time"] == " - T101000"
    assert out["d2"]["session-child-time"] == " - T102000"
    for sid in out:
        assert out[sid]["session-child-label"].startswith("-> ")


def test_long_title_stays_in_ellipsizing_label_not_in_time():
    for r in _run(_tree(LONG)):
        kids = dict(r["kids"])
        assert LONG.split("very")[0].strip() in kids["session-child-label"]
        assert "very long" not in kids["session-child-time"]


def _rule(selector_re):
    m = re.search(r"(?m)^\s*(" + selector_re + r")\s*\{([^}]*)\}", CSS)
    assert m, selector_re
    return m.group(2)


def test_css_title_ellipsizes_and_time_never_shrinks():
    label = _rule(r"\.session-child-label")
    for decl in ("flex:1 1 auto", "min-width:0", "overflow:hidden", "text-overflow:ellipsis", "white-space:nowrap"):
        assert decl in label
    assert "flex:none" in _rule(r"\.session-child-time")


def test_css_child_row_is_flex_so_time_sits_beside_title():
    row = _rule(r"\.session-child-session:not\(\.session-child-session-fork\),\.session-child-session-main")
    assert "display:flex" in row and "min-width:0" in row
