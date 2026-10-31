"""Issue #8141: parent-owned delegated children must follow their parent's bucket.

A delegated ``delegate_task`` subagent child is recorded in Hermes state.db with
``source='subagent'`` and a ``child_session`` link to its parent. The sidebar
partitioned rows by the row's OWN source, so a subagent child of a CLI parent was
counted under ``webui_session_count`` and sent in the WebUI payload, while the
renderer suppressed it (its CLI parent lives in the other bucket) — the WebUI tab
then showed 'WebUI sessions (15)' with an empty list.

The fix introduces a display-only source-bucket derivation: a parent-owned
delegated child travels with its resolved parent/lineage (same profile), applied
consistently to the server source filter + counters and the client partition +
reference-row scoping. Raw source / ``read_only`` / ownership flags are untouched.

These tests drive seeded CLI/WebUI parent payloads through the production
partition/attachment helpers (node) and the server bucket helpers (Python).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_PREAMBLE = """
const src = %s;
function extractFunc(name) {
  const start = src.search(new RegExp('function\\\\s+' + name + '\\\\s*\\\\('));
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start) + 1, depth = 1;
  while (depth > 0 && i < src.length) { if (src[i] === '{') depth++; else if (src[i] === '}') depth--; i++; }
  return src.slice(start, i);
}
function _isCliSession(s){ return !!(s && (s.is_cli_session || s.session_source==='cli')); }
function _isExternalSession(s){ return !!(s && (s.is_cli_session || s.session_source === 'messaging')); }
function _isMessagingSession(s){ return !!(s && s.session_source==='messaging'); }
function _hasUnreadForSession(s){ return !!(s && s.has_unread); }
function _sessionDisplayTitle(s){ return s && s.title; }
Object.assign(global, {_isCliSession, _isExternalSession, _isMessagingSession, _hasUnreadForSession, _sessionDisplayTitle});
global.INFLIGHT = {};
global.NO_PROJECT_FILTER = '__no_project__';
global.window = {};
global._archivedCliCount = 0; global._archivedWebuiCount = 0;
global._sidebarReferenceSessions = [];
global.S = { session: null, busy: false, activeStreamId: null };
global._showArchived = false;
global._sessionSourceFilter = 'webui';
for (const fn of ['_isSessionLocallyStreaming','_hasPendingUserMessageSignal','_isSessionEffectivelyStreaming',
  '_isChildSession','_isForkWithResolvableParent','_sessionLineageKey','_sidebarLineageKeyForRow',
  '_collapseSessionLineageForSidebar','_attachChildSessionsToSidebarRows','_sessionAttentionState',
  '_sidebarRowHasVisibleMessages','_isDelegatedSubagentRow','_sidebarProjectResolver','_sidebarRowsById','_sidebarHasUnprojectedRows',
  '_isDelegatedSubagentChild','_sidebarSameProfileForLineage','_sidebarDisplayBucketIsCli',
  '_partitionSidebarSessionRows','_scopedSidebarReferenceRows','_renderSidebarRowsFromRawSessions']) {
  eval.call(global, extractFunc(fn));
}
// Seed shapes mirror the production payload: CLI parents carry session_source
// 'cli' + is_cli_session true; delegated children carry raw_source/source_tag
// 'subagent', session_source 'other', relationship_type 'child_session' and the
// server-side _cross_surface_child_session marker (parent source != child source).
const cliParent = { session_id:'cli_parent', title:'CLI Parent', session_source:'cli', raw_source:'cli',
  source_tag:'cli', source_label:'CLI', is_cli_session:true, message_count:5, last_message_at:100, updated_at:100 };
const webuiParent = { session_id:'webui_parent', title:'WebUI Parent', session_source:'webui', raw_source:'webui',
  source_tag:'webui', source_label:'WebUI', is_cli_session:false, message_count:5, last_message_at:100, updated_at:100 };
const cliSub1 = { session_id:'cli_sub1', title:'Subagent Session', parent_session_id:'cli_parent',
  relationship_type:'child_session', raw_source:'subagent', source_tag:'subagent', session_source:'other',
  source:'subagent', is_cli_session:false, read_only:true, _cross_surface_child_session:true,
  message_count:3, last_message_at:101, updated_at:101 };
const cliSub2 = Object.assign({}, cliSub1, { session_id:'cli_sub2', last_message_at:102, updated_at:102 });
const webuiSub = Object.assign({}, cliSub1, { session_id:'webui_sub', parent_session_id:'webui_parent' });
const archivedCliParent = Object.assign({}, cliParent, { session_id:'archived_cli_parent', archived:true,
  parent_session_id:'archived_root' });
