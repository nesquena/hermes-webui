"""#8762: Desktop rows keep their WebUI metadata and render in exactly one sidebar group.

exp-v0.52.618 (#7415) moved Desktop sessions into the CLI group. Two rows broke there:

1. An imported legacy Desktop history whose message roles are all NULL lost its pin. The state.db projection keeps
   that history visible through its role fallback, but the imported copy counted no user turns, so the copy was
   dropped and the bare state.db row (``pinned: False``) stood in for it.
2. A WebUI session carrying a Desktop tag (``source_tag='desktop'`` or ``source_label='Desktop'``) was listed in the
   WebUI group by the server, while the browser's ``_isCliSession`` called it CLI, so it rendered in neither group.

The probe builds a real ``state.db`` and WebUI state in an isolated process and takes the server's payloads for both
groups. The sidebar test then feeds those payloads through the real partition and render helpers from
``static/sessions.js`` under node, and checks that every row the server sends for a group renders in that group and
in no other. The fixture also carries Desktop and CLI parents with delegated children, so the invariant covers them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = REPO / "static" / "sessions.js"
NODE = shutil.which("node")

_PROBE = r'''
import io, json, sqlite3, sys
from urllib.parse import urlparse
from api import models, routes

db = sqlite3.connect(sys.argv[1])
db.executescript("""CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL,
  message_count INTEGER, parent_session_id TEXT, model_config TEXT);
CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);""")
clock = [1800000000.0]


def add(sid, source, roles, parent=None):
    clock[0] += 1
    cfg = json.dumps({"_delegate_from": parent}) if parent else None
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
               (sid, source, "Title " + sid, "m", clock[0], len(roles), parent, cfg))
    for role in roles:
        db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)",
                   (sid, role, "x", clock[0]))


for parent, source in (("dk", "desktop"), ("ck", "cli")):
    add(parent, source, ["user", "assistant"])
    add(parent + "-kid", "subagent", ["user", "assistant"], parent=parent)
    add(parent + "-kid-open", "subagent", ["user", "assistant"], parent=parent)
add("dk-old", "desktop", [None, None])           # legacy history: roles unknown
add("dk-stub", "desktop", ["assistant", "tool"])  # connection stub: roles known, no user turn
db.commit()


class Handler:
    def __init__(self, path, body):
        raw = json.dumps(body).encode()
        self.rfile, self.wfile = io.BytesIO(raw), io.BytesIO()
        self.headers = {"Content-Length": str(len(raw))}
        self.path, self.command, self.client_address, self.code = path, "POST", ("127.0.0.1", 9), None

    def send_response(self, code):
        self.code = code

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass


codes = {}
for sid in ("dk-old", "dk-stub", "dk-kid-open", "ck-kid-open"):
    h = Handler("/api/session/import_cli", {"session_id": sid})
    routes._handle_session_import_cli(h, {"session_id": sid})
    codes["import " + sid] = h.code
h = Handler("/api/session/pin", {"session_id": "dk-old", "pinned": True})
routes.handle_post(h, urlparse(h.path))
codes["pin dk-old"] = h.code
for sid, marker in (("wd", {"source_tag": "desktop"}), ("wl", {"source_label": "Desktop"})):
    models.Session(sid, title="WebUI chat " + sid, workspace=sys.argv[2], profile="default",
                   messages=[{"role": "user", "content": "hi"}], session_source="webui", **marker).save()
models.clear_cli_sessions_cache()
payloads = {}
for group in ("webui", "cli"):
    payloads[group] = routes._build_session_list_cache_payload(
        active_profile="default", all_profiles=False, show_cli_sessions=True,
        show_previous_messaging_sessions=False, show_cron_sessions=False, show_claude_code_sessions=False,
        sidebar_source=group,
    )["sessions"]
print(json.dumps({"codes": codes, "payloads": payloads}, default=str))
'''


@pytest.fixture(scope="module")
def server_payloads(tmp_path_factory):
    root = tmp_path_factory.mktemp("issue8762")
    home = root / "home"
    state = root / "webui"
    (state / "sessions").mkdir(parents=True)
    home.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(root),
            "HERMES_HOME": str(home),
            "HERMES_BASE_HOME": str(home),
            "HERMES_WEBUI_STATE_DIR": str(state),
            "HERMES_WEBUI_ISOLATED_PROFILE": "0",
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(home / "state.db"), str(root)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["codes"] == {
        "import dk-old": 200,
        "import dk-stub": 200,
        "import dk-kid-open": 200,
        "import ck-kid-open": 200,
        "pin dk-old": 200,
    }
    return result["payloads"]


def _ids(rows):
    return [row["session_id"] for row in rows]


def test_imported_legacy_desktop_history_keeps_its_pin(server_payloads):
    rows = [row for row in server_payloads["cli"] if row["session_id"] == "dk-old"]
    assert len(rows) == 1, _ids(server_payloads["cli"])
    assert rows[0]["pinned"] is True
    assert "dk-old" not in _ids(server_payloads["webui"])


def test_desktop_connection_stub_stays_hidden(server_payloads):
    # Its roles are known and none is a user turn, so neither the copy nor the state.db row may surface.
    assert "dk-stub" not in _ids(server_payloads["cli"])
    assert "dk-stub" not in _ids(server_payloads["webui"])


def test_server_lists_webui_sessions_with_a_desktop_marker_as_webui(server_payloads):
    for sid in ("wd", "wl"):
        assert sid in _ids(server_payloads["webui"])
        assert sid not in _ids(server_payloads["cli"])


_EXTRACT_FUNC = r"""
// Extract a top-level function declaration from `src`: the shortest slice ending in '}' that the JS parser accepts
// as a whole function. The parser, not a hand scanner, decides where strings, template literals (with nested
// interpolation), regex literals and comments end, so a brace inside any of them cannot cut the function short.
function extractFunc(src, name) {
  const vm = require('vm');
  const start = src.search(new RegExp('^function\\s+' + name + '\\s*\\(', 'm'));
  if (start < 0) throw new Error(name + ' not found');
  for (let i = src.indexOf('}', start); i !== -1; i = src.indexOf('}', i + 1)) {
    const body = src.slice(start, i + 1);
    try {
      new vm.Script('(' + body + '\n)');
      return body;
    } catch (e) {
      if (!(e instanceof SyntaxError)) throw e;
    }
  }
  throw new Error('unbalanced ' + name);
}
"""

_SIDEBAR = _EXTRACT_FUNC + r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const payloads = JSON.parse(require('fs').readFileSync(0, 'utf8'));
// Load the real partition/render entry points plus every top-level helper they reach, so the harness follows
// the sidebar code as it changes instead of pinning a list of helper names.
const topLevel = new Set([...src.matchAll(/^function\s+([A-Za-z_$][\w$]*)\s*\(/gm)].map(m => m[1]));
const stubbed = new Set(['_hasUnreadForSession']);
const loaded = new Set();
const queue = ['_partitionSidebarSessionRows', '_renderSidebarRowsFromRawSessions', '_isCliSession'];
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
  _sidebarReferenceSessions: [], S: {session: null, busy: false, activeStreamId: null},
  _activeProject: null, _showArchived: false,
});
const out = {};
for (const group of ['webui', 'cli']) {
  global._sessionSourceFilter = group;
  const part = _partitionSidebarSessionRows(payloads[group], null);
  const reference = group === 'cli' ? part.cliReferenceRaw : part.webuiReferenceRaw;
  const rendered = {};
  const visit = (row, parent) => {
    rendered[row.session_id] = {parent, pinned: !!row.pinned};
    for (const child of row._child_sessions || []) visit(child, row.session_id);
  };
  for (const row of _renderSidebarRowsFromRawSessions(part.sessionsRaw, reference)) visit(row, null);
  out[group] = rendered;
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_sidebar_renders_every_server_row_in_its_own_group(server_payloads):
    proc = subprocess.run(
        [NODE, "-e", _SIDEBAR, str(SESSIONS_JS)],
        input=json.dumps(server_payloads),
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    rendered = json.loads(proc.stdout.strip().splitlines()[-1])
    for group, other in (("webui", "cli"), ("cli", "webui")):
        sent = set(_ids(server_payloads[group]))
        assert set(rendered[group]) == sent, (group, sorted(sent), rendered[group])
        assert not set(rendered[group]) & set(rendered[other]), (group, rendered)
    assert rendered["cli"]["dk-old"]["pinned"] is True
    for sid in ("wd", "wl"):
        assert rendered["webui"][sid]["parent"] is None


_TRICKY_JS = r"""
function tricky(a) {
  const re = /\}/;  // a } in a line comment
  /* and } in a block comment */
  const s = '}' + "{" + `${ {k: '}'}.k }` + `${`${'}'}`}`;
  return re.test(s) ? {done: true, s} : {};
}
function after() { return {}; }
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_function_extractor_ends_where_the_parser_does(tmp_path):
    # The harness loads sidebar helpers by name; a brace inside a regex, string, template or comment must not end a
    # function early (a hand-written brace scanner did on the regex and the nested template).
    source = tmp_path / "tricky.js"
    source.write_text(_TRICKY_JS, encoding="utf-8")
    script = _EXTRACT_FUNC + r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const tricky = extractFunc(src, 'tricky');
