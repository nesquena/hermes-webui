"""Tests for #7242: approval flyout durable dismissal and lifecycle settlement.

Covers the four-point contract from the issue:

1. Every rendered approval carries a stable actionable approval_id (idless
   legacy entries are normalized server-side and the card degrades to an
   explicit unresolved state when identity is still missing).
2. X durably dismisses the card (server deny + local dismissal that the
   pending poll honors).
3. Dismissal resolves the matching server-side pending entry.
4. Terminal runs (completed / failed / cancelled / 60s BLOCKED timeout) settle
   the approval/control-boundary state so no stale pending head re-opens the
   flyout.

Plus the gate-recertification fixes for the dismiss path:

5. The durable denial is sent ONLY with an approval_id (an identityless deny
   would consume whatever head the legacy FIFO path pops next) and the local
   dismissal is settled from the authoritative response — success or an
   authoritative 404/409 keep the card hidden; network/5xx failures restore
   the card with an error + retry instead of leaving the server approval
   silently pending.
6. The X never races an in-flight Allow/Deny (and vice-versa) on the same
   approval, and the dismiss X is labelled "Dismiss and deny" via i18n.

Backend assertions exercise the real api.route_approvals functions directly
(no server boot required); frontend assertions use the node-driver static
source extraction pattern used across the suite plus behavioral node
scenarios that execute the real approval-frontend block from static/messages.js
against a stubbed DOM.
"""
import json
import shutil
import subprocess
import tempfile
import uuid

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTE_APPROVALS_SRC = (ROOT / "api" / "route_approvals.py").read_text(encoding="utf-8")

# Import api.config FIRST: its module-level code appends the agent dir to
# sys.path (api/config.py "Inject agent dir into sys.path"), which makes
# `tools.approval` importable so route_approvals binds to the REAL shared
# state. Importing api.route_approvals before that would silently bind it to
# the ImportError fallback stubs (private _pending/_gateway_queues dicts),
# breaking module-identity assumptions for every later in-process test.
import api.config  # noqa: F401  (side effect: sys.path += agent dir)

from api.route_approvals import (
    _GATEWAY_MIRROR_FLAG,
    _GATEWAY_MIRROR_RETAINED,
    _pending,
    reconcile_gateway_pending_mirror_locked,
    retire_gateway_pending_mirror,
    submit_gateway_pending_mirror,
)


def _compact(text: str) -> str:
    return "".join(text.split())


