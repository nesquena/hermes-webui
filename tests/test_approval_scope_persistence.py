"""Session/always grants: the Agent owns them for approvals it is waiting on.

Once WebUI binds the Agent's real ``approve_session``/``approve_permanent``
(#9119), WebUI saving a session/always answer itself skips the Agent's
per-approval scope rule: the Agent offers only "once" for a one-operation
finding (a smart-DENY owner override, a manual-only osascript class, an
incomplete scan) and does not save a broader answer an older client returns.
WebUI's own save also ran in the HTTP thread, outside the session's profile.

``_resolve_approval_legacy`` now leaves the grant to the Agent whenever an Agent
is waiting on the answer, saves nothing for any gateway mirror (an orphaned
mirror's answer was never used), and for raw entries no Agent waits on it saves
only within the scopes the entry offered (``allow_session``/``allow_permanent``/
``smart_denied``; missing flags keep the answer as given) and only in the
session's own profile (tests/test_approval_profile_scoped_grants.py): through the
Agent's ``_persist_choice`` when it takes plain keys, else WebUI grants and saves
only the keys just granted. Every allowlist save in the process is serialized.

The unit tests record WebUI's grant calls. The end-to-end tests drive the real
Agent gate in a subprocess, answer through WebUI's respond route, and check
whether the next matching command is asked again and who saved what.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import models
from api import route_approvals
from api import routes

REPO_ROOT = Path(__file__).resolve().parents[1]
GRANT_NAMES = ("approve_session", "approve_permanent", "save_permanent_allowlist")


@pytest.fixture
def grants(monkeypatch):
    """Record WebUI's own grant calls instead of touching any allowlist."""
    calls = []
    for name in GRANT_NAMES:
        monkeypatch.setattr(routes, name, lambda *args, _name=name: calls.append((_name, args)))
    # WebUI's own grant calls, not the Agent's _persist_choice (tested separately).
    monkeypatch.setattr(routes, "persist_grant_choice", None)
    return calls


@pytest.fixture
def sid(monkeypatch):
    session_id = f"approval-scope-{uuid.uuid4().hex[:8]}"
    # A default-profile WebUI session, so WebUI-held grants have a profile home.
    monkeypatch.setitem(
        models.SESSIONS, session_id, SimpleNamespace(session_id=session_id, profile="default")
    )
    yield session_id
    with routes._lock:
        routes._pending.pop(session_id, None)
        routes._gateway_queues.pop(session_id, None)


class _Producer:
    """The shape of a queued Agent approval (``_ApprovalEntry``)."""

    def __init__(self, data):
        self.data = data
        self.event = threading.Event()
        self.result = None
        self.reason = None


def _card(key="recursive delete", **flags):
    return {
        "command": "rm -rf /tmp/approval-scope",
        "pattern_key": key,
        "pattern_keys": [key],
        "description": key,
        **flags,
    }


# ── WebUI-only entries: saved by WebUI within the offered scope ─────────────

FLAG_CASES = {
    "flags_missing": {},
    "all_allowed": {"allow_session": True, "allow_permanent": True},
    "no_permanent": {"allow_session": True, "allow_permanent": False},
    "one_operation": {"allow_session": False, "allow_permanent": False},
    "smart_denied_older_agent": {"smart_denied": True, "allow_permanent": False},
}
EXPECTED_SCOPE = {
    ("flags_missing", "session"): "session",
    ("flags_missing", "always"): "always",
    ("all_allowed", "session"): "session",
    ("all_allowed", "always"): "always",
    ("no_permanent", "session"): "session",
    ("no_permanent", "always"): "session",
    ("one_operation", "session"): "once",
    ("one_operation", "always"): "once",
    ("smart_denied_older_agent", "session"): "once",
    ("smart_denied_older_agent", "always"): "once",
}


@pytest.mark.parametrize("case", FLAG_CASES)
@pytest.mark.parametrize("choice", ["once", "session", "always", "deny"])
def test_scope_offered_by_entry(case, choice):
    expected = EXPECTED_SCOPE.get((case, choice), choice)
    assert routes._scope_offered_by_entry(_card(**FLAG_CASES[case]), choice) == expected
    assert routes._scope_offered_by_entry(None, choice) == choice


def _expected_grants(sid, scope, key="recursive delete"):
    if scope == "session":
        return [("approve_session", (sid, key))]
    if scope == "always":
        return [
            ("approve_session", (sid, key)),
            ("approve_permanent", (key,)),
            ("save_permanent_allowlist", ({key},)),
        ]
    return []


