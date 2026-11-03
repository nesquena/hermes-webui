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
        active_profile="default", all_profiles=scenario == "profiles", show_cli_sessions=True,
        show_previous_messaging_sessions=False, show_cron_sessions=False, show_claude_code_sessions=False,
        sidebar_source=group,
    ))["sessions"]
print(json.dumps(payloads, default=str))
'''

_SIDEBAR = _EXTRACT_FUNC + r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const payloads = JSON.parse(require('fs').readFileSync(0, 'utf8'));
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
  _sidebarReferenceSessions: [], S: {session: null, busy: false, activeStreamId: null, activeProfile: 'default'},
  _activeProject: null, _showArchived: false,
});
const key = r => (r.profile || 'default') + ':' + r.session_id;
const out = {};
for (const group of ['webui', 'cli']) {
  global._sessionSourceFilter = group;
  global._allSessions = payloads[group];
  const rendered = {};
  const visit = (row, parent) => {
    rendered[key(row)] = parent;
    for (const child of row._child_sessions || []) visit(child, key(row));
  };
  for (const row of _renderSidebarRowsFromPartition(_partitionSidebarSessionRows(payloads[group], null))) visit(row, null);
  out[group] = rendered;
}
console.log(JSON.stringify(out));
"""


def _render(scenario: str, tmp_path: Path) -> dict:
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
    payloads = json.loads(proc.stdout.strip().splitlines()[-1])
    proc = subprocess.run(
        [NODE, "-e", _SIDEBAR, str(SESSIONS_JS)],
        input=json.dumps(payloads), cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    rendered = json.loads(proc.stdout.strip().splitlines()[-1])
    # Every row the server sends for a group renders in that group, and in no other.
    for group, other in (("webui", "cli"), ("cli", "webui")):
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
