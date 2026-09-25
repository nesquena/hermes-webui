"""Regression coverage for bounded synchronous cron session enrichment."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import types
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


class _JSONHandler:
    def __init__(self):
        self.status = None
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _stub_cron_jobs(monkeypatch, *, jobs=None):
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    if jobs is not None:
        cron_jobs.list_jobs = lambda include_disabled=True: jobs
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    return cron_jobs


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    if source[max(0, start - 6) : start] == "async ":
        start -= 6
    brace = source.index("{", start)
    depth = 1
    pos = brace + 1
    while depth and pos < len(source):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    assert depth == 0
    return source[start:pos]


_ONE_JOB = [
    {"id": "a", "name": "Job A", "last_run_at": 50, "last_status": "success"},
]


def test_cron_recent_returns_promptly_when_session_info_read_raises(monkeypatch):
    """A failed bounded read does not delay or discard the completion."""
    import api.routes as routes

    _stub_cron_jobs(monkeypatch, jobs=list(_ONE_JOB))

    def _raising_session_info(job_ids, completed_job_ids=None, deadline_s=None):
        raise RuntimeError("state db unavailable")

    monkeypatch.setattr(routes, "_latest_cron_session_info_for_jobs", _raising_session_info)

    handler = _JSONHandler()
    started = time.monotonic()
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=10"))
    elapsed = time.monotonic() - started

    assert handler.status == 200
    body = _payload(handler)
    assert {item["job_id"] for item in body["completions"]} == {"a"}
    assert body["completions"][0]["completed_at"] == 50.0
    assert body["completions"][0]["session_id"] == ""
    assert body["session_lookup_failed"] is True
    assert elapsed < 1.0, f"handler blocked for {elapsed:.1f}s"


def test_cron_recent_still_enriches_when_session_info_is_fast(monkeypatch):
    """The short deadline must not degrade the common case."""
    import api.routes as routes

    _stub_cron_jobs(monkeypatch, jobs=list(_ONE_JOB))
    monkeypatch.setattr(
        routes,
        "_latest_cron_session_info_for_jobs",
        lambda job_ids, completed_job_ids=None, deadline_s=None: {
            "a": {"session_id": "sess-1", "message_count": 3}
        },
    )

    handler = _JSONHandler()
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=10"))

    assert handler.status == 200
    body = _payload(handler)
    completion = body["completions"][0]
    assert completion["session_id"] == "sess-1"
    assert completion["message_count"] == 3
    assert body["session_lookup_failed"] is False


def test_named_request_profile_uses_its_state_db(monkeypatch, tmp_path):
    """Synchronous lookup keeps the request thread's named profile context."""
    import api.profiles as profiles
    import api.routes as routes

    default_home = tmp_path / "default"
    named_home = default_home / "profiles" / "named"
    default_home.mkdir()
    named_home.mkdir(parents=True)

    def _make_db(path: Path, session_id: str):
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    source TEXT,
                    started_at REAL,
                    message_count INTEGER
                );
                """
            )
            conn.execute(
                "INSERT INTO sessions VALUES (?, 'cron', 100, 4)",
                (session_id,),
            )

    _make_db(default_home / "state.db", "cron_a_default")
    _make_db(named_home / "state.db", "cron_a_named")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", default_home)
    _stub_cron_jobs(monkeypatch, jobs=list(_ONE_JOB))

    profiles.set_request_profile("named")
    try:
        handler = _JSONHandler()
        routes._handle_cron_recent(handler, SimpleNamespace(query="since=10"))
    finally:
        profiles.clear_request_profile()

    completion = _payload(handler)["completions"][0]
    assert completion["session_id"] == "cron_a_named"
    assert completion["message_count"] == 4


def test_failed_lookup_is_retried_and_recovers(monkeypatch):
    """A failed lookup recovers on the next request for the same window.

    The client owns the cursor and retries the detail separately, so the server
    must keep answering for the same ``since`` until the detail resolves.
    """
    import api.routes as routes

    _stub_cron_jobs(monkeypatch, jobs=list(_ONE_JOB))
    results = iter(
        [
            RuntimeError("state db busy"),
            {"a": {"session_id": "cron_a_recovered", "message_count": 2}},
        ]
    )

    def _lookup(job_ids, completed_job_ids=None, deadline_s=None):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(routes, "_latest_cron_session_info_for_jobs", _lookup)

    first = _JSONHandler()
    routes._handle_cron_recent(first, SimpleNamespace(query="since=10"))
    first_body = _payload(first)
    assert first_body["session_lookup_failed"] is True
    assert first_body["completions"][0]["completed_at"] == 50.0
    assert first_body["completions"][0]["session_id"] == ""

    second = _JSONHandler()
    routes._handle_cron_recent(second, SimpleNamespace(query="since=10"))
    second_body = _payload(second)
    assert second_body["session_lookup_failed"] is False
    assert second_body["completions"][0]["completed_at"] == 50.0
    assert second_body["completions"][0]["session_id"] == "cron_a_recovered"


def test_cron_recent_does_not_leave_extra_threads(monkeypatch):
    import api.routes as routes

    _stub_cron_jobs(monkeypatch, jobs=list(_ONE_JOB))
    monkeypatch.setattr(
        routes,
        "_latest_cron_session_info_for_jobs",
        lambda job_ids, completed_job_ids=None, deadline_s=None: {},
    )
    before = {thread.ident for thread in threading.enumerate() if thread.is_alive()}

    handler = _JSONHandler()
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=10"))

    after = {thread.ident for thread in threading.enumerate() if thread.is_alive()}
    assert after == before


def test_lookup_deadline_bounds_the_scan_not_just_the_lock_wait(tmp_path):
    """A wall-clock deadline aborts a read whose cost is the query itself."""
    import api.agent_sessions as agent_sessions

    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, started_at REAL, message_count INTEGER);"
        )

    with closing(agent_sessions.open_state_db_readonly(db, deadline_s=0.05)) as conn:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "WITH RECURSIVE c(x) AS ("
                "SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<200000000"
                ") SELECT count(*) FROM c"
            ).fetchone()
        elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"deadline did not abort the scan ({elapsed:.1f}s)"


def test_lookup_deadline_does_not_trip_a_fast_read(tmp_path):
    """The deadline is inert for a query that finishes inside its budget."""
    import api.agent_sessions as agent_sessions

    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, started_at REAL, message_count INTEGER);"
        )

    with closing(agent_sessions.open_state_db_readonly(db, deadline_s=30.0)) as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_lookup_scan_is_row_capped(tmp_path, monkeypatch):
    """The scan carries a row cap, so a long cron history cannot be read whole."""
    import api.profiles as profiles
    import api.routes as routes

    home = tmp_path / "home"
    home.mkdir()
    db = home / "state.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, started_at REAL, message_count INTEGER);"
        )
        conn.executemany(
            "INSERT INTO sessions VALUES (?, 'cron', ?, 1)",
            [(f"cron_a_{i:06d}", float(i)) for i in range(5000)],
        )

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", home)

    seen = []
    real_open = routes.open_state_db_readonly

    def _recording_open(db_path, *args, **kwargs):
        conn = real_open(db_path, *args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(routes, "open_state_db_readonly", _recording_open)

    info = routes._latest_cron_session_info_for_jobs(["a"], ["a"], deadline_s=5.0)

    scans = [sql for sql in seen if "FROM sessions" in sql]
    assert scans, "expected a sessions scan"
    assert all("LIMIT 200" in sql for sql in scans)
    # The newest row still wins: the cap must not cut the answer off.
    assert info["a"]["session_id"] == "cron_a_004999"


_POLL_HARNESS_PREAMBLE = """
let _cronPollSince=10;
let _cronPollTimer=null;
let _cronUnreadCount=0;
let _cronPollGeneration=0;
const _cronNewJobIds=new Set();
const _CRON_PENDING_MAX=50;
const _CRON_PENDING_ATTEMPTS=5;
const _cronPendingDetails=new Map();
const markCalls=[];
const toastCalls=[];
const urls=[];
let responses=[];
let intervalCallback=null;
global.document={hidden:false};
global.setInterval=(callback)=>{ intervalCallback=callback; return 1; };
global.api=async(url)=>{
  urls.push(url);
  return responses.length ? responses.shift() : {completions:[],session_lookup_failed:false};
};
global.showToast=(...args)=>{ toastCalls.push(args); };
global.t=(...args)=>args.join('|');
global.updateCronBadge=()=>{ _cronUnreadCount=_cronNewJobIds.size; };
function _markSessionCompletionUnreadIfBackground(sid,count){ markCalls.push([sid,count]); }
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_poll_advances_cursor_and_retries_detail_without_replaying_completion():
    """A failed lookup advances the cursor; the retry neither re-badges nor re-toasts.

    Guards the two round-3 findings: holding the cursor replayed completions so an
    opened job's badge came back, and old toasts returned once the toast memory
    evicted them.
    """
    polling = _extract_function(PANELS_JS, "startCronPolling")
    remember = _extract_function(PANELS_JS, "_cronRememberPendingDetails")
    retry = _extract_function(PANELS_JS, "_cronRetryPendingDetails")
    script = (
        _POLL_HARNESS_PREAMBLE
        + f"""
{remember}
{retry}
{polling}
(async()=>{{
  responses=[
    // tick 1 — completion arrives, but the bounded lookup failed
    {{completions:[{{job_id:'job-a',completed_at:20,name:'A',status:'success',toast_notifications:true}}],session_lookup_failed:true}},
    // tick 1 retry — still failing
    {{completions:[{{job_id:'job-a',completed_at:20,name:'A',status:'success'}}],session_lookup_failed:true}},
    // tick 2 — nothing new on the primary poll
    {{completions:[],session_lookup_failed:false}},
    // tick 2 retry — detail resolves
    {{completions:[{{job_id:'job-a',completed_at:20,name:'A',status:'success',session_id:'cron_a_20',message_count:2}}],session_lookup_failed:false}},
  ];
  startCronPolling();
  await intervalCallback();
  const afterFirst={{since:_cronPollSince,badge:Array.from(_cronNewJobIds),pending:_cronPendingDetails.size,toasts:toastCalls.length}};
  // The user opens the job, clearing its badge.
  _cronNewJobIds.delete('job-a');
  await intervalCallback();
  process.stdout.write(JSON.stringify({{
    afterFirst,
    afterSecond:{{
      since:_cronPollSince,
      badge:Array.from(_cronNewJobIds),
      pending:_cronPendingDetails.size,
      toasts:toastCalls.length,
      markCalls,
    }},
    urls,
  }}));
}})().catch(error=>{{ console.error(error); process.exit(1); }});
"""
    )
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout)

    assert state["afterFirst"] == {"since": 20, "badge": ["job-a"], "pending": 1, "toasts": 1}
    assert state["afterSecond"]["since"] == 20
    assert state["afterSecond"]["badge"] == []
    assert state["afterSecond"]["pending"] == 0
    assert state["afterSecond"]["toasts"] == 1
    assert state["afterSecond"]["markCalls"] == [["cron_a_20", 2]]
    # The retry is a separate, narrow request — not a replay of the primary poll.
    assert len(state["urls"]) == 4


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_poll_gives_up_on_a_permanently_failing_lookup():
    """After a bounded number of attempts the page drops the pending completion."""
    polling = _extract_function(PANELS_JS, "startCronPolling")
    remember = _extract_function(PANELS_JS, "_cronRememberPendingDetails")
    retry = _extract_function(PANELS_JS, "_cronRetryPendingDetails")
    script = (
        _POLL_HARNESS_PREAMBLE
        + f"""
{remember}
{retry}
{polling}
(async()=>{{
  responses=[
    {{completions:[{{job_id:'job-a',completed_at:20,name:'A',status:'success',toast_notifications:true}}],session_lookup_failed:true}},
    {{completions:[],session_lookup_failed:true}},
  ];
  global.api=async(url)=>{{
    urls.push(url);
    if(responses.length) return responses.shift();
    return {{completions:[],session_lookup_failed:true}};
  }};
  startCronPolling();
  const sizes=[];
  const toasts=[];
  for(let i=0;i<6;i+=1){{
    await intervalCallback();
    sizes.push(_cronPendingDetails.size);
    toasts.push(toastCalls.length);
  }}
  process.stdout.write(JSON.stringify({{sizes,toasts}}));
}})().catch(error=>{{ console.error(error); process.exit(1); }});
"""
    )
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout)

    # One pending entry after the first poll; gone after _CRON_PENDING_ATTEMPTS.
    assert state["sizes"][0] == 1
    assert state["sizes"][-1] == 0
    assert state["sizes"] == [1, 1, 1, 1, 0, 0]
    assert state["toasts"] == [1, 1, 1, 1, 1, 1]