const archivedRoot = Object.assign({}, cliParent, { session_id:'archived_root', archived:true });
function renderFor(filter, rows, refs, project){
  global._sessionSourceFilter = filter;
  global.window._showCliSessions = true;
  global._activeProject = project || NO_PROJECT_FILTER;
  if (refs) global._sidebarReferenceSessions = refs; else global._sidebarReferenceSessions = [];
  const part = _partitionSidebarSessionRows(rows, null);
  const isCli = filter === 'cli';
  const out = _renderSidebarRowsFromRawSessions(part.sessionsRaw,
    [...(isCli ? part.cliReferenceRaw : part.webuiReferenceRaw),
     ..._scopedSidebarReferenceRows(isCli, part.projectIdFor)]);
  const top = out.map(r => r.session_id);
  const children = {};
  for (const r of out) children[r.session_id] = (r._child_sessions || []).map(c => c.session_id);
  return {
    cliRaw: part.cliSessionsRaw.map(s => s.session_id).sort(),
    webuiRaw: part.webuiSessionsRaw.map(s => s.session_id).sort(),
    cliCount: part.cliSessionCount,
    top, children,
    childFlags: (part.sessionsRaw.find(s => s.session_id === 'cli_sub1') || {}).read_only,
    childCliFlag: (part.sessionsRaw.find(s => s.session_id === 'cli_sub1') || {}).is_cli_session,
  };
}
"""


def _run(scenario: str) -> dict:
    assert NODE is not None
    source = (_PREAMBLE % json.dumps(SESSIONS_JS)) + scenario
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# Client-side: production partition + attachment helpers (node)
# ---------------------------------------------------------------------------


def test_cli_owned_subagent_children_travel_with_cli_parent():
    """CLI tab: CLI parent + delegated children land in the CLI bucket and attach
    underneath the parent instead of inflating the WebUI bucket."""
    out = _run("console.log(JSON.stringify(renderFor('cli', [cliParent, cliSub1, cliSub2])));")
    assert out["cliRaw"] == ["cli_parent", "cli_sub1", "cli_sub2"], out
    assert out["webuiRaw"] == [], out
    assert out["top"] == ["cli_parent"], out
    assert out["children"]["cli_parent"] == ["cli_sub1", "cli_sub2"], out


def test_webui_tab_excludes_cli_owned_children_on_mixed_payload():
    """WebUI tab (mixed payload): only WebUI-owned rows stay in the WebUI bucket."""
    out = _run("console.log(JSON.stringify(renderFor('webui', [cliParent, cliSub1, cliSub2, webuiParent, webuiSub])));")
    assert out["webuiRaw"] == ["webui_parent", "webui_sub"], out
    assert out["cliRaw"] == ["cli_parent", "cli_sub1", "cli_sub2"], out
    assert out["top"] == ["webui_parent"], out
    assert out["children"]["webui_parent"] == ["webui_sub"], out


def test_webui_owned_subagent_children_stay_webui_and_attach():
    """Regression guard: WebUI parents keep their children (unchanged behavior)."""
    out = _run("console.log(JSON.stringify(renderFor('webui', [webuiParent, webuiSub])));")
    assert out["webuiRaw"] == ["webui_parent", "webui_sub"], out
    assert out["top"] == ["webui_parent"], out
    assert out["children"]["webui_parent"] == ["webui_sub"], out


def test_read_only_and_cli_flags_untouched_on_bucketed_child():
    """The display bucket must not mutate read_only / is_cli_session ownership flags."""
    out = _run("console.log(JSON.stringify(renderFor('cli', [cliParent, cliSub1])));")
    assert out["childFlags"] is True, out  # read_only preserved
    assert out["childCliFlag"] is False, out  # never becomes a writable CLI session


def test_missing_parent_child_falls_back_to_own_bucket_and_stays_suppressed():
    """A cross-surface delegated child whose parent is absent from the payload is
    NOT promoted to a contextless top-level orphan (server and client agree on the
    fallback bucket)."""
    out = _run("console.log(JSON.stringify(renderFor('webui', [cliSub1])));")
    assert out["webuiRaw"] == ["cli_sub1"], out
    assert out["top"] == [], out


def test_archived_cli_parent_reference_resolves_child_into_cli_bucket_and_hides_it():
    """Archived parents: the child resolves its archived CLI parent (present only
    as a reference row) into the CLI bucket and stays hidden under the archived
    ancestor instead of orphan-rendering."""
    out = _run("""
global._sessionSourceFilter = 'cli';
global.window._showCliSessions = true;
global._activeProject = NO_PROJECT_FILTER;
global._sidebarReferenceSessions = [archivedRoot, archivedCliParent];
const orphanChild = Object.assign({}, cliSub1, { session_id:'archived_cli_sub',
  parent_session_id:'archived_cli_parent' });
const part = _partitionSidebarSessionRows([orphanChild], null);
const out = _renderSidebarRowsFromRawSessions(part.sessionsRaw,
  [...part.cliReferenceRaw, ..._scopedSidebarReferenceRows(true, part.projectIdFor)]);
