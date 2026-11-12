"""Regression tests for #6728: active cron sessions wrongly appear as completed.

The sidebar polls /api/sessions (never /api/crons/status), so cron liveness
must be stamped onto the session-list rows. A still-running cron job's row
must carry ``cron_running=True`` so the client defers its completion/unread
transition; a finished (or non-cron) row must not. Because cron session ids
are ``cron_{job_id}_{run_timestamp}``, only the run whose ``created_at`` is at
or after the tracked start belongs to the live execution — older runs of the
same job must stay completed.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent
SESSIONS_JS_PATH = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = pathlib.Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _extract_func_script(js: str) -> str:
    # Brace-matches a function body while skipping braces inside string / template
    # / regex literals and comments (same hardened extractor as the sibling #5744
    # suite).
    prelude = "const src = " + json.dumps(js) + ";\n"
    body = r"""
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  let str = null;
  let inLine = false;
  let inBlock = false;
  let inRegex = false;
  let prev = '';
  while (depth > 0 && i < src.length) {
    const c = src[i];
    const n = src[i + 1];
    if (inLine) { if (c === '\n') inLine = false; i++; continue; }
    if (inBlock) { if (c === '*' && n === '/') { inBlock = false; i++; } i++; continue; }
    if (str) {
      if (c === '\\') { i += 2; continue; }
      if (c === str) str = null;
      i++; continue;
    }
    if (inRegex) {
      if (c === '\\') { i += 2; continue; }
      if (c === '/') inRegex = false;
      i++; continue;
    }
    if (c === '/' && n === '/') { inLine = true; i += 2; continue; }
    if (c === '/' && n === '*') { inBlock = true; i += 2; continue; }
    if (c === '"' || c === "'" || c === '`') { str = c; i++; continue; }
    if (c === '/' && !'})]0123456789'.includes(prev) && !/[A-Za-z_$]/.test(prev)) {
      inRegex = true; i++; continue;
    }
    if (c === '{') depth++;
    else if (c === '}') depth--;
    if (c.trim()) prev = c;
    i++;
  }
  return src.slice(start, i);
}
"""
    return prelude + body


def _js_prelude() -> str:
    """Globals/stubs needed by the extracted sessions.js functions.

    `_markPollingCompletionUnreadTransitions` reads/writes the module-level
    `_sessionStreamingById` / `_sessionListSnapshotById` Maps directly, and
    calls `_isSessionEffectivelyStreaming` (real), `_isSessionActivelyViewedForList`,
    `_getSessionObservedStreaming`, `_markSessionCompletionUnread`,
    `_setSessionViewedCount`, `_rememberObservedStreamingSession` and
    `_forgetObservedStreamingSession`. All the `typeof`-guarded collaborators
    (`_sessionListSourceById`, `_allSessionsScope`, `_rememberSessionListSource`,
    `_cronCompletionUnreadMetaForSession`, `_showAllProfiles`,
    `_cronMarkerProfileMatchesActive`) are intentionally left undefined so the
    guards take their safe branches.
    """
    return r"""
