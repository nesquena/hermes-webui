"""#7826: the archive client must consume the structured 409 envelope.

The all-profiles sidebar offers archives for sessions owned by another
profile; the archive endpoint answers those with
``409 session_profile_mismatch`` (matching the detail-load contract #7710).
The pre-fix client swallowed every archive error as a generic failure, so a
foreign-profile archive just toasted "failed" and left the session
unarchived. The fix switches to the owning profile and retries exactly once,
guarded against infinite recursion.

These are behaviour tests: the real ``_archiveSession`` body is extracted
from ``static/sessions.js`` and run under Node with stubbed globals, so a
future refactor that keeps the literals but breaks the flow fails here.
"""
import json
from pathlib import Path
import shutil
import subprocess

SESSIONS_JS = (Path(__file__).resolve().parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_async_function(source: str, name: str) -> str:
    start = source.find(f"async function {name}(")
    assert start != -1, f"Could not find async function {name}"
    brace = source.find("{", start)
    assert brace != -1, f"Could not find opening brace for {name}"
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:idx + 1]
    raise AssertionError(f"Could not extract complete function body for {name}")


def _extract_function(source: str, name: str) -> str:
    start = source.find(f"function {name}(")
    assert start != -1, f"Could not find function {name}"
    brace = source.find("{", start)
    assert brace != -1, f"Could not find opening brace for {name}"
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:idx + 1]
    raise AssertionError(f"Could not extract complete function body for {name}")


