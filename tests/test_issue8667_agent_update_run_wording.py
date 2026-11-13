"""#8667: a forced Agent update stops Gateway runs too, so every text must say so.

#8660/#8662 made an Agent update count every live run
(``run_drain.all_active_runs``): the update also cycles the Hermes Gateway that
executes the resumable runs, so those runs do not survive it. The refusal text,
the forced-path log and the confirm-dialog message still described the
WebUI-only case, which promised the user that Gateway work keeps running.

The wording is asserted on the values that reach the user/operator (the refusal
payload, the log records, the locale table evaluated in Node), not on source
strings.
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
I18N_JS = ROOT / "static" / "i18n.js"
NODE = shutil.which("node")

# Both kinds of run an Agent update interrupts. "WebUI" and "Gateway" are kept
# verbatim in every locale of static/i18n.js (product names).
BOTH_RUN_TYPES = ("WebUI", "Gateway")

_MESSAGE_KEYS = (
    "update_force_active_runs_message",
    "update_force_active_runs_message_agent",
    "update_force_active_runs_message_agent_external",
    "update_force_active_runs_message_agent_webui_only",
    "update_force_active_runs_no_runs",
)


# ── The refusal payload the banner renders ──────────────────────────────────


def test_agent_refusal_names_webui_and_gateway_runs(monkeypatch):
    import api.updates as upd

    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": True}
    )

    result = upd._restart_blocked_response(
        "agent", {"active_runs": 2, "active_streams": 2, "webui_owned_runs": 1, "gateway_runs": 1}
    )

    assert result["gateway_restart_managed"] is True
    assert result["webui_owned_runs"] == 1
    assert result["gateway_runs"] == 1
    message = result["message"]
    assert "2 agent runs are active" in message
    assert "(WebUI and Gateway)" in message
    for kind in BOTH_RUN_TYPES:
        assert kind in message
    # The user must learn WHY the Gateway runs are part of the count: the
    # Agent update restarts the Gateway this profile uses.
    assert (
        "Updating the Agent also restarts the Gateway this profile uses and stops every "
        "run that Gateway is executing, including runs started from other profiles that "
        "use the same Gateway endpoint; only the WebUI runs are settled as interrupted."
    ) in message
    # The way out is unchanged.
    assert (
        "force the update to interrupt the 1 WebUI run and restart the Gateway this profile uses."
    ) in message


def test_agent_refusal_uses_the_singular_form(monkeypatch):
    import api.updates as upd

    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": True}
    )

    result = upd._restart_blocked_response(
        "agent", {"active_runs": 1, "active_streams": 0, "webui_owned_runs": 0, "gateway_runs": 1}
    )
    assert result["gateway_restart_managed"] is True
    assert result["webui_owned_runs"] == 0
    assert result["gateway_runs"] == 1
    message = result["message"]

    assert "1 agent run is active" in message
    assert "(WebUI and Gateway)" in message
    for kind in BOTH_RUN_TYPES:
        assert kind in message
    assert (
        "Updating the Agent also restarts the Gateway this profile uses and stops every "
        "run that Gateway is executing, including runs started from other profiles that "
        "use the same Gateway endpoint; only the WebUI runs are settled as interrupted."
    ) in message
    assert (
        "force the update to restart the Gateway this profile uses and stop the Gateway runs it is executing."
    ) in message


def test_agent_refusal_when_gateway_unmanaged_does_not_promise_restart(monkeypatch):
    import api.updates as upd

    monkeypatch.setattr(
        upd,
        "gateway_lifecycle_capability",
        lambda profile=None: {"managed": False, "reason": "external_gateway"},
    )

    result_plural = upd._restart_blocked_response(
        "agent", {"active_runs": 2, "active_streams": 2, "webui_owned_runs": 1, "gateway_runs": 1}
    )
    assert result_plural["gateway_restart_managed"] is False
    assert result_plural["webui_owned_runs"] == 1
    assert result_plural["gateway_runs"] == 1
    assert result_plural["message"] == (
        "Cannot update agent while 2 agent runs are active (WebUI and Gateway). "
        "The Gateway this profile uses is managed outside the WebUI, so updating the Agent "
        "does not restart it and its Gateway runs keep running. "
        "Wait for the response to finish and retry, or force the update to interrupt the 1 WebUI run; "
        "the Gateway runs keep running."
    )
    assert "restarts the Gateway" not in result_plural["message"]
    assert "managed outside the WebUI" in result_plural["message"]
    for kind in BOTH_RUN_TYPES:
        assert kind in result_plural["message"]

    result_singular = upd._restart_blocked_response(
        "agent", {"active_runs": 1, "active_streams": 0, "webui_owned_runs": 0, "gateway_runs": 1}
    )
    assert result_singular["gateway_restart_managed"] is False
    assert result_singular["webui_owned_runs"] == 0
    assert result_singular["gateway_runs"] == 1
    assert result_singular["message"] == (
        "Cannot update agent while 1 agent run is active (WebUI and Gateway). "
        "The Gateway this profile uses is managed outside the WebUI, so updating the Agent "
        "does not restart it and its Gateway runs keep running. "
        "Wait for the Gateway runs to finish and retry; forcing the update will not stop them."
    )


def test_agent_refusal_resolves_unproven_capability_safely_to_unmanaged(monkeypatch):
    import api.updates as upd

    def _broken(profile=None):
        raise RuntimeError("capability check failed")

    monkeypatch.setattr(upd, "gateway_lifecycle_capability", _broken)

    result = upd._restart_blocked_response(
        "agent", {"active_runs": 2, "active_streams": 2, "webui_owned_runs": 1, "gateway_runs": 1}
    )
    assert result["gateway_restart_managed"] is False
    assert "The WebUI cannot prove it owns the Gateway" in result["message"]

    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": "invalid"}
    )
    result_malformed = upd._restart_blocked_response(
        "agent", {"active_runs": 2, "active_streams": 2, "webui_owned_runs": 1, "gateway_runs": 1}
    )
    assert result_malformed["gateway_restart_managed"] is False
    assert "The WebUI cannot prove it owns the Gateway" in result_malformed["message"]


def test_agent_refusal_cross_profile_warning_scope(monkeypatch):
    """#8746: cross-profile warning with explicit scope controls.

    When a durable Gateway run belongs to profile 'beta' while the request
    selects 'alpha', the process-wide census counts 1 run, WebUI-owned runs
    are empty, and the restart dispatcher asks only for 'alpha'. The refusal
    warns that restarting the Gateway used by this profile stops runs from
    any profile using the same endpoint.
    """
    import api.config as cfg
    import api.gateway_chat as gateway_chat
    import api.run_drain as run_drain
    import api.updates as upd

    monkeypatch.setattr(upd, "get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": True}
    )
    monkeypatch.setattr(
        gateway_chat, "gateway_run_durably_resumable", lambda stream_id: True
    )

    restarted_profiles: list[str] = []

    def _fake_restart(*, profile="default"):
        restarted_profiles.append(profile)
        return {"status": "completed"}

    monkeypatch.setattr(upd, "restart_active_profile_gateway", _fake_restart)
    monkeypatch.setattr(
        upd, "get_active_profile_gateway_running_pid", lambda profile=None: 1234
    )

    # 1. Endpoints compartilhados: alpha e beta resolvem par idêntico
    assert gateway_chat._gateway_endpoint_for_profile("alpha") == gateway_chat._gateway_endpoint_for_profile("beta")

    with cfg.ACTIVE_RUNS_LOCK:
        old_active = dict(cfg.ACTIVE_RUNS)
        cfg.ACTIVE_RUNS.clear()
    with cfg.STREAMS_LOCK:
        old_streams = dict(cfg.STREAMS)
        cfg.STREAMS.clear()

    stream_beta = "stream-cross-profile-beta"
    try:
        cfg.register_active_run(stream_beta, backend="gateway", profile="beta")

        # Pre-oracle assertions
        all_runs = run_drain.all_active_runs()
        assert len(all_runs) == 1
        assert run_drain.webui_owned_active_runs() == []

        ok, restart_payload = upd._ensure_gateway_restart_for_agent_update()
        assert ok is True
        assert restarted_profiles == ["alpha"]

        # Oracle assertions on the refusal message
        blocker = upd._update_blocker_snapshot("agent")
        # #8660 keeps the blocking census free of any durability read; the
        # per-kind split is built where the copy is, in the refusal.
        assert blocker["active_runs"] == 1
        assert "gateway_runs" not in blocker
        result = upd._restart_blocked_response("agent", blocker)
        assert result["gateway_restart_managed"] is True
        assert result["gateway_runs"] == 1
        assert result["webui_owned_runs"] == 0
        message = result["message"]

        assert "including runs started from other profiles that use the same Gateway endpoint" in message
        assert "only the WebUI runs are settled as interrupted" in message
        assert "Gateway runs in other profiles are outside that restart" not in message
        assert "outside that restart" not in message
        assert "outside the restart" not in message
        assert (
            "restarts the Gateway this profile uses and stops every run that Gateway is executing"
        ) in message

        # 2. Endpoints separados (controle): profile beta com URL própria
        def _separate_endpoint(p):
            if p == "beta":
                return ("http://127.0.0.1:9999", "key-beta")
            return ("http://127.0.0.1:8642", "")

        monkeypatch.setattr(gateway_chat, "_gateway_endpoint_for_profile", _separate_endpoint)
        assert gateway_chat._gateway_endpoint_for_profile("alpha") != gateway_chat._gateway_endpoint_for_profile("beta")

        result_sep = upd._restart_blocked_response("agent", blocker)
        message_sep = result_sep["message"]
        assert "including runs started from other profiles that use the same Gateway endpoint" in message_sep
        assert "outside that restart" not in message_sep
        assert "outside the restart" not in message_sep
    finally:
        cfg.unregister_active_run(stream_beta)
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.clear()
            cfg.ACTIVE_RUNS.update(old_active)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.clear()
            cfg.STREAMS.update(old_streams)


def test_webui_refusal_keeps_its_webui_only_wording():
    """A WebUI update still reattaches resumable Gateway runs (#7293)."""

    import api.updates as upd

    result = upd._restart_blocked_response(
        "webui", {"active_runs": 2, "active_streams": 2}
    )
    message = result["message"]

    assert message == (
        "Cannot update webui while 2 WebUI agent runs are active. "
        "Wait for the response to finish and retry, or force the update to "
        "interrupt the active runs."
    )
    assert "Gateway" not in message
    assert "gateway_restart_managed" not in result


def test_agent_update_refusal_reaches_the_caller_with_both_run_types(tmp_path, monkeypatch):
    """End to end through apply_update(): the payload the banner receives."""

    import queue

    import api.gateway_chat as gateway_chat
    import api.updates as upd
    from api.config import ACTIVE_RUNS, ACTIVE_RUNS_LOCK, STREAMS, STREAMS_LOCK

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(upd, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(upd, "_AGENT_DIR", tmp_path)
    monkeypatch.setattr(upd, "_run_git", lambda *a, **k: ("", True))
    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": True}
    )
    monkeypatch.setattr(
        gateway_chat, "gateway_run_durably_resumable", lambda stream_id: True
    )
    monkeypatch.setattr(
        upd,
        "_schedule_restart",
        lambda delay=2.0: (_ for _ in ()).throw(AssertionError("must not restart")),
    )

    with STREAMS_LOCK:
        old_streams = dict(STREAMS)
        STREAMS.clear()
        STREAMS["stream_active"] = queue.Queue()
    with ACTIVE_RUNS_LOCK:
        old_runs = dict(ACTIVE_RUNS)
        ACTIVE_RUNS.clear()
        ACTIVE_RUNS["gw_active"] = {
            "session_id": "s1",
            "stream_id": "gw_active",
            "backend": "gateway",
            "profile": "default",
        }
    try:
        result = upd.apply_update("agent")
    finally:
        with ACTIVE_RUNS_LOCK:
            ACTIVE_RUNS.clear()
            ACTIVE_RUNS.update(old_runs)
        with STREAMS_LOCK:
            STREAMS.clear()
            STREAMS.update(old_streams)

    assert result["ok"] is False
    assert result["restart_blocked"] is True
    assert result["active_runs"] == 2
    assert result["webui_owned_runs"] == 1
    assert result["gateway_runs"] == 1
    for kind in BOTH_RUN_TYPES:
        assert kind in result["message"], result["message"]


# ── The forced-path log the operator reads ──────────────────────────────────


def _forced_update(monkeypatch, target):
    import api.updates as upd
    from api import run_drain

    blocked = {
        "restart_blocked": True,
        "active_runs": 3,
        "active_streams": 2,
        "webui_owned_runs": 1,
        "gateway_runs": 2,
        "blocking_run_ids": [],
        "blocking_stream_ids": [],
    }
    monkeypatch.setattr(upd, "_update_blocker_snapshot", lambda _target: dict(blocked))
    monkeypatch.setattr(run_drain, "hold_run_admission", lambda owner, force=False: "hold")
    monkeypatch.setattr(run_drain, "release_run_admission_hold", lambda token: None)
    return upd._run_fenced_update(target, True, lambda: {"ok": True})


def test_forced_agent_update_log_names_both_run_types(monkeypatch, caplog):
    import api.updates as upd

    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": True}
    )

    with caplog.at_level(logging.WARNING, logger=upd.logger.name):
        result = _forced_update(monkeypatch, "agent")

    assert result == {"ok": True}
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "3" in text
    for kind in BOTH_RUN_TYPES:
        assert kind in text, text
    # The WebUI settles its own runs; the Gateway runs end with the Gateway
    # cycle and are settled afterwards by the worker teardown/watchdog.
    assert "settled as interrupted" in text
    assert "Gateway cycles" in text
    assert (
        "the Gateway runs this profile's Gateway is executing end when the Gateway cycles, "
        "including runs started from other profiles that use the same Gateway endpoint"
    ) in text


