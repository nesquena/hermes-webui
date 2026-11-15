"""Issue #9011: the sidebar badge counted children the renderer drops.

With Show archived off, ``_attachChildSessionsToSidebarRows`` drops a child that
no visible row holds when its exact parent chain reaches an archived ancestor
(``hasHiddenArchivedAncestor``, #4293). ``webui_session_count`` still counted
it, so a chain ``A (archived) <- B <- C`` showed a badge of 3 over a list of 1.

The server now decides which rows that drop removes with the browser's own
rules (``_sidebar_rows_dropped_by_archived_ancestors``): per tab and profile, a
visible holder first (resolved parent, compression segments, lineage tip, root
and key), then the exact-id chain through the tab's rendered and archived
reference rows. Compression-lineage aliases never make an archived row an
ancestor the browser does not reach.

The tests compose the whole path: rows (seeded, or projected from a real
``state.db`` in an isolated process) -> ``_build_session_list_cache_payload``
for each tab -> the serialized response -> the browser's real partition and
render entry points under node, and check that each tab's badge equals the rows
that tab renders (top-level, nested and collapsed segments), with Show archived
off and on. Orphan promotion (#5305) is deliberately unchanged.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_issue8141_subagent_bucket import (
    NODE,
    _cli_parent_row,
    _payload,
    _routes,
    _subagent_child_row,
    _webui_parent_row,
)
from tests.test_issue8762_desktop_sidebar_rows import _EXTRACT_FUNC

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = REPO / "static" / "sessions.js"

needs_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# ── the browser: real partition + render entry points under node ──────────

_SIDEBAR = _EXTRACT_FUNC + r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
// Load the real partition/render entry points plus every top-level helper they reach.
const topLevel = new Set([...src.matchAll(/^function\s+([A-Za-z_$][\w$]*)\s*\(/gm)].map(m => m[1]));
const stubbed = new Set(['_hasUnreadForSession']);
const loaded = new Set();
const queue = ['_partitionSidebarSessionRows', '_renderSidebarRowsFromPartition', '_isCliSession'];
while (queue.length) {
  const name = queue.pop();
  if (loaded.has(name) || stubbed.has(name)) continue;
  const body = extractFunc(src, name);
  eval(body.replace('function ' + name, 'global.' + name + ' = function'));
  loaded.add(name);
  for (const m of body.matchAll(/[A-Za-z_$][\w$]*/g)) if (topLevel.has(m[0]) && !loaded.has(m[0])) queue.push(m[0]);
}
eval(src.match(/const _MESSAGING_RAW_SOURCES = [^;]*;/)[0].replace('const ', 'global.'));
global._hasUnreadForSession = s => !!(s && s.has_unread);
Object.assign(global, {
  INFLIGHT: {}, NO_PROJECT_FILTER: '__no_project__', window: {_showCliSessions: true},
  _archivedCliCount: 0, _archivedWebuiCount: 0, _serverWebuiSessionCount: null, _serverCliSessionCount: null,
  S: {session: null, busy: false, activeStreamId: null, activeProfile: 'default'},
  _activeProject: null, _showArchived: !!input.showArchived,
});
global.$ = () => ({value: ''});
const key = r => (r.profile || 'default') + ':' + r.session_id;
const out = {};
for (const tab of ['webui', 'cli']) {
  const response = input.tabs[tab];
  global._sessionSourceFilter = tab;
  global._allSessions = response.sessions;
  global._sidebarReferenceSessions = response.sidebar_reference_sessions || [];
  const rendered = {};
  const visit = (row, parent) => {
    if (key(row) in rendered) throw new Error('rendered twice: ' + key(row));
    rendered[key(row)] = parent;
    for (const seg of row._lineage_segments || []) {
      if (key(seg) !== key(row) && !(key(seg) in rendered)) rendered[key(seg)] = 'segment:' + key(row);
    }
    for (const child of row._child_sessions || []) visit(child, key(row));
  };
  for (const row of _renderSidebarRowsFromPartition(_partitionSidebarSessionRows(response.sessions, null))) {
    visit(row, row._orphan_child_session ? 'orphan' : 'top');
  }
  out[tab] = rendered;
}
console.log(JSON.stringify(out));
"""


