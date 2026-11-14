"""14-09-2026 (Michael): approval: {mode: automatic, prompt} decides a person's
ACCESS REQUESTS in the queue; it never reviews tool calls. Uncertain verdicts
and out-of-bounds approvals leave the row to an administrator."""
import threading
import time

import pytest
import yaml

from api import approvals, config, grant_auto_review
from api.governance import loader

ME = "vansh@example.test"


def _raw(approval=True, deny_bunq=True):
    user = {"roles": ["member"], "access_mode": "blacklist", "access_level": "elevated",
            "grants": {"cli": {"commands": ["*"]}}}
    if approval:
        user["approval"] = {"mode": "automatic", "prompt": "Approve ordinary technical work; deny bank and Productive."}
    if deny_bunq:
        user["deny"] = {"cli": {"commands": ["bunq"]}}
    return {"mode": "enforce", "bootstrap_admins": ["michael@example.test"],
            "roles": {"member": {"grants": {"permissions": ["chat:use", "terminal:use"], "routes": ["*"], "profiles": ["vansh"],
                                            "tools": {"builtins": ["*"]}, "cli": {"commands": ["*"]}}}},
            "users": {ME: user}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_WEBUI_GOVERNANCE_POLICY", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_DISABLE_PROFILE_SYNC", "1")
    (tmp_path / "webui").mkdir()
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "webui")
    monkeypatch.setattr("api.grant_requests.notify_requester_of_decision", lambda *a, **k: True)
    loader._clear_policy_cache()

    def write(raw):
        (tmp_path / "dashboard-governance.yaml").write_text(yaml.safe_dump(raw))
        loader._clear_policy_cache()

    def request(value="jq", gkind="cli"):
        key = f"{ME}|{gkind}|{value}"
        approvals.request(approvals.KIND_GRANT, key, ME, label=f"CLI: {value}",
                          payload={"email": ME, "gkind": gkind, "value": value, "reason": "cli_command_not_allowed",
                                   "tool": "terminal", "detail": value, "trigger": "parse the json export", "count": 1})
        return approvals.get(approvals.KIND_GRANT, key)

    write(_raw())
    return type("Env", (), {"tmp": tmp_path, "write": write, "request": request})


def _verdict(monkeypatch, decision, confidence=1.0):
    calls = []
    monkeypatch.setattr(grant_auto_review, "_ask_model",
                        lambda access, request: calls.append((access.subject.email, request)) or
                        {"decision": decision, "reason": f"model says {decision}", "confidence": confidence})
    return calls


def _policy_cli(tmp):
    raw = yaml.safe_load((tmp / "dashboard-governance.yaml").read_text())
    return raw["users"][ME].get("grants", {}).get("cli", {}).get("commands", [])


def test_an_approved_request_is_granted_without_a_person(env, monkeypatch):
    calls = _verdict(monkeypatch, "approve")
    entry = env.request("jq")
    review = grant_auto_review.auto_review_entry(entry)
    assert review["decision"] == "approve" and review["decided"] is True
    assert calls and calls[0][1]["asked_for"] == "parse the json export"
    assert approvals.get(approvals.KIND_GRANT, entry["key"])["status"] == "approved"
    assert "jq" in _policy_cli(env.tmp)


def test_a_denied_request_is_rejected_with_the_reason(env, monkeypatch):
    _verdict(monkeypatch, "deny")
    entry = env.request("trading212")
    review = grant_auto_review.auto_review_entry(entry)
    assert review["decision"] == "deny" and review["decided"] is True
    row = approvals.get(approvals.KIND_GRANT, entry["key"])
    assert row["status"] == "rejected" and "trading212" not in _policy_cli(env.tmp)


def test_an_uncertain_verdict_leaves_the_row_to_an_administrator(env, monkeypatch):
    _verdict(monkeypatch, "approve", confidence=0.4)
    entry = env.request("jq")
    review = grant_auto_review.auto_review_entry(entry)
    assert review["decision"] == "manual" and review["decided"] is False
    row = approvals.get(approvals.KIND_GRANT, entry["key"])
    assert row["status"] == "pending" and row["payload"]["auto_review"]["decision"] == "manual"


def test_without_an_approval_section_nothing_happens(env, monkeypatch):
    env.write(_raw(approval=False))
    calls = _verdict(monkeypatch, "approve")
    entry = env.request("jq")
    assert grant_auto_review.auto_review_entry(entry) is None
    assert not calls and approvals.get(approvals.KIND_GRANT, entry["key"])["status"] == "pending"


def test_an_explicit_deny_is_never_overridden_by_the_reviewer(env, monkeypatch):
    _verdict(monkeypatch, "approve")
    entry = env.request("bunq")
    review = grant_auto_review.auto_review_entry(entry)
    assert review["decision"] == "manual" and review["decided"] is False and "not applied" in review["reason"]
    assert approvals.get(approvals.KIND_GRANT, entry["key"])["status"] == "pending"
    assert "bunq" not in _policy_cli(env.tmp)


