"""#7826 root fix: profile-scoped archive — the client never switches profiles.

The all-profiles sidebar offers archives for sessions owned by another
profile. The pre-fix client switched the ACTIVE profile to the owner, archived,
then bounced back via a 90-line restore pipeline that leaked across four
review rounds (no-chat-open early-return, failed-row mis-counting as archived,
switch-before-request). The root fix removes the whole switch/restore
pipeline: the archive request carries the row's OWNER profile, and the server
resolves/validates against that profile. The client never calls
_switchProfileForSessionLoad from any archive path.

Round 5 (maintainer CHANGES_REQUESTED at 1c87a20578b5) removed one more
layer: the client-side retry that re-POSTed with the 409 envelope's profile.
It re-issued a WRITE against a session the server had just denied, so a
denied archive could still rewrite a foreign profile's transcript. A
structured 409 now surfaces as a failure with exactly ONE request.

These are behaviour tests: the real ``_archiveSession`` /
``_archiveBatchSessions`` bodies are extracted from ``static/sessions.js``
and run under Node with stubbed globals, so a future refactor that keeps the
literals but breaks the flow fails here.

Revert-sensitivity note: the batch driver deliberately defines NO
``_restoreProfileAfterArchive`` stand-in. If the production executor ever
regresses to calling it, the driver throws ReferenceError under Node and the
test goes RED — the stand-in that masked the real logic for four rounds is
gone with the logic itself.
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


def _build_driver(archive_body: str, mismatch_body: str, fail_mode: str = 'first',
                  open_session_id: str | None = "root-chat",
                  session_profile: str | None = None,
                  show_all_profiles: bool = False) -> str:
    """Stub every global _archiveSession touches and drive the scenario.

    ``fail_mode``:
      - 'first':  the first archive api call throws a structured 409
                  (foreign profile) — the request must NOT be re-sent with
                  the envelope's profile (round 5: no retry at all).
      - 'always': every archive api call throws 409 — the failure surfaces
                  without any further request.
      - 'never':  every archive api call succeeds — the happy path sends
                  exactly one request carrying the row's own profile.
    ``session_profile``: the archived row's known owner profile (absent on
    legacy root-owned rows).
    ``show_all_profiles``: drive the all-profiles sidebar view (the
    ``_showAllProfiles`` toggle on).
    """
    if fail_mode == 'first':
        calls = """
let apiCalls = 0;
const requests = [];
async function api(path, opts){
  apiCalls++;
  requests.push({path, body: JSON.parse(opts.body)});
  const e = new Error('409');
  e.status = 409;
  e.body = JSON.stringify({code:'session_profile_mismatch',profile:'work',session_id:'s1'});
  throw e;
}"""
    elif fail_mode == 'always':
        calls = """let apiCalls = 0;
const requests = [];
async function api(path, opts){
  apiCalls++;
  requests.push({path, body: JSON.parse(opts.body)});
  const e = new Error('409');
  e.status = 409;
  e.body = JSON.stringify({code:'session_profile_mismatch',profile:'work',session_id:'s1'});
  throw e;
}"""
    else:
        calls = """let apiCalls = 0;
const requests = [];
async function api(path, opts){
  apiCalls++;
  requests.push({path, body: JSON.parse(opts.body)});
  return {ok:true};
}"""
    session_js = ("{ session_id:'s1', archived:false }"
                  if session_profile is None
                  else "{ session_id:'s1', archived:false, profile:'%s' }" % session_profile)
    show_all_js = "let _showAllProfiles = true;" if show_all_profiles else ""
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
function _repaintSidebarAfterSessionCompletion() {{ events.push('render-cache'); }}
function renderSessionList() {{ events.push('render-full'); }}
function _captureSessionReflowPositions() {{ return 'reflow'; }}
function _sessionPrefersReducedMotion() {{ return false; }}
function _isReadOnlySession() {{ return false; }}
const _showArchived = true;
{show_all_js}
const _sessionSwipeReturnOffsets = {{ _m: new Map(), set(k, v) {{ this._m.set(k, v); }} }};
let _pendingSessionReflowPositions = null;

{mismatch_body}

{archive_body}

let switchCalls = 0;
async function _switchProfileForSessionLoad(profile) {{
  switchCalls++;
  events.push('switch:' + profile);
  S.activeProfile = profile;
  return {{ active: profile }};
}}
function _profileMatchesActiveProfile(profile, activeProfile){{
  const eventName = (typeof profile === 'string' && profile.trim()) ? profile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(eventName === activeName) return true;
  return eventName === 'default' && !!S.activeProfileIsDefault;
}}
function _sessionSnapshotById(sid) {{
  return sessions.find(s => s && s.session_id === sid) || null;
}}

{calls}

(async () => {{
  const sid = 's1';
  sessions.push({{ session_id: sid, archived: false }});
  const result = await _archiveSession({session_js}, true, null);
  console.log(JSON.stringify({{ result, apiCalls, switchCalls, requests, events, activeProfile: S.activeProfile }}));
}})();

"""


# --- single archive: profile-scoped, zero switches --------------------------


