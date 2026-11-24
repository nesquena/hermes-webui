"""#8746: a confirmed forced Agent update does exactly what the user confirmed.

The offer the user confirms names the Gateway restart impact (managed or not,
profile, endpoint) and how many runs the update interrupts. The forced request
echoes all of that back, and:

* a confirmed-unmanaged force never restarts the Gateway, at the dispatch or on
  a retry, whatever the ownership reads afterwards;
* a confirmed-managed force restarts only the exact confirmed Gateway: the
  restart helper compares its own capability read, the profile and the endpoint
  with the confirmation immediately before every launch, retries included;
* the pre-update re-check validates a supplied confirmation even when no run is
  left in the census, and refuses again when the update would now interrupt
  more runs than were confirmed.

These run the real refusal, fence, census, entry points (``apply_update``,
``apply_force_update``, ``apply_clear_lock`` and their updaters over a local git
origin), the real dispatch and the real ``restart_active_profile_gateway`` with
its real ``gateway_lifecycle_capability`` read. Only ``Popen`` in the restart
helper (recorded, never a real ``hermes`` command), the Gateway PID read, the
WebUI self-restart, Gateway durability and the active profile home are stubbed.
"""

import io
import json
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import api.agent_health as agent_health
import api.config as cfg
import api.gateway_chat as gateway_chat
import api.gateway_restart as gateway_restart
import api.profiles as profiles
import api.run_drain as run_drain
import api.updates as upd
from tests import test_issue7293_force_update_offer_ui as harness

FIELDS = ("gateway_restart_managed", "gateway_profile", "gateway_endpoint")
_URL_VARS = ("GATEWAY_HEALTH_URL", "HERMES_GATEWAY_HEALTH_URL", "HERMES_API_URL", "HERMES_WEBUI_GATEWAY_BASE_URL")
ENDPOINT = "http://127.0.0.1:8642"
LEFT_RUNNING = (
    "The Agent checkout was updated, but the Gateway this profile uses changed after you "
    "confirmed, so the WebUI left the Gateway and itself running. Restart the WebUI and the "
    "Gateway when it suits you to finish the update."
)
RETRY_BLOCKED = (
    "The Agent checkout was updated and a Gateway restart was attempted, but it failed. The "
    "Gateway this profile uses then changed after you confirmed, so the WebUI did not retry "
    "the restart and left itself running. Restart the WebUI and the Gateway when it suits "
    "you to finish the update."
)
STALE = "The Gateway this profile uses changed since you confirmed. Try the update again."
ENTRIES = ("apply", "force", "clear_lock")


def _git(*args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def _agent_checkout(root: Path) -> Path:
    """A clone one commit behind its local origin."""
    seed, origin, clone = root / "seed", root / "origin.git", root / "agent"
    seed.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=seed)
    (seed / "f.txt").write_text("1\n")
    _git("add", ".", cwd=seed)
    _git("commit", "-qm", "c1", cwd=seed)
    _git("clone", "-q", "--bare", str(seed), str(origin), cwd=root)
    _git("clone", "-q", str(origin), str(clone), cwd=root)
    (seed / "f.txt").write_text("2\n")
    _git("commit", "-qam", "c2", cwd=seed)
    _git("push", "-q", str(origin), "main", cwd=seed)
    return clone