def _render_tabs(tabs: dict, show_archived: bool) -> dict:
    proc = subprocess.run(
        [NODE, "-e", _SIDEBAR, str(SESSIONS_JS)],
        input=json.dumps({"tabs": tabs, "showArchived": show_archived}),
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_badges_match(tabs: dict, rendered: dict) -> None:
    """Each tab's badge equals the rows that tab renders."""
    for tab, count_key in (("webui", "webui_session_count"), ("cli", "cli_session_count")):
        badge = tabs[tab][count_key]
        assert badge == len(rendered[tab]), (tab, badge, rendered[tab])


# ── seeded rows through the real payload builder ──────────────────────────


def _seeded_tabs(monkeypatch, rows, **over) -> dict:
    routes = _routes()
    tabs = {}
    for tab in ("webui", "cli"):
        payload = _payload(monkeypatch, routes, rows, sidebar_source=tab, **over)
        tabs[tab] = routes._session_list_payload_to_response(payload)
    return tabs


def _seeded_render(monkeypatch, rows, show_archived=False, **over):
    tabs = _seeded_tabs(monkeypatch, rows, include_archived=show_archived, **over)
    rendered = _render_tabs(tabs, show_archived)
    _assert_badges_match(tabs, rendered)
    return tabs, rendered


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


@needs_node
@pytest.mark.parametrize("rows_factory,show_previous", [(_chain, False), (_messaging_chain, True)])
def test_reported_chain_is_neither_rendered_nor_counted(monkeypatch, rows_factory, show_previous):
    tabs, rendered = _seeded_render(monkeypatch, rows_factory(), show_cli_sessions=False,
                                    show_previous_messaging_sessions=show_previous)

    assert rendered["webui"] == {"default:other": "top"}, rendered
    assert tabs["webui"]["webui_session_count"] == 1
    # The children are still in the payload; only the count changes.
    assert {"B", "C"} <= {r["session_id"] for r in tabs["webui"]["sessions"]}
    assert [r["session_id"] for r in tabs["webui"]["sidebar_reference_sessions"]] == ["A"]


@needs_node
@pytest.mark.parametrize("rows_factory,show_previous", [(_chain, False), (_messaging_chain, True)])
def test_show_archived_on_counts_and_renders_the_whole_chain(monkeypatch, rows_factory, show_previous):
    tabs, rendered = _seeded_render(monkeypatch, rows_factory(), show_archived=True, show_cli_sessions=False,
                                    show_previous_messaging_sessions=show_previous)

    # Nested children attach under the top-level row that holds the chain.
    assert rendered["webui"]["default:B"] == "default:A", rendered
    assert rendered["webui"]["default:C"] == "default:A", rendered
    assert tabs["webui"]["webui_session_count"] == 4


@needs_node
def test_unarchived_root_keeps_its_children_counted(monkeypatch):
    tabs, rendered = _seeded_render(monkeypatch, _chain(root_archived=False), show_cli_sessions=False)
    assert len(rendered["webui"]) == tabs["webui"]["webui_session_count"] == 4


@needs_node
def test_cli_parent_subagent_child_still_buckets_cli_and_counts_once(monkeypatch):
    """#8141: a delegated child of a CLI parent counts under CLI, once."""
    tabs, rendered = _seeded_render(
        monkeypatch, [_cli_parent_row(), _subagent_child_row("cli_sub1", "cli_parent"), _webui_parent_row()],
        show_cli_sessions=True,
    )
    assert tabs["webui"]["webui_session_count"] == 1
    assert tabs["cli"]["cli_session_count"] == 2
    assert rendered["cli"]["default:cli_sub1"] == "default:cli_parent", rendered


@needs_node
def test_child_of_a_visible_parent_is_still_counted(monkeypatch):
    tabs, rendered = _seeded_render(
        monkeypatch, [_webui_parent_row("p"), _webui_child_row("kid", "p")], show_cli_sessions=False,
    )
    assert rendered["webui"] == {"default:p": "top", "default:kid": "default:p"}, rendered


# ── holders the browser finds that an alias-based walk misses ─────────────


@needs_node
def test_an_archived_reference_child_attached_to_a_visible_row_holds_its_own_child(monkeypatch):
    """P <- Z (archived child) <- kid: Z is sent as a reference, attaches under P, and holds kid."""
    rows = [
        _webui_parent_row("P", last_message_at=100, updated_at=100),
        _webui_child_row("Z", "P", archived=True, last_message_at=110, updated_at=110),
        _webui_child_row("kid", "Z", last_message_at=120, updated_at=120),
    ]
    tabs, rendered = _seeded_render(monkeypatch, rows, show_cli_sessions=False)
    assert rendered["webui"] == {"default:P": "top", "default:kid": "default:P"}, rendered
    assert tabs["webui"]["webui_session_count"] == 2


@needs_node
def test_a_visible_top_level_row_holds_a_child_by_its_lineage_key(monkeypatch):
    """A visible non-child row whose parent is archived A keys its lineage by A, so a child of A nests there."""
    rows = [
        _webui_parent_row("A", archived=True, last_message_at=90, updated_at=90),
        _webui_parent_row("P", parent_session_id="A", last_message_at=100, updated_at=100),
        _webui_child_row("kid", "A", last_message_at=120, updated_at=120),
    ]
    tabs, rendered = _seeded_render(monkeypatch, rows, show_cli_sessions=False)
    assert rendered["webui"] == {"default:P": "top", "default:kid": "default:P"}, rendered


@needs_node
def test_a_visible_lineage_tip_holds_a_child_of_its_archived_old_segment(monkeypatch):
    """The archived pre-compression row is a reference; the child nests under the visible tip."""
    rows = [
        _webui_parent_row("old", archived=True, pre_compression_snapshot=True, last_message_at=90, updated_at=90),
        _webui_parent_row("tip", parent_session_id="old", _lineage_root_id="old", _lineage_tip_id="tip",
                          last_message_at=130, updated_at=130),
        _webui_child_row("kid", "old", _parent_lineage_root_id="old", last_message_at=120, updated_at=120),
    ]
    tabs, rendered = _seeded_render(monkeypatch, rows, show_cli_sessions=False)
    assert rendered["webui"] == {"default:tip": "top", "default:kid": "default:tip"}, rendered
    assert tabs["webui"]["webui_session_count"] == 2


@needs_node
def test_an_archived_tip_reached_only_through_a_lineage_alias_does_not_hide_the_child(monkeypatch):
    """old is represented by archived tip; a child naming old gets no reference row and renders as an orphan."""
    rows = [
        _webui_parent_row("tip", archived=True, parent_session_id="old", _lineage_root_id="old",
                          _lineage_tip_id="tip", last_message_at=130, updated_at=130),
        _webui_child_row("kid", "old", _parent_lineage_root_id="old", last_message_at=120, updated_at=120),
    ]
    tabs, rendered = _seeded_render(monkeypatch, rows, show_cli_sessions=False)
    assert rendered["webui"] == {"default:kid": "orphan"}, rendered
    assert tabs["webui"]["webui_session_count"] == 1


@needs_node
def test_a_fork_of_an_archived_session_is_neither_rendered_nor_counted(monkeypatch):
    rows = [
        _webui_parent_row("A", archived=True, last_message_at=90, updated_at=90),
        _webui_parent_row("F", session_source="fork", parent_session_id="A", last_message_at=120, updated_at=120),
        _webui_parent_row("other", last_message_at=130, updated_at=130),
    ]
    tabs, rendered = _seeded_render(monkeypatch, rows, show_cli_sessions=False)
    assert rendered["webui"] == {"default:other": "top"}, rendered


@needs_node
def test_each_profile_decides_its_own_rows_with_all_profiles_on(monkeypatch):
    """Two profiles share ids: alpha's root is archived, beta's is not."""
    rows = [
        _webui_parent_row("A", profile="alpha", archived=True, last_message_at=90, updated_at=90),
        _webui_child_row("kid", "A", profile="alpha", last_message_at=110, updated_at=110),
        _webui_parent_row("A", profile="beta", last_message_at=95, updated_at=95),
        _webui_child_row("kid", "A", profile="beta", last_message_at=115, updated_at=115),
        _webui_parent_row("B", profile="alpha", last_message_at=100, updated_at=100),
        _webui_parent_row("Z", profile="alpha", archived=True, last_message_at=80, updated_at=80),
        _webui_child_row("kid2", "Z", profile="alpha", last_message_at=120, updated_at=120),
    ]
    tabs, rendered = _seeded_render(monkeypatch, rows, show_cli_sessions=False, all_profiles=True)
    assert rendered["webui"]["beta:kid"] == "beta:A", rendered
    assert "alpha:kid2" not in rendered["webui"], rendered
    # The reference rows are chosen profile-blind (an existing limitation): beta's
    # visible A stops the walk, alpha's archived A is never sent, and alpha's kid
    # renders as an orphan. The badge follows what renders.
    assert rendered["webui"]["alpha:kid"] == "orphan", rendered


# ── real state.db projection in an isolated process ───────────────────────

_PROBE = r'''
import json, sqlite3, sys
from pathlib import Path
from api import models, routes

scenario, source, home = sys.argv[1], sys.argv[2], Path(sys.argv[3])
db = sqlite3.connect(home / "state.db")
db.executescript("""CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT,
  started_at REAL, message_count INTEGER, parent_session_id TEXT, model_config TEXT, end_reason TEXT,
  ended_at REAL, archived INTEGER DEFAULT 0);
CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);""")
T = 1800000000.0


def add(sid, start, parent=None, end_reason=None, ended=None, archived=0):
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (sid, source, "Title " + sid, "m", T + start, 2, parent, None, end_reason,
                T + ended if ended else None, archived))
    for i, role in enumerate(("user", "assistant")):
        db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)",
                   (sid, role, "x", T + start + i))


add("other", 300)
if scenario == "alias":
    # old ends by compression and is represented by tip; tip is archived. kid ran
    # concurrently with old and still names it.
    add("old", 100, end_reason="compression", ended=200)
    add("tip", 210, parent="old", archived=1)
    add("kid", 150, parent="old")
elif scenario == "visible-tip":
    # The same lineage with the tip visible and the pre-compression segment kept
    # as an archived WebUI snapshot, which the payload sends as a reference row.
    add("old", 100, end_reason="compression", ended=200)
    add("tip", 210, parent="old")
    add("kid", 150, parent="old")
elif scenario == "chain":
    add("A", 100, archived=1)
    add("B", 110, parent="A")
    add("C", 120, parent="B")
elif scenario == "native":
    # Native WebUI conversations: state.db rows plus WebUI sidecars. A sidecar
    # keeps parent_session_id but not relationship_type, which only the lineage
    # enrichment adds from state.db; A is archived in the WebUI.
    add("A", 100)
    add("B", 110, parent="A")
    add("C", 120, parent="B")
db.commit()
db.close()
if scenario == "native":
    for sid, start, parent in (("A", 100, None), ("B", 110, "A"), ("C", 120, "B"), ("other", 300, None)):
        ts = T + start
        models.Session(sid, title="Title " + sid, workspace=str(home), profile="default",
                       parent_session_id=parent, archived=sid == "A",
                       messages=[{"role": "user", "content": "x", "timestamp": ts},
                                 {"role": "assistant", "content": "x", "timestamp": ts + 1}],
                       created_at=ts, updated_at=ts, session_source="webui").save(touch_updated_at=False)
if scenario == "visible-tip":
    ts = T + 100
    models.Session("old", title="Old snapshot", workspace=str(home), profile="default",
                   pre_compression_snapshot=True, archived=True,
                   messages=[{"role": "user", "content": "x", "timestamp": ts}] * 3,
                   created_at=ts, updated_at=ts, session_source="webui").save(touch_updated_at=False)
models.clear_cli_sessions_cache()
tabs = {}
for tab in ("webui", "cli"):
    tabs[tab] = routes._session_list_payload_to_response(routes._build_session_list_cache_payload(
        active_profile="default", all_profiles=False, show_cli_sessions=scenario != "native",
        include_archived=False,
        show_previous_messaging_sessions=True, show_cron_sessions=False, show_claude_code_sessions=False,
        sidebar_source=tab,
    ))
print(json.dumps(tabs, default=str))
'''


def _projected_tabs(scenario: str, source: str, tmp_path: Path) -> dict:
    home = tmp_path / "home"
    state = tmp_path / "webui"
    (state / "sessions").mkdir(parents=True)
    home.mkdir()
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "HERMES_HOME": str(home),
        "HERMES_BASE_HOME": str(home),
        "HERMES_WEBUI_STATE_DIR": str(state),
        "HERMES_WEBUI_ISOLATED_PROFILE": "0",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
        "PYTHONPATH": str(REPO),
    })
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, scenario, source, str(home)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _tab_of(source: str) -> str:
    return "cli" if source == "cli" else "webui"