def test_forced_agent_update_log_when_gateway_unmanaged(monkeypatch, caplog):
    import api.updates as upd

    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": False}
    )

    with caplog.at_level(logging.WARNING, logger=upd.logger.name):
        result = _forced_update(monkeypatch, "agent")

    assert result == {"ok": True}
    text = " ".join(record.getMessage() for record in caplog.records)
    assert (
        "Forced agent update with 3 active run(s) (WebUI and Gateway); "
        "the WebUI runs are settled as interrupted before the restart, "
        "and the Gateway runs keep running because the Gateway is managed "
        "outside the WebUI"
    ) in text
    assert "Gateway cycles" not in text


def test_forced_webui_update_log_does_not_claim_gateway_runs_stop(monkeypatch, caplog):
    """A WebUI update leaves resumable Gateway runs running (#7293)."""

    import api.updates as upd

    with caplog.at_level(logging.WARNING, logger=upd.logger.name):
        result = _forced_update(monkeypatch, "webui")

    assert result == {"ok": True}
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "settled as interrupted" in text
    assert "Gateway" not in text, text


# ── The confirm dialog copy in every locale ─────────────────────────────────

_I18N_PROBE = r"""
const fs = require('fs');
const vm = require('vm');

const src = fs.readFileSync(process.argv[1], 'utf8');
const ctx = {
  console,
  navigator: { languages: ['en-US'], language: 'en-US' },
  localStorage: { getItem() { return null; }, setItem() {} },
  sessionStorage: { removeItem() {} },
  document: {
    documentElement: {},
    readyState: 'complete',
    addEventListener() {},
    querySelectorAll() { return []; },
    createElement() { return {}; },
    getElementById() { return null; },
  },
  setTimeout() {},
  clearTimeout() {},
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);

const keys = JSON.parse(process.argv[2]);
const codes = vm.runInContext('Object.keys(LOCALES)', ctx);
const out = {};
for (const code of codes) {
  out[code] = {};
  for (const key of keys) {
    const value = vm.runInContext(
      'LOCALES[' + JSON.stringify(code) + '][' + JSON.stringify(key) + ']', ctx);
    if (value === undefined) {
      out[code][key] = null;
    } else if (key === 'update_force_active_runs_no_runs') {
      out[code][key] = String(typeof value === 'function' ? value() : value);
    } else {
      out[code][key] = [1, 2].map((n) => String(typeof value === 'function' ? value(n, n) : value));
    }
  }
}
console.log(JSON.stringify({ order: codes, messages: out }));
"""


