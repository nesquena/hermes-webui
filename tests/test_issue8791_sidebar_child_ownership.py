"""#8791: delegated children render under their own parent in the sidebar.

exp-v0.52.618 (#7415) moved Desktop sessions into the CLI group and #8245 (exp-v0.52.622) made delegated children
follow their parent's group. Three children still lost their parent:

1. A delegated child the user opened has a persisted WebUI sidecar. Its ``relationship_type`` only arrives with the
   lineage enrichment that runs after the server derives the sidebar group, so the copy kept its own WebUI group while
   its Desktop/CLI parent sat in the CLI group, and it rendered in neither.
2. Compression leaves only the parent's tip in the payload, while a delegated child still names the pre-compression
   parent. The server's parent map had no alias for that id, so the child again rendered in neither group. A compressed
   delegated intermediate stranded its own child the same way.
3. With All profiles on, two profiles can hold the same parent id. The browser keyed child attachment by bare id, so
   one profile's child rendered under the other profile's parent.

The probe builds real ``state.db`` and WebUI state in isolated processes (one per fixture) and takes the server's
payloads for both groups. The sidebar step feeds them through the real partition and render path from
``static/sessions.js`` under node, and the tests assert where each row finally renders: which group, under which
parent, in which profile.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_issue8762_desktop_sidebar_rows import _EXTRACT_FUNC

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = REPO / "static" / "sessions.js"
NODE = shutil.which("node")

_PROBE = r'''
import json, sqlite3, sys
from pathlib import Path
from api import models, routes

scenario, home, workspace = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
SCHEMA = """CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL,
  message_count INTEGER, parent_session_id TEXT, model_config TEXT, end_reason TEXT, ended_at REAL);
CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);"""
clock = [1800000000.0]


def add(db, sid, source, parent=None, delegate=None, end_reason=None):
    clock[0] += 10
    cfg = json.dumps({"_delegate_from": delegate}) if delegate else None
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
               (sid, source, "Title " + sid, "m", clock[0], 2, parent, cfg, end_reason,
                clock[0] + 5 if end_reason else None))
    for role in ("user", "assistant"):
        db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)",
                   (sid, role, "x", clock[0]))
    return clock[0]


db = sqlite3.connect(home / "state.db")
db.executescript(SCHEMA)
opened = []
if scenario == "opened":
    for parent, source in (("dk", "desktop"), ("ck", "cli")):
        add(db, parent, source)
        opened.append((parent + "-kid-open", parent, add(db, parent + "-kid-open", "subagent", parent, parent)))
elif scenario == "compressed":
    add(db, "cp-old", "desktop", end_reason="compression")
    add(db, "cp-tip", "desktop", parent="cp-old")
    add(db, "cp-kid", "subagent", parent="cp-old", delegate="cp-old")
    add(db, "nest-host", "cli")
    add(db, "nest-old", "subagent", parent="nest-host", delegate="nest-host", end_reason="compression")
    add(db, "nest-tip", "subagent", parent="nest-old", delegate="nest-host")
    add(db, "nest-leaf", "subagent", parent="nest-old", delegate="nest-old")
elif scenario == "intermediate":
    # An opened child naming an intermediate segment of a twice-compressed Desktop parent.
    add(db, "root", "desktop", end_reason="compression")
    add(db, "mid", "desktop", parent="root", end_reason="compression")
    add(db, "tip", "desktop", parent="mid")
    opened.append(("kid", "mid", add(db, "kid", "subagent", parent="mid", delegate="mid")))
    # An opened, compressed delegated row, and a child that names its pre-compression id.
    add(db, "host", "cli")
    add(db, "sub-old", "subagent", parent="host", delegate="host", end_reason="compression")
    opened.append(("sub-tip", "sub-old", add(db, "sub-tip", "subagent", parent="sub-old", delegate="host")))
    add(db, "sub-leaf", "subagent", parent="sub-old", delegate="sub-old")
elif scenario == "renamed":
    # The root profile renamed to "work": its rows carry "work", an opened child's copy the legacy "default".
    from api import profiles
    profiles._root_profile_name_cache = {"default", "work"}
    profiles._root_profile_name_cache_loaded = True
    ts = add(db, "proj-host", "webui")
    models.Session("proj-host", title="Project host", workspace=workspace, profile="work", project_id="proj",
                   messages=[{"role": "user", "content": "x", "timestamp": ts}], created_at=ts, updated_at=ts,
                   session_source="webui").save(touch_updated_at=False)
    opened.append(("proj-kid", "proj-host", add(db, "proj-kid", "subagent", parent="proj-host", delegate="proj-host")))
elif scenario == "root-alias":
    # The root renamed to "work" while another profile is active and the profile roster isn't loaded: an archived
    # pre-compression snapshot tagged the legacy "default" and its continuation tagged "work" are one conversation.
    from api import profiles
    profiles._root_profile_name_cache = {"default", "work"}
    profiles._root_profile_name_cache_loaded = True
    ts = add(db, "old", "webui", end_reason="compression")
    models.Session("old", title="Old snapshot", workspace=workspace, profile="default", pre_compression_snapshot=True,
                   archived=True, messages=[{"role": "user", "content": "x", "timestamp": ts}] * 3, created_at=ts,
                   updated_at=ts, session_source="webui").save(touch_updated_at=False)
    ts = add(db, "tip", "webui", parent="old")
    models.Session("tip", title="New continuation", workspace=workspace, profile="work", parent_session_id="old",
                   messages=[{"role": "user", "content": "x", "timestamp": ts}], created_at=ts, updated_at=ts,
                   session_source="webui").save(touch_updated_at=False)
elif scenario == "copy-root":
    # An opened, compressed delegated row whose pre-compression id is also a plain session in another profile.
    add(db, "host", "cli")
    add(db, "old", "subagent", parent="host", delegate="host", end_reason="compression")
    opened.append(("tip", "old", add(db, "tip", "subagent", parent="old", delegate="host")))
    beta = home / "profiles" / "beta"
    beta.mkdir(parents=True)
    db2 = sqlite3.connect(beta / "state.db")
    db2.executescript(SCHEMA)
    add(db2, "old", "cli")
    db2.commit()
    db2.close()
elif scenario == "shared-root":
    # Two profiles whose compressed conversations share a pre-compression id but not a current one.
    add(db, "shared-root", "cli", end_reason="compression")
    add(db, "alpha-tip", "cli", parent="shared-root")
    add(db, "alpha-kid", "subagent", parent="shared-root", delegate="shared-root")
    beta = home / "profiles" / "beta"
    beta.mkdir(parents=True)
    db2 = sqlite3.connect(beta / "state.db")
    db2.executescript(SCHEMA)
    add(db2, "shared-root", "cli", end_reason="compression")
    add(db2, "beta-tip", "cli", parent="shared-root")
    add(db2, "beta-kid", "subagent", parent="shared-root", delegate="shared-root")
    db2.commit()
    db2.close()
elif scenario.startswith("twin-"):
    # With All profiles on, another profile holds an unrelated CLI session with the SAME id as an opened
    # delegated child of a twice-compressed Desktop parent. The child names the intermediate segment, so
    # its lineage ids must come from its own profile's state.db row, whichever profile's row is newer.
    beta = home / "profiles" / "beta"
    beta.mkdir(parents=True)
    db2 = sqlite3.connect(beta / "state.db")
    db2.executescript(SCHEMA)
    if scenario == "twin-older":
        add(db2, "kid", "cli")
        add(db2, "sub-tip", "cli")
    add(db, "root", "desktop", end_reason="compression")
    add(db, "mid", "desktop", parent="root", end_reason="compression")
    add(db, "tip", "desktop", parent="mid")
    opened.append(("kid", "mid", add(db, "kid", "subagent", parent="mid", delegate="mid")))
    # An opened, compressed delegated row whose id another profile also holds: its own lineage
    # ids must come from its own profile's row too, or it renders top-level instead of under its host.
    add(db, "host", "cli")
    add(db, "sub-old", "subagent", parent="host", delegate="host", end_reason="compression")
    opened.append(("sub-tip", "sub-old", add(db, "sub-tip", "subagent", parent="sub-old", delegate="host")))
    if scenario == "twin-newer":
        add(db2, "kid", "cli")
        add(db2, "sub-tip", "cli")
    db2.commit()
    db2.close()
elif scenario == "profiles":
    add(db, "shared-parent", "cli")
    add(db, "alpha-kid", "subagent", parent="shared-parent", delegate="shared-parent")
    beta = home / "profiles" / "beta"
    beta.mkdir(parents=True)
    db2 = sqlite3.connect(beta / "state.db")
    db2.executescript(SCHEMA)
    add(db2, "shared-parent", "cli")
    add(db2, "beta-kid", "subagent", parent="shared-parent", delegate="shared-parent")
    db2.commit()
    db2.close()
db.commit()
db.close()

# The sidecar a release persisted when the user opened a delegated child (#5307 guards: view-only subagent copy).
for sid, parent, ts in opened:
    models.Session(sid, title="Opened " + sid, workspace=workspace, profile="default",
                   messages=[{"role": "user", "content": "x", "timestamp": ts},
                             {"role": "assistant", "content": "x", "timestamp": ts}],
                   created_at=ts, updated_at=ts, parent_session_id=parent, session_source="other",
                   source_tag="subagent", raw_source="subagent", read_only=True,
                   is_cli_session=False).save(touch_updated_at=False)
models.clear_cli_sessions_cache()
payloads = {}
for group in ("webui", "cli"):
    payloads[group] = routes._session_list_payload_to_response(routes._build_session_list_cache_payload(
        active_profile={"renamed": "work", "root-alias": "beta"}.get(scenario, "default"),
        all_profiles=scenario in ("profiles", "shared-root", "copy-root", "root-alias") or scenario.startswith("twin-"),
        show_cli_sessions=True,
        include_archived=scenario == "root-alias",
        show_previous_messaging_sessions=False, show_cron_sessions=False, show_claude_code_sessions=False,
        sidebar_source=group,
    ))["sessions"]
if scenario == "root-alias":
    # A content search for "x" (every message is "x"), as the sidebar issues it, to merge into the list.
    from urllib.parse import urlparse
    routes.j = lambda handler, response: response
    payloads["search"] = routes._handle_sessions_search(
        None, urlparse("/api/sessions/search?q=x&content=1&all_profiles=1"))["sessions"]
print(json.dumps(payloads, default=str))
'''

_SIDEBAR = _EXTRACT_FUNC + r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const payloads = JSON.parse(require('fs').readFileSync(0, 'utf8'));
// Load the real partition/render entry points plus every top-level helper they reach.
const topLevel = new Set([...src.matchAll(/^function\s+([A-Za-z_$][\w$]*)\s*\(/gm)].map(m => m[1]));
const stubbed = new Set(['_hasUnreadForSession']);
const loaded = new Set();
const queue = ['_partitionSidebarSessionRows', '_renderSidebarRowsFromPartition', '_isCliSession', '_sessionSearchMergeMatches'];
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
  _sidebarReferenceSessions: [], S: {session: null, busy: false, activeStreamId: null, activeProfile: 'default'},
  _activeProject: null, _showArchived: false,
});
// Per-scenario UI state: the active profile, and an active project filter.
const extra = JSON.parse(process.argv[2] || '{}');
Object.assign(global.S, extra.S || {});
if ('_activeProject' in extra) global._activeProject = extra._activeProject;
if ('_showArchived' in extra) global._showArchived = extra._showArchived;
// A search query: merge the server's search rows into the list as the sidebar does.
global.$ = () => ({value: extra.search || ''});
const key = r => (r.profile || 'default') + ':' + r.session_id;
const out = {};
for (const group of ['webui', 'cli']) {
  global._sessionSourceFilter = group;
  global._allSessions = payloads[group];
  const rendered = {};
  const visit = (row, parent) => {
    if (key(row) in rendered) throw new Error('rendered twice: ' + key(row) + ' under ' + rendered[key(row)] + ' and ' + parent);
    rendered[key(row)] = parent;
    // A collapsed compression lineage shows its other segments inside the row.
    for (const seg of row._lineage_segments || []) {
      if (key(seg) !== key(row) && !(key(seg) in rendered)) rendered[key(seg)] = 'segment:' + key(row);
    }
    for (const child of row._child_sessions || []) visit(child, key(row));
  };
  const listed = extra.search ? _sessionSearchMergeMatches(payloads[group], extra.search, payloads.search) : payloads[group];
  for (const row of _renderSidebarRowsFromPartition(_partitionSidebarSessionRows(listed, null))) visit(row, null);
  out[group] = rendered;
}
console.log(JSON.stringify(out));
"""