def _fn_body(compact: str, fn_name: str) -> str:
    """Extract a function body with brace matching (nested blocks safe)."""
    start = compact.find("function" + fn_name + "(")
    assert start != -1, f"function {fn_name}( not found"
    brace = compact.find("{", start)
    assert brace != -1
    depth = 0
    for i in range(brace, len(compact)):
        ch = compact[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return compact[start:i + 1]
    raise AssertionError(f"unbalanced braces in function {fn_name}")


def _cleanup_sid(sid: str) -> None:
    _pending.pop(sid, None)


# ── Backend: idless normalization (contract 1) ────────────────────────────

def test_reconcile_mints_approval_id_for_idless_entry():
    """A legacy idless entry in the shared queue must gain a stable id."""
    sid = "test-7242-idless-" + uuid.uuid4().hex[:8]
    try:
        _pending[sid] = [
            {_GATEWAY_MIRROR_FLAG: True, "run_id": "", "description": "tirith mass deletion"},
        ]
        head, total, changed = reconcile_gateway_pending_mirror_locked(sid)
        assert head is not None
        assert str(head.get("approval_id") or "").strip(), "reconcile must mint approval_id"
        assert total == 1
        assert changed is True
        assert str(_pending[sid][0]["approval_id"] or "").strip()
    finally:
        _cleanup_sid(sid)


def test_reconcile_mints_id_only_once():
    """Repeated reconciles must keep the minted id stable (no churn)."""
    sid = "test-7242-stable-" + uuid.uuid4().hex[:8]
    try:
        _pending[sid] = [
            {_GATEWAY_MIRROR_FLAG: True, "run_id": "", "description": "x"},
        ]
        reconcile_gateway_pending_mirror_locked(sid)
        first_id = _pending[sid][0]["approval_id"]
        head, total, changed = reconcile_gateway_pending_mirror_locked(sid)
        assert head["approval_id"] == first_id
        assert changed is False, "id must be stable across reconciles"
    finally:
        _cleanup_sid(sid)


def test_submit_gateway_pending_mirror_mints_id_for_orphan():
    """An orphan mirror (no run_id, no request_id, no live producer) must
    still reach the client with an actionable approval_id."""
    sid = "test-7242-orphan-" + uuid.uuid4().hex[:8]
    try:
        head, total = submit_gateway_pending_mirror(
            sid, {"description": "legacy no-identity approval"}
        )
        assert head is not None
        assert str(head.get("approval_id") or "").strip(), (
            "submit_gateway_pending_mirror must mint approval_id for orphan mirrors"
        )
        assert total == 1
    finally:
        _cleanup_sid(sid)


# ── Backend: terminal settlement (contract 4) ─────────────────────────────

def test_retire_with_run_id_clears_plain_and_mirror_entries():
    """A terminated run must clear BOTH gateway mirrors and plain local
    pending entries bound to it (BLOCKED timeout leaves no pending head)."""
    sid = "test-7242-run-" + uuid.uuid4().hex[:8]
    run_id = "run-7242-" + uuid.uuid4().hex[:8]
    try:
        _pending[sid] = [
            {"approval_id": "plain-1", "run_id": run_id, "description": "local"},
            {
                _GATEWAY_MIRROR_FLAG: True,
                "approval_id": "mirror-1",
                "run_id": run_id,
                "_gateway_mirror_token": "tok",
            },
            {"approval_id": "other-run", "run_id": "run-other", "description": "keep"},
        ]
        retired = retire_gateway_pending_mirror(sid, run_id=run_id)
        assert retired is True
        remaining = _pending.get(sid) or []
        remaining_ids = [e.get("approval_id") for e in remaining]
        assert "plain-1" not in remaining_ids, "plain run entry must be retired"
        assert "mirror-1" not in remaining_ids, "run mirror must be retired"
        assert "other-run" in remaining_ids, "unrelated run entries must survive"
    finally:
        _cleanup_sid(sid)


def test_retire_teardown_preserves_plain_entries_retires_no_run_mirrors():
    """Session teardown (no run_id) must retire no-run gateway mirrors but
    preserve plain local entries, whose embedded-agent producer may still be
    blocked on the approval."""
    sid = "test-7242-teardown-" + uuid.uuid4().hex[:8]
    try:
        _pending[sid] = [
            {"approval_id": "plain-1", "description": "local"},
            {
                _GATEWAY_MIRROR_FLAG: True,
                "approval_id": "mirror-1",
                "run_id": "r1",
                "_gateway_mirror_token": "tok1",
            },
            {_GATEWAY_MIRROR_RETAINED: True, _GATEWAY_MIRROR_FLAG: True, "approval_id": "ret-1"},
        ]
        retire_gateway_pending_mirror(sid)
        remaining = _pending.get(sid) or []
        remaining_ids = [e.get("approval_id") for e in remaining]
        assert "plain-1" in remaining_ids, "plain local entries must survive teardown"
        assert "mirror-1" not in remaining_ids, "run mirror must be retired"
        assert "ret-1" not in remaining_ids, "no-run mirror must be retired"
    finally:
        _cleanup_sid(sid)


# ── Frontend: durable dismiss (contracts 2 + 3 + gate fixes) ──────────────

def test_dismiss_approval_card_sends_deny_to_server():
    """X must resolve the server-side pending entry, not just hide locally."""
    body = _fn_body(_compact(MESSAGES_JS), "dismissApprovalCard")
    assert '"/api/approval/respond"' in body, "dismiss must POST to /api/approval/respond"
    assert 'choice:"deny"' in body, "dismiss must send choice deny"


def test_dismiss_approval_card_keeps_local_dismissal_behavior():
    """The optimistic local dismissal behavior must be preserved: the card is
    hidden and the local projection cleared immediately, then settled."""
    body = _fn_body(_compact(MESSAGES_JS), "dismissApprovalCard")
    assert "_markApprovalDismissed(ownerSid,ownerApprovalId)" in body
    assert "hideApprovalCard(true)" in body
    assert "_clearApprovalPendingForSession(ownerSid)" in body


def test_dismiss_deny_always_carries_approval_id():
    """The durable deny must include approval_id unconditionally (a body
    without it would hit the legacy no-id FIFO head path)."""
    body = _fn_body(_compact(MESSAGES_JS), "dismissApprovalCard")
    assert "approval_id:ownerApprovalId" in body, (
        "deny body must embed the captured approval_id unconditionally"
    )
    assert "body:JSON.stringify(body)" in body
    # The only api() calls in the whole function are the identified deny POST
    # and the read-only pending re-fetch (both carry the captured identity):
    # an idless dismiss must never emit a response.
    assert body.count("api(") == 2, "idless dismiss must never reach api()"
    assert '"/api/approval/respond"' in body
    assert '"/api/approval/pending?session_id="' in body


def test_dismiss_idless_card_hides_without_response():
    """An idless/legacy card hides locally but must never emit a response —
    an identityless deny would consume whatever head the legacy FIFO path
    pops next (the user never saw that approval)."""
    body = _fn_body(_compact(MESSAGES_JS), "dismissApprovalCard")
    guard_start = body.find("_captureApprovalResponseOwner()")
    assert guard_start != -1, "dismiss must capture the response owner"
    local_start = body.find("if(!owner){")
    local_end = body.find("_approvalPendingBySession.get(ownerSid)")
    assert local_start != -1 and local_end != -1
    local_only = body[local_start:local_end]
    assert "hideApprovalCard(true)" in local_only
    assert "_clearApprovalPendingForSession(sid)" in local_only
    assert "api(" not in local_only, "idless path must not POST"


def test_dismiss_guard_blocks_x_during_inflight_response():
    """The X must never race an in-flight Allow/Deny response owner into a
    concurrent deny on the same approval."""
    body = _fn_body(_compact(MESSAGES_JS), "dismissApprovalCard")
    guard = "if(approvalId&&_approvalResponseMatches(sid,approvalId))return;"
    assert guard in body, "dismiss must bail out when a response owner is in flight"
    assert body.find(guard) < body.find("_markApprovalDismissed("), (
        "the in-flight guard must run before any state is changed"
    )


def test_dismiss_rollback_restores_card_after_failure():
    """Network/5xx failures must restore the card: remove the local dismissal
    marker, restore the pending projection snapshot, re-enable controls and
    surface an error (the card is the retry affordance)."""
    body = _fn_body(_compact(MESSAGES_JS), "dismissApprovalCard")
    assert "_unmarkApprovalDismissed(ownerSid,ownerApprovalId)" in body
    assert "_approvalPendingBySession.set(ownerSid,snapshot)" in body, (
        "rollback must restore the pending projection snapshot"
    )
    assert "_restoreFailedApprovalResponse(owner,errMsg)" in body
    assert "Tryagain." in body, (
        "the failure path must surface an explicit retry affordance"
    )


def test_dismiss_404_is_authoritative_terminal():
    """A 404 (the entry or its session no longer exists server-side) is
    authoritative — it keeps the dismissal and never re-renders."""
    body = _fn_body(_compact(MESSAGES_JS), "dismissApprovalCard")
    assert "err.status===404" in body, "catch must keep 404 as an authoritative terminal"
    term_start = body.find("err.status===404")
    assert "_releaseApprovalResponseOwner(owner)" in body[term_start:term_start + 160], (
        "404 must release the response owner and keep the card hidden"
    )


def test_dismiss_409_requires_structured_code_before_terminality():
    """An HTTP 409 alone is NOT proof of settlement: the catch must parse the
    structured error code first, restore the card for the retryable codes
    (gateway_approval_in_progress / gateway_run_unavailable) and keep it
    hidden only when the re-fetched pending state is authoritatively
    absent/settled."""
    body = _fn_body(_compact(MESSAGES_JS), "dismissApprovalCard")
    assert "JSON.parse(err.body)" in body, "catch must parse the structured error body"
    assert 'code==="gateway_approval_in_progress"' in body, (
        "in-progress 409s are retryable — must restore, never hide"
    )
    assert 'code==="gateway_run_unavailable"' in body, (
        "run-unavailable 409s need the re-fetch decision path"
    )
    assert '"/api/approval/pending?session_id="' in body, (
        "run-unavailable must re-fetch the authoritative pending state"
    )
    # The only bare-status terminal left is 404; the 409 branch must not
    # contain a status-only release.
    parse_start = body.find("JSON.parse(err.body)")
    code_idx = body.find('code==="gateway_approval_in_progress"')
    assert parse_start != -1 and code_idx != -1
    assert parse_start < body.find("err.status===404") < code_idx, (
        "404 may stay terminal, but 409 handling must be code-driven"
    )


def test_dismiss_label_says_dismiss_and_deny_via_i18n():
    """The X now really denies — its accessible name and title must say
    'Dismiss and deny' through the normal i18n path (en bundle key +
    data-i18n attributes, with a literal English fallback in the markup)."""
    assert 'aria-label="Dismiss and deny"' in INDEX_HTML
    assert 'title="Dismiss and deny"' in INDEX_HTML
    assert 'data-i18n-aria-label="approval_dismiss_deny"' in INDEX_HTML
    assert 'data-i18n-title="approval_dismiss_deny"' in INDEX_HTML
    assert "approval_dismiss_deny: 'Dismiss and deny'" in I18N_JS, (
        "the en i18n bundle must carry the approval_dismiss_deny key"
    )


def test_poll_skips_dismissed_pending_head():
    """The fallback poll must not re-render a durably dismissed head."""
    compact = _compact(MESSAGES_JS)
    poll_start = compact.find("function_startApprovalFallbackPoll(")
    assert poll_start != -1
    poll_body_end = compact.find("functionstopApprovalPollingForSession(", poll_start)
    poll_body = compact[poll_start:poll_body_end]
    assert "_isApprovalDismissed(sid," in poll_body, (
        "poll must check the dismissal set before rendering the pending head"
    )
    dismiss_check = poll_body.find("_isApprovalDismissed(sid,")
    render_call = poll_body.find("showApprovalForSession(sid,data.pending")
    assert dismiss_check != -1 and render_call != -1
    assert dismiss_check < render_call, (
        "the dismissal check must gate the showApprovalForSession render"
    )


# ── Frontend: idless card degrades to unresolved state (contract 1) ───────

def test_show_approval_card_disables_controls_without_identity():
    """Without an actionable approval_id the card must render disabled rather
    than silently no-op on Allow/Deny."""
    compact = _compact(MESSAGES_JS)
    func_start = compact.find("functionshowApprovalCard(")
    assert func_start != -1
    # Locate the idless guard after the responding-controls block.
    guard = "_setApprovalControlsDisabled(null,true)"
    assert guard in compact[func_start:], (
        "showApprovalCard must disable action controls when identity is missing"
    )
    responding_block = compact.find("_approvalResponseMatches(sid,_approvalCurrentId)", func_start)
    guard_idx = compact.find(guard, func_start)
    assert responding_block != -1 and guard_idx != -1
    assert responding_block < guard_idx, (
        "the idless guard must run after the normal responding-controls update"
    )


# ── Behavioral scenarios: real approval-frontend block under a stubbed DOM ─

NODE = shutil.which("node")

_JS_PRELUDE = r'''
// ── harness prelude: stubs for the approval flyout block of messages.js ──
const els = {};
function makeEl(id) {
  const cls = new Set();
  const el = {
    id, hidden: false, disabled: false, textContent: "", title: "", inert: false,
    style: { display: "", setProperty() {}, removeProperty() {}, getPropertyValue() { return ""; } },
    classList: {
      add(c) { cls.add(c); },
      remove(c) { cls.delete(c); },
      contains(c) { return cls.has(c); },
      toggle(c, on) {
        if (on === undefined) { if (cls.has(c)) { cls.delete(c); } else { cls.add(c); } }
        else if (on) { cls.add(c); } else { cls.delete(c); }
      },
    },
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
    },
    removeAttribute(k) { delete this.attrs[k]; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    focus() {},
    addEventListener() {},
    removeEventListener() {},
    getBoundingClientRect() { return { height: 140, width: 480 }; },
    offsetHeight: 140, scrollHeight: 140, clientHeight: 140, offsetWidth: 480,
  };
  return el;
}
["approvalCard", "approvalDesc", "approvalCmd", "approvalCounter", "approvalCollapse",
 "approvalBtnOnce", "approvalBtnSession", "approvalBtnAlways", "approvalBtnDeny",
 "approvalSkipAll", "messages", "msg"].forEach((id) => { els[id] = makeEl(id); });
function $(id) { return els[id] || null; }

let S = { session: null };
let _loadSessionGeneration = 0;
let _yoloEnabled = false;
const toasts = [];
const statuses = [];
function showToast(m) { toasts.push(m); }
function setStatus(m) { statuses.push(m); }
function syncTopbar() {}
function _updateYoloPill() {}
function t(key) { return key; }
globalThis.document = { activeElement: null };
const _lsStore = new Map();
globalThis.localStorage = {
  getItem: (k) => (_lsStore.has(k) ? _lsStore.get(k) : null),
  setItem: (k, v) => { _lsStore.set(k, String(v)); },
  removeItem: (k) => { _lsStore.delete(k); },
};
globalThis.setTimeout = (fn) => { if (typeof fn === "function") { fn(); } return 1; };
globalThis.clearTimeout = () => {};

let apiCalls = [];
let apiImpl = async () => { throw new Error("no apiImpl configured"); };
async function api(path, opts) {
  let body = null;
  if (opts && typeof opts.body === "string") {
    try { body = JSON.parse(opts.body); } catch (_) { body = null; }
  }
  apiCalls.push({ path, method: opts ? opts.method : "GET", body });
  return apiImpl(path, opts, body);
}
const flush = () => new Promise((r) => setImmediate(r));
function assertTrue(v, msg) { if (!v) { throw new Error(msg); } }
function assertEq(a, b, msg) { if (a !== b) { throw new Error(msg + " | expected=" + JSON.stringify(b) + " got=" + JSON.stringify(a)); } }
function cardVisible() { return els.approvalCard.classList.contains("visible"); }
function showApproval(pending, sid) {
  S.session = { session_id: sid };
  showApprovalCard(pending, 1);
}
'''

_JS_MAIN_RUNNER = r'''
(async () => {
  try {
    const out = await main() || {};
    process.stdout.write(JSON.stringify(out));
  } catch (e) {
    console.error("SCENARIO FAILED: " + (e && e.stack ? e.stack : String(e)));
    process.exit(1);
  }
})();
'''


def _approval_frontend_block(src: str) -> str:
    """Extract the full approval frontend state + helpers: from the state vars
    that open the block up to the end of respondApproval()."""
    start = src.index("let _approvalHideTimer = null;")
    anchor = "async function respondApproval("
    aidx = src.index(anchor)
    # The opening brace of the body is the LAST brace on the header line
    # (default params like `options = {}` open earlier).
    header_end = src.index("\n", aidx)
    brace = src.rfind("{", aidx, header_end)
    if brace == -1:
        brace = src.index("{", aidx)
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("unbalanced braces in approval frontend block")


def _run_node_scenario(scene_body: str) -> dict:
    assert NODE, "node is required for the behavioral scenarios"
    block = _approval_frontend_block(MESSAGES_JS)
    script = _JS_PRELUDE + "\n" + block + "\n" + scene_body + "\n" + _JS_MAIN_RUNNER
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
        tf.write(script)
        path = tf.name
    try:
        proc = subprocess.run([NODE, path], capture_output=True, text=True, timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert proc.returncode == 0, f"node scenario failed:\n{proc.stderr}\n---\n{proc.stdout}"
    return json.loads(proc.stdout)


def test_node_idless_dismiss_never_posts_and_hides():
    """An idless stale card must hide locally without emitting any response —
    it can never consume the next FIFO head's denial."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ description: "legacy no-identity cmd", command: "ls" }, "sidA");
  assertTrue(cardVisible(), "idless card renders");
  dismissApprovalCard();
  await flush();
  assertEq(apiCalls.length, 0, "idless dismiss must never POST");
  assertTrue(!cardVisible(), "idless card hides locally");
  assertTrue(!_approvalPendingBySession.has("sidA"), "local projection cleared");
  return { apiCalls: apiCalls.length };
}
''')
    assert out["apiCalls"] == 0


def test_node_dismiss_with_id_denies_matching_approval():
    """X with a real approval_id must deny exactly that approval and settle
    the local state (hidden card, cleared projection, durable marker)."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", run_id: "", description: "rm -rf" }, "sidA");
  assertTrue(cardVisible(), "card visible");
  apiImpl = async () => ({ ok: true });
  dismissApprovalCard();
  await flush();
  assertEq(apiCalls.length, 1, "exactly one POST");
  assertEq(apiCalls[0].path, "/api/approval/respond", "endpoint");
  assertEq(apiCalls[0].body.choice, "deny", "choice deny");
  assertEq(apiCalls[0].body.approval_id, "a1", "approval_id sent unconditionally");
  assertEq(apiCalls[0].body.session_id, "sidA", "session id");
  assertTrue(!cardVisible(), "card hidden after success");
  assertTrue(!_approvalPendingBySession.has("sidA"), "projection cleared");
  assertTrue(_isApprovalDismissed("sidA", "a1"), "durable marker set");
  return { calls: apiCalls.length };
}
''')
    assert out["calls"] == 1


def test_node_dismiss_network_failure_restores_card():
    """A network failure must restore the card + controls and drop the local
    dismissal marker — the server approval is still pending and must remain
    visible/re-renderable (no silent blocked-agent state)."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", description: "cmd" }, "sidA");
  apiImpl = async () => { throw new Error("network down"); };
  dismissApprovalCard();
  assertTrue(!cardVisible(), "optimistic hide");
  assertTrue(_isApprovalDismissed("sidA", "a1"), "optimistic marker");
  await flush();
  assertTrue(cardVisible(), "card restored after network failure");
  assertTrue(!_isApprovalDismissed("sidA", "a1"), "dismissal marker removed on failure");
  assertEq(apiCalls.length, 1, "single POST attempted");
  assertTrue(toasts.length >= 1, "error toast shown");
  assertEq(els.approvalBtnDeny.disabled, false, "controls re-enabled");
  return { visible: cardVisible(), toasts: toasts.length };
}
''')
    assert out["visible"] is True
    assert out["toasts"] >= 1