@needs_node
@pytest.mark.parametrize("source", ["cli", "whatsapp"])
def test_projection_child_of_an_archived_alias_tip_renders_and_counts(source, tmp_path):
    tabs = _projected_tabs("alias", source, tmp_path)
    rendered = _render_tabs(tabs, show_archived=False)
    _assert_badges_match(tabs, rendered)
    assert rendered[_tab_of(source)] == {"default:other": "top", "default:kid": "orphan"}, rendered


@needs_node
@pytest.mark.parametrize("source", ["cli", "whatsapp"])
def test_projection_visible_tip_holds_the_child_of_its_archived_old_segment(source, tmp_path):
    tabs = _projected_tabs("visible-tip", source, tmp_path)
    rendered = _render_tabs(tabs, show_archived=False)
    _assert_badges_match(tabs, rendered)
    assert rendered[_tab_of(source)]["default:kid"] == "default:tip", rendered


@needs_node
@pytest.mark.parametrize("source", ["cli", "whatsapp"])
def test_projection_chain_under_an_archived_root_is_neither_rendered_nor_counted(source, tmp_path):
    tabs = _projected_tabs("chain", source, tmp_path)
    rendered = _render_tabs(tabs, show_archived=False)
    _assert_badges_match(tabs, rendered)
    assert rendered[_tab_of(source)] == {"default:other": "top"}, rendered