def test_schedule_pings_administrators_only_when_the_row_stays(env, monkeypatch):
    pinged = []
    done = threading.Event()
    def on_manual(entry):
        pinged.append(entry["key"]); done.set()
    _verdict(monkeypatch, "approve")
    entry = env.request("jq")
    grant_auto_review.schedule(entry, on_manual=on_manual)
    deadline = time.time() + 5
    while time.time() < deadline and approvals.get(approvals.KIND_GRANT, entry["key"])["status"] == "pending":
        time.sleep(0.05)
    assert approvals.get(approvals.KIND_GRANT, entry["key"])["status"] == "approved" and not pinged
    _verdict(monkeypatch, "manual")
    entry2 = env.request("yq")
    grant_auto_review.schedule(entry2, on_manual=on_manual)
    assert done.wait(5) and pinged == [entry2["key"]]


@pytest.mark.parametrize("decision", ["approve", "deny"])
@pytest.mark.parametrize("change", ["manual", "prompt", "removed", "disabled"])
def test_policy_change_during_model_review_leaves_request_pending(env, monkeypatch, decision, change):
    original = _raw()
    original["users"][ME]["access_mode"] = "whitelist"
    original["users"][ME]["grants"] = {}
    env.write(original)
    entry = env.request("jq")

    def changed_policy(_access, _request):
        if change == "manual":
            original["users"][ME]["approval"] = {"mode": "manual"}
        elif change == "prompt":
            original["users"][ME]["approval"]["prompt"] = "Require a human for all new capabilities."
        elif change == "removed":
            original["users"][ME].pop("approval")
        else:
            original["mode"] = "off"
        env.write(original)
        return {"decision": decision, "reason": "old rules allowed a decision", "confidence": 1.0}

    monkeypatch.setattr(grant_auto_review, "_ask_model", changed_policy)
    review = grant_auto_review.auto_review_entry(entry)
    assert review["decided"] is False
    assert review["decision"] == "manual"
    assert approvals.get(approvals.KIND_GRANT, entry["key"])["status"] == "pending"
    assert "jq" not in _policy_cli(env.tmp)


def test_replaced_request_during_review_cannot_inherit_old_verdict(env, monkeypatch):
    entry = env.request("jq")

    def replace_request(_access, _request):
        approvals.remove(approvals.KIND_GRANT, entry["key"])
        approvals.request(approvals.KIND_GRANT, entry["key"], ME,
                          payload={"email": ME, "gkind": "cli", "value": "yq"})
        return {"decision": "approve", "reason": "jq was reviewed", "confidence": 1.0}

    monkeypatch.setattr(grant_auto_review, "_ask_model", replace_request)
    review = grant_auto_review.auto_review_entry(entry)
    assert review["decided"] is False
    stored = approvals.get(approvals.KIND_GRANT, entry["key"])
    assert stored["status"] == "pending"
    assert "auto_review" not in stored["payload"]
    assert "jq" not in _policy_cli(env.tmp) and "yq" not in _policy_cli(env.tmp)


@pytest.mark.parametrize("first", ["approve", "reject"])
def test_concurrent_opposite_decisions_commit_only_one_outcome(env, monkeypatch, first):
    from concurrent.futures import ThreadPoolExecutor
    from api import governance_api
    from api.governance.models import GovernanceSubject

    raw = _raw()
    raw["users"][ME]["access_mode"] = "whitelist"
    raw["users"][ME]["grants"] = {}
    env.write(raw)
    entry = env.request("jq")
    first_at_write, release_first = threading.Event(), threading.Event()
    second_started, second_done = threading.Event(), threading.Event()
    saved_policy = governance_api.save_governance_policy
    saved_decide = approvals.decide

    def pause_first():
        first_at_write.set()
        assert release_first.wait(5), "test must release first decision"

    def save_policy(data):
        if first == "approve":
            pause_first()
        return saved_policy(data)

    def save_decision(kind, key, decision, *args, **kwargs):
        if first == "reject" and decision == "reject":
            pause_first()
        return saved_decide(kind, key, decision, *args, **kwargs)

    monkeypatch.setattr(governance_api, "save_governance_policy", save_policy)
    monkeypatch.setattr(approvals, "decide", save_decision)
    subject = GovernanceSubject(email="michael@example.test")

    def decide(choice, second=False):
        if second:
            second_started.set()
        try:
            return governance_api.decide_grant_request(entry["key"], choice, subject)
        finally:
            if second:
                second_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leading = pool.submit(decide, first)
        try:
            assert first_at_write.wait(5)
            trailing = pool.submit(decide, "reject" if first == "approve" else "approve", True)
            assert second_started.wait(5)
            second_done.wait(.3)
        finally:
            release_first.set()
        first_result, second_result = leading.result(5), trailing.result(5)
    assert first_result[0] == 200
    assert second_result[0] == 409
    assert approvals.get(approvals.KIND_GRANT, entry["key"])["status"] == (
        "approved" if first == "approve" else "rejected")
    assert ("jq" in _policy_cli(env.tmp)) is (first == "approve")