def test_single_archive_sends_profile_field_in_one_request():
    """A row whose owner profile is known sends exactly ONE request carrying
    that profile — no 409, no retry, and zero _switchProfileForSessionLoad
    calls: the active profile is never touched for a foreign-row archive."""
    if NODE is None:
        return
    archive_body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    mismatch_body = _extract_function(SESSIONS_JS, "_sessionProfileMismatchFromError")
    out = _run_node(_build_driver(
        archive_body, mismatch_body, fail_mode='never',
        session_profile="work"))
    data = json.loads(out)
    assert data["result"] is True, f"archive should succeed: {data}"
    assert data["apiCalls"] == 1, f"no 409 means exactly one request: {data}"
    assert data["switchCalls"] == 0, f"zero profile switches allowed: {data}"
    assert data["requests"] == [{"path": "/api/session/archive",
                                 "body": {"session_id": "s1", "archived": True,
                                           "profile": "work"}}], data
    assert data["activeProfile"] == "default", (
        f"active profile must never move: {data}")


def test_single_archive_409_reports_failure_without_retrying():
    """Round 5 (maintainer finding, security): a structured 409 must surface
    as a failure with EXACTLY ONE request. The old client re-POSTed with the
    409 envelope's profile, re-issuing a WRITE against a session the server
    had just denied — which let a denied archive rewrite a foreign profile's
    transcript. No retry of any kind, and no profile switch."""
    if NODE is None:
        return
    archive_body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    mismatch_body = _extract_function(SESSIONS_JS, "_sessionProfileMismatchFromError")
    out = _run_node(_build_driver(archive_body, mismatch_body, fail_mode='first'))
    data = json.loads(out)
    assert data["result"] is False, f"409 must surface as failure: {data}"
    assert data["apiCalls"] == 1, (
        f"a denied archive must never be re-sent: {data}")
    assert data["switchCalls"] == 0, f"no switching, ever: {data}"
    assert data["requests"] == [{"path": "/api/session/archive",
                                 "body": {"session_id": "s1",
                                          "archived": True}}], data
    assert data["activeProfile"] == "default", (
        f"active profile must never move: {data}")


def test_single_archive_no_profile_field_for_legacy_row():
    """A legacy root-owned row (no profile on the snapshot) sends no profile
    field at all — the server resolves it against the active (root) profile,
    which is the old same-profile contract."""
    if NODE is None:
        return
    archive_body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    mismatch_body = _extract_function(SESSIONS_JS, "_sessionProfileMismatchFromError")
    out = _run_node(_build_driver(archive_body, mismatch_body, fail_mode='never'))
    data = json.loads(out)
    assert data["result"] is True, data
    assert data["apiCalls"] == 1, data
    assert data["requests"][0]["body"] == {"session_id": "s1", "archived": True}, (
        f"legacy row must send no profile field: {data}")


def test_single_archive_does_not_loop_when_409_persists():
    """A 409 that persists (every request denied) must not loop: one request,
    then the failure is reported. There is no second attempt of any kind."""
    if NODE is None:
        return
    archive_body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    mismatch_body = _extract_function(SESSIONS_JS, "_sessionProfileMismatchFromError")
    out = _run_node(_build_driver(archive_body, mismatch_body, fail_mode='always'))
    data = json.loads(out)
    assert data["result"] is False, f"409 should surface as failure: {data}"
    assert data["apiCalls"] == 1, f"exactly one request, no loop: {data}"
    assert data["switchCalls"] == 0, f"no switch even when the retry fails: {data}"


def test_archive_source_has_no_profile_switch_pipeline():
    """Static contract: the archive pipeline must contain ZERO profile
    switching. The production definitions of the old switch/restore helpers
    are gone entirely — if anyone resurrects them, this goes RED (revert
    sensitivity for the pipeline deletion)."""
    assert "async function _restoreProfileAfterArchive(" not in SESSIONS_JS, (
        "the restore helper must be gone, not stubbed around")
    assert "async function _switchProfileForActiveProfile(" not in SESSIONS_JS, (
        "the lean restore switch must be gone, not stubbed around")
    body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    assert "_switchProfileForSessionLoad" not in body, (
        "_archiveSession must never switch profiles (root bug 3)")
    assert "api('/api/profile/switch'" not in body, (
        "_archiveSession must never call the switch endpoint")
    assert "_restoreProfileAfterArchive" not in body, (
        "_archiveSession must not wrap outcomes in a restore")
    # Round 5: the claimed-profile retry is gone. There must be no recursive
    # _archiveSession call and no _retried guard left behind.
    assert "_retried" not in body, (
        "the claimed-profile retry and its guard must be gone")
    # The signature line `async function _archiveSession(` contains the name;
    # strip it so only real call sites are scanned.
    recurrences = body[len("async function _archiveSession("):]
    assert "_archiveSession(" not in recurrences, (
        "_archiveSession must never call itself (the retry is removed)")
    assert "profile:_ownedProfile" in body, (
        "the first attempt must carry the row's own profile when known")


# --- batch archive: per-row profile, zero switches --------------------------


