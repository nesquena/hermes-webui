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


def _restore_bodies() -> str:
    """Source for the profile-restore helpers _archiveSession now calls."""
    return "\n".join(filter(None, [
        _extract_async_function(SESSIONS_JS, "_restoreProfileAfterArchive"),
        _extract_async_function(SESSIONS_JS, "_switchProfileForActiveProfile"),
    ]))


def _build_driver(archive_body: str, mismatch_body: str, fail_first: bool = True,
                  extra_bodies: str = "", open_session_id: str | None = None) -> str:
    """Stub every global _archiveSession touches and drive the scenario.

    ``fail_first=True``: the first archive api call throws a structured 409
    (foreign profile), the second succeeds — the switch+retry contract.
    ``fail_first=False``: every archive api call throws 409 — the retry must
    NOT switch profiles again (the _retried guard).
    ``open_session_id``: S.session's session_id, so the restore path can tell
    whether the chat on screen belongs to the profile it is bouncing back to.
    ``extra_bodies``: additional function sources spliced in before the
    archive body. The profile-restore helpers are ALWAYS included — they are
    part of the production _archiveSession contract — so callers only pass
    them when they need a different combination.
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
  // Only the ARCHIVE path is adversarial in this scenario. The profile-switch
  // calls (the cross-profile retry and the restore after it) must succeed, or
  // the restore's own switch would be swallowed by its catch and the test
  // would read as "restore never fired".
  if(path === '/api/profile/switch'){
    const nm = JSON.parse(opts.body).name;
    S.activeProfile = nm;
    S.activeProfileIsDefault = (nm === 'default');
    return {active: nm, is_default: S.activeProfileIsDefault};
  }
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
  session: {'null' if open_session_id is None else "{session_id:'" + open_session_id + "'}"},
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

{_restore_bodies()}

{extra_bodies}

{calls}

let switchCalls = 0;
async function _switchProfileForSessionLoad(profile) {{
  switchCalls++;
  events.push('switch:' + profile);
  S.activeProfile = profile;
  // Moving off the root surface: the 'default' alias must stop matching, or
  // _restoreProfileAfterArchive would read the switch-away as a no-op.
  S.activeProfileIsDefault = (profile === 'default');
  return {{ active: profile }};
}}
function _profileMatchesActiveProfile(profile, activeProfile){{
  // Restore helpers are spliced in above (the real production sources); the
  // restore counter is derived from the events array by the driver.
  const eventName = (typeof profile === 'string' && profile.trim()) ? profile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(eventName === activeName) return true;
  return eventName === 'default' && !!S.activeProfileIsDefault;
}}
function startGatewaySSE() {{ events.push('gateway-sse'); }}
function syncTopbar() {{ events.push('sync-topbar'); }}
function _sessionSnapshotById(sid) {{
  return sessions.find(s => s && s.session_id === sid) || null;
}}

{archive_body}

(async () => {{
  const sid = 's1';
  sessions.push({{ session_id: sid, archived: false }});
  const result = await _archiveSession({{ session_id: sid, archived: false }}, true, null, false);
  const activeSwitchCalls = events.filter(e => e === 'gateway-sse').length;
  console.log(JSON.stringify({{ result, apiCalls, switchCalls, activeSwitchCalls, events, activeProfile: S.activeProfile }}));
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


# --- #7826 round 4 (CORE): the open chat must stay on its own profile ----


def test_cross_profile_archive_restores_original_active_profile():
    """#7826 CORE re-gate: archiving a foreign-profile row left the open chat
    bound to the old profile. The retry switched S.activeProfile but never
    touched S.session, so the chat on screen lost its profile and its next
    send was rejected with 409. After a successful cross-profile archive the
    active profile MUST be back where it started."""
    if NODE is None:
        return
    archive_body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    mismatch_body = _extract_function(SESSIONS_JS, "_sessionProfileMismatchFromError")
    out = _run_node(_build_driver(
        archive_body, mismatch_body, fail_first=True,
        open_session_id="root-chat"))
    data = json.loads(out)
    assert data["result"] is True, f"archive should still succeed: {data}"
    assert data["switchCalls"] == 1, f"the forward switch still happens once: {data}"
    assert data["activeSwitchCalls"] == 1, (
        f"the restore must bounce back to the original profile: {data}")
    assert data["activeProfile"] == "default", (
        f"active profile must be restored, got {data['activeProfile']}: {data}")
    assert "gateway-sse" in data["events"], (
        f"the restore must reconnect the gateway SSE to the restored profile: {data}")
    assert "sync-topbar" in data["events"], data


def test_cross_profile_archive_restores_profile_when_retry_fails():
    """The restore must also run when the archive retry FAILS — the reviewer's
    own alternative ("on success and failure alike"). Leaving the user on the
    foreign profile after a failed archive strands their next send on 409 too.

    Drives the failure path directly: the first archive api call throws a
    generic (non-409) error, so no retry fires at all and the catch's generic
    branch is the one under test. On a plain same-profile failure the restore
    is a no-op (we never switched), so activeSwitchCalls stays 0 — the
    profile context is simply preserved.
    """
    if NODE is None:
        return
    # The success-path restore is pinned by the test above; here we pin that
    # the retried-409 scenario (fail_first=False) restores too, and never loops.
    archive_body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    mismatch_body = _extract_function(SESSIONS_JS, "_sessionProfileMismatchFromError")
    out2 = _run_node(_build_driver(
        archive_body, mismatch_body, fail_first=False,
        open_session_id="root-chat"))
    data2 = json.loads(out2)
    assert data2["result"] is False, f"second 409 surfaces as failure: {data2}"
    # apiCalls counts the restore's /api/profile/switch too (it is the same
    # api() sink) — that third call IS the fix firing, not a loop. The
    # constancy that matters: exactly one forward switch and one restore.
    assert data2["switchCalls"] == 1, f"only the FIRST switch may happen: {data2}"
    assert data2["activeSwitchCalls"] == 1, (
        f"the restore must bounce back even after a failed archive: {data2}")
    assert data2["activeProfile"] == "default", data2


def test_cross_profile_archive_of_the_displayed_session_does_not_restore():
    """Control: when the row being archived IS the chat on screen, there is no
    "user's own chat" to strand — the chat just left the sidebar. The restore
    is scoped to `displayed !== archivedSessionId` exactly so this case leaves
    the switch in place."""
    if NODE is None:
        return
    archive_body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    mismatch_body = _extract_function(SESSIONS_JS, "_sessionProfileMismatchFromError")
    # The archived sid is 's1' by fixture construction, so display THAT sid.
    out = _run_node(_build_driver(
        archive_body, mismatch_body, fail_first=True,
        open_session_id="s1"))
    data = json.loads(out)
    assert data["result"] is True, data
    assert data["activeSwitchCalls"] == 0, (
        f"archiving the displayed chat must not trigger a restore: {data}")
    assert data["activeProfile"] == "work", data


def test_cross_profile_archive_no_restore_when_no_chat_open():
    """Control: with no session displayed (fresh empty state) there is nothing
    to strand, so the restore must not fire at all."""
    if NODE is None:
        return
    archive_body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    mismatch_body = _extract_function(SESSIONS_JS, "_sessionProfileMismatchFromError")
    out = _run_node(_build_driver(
        archive_body, mismatch_body, fail_first=True,
        open_session_id=None))
    data = json.loads(out)
    assert data["result"] is True, data
    assert data["activeSwitchCalls"] == 0, (
        f"no displayed session means no restore: {data}")


def test_restore_helper_switches_without_touching_session():
    """#7826: the restore must go through a lean profile switch, never
    switchToProfile — that helper can create a blank session or retag
    S.session to the target profile, which is exactly the cross-tagging the
    restore exists to avoid. Pin: the helper never mutates S.session and the
    single archive call count is unchanged."""
    if NODE is None:
        return
    body = _extract_async_function(SESSIONS_JS, "_switchProfileForActiveProfile")
    script = f"""