def test_node_dismiss_5xx_failure_restores_card():
    """A 5xx gateway failure is not authoritative — the card must come back
    with an error + retry affordance."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", description: "cmd" }, "sidA");
  const err = new Error("boom");
  err.status = 500;
  err.body = JSON.stringify({ error: "server exploded" });
  apiImpl = async () => { throw err; };
  dismissApprovalCard();
  await flush();
  assertTrue(cardVisible(), "5xx restores the card");
  assertTrue(!_isApprovalDismissed("sidA", "a1"), "marker removed on 5xx");
  assertTrue(toasts.length >= 1, "error toast with retry shown");
  assertTrue(cardVisible(), "card is the retry affordance");
  return { visible: cardVisible(), toasts: toasts.length };
}
''')
    assert out["visible"] is True


def test_node_dismiss_404_keeps_hidden():
    """An authoritative 404 (the entry or its session is gone) keeps the
    dismissal — never restores a card whose server entry no longer exists."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", description: "cmd" }, "sidA");
  const err = new Error("not found");
  err.status = 404;
  apiImpl = async () => { throw err; };
  dismissApprovalCard();
  await flush();
  assertTrue(!cardVisible(), "404 keeps the card hidden");
  assertTrue(_isApprovalDismissed("sidA", "a1"), "dismissal stands after 404");
  assertEq(toasts.length, 0, "no restore toast for authoritative 404");
  assertEq(_approvalResponding, null, "response owner released");
  return { visible: cardVisible() };
}
''')
    assert out["visible"] is False


def test_node_dismiss_409_without_code_restores_card():
    """A 409 without a structured body/code is a proxy artifact, not backend
    settlement — the card must come back with a retry affordance."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", description: "cmd" }, "sidA");
  const err = new Error("conflict");
  err.status = 409;
  apiImpl = async () => { throw err; };
  dismissApprovalCard();
  await flush();
  assertTrue(cardVisible(), "unstructured 409 restores the card");
  assertTrue(!_isApprovalDismissed("sidA", "a1"), "marker removed on unstructured 409");
  assertTrue(toasts.length >= 1, "restore toast with retry shown");
  assertEq(_approvalResponding, null, "response owner released");
  return { visible: cardVisible() };
}
''')
    assert out["visible"] is True