@pytest.mark.parametrize("kind", ["raw_entry", "orphan_mirror"])
@pytest.mark.parametrize("case", FLAG_CASES)
@pytest.mark.parametrize("choice", ["once", "session", "always"])
def test_webui_only_entry_is_saved_within_its_offered_scope(grants, sid, kind, case, choice):
    """No Agent waits on a raw entry (the loopback test-seeding route, an older Agent's
    no-notifier ``_pending`` write) or on a mirror whose producer is gone. Only the raw
    entry is saved: the orphan mirror's command was never run on this answer."""
    entry = {**_card(**FLAG_CASES[case]), "approval_id": f"aid-{sid}"}
    if kind == "orphan_mirror":
        entry[route_approvals._GATEWAY_MIRROR_FLAG] = True
    with routes._lock:
        routes._pending[sid] = [entry]

    assert routes._resolve_approval_legacy(sid, entry["approval_id"], choice) is True

    expected = EXPECTED_SCOPE.get((case, choice), choice) if kind == "raw_entry" else "once"
    assert grants == _expected_grants(sid, expected)
    with routes._lock:
        assert not routes._pending.get(sid)


def test_always_saves_only_the_keys_it_granted(grants, sid, monkeypatch):
    """Grants already in memory (another session or profile) are not passed to the save."""
    monkeypatch.setattr(routes, "_permanent_approved", {"earlier key from elsewhere"})
    entry = {**_card("first key"), "pattern_keys": ["first key", "second key"], "approval_id": f"aid-{sid}"}
    with routes._lock:
        routes._pending[sid] = [entry]

    assert routes._resolve_approval_legacy(sid, entry["approval_id"], "always") is True

    saves = [args for name, args in grants if name == "save_permanent_allowlist"]
    assert saves == [({"first key", "second key"},)]


def test_unknown_session_profile_saves_nothing(grants, monkeypatch):
    """With an Agent bound, a session whose profile can't be resolved is treated as once."""
    monkeypatch.setitem(route_approvals.APPROVAL_BRIDGE, "agent", True)
    unknown = f"approval-scope-missing-{uuid.uuid4().hex[:8]}"
    with routes._lock:
        routes._pending[unknown] = [{**_card(), "approval_id": f"aid-{unknown}"}]
    try:
        assert routes._resolve_approval_legacy(unknown, f"aid-{unknown}", "always") is True
    finally:
        with routes._lock:
            routes._pending.pop(unknown, None)
    assert grants == []


def test_agent_less_mode_still_saves_unscoped(grants, monkeypatch):
    """No Agent: there is no profile-scoped grant store, so behaviour is unchanged."""
    monkeypatch.setitem(route_approvals.APPROVAL_BRIDGE, "agent", False)
    unknown = f"approval-scope-agentless-{uuid.uuid4().hex[:8]}"
    with routes._lock:
        routes._pending[unknown] = [{**_card(), "approval_id": f"aid-{unknown}"}]
    try:
        assert routes._resolve_approval_legacy(unknown, f"aid-{unknown}", "always") is True
    finally:
        with routes._lock:
            routes._pending.pop(unknown, None)
    assert grants == _expected_grants(unknown, "always")


def test_raw_always_is_granted_through_a_keys_persist_choice(grants, sid, monkeypatch):
    persisted = []
    monkeypatch.setattr(routes, "persist_grant_choice", lambda *args: persisted.append(args))
    entry = {**_card(), "pattern_keys": ["first key", "second key"], "approval_id": f"aid-{sid}"}
    with routes._lock:
        routes._pending[sid] = [entry]

    assert routes._resolve_approval_legacy(sid, entry["approval_id"], "always") is True

    assert persisted == [(sid, "always", ["first key", "second key"])]
    assert grants == []  # not WebUI's own grant calls as well


def _tuple_shape(session_key, choice, warnings):  # Agent v2026.9.24
    raise AssertionError("never called")


def _keys_shape(session_key, choice, keys):
    raise AssertionError("never called")


class _NoSignature:
    __signature__ = "not a signature"  # inspect.signature() raises TypeError

    def __call__(self, *args):
        raise AssertionError("never called")


@pytest.mark.parametrize(
    "module, uses_it",
    [
        (SimpleNamespace(_persist_choice=_keys_shape), True),
        (SimpleNamespace(_persist_choice=_tuple_shape), False),
        (SimpleNamespace(_persist_choice=lambda session_key, choice: None), False),
        (SimpleNamespace(_persist_choice=_NoSignature()), False),
        (SimpleNamespace(_persist_choice="not callable"), False),
        (SimpleNamespace(), False),
        (None, False),  # Agent-less
    ],
    ids=["keys", "tuples", "two_params", "no_signature", "not_callable", "missing", "agentless"],
)
def test_persist_choice_is_used_only_with_a_keys_parameter(module, uses_it):
    found = route_approvals._keys_persist_choice(module)
    assert found is (module._persist_choice if uses_it else None)