def _run_node(script: str) -> str:
    assert NODE, "node not available"
    proc = subprocess.run(
        [NODE, "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}\n{proc.stdout}"
    return proc.stdout.strip()


def _build_driver(archive_body: str, mismatch_body: str, fail_first: bool = True) -> str:
    """Stub every global _archiveSession touches and drive the scenario.

    ``fail_first=True``: the first archive api call throws a structured 409
    (foreign profile), the second succeeds — the switch+retry contract.
    ``fail_first=False``: every archive api call throws 409 — the retry must
    NOT switch profiles again (the _retried guard).
    """
    calls = ""
    if fail_first:
        calls = """
let apiCalls = 0;
async function api(path, opts){
  apiCalls++;
  if(apiCalls === 1){
    const e = new Error('409');
    e.status = 409;
    e.body = JSON.stringify({code:'session_profile_mismatch',profile:'work',session_id:'s1'});
    throw e;
  }
  return {ok:true};
}"""
    else:
        calls = """let apiCalls = 0;
async function api(path, opts){
  apiCalls++;
  const e = new Error('409');
  e.status = 409;
  e.body = JSON.stringify({code:'session_profile_mismatch',profile:'work',session_id:'s1'});
  throw e;
}"""
    return f"""
const events = [];
const sessions = [];
const _allSessions = sessions;
const S = {{
  session: null,
  activeProfile: 'default',
  activeProfileIsDefault: true,
}};
const localStorage = {{
  _m: new Map(),
  getItem(k) {{ return this._m.has(k) ? this._m.get(k) : null; }},
  setItem(k, v) {{ this._m.set(k, v); }},
  removeItem(k) {{ this._m.delete(k); }},
}};
function showToast(msg, dur) {{ events.push('toast:' + msg); }}
function t(key) {{ return 'T::' + key; }}
function _sessionArchiveToast(r, s) {{ return 'archive-toast'; }}
function renderSessionListFromCache() {{ events.push('render-cache'); }}
function renderSessionList() {{ events.push('render-full'); }}
function _captureSessionReflowPositions() {{ return 'reflow'; }}
function _sessionPrefersReducedMotion() {{ return false; }}
function _isReadOnlySession() {{ return false; }}
const _showArchived = true;
const _sessionSwipeReturnOffsets = {{ _m: new Map(), set(k, v) {{ this._m.set(k, v); }} }};
let _pendingSessionReflowPositions = null;

{mismatch_body}

{calls}

let switchCalls = 0;
async function _switchProfileForSessionLoad(profile) {{
  switchCalls++;
  events.push('switch:' + profile);
  S.activeProfile = profile;
  return {{ active: profile }};
}}
function _sessionSnapshotById(sid) {{
  return sessions.find(s => s && s.session_id === sid) || null;
}}

{archive_body}

(async () => {{
  const sid = 's1';
  sessions.push({{ session_id: sid, archived: false }});
  const result = await _archiveSession({{ session_id: sid, archived: false }}, true, null, false);
  console.log(JSON.stringify({{ result, apiCalls, switchCalls, events }}));
}})();
"""


def test_archive_session_switches_profile_and_retries_once_on_409():
    """The first 409 triggers one profile switch + one archive retry."""
    if NODE is None:
        return  # no node on this box
    archive_body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    mismatch_body = _extract_function(SESSIONS_JS, "_sessionProfileMismatchFromError")
    out = _run_node(_build_driver(archive_body, mismatch_body, fail_first=True))
    data = json.loads(out)
    assert data["result"] is True, f"retry should succeed: {data}"
    assert data["apiCalls"] == 2, f"one switch means exactly 2 api calls: {data}"
    assert data["switchCalls"] == 1, f"exactly one profile switch: {data}"
    switch_event = [e for e in data["events"] if e.startswith("switch:")]
    assert switch_event == ["switch:work"], data


def test_archive_session_does_not_loop_when_second_attempt_409s():
    """A 409 on the retried attempt must NOT switch profiles again — the
    _retried guard breaks the recursion."""
    if NODE is None:
        return
    archive_body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    mismatch_body = _extract_function(SESSIONS_JS, "_sessionProfileMismatchFromError")
    out = _run_node(_build_driver(archive_body, mismatch_body, fail_first=False))
    data = json.loads(out)
    assert data["result"] is False, f"second 409 should surface as failure: {data}"
    assert data["switchCalls"] == 1, f"only the FIRST switch may happen: {data}"
    assert data["apiCalls"] == 2, f"exactly one retry, no loop: {data}"


def test_archive_switches_before_retry_only_for_409():
    """Static contract: the retry path must be gated on the structural 409
    envelope AND the recursion guard — a non-409 error must go straight to
    the generic failure toast without touching _switchProfileForSessionLoad."""
    body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    # the mismatch decode + guard must appear BEFORE any profile switch
    assert "_sessionProfileMismatchFromError(err)" in body
    assert "_switchProfileForSessionLoad(profileMismatch.profile)" in body
    assert "!_retried" in body, "retry must carry the recursion guard"
    assert body.index("_sessionProfileMismatchFromError(err)") < body.index(
        "_switchProfileForSessionLoad(profileMismatch.profile)"), (
        "mismatch decode must precede the profile switch")
    # the generic failure branch must be the un-guarded fallthrough
    assert "showToast(t('session_archive_failed')+err.message)" in body


# --- #7826 round 3: batch archive preflight + sequential execution -------

def _build_batch_driver(owners_body, batch_body, scenario, rows):
    """Drive _archiveBatchOwners / _archiveBatchSessions under Node.

    ``scenario`` selects the api/switch behaviour:
      - 'switch-then-ok':    S is default, owner work → switch once, then every
                             archive call succeeds.
      - 'fail-last':         S is default, owner work → switch once, first
                             archive call succeeds, second throws → batch must
                             stop and report error (no false success).
      - 'switch-fails':      S is default, owner work → switch throws → zero
                             archive calls.
    """
    api_impl = {
        'switch-then-ok': """let apiCalls = 0;
async function api(path, opts){
  apiCalls++;
  events.push('api:' + path);
  return {worktree_retained:false};
}""",
        'fail-last': """let apiCalls = 0;
async function api(path, opts){
  apiCalls++;
  events.push('api:' + path);
  if(apiCalls === 2){ const e = new Error('boom-archive'); throw e; }
  return {worktree_retained:false};
}""",
        'switch-fails': """let apiCalls = 0;
async function api(path, opts){
  apiCalls++;
  events.push('api:' + path);
  return {worktree_retained:false};
}""",
    }[scenario]
    switch_impl = {
        'switch-fails': """async function _switchProfileForSessionLoad(profile){
  switchCalls++;
  events.push('switch:' + profile);
  const e = new Error('boom-switch');
  throw e;
}""",
    }.get(scenario, """async function _switchProfileForSessionLoad(profile){
  switchCalls++;
  events.push('switch:' + profile);
  S.activeProfile = profile;
  return {active: profile};
}""")
    rows_js = ", ".join(
        "{ session_id: '%s', profile: %s }" % (
            s["id"],
            "null" if s.get("profile") is None else ("%r" % s["profile"]),
        )
        for s in rows)
    return f"""
const events = [];
const sessions = [];
const _allSessions = sessions;
const S = {{
  session: null,
  activeProfile: 'default',
  activeProfileIsDefault: true,
}};
function showToast(msg, dur) {{ events.push('toast:' + msg); }}
function t(key) {{ return 'T::' + key; }}
function _sessionResponseRetainsWorktree(response, session){{
  if(response && typeof response.worktree_retained === 'boolean') return response.worktree_retained;
  return !!(session && session.worktree_path);
}}
{owners_body}

let switchCalls = 0;
{switch_impl}
{api_impl}

{batch_body}

(async () => {{
  const rows = [{rows_js}];
  const sessionsById = new Map(rows.map(r => [r.session_id, r]));
  const ids = rows.map(r => r.session_id);
  const preflight = _archiveBatchOwners(ids, sessionsById);
  const outcome = await _archiveBatchSessions(ids, sessionsById, preflight.owner);
  console.log(JSON.stringify({{ preflight, outcome, switchCalls, apiCalls, events }}));
}})();
"""


def _run_batch(scenario, rows):
    owners_body = _extract_function(SESSIONS_JS, "_archiveBatchOwners")
    batch_body = _extract_async_function(SESSIONS_JS, "_archiveBatchSessions")
    out = _run_node(_build_batch_driver(owners_body, batch_body, scenario=scenario, rows=rows))
    return json.loads(out)


def test_batch_foreign_owner_switches_once_then_archives():
    """Two rows in one foreign profile: one switch, then two successful
    archive calls — never a switch per row."""
    if NODE is None:
        return
    rows = [{"id": "s1", "profile": "work"}, {"id": "s2", "profile": "work"}]
    data = _run_batch("switch-then-ok", rows)
    assert data["preflight"]["owner"] == "work", data
    assert data["outcome"]["ok"] is True, data
    assert data["switchCalls"] == 1, f"exactly one switch for uniform foreign batch: {data}"
    assert data["apiCalls"] == 2, f"both rows archived: {data}"
    assert data["outcome"]["retainedCount"] == 0, data


def test_batch_mixed_owners_zero_archive_calls():
    """Rows from two known profiles: preflight rejects with zero archive
    calls — the onclick guard returns before _archiveBatchSessions runs."""
    if NODE is None:
        return
    rows = [{"id": "s1", "profile": "work"}, {"id": "s2", "profile": "home"}]
    data = _run_batch("switch-then-ok", rows)
    assert data["preflight"]["owner"] is None, data
    assert data["preflight"]["reason"] == "mixed", data
    assert data["apiCalls"] == 0, f"no archive call may fire for mixed selection: {data}"


def test_batch_known_plus_profileless_zero_archive_calls():
    """A known row plus a profile-less row must fail closed with zero
    archive calls — the truncated owner set used to slip past the old
    length>1 check via .filter(Boolean)."""
    if NODE is None:
        return
    rows = [{"id": "s1", "profile": "work"}, {"id": "s2", "profile": None}]
    data = _run_batch("switch-then-ok", rows)
    assert data["preflight"]["owner"] is None, data
    assert data["preflight"]["reason"] == "missing-or-profileless", data
    assert data["apiCalls"] == 0, f"profile-less row must fail closed: {data}"


def test_batch_switch_failure_zero_archive_calls():
    """Failed profile switch aborts the batch before any archive request."""
    if NODE is None:
        return
    rows = [{"id": "s1", "profile": "work"}, {"id": "s2", "profile": "work"}]
    data = _run_batch("switch-fails", rows)
    assert data["outcome"]["ok"] is False, data
    assert data["switchCalls"] == 1, data
    assert data["apiCalls"] == 0, f"switch failure must precede any archive call: {data}"


def test_batch_mid_failure_never_reports_success():
    """Sequential loop: a failure after the first archive stops the batch
    and reports an error — a subset success must never surface as success,
    and no further rows are archived after the failure."""
    if NODE is None:
        return
    rows = [{"id": "s1", "profile": "work"}, {"id": "s2", "profile": "work"}, {"id": "s3", "profile": "work"}]
    data = _run_batch("fail-last", rows)
    assert data["outcome"]["ok"] is False, f"partial success must not report ok: {data}"
    assert data["apiCalls"] == 2, f"loop must stop at first failure (2 calls, s3 never sent): {data}"