def test_node_dismiss_409_in_progress_restores_card():
    """A retryable 409 gateway_approval_in_progress means another response
    owns the run — the exact approval is NOT settled. The card must be
    unmarked and restored so the user keeps the deny affordance."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", description: "cmd" }, "sidA");
  const err = new Error("conflict");
  err.status = 409;
  err.body = JSON.stringify({ ok: false, code: "gateway_approval_in_progress", error: "Another approval response for this Gateway run is already in progress. Wait for it to finish, then retry if the card is still visible." });
  apiImpl = async () => { throw err; };
  dismissApprovalCard();
  await flush();
  assertTrue(cardVisible(), "in-progress 409 restores the card");
  assertTrue(!_isApprovalDismissed("sidA", "a1"), "dismissal marker removed");
  assertEq(toasts.length, 1, "restore toast with the backend message");
  assertEq(els.approvalBtnDeny.disabled, false, "controls re-enabled");
  assertEq(apiCalls.length, 1, "no extra call from the restore path");
  return { visible: cardVisible() };
}
''')
    assert out["visible"] is True


def test_node_dismiss_409_run_unavailable_retained_restores_card():
    """A retryable 409 gateway_run_unavailable whose re-fetch shows the exact
    mirror still pending must restore the card (the deny remains possible)."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", run_id: "r1", _gateway_mirror_token: "t1", description: "cmd" }, "sidA");
  const err = new Error("conflict");
  err.status = 409;
  err.body = JSON.stringify({ ok: false, code: "gateway_run_unavailable", error: "Gateway approval could not be relayed because the active run is unavailable." });
  apiImpl = async (path) => {
    if (path.indexOf("/approval/pending") !== -1) {
      return { pending: { approval_id: "a1", run_id: "r1", _gateway_mirror_token: "t1", description: "cmd" }, pending_count: 1 };
    }
    throw err;
  };
  dismissApprovalCard();
  await flush();
  assertTrue(cardVisible(), "retained run-unavailable mirror restores the card");
  assertTrue(!_isApprovalDismissed("sidA", "a1"), "dismissal marker removed");
  assertTrue(toasts.length >= 1, "restore toast shown");
  assertEq(apiCalls.length, 2, "deny POST + pending re-fetch");
  assertEq(_approvalResponding, null, "response owner released");
  return { visible: cardVisible() };
}
''')
    assert out["visible"] is True


def test_node_dismiss_409_run_unavailable_settled_keeps_hidden():
    """A 409 gateway_run_unavailable whose re-fetch shows the captured tuple
    is authoritatively absent/settled keeps the dismissal hidden — no zombie
    card for an approval that is gone server-side."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", run_id: "r1", _gateway_mirror_token: "t1", description: "cmd" }, "sidA");
  const err = new Error("conflict");
  err.status = 409;
  err.body = JSON.stringify({ ok: false, code: "gateway_run_unavailable", error: "Gateway approval could not be relayed because the active run is unavailable." });
  apiImpl = async (path) => {
    if (path.indexOf("/approval/pending") !== -1) {
      return { pending: null, pending_count: 0 };
    }
    throw err;
  };
  dismissApprovalCard();
  await flush();
  assertTrue(!cardVisible(), "settled mirror keeps the card hidden");
  assertTrue(_isApprovalDismissed("sidA", "a1"), "dismissal stands when settled");
  assertEq(toasts.length, 0, "no restore toast when authoritatively settled");
  assertEq(_approvalResponding, null, "response owner released");
  assertEq(apiCalls.length, 2, "deny POST + pending re-fetch");
  return { visible: cardVisible() };
}
''')
    assert out["visible"] is False