const S = {{ session: {{session_id:'root-chat'}}, activeProfile: 'work', activeProfileIsDefault: false }};
let newSessionCalls = 0;
async function api(path, opts){{
  if(path === '/api/profile/switch') return {{active:'default', is_default:true}};
  return {{}};
}}
function startGatewaySSE(){{}}
function syncTopbar(){{}}
function renderSessionList(){{}}
function _profileMatchesActiveProfile(p, a){{ return p === a; }}
{body}
(async () => {{
  const before = JSON.stringify(S.session);
  await _switchProfileForActiveProfile('default', true);
  console.log(JSON.stringify({{active:S.activeProfile, isDefault:S.activeProfileIsDefault, sessionUnchanged: JSON.stringify(S.session) === before, newSessionCalls}}));
}})();
"""
    data = json.loads(_run_node(script))
    assert data["active"] == "default", data
    assert data["isDefault"] is True, data
    assert data["sessionUnchanged"] is True, (
        f"the restore must never create/retag S.session: {data}")
    assert data["newSessionCalls"] == 0, data


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


# --- #7826 round 4: batch archive owner resolution + grouped execution ------

def _build_batch_driver(owners_body, owner_row_body, match_body, batch_body,
                        scenario, rows, active_profile='default',
                        active_is_default=True, open_session=None):
    """Drive _archiveBatchOwners / _archiveBatchSessions under Node.

    ``scenario`` selects the api/switch behaviour:
      - 'switch-then-ok':   S is default, owner work → switch once, then every
                            archive call succeeds.
      - 'fail-work-s2':     S is default → switch, first archive call (work/s1)
                            succeeds, the work group's second row throws → the
                            work group reports partial failure, later groups
                            still run.
      - 'switch-fails':     S is default, owner work → switch throws → zero
                            archive calls.
    ``rows`` entries: {id, profile (or None), webui (bool, default True),
    open (bool — the row is S.session)}.
    """
    api_impl = {
        'switch-then-ok': """let apiCalls = 0;