console.log(JSON.stringify({cliRaw: part.cliSessionsRaw.map(s=>s.session_id),
  top: out.map(r=>r.session_id)}));
""")
    assert out["cliRaw"] == ["archived_cli_sub"], out
    assert out["top"] == [], out


def test_compressed_cli_lineage_root_resolves_child_bucket():
    """Parent compression/rotation: the direct parent row may be gone, but the
    lineage root (CLI) in the payload still derives the child's CLI bucket."""
    out = _run("""
const rotated = Object.assign({}, cliSub1, { session_id:'cli_sub3', parent_session_id:'rotated_parent',
  _parent_lineage_root_id:'cli_parent' });
console.log(JSON.stringify(renderFor('cli', [cliParent, rotated])));
""")
    assert out["cliRaw"] == ["cli_parent", "cli_sub3"], out
    assert out["top"] == ["cli_parent"], out
    assert out["children"]["cli_parent"] == ["cli_sub3"], out


def test_project_filter_keeps_cli_owned_children_with_parent():
    """Project filters: delegated children follow the parent's project in the CLI
    bucket; a different project filters parent and children together."""
    out = _run("""
const projParent = Object.assign({}, cliParent, { session_id:'proj_parent', project_id:'projX' });
const projChild = Object.assign({}, cliSub1, { session_id:'proj_child', parent_session_id:'proj_parent' });
const inProj = renderFor('cli', [projParent, projChild], null, 'projX');
const otherProj = renderFor('cli', [projParent, projChild], null, 'projY');
console.log(JSON.stringify({inProj, otherProj}));
""")
    assert out["inProj"]["cliRaw"] == ["proj_child", "proj_parent"], out
    assert out["inProj"]["children"]["proj_parent"] == ["proj_child"], out
    assert out["otherProj"]["cliRaw"] == [], out
    assert out["otherProj"]["top"] == [], out


def test_nested_delegated_grandchild_keeps_inherited_cli_bucket():
    """Nested delegation: CLI parent -> subagent child -> subagent grandchild.

    With the grandchild row processed before its intermediate parent (payload
    row order), the grandchild's lineage walk memo-hits the CLI parent
    mid-chain. The later ``if (cur !== s)`` branch must NOT overwrite the
    inherited CLI bucket with the intermediate subagent's raw (non-CLI)
    classification — otherwise both the grandchild and its intermediate
    parent get memoized as WebUI and drift into the wrong bucket (the CLI
    badge then loses both rows and nothing attaches under the CLI parent).
    """
    out = _run("""
const cliIntermediate = Object.assign({}, cliSub1, { session_id:'cli_intermediate' });
const cliGrandchild = Object.assign({}, cliSub1, { session_id:'cli_grandchild',
  parent_session_id:'cli_intermediate' });
console.log(JSON.stringify(renderFor('cli', [cliParent, cliGrandchild, cliIntermediate])));
""")
    assert out["cliRaw"] == ["cli_grandchild", "cli_intermediate", "cli_parent"], out
    assert out["webuiRaw"] == [], out
    assert out["top"] == ["cli_parent"], out
    # The attach helper nests depth-2 children flat under the visible top row
    # (the intermediate is the segment marker, not a second visible level).
    assert out["children"]["cli_parent"] == ["cli_intermediate", "cli_grandchild"], out


# ---------------------------------------------------------------------------
# Server-side: display-bucket helpers (Python)
# ---------------------------------------------------------------------------


def _routes():
    import api.routes as routes  # noqa: F401 (ensures repo import path is set up)
    return routes


def _row(sid, *, source, parent=None, relationship=None, raw_source=None,
         source_tag=None, session_source=None, extra=None):
    row = {"session_id": sid, "source": source, "title": sid}
    if parent is not None:
        row["parent_session_id"] = parent
    if relationship is not None:
        row["relationship_type"] = relationship
    if raw_source is not None:
        row["raw_source"] = raw_source
    if source_tag is not None:
        row["source_tag"] = source_tag
    if session_source is not None:
        row["session_source"] = session_source
    if extra:
        row.update(extra)
    return row


def test_server_helper_detects_parent_owned_delegated_child():
    routes = _routes()
    child = _row("c", source="subagent", parent="cli_parent",
                 relationship="child_session", raw_source="subagent")
    assert routes._is_delegated_subagent_child(child) is True
    # Same row without the child_session relationship is not delegated work.
    assert routes._is_delegated_subagent_child(dict(child, relationship_type=None)) is False
    # Non-subagent children are not delegated work.
    other = _row("w", source="webui", parent="cli_parent",
                 relationship="child_session", raw_source="webui")
    assert routes._is_delegated_subagent_child(other) is False
    assert routes._is_delegated_subagent_child(None) is False


