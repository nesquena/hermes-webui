"""Nested subagent rows drop the agent's "Subagent: " title prefix; rename and search keep it."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parent.parent / "static" / "sessions.js").read_text()
CHILD = {"session_id": "c1", "title": "Subagent: Build index", "parent_session_id": "p",
         "relationship_type": "child_session", "raw_source": "subagent"}
TITLE_FNS = ("_isChildSession", "_isDelegatedSubagentRow", "_sessionDisplayTitle", "_nestedChildTitle")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node required")


def _fn(name):
    m = re.search(r"^function " + name + r"\(.*?^}\n", SRC, re.S | re.M)
    assert m, name
    return m.group(0)


def _node(names, body):
    js = "\n".join(_fn(n) for n in names) + "\n" + body
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def test_nested_label_strips_prefix_only_for_delegated_children():
    rows = [
        {**CHILD, "title": "Subagent: STRUCT-GO: implement parsers"},
        {"title": "Subagent: plain chat", "raw_source": "webui"},
        {**CHILD, "title": "Subagent:"},
    ]
    out = _node(TITLE_FNS, f"console.log(JSON.stringify({json.dumps(rows)}.map(_nestedChildTitle)));")
    assert out == ["STRUCT-GO: implement parsers", "Subagent: plain chat", ""]


def test_only_nested_child_label_uses_stripped_title():
    m = re.search(r"const childLabelFor=\(child\)=>\{\n\s*const childTitle=(.*?);", SRC)
    assert m and m.group(1).startswith("_nestedChildTitle(child)")
    assert SRC.count("_nestedChildTitle(") == 2  # definition + childLabelFor


def test_search_and_display_title_keep_subagent_prefix():
    body = f"""
    function _sessionSearchDirectSessionMatches(){{return [];}}
    const row={json.dumps(CHILD)};
    console.log(JSON.stringify({{
      display:_sessionDisplayTitle(row),
      nested:_nestedChildTitle(row),
      hits:_sessionSearchDirectAndTitleMatches([row],'Subagent').map(s=>s.session_id),
    }}));"""
    out = _node(TITLE_FNS + ("_sessionSearchDirectAndTitleMatches",), body)
    assert out == {"display": "Subagent: Build index", "nested": "Build index", "hits": ["c1"]}


def test_rename_open_then_cancel_keeps_cached_title_fields():
    body = f"""
    let _loadingSessionId=null,_renamingSid=null;
    const S={{session:null}};
    function _isReadOnlySession(){{return false;}}
    function closeSessionActionMenu(){{}}
    function renderSessionListFromCache(){{}}
    function syncTopbar(){{}}
    let inp=null;
    const document={{createElement(){{inp={{isConnected:false,addEventListener(){{}},replaceWith(){{}},focus(){{}},select(){{}}}};return inp;}}}};
    const row={json.dumps(CHILD)};
    const _allSessions=[{{...row}}];
    _buildSessionRenameStarter(row, {{replaceWith(){{}}}}, ()=>{{}})();
    const opened=inp.value;
    inp.onkeydown({{key:'Escape',preventDefault(){{}},stopPropagation(){{}}}});
    const pick=o=>[o.title,o.display_title,o._state_db_title];
    console.log(JSON.stringify({{opened,row:pick(row),cached:pick(_allSessions[0])}}));"""
    out = _node(TITLE_FNS + ("_buildSessionRenameStarter",), body)
    want = ["Subagent: Build index"] * 3
    assert out == {"opened": "Subagent: Build index", "row": want, "cached": want}