def test_allowlist_save_is_wrapped_once_and_held_for_the_whole_call():
    seen = []

    def save(patterns):
        seen.append((set(patterns), lock.locked()))
        return "saved"

    module = SimpleNamespace(save_permanent_allowlist=save)
    lock = route_approvals._serialize_allowlist_saves(module)
    wrapper = module.save_permanent_allowlist
    assert lock is not None and wrapper is not save
    assert wrapper._webui_serialized is True and wrapper._webui_original is save

    # A reload or second import reuses the wrapper and its lock: no wrapper of a wrapper.
    assert route_approvals._serialize_allowlist_saves(module) is lock
    assert module.save_permanent_allowlist is wrapper

    assert wrapper({"k"}) == "saved"
    assert seen == [({"k"}, True)] and not lock.locked()


@pytest.mark.parametrize(
    "module", [SimpleNamespace(), SimpleNamespace(save_permanent_allowlist=None), None],
    ids=["missing", "not_callable", "agentless"],
)
def test_allowlist_save_wrap_skips_a_missing_name(module):
    assert route_approvals._serialize_allowlist_saves(module) is None
    assert getattr(module, "save_permanent_allowlist", None) is None


# ── Approvals an Agent is waiting on: WebUI saves nothing ───────────────────


@pytest.mark.parametrize("choice", ["session", "always"])
def test_local_gateway_answer_leaves_the_grant_to_the_agent(grants, sid, choice):
    producer = _Producer({**_card(allow_session=True, allow_permanent=True), "request_id": uuid.uuid4().hex})
    with routes._lock:
        routes._gateway_queues[sid] = [producer]
    head, _total = route_approvals.submit_gateway_pending_mirror(sid, dict(producer.data))
    assert head and head.get(route_approvals._GATEWAY_MIRROR_FLAG)

    assert routes._resolve_approval_legacy(sid, head["approval_id"], choice) is True

    assert grants == []
    assert producer.event.is_set() and producer.result == choice  # the Agent gets the answer as given


@pytest.mark.parametrize("choice", ["session", "always"])
def test_id_less_answer_to_the_queue_head_leaves_the_grant_to_the_agent(grants, sid, choice):
    producer = _Producer(_card(allow_session=True, allow_permanent=True))
    with routes._lock:
        routes._gateway_queues[sid] = [producer]

    assert routes._resolve_approval_legacy(sid, "", choice) is True

    assert grants == []
    if route_approvals.APPROVAL_BRIDGE["agent"]:  # Agent-less mode has nobody to hand it to
        assert producer.event.is_set() and producer.result == choice


@pytest.mark.parametrize("choice", ["session", "always"])
def test_relayed_run_answer_leaves_the_grant_to_the_run(grants, sid, choice):
    run_id, approval_id = "run-1", "prompt-1"
    with routes._lock:
        routes._pending[sid] = [{
            **_card(allow_session=True, allow_permanent=True),
            "approval_id": approval_id,
            "run_id": run_id,
            route_approvals._GATEWAY_MIRROR_FLAG: True,
            route_approvals._GATEWAY_MIRROR_TOKEN: uuid.uuid4().hex,
            route_approvals._GATEWAY_MIRROR_RETAINED: True,
        }]

    assert routes._resolve_approval_legacy(sid, approval_id, choice, run_id=run_id) is True

    assert grants == []


def test_id_less_answer_saves_a_raw_entry_but_not_the_queue_head(grants, sid):
    """A legacy id-less click answers the oldest raw entry and the Agent's head."""
    producer = _Producer(_card("other key", allow_session=True, allow_permanent=True))
    with routes._lock:
        routes._pending[sid] = [{**_card(), "approval_id": f"aid-{sid}"}]
        routes._gateway_queues[sid] = [producer]

    routes._resolve_approval_legacy(sid, "", "session")

    assert grants == [("approve_session", (sid, "recursive delete"))]


# ── End to end through the real Agent gate (subprocess) ─────────────────────