def test_server_bucket_derivation_follows_resolved_parent():
    routes = _routes()
    cli_parent = _row("cli_parent", source="cli", raw_source="cli", session_source="cli")
    webui_parent = _row("webui_parent", source="webui", raw_source="webui", session_source="webui")
    cli_child = _row("cli_sub", source="subagent", parent="cli_parent",
                     relationship="child_session", raw_source="subagent",
                     extra={"read_only": True, "is_cli_session": False})
    webui_child = _row("webui_sub", source="subagent", parent="webui_parent",
                       relationship="child_session", raw_source="subagent",
                       extra={"read_only": True, "is_cli_session": False})
    by_id = {"cli_parent": cli_parent, "webui_parent": webui_parent}

    assert routes._sidebar_display_is_cli(cli_child, by_id) is True
    assert routes._sidebar_display_is_cli(webui_child, by_id) is False
    # Top-level rows classify by their own source, unchanged.
    assert routes._sidebar_display_is_cli(cli_parent, by_id) is True
    assert routes._sidebar_display_is_cli(webui_parent, by_id) is False


def test_server_bucket_derivation_falls_back_when_parent_missing():
    routes = _routes()
    child = _row("cli_sub", source="subagent", parent="gone",
                 relationship="child_session", raw_source="subagent",
                 extra={"read_only": True, "is_cli_session": False})
    # Parent absent from the same-profile payload: fall back to own classification.
    assert routes._sidebar_display_is_cli(child, {}) is False
    # The helper must never mutate the row's ownership flags.
    assert child["read_only"] is True and child["is_cli_session"] is False


def test_server_bucket_derivation_uses_lineage_root_for_rotated_parent():
    routes = _routes()
    cli_parent = _row("cli_parent", source="cli", raw_source="cli", session_source="cli")
    child = _row("cli_sub", source="subagent", parent="rotated_gone",
                 relationship="child_session", raw_source="subagent",
                 extra={"_parent_lineage_root_id": "cli_parent"})
    assert routes._sidebar_display_is_cli(child, {"cli_parent": cli_parent}) is True


def test_server_bucket_derivation_subagent_parent_stays_webui():
    routes = _routes()
    sub_parent = _row("sub_parent", source="subagent", raw_source="subagent")
    child = _row("sub_child", source="subagent", parent="sub_parent",
                 relationship="child_session", raw_source="subagent")
    assert routes._sidebar_display_is_cli(child, {"sub_parent": sub_parent}) is False


def test_server_bucket_derivation_cycle_safe():
    routes = _routes()
    a = _row("a", source="subagent", parent="b", relationship="child_session", raw_source="subagent")
    b = _row("b", source="subagent", parent="a", relationship="child_session", raw_source="subagent")
    assert routes._sidebar_display_is_cli(a, {"a": a, "b": b}) is False


def test_server_bucket_derivation_resolves_archived_parent():
    routes = _routes()
    archived_cli = _row("archived_cli", source="cli", raw_source="cli", extra={"archived": True})
    child = _row("cli_sub", source="subagent", parent="archived_cli",
                 relationship="child_session", raw_source="subagent")
    assert routes._sidebar_display_is_cli(child, {"archived_cli": archived_cli}) is True