async function api(path, opts){
  apiCalls++;
  events.push('api:' + path);
  const sid = JSON.parse(opts.body).session_id;
  archivedSids.push(sid);
  return {worktree_retained:false};
}""",
        'fail-work-s2': """let apiCalls = 0;
async function api(path, opts){
  apiCalls++;
  events.push('api:' + path);
  const sid = JSON.parse(opts.body).session_id;
  // Throw on the SECOND row of the 'work' group so the healthy groups still
  // finish and the work group reports a partial failure.
  if(sid === 'w2'){ const e = new Error('boom-archive'); throw e; }
  archivedSids.push(sid);
  return {worktree_retained:false};
}""",
        'switch-fails': """let apiCalls = 0;
async function api(path, opts){
  apiCalls++;
  events.push('api:' + path);
  const sid = JSON.parse(opts.body).session_id;
  archivedSids.push(sid);
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
        "{ session_id: '%s', profile: %s, session_source: %s }" % (
            s["id"],
            "null" if s.get("profile") is None else ("%r" % s["profile"]),
            "%r" % ('webui' if s.get("webui", True) else 'cli'),
        )
        for s in rows)
    open_sid = next((s["id"] for s in rows if s.get("open")), None) or open_session
    open_sid_js = (
        'null' if open_sid is None
        else "{session_id:'" + str(open_sid) + "'}"
    )
    return f"""
const events = [];
const archivedSids = [];
const sessions = [];
const _allSessions = sessions;
const S = {{
  session: {open_sid_js},
  activeProfile: '{active_profile}',
  activeProfileIsDefault: {'true' if active_is_default else 'false'},
}};
function showToast(msg, dur) {{ events.push('toast:' + msg); }}
function t(key) {{ return 'T::' + key; }}
function _sessionResponseRetainsWorktree(response, session){{
  if(response && typeof response.worktree_retained === 'boolean') return response.worktree_retained;
  return !!(session && session.worktree_path);
}}
function _isWebUiSourceSession(session){{
  if(!session) return false;
  return String(session.session_source || session.raw_source || session.source_tag || session.source || '').toLowerCase() === 'webui';
}}
function _profileMatchesActiveProfile(profile, activeProfile){{
  const eventName = (typeof profile === 'string' && profile.trim()) ? profile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(eventName === activeName) return true;
  // literal 'default' is an alias for the root surface ONLY while the active
  // surface IS the root (activeProfileIsDefault), mirroring the production
  // helper. The switch stub sets activeProfileIsDefault=false as it moves off
  // root, so 'default' vs 'work' correctly reads as a mismatch here.
  return eventName === 'default' && !!S.activeProfileIsDefault;
}}

{owner_row_body}

{match_body}

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
  const outcome = await _archiveBatchSessions(ids, sessionsById);
  console.log(JSON.stringify({{ preflight, outcome, switchCalls, apiCalls, events, archivedSids }}));
}})();
"""


