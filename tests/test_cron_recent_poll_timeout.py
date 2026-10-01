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


def test_lookup_finds_target_behind_newer_rows_from_other_jobs(tmp_path, monkeypatch):
    """A requested job's session is found even behind 200+ newer rows of others (#7830).

    The previous head applied a global ``LIMIT`` over all cron rows before
    matching the requested jobs, so a target sitting behind more than the window
    came back empty while the lookup reported success - a permanently lost unread
    marker. The candidate window must not be capped globally.
    """
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
        # 5000 newer sessions belonging to a DIFFERENT job ...
        conn.executemany(
            "INSERT INTO sessions VALUES (?, 'cron', ?, 1)",
            [(f"cron_b_{i:06d}", float(1000 + i)) for i in range(5000)],
        )
        # ... and one older session for the requested job.
        conn.execute("INSERT INTO sessions VALUES ('cron_a_000001', 'cron', 1.0, 7)")

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", home)

    seen = []
    real_open = routes.open_state_db_readonly

    def _recording_open(db_path, *args, **kwargs):
        conn = real_open(db_path, *args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(routes, "open_state_db_readonly", _recording_open)

    info = routes._latest_cron_session_info_for_jobs(["a", "b"], ["a"], deadline_s=5.0)

    scans = [sql for sql in seen if "FROM sessions" in sql]
    assert scans, "expected a sessions scan"
    assert not any("LIMIT" in sql for sql in scans), (
        "the lookup must not cap the candidate window globally, or a requested "
        "job behind newer rows from other jobs is silently dropped"
    )
    assert info["a"]["session_id"] == "cron_a_000001"
    assert info["a"]["message_count"] == 7


def test_lookup_fully_scanned_missing_session_is_empty(tmp_path, monkeypatch):
    """A completed job with no persisted session is an empty result, not a failure.

    The scan ran to completion and simply found nothing for the job, so the
    caller must report success with an empty ``session_id`` - not a retryable
    failure. This is the distinction the #7830 fix must preserve.
    """
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
            [(f"cron_b_{i:06d}", float(i)) for i in range(50)],
        )

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", home)

    info = routes._latest_cron_session_info_for_jobs(["a", "b"], ["a"], deadline_s=5.0)

    assert info["a"] == {"session_id": "", "message_count": None}


def test_cron_recent_no_session_is_not_a_failed_lookup(tmp_path, monkeypatch):
    """A fully scanned lookup with no session for the job reports success (#7830)."""
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
            [(f"cron_b_{i:06d}", float(i)) for i in range(50)],
        )

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", home)
    _stub_cron_jobs(monkeypatch, jobs=list(_ONE_JOB))

    handler = _JSONHandler()
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=10"))

    body = _payload(handler)
    assert body["session_lookup_failed"] is False
    assert body["completions"][0]["session_id"] == ""


