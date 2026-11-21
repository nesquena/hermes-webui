"""#8746: a forced Agent update re-checks the Gateway restart impact the user confirmed.

The refusal says what forcing does to the Gateway: restart it (managed) or leave
its runs running. The user confirms that copy, so the forced request carries the
confirmed ``gateway_restart_managed``, profile and Gateway endpoint. If any of
them changed before the forced request arrives (a loopback Gateway whose process
ID became visible, a profile switch in another tab, another endpoint), the server
refuses again with a fresh ``restart_blocked`` before any side effect, and the
Gateway restart dispatch checks once more after the checkout update. A forced
request without those fields behaves as before.

The real run registry, blocking census, refusal producer, fence and restart
dispatch run. The seams are Gateway durability, lifecycle capability, the active
profile, the updater body (``_apply_update_inner``) and the Gateway CLI
(``restart_active_profile_gateway``, with ``Popen`` refusing as a backstop): no
update and no Gateway command ever runs.
"""

import logging

import pytest

import api.config as cfg
import api.gateway_chat as gateway_chat
import api.gateway_restart as gateway_restart
import api.run_drain as run_drain
import api.updates as upd
from tests import test_issue7293_force_update_offer_ui as harness

FIELDS = ("gateway_restart_managed", "gateway_profile", "gateway_endpoint")
_URL_VARS = ("GATEWAY_HEALTH_URL", "HERMES_GATEWAY_HEALTH_URL", "HERMES_API_URL", "HERMES_WEBUI_GATEWAY_BASE_URL")


@pytest.fixture
def world(monkeypatch):
    """One durable Gateway run, a switchable capability/profile, a recording Gateway CLI."""
    state = {"managed": False, "reason": "local_gateway_unproven", "profile": "alpha"}
    calls = {"restart": [], "update": 0}
    for var in _URL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8642/")
    monkeypatch.setattr(gateway_chat, "gateway_run_durably_resumable", lambda stream_id: True)
    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability",
        lambda profile=None: {"managed": state["managed"], "reason": state["reason"]},
    )
    monkeypatch.setattr(upd, "get_active_profile_name", lambda: state["profile"])
    monkeypatch.setattr(upd, "get_active_profile_gateway_running_pid", lambda profile=None: None)

    def _restart(profile=None, expected_impact=None):
        calls["restart"].append(profile)
        return {"status": "completed"}

    def _no_popen(*args, **kwargs):
        raise AssertionError(f"Gateway CLI must not run: {args!r}")

    monkeypatch.setattr(upd, "restart_active_profile_gateway", _restart)
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", _no_popen)

    def _inner(target, channel):
        calls["update"] += 1
        hook = state.get("during_update")
        if hook:
            hook()
        ok, result = upd._ensure_gateway_restart_for_agent_update()
        return {"ok": ok, "target": target, "gateway_restart": result.get("status"), "result": result}

    monkeypatch.setattr(upd, "_apply_update_inner", _inner)
    with cfg.ACTIVE_RUNS_LOCK:
        old_runs = dict(cfg.ACTIVE_RUNS)
        cfg.ACTIVE_RUNS.clear()
    with cfg.STREAMS_LOCK:
        old_streams = dict(cfg.STREAMS)
        cfg.STREAMS.clear()
    run_drain._reset_for_tests()
    cfg.register_active_run("gateway-0", backend="gateway")
    try:
        yield state, calls
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.clear()
            cfg.ACTIVE_RUNS.update(old_runs)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.clear()
            cfg.STREAMS.update(old_streams)
        run_drain._reset_for_tests()


def _refuse():
    refusal = upd.apply_update("agent", "stable")
    assert refusal["restart_blocked"] is True and refusal["force_available"] is True
    return refusal


def _forced_body(refusal):
    """The body the client re-sends after the confirmation (see ui.js)."""
    body = {"target": "agent", "force_active_runs": True}
    body.update({field: refusal[field] for field in FIELDS})
    return body


def _force(body):
    return upd.apply_update(
        "agent", "stable", force_active_runs=True,
        confirmed_gateway=upd.confirmed_gateway_impact(body),
    )


def _assert_fresh_refusal(out, calls):
    assert out["ok"] is False and out["restart_blocked"] is True and out["force_available"] is True
    assert calls == {"restart": [], "update": 0}, "refused before any update or Gateway restart"
    assert run_drain._HOLDS == {}, "the admission hold is released"


def test_unmanaged_refusal_that_flips_to_managed_gets_a_fresh_refusal_and_no_gateway_cli(world):
    state, calls = world
    refusal = _refuse()
    assert refusal["gateway_restart_managed"] is False
    assert "keep running" in refusal["message"]

    state.update(managed=True, reason="local_gateway")  # the Gateway PID became visible
    out = _force(_forced_body(refusal))

    _assert_fresh_refusal(out, calls)
    assert out["gateway_restart_managed"] is True
    assert out["gateway_profile"] == "alpha"
    assert "restarts the Gateway this profile uses" in out["message"]


def test_unchanged_ownership_proceeds_with_exactly_one_dispatch(world):
    state, calls = world
    state.update(managed=True, reason="local_gateway")
    refusal = _refuse()
    assert refusal["gateway_endpoint"] == "http://127.0.0.1:8642"

    out = _force(_forced_body(refusal))

    assert out["ok"] is True and out["gateway_restart"] == "completed"
    assert calls == {"restart": ["alpha"], "update": 1}


def test_unchanged_unmanaged_confirmation_proceeds_without_a_gateway_restart(world):
    """A confirmed-unmanaged force never reaches the Gateway restart helper."""
    _state, calls = world
    out = _force(_forced_body(_refuse()))

    assert out["ok"] is True and out["gateway_restart"] == "external"
    assert calls == {"restart": [], "update": 1}