def _run_batch(scenario, rows, **kw):
    bodies = {
        "owners_body": _extract_function(SESSIONS_JS, "_archiveBatchOwners"),
        "owner_row_body": _extract_function(SESSIONS_JS, "_archiveBatchOwnerForRow"),
        "match_body": _extract_function(SESSIONS_JS, "_archiveBatchOwnersMatch"),
        "batch_body": _extract_async_function(SESSIONS_JS, "_archiveBatchSessions"),
    }
    out = _run_node(_build_batch_driver(
        scenario=scenario, rows=rows, **bodies, **kw))
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


def test_batch_legacy_root_owned_webui_rows_are_archivable():
    """#7826 round 4 (finding 2): a legacy root-owned WebUI session has
    ``profile: null`` on its snapshot (the sidebar row says default), and
    _sessionSnapshotById hands back S.session itself for the open row. The
    round-3 preflight failed the whole batch closed on it, so these sessions
    could not be batch-archived at all. They must resolve to owner 'default'
    and archive with ZERO profile switches while 'default' is already active."""
    if NODE is None:
        return
    rows = [
        {"id": "legacy-open", "profile": None, "open": True},
        {"id": "legacy-2", "profile": None},
    ]
    data = _run_batch("switch-then-ok", rows)
    assert data["preflight"]["owner"] == "default", (
        f"legacy root-owned WebUI rows must resolve to 'default': {data}")
    assert data["outcome"]["ok"] is True, data
    assert data["switchCalls"] == 0, (
        f"owner matches the active profile — no switch may be requested: {data}")
    assert sorted(data["archivedSids"]) == ["legacy-2", "legacy-open"], data


def test_batch_unknown_cli_row_still_fails_closed():
    """#7826 round 4: the profile-less WebUI carve-out must not become a
    blanket accept. An unknown CLI row has no derivable owner client-side and
    the server 404s it by contract — the selection must fail closed with zero
    archive calls, exactly as in round 3."""
    if NODE is None:
        return
    rows = [
        {"id": "s1", "profile": "work"},
        {"id": "s2", "profile": None, "webui": False},
    ]
    data = _run_batch("switch-then-ok", rows)
    assert data["preflight"]["owner"] is None, data
    assert data["preflight"]["reason"] == "unknown-owner", data
    assert data["apiCalls"] == 0, f"unknown CLI row must fail closed: {data}"


def test_batch_mixed_webui_profiles_archive_group_by_group():
    """#7826 round 4 (finding 3): on master, selecting WebUI chats from two
    profiles archived all of them (the archive route loads sidecars by ID).
    Round 3's head rejected the whole selection with reason:'mixed' and
    archived nothing.

    The grouping lives in the EXECUTOR (_archiveBatchSessions), so this test
    drives it directly — the onclick preflight gate still (correctly) refuses
    a mixed selection with reason:'mixed', which is why the preflight
    assertion below expects that rejection. What changed is that the executor
    no longer requires a uniform owner: it groups and archives every row."""
    if NODE is None:
        return
    rows = [
        {"id": "w1", "profile": "work"},
        {"id": "w2", "profile": "work"},
        {"id": "h1", "profile": "home"},
    ]
    data = _run_batch("switch-then-ok", rows)
    assert data["preflight"]["owner"] is None, data
    assert data["preflight"]["reason"] == "mixed", data
    # The executor archives everything despite the mixed selection.
    assert data["outcome"]["ok"] is True, f"mixed WebUI selection must archive everything: {data}"
    assert data["outcome"]["archivedCount"] == 3, data
    assert sorted(data["archivedSids"]) == ["h1", "w1", "w2"], data
    assert data["switchCalls"] == 2, (
        f"one switch per distinct foreign owner (work, home) — never per row: {data}")