def test_lookup_restricts_read_to_requested_jobs_inside_deadline(tmp_path, monkeypatch):
    """The read is an id range over the requested jobs, not a full-table scan.

    A full scan and sort over a large state.db is what pushed the old lookup past
    its deadline; after a bounded number of failed attempts the page dropped the
    completion's unread marker. Restricting the query to the requested job ids
    keeps the read inside the budget whatever the number of other jobs' sessions.
    """
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
            [(f"cron_b_{i:06d}", float(1000 + i)) for i in range(200000)],
        )
        conn.execute("INSERT INTO sessions VALUES ('cron_a_000001', 'cron', 1.0, 7)")

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", home)

    seen = []
    real_open = routes.open_state_db_readonly

    def _recording_open(db_path, *args, **kwargs):
        conn = real_open(db_path, *args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(routes, "open_state_db_readonly", _recording_open)

    info = routes._latest_cron_session_info_for_jobs(["a", "b"], ["a"], deadline_s=0.25)

    scans = [sql for sql in seen if "FROM sessions" in sql]
    assert scans, "expected a sessions scan"
    assert any("s.id >=" in sql and "s.id <" in sql for sql in scans), (
        "the read must be restricted to the requested job ids, or a large state.db "
        "pushes it past the deadline and the page drops the completion"
    )
    assert info["a"]["session_id"] == "cron_a_000001"
    assert info["a"]["message_count"] == 7


_POLL_HARNESS_PREAMBLE = """
let _cronPollSince=10;
let _cronPollTimer=null;
let _cronUnreadCount=0;
let _cronPollGeneration=0;
const _cronNewJobIds=new Set();
const _CRON_RETRY_BATCH=50;
const _CRON_PENDING_BACKOFF_BASE_MS=30000;
const _CRON_PENDING_BACKOFF_MAX_MS=1800000;
const _cronPendingDetails=new Map();
let fakeNow=1000000;
Date.now=()=>fakeNow;
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
    """A failed lookup advances the cursor; the retry neither re-badges nor re-toasts."""
    polling = _extract_function(PANELS_JS, "startCronPolling")
    remember = _extract_function(PANELS_JS, "_cronRememberPendingDetails")
    retry = _extract_function(PANELS_JS, "_cronRetryPendingDetails")
    backoff = _extract_function(PANELS_JS, "_cronPendingBackoffMs")
    script = (
        _POLL_HARNESS_PREAMBLE
        + f"""
{backoff}
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
  fakeNow+=31000;  // let the backoff elapse so the retry fires again
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
def test_poll_backs_off_instead_of_discarding_a_failing_lookup():
    """A permanently failing lookup stays pending and grows its retry delay."""
    polling = _extract_function(PANELS_JS, "startCronPolling")
    remember = _extract_function(PANELS_JS, "_cronRememberPendingDetails")
    retry = _extract_function(PANELS_JS, "_cronRetryPendingDetails")
    backoff = _extract_function(PANELS_JS, "_cronPendingBackoffMs")
    script = (
        _POLL_HARNESS_PREAMBLE
        + f"""
{backoff}
{remember}
{retry}
{polling}
(async()=>{{
  responses=[{{completions:[{{job_id:'job-a',completed_at:20,name:'A',status:'success',toast_notifications:true}}],session_lookup_failed:true}}];
  global.api=async(url)=>{{ urls.push(url); if(responses.length) return responses.shift(); return {{completions:[],session_lookup_failed:true}}; }};
  startCronPolling();
  await intervalCallback();
  const read=()=>{{ const e=[..._cronPendingDetails.values()][0]; return {{size:_cronPendingDetails.size,attempts:e.attempts,next:e.next_at}}; }};
  const after1=read();
  for(let i=0;i<4;i+=1) await intervalCallback();
  const middle=read();
  fakeNow+=31000;
  await intervalCallback();
  const afterBackoff=read();
  process.stdout.write(JSON.stringify({{after1,middle,afterBackoff}}));
}})().catch(error=>{{ console.error(error); process.exit(1); }});
"""
    )
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout)

    # Kept the whole time — never discarded after a fixed number of tries.
    assert state["after1"]["size"] == 1
    assert state["middle"]["size"] == 1
    assert state["afterBackoff"]["size"] == 1
    # The first retry fires; while backing off, no further attempts are made.
    assert state["after1"]["attempts"] == 1
    assert state["middle"]["attempts"] == 1
    # After the 30 s backoff elapses, one more attempt, and the delay doubles.
    assert state["afterBackoff"]["attempts"] == 2
    assert state["afterBackoff"]["next"] == 1000000 + 31000 + 60000


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_poll_keeps_every_pending_completion_and_batches_the_retry():
    """51 failed enrichments are all kept; one retry request covers only the batch."""
    polling = _extract_function(PANELS_JS, "startCronPolling")
    remember = _extract_function(PANELS_JS, "_cronRememberPendingDetails")
    retry = _extract_function(PANELS_JS, "_cronRetryPendingDetails")
    backoff = _extract_function(PANELS_JS, "_cronPendingBackoffMs")
    script = (
        _POLL_HARNESS_PREAMBLE
        + f"""
{backoff}
{remember}
{retry}
{polling}
(async()=>{{
  const comps=[];
  for(let i=0;i<51;i+=1) comps.push({{job_id:'job-'+i,completed_at:1000+i,name:'J'+i,status:'success',toast_notifications:false}});
  responses=[{{completions:comps,session_lookup_failed:true}}];
  global.api=async(url)=>{{ urls.push(url); if(responses.length) return responses.shift(); return {{completions:[],session_lookup_failed:true}}; }};
  startCronPolling();
  await intervalCallback();
  let attempted=0, untouched=0;
  for(const e of _cronPendingDetails.values()){{ if(e.attempts>0) attempted+=1; else untouched+=1; }}
  process.stdout.write(JSON.stringify({{size:_cronPendingDetails.size,attempted,untouched}}));
}})().catch(error=>{{ console.error(error); process.exit(1); }});
"""
    )
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout)

    assert state["size"] == 51         # nothing evicted past the old 50-entry cap
    assert state["attempted"] == 50    # one retry request covered only the batch
    assert state["untouched"] == 1     # the newest entry waits for the next batch