@pytest.fixture
def gw(monkeypatch, tmp_path):
    """One durable Gateway run, a real restart helper and a recording Popen."""
    for var in _URL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_API_URL", ENDPOINT)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path / "hermes")
    monkeypatch.setattr(profiles, "_root_profile_name_cache_loaded", True)
    monkeypatch.setattr(profiles, "_root_profile_name_cache", {"default"})
    monkeypatch.setattr(agent_health, "_gateway_ownership_record_path", lambda: tmp_path / "ownership.json")
    state = {"pid": None, "popen": [], "returncodes": [], "on_popen": None, "sched": 0, "checkout_moved": False}

    def pid(profile=None):
        return state["pid"]

    monkeypatch.setattr(agent_health, "get_active_profile_gateway_running_pid", pid)
    monkeypatch.setattr(upd, "get_active_profile_gateway_running_pid", pid)
    monkeypatch.setattr(gateway_chat, "gateway_run_durably_resumable", lambda sid: sid.startswith("gateway"))
    monkeypatch.setattr(gateway_restart, "_resolve_hermes_command", lambda: "stub-hermes")

    class Proc:
        def __init__(self, returncode):
            self.returncode = returncode

        def communicate(self, timeout=None):
            return ("", "simulated failure") if self.returncode else ("restarted", "")

    def popen(cmd, **kwargs):
        state["popen"].append({"cmd": list(cmd), "endpoint": agent_health._remote_gateway_base_url()})
        if state["on_popen"]:
            state["on_popen"](len(state["popen"]))
        return Proc(state["returncodes"].pop(0) if state["returncodes"] else 0)

    # Replace only the restart helper's view of subprocess: the updaters run
    # real git through the real module.
    monkeypatch.setattr(gateway_restart, "subprocess", SimpleNamespace(
        Popen=popen, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired,
    ))
    monkeypatch.setattr(upd, "_schedule_restart", lambda *a, **k: state.__setitem__("sched", state["sched"] + 1))
    monkeypatch.setattr(upd, "_AGENT_GATEWAY_RESTART_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(upd, "_AGENT_DIR", _agent_checkout(tmp_path / "repo"))
    real_run_git = upd._run_git

    def run_git(args, *a, **k):
        if args and args[0] in ("pull", "reset", "merge"):
            state["checkout_moved"] = True
        return real_run_git(args, *a, **k)

    monkeypatch.setattr(upd, "_run_git", run_git)
    profiles.set_request_profile("alpha")
    with cfg.ACTIVE_RUNS_LOCK:
        old_runs = dict(cfg.ACTIVE_RUNS)
        cfg.ACTIVE_RUNS.clear()
    with cfg.STREAMS_LOCK:
        old_streams = dict(cfg.STREAMS)
        cfg.STREAMS.clear()
    run_drain._reset_for_tests()
    cfg.register_active_run("gateway-0", backend="gateway")
    try:
        yield state
    finally:
        profiles.clear_request_profile()
        upd._forced_update_state.confirmed_gateway = None
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.clear()
            cfg.ACTIVE_RUNS.update(old_runs)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.clear()
            cfg.STREAMS.update(old_streams)
        run_drain._reset_for_tests()


def _entry(entry, **kwargs):
    if entry == "apply":
        return upd.apply_update("agent", "stable", **kwargs)
    if entry == "force":
        return upd.apply_force_update("agent", "stable", **kwargs)
    return upd.apply_clear_lock("agent", **kwargs)


def _refuse(entry):
    refusal = _entry(entry)
    assert refusal["restart_blocked"] is True and refusal["force_available"] is True, refusal
    return refusal


def _client_body(refusal):
    """The forced body static/ui.js sends after the confirmation."""
    w, g = refusal["webui_owned_runs"], refusal["gateway_runs"]
    interrupted = w if refusal["gateway_restart_managed"] is False else w + g
    body = {"target": "agent", "force_active_runs": True, "interrupted_runs": interrupted}
    body.update({field: refusal[field] for field in FIELDS})
    return body


def _force(entry, body):
    return _entry(entry, force_active_runs=True, confirmed_gateway=upd.confirmed_gateway_impact(body))


def _head():
    return _git("log", "-1", "--format=%s", cwd=upd._AGENT_DIR)


def _assert_nothing_happened(state):
    assert state["popen"] == [], "no Gateway restart was launched"
    assert state["sched"] == 0, "the WebUI did not restart itself"
    assert _head() == "c1", "the Agent checkout did not move"
    assert run_drain._HOLDS == {}, "the admission hold is released"


# --- R1: a confirmed-unmanaged force never restarts the Gateway -------------


@pytest.mark.parametrize("entry", ENTRIES)
def test_s3_gateway_restartable_after_the_dispatch_check_is_not_restarted(gw, monkeypatch, entry):
    """The Gateway PID becomes visible right after the last dispatch-side read."""
    refusal = _refuse(entry)
    assert refusal["gateway_restart_managed"] is False
    real_impact = upd._gateway_restart_impact

    def impact_then_flip():
        out = real_impact()
        if gw["checkout_moved"]:
            gw["pid"] = 4242  # restartable from now on, after this read
            gw["flipped"] = True
        return out

    monkeypatch.setattr(upd, "_gateway_restart_impact", impact_then_flip)
    out = _force(entry, _client_body(refusal))

    assert gw.get("flipped") is True, "the flip happened after the checkout moved"
    assert gw["popen"] == [], "confirmed-unmanaged: no Gateway restart, whatever ownership reads later"
    assert out["ok"] is True and out["gateway_restart"] == "external"
    assert _head() == "c2" and gw["sched"] == 1


def test_c1_pid_visible_after_the_final_check_launches_nothing(gw, monkeypatch):
    refusal = _refuse("apply")
    real_pid = upd.get_active_profile_gateway_running_pid

    def pid_then_flip(profile=None):
        gw["pid"] = 4242
        return real_pid(profile=profile)

    monkeypatch.setattr(upd, "get_active_profile_gateway_running_pid", pid_then_flip)
    out = _force("apply", _client_body(refusal))

    assert gw["popen"] == []
    assert out["ok"] is True and out["gateway_restart"] == "external"


def test_unmanaged_confirmation_unchanged_reports_the_external_result(gw):
    out = _force("apply", _client_body(_refuse("apply")))
    assert gw["popen"] == []
    assert out["ok"] is True and out["gateway_restart"] == "external" and out["restart_scheduled"] is True
    assert _head() == "c2" and gw["sched"] == 1
    assert upd._forced_update_state.confirmed_gateway is None, "the confirmation does not outlive the update"


@pytest.mark.parametrize("entry", ENTRIES)
def test_gateway_restartable_during_the_update_leaves_both_running(gw, monkeypatch, entry):
    """Item 4: the checkout moved, the Gateway is left alone, and so is the WebUI."""
    refusal = _refuse(entry)
    real_run_git = upd._run_git

    def flip_on_update(args, *a, **k):
        result = real_run_git(args, *a, **k)
        if args and args[0] in ("pull", "reset", "merge"):
            gw["pid"] = 4242
        return result

    monkeypatch.setattr(upd, "_run_git", flip_on_update)
    assert upd._check_repo(upd._AGENT_DIR, "agent", "stable")["behind"] == 1
    out = _force(entry, _client_body(refusal))

    assert gw["popen"] == [] and gw["sched"] == 0
    assert {k: out.get(k) for k in ("ok", "message", "target", "gateway_restart")} == {
        "ok": False, "message": LEFT_RUNNING, "target": "agent", "gateway_restart": "restart_blocked",
    }
    assert "restart_blocked" not in out, "no force offer: the checkout already moved"
    assert _head() == "c2"
    # After a reload the banner offers no Agent update (static/ui.js
    # agentUpdatable needs behind>0): the checkout is current, so the message
    # asks for a restart instead of "Update Now".
    assert upd._check_repo(upd._AGENT_DIR, "agent", "stable")["behind"] == 0


# --- R2: a confirmed-managed force restarts only the confirmed Gateway --------


@pytest.mark.parametrize("entry", ENTRIES)
def test_managed_confirmation_unchanged_restarts_exactly_that_gateway(gw, entry):
    gw["pid"] = 4242
    refusal = _refuse(entry)
    assert refusal["gateway_restart_managed"] is True
    out = _force(entry, _client_body(refusal))
    assert [p["cmd"] for p in gw["popen"]] == [["stub-hermes", "--profile", "alpha", "gateway", "restart"]]
    assert out["ok"] is True and out["gateway_restart"] == "completed" and gw["sched"] == 1


def test_c3_endpoint_change_during_the_retry_delay_launches_no_second_restart(gw, monkeypatch):
    gw["pid"] = 4242
    refusal = _refuse("apply")
    gw["returncodes"] = [1]  # the first launch fails transiently
    gw["on_popen"] = lambda n: monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8643") if n == 1 else None

    out = _force("apply", _client_body(refusal))

    assert [p["endpoint"] for p in gw["popen"]] == [ENDPOINT], "no retry against an unconfirmed endpoint"
    assert out["ok"] is False and out["gateway_restart"] == "restart_blocked"
    assert out["message"] == RETRY_BLOCKED and gw["sched"] == 0, "one restart already ran and failed"


def test_managed_retry_still_runs_when_the_confirmation_holds(gw):
    """Control for C3: the one #6045 retry is kept for the confirmed Gateway."""
    gw["pid"] = 4242
    refusal = _refuse("apply")
    gw["returncodes"] = [1, 0]
    out = _force("apply", _client_body(refusal))
    assert len(gw["popen"]) == 2
    assert out["ok"] is True and out["gateway_restart"] == "completed"


def test_managed_endpoint_change_during_the_update_is_caught_by_the_helper(gw, monkeypatch):
    gw["pid"] = 4242
    refusal = _refuse("apply")
    real_run_git = upd._run_git

    def move_endpoint(args, *a, **k):
        result = real_run_git(args, *a, **k)
        if args and args[0] in ("pull", "reset", "merge"):
            monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:9999")
        return result

    monkeypatch.setattr(upd, "_run_git", move_endpoint)
    out = _force("apply", _client_body(refusal))
    assert gw["popen"] == []
    assert out["ok"] is False and out["message"] == LEFT_RUNNING


@pytest.mark.parametrize("expected,launched", [
    ({"gateway_restart_managed": True, "gateway_profile": "alpha", "gateway_endpoint": ENDPOINT}, 1),
    ({"gateway_restart_managed": True, "gateway_profile": "beta", "gateway_endpoint": ENDPOINT}, 0),
    ({"gateway_restart_managed": True, "gateway_profile": "alpha", "gateway_endpoint": "http://127.0.0.1:1"}, 0),
    ({"gateway_restart_managed": False, "gateway_profile": "alpha", "gateway_endpoint": ENDPOINT}, 0),
], ids=["match", "profile", "endpoint", "managed"])
def test_restart_helper_compares_its_own_read_before_launching(gw, expected, launched):
    gw["pid"] = 4242
    out = gateway_restart.restart_active_profile_gateway(profile="alpha", expected_impact=expected)
    assert len(gw["popen"]) == launched
    assert out["status"] == ("completed" if launched else "restart_blocked")


# --- R3: the confirmation is validated regardless of the run census (C2) ------


@pytest.mark.parametrize("entry", ENTRIES)
def test_c2_tracked_run_finished_then_gateway_changed_is_stale(gw, entry):
    refusal = _refuse(entry)
    cfg.unregister_active_run("gateway-0")  # the tracked run finished
    gw["pid"] = 4242  # and the Gateway became restartable

    out = _force(entry, _client_body(refusal))

    _assert_nothing_happened(gw)
    assert out["ok"] is False and out["update_confirmation_stale"] is True
    assert out["message"] == STALE
    assert "restart_blocked" not in out and "force_available" not in out


def test_c2_managed_endpoint_change_with_an_empty_census_is_stale(gw, monkeypatch):
    gw["pid"] = 4242
    refusal = _refuse("apply")
    cfg.unregister_active_run("gateway-0")
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8643")
    out = _force("apply", _client_body(refusal))
    _assert_nothing_happened(gw)
    assert out["update_confirmation_stale"] is True


def test_empty_census_with_an_unchanged_confirmation_proceeds(gw):
    refusal = _refuse("apply")
    cfg.unregister_active_run("gateway-0")
    out = _force("apply", _client_body(refusal))
    assert out["ok"] is True and out["gateway_restart"] == "external" and gw["popen"] == []


@pytest.mark.parametrize("target,force", [("webui", True), ("webui", False), ("agent", False)])
def test_a_confirmation_counts_only_on_a_forced_agent_update(gw, target, force):
    """A WebUI or unforced request with a confirmation that no longer matches
    runs as usual: there is no Gateway impact to compare for the WebUI, and an
    unforced update follows the current ownership, with nothing bound to it."""
    cfg.unregister_active_run("gateway-0")  # nothing blocks an unforced update
    stale = upd.confirmed_gateway_impact({
        "gateway_restart_managed": True, "gateway_profile": "alpha",
        "gateway_endpoint": ENDPOINT, "interrupted_runs": 0,
    })
    seen = []

    def run():
        seen.append(upd._forced_update_state.confirmed_gateway)
        return {"ok": True, "target": target}

    out = upd._run_fenced_update(target, force, run, confirmed_gateway=stale)

    assert out == {"ok": True, "target": target}
    assert seen == [None], "the update ran with no confirmation bound to it"


# --- R4: the interrupted-run count is part of the confirmation (S9) ----------


@pytest.mark.parametrize("entry", ENTRIES)
def test_s9_webui_run_started_after_a_zero_interrupt_confirmation_refuses_again(gw, entry):
    refusal = _refuse(entry)
    assert (refusal["webui_owned_runs"], refusal["gateway_runs"], refusal["gateway_restart_managed"]) == (0, 1, False)
    body = _client_body(refusal)
    assert body["interrupted_runs"] == 0
    cfg.register_active_run("webui-late", backend="legacy")  # starts while the offer is on screen

    out = _force(entry, body)

    _assert_nothing_happened(gw)
    assert out["restart_blocked"] is True and out["force_available"] is True
    assert (out["webui_owned_runs"], out["gateway_runs"]) == (1, 1)


def test_more_runs_than_confirmed_on_a_managed_gateway_refuses_again(gw):
    gw["pid"] = 4242
    refusal = _refuse("apply")
    body = _client_body(refusal)
    assert body["interrupted_runs"] == 1
    cfg.register_active_run("gateway-1", backend="gateway")
    out = _force("apply", body)
    _assert_nothing_happened(gw)
    assert out["restart_blocked"] is True and out["gateway_runs"] == 2


def test_fewer_runs_than_confirmed_proceeds(gw):
    cfg.register_active_run("webui-0", backend="legacy")
    refusal = _refuse("apply")
    body = _client_body(refusal)
    assert body["interrupted_runs"] == 1
    cfg.unregister_active_run("webui-0")
    out = _force("apply", body)
    assert out["ok"] is True and gw["popen"] == []


def test_a_body_without_the_count_is_not_count_checked(gw):
    """Old clients (no interrupted_runs) keep the fold behaviour."""
    refusal = _refuse("apply")
    body = _client_body(refusal)
    del body["interrupted_runs"]
    cfg.register_active_run("webui-late", backend="legacy")
    out = _force("apply", body)
    assert out["ok"] is True and gw["popen"] == []


# --- Item 5: the forced log names no WebUI runs when there are none ----------


def test_forced_log_drops_the_webui_clause_without_webui_runs(gw, caplog):
    with caplog.at_level(logging.WARNING, logger=upd.logger.name):
        upd.apply_update("agent", "stable", force_active_runs=True)
    text = " ".join(r.getMessage() for r in caplog.records if r.getMessage().startswith("Forced agent"))
    assert text.startswith(
        "Forced agent update with 1 active run(s) (0 WebUI, 1 Gateway); the WebUI could not prove"
    ), text
    assert "saved as interrupted" not in text


# --- Body parsing, routes (item 3) and wrong-typed fields (item 6) ------------


def test_confirmation_parser_reads_the_count():
    base = {"gateway_restart_managed": False, "gateway_profile": "alpha", "gateway_endpoint": ENDPOINT}
    assert upd.confirmed_gateway_impact({**base, "interrupted_runs": 0})["interrupted_runs"] == 0
    assert upd.confirmed_gateway_impact(base)["interrupted_runs"] is None
    for bad in (True, -1, 1.0, "0", None):
        with pytest.raises(ValueError):
            upd.confirmed_gateway_impact({**base, "interrupted_runs": bad})
    for bad in ("false", 0, None):
        with pytest.raises(ValueError):
            upd.confirmed_gateway_impact({**base, "gateway_restart_managed": bad})
    assert upd.confirmed_gateway_impact({"target": "agent", "force_active_runs": True}) is None


class _Handler:
    def __init__(self, path, body):
        raw = json.dumps(body).encode()
        self.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json",
                        "Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787"}
        self.rfile, self.wfile = io.BytesIO(raw), io.BytesIO()
        self.client_address, self.command, self.path, self.status = ("127.0.0.1", 5), "POST", path, None

    def send_response(self, status, *args):
        self.status = status

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass

    def log_message(self, *args):
        pass