const _sessionStreamingById = new Map();
const _sessionListSnapshotById = new Map();
const _observedStreaming = {};
let markCount = 0;
const markedSids = [];
function _getSessionObservedStreaming() { return _observedStreaming; }
function _markSessionCompletionUnread(sid, messageCount, meta) { markCount += 1; markedSids.push(sid); }
function _setSessionViewedCount(sid, messageCount) {}
function _isSessionActivelyViewedForList(sid) { return false; }
function _rememberObservedStreamingSession(s) {}
function _forgetObservedStreamingSession(sid) {}
let S = { session: null, busy: false };
"""


def _cron_row(sid, created_at, **overrides):
    row = {
        "session_id": sid,
        "title": "Cron Session",
        "profile": "default",
        "created_at": created_at,
        "updated_at": created_at,
        "last_message_at": created_at,
        "message_count": 1,
        "user_message_count": 1,
        "archived": False,
        "project_id": "cron-project",
        "source_tag": "cron",
        "raw_source": "cron",
        "session_source": "cron",
        "source_label": "Cron",
        "is_cli_session": False,
    }
    row.update(overrides)
    return row


def test_overlay_marks_current_running_cron_row(monkeypatch):
    import api.routes as routes
    import api.route_session_list_cache as slc

    # job6728 started at epoch 1000; the current run's session was created at
    # 1100 (>= start) and carries the matching cron_{job_id}_ prefix.
    monkeypatch.setattr(routes, "_RUNNING_CRON_JOBS", {"job6728": 1000.0})
    monkeypatch.setattr(slc, "_session_list_cache_active_stream_ids", lambda: set())

    rows = slc._session_list_cache_overlay_runtime_rows(
        [_cron_row("cron_job6728_20260803_100000", created_at=1100)]
    )
    assert len(rows) == 1
    assert rows[0]["cron_running"] is True


def test_overlay_does_not_mark_finished_or_other_cron_rows(monkeypatch):
    import api.routes as routes
    import api.route_session_list_cache as slc

    # job6728 is running (started at 1000) but:
    # - cron_job9999... belongs to a different job (not tracked)
    # - cron_job6728_20260701... is an OLD run of the same job (created < start)
    monkeypatch.setattr(routes, "_RUNNING_CRON_JOBS", {"job6728": 1000.0})
    monkeypatch.setattr(slc, "_session_list_cache_active_stream_ids", lambda: set())

    rows = slc._session_list_cache_overlay_runtime_rows(
        [
            _cron_row("cron_job9999_20260803_100000", created_at=1100),
            _cron_row("cron_job6728_20260701_050000", created_at=500),
            {"session_id": "regular-chat", "title": "Chat", "is_cli_session": False},
        ]
    )
    by_sid = {row["session_id"]: row for row in rows}
    assert by_sid["cron_job9999_20260803_100000"]["cron_running"] is False
    assert by_sid["cron_job6728_20260701_050000"]["cron_running"] is False
    assert by_sid["regular-chat"]["cron_running"] is False


def test_overlay_fails_closed_when_routes_unavailable(monkeypatch):
    import api.route_session_list_cache as slc

    monkeypatch.setattr(slc, "_session_list_cache_running_cron_jobs", lambda: {})

    rows = slc._session_list_cache_overlay_runtime_rows(
        [_cron_row("cron_job6728_20260803_100000", created_at=1100)]
    )
    assert rows[0]["cron_running"] is False


def test_overlay_nested_job_prefixes_do_not_cross_claim(monkeypatch):
    """Overlapping cron job ids (backup vs backup_full) must not cross-claim.

    ``cron_backup_`` is a valid prefix of ``cron_backup_full_...``, so a
    first-match scan in insertion order would attribute a ``backup_full``
    session to the running ``backup`` job, and a fall-through time-miss would
    let ``backup`` claim an OLD completed ``backup_full`` run. The
    longest-prefix-first scan must attribute each session to its own job with
    no fall-through (mirrors api.routes._latest_cron_session_info_for_jobs).
    """
    import api.routes as routes
    import api.route_session_list_cache as slc

    # Both jobs running; `backup` is inserted FIRST so a first-match scan in
    # insertion order would claim cron_backup_full_... sessions as `backup`.
    monkeypatch.setattr(
        routes, "_RUNNING_CRON_JOBS", {"backup": 1000.0, "backup_full": 2000.0}
    )
    monkeypatch.setattr(slc, "_session_list_cache_active_stream_ids", lambda: set())

    rows = slc._session_list_cache_overlay_runtime_rows(
        [
            _cron_row("cron_backup_20260803_100000", created_at=1100),
            _cron_row("cron_backup_full_20260803_210000", created_at=2100),
            _cron_row("cron_backup_full_20260803_190000", created_at=1900),
        ]
    )
    by_sid = {row["session_id"]: row for row in rows}
    assert by_sid["cron_backup_20260803_100000"]["cron_running"] is True
    assert by_sid["cron_backup_full_20260803_210000"]["cron_running"] is True
    # Old backup_full run created after `backup` started: must NOT fall through
    # to the shorter cron_backup_ prefix (it belongs to a completed run).
    assert by_sid["cron_backup_full_20260803_190000"]["cron_running"] is False

    # Single running job, non-overlapping id: unchanged behavior.
    monkeypatch.setattr(routes, "_RUNNING_CRON_JOBS", {"job6728": 1000.0})
    rows = slc._session_list_cache_overlay_runtime_rows(
        [_cron_row("cron_job6728_20260803_100000", created_at=1100)]
    )
    assert rows[0]["cron_running"] is True


def test_overlay_shorter_running_job_cannot_claim_longer_job_session(monkeypatch):
    """A running `backup` must NOT claim a `backup_full` session when only

    `backup` is running (#6728 gate finding). Prefixes are built ONLY from
    RUNNING jobs, so if `backup_full` is not running, longest-prefix sorting
    never sees the true longer owner and `cron_backup_` would otherwise swallow
    `cron_backup_full_YYYYMMDD_HHMMSS`. The remainder after the matched prefix
    must be EXACTLY a run timestamp, so `full_20260803_210000` is rejected.
    """
    import api.routes as routes
    import api.route_session_list_cache as slc

    # Only `backup` is running; `backup_full` is NOT tracked.
    monkeypatch.setattr(routes, "_RUNNING_CRON_JOBS", {"backup": 1000.0})
    monkeypatch.setattr(slc, "_session_list_cache_active_stream_ids", lambda: set())

    rows = slc._session_list_cache_overlay_runtime_rows(
        [
            _cron_row("cron_backup_20260803_100000", created_at=1100),
            _cron_row("cron_backup_full_20260803_210000", created_at=2100),
        ]
    )
    by_sid = {row["session_id"]: row for row in rows}
    # The genuine `backup` run is live.
    assert by_sid["cron_backup_20260803_100000"]["cron_running"] is True
    # The `backup_full` session must stay completed — `backup`'s prefix leaves
    # `full_20260803_210000`, which is not a bare run timestamp.
    assert by_sid["cron_backup_full_20260803_210000"]["cron_running"] is False


def test_sidebar_response_preserves_cron_running(monkeypatch):
    import api.routes as routes

    row = _cron_row("cron_job6728_20260803_100000", created_at=1100)
    row["cron_running"] = True
    item = routes._sidebar_session_response_item(row)
    assert item.get("cron_running") is True


def test_payload_response_roundtrip_stamps_running_cron(monkeypatch):
    """End-to-end: /api/sessions response rows carry cron_running for live jobs."""
    import api.routes as routes

    raw_cron_row = _cron_row("cron_job6728_20260803_100000", created_at=1100)
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [])
    monkeypatch.setattr(
        routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [raw_cron_row]
    )
    monkeypatch.setattr(
        routes, "_reconcile_stale_stream_state_for_session_rows", lambda _sessions: False
    )
    monkeypatch.setattr(routes, "_RUNNING_CRON_JOBS", {"job6728": 1000.0})

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=True,
    )
    response = routes._session_list_payload_to_response(payload)
    rows = response["sessions"]
    matching = [row for row in rows if row["session_id"] == "cron_job6728_20260803_100000"]
    assert len(matching) == 1
    assert matching[0]["cron_running"] is True


def test_running_cron_row_renders_effectively_streaming():
    """A row with cron_running=true must render ACTIVE in the sidebar.

    Rendering, sorting, polling and completion-transition tracking all key off
    `_isSessionEffectivelyStreaming` (static/sessions.js). Before the fix the
    predicate ignored `cron_running`, so a running cron row rendered visually
    idle and could lose its eventual completion/unread transition.
    """
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _js_prelude() + r"""
eval(extractFunc('_hasPendingUserMessageSignal'));
eval(extractFunc('_isSessionLocallyStreaming'));
eval(extractFunc('_isSessionEffectivelyStreaming'));
const running = { session_id: 'cron_job6728_20260803_100000', cron_running: true, is_streaming: false };
const idle = { session_id: 'cron_job6728_20260701_050000', cron_running: false, is_streaming: false };
console.log(JSON.stringify({
  runningActive: _isSessionEffectivelyStreaming(running),
  idleInactive: _isSessionEffectivelyStreaming(idle),
}));
"""
    m = json.loads(_run_node(source))
    assert m["runningActive"] is True
    assert m["idleInactive"] is False


def test_cron_running_defers_completion_unread_until_flag_clears():
    """A cron_running row with advanced message_count must NOT be marked
    completed-unread; exactly ONE true->false completion transition happens
    when the flag clears.

    The `!cronRunning` guards in `_markPollingCompletionUnreadTransitions`
    defer all three completion signals while the job runs; the focused
    regression proves the row is only marked once, on the poll where
    cron_running flips to false.
    """
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _js_prelude() + r"""
eval(extractFunc('_hasPendingUserMessageSignal'));
eval(extractFunc('_isSessionLocallyStreaming'));
eval(extractFunc('_isSessionEffectivelyStreaming'));
eval(extractFunc('_markPollingCompletionUnreadTransitions'));
const sid = 'cron_job6728_20260803_100000';
_sessionListSnapshotById.set(sid, { message_count: 1, last_message_at: 1000 });
let row = {
  session_id: sid,
  cron_running: true,
  is_streaming: false,
  message_count: 2,
  last_message_at: 1100,
};
// Poll 1: cron still running, message_count advanced -> no completion mark.
_markPollingCompletionUnreadTransitions([row]);
const marksWhileRunning = markCount;
const streamWhileRunning = _sessionStreamingById.get(sid);
// Poll 2: cron flag clears -> exactly one true->false completion transition.
row.cron_running = false;
_markPollingCompletionUnreadTransitions([row]);
const marksAfterClear = markCount;
const streamAfterClear = _sessionStreamingById.get(sid);
console.log(JSON.stringify({
  marksWhileRunning,
  marksAfterClear,
  streamWhileRunning,
  streamAfterClear,
  markedSids,
}));
"""
    m = json.loads(_run_node(source))
    assert m["marksWhileRunning"] == 0, (
        "cron_running row with advanced message_count must not be marked completed-unread"
    )
    assert m["marksAfterClear"] == 1, "exactly one completion transition when the flag clears"
    assert m["streamWhileRunning"] is True, "row must render active while cron_running"
    assert m["streamAfterClear"] is False
    assert m["markedSids"] == ["cron_job6728_20260803_100000"]