def _lineage_db(path):
    """CLI parent + 2 subagent children, WebUI parent + 1 subagent child."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT, "
        "message_count INTEGER, started_at REAL, source TEXT, parent_session_id TEXT)"
    )
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL)")
    sessions = [
        ("cli_parent", "CLI Parent", "cli", None),
        ("cli_sub1", "Subagent Session", "subagent", "cli_parent"),
        ("cli_sub2", "Subagent Session", "subagent", "cli_parent"),
        ("webui_parent", "WebUI Parent", "webui", None),
        ("webui_sub", "Subagent Session", "subagent", "webui_parent"),
    ]
    for sid, title, source, parent in sessions:
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, source, parent_session_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, title, "gpt", 2, 1000.0, source, parent),
        )
    for sid, ts in [("cli_parent", 100.0), ("cli_sub1", 101.0), ("cli_sub2", 102.0),
                    ("webui_parent", 200.0), ("webui_sub", 201.0)]:
        conn.execute("INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)", (sid, "user", ts))
        conn.execute("INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)", (sid, "assistant", ts + 0.1))
    conn.commit()
    conn.close()


def test_server_counts_use_derived_bucket_on_production_projection(tmp_path):
    """Drive the real read_importable_agent_session_rows projection through the
    derived bucket: WebUI count must exclude CLI-owned children, CLI count must
    include parent + children (logical sidebar rows)."""
    from api.agent_sessions import read_importable_agent_session_rows

    routes = _routes()
    db = tmp_path / "state.db"
    _lineage_db(db)
    rows = read_importable_agent_session_rows(db, limit=200, exclude_sources=None)
    by_id = {str(r["id"]): r for r in rows}

    webui_count = sum(1 for r in rows if not routes._sidebar_display_is_cli(r, by_id))
    cli_count = sum(1 for r in rows if routes._sidebar_display_is_cli(r, by_id))

    assert {r["id"] for r in rows} == {"cli_parent", "cli_sub1", "cli_sub2", "webui_parent", "webui_sub"}
    assert webui_count == 2, rows  # webui_parent + webui_sub
    assert cli_count == 3, rows  # cli_parent + cli_sub1 + cli_sub2


def _cli_parent_row(sid="cli_parent", **over):
    row = {
        "session_id": sid, "title": "CLI Parent", "profile": "default",
        "source": "cli", "raw_source": "cli", "source_tag": "cli",
        "session_source": "cli", "source_label": "CLI", "is_cli_session": True,
        "message_count": 5, "user_message_count": 2, "last_message_at": 100,
        "updated_at": 100,
    }
    row.update(over)
    return row


def _subagent_child_row(sid, parent, **over):
    row = {
        "session_id": sid, "title": "Subagent Session", "profile": "default",
        "source": "subagent", "raw_source": "subagent", "source_tag": "subagent",
        "session_source": "other", "source_label": "Subagent", "is_cli_session": False,
        "read_only": True, "relationship_type": "child_session",
        "parent_session_id": parent, "parent_source": "cli",
        "_cross_surface_child_session": True,
        "message_count": 3, "user_message_count": 1, "last_message_at": 101,
        "updated_at": 101,
    }
    row.update(over)
    return row


def _webui_parent_row(sid="webui_parent", **over):
    row = {
        "session_id": sid, "title": "WebUI Parent", "profile": "default",
        "source": "webui", "raw_source": "webui", "source_tag": "webui",
        "session_source": "webui", "source_label": "WebUI", "is_cli_session": False,
        "message_count": 5, "user_message_count": 2, "last_message_at": 100,
        "updated_at": 100,
    }
    row.update(over)
    return row


def _patch_sidebar_inputs(monkeypatch, routes, webui_rows):
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None, **kw: [dict(r) for r in webui_rows])
    monkeypatch.setattr(
        routes, "_reconcile_stale_stream_state_for_session_rows", lambda _sessions: False
    )
    # Keep the payload hermetic: the seeded ids exist in state.db, and the
    # real probe would otherwise read the shared test state.db (and prune).
    monkeypatch.setattr(
        routes, "agent_session_rows_existing",
        lambda ids, profile=None: frozenset(ids or ()),
    )


def _payload(monkeypatch, routes, webui_rows, **over):
    _patch_sidebar_inputs(monkeypatch, routes, webui_rows)
    kwargs = {
        "active_profile": "default",
        "all_profiles": False,
        "show_cli_sessions": True,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
        "show_webhook_sessions": False,
        "show_kanban_sessions": False,
        "show_claude_code_sessions": False,
    }
    kwargs.update(over)
    return routes._build_session_list_cache_payload(**kwargs)


def test_cli_visibility_off_excludes_cli_owned_children(monkeypatch):
    """Reported symptom: a fresh install (CLI bucket hidden) must not count or
    list delegated subagent children under WebUI."""
    routes = _routes()
    payload = _payload(
        monkeypatch, routes,
        [_cli_parent_row(), _subagent_child_row("cli_sub1", "cli_parent"),
         _subagent_child_row("cli_sub2", "cli_parent"), _webui_parent_row()],
        show_cli_sessions=False,
    )
    ids = [r["session_id"] for r in payload["sessions"]]
    assert "cli_sub1" not in ids and "cli_sub2" not in ids, ids
    assert "webui_parent" in ids, ids
    assert payload["webui_session_count"] == 1, payload
    assert payload["cli_session_count"] == 0, payload


def test_webui_owned_children_kept_when_cli_visibility_off(monkeypatch):
    """WebUI parents keep their delegated children in the WebUI payload."""
    routes = _routes()
    payload = _payload(
        monkeypatch, routes,
        [_webui_parent_row(), _subagent_child_row("webui_sub", "webui_parent")],
        show_cli_sessions=False,
    )
    ids = [r["session_id"] for r in payload["sessions"]]
    assert sorted(ids) == ["webui_parent", "webui_sub"], ids
    assert payload["webui_session_count"] == 2, payload


def test_cli_visibility_on_buckets_cli_owned_children(monkeypatch):
    """CLI bucket visible: delegated children of a CLI parent ride the CLI
    payload and never inflate the WebUI counter."""
    routes = _routes()
    payload = _payload(
        monkeypatch, routes,
        [_cli_parent_row(), _subagent_child_row("cli_sub1", "cli_parent"),
         _webui_parent_row(), _subagent_child_row("webui_sub", "webui_parent")],
        show_cli_sessions=True,
    )
    # The unfiltered payload carries all four rows; the source-scoped filter
    # (_filter_sidebar_source) decides what each tab sends.
    webui_ids = [r["session_id"] for r in payload["sessions"]
                 if r["session_id"] in {"webui_parent", "webui_sub", "cli_parent", "cli_sub1"}]
    assert sorted(webui_ids) == ["cli_parent", "cli_sub1", "webui_parent", "webui_sub"], webui_ids
    assert payload["webui_session_count"] == 2, payload
    assert payload["cli_session_count"] == 2, payload  # parent + its child


def test_sidebar_source_filter_matches_counters_for_cli_owned_children(monkeypatch):
    """The webui sidebar_source filter must drop CLI-owned children exactly like
    the WebUI counter, and the cli filter must keep them with their parent."""
    routes = _routes()
    rows = [_cli_parent_row(), _subagent_child_row("cli_sub1", "cli_parent"),
            _webui_parent_row(), _subagent_child_row("webui_sub", "webui_parent")]
    by_id = {r["session_id"]: r for r in rows}
    webui_payload = _payload(
        monkeypatch, routes, rows, show_cli_sessions=True, sidebar_source="webui",
    )
    cli_payload = _payload(
        monkeypatch, routes, rows, show_cli_sessions=True, sidebar_source="cli",
    )
    assert sorted(r["session_id"] for r in webui_payload["sessions"]) == [
        "webui_parent", "webui_sub"
    ], webui_payload["sessions"]
    assert sorted(r["session_id"] for r in cli_payload["sessions"]) == [
        "cli_parent", "cli_sub1"
    ], cli_payload["sessions"]
    assert routes._sidebar_display_is_cli(rows[1], by_id) is True
    assert routes._sidebar_display_is_cli(rows[3], by_id) is False


def test_bucket_derivation_keeps_read_only_flag(monkeypatch):
    """The display bucket must not strip read_only from delegated children."""
    routes = _routes()
    payload = _payload(
        monkeypatch, routes,
        [_cli_parent_row(), _subagent_child_row("cli_sub1", "cli_parent")],
        show_cli_sessions=True,
    )
    child = next(r for r in payload["sessions"] if r["session_id"] == "cli_sub1")
    assert child["read_only"] is True, child
    assert child["raw_source"] == "subagent", child
    assert child["is_cli_session"] is False, child


def test_archived_cli_owned_child_counted_under_archived_cli(monkeypatch):
    """Archived parents: the child follows its archived CLI parent's bucket."""
    routes = _routes()
    payload = _payload(
        monkeypatch, routes,
        [_cli_parent_row(archived=True),
         _subagent_child_row("cli_sub1", "cli_parent", archived=True)],
        show_cli_sessions=True,
        include_archived=True,
    )
    assert payload["archived_cli_count"] == 2, payload
    assert payload["archived_webui_count"] == 0, payload