_ROUTES = {"/api/updates/apply": "apply_update", "/api/updates/force": "apply_force_update",
           "/api/updates/clear_lock": "apply_clear_lock"}
_CONFIRMATION = {"gateway_restart_managed": False, "gateway_profile": "alpha",
                 "gateway_endpoint": ENDPOINT, "interrupted_runs": 0}


@pytest.fixture
def routes_recorder(monkeypatch):
    import api.routes as routes

    seen = []
    for name in _ROUTES.values():
        monkeypatch.setattr(upd, name, lambda *a, _n=name, **k: seen.append((_n, k)) or {"ok": True})
    return routes, seen


@pytest.mark.parametrize("path", sorted(_ROUTES))
def test_each_update_route_forwards_the_confirmation(routes_recorder, path):
    routes, seen = routes_recorder
    handler = _Handler(path, {"target": "agent", "force_active_runs": True, **_CONFIRMATION})
    routes.handle_post(handler, urlparse(path))
    assert seen == [(_ROUTES[path], {"force_active_runs": True, "confirmed_gateway": _CONFIRMATION})]


@pytest.mark.parametrize("path", sorted(_ROUTES))
@pytest.mark.parametrize("field,value", [("gateway_restart_managed", "false"), ("interrupted_runs", -1)])
def test_each_update_route_rejects_a_wrong_typed_confirmation(routes_recorder, path, field, value):
    routes, seen = routes_recorder
    handler = _Handler(path, {"target": "agent", "force_active_runs": True, **_CONFIRMATION, field: value})
    routes.handle_post(handler, urlparse(path))
    assert handler.status == 400 and seen == []