@pytest.mark.parametrize("change", ["profile", "endpoint"])
def test_profile_or_endpoint_change_is_caught(world, monkeypatch, change):
    state, calls = world
    state.update(managed=True, reason="local_gateway")
    refusal = _refuse()
    assert (refusal["gateway_profile"], refusal["gateway_endpoint"]) == ("alpha", "http://127.0.0.1:8642")

    if change == "profile":
        state["profile"] = "beta"  # a profile switch in another tab
    else:
        monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8643")
    out = _force(_forced_body(refusal))

    _assert_fresh_refusal(out, calls)
    assert (out["gateway_profile"], out["gateway_endpoint"]) == (
        ("beta", "http://127.0.0.1:8642") if change == "profile" else ("alpha", "http://127.0.0.1:8643")
    )


def test_a_request_without_the_fields_keeps_todays_behaviour(world):
    """Control: the pre-#8746 forced request (no confirmation) proceeds as before."""
    state, calls = world
    _refuse()
    state.update(managed=True, reason="local_gateway")

    out = upd.apply_update("agent", "stable", force_active_runs=True)

    assert out["ok"] is True and out["gateway_restart"] == "completed"
    assert calls == {"restart": ["alpha"], "update": 1}


@pytest.mark.parametrize("body", [
    {"target": "agent", "force_active_runs": True},
    {"target": "agent", "force_active_runs": True, "gateway_profile": "alpha", "gateway_endpoint": ""},
    None,
])
def test_a_body_without_a_boolean_confirmation_is_not_a_confirmation(body):
    assert upd.confirmed_gateway_impact(body) is None


def test_a_non_boolean_confirmation_is_a_bad_request():
    with pytest.raises(ValueError):
        upd.confirmed_gateway_impact({"target": "agent", "force_active_runs": True, "gateway_restart_managed": "false"})


def test_restart_dispatch_rechecks_a_change_during_the_update(world):
    """Ownership that flips while the checkout updates does not restart the Gateway."""
    state, calls = world
    refusal = _refuse()
    state["during_update"] = lambda: state.update(managed=True, reason="local_gateway")

    out = _force(_forced_body(refusal))

    assert calls == {"restart": [], "update": 1}
    assert out["ok"] is False and out["gateway_restart"] == "restart_blocked"
    assert out["result"]["gateway_restart_managed"] is True


def _raise(profile=None):
    raise RuntimeError("capability read failed")


@pytest.mark.parametrize("capability,fate", [
    (lambda profile=None: {"managed": True, "reason": "local_gateway"},
     "the Gateway this profile uses is restarted, which stops every run it is executing"),
    (lambda profile=None: {"managed": False, "reason": "external_gateway"},
     "the Gateway this profile uses is managed outside the WebUI and is not restarted"),
    (lambda profile=None: {"managed": False, "reason": "local_gateway_unproven"},
     "the WebUI could not prove it owns the Gateway this profile uses"),
    (_raise, "the WebUI could not prove it owns the Gateway this profile uses"),
], ids=["managed", "external", "unproven", "raised"])
def test_forced_agent_log_names_the_real_gateway_case(world, monkeypatch, caplog, capability, fate):
    _state, calls = world
    monkeypatch.setattr(upd, "gateway_lifecycle_capability", capability)
    with caplog.at_level(logging.WARNING, logger=upd.logger.name):
        upd.apply_update("agent", "stable", force_active_runs=True)
    text = " ".join(record.getMessage() for record in caplog.records if "Forced agent" in record.getMessage())
    assert text.startswith("Forced agent update with 1 active run(s) (0 WebUI, 1 Gateway); " + fate), text
    assert "(WebUI and Gateway)" not in text and "saved as interrupted" not in text
    assert calls["update"] == 1


def test_gateway_only_refusal_names_only_the_gateway_runs(monkeypatch):
    monkeypatch.setattr(upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": True})
    res = upd._restart_blocked_response(
        "agent", {"active_runs": 2, "active_streams": 0, "webui_owned_runs": 0, "gateway_runs": 2}
    )
    assert res["message"] == (
        "Cannot update agent while 2 Gateway runs are active. Updating the Agent also restarts "
        "the Gateway this profile uses and stops every run that Gateway is executing, including "
        "runs started from other profiles that use the same Gateway endpoint. Wait for the "
        "response to finish and retry, or force the update to restart the Gateway this profile "
        "uses and stop the Gateway runs it is executing."
    )


@pytest.mark.skipif(harness.NODE is None, reason="node required for the ui.js harness")
def test_forced_resend_carries_the_confirmed_gateway_impact(tmp_path):
    refusal = harness._refusal(
        1, target="agent", gateway_restart_managed=False, webui_owned_runs=0, gateway_runs=1,
        gateway_profile="alpha", gateway_endpoint="http://127.0.0.1:8642",
    )
    out = harness._run({
        "entry": "apply", "target": "agent", "updateData": harness._AGENT_BEHIND,
        "responses": [refusal, {"ok": True, "target": "agent"}],
        "clickOffer": True, "confirmAnswers": [True],
    }, tmp_path)
    assert out["apiCalls"] == [
        {"url": "/api/updates/apply", "body": {"target": "agent"}},
        {"url": "/api/updates/apply", "body": {
            "target": "agent", "force_active_runs": True, "gateway_restart_managed": False,
            "gateway_profile": "alpha", "gateway_endpoint": "http://127.0.0.1:8642",
            "interrupted_runs": 0,
        }},
    ]