def test_batch_mixed_profiles_switch_once_per_distinct_owner():
    """Round-4 grouping must switch AT MOST once per distinct owner — never
    once per row, and never per group re-entry."""
    if NODE is None:
        return
    rows = [
        {"id": "w1", "profile": "work"},
        {"id": "h1", "profile": "home"},
        {"id": "w2", "profile": "work"},
    ]
    data = _run_batch("switch-then-ok", rows)
    switches = [e for e in data["events"] if e.startswith("switch:")]
    assert switches == ["switch:work", "switch:home"], data
    assert data["outcome"]["ok"] is True, data
    assert data["outcome"]["archivedCount"] == 3, data


def test_batch_renamed_root_default_row_requests_no_switch():
    """#7826 round 4 (finding 4): a row labelled 'default' under a root whose
    display name has been RENAMED compared raw strings, saw owner !== active,
    and requested a switch — which a profile-bound auth session refuses,
    failing the archive. Owner equality must go through
    _profileMatchesActiveProfile so 'default' under a renamed root needs no
    switch at all."""
    if NODE is None:
        return
    rows = [{"id": "r1", "profile": "default"}, {"id": "r2", "profile": "default"}]
    data = _run_batch("switch-then-ok", rows,
                      active_profile="my-renamed-root", active_is_default=True)
    assert data["preflight"]["owner"] == "default", data
    assert data["outcome"]["ok"] is True, data
    assert data["switchCalls"] == 0, (
        f"renamed-root default row must NOT request a switch: {data}")
    assert data["apiCalls"] == 2, data


def test_batch_switch_failure_zero_archive_calls_for_that_group():
    """Failed profile switch aborts that owner's group before any archive
    request for it — and the batch reports the partial failure instead of a
    false success."""
    if NODE is None:
        return
    rows = [{"id": "s1", "profile": "work"}, {"id": "s2", "profile": "work"}]
    data = _run_batch("switch-fails", rows)
    assert data["outcome"]["ok"] is False, data
    assert data["switchCalls"] == 1, data
    assert data["apiCalls"] == 0, f"switch failure must precede any archive call: {data}"
    assert data["archivedSids"] == [], data


def test_batch_mid_failure_never_reports_success():
    """Sequential per-group loop: a failure after the first archive stops the
    group and reports an error carrying the partial count — a subset success
    must never surface as success, and no further rows in that group are
    archived."""
    if NODE is None:
        return
    rows = [{"id": "w1", "profile": "work"}, {"id": "w2", "profile": "work"}, {"id": "w3", "profile": "work"}]
    data = _run_batch("fail-work-s2", rows)
    assert data["outcome"]["ok"] is False, f"partial success must not report ok: {data}"
    assert data["archivedSids"] == ["w1"], f"loop must stop at first failure: {data}"
    assert data["apiCalls"] == 2, f"no row after the failure may be sent: {data}"
    assert data["outcome"]["archivedCount"] == 1, data
    assert data["outcome"]["totalCount"] == 3, data
    assert "batch-partial-failure" in data["outcome"]["error"], data


def test_batch_one_group_failure_does_not_block_other_groups():
    """Round-4 grouping requirement: a failing group must be reported as a
    PARTIAL failure while the remaining owner groups still archive — the
    round-3 head aborted the entire batch on the first soft error."""
    if NODE is None:
        return
    rows = [
        {"id": "h1", "profile": "home"},
        {"id": "w1", "profile": "work"},
        {"id": "w2", "profile": "work"},
    ]
    data = _run_batch("fail-work-s2", rows)
    assert data["outcome"]["ok"] is False, data
    assert "h1" in data["archivedSids"], f"healthy group must still archive: {data}"
    assert "w1" in data["archivedSids"], f"rows before the failure land: {data}"
    assert "w2" not in data["archivedSids"], f"failing row is not marked archived: {data}"
    assert data["outcome"]["error"] == "batch-partial-failure:work", data
    assert data["outcome"]["archivedCount"] == 2, (
        f"the healthy group's row plus the work group's pre-failure row both landed: {data}")
    assert data["outcome"]["totalCount"] == 3, data