def test_node_allow_inflight_blocks_x():
    """Permutation 1: with an Allow in flight, the X must not emit a
    concurrent deny — it is a no-op while the response owner is active."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", description: "cmd" }, "sidA");
  let resolveApi;
  apiImpl = () => new Promise((r) => { resolveApi = r; });
  const pAllow = respondApproval("always");
  await flush();
  assertEq(apiCalls.length, 1, "allow in flight");
  dismissApprovalCard();
  await flush();
  assertEq(apiCalls.length, 1, "X must not race an in-flight Allow/Deny");
  assertTrue(cardVisible(), "card stays visible while allow is in flight");
  resolveApi({ ok: true });
  await pAllow;
  await flush();
  assertTrue(!cardVisible(), "allow settles and hides the card");
  assertEq(_approvalResponding, null, "response owner released");
  return { calls: apiCalls.length };
}
''')
    assert out["calls"] == 1


def test_node_dismiss_inflight_blocks_allow():
    """Permutation 2: with the X deny in flight, an Allow click must be
    rejected (no concurrent POST) and the deny settles the card hidden."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", description: "cmd" }, "sidA");
  let resolveApi;
  apiImpl = () => new Promise((r) => { resolveApi = r; });
  dismissApprovalCard();
  await flush();
  assertEq(apiCalls.length, 1, "deny in flight");
  assertEq(apiCalls[0].body.choice, "deny", "in-flight choice is deny");
  const allowResult = await respondApproval("always");
  assertEq(allowResult, false, "Allow rejected while the X deny is in flight");
  assertEq(apiCalls.length, 1, "no concurrent allow POST");
  resolveApi({ ok: true });
  await flush();
  assertTrue(!cardVisible(), "deny settles and keeps the card hidden");
  assertEq(_approvalResponding, null, "response owner released");
  return { calls: apiCalls.length };
}
''')
    assert out["calls"] == 1


def test_node_successor_head_is_interactive_after_dismiss():
    """A later successor head (B) arriving after A was dismissed must render
    fully interactive — the released owner must not leak into B — and B's X
    denies B by its own id."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", description: "cmd A" }, "sidA");
  apiImpl = async () => ({ ok: true });
  dismissApprovalCard();
  await flush();
  assertTrue(!cardVisible(), "A dismissed");
  assertEq(_approvalResponding, null, "response owner released");
  showApproval({ approval_id: "b2", description: "cmd B" }, "sidA");
  assertTrue(cardVisible(), "successor B renders");
  assertEq(els.approvalBtnAlways.disabled, false, "B controls enabled");
  dismissApprovalCard();
  await flush();
  assertEq(apiCalls.length, 2, "second deny for B");
  assertEq(apiCalls[1].body.approval_id, "b2", "successor denied by its own id");
  assertTrue(!cardVisible(), "B dismissed");
  return { calls: apiCalls.length };
}
''')
    assert out["calls"] == 2