# ---------------------------------------------------------------------------
# 2026-10-19 re-gate: the archive pager's loaded count and the cross-profile
# lineage lookup.
# ---------------------------------------------------------------------------


def test_archive_pager_loaded_count_uses_the_derived_bucket() -> None:
    """The CLI tab's "Load more archived" must reach zero.

    ``archived_cli_count`` comes from the server's DERIVED bucket, so an
    archived delegated child of a CLI parent is inside that total. The loaded
    count used the raw ``_isCliSession()``, which never sees the child, so
    ``remaining`` never reached zero and the pager kept offering more rows
    after everything was loaded. The count must use the same derivation.
    """
    src = (Path(__file__).resolve().parents[1] / "static" / "sessions.js").read_text(
        encoding="utf-8"
    )
    marker = "const _archivedInBucket="
    assert marker in src, "the archive pager's bucket predicate was not found"
    block = src[src.find(marker) : src.find(marker) + 800]
    assert "_sidebarDisplayBucketIsCli" in block, (
        "the archive pager must count loaded rows with the derived display "
        "bucket, not the raw _isCliSession(), so an archived delegated child "
        "of a CLI parent is counted on both sides of `remaining`"
    )
    assert "_isCliSession(s)" not in block, (
        "the raw classifier must not be used for the loaded count"
    )
    # ...and the count itself must be driven by that predicate.
    count_at = src.find("const loadedArchivedCount=sidebarRows.filter(_archivedInBucket)")
    assert count_at > src.find(marker), (
        "the loaded count must be computed through the derived-bucket predicate"
    )


def test_lineage_lookup_refuses_a_parent_from_another_profile() -> None:
    """The All-profiles view must not inherit another profile's bucket.

    Session ids are unique per profile home and the All-profiles view merges
    several homes into one lookup map, so a bare id lookup can resolve a parent
    that belongs to a different profile. A lineage edge is only meaningful
    inside one profile: a cross-profile hit is treated as absent and the row
    keeps its own raw bucket.
    """
    src = (Path(__file__).resolve().parents[1] / "static" / "sessions.js").read_text(
        encoding="utf-8"
    )
    assert "function _sidebarSameProfileForLineage(" in src, (
        "the lineage walk needs a same-profile guard helper"
    )
    walk = src[src.find("function _sidebarDisplayBucketIsCli(") :]
    walk = walk[: walk.find("\nfunction ", 10)]
    assert "_sidebarSameProfileForLineage(cur,parent)" in walk, (
        "the parent walk must refuse a cross-profile parent"
    )
    assert "_sidebarSameProfileForLineage(s,lineageRow)" in walk, (
        "the lineage root/tip fallback must refuse a cross-profile row"
    )