def _payloads(scenario: str, tmp_path: Path) -> dict:
    home = tmp_path / "home"
    state = tmp_path / "webui"
    (state / "sessions").mkdir(parents=True)
    home.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "HERMES_HOME": str(home),
            "HERMES_BASE_HOME": str(home),
            "HERMES_WEBUI_STATE_DIR": str(state),
            "HERMES_WEBUI_ISOLATED_PROFILE": "0",
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, scenario, str(home), str(tmp_path)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _render(scenario: str, tmp_path: Path, ui: dict | None = None) -> dict:
    payloads = _payloads(scenario, tmp_path)
    proc = subprocess.run(
        [NODE, "-e", _SIDEBAR, str(SESSIONS_JS), json.dumps(ui or {})],
        input=json.dumps(payloads), cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    rendered = json.loads(proc.stdout.strip().splitlines()[-1])
    # Every row the server sends for a group renders in that group, and in no other.
    for group, other in (("webui", "cli"), ("cli", "webui")):
        if (ui or {}).get("search"):
            break  # a search shows matches only
        sent = {(r.get("profile") or "default") + ":" + r["session_id"] for r in payloads[group]}
        assert set(rendered[group]) == sent, (group, sorted(sent), rendered[group])
        assert not set(rendered[group]) & set(rendered[other]), (group, rendered)
    return rendered


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_an_opened_delegated_child_renders_under_its_parent(tmp_path):
    rendered = _render("opened", tmp_path)
    assert rendered["cli"].get("default:dk-kid-open") == "default:dk", rendered
    assert rendered["cli"].get("default:ck-kid-open") == "default:ck", rendered
    assert rendered["webui"] == {}, rendered


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_a_child_of_a_compressed_parent_renders_under_the_tip(tmp_path):
    rendered = _render("compressed", tmp_path)
    assert rendered["cli"].get("default:cp-kid") == "default:cp-tip", rendered
    # A compressed delegated intermediate and the child that names its pre-compression id stay with their host.
    assert rendered["cli"].get("default:nest-tip") == "default:nest-host", rendered
    assert rendered["cli"].get("default:nest-leaf") == "default:nest-host", rendered
    assert rendered["webui"] == {}, rendered


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_a_child_never_attaches_under_another_profiles_parent(tmp_path):
    rendered = _render("profiles", tmp_path)
    assert rendered["cli"].get("default:alpha-kid") == "default:shared-parent", rendered
    assert rendered["cli"].get("beta:beta-kid") == "beta:shared-parent", rendered
    assert rendered["webui"] == {}, rendered


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_an_opened_child_of_an_intermediate_segment_renders_under_the_tip(tmp_path):
    rendered = _render("intermediate", tmp_path)
    # The opened child names "mid", which only the state.db row's parent lineage links to the tip.
    assert rendered["cli"].get("default:kid") == "default:tip", rendered
    # An opened compressed delegated row, and the child naming its pre-compression id, stay with their host.
    assert rendered["cli"].get("default:sub-tip") == "default:host", rendered
    assert rendered["cli"].get("default:sub-leaf") == "default:host", rendered
    assert rendered["webui"] == {}, rendered


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("scenario", ["twin-older", "twin-newer"])
def test_an_opened_childs_lineage_comes_from_its_own_profiles_row(scenario, tmp_path):
    """Another profile's same-id session must not stand in for the opened child's state.db twin: the
    child still gets its parent's lineage ids and renders under the tip, in its own profile."""
    rendered = _render(scenario, tmp_path)
    assert rendered["cli"]["default:kid"] == "default:tip", rendered
    assert rendered["cli"]["default:sub-tip"] == "default:host", rendered


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_two_profiles_sharing_a_pre_compression_id_keep_their_own_rows(tmp_path):
    rendered = _render("shared-root", tmp_path)
    assert rendered["cli"].get("default:alpha-tip", "missing") is None, rendered
    assert rendered["cli"].get("beta:beta-tip", "missing") is None, rendered
    assert rendered["cli"].get("default:alpha-kid") == "default:alpha-tip", rendered
    assert rendered["cli"].get("beta:beta-kid") == "beta:beta-tip", rendered
    assert rendered["webui"] == {}, rendered


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_a_renamed_roots_legacy_child_stays_in_its_parents_project(tmp_path):
    # Active profile "work" is the renamed root; the opened child's copy is tagged the legacy "default".
    ui = {"S": {"activeProfile": "work", "activeProfileIsDefault": True}, "_activeProject": "proj"}
    rendered = _render("renamed", tmp_path, ui)
    assert rendered["webui"].get("default:proj-kid") == "work:proj-host", rendered


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_a_renamed_roots_legacy_snapshot_stays_in_its_conversation_before_the_roster_loads(tmp_path):
    # Active profile "beta", roster not loaded: the server's profile group keeps "default" and "work" together.
    ui = {"S": {"activeProfile": "beta", "activeProfileIsDefault": False}, "_showArchived": True}
    rendered = _render("root-alias", tmp_path, ui)
    assert rendered["webui"] == {"work:tip": None, "default:old": "segment:work:tip"}, rendered


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_a_renamed_roots_rows_stay_together_when_search_hits_merge_in(tmp_path):
    # The same conversation, found through the content search: search rows carry the server's profile group too.
    ui = {"S": {"activeProfile": "beta", "activeProfileIsDefault": False}, "_showArchived": True, "search": "x"}
    payloads = _payloads("root-alias", tmp_path)
    assert {r["session_id"]: r.get("sidebar_profile_group") for r in payloads["search"]} == {
        "tip": "default", "old": "default"}, payloads["search"]
    rendered = _render("root-alias", tmp_path / "render", ui)
    assert rendered["webui"] == {"work:tip": None, "default:old": "segment:work:tip"}, rendered


def test_an_opened_sidecars_lineage_ids_do_not_hide_another_profiles_session(tmp_path):
    # The sidecar's own pre-compression id ("old") must not dedupe away beta's unrelated session "old".
    payloads = _payloads("copy-root", tmp_path)
    sent = {(row.get("profile") or "default") + ":" + row["session_id"] for group in payloads.values() for row in group}
    assert {"beta:old", "default:tip", "default:host"} <= sent, sent


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_a_row_whose_parent_is_only_in_another_profile_is_never_hidden(tmp_path):
    # beta's plain "old" carries the default profile's parent link (the active-profile-only lineage enrichment
    # applies default's metadata to every row with that id). It must not attach under default's host, and it
    # must not vanish either: it renders as a top-level row in its own profile.
    rendered = _render("copy-root", tmp_path)
    assert rendered["cli"].get("beta:old", "missing") is None, rendered
    assert rendered["cli"].get("default:tip") == "default:host", rendered
    assert rendered["cli"].get("default:host", "missing") is None, rendered
    assert rendered["webui"] == {}, rendered


# Server half on crafted rows: the bucket derivation itself, independent of the browser.
def _child(sid, parent, **extra):
    return {"session_id": sid, "profile": "default", "parent_session_id": parent, "raw_source": "subagent",
            "source_tag": "subagent", "session_source": "other", **extra}


def _host(sid, source, **extra):
    return {"session_id": sid, "profile": "default", "raw_source": source, "source_tag": source,
            "session_source": "cli" if source != "webui" else "webui", "is_cli_session": source != "webui", **extra}


def test_bucket_follows_a_compressed_parent_through_its_lineage_alias():
    from api import routes

    tip = _host("cp-tip", "cli", parent_session_id="cp-old", _lineage_root_id="cp-old")
    kid = _child("cp-kid", "cp-old", relationship_type="child_session", _parent_lineage_root_id="cp-old")
    rows = routes._sidebar_rows_by_profile([tip, kid])["default"]
    assert rows["cp-old"] is tip
    assert routes._sidebar_display_is_cli(kid, rows) is True


def test_a_real_row_wins_over_a_lineage_alias():
    from api import routes

    real = _host("old", "webui")
    tip = _host("tip", "cli", parent_session_id="old", _lineage_root_id="old")
    rows = routes._sidebar_rows_by_profile([tip, real])["default"]
    assert rows["old"] is real


def test_bucket_skips_a_self_alias_and_follows_the_lineage_roots_parent():
    from api import routes

    host = _host("nest-host", "cli")
    tip = _child("nest-tip", "nest-old", relationship_type="child_session", _lineage_root_id="nest-old",
                 _parent_lineage_root_id="nest-host")
    leaf = _child("nest-leaf", "nest-old", relationship_type="child_session", _parent_lineage_root_id="nest-old")
    rows = routes._sidebar_rows_by_profile([host, tip, leaf])["default"]
    assert routes._sidebar_display_is_cli(tip, rows) is True
    assert routes._sidebar_display_is_cli(leaf, rows) is True


def test_an_opened_subagent_sidecar_without_a_relationship_follows_its_parent():
    from api import routes

    parent = _host("dk", "desktop")
    sidecar = _child("dk-kid-open", "dk", read_only=True)  # no relationship_type before lineage enrichment
    rows = routes._sidebar_rows_by_profile([parent, sidecar])["default"]
    assert routes._sidebar_display_is_cli(sidecar, rows) is True
    # A declared non-child relationship, or a subagent with no parent, keeps its own bucket.
    assert routes._sidebar_display_is_cli({**sidecar, "relationship_type": "continuation"}, rows) is False
    assert routes._sidebar_display_is_cli({**sidecar, "parent_session_id": ""}, rows) is False


def test_a_delegation_cycle_stops():
    from api import routes

    a = _child("a", "b", relationship_type="child_session")
    b = _child("b", "a", relationship_type="child_session")
    rows = routes._sidebar_rows_by_profile([a, b])["default"]
    assert routes._sidebar_display_is_cli(a, rows) is False