@pytest.fixture(scope="module")
def locale_messages():
    if NODE is None:  # pragma: no cover - the suite runs with node present
        pytest.skip("node required to evaluate static/i18n.js")
    result = subprocess.run(
        [NODE, "-e", _I18N_PROBE, str(I18N_JS), json.dumps(list(_MESSAGE_KEYS))],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node probe failed: {result.stderr or result.stdout}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_every_locale_has_the_agent_message_key(locale_messages):
    missing = [
        code
        for code, keys in locale_messages["messages"].items()
        if keys["update_force_active_runs_message_agent"] is None
    ]
    assert missing == [], f"locales without update_force_active_runs_message_agent: {missing}"
    assert len(locale_messages["order"]) == 15


def test_every_locale_has_the_agent_external_message_key(locale_messages):
    missing = [
        code
        for code, keys in locale_messages["messages"].items()
        if keys["update_force_active_runs_message_agent_external"] is None
    ]
    assert missing == [], f"locales without update_force_active_runs_message_agent_external: {missing}"
    assert len(locale_messages["order"]) == 15


def test_every_locale_has_the_agent_webui_only_message_key(locale_messages):
    missing = [
        code
        for code, keys in locale_messages["messages"].items()
        if keys["update_force_active_runs_message_agent_webui_only"] is None
    ]
    assert missing == [], f"locales without update_force_active_runs_message_agent_webui_only: {missing}"
    assert len(locale_messages["order"]) == 15


def test_every_locale_has_the_no_runs_key(locale_messages):
    missing = [
        code
        for code, keys in locale_messages["messages"].items()
        if keys["update_force_active_runs_no_runs"] is None
    ]
    assert missing == [], f"locales without update_force_active_runs_no_runs: {missing}"
    assert len(locale_messages["order"]) == 15


def test_no_runs_returns_a_string(locale_messages):
    for code, keys in locale_messages["messages"].items():
        val = keys["update_force_active_runs_no_runs"]
        assert isinstance(val, str) and len(val) > 0, f"{code}: empty no_runs"


def test_agent_confirm_message_names_both_run_types_in_every_locale(locale_messages):
    failures = []
    for code, keys in locale_messages["messages"].items():
        for index, count in ((0, 1), (1, 2)):
            message = keys["update_force_active_runs_message_agent"][index]
            for kind in BOTH_RUN_TYPES:
                if kind not in message:
                    failures.append(f"{code} (n={count}): missing {kind!r} -> {message!r}")
    assert failures == [], "\n".join(failures)


def test_agent_external_confirm_message_names_both_run_types_in_every_locale(locale_messages):
    failures = []
    for code, keys in locale_messages["messages"].items():
        for index, count in ((0, 1), (1, 2)):
            message = keys["update_force_active_runs_message_agent_external"][index]
            for kind in BOTH_RUN_TYPES:
                if kind not in message:
                    failures.append(f"{code} (n={count}): missing {kind!r} -> {message!r}")
    assert failures == [], "\n".join(failures)


def test_agent_confirm_message_differs_from_the_webui_one_in_every_locale(locale_messages):
    """The Agent wording must add the Gateway case, not repeat the WebUI copy."""

    identical = [
        code
        for code, keys in locale_messages["messages"].items()
        if keys["update_force_active_runs_message_agent"]
        == keys["update_force_active_runs_message"]
    ]
    assert identical == [], f"locales where the Agent copy is the WebUI copy: {identical}"


def test_agent_external_confirm_message_differs_from_managed_in_every_locale(locale_messages):
    """The external Agent copy must differ from the managed Agent copy in every locale."""

    identical = [
        code
        for code, keys in locale_messages["messages"].items()
        if keys["update_force_active_runs_message_agent_external"]
        == keys["update_force_active_runs_message_agent"]
    ]
    assert identical == [], f"locales where external copy is managed copy: {identical}"


def test_agent_confirm_message_reports_the_run_count(locale_messages):
    """Every locale interpolates N — a message that drops it loses the context."""

    missing_count = []
    for code, keys in locale_messages["messages"].items():
        for index, count in ((0, 1), (1, 2)):
            message = keys["update_force_active_runs_message_agent"][index]
            if str(count) not in message:
                missing_count.append(f"{code} (n={count}): {message!r}")
    assert missing_count == [], "\n".join(missing_count)


def test_agent_external_confirm_message_reports_the_run_count(locale_messages):
    """Every locale interpolates N — a message that drops it loses the context."""

    missing_count = []
    for code, keys in locale_messages["messages"].items():
        for index, count in ((0, 1), (1, 2)):
            message = keys["update_force_active_runs_message_agent_external"][index]
            if str(count) not in message:
                missing_count.append(f"{code} (n={count}): {message!r}")
    assert missing_count == [], "\n".join(missing_count)


def test_agent_webui_only_reports_the_run_count(locale_messages):
    missing_count = []
    for code, keys in locale_messages["messages"].items():
        for index, count in ((0, 1), (1, 2)):
            message = keys["update_force_active_runs_message_agent_webui_only"][index]
            if str(count) not in message:
                missing_count.append(f"{code} (n={count}): {message!r}")
    assert missing_count == [], "\n".join(missing_count)


def test_webui_confirm_message_still_names_only_the_webui_case(locale_messages):
    """#7293 copy is unchanged for a WebUI update."""

    offenders = [
        code
        for code, keys in locale_messages["messages"].items()
        if "WebUI" not in keys["update_force_active_runs_message"][1]
    ]
    assert offenders == []
    assert "WebUI agent runs are still working" in (
        locale_messages["messages"]["en"]["update_force_active_runs_message"][1]
    )
