"""Nested delegated-subagent children never surface as sidebar orphans.

The server stamps ``_cross_surface_child_session`` only when a child's source
differs from its parent's. A subagent that itself delegated produces a
``source='subagent'`` child of a ``source='subagent'`` parent, so the marker is
absent and the #5305 "parent out of view" suppression did not apply: the row
escaped to the top level as a view-only "Subagent Session" orphan.

#7263 covered delegated children of *messaging* parents (cross-surface marker
set). This covers the same-source nested-delegation case, while an ordinary
WebUI child of an absent parent keeps its top-level orphan fallback.
"""
import json

from tests.test_5306_subagent_sidebar_flicker import SESSIONS_JS_PATH, _preamble, _run_node


def _rows(collapsed_js: str, raw_js: str):
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + f"""
global._showArchived = false;
const collapsed = {collapsed_js};
const raw = {raw_js};
const rows = _attachChildSessionsToSidebarRows(collapsed, raw);
console.log(JSON.stringify({{
  topLevel: rows.map(r=>({{sid:r.session_id, orphan:!!r._orphan_child_session}})),
  children: Object.fromEntries(rows.map(r=>[r.session_id,(r._child_sessions||[]).map(c=>c.session_id)])),
}}));
"""
    return json.loads(_run_node(source))


_NESTED_CHILD = (
    "{ session_id:'nested_subagent_child', title:'Subagent Session', parent_session_id:'subagent_parent',"
    " relationship_type:'child_session', raw_source:'subagent', source_tag:'subagent', session_source:'other',"
    " source_label:'Subagent', parent_source:'subagent', message_count:46 }"
)


def test_nested_subagent_child_without_visible_parent_is_not_orphaned():
    out = _rows("[]", f"[{_NESTED_CHILD}]")
    assert out["topLevel"] == []


def test_nested_subagent_child_stacks_under_visible_subagent_parent():
    parent = (
        "{ session_id:'subagent_parent', title:'Subagent Session', raw_source:'subagent',"
        " source_tag:'subagent', session_source:'other', message_count:12 }"
    )
    out = _rows(f"[{parent}]", f"[{parent}, {_NESTED_CHILD}]")
    assert [r["sid"] for r in out["topLevel"]] == ["subagent_parent"]
    assert out["children"]["subagent_parent"] == ["nested_subagent_child"]


def test_ordinary_webui_child_of_absent_parent_still_orphans():
    child = (
        "{ session_id:'webui_child', title:'Ordinary WebUI child', parent_session_id:'absent_parent',"
        " relationship_type:'child_session', raw_source:'webui', source_tag:'webui', session_source:'webui',"
        " message_count:5 }"
    )
    out = _rows("[]", f"[{child}]")
    assert out["topLevel"] == [{"sid": "webui_child", "orphan": True}]