_SCOPE_DRIVER = r'''
import json, os, sys, threading, time

repo, agent_dir, mode = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, repo)
sys.path.append(agent_dir)
import tools.approval as A
from tools import approval_detection as D
from tools.approval_context import set_current_session_key
import api.route_approvals as R
import api.routes as ROUTES

OSASCRIPT = "osascript -e 'tell application \"Keynote\" to quit'"
RM = "rm -rf /tmp/approval-scope-target"
CHMOD = "chmod 777 /tmp/approval-scope-target"
CHMOD2 = "chmod -R 777 /tmp/approval-scope-target"
SID, SID2 = "approval-scope-" + mode, "approval-scope-other-" + mode
os.environ["HERMES_EXEC_ASK"] = "1"
os.environ["HERMES_SESSION_PLATFORM"] = "webui"
os.environ["HERMES_SESSION_KEY"] = SID

out = {"manual_only_supported": bool(getattr(D, "SMART_MANUAL_ONLY_DESCRIPTIONS", None))
       and D.detect_dangerous_command(OSASCRIPT)[1] in D.SMART_MANUAL_ONLY_DESCRIPTIONS}
if mode == "manual_only_session" and not out["manual_only_supported"]:
    print("RESULT " + json.dumps(out))
    sys.exit(0)
if mode == "smart_deny_always":
    # Stub the guardian LLM the way the Agent's own tests do: it refuses, and
    # the owner overrides the refusal on the card.
    A._smart_verdict = lambda *a, **k: "deny"

grants = []

def recording(who, name, fn):
    def wrapper(*args, **kwargs):
        grants.append([who, name] + [a if isinstance(a, str) else sorted(a) for a in args])
        return fn(*args, **kwargs)
    return wrapper

for name in ("approve_session", "approve_permanent", "save_permanent_allowlist"):
    original = getattr(A, name)
    setattr(A, name, recording("agent", name, original))      # the Agent's own saves
    setattr(ROUTES, name, recording("webui", name, original))  # WebUI's saves

cards = {SID: [], SID2: []}

def make_notify(sid):
    def notify(approval_data):  # the api/streaming.py _approval_notify_cb shape
        R.require_approval_bridge()
        auto_resolved, head, total = R.settle_gateway_pending_local_notification(sid, approval_data)
        if auto_resolved and head is None:
            return
        cards[sid].append({**(head or approval_data), "pending_count": total})
    return notify

for sid in cards:
    A.register_gateway_notify(sid, make_notify(sid))
responses = []
ROUTES.j = lambda _h, data, status=200, extra_headers=None: responses.append([status, data]) or data

def step(sid, command, answer, delay=0.0):
    seen = len(cards[sid])
    gate = {}

    def turn():
        set_current_session_key(sid)
        gate["result"] = A.check_all_command_guards(command, "local")

    thread = threading.Thread(target=turn, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and thread.is_alive() and not (
        len(cards[sid]) > seen and A.has_blocking_approval(sid)
    ):
        time.sleep(0.02)
    prompted = len(cards[sid]) > seen and thread.is_alive()
    card = cards[sid][-1] if prompted else {}
    responded = None
    if prompted:
        time.sleep(delay)
        responder = threading.Thread(target=ROUTES._handle_approval_respond, daemon=True, args=(
            object(), {"session_id": sid, "choice": answer, "approval_id": card.get("approval_id")}
        ))
        responder.start()
        responder.join(timeout=15)
        responded = not responder.is_alive()
    thread.join(timeout=15)
    result = gate.get("result") or {}
    return {
        "sid": sid, "answer": answer if prompted else None, "prompted": prompted,
        "responded": responded, "gate_finished": not thread.is_alive(),
        "approved": result.get("approved"), "outcome": result.get("outcome"),
        "card": {k: card.get(k) for k in ("allow_session", "allow_permanent", "smart_denied", "pattern_keys")},
    }

PLANS = {
    "manual_only_session": [(SID, OSASCRIPT, "session"), (SID, OSASCRIPT, "once")],
    "smart_deny_always": [(SID, RM, "always"), (SID, RM, "once"), (SID2, RM, "once")],
    "ordinary_session": [(SID, RM, "session"), (SID, RM, "once")],
    "ordinary_always": [(SID, RM, "always"), (SID2, RM, "once")],
    # approvals.timeout is 1s here: answers land just before and just after it.
    "timeout_race": [(SID, RM, "always", 0.95), (SID2, CHMOD, "always", 1.0), (SID, CHMOD2, "always", 1.05)],
}
out["steps"] = [step(*s) for s in PLANS[mode]]
keys = out["steps"][0]["card"]["pattern_keys"] or []
out["keys"] = keys
out["grants"] = grants
out["responses"] = [[s, {k: d.get(k) for k in ("ok", "code", "error")}] for s, d in responses]
out["approved_after"] = {sid: [A.is_approved(sid, k) for k in keys] for sid in (SID, SID2)}
from hermes_cli.config import load_config_readonly
out["allowlist_on_disk"] = sorted(load_config_readonly().get("command_allowlist") or [])
for sid in cards:
    A.unregister_gateway_notify(sid)
print("RESULT " + json.dumps(out, default=str))
'''