def test_node_dismiss_409_run_unavailable_refetch_rejected_5xx_restores_card():
    """A rejected/5xx re-fetch is NOT an authoritative absence: the untouched
    `pending === null` must not settle the dismissal, so the card comes back
    and the user can deny again."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", run_id: "r1", _gateway_mirror_token: "t1", description: "cmd A" }, "sidA");
  const err = new Error("conflict");
  err.status = 409;
  err.body = JSON.stringify({ ok: false, code: "gateway_run_unavailable", error: "Gateway approval could not be relayed because the active run is unavailable." });
  const boom = new Error("bad gateway");
  boom.status = 503;
  apiImpl = async (path) => {
    if (path.indexOf("/approval/pending") !== -1) throw boom;
    throw err;
  };
  dismissApprovalCard();
  await flush();
  assertTrue(cardVisible(), "failed re-fetch restores the card (fail closed)");
  assertTrue(!_isApprovalDismissed("sidA", "a1"), "unproven dismissal marker dropped");
  assertEq(els.approvalBtnDeny.disabled, false, "restored controls are usable");
  assertTrue(toasts.length >= 1, "retry toast shown");
  assertEq(_approvalResponding, null, "response owner released");
  assertEq(apiCalls.length, 2, "deny POST + attempted re-fetch");
  apiImpl = async () => ({ ok: true });
  dismissApprovalCard();
  await flush();
  assertEq(apiCalls.length, 3, "the restored card can be denied again");
  assertEq(apiCalls[2].body.approval_id, "a1", "retry denies the same approval");
  assertEq(apiCalls[2].body.run_id, "r1", "retry keeps the captured run ownership");
  assertTrue(!cardVisible(), "retry settles hidden");
  return { visible: cardVisible() };
}
''')
    assert out["visible"] is False


def test_node_dismiss_409_run_unavailable_refetch_without_pending_field_restores_card():
    """A re-fetch that resolves to `undefined`, or to a payload without a
    `pending` field, carries no authority over the dismissal either."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", description: "cmd A" }, "sidA");
  const err = new Error("conflict");
  err.status = 409;
  err.body = JSON.stringify({ ok: false, code: "gateway_run_unavailable", error: "unavailable" });
  let variant = 0;
  apiImpl = async (path) => {
    if (path.indexOf("/approval/pending") !== -1) {
      variant += 1;
      return variant === 1 ? undefined : { ok: true, pending_count: 1 };
    }
    throw err;
  };
  dismissApprovalCard();
  await flush();
  assertTrue(cardVisible(), "an undefined re-fetch payload restores the card");
  assertTrue(!_isApprovalDismissed("sidA", "a1"), "marker dropped for an undefined payload");
  assertEq(_approvalResponding, null, "response owner released");
  dismissApprovalCard();
  await flush();
  assertTrue(cardVisible(), "a re-fetch without a pending field restores the card");
  assertTrue(!_isApprovalDismissed("sidA", "a1"), "marker dropped for a missing pending field");
  assertEq(apiCalls.length, 4, "two deny POSTs + two non-authoritative re-fetches");
  return { visible: cardVisible() };
}
''')
    assert out["visible"] is True


def test_node_dismiss_409_run_unavailable_moved_session_failed_refetch_stays_fail_closed():
    """Session/generation movement during the await must not leak a restore
    into the new view — but the unproven dismissal still cannot stand, so the
    card returns when the user goes back to the dismissed session."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", run_id: "r1", _gateway_mirror_token: "t1", description: "cmd A" }, "sidA");
  const err = new Error("conflict");
  err.status = 409;
  err.body = JSON.stringify({ ok: false, code: "gateway_run_unavailable", error: "unavailable" });
  let releaseFetch;
  const gate = new Promise((r) => { releaseFetch = r; });
  apiImpl = async (path) => {
    if (path.indexOf("/approval/pending") !== -1) { await gate; throw err; }
    throw err;
  };
  dismissApprovalCard();
  await flush();
  // the user switches session while the deny's re-fetch is still in flight
  S.session = { session_id: "sidB" };
  _loadSessionGeneration += 1;
  releaseFetch();
  await flush();
  assertTrue(!cardVisible(), "no card is restored into the moved-away view");
  assertTrue(!_approvalPendingBySession.has("sidB"), "the new session projection is untouched");
  assertTrue(!_isApprovalDismissed("sidA", "a1"), "unproven dismissal marker dropped after movement");
  assertEq(_approvalResponding, null, "response owner released after movement");
  // back on sidA the still-unsettled approval is actionable again
  S.session = { session_id: "sidA" };
  _renderPendingApprovalForActiveSession();
  assertTrue(cardVisible(), "returning to sidA re-shows the still-pending approval");
  assertEq(els.approvalBtnDeny.disabled, false, "the re-shown card stays usable");
  return { visible: cardVisible() };
}
''')
    assert out["visible"] is True