# ---------------------------------------------------------------------------
# 2026-10-27 re-gate: cross-profile lineage resolution and the search case.
# ---------------------------------------------------------------------------


def _profile_lineage_db(path, *sessions):
    """One isolated Agent SessionDB (state.db) for a single profile home.

    Mirrors the schema Hermes Agent writes (``sessions`` + ``messages``) so
    the production projection (``read_importable_agent_session_rows``)
    synthesizes the same ``raw_source``/``relationship_type`` signals the
    sidebar classifier keys on. Each entry is
    ``(sid, title, source, parent, timestamp)``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT, "
        "message_count INTEGER, started_at REAL, source TEXT, parent_session_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL)"
    )
    for sid, title, source, parent, ts in sessions:
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, "
            "source, parent_session_id) VALUES (?,?,?,?,?,?,?)",
            (sid, title, "gpt", 2, 1000.0, source, parent),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)",
            (sid, "user", ts),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)",
            (sid, "assistant", ts + 0.1),
        )
    conn.commit()
    conn.close()


def test_cross_profile_child_not_classified_by_foreign_parent(monkeypatch, tmp_path):
    """With the CLI bucket hidden and All-profiles off, beta's orphan child
    (its parent lives in alpha) must stay in beta's own list.

    Reproduced the maintainer's way: two ISOLATED Agent SessionDB databases
    (one per profile home) read through the production projection
    (``read_importable_agent_session_rows``), then merged cross-profile into
    the sidebar payload — exactly the state ``_cli_off_rows_by_id`` saw
    BEFORE profile scoping. The parent-lookup map must be scoped per
    profile: alpha's shared parent resolved beta's child and inherited its
    CLI bucket, filtering it out of beta's own list entirely (the default
    view on established installs). Master lists it.
    """
    from api.agent_sessions import read_importable_agent_session_rows

    routes = _routes()
    alpha_db = tmp_path / "alpha" / "state.db"
    beta_db = tmp_path / "beta" / "state.db"
    _profile_lineage_db(
        alpha_db,
        ("alpha_shared_parent", "Alpha Parent", "cli", None, 100.0),
    )
    _profile_lineage_db(
        beta_db,
        ("beta_orphan", "Subagent Session", "subagent", "alpha_shared_parent", 101.0),
        ("beta_webui_parent", "WebUI Parent", "webui", None, 200.0),
    )
    alpha_rows = read_importable_agent_session_rows(alpha_db, limit=200, exclude_sources=None)
    beta_rows = read_importable_agent_session_rows(beta_db, limit=200, exclude_sources=None)
    assert {str(r["id"]) for r in [*alpha_rows, *beta_rows]} == {
        "alpha_shared_parent", "beta_orphan", "beta_webui_parent",
    }
    # The sidebar loader tags each row with the home it came from.
    rows = [
        {**r, "session_id": r["id"], "profile": "alpha"} for r in alpha_rows
    ] + [
        {**r, "session_id": r["id"], "profile": "beta"} for r in beta_rows
    ]
    payload = _payload(
        monkeypatch, routes, rows,
        active_profile="beta", all_profiles=False, show_cli_sessions=False,
    )
    ids = [r["session_id"] for r in payload["sessions"]]
    assert "beta_orphan" in ids, (
        f"beta's own child must not inherit alpha's parent bucket: {ids}")
    assert "alpha_shared_parent" not in ids, ids
    assert payload["webui_session_count"] == 2, payload


def test_same_profile_child_still_follows_its_cli_parent(monkeypatch):
    """Control: within one profile the derivation is unchanged — a CLI
    parent's delegated child still rides the CLI bucket out of the WebUI
    payload when the CLI tab is hidden."""
    routes = _routes()
    payload = _payload(
        monkeypatch, routes,
        [_cli_parent_row(), _subagent_child_row("cli_sub1", "cli_parent"),
         _webui_parent_row()],
        show_cli_sessions=False,
    )
    ids = [r["session_id"] for r in payload["sessions"]]
    assert "cli_sub1" not in ids, ids
    assert "webui_parent" in ids, ids


def test_client_memo_shortcut_runs_after_the_profile_check():
    """The memo short-circuit must run AFTER the same-profile check.

    The server sorts by recency, so a newer alpha parent can be memoised
    first; beta's child then hit ``memo.has(parentId)`` and inherited
    alpha's bucket. Parent-first and child-first orders must give the SAME
    (own-bucket) answer.
    """
    out = _run("""