const after = extractFunc(src, 'after');
const run = eval('(' + tricky + ')')(1);
console.log(JSON.stringify({tricky, after, run}));
"""
    proc = subprocess.run([NODE, "-e", script, str(source)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["tricky"] == _TRICKY_JS[_TRICKY_JS.index("function tricky"):_TRICKY_JS.index("\nfunction after")]
    assert got["after"] == "function after() { return {}; }"
    assert got["run"] == {"done": True, "s": "}{}}"}


# The copy defers to its own state.db row only. Run the real builder with crafted rows for the two guards.


def _copy_row(sid: str, *, profile: str, message_count: int) -> dict:
    return {
        "session_id": sid,
        "profile": profile,
        "source_tag": "desktop",
        "raw_source": "desktop",
        "session_source": "cli",
        "source_label": "Desktop",
        "is_cli_session": True,
        "title": "Imported legacy Desktop history",
        "message_count": message_count,
        "user_message_count": 0,
        "pinned": True,
        "updated_at": 2,
    }


def _state_row(sid: str, *, profile: str, user_turns: int = 2) -> dict:
    return {
        "session_id": sid,
        "profile": profile,
        "source_tag": "desktop",
        "raw_source": "desktop",
        "session_source": "cli",
        "source_label": "Desktop",
        "is_cli_session": True,
        "title": "Imported legacy Desktop history",
        "message_count": 2,
        "actual_message_count": 2,
        "actual_user_message_count": user_turns,
        "pinned": False,
        "updated_at": 1,
    }


def _build(monkeypatch, copies, state_rows, *, all_profiles=False):
    import api.routes as routes

    monkeypatch.setattr(routes, "all_sessions", lambda **_kwargs: [dict(row) for row in copies])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **_kwargs: [dict(row) for row in state_rows])
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(
        routes, "_prune_orphaned_webui_zero_message_sessions", lambda current, **_kwargs: list(current)
    )
    monkeypatch.setattr(routes, "_prune_orphaned_desktop_sidecar_sessions", lambda current, **_kwargs: list(current))
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)
    monkeypatch.setattr(routes, "prune_session_from_index", lambda _sid: None)
    return routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=all_profiles,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )["sessions"]


def test_copy_with_messages_represents_its_visible_state_row(monkeypatch):
    rows = _build(monkeypatch, [_copy_row("legacy", profile="default", message_count=2)],
                  [_state_row("legacy", profile="default")])
    assert [(row["session_id"], row["pinned"]) for row in rows] == [("legacy", True)]


def test_copy_never_borrows_another_profiles_state_row(monkeypatch):
    rows = _build(monkeypatch, [_copy_row("shared", profile="work", message_count=2)],
                  [_state_row("shared", profile="personal")], all_profiles=True)
    assert [(row["session_id"], row["profile"], row["pinned"]) for row in rows] == [("shared", "personal", False)]


def test_copy_for_an_id_two_profiles_share_keeps_both_state_rows(monkeypatch):
    # The dedupe is by id, so keeping the work copy would hide the personal profile's session too. The work row
    # comes last, so it is the one cli_by_id holds for the id.
    rows = _build(monkeypatch, [_copy_row("cloned", profile="work", message_count=2)],
                  [_state_row("cloned", profile="personal"), _state_row("cloned", profile="work")],
                  all_profiles=True)
    assert sorted((row["session_id"], row["profile"]) for row in rows) == [("cloned", "personal"), ("cloned", "work")]


def test_copy_matches_its_state_row_through_the_renamed_root_alias(monkeypatch):
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name in ("default", "kinni"))
    rows = _build(monkeypatch, [_copy_row("legacy", profile="default", message_count=2)],
                  [_state_row("legacy", profile="kinni")])
    assert [(row["session_id"], row["pinned"]) for row in rows] == [("legacy", True)]


def test_empty_copy_leaves_the_state_row_in_place(monkeypatch):
    rows = _build(monkeypatch, [_copy_row("empty", profile="default", message_count=0)],
                  [_state_row("empty", profile="default")])
    assert [(row["session_id"], row["pinned"]) for row in rows] == [("empty", False)]


def test_copy_never_revives_a_hidden_state_row(monkeypatch):
    # A Desktop connection record with known roles and no user turn is hidden in state.db; its copy stays hidden too.
    rows = _build(monkeypatch, [_copy_row("stub", profile="default", message_count=2)],
                  [_state_row("stub", profile="default", user_turns=0)])
    assert rows == []