def test_node_dismiss_409_run_unavailable_moved_session_authoritative_absence_stays_terminal():
    """Control: a successfully decoded authoritative absence still settles the
    dismissal (hidden, marker kept) even when the view moved on."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", description: "cmd A" }, "sidA");
  const err = new Error("conflict");
  err.status = 409;
  err.body = JSON.stringify({ ok: false, code: "gateway_run_unavailable", error: "unavailable" });
  let releaseFetch;
  const gate = new Promise((r) => { releaseFetch = r; });
  apiImpl = async (path) => {
    if (path.indexOf("/approval/pending") !== -1) { await gate; return { pending: null, pending_count: 0 }; }
    throw err;
  };
  dismissApprovalCard();
  await flush();
  S.session = { session_id: "sidB" };
  _loadSessionGeneration += 1;
  releaseFetch();
  await flush();
  assertTrue(_isApprovalDismissed("sidA", "a1"), "authoritative absence keeps the marker");
  assertEq(toasts.length, 0, "no restore toast for a settled dismissal");
  assertEq(_approvalResponding, null, "response owner released");
  S.session = { session_id: "sidA" };
  _renderPendingApprovalForActiveSession();
  assertTrue(!cardVisible(), "the settled approval never re-renders");
  return { visible: cardVisible() };
}
''')
    assert out["visible"] is False


def test_node_dismiss_409_run_unavailable_successor_rendered_during_refetch_is_preserved():
    """A successor head rendered by a parallel poll while the re-fetch is in
    flight owns the card: it must stay visible and interactive."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", run_id: "r1", _gateway_mirror_token: "t1", description: "cmd A" }, "sidA");
  const err = new Error("conflict");
  err.status = 409;
  err.body = JSON.stringify({ ok: false, code: "gateway_run_unavailable", error: "unavailable" });
  let releaseFetch;
  const gate = new Promise((r) => { releaseFetch = r; });
  apiImpl = async (path) => {
    if (path.indexOf("/approval/pending") !== -1) { await gate; throw err; }
    throw err;
  };
  dismissApprovalCard();
  await flush();
  showApproval({ approval_id: "b2", description: "cmd B" }, "sidA");
  assertTrue(cardVisible(), "successor B renders during the in-flight re-fetch");
  releaseFetch();
  await flush();
  assertTrue(cardVisible(), "the successor survives the failed re-fetch");
  assertEq(els.approvalDesc.textContent, "cmd B", "the successor still owns the card");
  assertEq(els.approvalBtnAlways.disabled, false, "successor controls stay enabled");
  assertTrue(!_isApprovalDismissed("sidA", "b2"), "the successor is never suppressed");
  assertEq(_approvalResponding, null, "our response owner released");
  apiImpl = async () => ({ ok: true });
  dismissApprovalCard();
  await flush();
  assertEq(apiCalls[2].body.approval_id, "b2", "the successor is denied by its own id");
  return { calls: apiCalls.length };
}
''')
    assert out["calls"] == 3


def test_node_dismiss_409_run_unavailable_different_head_renders_successor():
    """A different queue head returned by an authoritative re-fetch must be
    rendered and usable, while the dismissed tuple keeps its own marker."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", run_id: "r1", _gateway_mirror_token: "t1", description: "cmd A" }, "sidA");
  const err = new Error("conflict");
  err.status = 409;
  err.body = JSON.stringify({ ok: false, code: "gateway_run_unavailable", error: "unavailable" });
  apiImpl = async (path) => {
    if (path.indexOf("/approval/pending") !== -1) {
      return { pending: { approval_id: "b2", description: "cmd B" }, pending_count: 2 };
    }
    throw err;
  };
  dismissApprovalCard();
  await flush();
  assertTrue(cardVisible(), "the live successor head is rendered");
  assertEq(els.approvalDesc.textContent, "cmd B", "the successor head owns the card");
  assertEq(els.approvalBtnAlways.disabled, false, "successor controls enabled");
  assertTrue(_isApprovalDismissed("sidA", "a1"), "the dismissed tuple keeps its marker");
  assertTrue(!_isApprovalDismissed("sidA", "b2"), "the successor is never suppressed");
  assertEq(_approvalResponding, null, "response owner released");
  apiImpl = async () => ({ ok: true });
  dismissApprovalCard();
  await flush();
  assertEq(apiCalls[2].body.approval_id, "b2", "the successor is denied by its own id");
  assertTrue(!cardVisible(), "successor settles hidden");
  return { calls: apiCalls.length };
}
''')
    assert out["calls"] == 3