const alphaParent = { session_id:'shared_parent', title:'Alpha Parent', profile:'alpha',
  session_source:'cli', raw_source:'cli', source_tag:'cli', is_cli_session:true,
  message_count:5, last_message_at:200, updated_at:200 };
const betaChild = { session_id:'beta_child', title:'Beta Child', profile:'beta',
  parent_session_id:'shared_parent', relationship_type:'child_session',
  raw_source:'subagent', source_tag:'subagent', session_source:'other',
  is_cli_session:false, read_only:true, _cross_surface_child_session:true,
  message_count:3, last_message_at:201, updated_at:201 };
global._activeProject = NO_PROJECT_FILTER;
function buckets(order){
  const rows = order === 'parent-first' ? [alphaParent, betaChild] : [betaChild, alphaParent];
  const part = _partitionSidebarSessionRows(rows, null);
  return { webui: part.webuiSessionsRaw.map(s=>s.session_id).sort(),
           cli: part.cliSessionsRaw.map(s=>s.session_id).sort() };
}
console.log(JSON.stringify({ parentFirst: buckets('parent-first'), childFirst: buckets('child-first') }));
""")
    # alpha's own parent is a CLI row and belongs in the CLI bucket either
    # way; what matters is that beta's child does NOT inherit it.
    assert out["parentFirst"]["webui"] == ["beta_child"], out
    assert out["parentFirst"]["cli"] == ["shared_parent"], out
    assert out["childFirst"]["webui"] == ["beta_child"], out
    assert out["childFirst"]["cli"] == ["shared_parent"], out
    assert out["parentFirst"] == out["childFirst"], (
        f"parent-first and child-first must agree: {out}")


def test_client_search_derives_bucket_from_complete_cached_rows():
    """Searching ``subagent`` matches the archived child but not its parent.

    The classifier's map held only the search matches, so the child could
    not resolve its parent and fell back to WebUI — then the WebUI fetch
    excluded it and it vanished from BOTH tabs. The bucket must be derived
    from the complete cached rows plus the sidebar's parent references.

    This scenario runs from the ARCHIVED tab (``_showArchived``), the view
    that renders archived search matches. There the server sends NO
    ``sidebar_reference_sessions`` (``_build_session_list_cache_payload``
    emits them only when ``include_archived`` is off), so the archived
    parent is reachable ONLY through the complete ``_allSessions`` cache —
    the exact hole the pre-fix map (matches + references) had.
    """
    out = _run("""
const archP = { session_id:'arch_p', title:'Archived CLI Parent', profile:'default',
  session_source:'cli', raw_source:'cli', source_tag:'cli', is_cli_session:true,
  archived:true, message_count:5, last_message_at:100, updated_at:100 };
const archC = { session_id:'arch_c', title:'Subagent Session', profile:'default',
  parent_session_id:'arch_p', relationship_type:'child_session',
  raw_source:'subagent', source_tag:'subagent', session_source:'other',
  is_cli_session:false, read_only:true, _cross_surface_child_session:true,
  archived:true, message_count:3, last_message_at:101, updated_at:101 };
// The search matches only the child; the complete cache holds both rows.
// Archived-tab payloads carry no sidebar_reference_sessions (include_archived
// skips them server-side), so the parent resolves ONLY via _allSessions.
global._activeProject = NO_PROJECT_FILTER;
global._showArchived = true;
global._allSessions = [archP, archC];
global._sidebarReferenceSessions = [];
const part = _partitionSidebarSessionRows([archC], null);
console.log(JSON.stringify({ webui: part.webuiSessionsRaw.map(s=>s.session_id),
  cli: part.cliSessionsRaw.map(s=>s.session_id), cliCount: part.cliSessionCount }));
""")
    assert out["cli"] == ["arch_c"], (
        f"the searched child must keep its parent's CLI bucket: {out}")
    assert out["webui"] == [], out
    assert out["cliCount"] == 1, out


def test_client_search_without_cached_parent_keeps_own_bucket():
    """Control: with the parent genuinely absent from every cache the child
    keeps its own raw bucket — the complete-rows derivation must not invent
    a bucket for an unresolvable lineage."""
    out = _run("""
const archC = { session_id:'arch_c', title:'Subagent Session', profile:'default',
  parent_session_id:'arch_p', relationship_type:'child_session',
  raw_source:'subagent', source_tag:'subagent', session_source:'other',
  is_cli_session:false, read_only:true, _cross_surface_child_session:true,
  message_count:3, last_message_at:101, updated_at:101 };
global._activeProject = NO_PROJECT_FILTER;
global._allSessions = [archC];
global._sidebarReferenceSessions = [];
const part = _partitionSidebarSessionRows([archC], null);
console.log(JSON.stringify({ webui: part.webuiSessionsRaw.map(s=>s.session_id),
  cli: part.cliSessionsRaw.map(s=>s.session_id) }));
""")
    assert out["webui"] == ["arch_c"], out
    assert out["cli"] == [], out
