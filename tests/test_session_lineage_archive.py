import json
import sqlite3
import subprocess
from pathlib import Path

from api.agent_sessions import read_session_lineage_ids

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_lineage_ids_enumerates_complete_continuation_tree_only(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, parent_session_id TEXT, end_reason TEXT, started_at REAL, ended_at REAL, source TEXT, session_source TEXT)")
    rows = [
        ("root", None, "compression", 1, 2, "cli", None),
        ("tip-a", "root", None, 3, None, "cli", None),
        ("mid-b", "root", "compression", 3, 4, "cli", None),
        ("tip-b", "mid-b", None, 5, None, "cli", None),
        ("fork", "root", None, 3, None, "cli", "fork"),
        ("child", "root", None, 1, None, "cli", None),
        ("cross-source", "root", None, 3, None, "tui", None),
    ]
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    assert set(read_session_lineage_ids(db, "tip-b")) == {"root", "tip-a", "mid-b", "tip-b"}


def test_lineage_archive_rechecks_scope_while_all_target_locks_are_held():
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    start = routes.index('        if body.get("lineage"):\n')
    end = routes.index("        if _session_is_subagent_view_only(sid):\n", start)
    archive = routes[start:end]

    assert "with ExitStack() as locks:" in archive
    assert "for lineage_sid in sorted(lineage_ids):" in archive
    assert archive.index("locks.enter_context(_get_session_agent_lock(lineage_sid))") < archive.index(
        "current_ids = read_session_lineage_ids(state_db_path, sid)"
    )
    assert "if set(current_ids) != set(lineage_ids):" in archive
    assert 'return bad(handler, "Session lineage changed during archive; retry", 409)' in archive


def test_archive_always_uses_one_backend_lineage_operation():
    assert "const payload={session_id:session.session_id,archived,lineage:true}" in SESSIONS_JS
    assert "Promise.all(targets.map" not in SESSIONS_JS
    script = f"""
const src={SESSIONS_JS!r};
const start=src.indexOf('async function _archiveSession('); let brace=src.indexOf('{{',start), depth=0, end=-1;
for(let i=brace;i<src.length;i++){{if(src[i]==='{{')depth++;else if(src[i]==='}}'&&--depth===0){{end=i+1;break;}}}}
eval(src.slice(start,end));
const calls=[];
Object.assign(globalThis,{{
  _isReadOnlySession:()=>false,_captureSessionReflowPositions:()=>new Map(),_sessionSegmentCount:()=>3,
  api:async(p,o)=>{{calls.push(JSON.parse(o.body));return {{session_ids:['root','tip']}};}},
  _allSessions:[],S:{{session:null}},localStorage:{{getItem:()=>null,removeItem:()=>{{}}}},showToast:()=>{{}},
  _sessionArchiveToast:()=>'',t:x=>x,_showArchived:false,_sessionPrefersReducedMotion:()=>true,
  _sessionSwipeReturnOffsets:new Map(),renderSessionListFromCache:()=>{{}},renderSessionList:async()=>{{}}
}});
_archiveSession({{session_id:'tip',archived:false}},true).then(()=>console.log(JSON.stringify(calls)));
"""
    proc = subprocess.run(["node"], input=script, text=True, capture_output=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [{"session_id": "tip", "archived": True, "lineage": True}]


def test_archive_does_not_trust_bounded_sidebar_metadata():
    """A row beyond the top-300 enrichment cap still delegates scope to the server."""
    script = f"""
const src={SESSIONS_JS!r};
const start=src.indexOf('async function _archiveSession('); let brace=src.indexOf('{{',start), depth=0, end=-1;
for(let i=brace;i<src.length;i++){{if(src[i]==='{{')depth++;else if(src[i]==='}}'&&--depth===0){{end=i+1;break;}}}}
eval(src.slice(start,end));
const calls=[];
Object.assign(globalThis,{{
  _isReadOnlySession:()=>false,_captureSessionReflowPositions:()=>new Map(),
  _sessionSegmentCount:()=>0,
  api:async(p,o)=>{{calls.push(JSON.parse(o.body));return {{session_ids:['hidden-root','row-301']}};}},
  _allSessions:Array.from({{length:301}},(_,i)=>({{session_id:`row-${{i+1}}`,archived:false}})),
  S:{{session:null}},localStorage:{{getItem:()=>null,removeItem:()=>{{}}}},showToast:()=>{{}},
  _sessionArchiveToast:()=>'',t:x=>x,_showArchived:false,_sessionPrefersReducedMotion:()=>true,
  _sessionSwipeReturnOffsets:new Map(),renderSessionListFromCache:()=>{{}},renderSessionList:async()=>{{}}
}});
_archiveSession(_allSessions[300],true).then(()=>console.log(JSON.stringify(calls)));
"""
    proc = subprocess.run(["node"], input=script, text=True, capture_output=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        {"session_id": "row-301", "archived": True, "lineage": True}
    ]
