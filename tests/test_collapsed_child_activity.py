"""Real attachment/full renderer: child activity is not a parent notification."""
import pytest

from tests.test_child_session_status import run_component, session


@pytest.mark.parametrize("kind", ["fork", "delegated", "reference"])
@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize("active", ["parent", "other"])
@pytest.mark.parametrize("own", ["idle", "unread", "approval", "clarify", "streaming"])
def test_collapsed_child_activity_preserves_own_notification(kind, expanded, active, own):
    parent = session("parent", own)
    child = session("child", "streaming", parent_session_id="parent",
                    relationship_type="child_session", raw_source="subagent",
                    session_source="fork" if kind == "fork" else "other")
    raw, refs = [parent, child], [parent, child]
    if kind == "reference":
        child.update(archived=True, _lineage_root_id="child")
        raw = [parent]
    out = run_component(raw, refs, expanded, active)
    assert not out["activity"], "Running-only children already have a chip spinner"
    assert "is-streaming" in out["chip"]["children"][-1]["className"].split()
    dot = out["dot"]["className"].split()
    assert ("is-streaming" in dot) == (own == "streaming")
    assert ("is-unread" in dot) == (own == "unread" and active != "parent")
    for state in ["approval", "clarify"]:
        assert (f"is-attention-{state}" in dot) == (own == state)
    assert ("unread" in out["parent"].split()) == (own == "unread" and active != "parent")
    assert ("needs-attention" in out["parent"].split()) == (own in ["approval", "clarify"])
    assert len(out["children"]) == int(expanded and kind != "reference")
    assert out["unchanged"]


def test_search_expansion_uses_child_rows_instead_of_parent_activity():
    parent = session("parent")
    child = session("child", "streaming", parent_session_id="parent", relationship_type="child_session")
    out = run_component([parent, child], [parent, child], False, "other", search="task")
    assert not out["activity"]
    assert len(out["children"]) == 1
    assert "streaming" in out["children"][0]["className"].split()


@pytest.mark.parametrize("attention", ["approval", "clarify", "generic"])
def test_other_child_attention_does_not_mask_collapsed_activity(attention):
    parent = session("parent", "unread")
    children = [session("running", "streaming", parent_session_id="parent", relationship_type="child_session"),
                session("waiting", attention, parent_session_id="parent", relationship_type="child_session")]
    out = run_component([parent, *children], [parent, *children], False, "other")
    assert len(out["activity"]) == 1
    marks = [c for c in out["chip"]["children"] if "session-child-count-state" in c["className"]]
    assert len(marks) == 1 and f"is-attention-{attention}" in marks[0]["className"].split()
    assert out["activity"][0] in out["chip"]["children"]
    assert "is-unread" in out["dot"]["className"].split()


@pytest.mark.parametrize("kind", ["fork", "delegated", "reference"])
@pytest.mark.parametrize("expanded", [False, True])
def test_running_chip_retains_concurrent_child_unread_in_accessible_name(kind, expanded):
    parent = session("parent", "unread")
    children = [session("running", "streaming", parent_session_id="parent", relationship_type="child_session"),
                session("unread", "unread", parent_session_id="parent", relationship_type="child_session",
                        session_source="fork" if kind == "fork" else "other")]
    if kind == "reference":
        for child in children:
            child.update(archived=True, _lineage_root_id=child["session_id"])
    raw = [parent] if kind == "reference" else [parent, *children]
    out = run_component(raw, [parent, *children], expanded, "other")
    assert not out["activity"]
    assert "is-streaming" in out["chip"]["children"][-1]["className"].split()
    assert "Child session is running" in out["chip"]["title"]
    assert "Unread child completion" in out["chip"]["attributes"]["aria-label"]
    assert "is-unread" in out["dot"]["className"].split()
    assert out["unchanged"]


@pytest.mark.parametrize("state", ["idle", "unread", "approval", "clarify", "generic"])
def test_settled_child_clears_activity_without_bubbling_notifications(state):
    parent = session("parent", _child_session_streaming=True)
    child = session("child", state, parent_session_id="parent", relationship_type="child_session")
    out = run_component([parent, child], [parent, child], False, "other")
    assert not out["activity"]
    assert not any(c.startswith("is-") for c in out["dot"]["className"].split())
    assert not any(c in out["parent"].split() for c in ["unread", "needs-attention", "streaming"])
