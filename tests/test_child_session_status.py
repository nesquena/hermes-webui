"""Child state stays visible without borrowing the parent's own notification."""
import json
import shutil
import subprocess

import pytest

from tests._sidebar_child_status_helpers import FAKE_DOM, component_script

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def session(sid, state="idle", **extra):
    row = {"session_id": sid, "title": sid, "message_count": 3,
           "last_message_at": 10, "has_unread": state == "unread",
           "is_streaming": state == "streaming", **extra}
    if state in ("approval", "clarify", "generic"):
        row["attention"] = {"kind": state, "count": 1}
    return row


def run_component(raw, references, expanded, active, search=""):
    component = component_script().replace("searchQueryRaw=''", f"searchQueryRaw={json.dumps(search)}")
    script = FAKE_DOM + component + f"""
const raw={json.dumps(raw)}, refs={json.dumps(references)};
const before=JSON.stringify([raw,refs]);
const result=renderFixture(raw,refs,{json.dumps(expanded)},{json.dumps(active)});
const nodes=flatten(result.element);
const chip=nodes.find(e=>(e.className||'').split(' ').includes('session-child-count'));
const children=nodes.filter(e=>(e.className||'').split(' ').includes('session-child-session'));
(async()=>{{
  for(const child of children){{
    const button=child.tag==='button'?child:child.children.find(e=>e.tag==='button');
    await button.onclick({{stopPropagation(){{}}}});
  }}
  console.log(JSON.stringify({{
    parent:result.element.className,
    time:nodes.find(e=>(e.className||'').startsWith('session-time')),
    dot:result.element.children.find(e=>(e.className||'').includes('session-attention-indicator')),
    activity:nodes.filter(e=>(e.className||'').split(' ').includes('session-child-activity-indicator')),
    chip:chip||null,children,opened,row:result.row,unchanged:before===JSON.stringify([raw,refs])
  }}));
}})();
"""
    assert NODE is not None
    result = subprocess.run([NODE], input=script, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def expected_mark(state):
    return {"streaming": "is-streaming", "unread": "is-unread",
            "approval": "is-attention-approval", "clarify": "is-attention-clarify",
            "generic": "is-attention-generic"}.get(state)


@pytest.mark.parametrize("kind", ["fork", "delegated", "reference"])
@pytest.mark.parametrize("state", ["idle", "streaming", "unread", "approval", "clarify", "generic"])
@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize("active", ["other", "parent", "child"])
def test_child_status_attach_and_render_matrix(kind, state, expanded, active):
    parent = session("parent")
    child = session("child", state, parent_session_id="parent",
                    relationship_type="child_session", raw_source="subagent",
                    session_source="fork" if kind == "fork" else "other")
    refs = [parent, child]
    raw = refs
    if kind == "reference":
        child.update(archived=True, _lineage_root_id="child")
        # Real server shape: archived reference-only ancestor of a visible child.
        descendant = session("descendant", parent_session_id="child",
                             relationship_type="child_session", raw_source="subagent")
        raw = [parent, descendant]
        refs = [parent, child, descendant]
    out = run_component(raw, refs, expanded, active)
    effective = "idle" if state == "unread" and active == "child" else state
    mark = expected_mark(state)
    chip_states = [c for c in out["chip"]["children"] if "session-child-count-state" in c["className"]]
    if mark:
        assert len(chip_states) == 1, "Collapsed child status must have a visible mark"
        assert mark in chip_states[0]["className"].split()
        assert out["chip"]["title"] != "1 children (click to expand/collapse)"
    else:
        assert not chip_states
    # Notification ownership is unchanged; collapsed child activity is separate.
    assert not out["activity"], "Single-state running children use the chip spinner"
    assert not any(c in out["parent"].split() for c in ("streaming", "unread", "needs-attention"))
    assert "is-hidden" not in out["time"]["className"]
    assert not any(c.startswith("is-") for c in out["dot"]["className"].split())
    assert out["unchanged"], "Attach/render must not acknowledge children or mutate inputs"
    assert len(out["children"]) == int(expanded)
    if expanded:
        row = out["children"][0]
        assert row["dataset"]["sid"] == ("descendant" if kind == "reference" else "child")
        assert out["opened"] == [{"sid": row["dataset"]["sid"], "options": {"skipLineageResolve": True}}]
        if kind != "reference":
            classes = row["className"].split()
            assert ("streaming" in classes) == (effective == "streaming")
            assert ("unread" in classes) == (effective == "unread")
            assert ("needs-attention" in classes) == (state in ("approval", "clarify", "generic"))
            states = [c for c in row["children"] if "session-child-session-state" in c.get("className", "")]
            assert len(states) == 1
            if expected_mark(effective):
                assert expected_mark(effective) in states[0]["className"].split()
            else:
                assert not any(c.startswith("is-") for c in states[0]["className"].split())


@pytest.mark.parametrize("own", ["idle", "unread", "streaming", "approval", "clarify"])
@pytest.mark.parametrize("states,priority", [
    (["unread", "streaming"], "streaming"),
    (["streaming", "clarify", "approval"], "approval"),
    (["approval", "clarify", "streaming"], "approval"),
    (["unread", "generic", "clarify"], "clarify"),
])
def test_mixed_children_priority_and_parent_independence(own, states, priority):
    parent = session("parent", own)
    children = [session(f"child{i}", state, parent_session_id="parent",
                        relationship_type="child_session", raw_source="subagent")
                for i, state in enumerate(states)]
    raw = [parent, *children]
    out = run_component(raw, raw, True, "other")
    assert expected_mark(priority) in out["chip"]["children"][-1]["className"].split()
    for state in ("streaming", "unread", "approval", "clarify"):
        assert (expected_mark(state) in out["dot"]["className"].split()) == (own == state)
    assert [c["dataset"]["sid"] for c in out["children"]] == [c["session_id"] for c in children]
    assert out["unchanged"]


@pytest.mark.parametrize("own", ["idle", "unread", "streaming", "approval", "clarify"])
@pytest.mark.parametrize("expanded", [False, True])
def test_concurrent_child_activity_is_owned_by_chip(own, expanded):
    parent = session("parent", own)
    children = [session("approval", "approval", parent_session_id="parent", relationship_type="child_session"),
                session("running", "streaming", parent_session_id="parent", relationship_type="child_session")]
    raw = [parent, *children]
    out = run_component(raw, raw, expanded, "other")
    activity = out["activity"]
    assert len(activity) == int(not expanded)
    if activity:
        assert activity[0] in out["chip"]["children"], "Child activity must not sit beside the parent's dot"
    marks = [c for c in out["chip"]["children"] if "session-child-count-state" in c["className"]]
    assert len(marks) == 1 and "is-attention-approval" in marks[0]["className"]
    assert "Child session is running" in out["chip"]["attributes"]["aria-label"]
    for state in ["unread", "streaming", "approval", "clarify"]:
        assert (expected_mark(state) in out["dot"]["className"].split()) == (own == state)


def test_no_children_has_no_chip():
    parent = session("parent", "approval")
    out = run_component([parent], [parent], True, "other")
    assert out["chip"] is None
    assert "is-attention-approval" in out["dot"]["className"]


def test_reference_only_running_child_has_status_even_without_visible_descendants():
    parent = session("parent")
    reference = session("child", "streaming", archived=True, parent_session_id="parent",
                        _lineage_root_id="child", relationship_type="child_session")
    out = run_component([parent], [parent, reference], False, "other")
    label, state = out["chip"]["children"]
    assert label["textContent"] == "Archived child"
    assert label["className"] == "session-child-count-label"
    assert "is-streaming" in state["className"]
    assert out["chip"]["attributes"]["role"] == "img", "Reference-only status is not an empty expander"
    assert not out["children"]


@pytest.mark.parametrize("kind", ["fork", "delegated"])
def test_attention_precedes_running_and_unread_on_same_child(kind):
    parent = session("parent")
    child = session("child", "approval", parent_session_id="parent",
                    relationship_type="child_session", raw_source="subagent",
                    session_source="fork" if kind == "fork" else "other",
                    is_streaming=True, has_unread=True)
    out = run_component([parent, child], [parent, child], True, "other")
    for state in [out["chip"]["children"][-1],
                  next(c for c in out["children"][0]["children"]
                       if "session-child-session-state" in c.get("className", ""))]:
        classes = state["className"].split()
        assert "is-attention-approval" in classes
        assert "is-streaming" not in classes and "is-unread" not in classes


def test_acknowledged_child_and_removed_children_clear_chip_projection():
    parent = session("parent", _child_session_streaming=True,
                     _child_session_has_unread=True,
                     _child_session_attention={"kind": "approval", "count": 1})
    child = session("child", parent_session_id="parent", relationship_type="child_session")
    out = run_component([parent, child], [parent, child], False, "child")
    assert len(out["chip"]["children"]) == 1
    out = run_component([parent], [parent], False, "other")
    assert out["chip"] is None
