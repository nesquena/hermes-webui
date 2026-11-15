"""Issue #9011: the sidebar badge counted children the renderer drops.

With Show archived off, a ``child_session`` row whose chain ends at an archived
ancestor gets no sidebar row: ``_attachChildSessionsToSidebarRows`` suppresses
it (``hasHiddenArchivedAncestor``, #4293). ``webui_session_count`` still counted
it, so a chain ``A (archived) <- B <- C`` showed a badge of 3 over a list of 1,
and every new gateway session chained onto the archived root added one more.

These tests build the real server payload, feed its rows and reference rows
through the production partition/attachment helpers (node), and check that the
badge total equals the rows the sidebar renders (top-level plus nested), in
both Show archived states. Orphan promotion is deliberately unchanged.
"""
from __future__ import annotations

import json

import pytest

from tests.test_issue8141_subagent_bucket import (
    NODE,
    _PREAMBLE,
    SESSIONS_JS,
    _cli_parent_row,
    _payload,
    _routes,
    _run,
    _subagent_child_row,
    _webui_parent_row,
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _webui_child_row(sid, parent, **over):
    row = _webui_parent_row(sid, title=sid, parent_session_id=parent, relationship_type="child_session")
    row.update(over)
    return row


def _messaging_row(sid, **over):
    row = {
        "session_id": sid, "title": sid, "profile": "default",
        "source": "whatsapp", "raw_source": "whatsapp", "source_tag": "whatsapp",
        "session_source": "messaging", "source_label": "WhatsApp", "is_cli_session": False,
        "message_count": 38, "user_message_count": 10, "last_message_at": 200, "updated_at": 200,
    }
    row.update(over)
    return row


def _chain(root_archived=True):
    """A (archived WebUI root) <- B <- C, plus one unrelated WebUI session."""
    return [
        _webui_parent_row("A", title="A", archived=root_archived, last_message_at=90, updated_at=90),
        _webui_child_row("B", "A", last_message_at=110, updated_at=110),
        _webui_child_row("C", "B", last_message_at=120, updated_at=120),
        _webui_parent_row("other", title="other", last_message_at=130, updated_at=130),
    ]


def _messaging_chain():
    """The reported shape: a gateway chat chained onto an archived root."""
    return [
        _messaging_row("A", archived=True, last_message_at=90, updated_at=90),
        _messaging_row("B", parent_session_id="A", relationship_type="child_session",
                       last_message_at=110, updated_at=110),
        _messaging_row("C", parent_session_id="B", relationship_type="child_session",
                       last_message_at=120, updated_at=120),
        _webui_parent_row("other", title="other", last_message_at=130, updated_at=130),
    ]


_RENDER = """
global._showArchived = %(show_archived)s;
const payload = %(payload)s;
global._sessionSourceFilter = 'webui';
global.window._showCliSessions = true;
global._activeProject = NO_PROJECT_FILTER;
global._sidebarReferenceSessions = payload.sidebar_reference_sessions || [];
const part = _partitionSidebarSessionRows(payload.sessions, null);
const out = _renderSidebarRowsFromRawSessions(part.sessionsRaw,
  [...part.webuiReferenceRaw, ..._scopedSidebarReferenceRows(false, part.projectIdFor)]);
const nested = [];
const walk = (rows) => { for (const r of rows) { nested.push(r.session_id); walk(r._child_sessions || []); } };
walk(out);
process.stdout.write(JSON.stringify({ top: out.map(r => r.session_id), rendered: nested }));
"""


def _render(payload: dict, show_archived: bool) -> dict:
    return _run(_RENDER % {"show_archived": "true" if show_archived else "false",
                           "payload": json.dumps(payload)})


# ── the reported symptom ──────────────────────────────────────────────────


@pytest.mark.parametrize("rows_factory,show_previous", [(_chain, False), (_messaging_chain, True)])
def test_badge_matches_rendered_rows_with_show_archived_off(monkeypatch, rows_factory, show_previous):
    routes = _routes()
    payload = _payload(monkeypatch, routes, rows_factory(), show_cli_sessions=False,
                       show_previous_messaging_sessions=show_previous)

    rendered = _render(payload, show_archived=False)

    assert rendered["rendered"] == ["other"], rendered
    assert payload["webui_session_count"] == len(rendered["rendered"]) == 1, payload["webui_session_count"]
    # The children are still in the payload; only the count changes.
    assert {"B", "C"} <= {r["session_id"] for r in payload["sessions"]}
    assert [r["session_id"] for r in payload["sidebar_reference_sessions"]] == ["A"]


@pytest.mark.parametrize("rows_factory,show_previous", [(_chain, False), (_messaging_chain, True)])
def test_badge_matches_rendered_rows_with_show_archived_on(monkeypatch, rows_factory, show_previous):
    routes = _routes()
    payload = _payload(monkeypatch, routes, rows_factory(), show_cli_sessions=False,
                       show_previous_messaging_sessions=show_previous, include_archived=True)

    rendered = _render(payload, show_archived=True)

    # A renders with B and C nested under it, and the badge counts all four rows.
    assert sorted(rendered["top"]) == ["A", "other"], rendered
    assert sorted(rendered["rendered"]) == ["A", "B", "C", "other"], rendered
    assert payload["webui_session_count"] == 4, payload["webui_session_count"]


def test_unarchived_root_keeps_its_children_counted(monkeypatch):
    routes = _routes()
    payload = _payload(monkeypatch, routes, _chain(root_archived=False), show_cli_sessions=False)

    rendered = _render(payload, show_archived=False)

    assert sorted(rendered["rendered"]) == ["A", "B", "C", "other"], rendered
    assert payload["webui_session_count"] == 4


# ── regression guards ─────────────────────────────────────────────────────


def test_cli_parent_subagent_child_still_buckets_cli_and_counts_once(monkeypatch):
    """#8141: a delegated child of a CLI parent counts under CLI, once."""
    routes = _routes()
    payload = _payload(
        monkeypatch, routes,
        [_cli_parent_row(), _subagent_child_row("cli_sub1", "cli_parent"), _webui_parent_row()],
        show_cli_sessions=True,
    )
    assert payload["webui_session_count"] == 1, payload
    assert payload["cli_session_count"] == 2, payload


def test_child_of_a_visible_parent_is_still_counted(monkeypatch):
    routes = _routes()
    payload = _payload(
        monkeypatch, routes,
        [_webui_parent_row("p"), _webui_child_row("kid", "p")],
        show_cli_sessions=False,
    )
    rendered = _render(payload, show_archived=False)
    assert sorted(rendered["rendered"]) == ["kid", "p"], rendered
    assert payload["webui_session_count"] == 2


# ── the server helper ─────────────────────────────────────────────────────


def _by_id(rows):
    return {r["session_id"]: r for r in rows}


def test_helper_walks_through_child_rows_to_the_archived_root():
    routes = _routes()
    rows = _chain()
    by_id = _by_id(rows)
    hidden = {r["session_id"] for r in rows if routes._sidebar_child_hidden_by_archived_ancestor(r, by_id)}
    assert hidden == {"B", "C"}


def test_helper_stops_at_a_visible_top_level_parent():
    routes = _routes()
    rows = [
        _webui_parent_row("A", archived=True),
        _webui_parent_row("P", parent_session_id="A"),  # top-level row (no child_session link)
        _webui_child_row("kid", "P"),
    ]
    assert not routes._sidebar_child_hidden_by_archived_ancestor(rows[2], _by_id(rows))


def test_helper_keeps_missing_or_cyclic_parents_counted():
    routes = _routes()
    missing = _webui_child_row("orphan", "gone")
    assert not routes._sidebar_child_hidden_by_archived_ancestor(missing, {"orphan": missing})

    x = _webui_child_row("x", "y")
    y = _webui_child_row("y", "x")
    assert not routes._sidebar_child_hidden_by_archived_ancestor(x, {"x": x, "y": y})


def test_helper_ignores_archived_rows_and_non_children():
    routes = _routes()
    rows = _chain()
    by_id = _by_id(rows)
    assert not routes._sidebar_child_hidden_by_archived_ancestor(by_id["A"], by_id)
    assert not routes._sidebar_child_hidden_by_archived_ancestor(by_id["other"], by_id)
    archived_child = _webui_child_row("Z", "A", archived=True)
    assert not routes._sidebar_child_hidden_by_archived_ancestor(archived_child, {**by_id, "Z": archived_child})


def test_server_predicate_mirrors_the_browser_child_check():
    assert "return !!(s&&s.parent_session_id&&s.relationship_type==='child_session');" in SESSIONS_JS
    assert "_PREAMBLE" and _PREAMBLE  # shared harness imported from the #8141 tests