# --- Client senders (Node harness) -------------------------------------------

_AGENT_REFUSAL = dict(
    target="agent", gateway_restart_managed=False, webui_owned_runs=0, gateway_runs=1,
    gateway_profile="alpha", gateway_endpoint=ENDPOINT,
)
_CLIENT_STATE_HARNESS = harness._HARNESS.replace(
    "afterClick = { offer: offerState() };",
    "afterClick = { offer: offerState(), errorText: errorEl._text,"
    " applyBtn: { disabled: applyBtn.disabled, text: applyBtn.textContent },"
    " forceBtn: { disabled: forceBtn.disabled, display: forceBtn.style.display },"
    " clearLockBtn: { disabled: clearLockBtn.disabled, text: clearLockBtn.textContent } };",
)


def _run_client(scenario, tmp_path):
    assert _CLIENT_STATE_HARNESS != harness._HARNESS
    snippet = tmp_path / "ui_snippet.js"
    snippet.write_text(harness._ui_snippet(), encoding="utf-8")
    result = subprocess.run(
        [harness.NODE, "-e", _CLIENT_STATE_HARNESS, json.dumps(scenario), str(harness.I18N_JS), str(snippet)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["caught"] == [], out["caught"]
    return out


_SENDERS = {"apply": "/api/updates/apply", "force": "/api/updates/force", "clear_lock": "/api/updates/clear_lock"}


def _sender_scenario(entry, second_response):
    return {
        "entry": entry, "target": "agent", "updateData": harness._AGENT_BEHIND,
        "responses": [harness._refusal(1, **_AGENT_REFUSAL), second_response],
        "clickOffer": True,
        # force: the discard-local-changes confirm first, then the offer's confirm.
        "confirmAnswers": [True, True] if entry == "force" else [True],
    }


@pytest.mark.skipif(harness.NODE is None, reason="node required for the ui.js harness")
@pytest.mark.parametrize("entry", sorted(_SENDERS))
def test_every_sender_forwards_the_confirmation_and_count(entry, tmp_path):
    out = _run_client(_sender_scenario(entry, {"ok": True, "target": "agent"}), tmp_path)
    forced = out["apiCalls"][1]
    assert forced["url"] == _SENDERS[entry]
    assert {k: forced["body"].get(k) for k in (*FIELDS, "interrupted_runs", "force_active_runs")} == {
        "gateway_restart_managed": False, "gateway_profile": "alpha", "gateway_endpoint": ENDPOINT,
        "interrupted_runs": 0, "force_active_runs": True,
    }


@pytest.mark.skipif(harness.NODE is None, reason="node required for the ui.js harness")
@pytest.mark.parametrize("entry", sorted(_SENDERS))
def test_every_sender_shows_a_stale_confirmation_and_unblocks(entry, tmp_path):
    stale = {"ok": False, "target": "agent", "update_confirmation_stale": True, "message": STALE}
    out = _run_client(_sender_scenario(entry, stale), tmp_path)
    after = out["afterClick"]
    assert STALE in after["errorText"], after
    assert after["offer"]["count"] == 0, "the offer for the old confirmation is gone"
    assert after["applyBtn"] == {"disabled": False, "text": "Update Now"}
    assert out["applyInFlight"] is False and out["waitCalls"] == 0
    if entry == "force":
        assert after["forceBtn"] == {"disabled": False, "display": "inline-block"}
    if entry == "clear_lock":
        assert after["clearLockBtn"]["disabled"] is False
