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
)


# ── The refusal payload the banner renders ──────────────────────────────────


def test_agent_refusal_names_webui_and_gateway_runs():
    import api.updates as upd

    result = upd._restart_blocked_response(
        "agent", {"active_runs": 2, "active_streams": 2}
    )

    message = result["message"]
    assert "2 agent runs are active" in message
    assert "(WebUI and Gateway)" in message
    for kind in BOTH_RUN_TYPES:
        assert kind in message
    # The user must learn WHY the Gateway runs are part of the count: the
    # Agent update restarts the Gateway.
    assert "also restarts the Gateway" in message
    # The way out is unchanged.
    assert "force the update to interrupt the active runs" in message


def test_agent_refusal_uses_the_singular_form():
    import api.updates as upd

    message = upd._restart_blocked_response(
        "agent", {"active_runs": 1, "active_streams": 0}
    )["message"]

    assert "1 agent run is active" in message
    for kind in BOTH_RUN_TYPES:
        assert kind in message
    assert "interrupt the active run." in message


def test_webui_refusal_keeps_its_webui_only_wording():
    """A WebUI update still reattaches resumable Gateway runs (#7293)."""

    import api.updates as upd

    message = upd._restart_blocked_response(
        "webui", {"active_runs": 2, "active_streams": 2}
    )["message"]

    assert message == (
        "Cannot update webui while 2 WebUI agent runs are active. "
        "Wait for the response to finish and retry, or force the update to "
        "interrupt the active runs."
    )
    assert "Gateway" not in message


def test_agent_update_refusal_reaches_the_caller_with_both_run_types(tmp_path, monkeypatch):
    """End to end through apply_update(): the payload the banner receives."""

    import queue

    import api.updates as upd
    from api.config import STREAMS, STREAMS_LOCK

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(upd, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(upd, "_AGENT_DIR", tmp_path)
    monkeypatch.setattr(upd, "_run_git", lambda *a, **k: ("", True))
    monkeypatch.setattr(
        upd,
        "_schedule_restart",
        lambda delay=2.0: (_ for _ in ()).throw(AssertionError("must not restart")),
    )

    with STREAMS_LOCK:
        old = dict(STREAMS)
        STREAMS.clear()
        STREAMS["stream_active"] = queue.Queue()
    try:
        result = upd.apply_update("agent")
    finally:
        with STREAMS_LOCK:
            STREAMS.clear()
            STREAMS.update(old)

    assert result["ok"] is False
    assert result["restart_blocked"] is True
    assert result["active_runs"] == 1
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
        "blocking_run_ids": [],
        "blocking_stream_ids": [],
    }
    monkeypatch.setattr(upd, "_update_blocker_snapshot", lambda _target: dict(blocked))
    monkeypatch.setattr(run_drain, "hold_run_admission", lambda owner, force=False: "hold")
    monkeypatch.setattr(run_drain, "release_run_admission_hold", lambda token: None)
    return upd._run_fenced_update(target, True, lambda: {"ok": True})


def test_forced_agent_update_log_names_both_run_types(monkeypatch, caplog):
    import api.updates as upd

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
    out[code][key] = value === undefined
      ? null
      : [1, 2].map((n) => String(typeof value === 'function' ? value(n) : value));
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


def test_agent_confirm_message_names_both_run_types_in_every_locale(locale_messages):
    failures = []
    for code, keys in locale_messages["messages"].items():
        for index, count in ((0, 1), (1, 2)):
            message = keys["update_force_active_runs_message_agent"][index]
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


def test_agent_confirm_message_reports_the_run_count(locale_messages):
    """Every locale interpolates N — a message that drops it loses the context."""

    missing_count = []
    for code, keys in locale_messages["messages"].items():
        for index, count in ((0, 1), (1, 2)):
            message = keys["update_force_active_runs_message_agent"][index]
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
