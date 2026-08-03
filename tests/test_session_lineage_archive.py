import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _run_node(source: str) -> str:
    proc = subprocess.run(
        ["node"],
        cwd=ROOT,
        input=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_collapsed_lineage_archive_and_restore_mutate_only_safe_segments():
    source = f"""
const src = {SESSIONS_JS!r};
function extractFunc(name) {{
  const syncStart = src.indexOf('function ' + name + '(');
  const asyncStart = src.indexOf('async function ' + name + '(');
  const starts = [syncStart, asyncStart].filter(index => index >= 0);
  const start = starts.length ? Math.min(...starts) : -1;
  if (start < 0) throw new Error('missing function ' + name);
  let brace = src.indexOf('{{', start);
  let depth = 0;
  for (let i = brace; i < src.length; i++) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') {{
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }}
  }}
  throw new Error('unterminated function ' + name);
}}

eval(extractFunc('_sessionArchiveTargets'));
eval(extractFunc('_archiveSession'));

const calls = [];
const toasts = [];
const local = new Map([['hermes-webui-session', 'tip']]);
globalThis.localStorage = {{
  getItem: key => local.has(key) ? local.get(key) : null,
  removeItem: key => local.delete(key),
}};
globalThis.S = {{session: {{session_id:'tip', archived:false}}}};
globalThis._allSessions = [
  {{session_id:'tip', profile:'default', archived:false}},
  {{session_id:'mid', profile:'default', archived:false}},
  {{session_id:'root', profile:'default', archived:false}},
  {{session_id:'fork', profile:'default', archived:false}},
  {{session_id:'child', profile:'default', archived:false}},
  {{session_id:'foreign', profile:'other-profile', archived:false}},
  {{session_id:'unrelated', profile:'default', archived:false}},
];
globalThis._showArchived = false;
globalThis._sessionSwipeReturnOffsets = new Map();
globalThis._pendingSessionReflowPositions = null;
globalThis._isReadOnlySession = () => false;
globalThis._captureSessionReflowPositions = () => new Map();
globalThis._sessionPrefersReducedMotion = () => true;
globalThis._sessionArchiveToast = () => 'archived';
globalThis.t = key => key;
globalThis.showToast = value => toasts.push(value);
globalThis.renderSessionListFromCache = () => {{}};
globalThis.renderSessionList = async () => {{}};
globalThis.api = async (path, options) => {{
  const body = JSON.parse(options.body);
  calls.push({{path, body}});
  return {{ok:true}};
}};

const row = {{
  session_id:'tip',
  profile:'default',
  archived:false,
  _lineage_segments:[
    {{session_id:'tip', profile:'default'}},
    {{session_id:'mid', profile:'default'}},
    {{session_id:'root', profile:'default'}},
    {{session_id:'fork', profile:'default', session_source:'fork'}},
    {{session_id:'child', profile:'default', relationship_type:'child_session'}},
    {{session_id:'foreign', profile:'other-profile'}},
    {{session_id:'mid', profile:'default'}},
  ],
  _child_sessions:[{{session_id:'unrelated', profile:'default'}}],
}};

(async () => {{
  const archived = await _archiveSession(row, true);
  const archivedCalls = calls.splice(0).map(call => call.body);
  const archiveState = Object.fromEntries(_allSessions.map(s => [s.session_id, s.archived]));
  const restored = await _archiveSession(row, false);
  const restoredCalls = calls.splice(0).map(call => call.body);
  const restoreState = Object.fromEntries(_allSessions.map(s => [s.session_id, s.archived]));
  const directTargets = {{
    fork: _sessionArchiveTargets({{session_id:'fork', profile:'default', session_source:'fork'}}).map(s => s.session_id),
    child: _sessionArchiveTargets({{session_id:'child', profile:'default', relationship_type:'child_session'}}).map(s => s.session_id),
  }};
  console.log(JSON.stringify({{
    archived,
    restored,
    archivedCalls,
    restoredCalls,
    archiveState,
    restoreState,
    directTargets,
    savedPointer: local.get('hermes-webui-session') || null,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = json.loads(_run_node(source))

    assert result["archived"] is True
    assert result["restored"] is True
    assert result["archivedCalls"] == [
        {"session_id": "tip", "archived": True},
        {"session_id": "mid", "archived": True},
        {"session_id": "root", "archived": True},
    ]
    assert result["restoredCalls"] == [
        {"session_id": "tip", "archived": False},
        {"session_id": "mid", "archived": False},
        {"session_id": "root", "archived": False},
    ]
    assert result["archiveState"] == {
        "tip": True,
        "mid": True,
        "root": True,
        "fork": False,
        "child": False,
        "foreign": False,
        "unrelated": False,
    }
    assert result["restoreState"] == {key: False for key in result["archiveState"]}
    assert result["directTargets"] == {"fork": ["fork"], "child": ["child"]}
    assert result["savedPointer"] is None