# ── the server helper ─────────────────────────────────────────────────────


class _CountingRow(dict):
    """A row that counts field reads, to bound the work of one decision pass."""

    reads = 0

    def get(self, *args):
        type(self).reads += 1
        return super().get(*args)


def _long_chain(n, root):
    rows = [_CountingRow(r) for r in root]
    parent = rows[0]["session_id"] if rows else "gone"
    for i in range(n):
        rows.append(_CountingRow(_webui_child_row(f"c{i}", parent)))
        parent = f"c{i}"
    return rows


@pytest.mark.parametrize("root,expected", [
    ([_webui_parent_row("root", archived=True)], True),
    ([], False),  # the root is missing: every child renders as an orphan
])
def test_one_pass_stays_linear_on_a_long_chain(root, expected):
    routes = _routes()
    n = 400
    rows = _long_chain(n, root)
    visible = [r for r in rows if not r.get("archived")]
    references = routes._hidden_archived_sidebar_reference_sessions(visible, [r for r in rows if r.get("archived")])
    _CountingRow.reads = 0
    dropped = routes._sidebar_rows_dropped_by_archived_ancestors(visible, references)

    assert (len(dropped) == n) is expected and (len(dropped) == 0) is not expected
    # One walk per chain, not one per child: a quadratic walk reads ~n²/2 rows.
    assert _CountingRow.reads < 100 * n, _CountingRow.reads