def test_manual_rejection_during_model_wait_cannot_be_overwritten(env, monkeypatch):
    from api import governance_api
    from api.governance.models import GovernanceSubject
    entry = env.request("jq")

    def reject_while_waiting(_access, _request):
        status, _ = governance_api.decide_grant_request(
            entry["key"], "reject", GovernanceSubject(email="michael@example.test"), "Human decision")
        assert status == 200
        return {"decision": "approve", "reason": "late automatic result", "confidence": 1.0}

    monkeypatch.setattr(grant_auto_review, "_ask_model", reject_while_waiting)
    review = grant_auto_review.auto_review_entry(entry)
    assert review["decided"] is False
    stored = approvals.get(approvals.KIND_GRANT, entry["key"])
    assert stored["status"] == "rejected" and stored["reason"] == "Human decision"
    assert "jq" not in _policy_cli(env.tmp)


def test_failed_registry_write_rolls_back_grant_and_does_not_resume(env, monkeypatch):
    from api import governance_api, approval_resume
    from api.governance.models import GovernanceSubject
    raw = _raw()
    raw["users"][ME]["access_mode"] = "whitelist"
    raw["users"][ME]["grants"] = {}
    env.write(raw)
    entry = env.request("jq")

    def unavailable_registry(_registry):
        raise OSError("Synthetic registry write failure")

    monkeypatch.setattr(approvals, "save", unavailable_registry)
    monkeypatch.setattr(approval_resume, "decide", lambda *a: pytest.fail("failed decision must not resume work"))
    status, _ = governance_api.decide_grant_request(
        entry["key"], "approve", GovernanceSubject(email="michael@example.test"))
    assert status == 500
    assert approvals.get(approvals.KIND_GRANT, entry["key"])["status"] == "pending"
    assert "jq" not in _policy_cli(env.tmp)


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_automatic_decision_requires_captured_revisions(env, decision):
    from api import governance_api
    from api.governance.models import GovernanceSubject
    entry = env.request("jq")
    status, out = governance_api.decide_grant_request(
        entry["key"], decision, GovernanceSubject(email=grant_auto_review.REVIEWER_EMAIL), source="automatic")
    assert status == 409 and out["error"] == "stale_review"
    assert approvals.get(approvals.KIND_GRANT, entry["key"])["status"] == "pending"
    assert "jq" not in _policy_cli(env.tmp)


@pytest.mark.parametrize("change", ["unchanged", "trigger", "count", "new_key"])
def test_ingest_keeps_manual_review_only_for_the_same_request(env, monkeypatch, change):
    from copy import deepcopy
    from api import grant_requests
    key = f"{ME}|cli|jq"
    item = {"email": ME, "gkind": "cli", "value": "jq", "reason": "cli_command_not_allowed",
            "tool": "terminal", "detail": "jq", "trigger": "parse the json export", "count": 1,
            "first_seen": 1234.5}
    spool = {key: item}
    monkeypatch.setattr(grant_requests, "_load_spool", lambda: spool)
    # Drive review explicitly so both ingest passes have deterministic timing.
    monkeypatch.setattr(grant_auto_review, "schedule", lambda *a, **kw: None)
    _verdict(monkeypatch, "manual")
    assert grant_requests.ingest_spool() == 1
    entry = approvals.get(approvals.KIND_GRANT, key)
    revision = approvals.request_revision(entry)
    review = grant_auto_review.auto_review_entry(entry)
    assert review["decision"] == "manual" and review["decided"] is False

    if change == "trigger":
        item["trigger"] = "inspect a different private export"
    elif change == "count":
        item["count"] = 2
    elif change == "new_key":
        next_key = f"{ME}|cli|yq"
        spool[next_key] = {**deepcopy(item), "value": "yq", "detail": "yq"}

    assert grant_requests.ingest_spool() == (2 if change == "new_key" else 1)
    stored = approvals.get(approvals.KIND_GRANT, key)
    if change in {"unchanged", "new_key"}:
        assert approvals.request_revision(stored) == revision
        assert stored["payload"].get("auto_review") == review
    else:
        assert approvals.request_revision(stored) != revision
        assert "auto_review" not in stored["payload"]
    if change == "new_key":
        added = approvals.get(approvals.KIND_GRANT, next_key)
        assert approvals.request_revision(added) != revision
        assert "auto_review" not in added["payload"]