def _run_scope_driver(tmp_path, mode):
    agent_approval = pytest.importorskip("tools.approval")
    pytest.importorskip("tools.approval_context")
    agent_dir = Path(agent_approval.__file__).resolve().parents[1]
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    approval_mode = "smart" if mode == "smart_deny_always" else "manual"
    timeout = 1 if mode == "timeout_race" else 6
    (home / ".hermes" / "config.yaml").write_text(
        f"approvals:\n  mode: {approval_mode}\n  timeout: {timeout}\n", encoding="utf-8"
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "HERMES_HOME": str(home / ".hermes"),
        "HERMES_WEBUI_STATE_DIR": str(tmp_path / "state"),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(tmp_path),
    }
    proc = subprocess.run(
        [sys.executable, "-B", "-c", _SCOPE_DRIVER, str(REPO_ROOT), str(agent_dir), mode],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=240,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, f"driver failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
    return json.loads(lines[-1][len("RESULT "):])


def _webui_grants(out):
    return [g for g in out["grants"] if g[0] == "webui"]


def test_real_agent_manual_only_session_answer_still_asks_next_time(tmp_path):
    out = _run_scope_driver(tmp_path, "manual_only_session")
    if not out["manual_only_supported"]:
        pytest.skip("this Agent has no manual-only (osascript) approval class")
    detail = json.dumps(out)
    first, second = out["steps"]
    assert first["prompted"] and first["approved"] is True, detail
    assert first["card"]["allow_session"] is False, detail  # the Agent offered "once" only
    assert second["prompted"], detail  # "session" did not pre-approve the next one
    assert out["grants"] == [], detail
    assert not any(out["approved_after"]["approval-scope-manual_only_session"]), detail


def test_real_agent_smart_deny_override_always_saves_nothing(tmp_path):
    out = _run_scope_driver(tmp_path, "smart_deny_always")
    detail = json.dumps(out)
    first, second, other_session = out["steps"]
    assert first["prompted"] and first["approved"] is True, detail
    assert first["card"]["smart_denied"] is True and first["card"]["allow_permanent"] is False, detail
    assert second["prompted"] and other_session["prompted"], detail
    assert out["grants"] == [], detail
    assert not set(out["keys"]) & set(out["allowlist_on_disk"]), detail


def test_real_agent_ordinary_session_answer_is_saved_once_by_the_agent(tmp_path):
    out = _run_scope_driver(tmp_path, "ordinary_session")
    detail = json.dumps(out)
    first, second = out["steps"]
    assert first["prompted"] and first["approved"] is True, detail
    assert not second["prompted"] and second["approved"] is True, detail
    sid = "approval-scope-ordinary_session"
    assert out["keys"] and out["grants"] == [["agent", "approve_session", sid, k] for k in out["keys"]], detail
    assert all(out["approved_after"][sid]), detail


def test_real_agent_ordinary_always_answer_is_saved_once_by_the_agent(tmp_path):
    out = _run_scope_driver(tmp_path, "ordinary_always")
    detail = json.dumps(out)
    first, other_session = out["steps"]
    assert first["prompted"] and first["approved"] is True, detail
    assert not other_session["prompted"] and other_session["approved"] is True, detail
    assert _webui_grants(out) == [], detail
    agent_calls = [g[1] for g in out["grants"]]
    assert agent_calls.count("approve_session") == len(out["keys"]) > 0, detail
    assert agent_calls.count("approve_permanent") == len(out["keys"]), detail
    assert set(out["keys"]) <= set(out["allowlist_on_disk"]), detail


def test_real_agent_always_answers_racing_the_approval_timeout_do_not_hang(tmp_path):
    """Answers land around the Agent's 1s approval timeout, so a grant's save can run while
    the waiter expires. Every gate and every respond call finishes (bounded joins)."""
    out = _run_scope_driver(tmp_path, "timeout_race")
    detail = json.dumps(out)
    assert out["steps"] and all(s["gate_finished"] for s in out["steps"]), detail
    assert all(s["responded"] in (True, None) for s in out["steps"]), detail
    for s in out["steps"]:
        if s["approved"] and s["answer"] == "always":
            assert set(s["card"]["pattern_keys"] or []) <= set(out["allowlist_on_disk"]), detail