def _build_batch_driver(owners_body, owner_row_body, match_body, batch_body,
                        scenario, rows, active_profile='default',
                        active_is_default=True, open_session=None,
                        show_all_profiles=False):
    """Drive _archiveBatchOwners / _archiveBatchSessions under Node.

    ``scenario`` selects the api behaviour:
      - 'all-ok':      every archive call succeeds.
      - 'fail-w2':     the archive call for sid 'w2' throws; every other row
                       still runs (per-row execution must not stop the batch).
    ``rows`` entries: {id, profile (or None), webui (bool, default True),
    open (bool — the row is S.session)}.
    ``show_all_profiles``: drive the all-profiles sidebar view (the
    ``_showAllProfiles`` toggle on) — known foreign owners are eligible
    there and every request carries ``all_profiles: true``.
    """
    api_impl = {
        'all-ok': """let apiCalls = 0;
const requests = [];
async function api(path, opts){
  apiCalls++;
  events.push('api:' + path);
  const body = JSON.parse(opts.body);
  requests.push(body);
  archivedSids.push(body.session_id);
  return {worktree_retained:false};
}""",
        'fail-w2': """let apiCalls = 0;
const requests = [];
async function api(path, opts){
  apiCalls++;
  events.push('api:' + path);
  const body = JSON.parse(opts.body);
  requests.push(body);
  if(body.session_id === 'w2'){ const e = new Error('boom-archive'); throw e; }
  archivedSids.push(body.session_id);
  return {worktree_retained:false};
}""",
    }[scenario]
    rows_js = ", ".join(
        "{ session_id: '%s', profile: %s, session_source: %s, read_only: %s }" % (
            s["id"],
            "null" if s.get("profile") is None else ("%r" % s["profile"]),
            "%r" % ('webui' if s.get("webui", True) else 'cli'),
            'true' if s.get("read_only") else 'false',
        )
        for s in rows)
    open_sid = next((s["id"] for s in rows if s.get("open")), None) or open_session
    open_sid_js = (
        'null' if open_sid is None
        else "{session_id:'" + str(open_sid) + "'}"
    )
    show_all_js = "let _showAllProfiles = true;" if show_all_profiles else ""
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
// #7826 re-gate CORE: the read-only predicate the preflight and the batch
// executor now consult (a read-only row must not veto the writable rows).
function _isReadOnlySession(session){{
  return !!(session && (session.read_only || session.is_read_only));
}}
function _profileMatchesActiveProfile(profile, activeProfile){{
  const eventName = (typeof profile === 'string' && profile.trim()) ? profile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(eventName === activeName) return true;
  return eventName === 'default' && !!S.activeProfileIsDefault;
}}
{show_all_js}

{owner_row_body}

{match_body}

{owners_body}

let switchCalls = 0;
async function _switchProfileForSessionLoad(profile){{
  switchCalls++;
  events.push('switch:' + profile);
  S.activeProfile = profile;
  return {{ active: profile }};
}}
{api_impl}

{batch_body}