def test_server_helpers_mirror_the_browser_predicates():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    assert "return !!(s&&s.parent_session_id&&s.relationship_type==='child_session');" in src
    assert "return !!(s&&s.session_source==='fork'&&s.parent_session_id&&sessionIdsInList&&sessionIdsInList.has(s.parent_session_id));" in src
    assert "if(!parentRow&&hasHiddenArchivedAncestor(child)){" in src
    assert ("const _MESSAGING_RAW_SOURCES = new Set(['weixin', 'telegram', 'discord', 'slack', 'email', 'wecom', "
            "'wecom_callback', 'matrix', 'signal', 'feishu', 'dingtalk']);") in src


@needs_node
def test_projection_native_webui_chain_counts_after_the_lineage_enrichment(tmp_path):
    """Greptile on #9089: native sidecars learn they are children only from the enrichment, which ran after the count."""
    tabs = _projected_tabs("native", "webui", tmp_path)
    rendered = _render_tabs(tabs, show_archived=False)
    _assert_badges_match(tabs, rendered)
    assert rendered["webui"] == {"default:other": "top"}, rendered
    assert tabs["webui"]["webui_session_count"] == 1
    # The CLI tab's payload enriches only its own rows (#4766), yet it carries the WebUI badge too.
    assert tabs["cli"]["webui_session_count"] == 1


def test_other_tab_lineage_read_uses_the_same_window_as_that_tabs_enrichment(monkeypatch):
    """Greptile on #9089: the top-N window covers the whole tab, other profiles included, before the owner check."""
    import api.agent_sessions as agent_sessions
    from api import profiles

    routes = _routes()
    monkeypatch.setenv("HERMES_WEBUI_LINEAGE_TOP_N", "2")
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    asked: list[set] = []

    def fake_read(_path, ids):
        asked.append(set(ids))
        return {}

    monkeypatch.setattr(agent_sessions, "read_session_lineage_metadata", fake_read)
    rows = [
        _webui_child_row("beta-new-1", "x", profile="beta"),
        _webui_child_row("beta-new-2", "x", profile="beta"),
        _webui_child_row("own-old", "A"),  # third in the tab: outside its own request's window
        _webui_child_row("own-new", "A"),
    ]
    rows = [rows[3], *rows[:3]]  # own-new is the newest, then beta's two, then own-old

    routes._sidebar_rows_with_count_lineage(rows, set(), lambda r: False)

    assert asked == [{"own-new"}], asked