def test_node_dismiss_409_run_unavailable_same_id_reowned_tuple_renders():
    """The server may reuse an approval_id under a different run/mirror
    ownership: the stale marker must not suppress that tuple, and the deny
    must carry the new ownership."""
    out = _run_node_scenario(r'''
async function main() {
  showApproval({ approval_id: "a1", run_id: "r1", _gateway_mirror_token: "t1", description: "cmd run1" }, "sidA");
  const err = new Error("conflict");
  err.status = 409;
  err.body = JSON.stringify({ ok: false, code: "gateway_run_unavailable", error: "unavailable" });
  apiImpl = async (path) => {
    if (path.indexOf("/approval/pending") !== -1) {
      return { pending: { approval_id: "a1", run_id: "r2", _gateway_mirror_token: "t2", description: "cmd run2" }, pending_count: 1 };
    }
    throw err;
  };
  dismissApprovalCard();
  await flush();
  assertTrue(cardVisible(), "the re-owned tuple is rendered");
  assertEq(els.approvalDesc.textContent, "cmd run2", "the re-owned tuple owns the card");
  assertTrue(!_isApprovalDismissed("sidA", "a1"), "the stale marker no longer suppresses the id");
  assertEq(_approvalDisplayedOwner && _approvalDisplayedOwner.runId, "r2", "displayed owner is the new run");
  assertEq(_approvalResponding, null, "response owner released");
  apiImpl = async () => ({ ok: true });
  dismissApprovalCard();
  await flush();
  assertEq(apiCalls[2].body.approval_id, "a1", "the re-owned tuple is denied by the same id");
  assertEq(apiCalls[2].body.run_id, "r2", "the deny carries the new run ownership");
  assertEq(apiCalls[2].body.mirror_token, "t2", "the deny carries the new mirror ownership");
  return { calls: apiCalls.length };
}
''')
    assert out["calls"] == 3