(async () => {{
  const rows = [{rows_js}];
  const sessionsById = new Map(rows.map(r => [r.session_id, r]));
  const ids = rows.map(r => r.session_id);
  const preflight = _archiveBatchOwners(ids, sessionsById);
  const outcome = await _archiveBatchSessions(ids, sessionsById);
  console.log(JSON.stringify({{ preflight, outcome, switchCalls, apiCalls, events, archivedSids, requests, activeProfile: S.activeProfile }}));
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


def test_batch_each_request_carries_own_owner_profile_zero_switches():
    """Eligible selection: every row archives, each request carries ITS row's
    owner profile, and zero _switchProfileForSessionLoad calls happen — the
    root-fix contract (pre-fix: one switch per distinct owner, restore on top).

    #7826 re-gate: every row's owner must match the ACTIVE profile. The server
    ignores the client-supplied ``profile`` on this endpoint and resolves the row
    against the active profile itself, so a foreign row's request 409s and the
    stop-on-failure contract then abandons every row after it — which made the
    outcome depend on selection order. Root aliases ('default' while
    ``S.activeProfileIsDefault``) still count as a match.
    """
    if NODE is None:
        return
    rows = [
        {"id": "w1", "profile": "default"},
        {"id": "w2", "profile": "default"},
        {"id": "h1", "profile": None, "webui": True},
    ]
    data = _run_batch("all-ok", rows)
    assert data["preflight"]["owners"], data
    assert data["outcome"]["ok"] is True, data
    assert data["outcome"]["archivedCount"] == 3, data
    assert data["switchCalls"] == 0, (
        f"batch archive must never switch profiles: {data}")
    bodies = data["requests"]
    assert [b["profile"] for b in bodies] == ["default", "default", "default"], data
    assert all(b["archived"] is True for b in bodies), data
    assert sorted(data["archivedSids"]) == ["h1", "w1", "w2"], data
    assert data["activeProfile"] == "default", (
        f"active profile must never move: {data}")


def test_batch_no_chat_open_zero_switches():
    """Finding 1 (the no-displayed-session leak): with S.session null the old
    restore early-returned and stranded the user on the foreign profile. Root
    fix: there is nothing to restore because nothing ever switches — the batch
    must archive with zero switches and leave the active profile untouched."""
    if NODE is None:
        return
    rows = [{"id": "w1", "profile": "work"}, {"id": "w2", "profile": "work"}]
    data = _run_batch("all-ok", rows, open_session=None)
    assert data["outcome"]["ok"] is True, data
    assert data["outcome"]["archivedCount"] == 2, data
    assert data["switchCalls"] == 0, (
        f"no displayed chat must not change the switch calculus: {data}")
    assert data["activeProfile"] == "default", (
        f"user must not be stranded on a foreign profile: {data}")
    assert [b["profile"] for b in data["requests"]] == ["work", "work"], data


def test_batch_partial_failure_reports_failed_sid_zero_switches():
    """Finding 2 (the failed-row-as-archived misread): the old executor passed
    the WHOLE id list to the restore, so a selected-but-failed row made the
    restore believe the displayed chat was archived and skip the bounce-back.
    Root fix: only rows that actually archived count, every failed sid lands
    in the partial-failure envelope, and zero switches happen.

    Round-3 contract restored: a mid-batch failure STOPS the loop, so w3 is
    never requested (``test_batch_mid_failure_stops_before_later_rows``
    pins that separately).
    """
    if NODE is None:
        return
    rows = [{"id": "w1", "profile": "work"}, {"id": "w2", "profile": "work"},
            {"id": "w3", "profile": "work"}]
    data = _run_batch("fail-w2", rows)
    assert data["outcome"]["ok"] is False, (
        f"partial success must not report ok: {data}")
    assert "batch-partial-failure:w2" in data["outcome"]["error"], data
    assert data["outcome"]["archivedCount"] == 1, (
        f"only rows that really archived count: {data}")
    assert data["outcome"]["totalCount"] == 3, data
    assert data["archivedSids"] == ["w1"], (
        f"the failed row must not be treated as archived: {data}")
    assert data["switchCalls"] == 0, (
        f"partial failure must never trigger a profile switch: {data}")
    assert data["activeProfile"] == "default", data


def test_batch_mid_failure_stops_before_later_rows():
    """Round-3 contract (maintainer-gated at 8e4f00dd): a mid-batch archive
    failure STOPS the sequential loop — no request may be sent for rows after
    the failing one, and the outcome is never reported as success.

    The profile-scoped rework had briefly changed this to "keep going, collect
    the failed sids", which silently archived a DIFFERENT subset than the user
    selected. That is a behaviour change on an already-gated contract, not an
    implementation detail, so the round-3 semantics are restored here and
    pinned.
    """
    if NODE is None:
        return
    rows = [{"id": "w1", "profile": "work"}, {"id": "w2", "profile": "work"},
            {"id": "w3", "profile": "work"}]
    data = _run_batch("fail-w2", rows)
    assert data["archivedSids"] == ["w1"], (
        f"no row after the failure may be archived: {data}")
    assert data["apiCalls"] == 2, (
        f"the failing request is the LAST one sent (w1 + the failed w2): {data}")
    assert data["outcome"]["ok"] is False, data
    assert "batch-partial-failure" in data["outcome"]["error"], data
    assert data["outcome"]["archivedCount"] == 1, (
        f"the aborted tail must not be counted as archived: {data}")


def test_batch_unknown_cli_row_still_fails_closed():
    """The profile-less WebUI carve-out must not become a blanket accept. An
    unknown CLI row has no derivable owner client-side and the server 404s it
    by contract — the selection must fail closed with zero archive requests."""
    if NODE is None:
        return
    rows = [
        {"id": "s1", "profile": "default"},
        {"id": "s2", "profile": None, "webui": False},
    ]
    data = _run_batch("all-ok", rows)
    assert data["preflight"]["owner"] is None, data
    assert data["preflight"]["reason"] == "unknown-owner", data
    assert data["outcome"]["ok"] is False, data
    assert data["outcome"]["error"] == "unknown-owner", data
    assert data["apiCalls"] == 0, f"unknown CLI row must fail closed: {data}"
    assert data["switchCalls"] == 0, data


def test_batch_readonly_row_refused_per_row_writable_row_archives():
    """Re-gate CORE (maintainer finding 2026-10-29): "Select all" -> Archive
    with a read-only Claude Code row (``read_only: true``, ``profile: null``)
    plus a writable row must still archive the writable row. The preflight
    skips the read-only row for the owner-mix decision; the executor refuses
    it PER ROW — no request (the server refuses read-only rows by contract) —
    and keeps archiving the writable rows, master's shape (writable 200,
    read-only named in the failure envelope). Before the fix the selection
    failed closed with zero requests and nothing archived."""
    if NODE is None:
        return
    rows = [
        {"id": "ro", "profile": None, "webui": False, "read_only": True},
        {"id": "w1", "profile": "default"},
    ]
    data = _run_batch("all-ok", rows)
    # The preflight must pass: the read-only row has nothing to veto.
    assert data["preflight"]["owners"], data
    assert not data["preflight"].get("reason"), data
    # Exactly one request, for the writable row, under its own profile.
    assert data["archivedSids"] == ["w1"], data
    assert data["apiCalls"] == 1, (
        f"the read-only row must be refused without a request: {data}")
    assert data["requests"] == [
        {"session_id": "w1", "archived": True, "profile": "default"}
    ], data
    # The read-only row is reported, not silently dropped.
    assert data["outcome"]["ok"] is False, data
    assert "batch-partial-failure:ro" in data["outcome"]["error"], data
    assert data["outcome"]["archivedCount"] == 1, data
    assert data["outcome"]["totalCount"] == 2, data
    assert data["switchCalls"] == 0, data


def test_batch_readonly_row_after_writable_row_does_not_stop_loop():
    """Order control: the read-only refusal must not stop the sequential
    loop. [writable, read-only] must archive the writable row exactly like
    [read-only, writable] — the per-row refusal is not a mid-batch failure."""
    if NODE is None:
        return
    rows = [
        {"id": "w1", "profile": "default"},
        {"id": "ro", "profile": None, "webui": False, "read_only": True},
    ]
    data = _run_batch("all-ok", rows)
    assert data["archivedSids"] == ["w1"], data
    assert data["apiCalls"] == 1, data
    assert "batch-partial-failure:ro" in data["outcome"]["error"], data
    assert data["outcome"]["archivedCount"] == 1, data


def test_batch_legacy_root_owned_rows_send_default_profile():
    """Legacy root-owned WebUI rows (profile null on snapshot, the sidebar row
    says default) must resolve to owner 'default' and archive with the
    request-scoped profile 'default' — matching the server's None→root
    coercion — with zero switches."""
    if NODE is None:
        return
    rows = [
        {"id": "legacy-open", "profile": None, "open": True},
        {"id": "legacy-2", "profile": None},
    ]
    data = _run_batch("all-ok", rows)
    assert data["preflight"]["owner"] == "default", data
    assert data["outcome"]["ok"] is True, data
    assert data["switchCalls"] == 0, data
    assert [b["profile"] for b in data["requests"]] == ["default", "default"], data
    assert sorted(data["archivedSids"]) == ["legacy-2", "legacy-open"], data


def test_batch_renamed_root_default_rows_request_no_switch():
    """A row labelled 'default' under a renamed root must archive under the
    request-scoped 'default' profile with zero switches — the owner check
    goes through the alias-aware match, and nothing needs a switch anymore."""
    if NODE is None:
        return
    rows = [{"id": "r1", "profile": "default"}, {"id": "r2", "profile": "default"}]
    data = _run_batch("all-ok", rows,
                      active_profile="my-renamed-root", active_is_default=True)
    assert data["preflight"]["owner"] == "default", data
    assert data["outcome"]["ok"] is True, data
    assert data["switchCalls"] == 0, data
    assert [b["profile"] for b in data["requests"]] == ["default", "default"], data


def test_batch_archive_button_handler_mixed_rows_zero_switches():
    """The real archive button handler must reject an ineligible batch BEFORE any
    request, and must keep the selection so the user can deselect and retry.

    #7826 re-gate: mixed rows used to reach the executor, the foreign row's
    request 409d, and the stop-on-failure contract abandoned the owned row —
    so which rows survived depended on selection order. The preflight now
    validates every owner against the ACTIVE profile, and the rejection branch
    no longer calls exitSessionSelectMode().
    """
    if NODE is None:
        return
    render_body = _extract_function(SESSIONS_JS, "_renderBatchActionBar")
    owners_body = _extract_function(SESSIONS_JS, "_archiveBatchOwners")
    owner_row_body = _extract_function(SESSIONS_JS, "_archiveBatchOwnerForRow")
    match_body = _extract_function(SESSIONS_JS, "_archiveBatchOwnersMatch")
    batch_body = _extract_async_function(SESSIONS_JS, "_archiveBatchSessions")
    script = f"""
const events=[]; const archivedSids=[]; const requests=[];
const rows=[{{session_id:'w1',profile:'work',session_source:'webui'}},{{session_id:'h1',profile:'home',session_source:'webui'}}];
const sessionsById=new Map(rows.map(r=>[r.session_id,r]));
const _allSessions=rows; const _selectedSessions=new Set(['w1','h1']);
const S={{session:{{session_id:'root-chat'}},activeProfile:'default',activeProfileIsDefault:true}};
const window={{_defaultModel:'root-model',_activeProvider:'root-provider'}};
const _showArchived=false; const _sessionSwipeReturnOffsets={{set(){{}}}};
let _pendingSessionReflowPositions=null;
const bar={{style:{{}},children:[],appendChild(node){{this.children.push(node);}}}};
function $(id){{return id==='batchActionBar'?bar:null;}}
const document={{createElement(tag){{return {{tag,style:{{}},className:'',textContent:'',appendChild(){{}},onclick:null}};}}}};
function t(key){{return key;}} function _worktreeSessionCount(){{return 0;}}
function _sessionSnapshotById(sid){{return sessionsById.get(sid)||null;}}
function showConfirmDialog(){{events.push('confirm');return Promise.resolve(true);}}
function exitSessionSelectMode(){{events.push('exit');}} function showToast(msg){{events.push('toast:'+msg);}}
function renderSessionList(){{}} function _sessionPrefersReducedMotion(){{return false;}}
function _sessionResponseRetainsWorktree(){{return false;}}
function _profileMatchesActiveProfile(profile,active){{return profile===active;}}
let switchCalls=0;
async function _switchProfileForSessionLoad(profile){{switchCalls++;S.activeProfile=profile;S.activeProfileIsDefault=false;window._defaultModel=profile+'-model';window._activeProvider=profile+'-provider';}}
async function api(path,opts){{
  const body=JSON.parse(opts.body);
  requests.push(body);
  archivedSids.push(body.session_id);
  return {{worktree_retained:false}};
}}
{owners_body}
{owner_row_body}
{match_body}
{batch_body}
{render_body}
_renderBatchActionBar();
(async()=>{{const button=bar.children.find(node=>node.textContent==='session_batch_archive');if(!button)throw new Error('no archive button');await button.onclick();console.log(JSON.stringify({{events,archivedSids,switchCalls,requests,activeProfile:S.activeProfile,defaultModel:window._defaultModel,activeProvider:window._activeProvider}}));}})();
"""
    data = json.loads(_run_node(script))
    # Zero archive requests: the preflight rejects the batch before any POST.
    assert data["requests"] == [], data
    assert data["archivedSids"] == [], data
    # The confirm dialog never opens for an ineligible batch.
    assert "confirm" not in data["events"], data
    # The selection is RETAINED so the user can deselect the foreign row.
    assert "exit" not in data["events"], (
        f"a failed preflight must not discard the selection: {data}")
    assert data["switchCalls"] == 0, (
        f"the batch button handler must never switch profiles: {data}")
    assert data["activeProfile"] == "default", data
    assert data["defaultModel"] == "root-model", data
    assert data["activeProvider"] == "root-provider", data


def test_batch_archive_button_readonly_row_keeps_writable_row_archiving():
    """Re-gate CORE: the real archive button handler, "Select all" selection
    holding a read-only Claude Code row (``read_only: true``, ``profile:
    null``) and a writable row. The preflight must pass (the read-only row
    has nothing to veto), the confirm dialog must open, and exactly ONE
    request must go out — for the writable row. The read-only row is refused
    per row and named in the partial-failure toast (master refuses it with a
    400 and still archives the writable row)."""
    if NODE is None:
        return
    render_body = _extract_function(SESSIONS_JS, "_renderBatchActionBar")
    owners_body = _extract_function(SESSIONS_JS, "_archiveBatchOwners")
    owner_row_body = _extract_function(SESSIONS_JS, "_archiveBatchOwnerForRow")
    match_body = _extract_function(SESSIONS_JS, "_archiveBatchOwnersMatch")
    batch_body = _extract_async_function(SESSIONS_JS, "_archiveBatchSessions")
    script = f"""
const events=[]; const archivedSids=[]; const requests=[];
const rows=[{{session_id:'ro',profile:null,session_source:'cli',read_only:true}},{{session_id:'w1',profile:'default',session_source:'webui'}}];
const sessionsById=new Map(rows.map(r=>[r.session_id,r]));
const _allSessions=rows; const _selectedSessions=new Set(['ro','w1']);
const S={{session:{{session_id:'root-chat'}},activeProfile:'default',activeProfileIsDefault:true}};
const window={{_defaultModel:'root-model',_activeProvider:'root-provider'}};
const _showArchived=false; const _sessionSwipeReturnOffsets={{set(){{}}}};
let _pendingSessionReflowPositions=null;
const bar={{style:{{}},children:[],appendChild(node){{this.children.push(node);}}}};
function $(id){{return id==='batchActionBar'?bar:null;}}
const document={{createElement(tag){{return {{tag,style:{{}},className:'',textContent:'',appendChild(){{}},onclick:null}};}}}};
function t(key){{return key;}} function _worktreeSessionCount(){{return 0;}}
function _sessionSnapshotById(sid){{return sessionsById.get(sid)||null;}}
function showConfirmDialog(){{events.push('confirm');return Promise.resolve(true);}}
function exitSessionSelectMode(){{events.push('exit');}} function showToast(msg){{events.push('toast:'+msg);}}
function renderSessionList(){{}} function _sessionPrefersReducedMotion(){{return false;}}
function _sessionResponseRetainsWorktree(){{return false;}}
function _profileMatchesActiveProfile(profile,active){{return profile===active;}}
function _isReadOnlySession(session){{return !!(session&&(session.read_only||session.is_read_only));}}
let switchCalls=0;
async function _switchProfileForSessionLoad(profile){{switchCalls++;S.activeProfile=profile;S.activeProfileIsDefault=false;window._defaultModel=profile+'-model';window._activeProvider=profile+'-provider';}}
async function api(path,opts){{
  const body=JSON.parse(opts.body);
  requests.push(body);
  archivedSids.push(body.session_id);
  return {{worktree_retained:false}};
}}
{owners_body}
{owner_row_body}
{match_body}
{batch_body}
{render_body}
_renderBatchActionBar();
(async()=>{{const button=bar.children.find(node=>node.textContent==='session_batch_archive');if(!button)throw new Error('no archive button');await button.onclick();console.log(JSON.stringify({{events,archivedSids,switchCalls,requests,activeProfile:S.activeProfile,defaultModel:window._defaultModel,activeProvider:window._activeProvider}}));}})();
"""
    data = json.loads(_run_node(script))
    # The writable row archives; the read-only row is refused per row.
    assert data["archivedSids"] == ["w1"], (
        f"the writable row must archive even with a read-only row selected: {data}")
    assert data["requests"] == [
        {"session_id": "w1", "archived": True, "profile": "default"}
    ], data
    assert "confirm" in data["events"], (
        f"an eligible selection must open the confirm dialog: {data}")
    assert "toast:session_batch_archive_mixed_profiles" not in data["events"], (
        f"the read-only row must not veto the batch with the mixed-profiles "
        f"toast: {data}")
    toast = next((e for e in data["events"] if e.startswith("toast:")), "")
    assert "batch-partial-failure:ro" in toast, (
        f"the refusal must name the read-only row: {toast!r}")
    assert data["switchCalls"] == 0, (
        f"the batch button handler must never switch profiles: {data}")
    assert data["activeProfile"] == "default", data


def test_batch_archive_content_search_row_archives():
    """Maintainer finding 1 (2026-10-19 review): a content-search result is
    rendered from _contentSearchResults (see _sessionSearchMergeMatches) and
    gets a select checkbox, but its snapshot lookup returns null — the row is
    absent from S.session and _allSessions — so the batch preflight used to
    fail closed with the mixed-profiles toast and never sent a request. The
    sessionsById construction must fall back to _contentSearchResults so the
    row's owner resolves and it archives exactly like a sidebar row.
    """
    if NODE is None:
        return
    render_body = _extract_function(SESSIONS_JS, "_renderBatchActionBar")
    owners_body = _extract_function(SESSIONS_JS, "_archiveBatchOwners")
    owner_row_body = _extract_function(SESSIONS_JS, "_archiveBatchOwnerForRow")
    match_body = _extract_function(SESSIONS_JS, "_archiveBatchOwnersMatch")
    batch_body = _extract_async_function(SESSIONS_JS, "_archiveBatchSessions")
    script = f"""
const events=[]; const archivedSids=[]; const requests=[];
const _allSessions=[];
const _contentSearchResults=[{{session_id:'cs1',profile:'default',session_source:'webui',match_type:'content'}}];
const _selectedSessions=new Set(['cs1']);
const S={{session:null,activeProfile:'default',activeProfileIsDefault:true}};
const window={{_defaultModel:'root-model',_activeProvider:'root-provider'}};
const _showArchived=false; const _sessionSwipeReturnOffsets={{set(){{}}}};
let _pendingSessionReflowPositions=null;
const bar={{style:{{}},children:[],appendChild(node){{this.children.push(node);}}}};
function $(id){{return id==='batchActionBar'?bar:null;}}
const document={{createElement(tag){{return {{tag,style:{{}},className:'',textContent:'',appendChild(){{}},onclick:null}};}}}};
function t(key){{return key;}} function _worktreeSessionCount(){{return 0;}}
function _sessionSnapshotById(sid){{return null;}}
function showConfirmDialog(){{events.push('confirm');return Promise.resolve(true);}}
function exitSessionSelectMode(){{events.push('exit');}} function showToast(msg){{events.push('toast:'+msg);}}
function renderSessionList(){{}} function _sessionPrefersReducedMotion(){{return false;}}
function _sessionResponseRetainsWorktree(){{return false;}}
function _profileMatchesActiveProfile(profile,active){{return profile===active;}}
let switchCalls=0;
async function _switchProfileForSessionLoad(profile){{switchCalls++;S.activeProfile=profile;S.activeProfileIsDefault=false;window._defaultModel=profile+'-model';window._activeProvider=profile+'-provider';}}
async function api(path,opts){{
  const body=JSON.parse(opts.body);
  requests.push(body);
  archivedSids.push(body.session_id);
  return {{worktree_retained:false}};
}}
{owners_body}
{owner_row_body}
{match_body}
{batch_body}
{render_body}
_renderBatchActionBar();
(async()=>{{const button=bar.children.find(node=>node.textContent==='session_batch_archive');if(!button)throw new Error('no archive button');await button.onclick();console.log(JSON.stringify({{events,archivedSids,switchCalls,requests,activeProfile:S.activeProfile,defaultModel:window._defaultModel,activeProvider:window._activeProvider}}));}})();
"""
    data = json.loads(_run_node(script))
    # One request, owner resolved from the content-search row, archived.
    assert data["archivedSids"] == ["cs1"], (
        f"a content-search-only row must archive: {data}")
    assert data["requests"] == [
        {"session_id": "cs1", "archived": True, "profile": "default"}
    ], data
    assert data["switchCalls"] == 0, (
        f"content-search rows must not trigger a profile switch: {data}")
    assert "toast:session_archived" in data["events"], data
    assert "exit" in data["events"], (
        f"a successful content-search batch must exit select mode: {data}")


# --- all-profiles view: archive/restore another profile's session -------------
#
# 2026-10-27 re-gate regression (CORE): with "Show all profiles" ON the
# all-profiles sidebar offers archive/restore for sessions owned by another
# profile.  The profile-scoped rework dropped master's `all_profiles: true`
# from every archive request, so the server resolved each request against the
# ACTIVE profile: a single foreign row 404'd/409'd where master returned
# 200/200, and the batch preflight rejected the whole selection with
# session_batch_archive_foreign_owner where master sent two requests that
# both returned 200.  The flag must travel alongside the row's owner profile,
# and the preflight must accept known foreign owners in that mode only.


def test_single_archive_all_profiles_view_sends_all_profiles_flag():
    """With the all-profiles view ON, a foreign row's archive carries
    ``all_profiles: true`` alongside its owner profile — master's shape —
    so the server resolves the request against the claimed owner instead of
    the active profile.  Still exactly one request and zero switches."""
    if NODE is None:
        return
    archive_body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    mismatch_body = _extract_function(SESSIONS_JS, "_sessionProfileMismatchFromError")
    out = _run_node(_build_driver(
        archive_body, mismatch_body, fail_mode='never',
        session_profile="work", show_all_profiles=True))
    data = json.loads(out)
    assert data["result"] is True, f"archive should succeed: {data}"
    assert data["apiCalls"] == 1, f"exactly one request: {data}"
    assert data["switchCalls"] == 0, f"zero profile switches allowed: {data}"
    assert data["requests"] == [{"path": "/api/session/archive",
                                 "body": {"session_id": "s1", "archived": True,
                                          "profile": "work",
                                          "all_profiles": True}}], (
        f"all-profiles view must send all_profiles with the owner profile: {data}")


def test_single_archive_default_view_sends_no_all_profiles_flag():
    """Control: the default (active-profile) view keeps the plain scoped
    request — no all_profiles flag, so a foreign row still 409s there."""
    if NODE is None:
        return
    archive_body = _extract_async_function(SESSIONS_JS, "_archiveSession")
    mismatch_body = _extract_function(SESSIONS_JS, "_sessionProfileMismatchFromError")
    out = _run_node(_build_driver(
        archive_body, mismatch_body, fail_mode='never',
        session_profile="work"))
    data = json.loads(out)
    assert data["requests"] == [{"path": "/api/session/archive",
                                 "body": {"session_id": "s1", "archived": True,
                                          "profile": "work"}}], (
        f"default view must not send all_profiles: {data}")


def test_batch_all_profiles_view_accepts_foreign_owner_and_sends_flag():
    """With the all-profiles view ON, the batch preflight accepts a KNOWN
    foreign owner (each row archives under its own profile) and every
    request carries ``all_profiles: true`` — master sends both and both
    return 200.  Zero switches, active profile untouched."""
    if NODE is None:
        return
    rows = [{"id": "w1", "profile": "work"}, {"id": "w2", "profile": "work"}]
    data = _run_batch("all-ok", rows, show_all_profiles=True)
    assert data["preflight"]["owner"] == "work", (
        f"all-profiles view must accept a known foreign owner: {data}")
    assert data["preflight"]["owners"] == ["work"], data
    assert data["outcome"]["ok"] is True, data
    assert data["outcome"]["archivedCount"] == 2, data
    assert data["switchCalls"] == 0, (
        f"batch archive must never switch profiles: {data}")
    assert data["requests"] == [
        {"session_id": "w1", "archived": True, "profile": "work",
         "all_profiles": True},
        {"session_id": "w2", "archived": True, "profile": "work",
         "all_profiles": True},
    ], data
    assert data["activeProfile"] == "default", (
        f"active profile must never move: {data}")


def test_batch_foreign_owner_still_rejected_without_all_profiles_view():
    """Control: with the view OFF the preflight still rejects a known
    foreign owner (the server 409s it) — the all-profiles carve-out must
    not widen the default view.  (The driver calls the executor
    unconditionally, so the preflight verdict is the contract here; the
    button handler's zero-request behaviour is pinned by
    test_batch_archive_button_handler_mixed_rows_zero_switches.)"""
    if NODE is None:
        return
    rows = [{"id": "w1", "profile": "work"}]
    data = _run_batch("all-ok", rows)
    assert data["preflight"]["owner"] is None, data
    assert data["preflight"]["owners"] == [], data
    assert data["preflight"]["reason"] == "foreign-owner:work", data


def test_batch_archive_button_all_profiles_view_archives_foreign_rows():
    """The real archive button handler under the all-profiles view: the
    preflight passes, the confirm dialog opens, both foreign rows archive
    with all_profiles: true, and the selection is retained semantics of a
    successful batch (exit select mode) — master's two-200 shape.  This
    head used to send none and show session_batch_archive_foreign_owner."""
    if NODE is None:
        return
    render_body = _extract_function(SESSIONS_JS, "_renderBatchActionBar")
    owners_body = _extract_function(SESSIONS_JS, "_archiveBatchOwners")
    owner_row_body = _extract_function(SESSIONS_JS, "_archiveBatchOwnerForRow")
    match_body = _extract_function(SESSIONS_JS, "_archiveBatchOwnersMatch")
    batch_body = _extract_async_function(SESSIONS_JS, "_archiveBatchSessions")
    script = f"""
const events=[]; const archivedSids=[]; const requests=[];
const rows=[{{session_id:'w1',profile:'work',session_source:'webui'}},{{session_id:'w2',profile:'work',session_source:'webui'}}];
const sessionsById=new Map(rows.map(r=>[r.session_id,r]));
const _allSessions=rows; const _selectedSessions=new Set(['w1','w2']);
const S={{session:{{session_id:'root-chat'}},activeProfile:'default',activeProfileIsDefault:true}};
const window={{_defaultModel:'root-model',_activeProvider:'root-provider'}};
const _showArchived=false; const _sessionSwipeReturnOffsets={{set(){{}}}};
let _showAllProfiles = true;
let _pendingSessionReflowPositions=null;
const bar={{style:{{}},children:[],appendChild(node){{this.children.push(node);}}}};
function $(id){{return id==='batchActionBar'?bar:null;}}
const document={{createElement(tag){{return {{tag,style:{{}},className:'',textContent:'',appendChild(){{}},onclick:null}};}}}};
function t(key){{return key;}} function _worktreeSessionCount(){{return 0;}}
function _sessionSnapshotById(sid){{return sessionsById.get(sid)||null;}}
function showConfirmDialog(){{events.push('confirm');return Promise.resolve(true);}}
function exitSessionSelectMode(){{events.push('exit');}} function showToast(msg){{events.push('toast:'+msg);}}
function renderSessionList(){{}} function _sessionPrefersReducedMotion(){{return false;}}
function _sessionResponseRetainsWorktree(){{return false;}}
function _profileMatchesActiveProfile(profile,active){{return profile===active;}}
let switchCalls=0;
async function _switchProfileForSessionLoad(profile){{switchCalls++;S.activeProfile=profile;S.activeProfileIsDefault=false;window._defaultModel=profile+'-model';window._activeProvider=profile+'-provider';}}
async function api(path,opts){{
  const body=JSON.parse(opts.body);
  requests.push(body);
  archivedSids.push(body.session_id);
  return {{worktree_retained:false}};
}}
{owners_body}
{owner_row_body}
{match_body}
{batch_body}
{render_body}
_renderBatchActionBar();
(async()=>{{const button=bar.children.find(node=>node.textContent==='session_batch_archive');if(!button)throw new Error('no archive button');await button.onclick();console.log(JSON.stringify({{events,archivedSids,switchCalls,requests,activeProfile:S.activeProfile,defaultModel:window._defaultModel,activeProvider:window._activeProvider}}));}})();

"""
    data = json.loads(_run_node(script))
    assert "toast:session_batch_archive_foreign_owner" not in data["events"], (
        f"all-profiles view must not reject a known foreign owner: {data}")
    assert "confirm" in data["events"], (
        f"the confirm dialog must open for an eligible batch: {data}")
    assert data["archivedSids"] == ["w1", "w2"], data
    assert data["requests"] == [
        {"session_id": "w1", "archived": True, "profile": "work",
         "all_profiles": True},
        {"session_id": "w2", "archived": True, "profile": "work",
         "all_profiles": True},
    ], data
    assert data["switchCalls"] == 0, data
    assert data["activeProfile"] == "default", data
    assert data["defaultModel"] == "root-model", data
    assert data["activeProvider"] == "root-provider", data
    assert "exit" in data["events"], (
        f"a successful batch must exit select mode: {data}")
