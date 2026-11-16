"""#8746: the forced Agent update confirmation stays coherent when it interrupts nothing.

With only durable work on an external Gateway the refusal reports
``webui_owned_runs=0`` and the offer reads "Update anyway (interrupts nothing)".
Clicking it must not open an "Interrupt active runs?" dialog that starts with
"0 WebUI agent runs are still working": the title, message and confirm label
name the Gateway runs that keep running instead. The mixed, managed,
non-durable Gateway and WebUI-update dialogs keep their interruption copy.

The real run registry, blocking census and refusal producer feed the production
``_renderActiveRunsForceOffer`` through the #7293 Node minimal-DOM harness with
the real ``static/i18n.js``. Gateway durability and lifecycle capability are the
controlled seams; the network, confirmation and reload are doubled, and every
census-driven confirmation is cancelled.
"""

import json
import subprocess

import pytest

import api.config as cfg
import api.gateway_chat as gateway_chat
import api.updates as upd
from tests import test_issue7293_force_update_offer_ui as harness

pytestmark = pytest.mark.skipif(harness.NODE is None, reason="node required for the ui.js harness")

INTERRUPT_TITLE = "Interrupt active runs?"
INTERRUPT_CONFIRM = "Interrupt and update"
NEUTRAL_TITLE = "Update the Agent now?"
NEUTRAL_CONFIRM = "Update anyway"
EXTERNAL = " because the WebUI cannot restart this profile's Gateway."
MANAGED_TAIL = (
    "Updating the Agent also restarts the Gateway this profile uses and stops every run "
    "that Gateway is executing, including runs started from other profiles that use the "
    "same Gateway endpoint. The WebUI runs are saved as interrupted. Wait for them to "
    "finish if you can."
)
GATEWAY_ONLY_TAIL = (
    "Updating the Agent also restarts the Gateway this profile uses and stops every run "
    "that Gateway is executing, including runs started from other profiles that use the "
    "same Gateway endpoint."
)

# name, webui runs, gateway runs, gateway managed, target, gateway runs durable,
# expected (offer label, title, message, confirm label)
CASES = [
    ("external-gateway-single", 0, 1, False, "agent", True, (
        "Update anyway (interrupts nothing)", NEUTRAL_TITLE,
        "1 Gateway run keeps running" + EXTERNAL + " Forcing the update will not stop it.",
        NEUTRAL_CONFIRM)),
    ("external-gateway-plural", 0, 2, False, "agent", True, (
        "Update anyway (interrupts nothing)", NEUTRAL_TITLE,
        "2 Gateway runs keep running" + EXTERNAL + " Forcing the update will not stop them.",
        NEUTRAL_CONFIRM)),
    ("external-mixed-control", 1, 2, False, "agent", True, (
        "Update anyway (interrupts 1 run)", INTERRUPT_TITLE,
        "1 WebUI agent run is still working, and 2 Gateway runs keep running"
        + EXTERNAL[:-1] + ". Only the WebUI runs are saved as interrupted.",
        INTERRUPT_CONFIRM)),
    ("managed-mixed-control", 1, 1, True, "agent", True, (
        "Update anyway (interrupts 2 runs)", INTERRUPT_TITLE,
        "1 WebUI agent run is still working and 1 Gateway run is running. " + MANAGED_TAIL,
        INTERRUPT_CONFIRM)),
    # A managed Gateway with no WebUI run: only Gateway runs are interrupted, so
    # the dialog names them alone (no "0 WebUI ..." and no "saved as interrupted").
    ("managed-gateway-only-single", 0, 1, True, "agent", True, (
        "Update anyway (interrupts 1 run)", INTERRUPT_TITLE,
        "1 Gateway run is still working. " + GATEWAY_ONLY_TAIL + " Wait for it to finish if you can.",
        INTERRUPT_CONFIRM)),
    ("managed-gateway-only-plural", 0, 2, True, "agent", True, (
        "Update anyway (interrupts 2 runs)", INTERRUPT_TITLE,
        "2 Gateway runs are still working. " + GATEWAY_ONLY_TAIL + " Wait for them to finish if you can.",
        INTERRUPT_CONFIRM)),
    ("nondurable-gateway-control", 0, 1, False, "agent", False, (
        "Update anyway (interrupts 1 run)", INTERRUPT_TITLE,
        "1 WebUI agent run is still working. The WebUI runs are saved as interrupted "
        "if you force the update.",
        INTERRUPT_CONFIRM)),
    ("webui-control", 1, 0, False, "webui", True, (
        "Update anyway (interrupts 1 run)", INTERRUPT_TITLE,
        "1 WebUI agent run is still working. Updating now restarts the server and stops it; "
        "it is saved as interrupted. Wait for it to finish if you can.",
        INTERRUPT_CONFIRM)),
]


def _census_refusal(w, g, managed, target, durable, monkeypatch):
    """Register real runs, then call the real blocking census and refusal producer."""
    with cfg.ACTIVE_RUNS_LOCK:
        old_runs = dict(cfg.ACTIVE_RUNS)
        cfg.ACTIVE_RUNS.clear()
    with cfg.STREAMS_LOCK:
        old_streams = dict(cfg.STREAMS)
        cfg.STREAMS.clear()
    monkeypatch.setattr(gateway_chat, "gateway_run_durably_resumable", lambda stream_id: durable)
    monkeypatch.setattr(
        upd,
        "gateway_lifecycle_capability",
        lambda profile=None: {
            "managed": managed,
            "reason": "local_gateway" if managed else "external_gateway",
        },
    )
    try:
        for i in range(w):
            cfg.register_active_run("owned-" + str(i), backend="webui")
        for i in range(g):
            cfg.register_active_run("gateway-" + str(i), backend="gateway")
        snapshot = upd._update_blocker_snapshot(target)
        return upd._restart_blocked_response(target, snapshot)
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.clear()
            cfg.ACTIVE_RUNS.update(old_runs)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.clear()
            cfg.STREAMS.update(old_streams)


def _scenario(target, response, answers):
    return {
        "entry": "apply",
        "target": target,
        "updateData": harness._AGENT_BEHIND if target == "agent" else harness._WEBUI_BEHIND,
        "responses": response if isinstance(response, list) else [response],
        "clickOffer": True,
        "confirmAnswers": answers,
    }


@pytest.mark.parametrize(
    "name,w,g,managed,target,durable,expected", CASES, ids=[row[0] for row in CASES]
)
def test_census_refusal_renders_a_coherent_complete_dialog(
    name, w, g, managed, target, durable, expected, tmp_path, monkeypatch
):
    response = _census_refusal(w, g, managed, target, durable, monkeypatch)
    assert response["restart_blocked"] is True and response["force_available"] is True
    assert response["active_runs"] == w + g
    if target == "agent":
        assert response["gateway_runs"] == (g if durable else 0)
        assert response["webui_owned_runs"] == w + (0 if durable else g)

    out = harness._run(_scenario(target, response, [False]), tmp_path)

    label, title, message, confirm = expected
    assert out["afterRefusal"]["offer"]["label"] == label
    assert out["confirmCalls"] == [
        {"title": title, "message": message, "confirmLabel": confirm,
         # The neutral dialog is not a destructive action; cancel keeps the focus.
         "danger": label != "Update anyway (interrupts nothing)", "focusCancel": True}
    ]
    # Cancelling never re-sends: the one call is the refused update.
    assert len(out["apiCalls"]) == 1
    assert "force_active_runs" not in out["apiCalls"][0]["body"]
    assert out["waitCalls"] == 0
    if target == "agent" and w == 0 and durable:
        rendered = out["confirmCalls"][0]["message"]
        assert "0 WebUI" not in rendered and "saved as interrupted" not in rendered, rendered
    if label == "Update anyway (interrupts nothing)":
        dialog = out["confirmCalls"][0]
        for text in (dialog["title"], dialog["message"], dialog["confirmLabel"]):
            assert "interrupt" not in text.lower(), dialog
        assert not dialog["message"].startswith("0 "), dialog


def test_gateway_only_confirmation_still_resends_with_force(tmp_path):
    """The neutral dialog keeps the explicit confirmation and the forced re-send."""
    refusal = harness._refusal(
        2, target="agent", gateway_restart_managed=False, webui_owned_runs=0, gateway_runs=2
    )
    out = harness._run(_scenario("agent", [refusal, {"ok": True, "target": "agent"}], [True]), tmp_path)
    assert [c["title"] for c in out["confirmCalls"]] == [NEUTRAL_TITLE]
    assert out["apiCalls"] == [
        {"url": "/api/updates/apply", "body": {"target": "agent"}},
        {"url": "/api/updates/apply",
         "body": {"target": "agent", "force_active_runs": True, "gateway_restart_managed": False,
                  "interrupted_runs": 0}},
    ]


_STYLED_HARNESS = harness._HARNESS.replace(
    "label: offer ? offer.textContent : null,",
    "label: offer ? offer.textContent : null, className: offer ? offer.className : null,"
    " style: offer ? Object.assign({}, offer.style) : null,",
)


@pytest.mark.parametrize("w,g,managed,neutral", [(0, 1, False, True), (0, 2, False, True), (1, 1, False, False), (0, 1, True, False)])
def test_neutral_offer_is_not_styled_as_destructive(w, g, managed, neutral, tmp_path):
    """interruptedCount===0: the accent update button, no red inline styles, a non-danger dialog."""
    assert _STYLED_HARNESS != harness._HARNESS
    refusal = harness._refusal(
        w + g, target="agent", gateway_restart_managed=managed, webui_owned_runs=w, gateway_runs=g
    )
    snippet = tmp_path / "ui_snippet.js"
    snippet.write_text(harness._ui_snippet(), encoding="utf-8")
    result = subprocess.run(
        [harness.NODE, "-e", _STYLED_HARNESS, json.dumps(_scenario("agent", refusal, [False])),
         str(harness.I18N_JS), str(snippet)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    out = json.loads(result.stdout.strip().splitlines()[-1])
    offer = out["afterRefusal"]["offer"]
    red = {"background": "var(--error,#e05)", "color": "#fff", "borderColor": "var(--error,#e05)"}
    if neutral:
        assert offer["className"] == "update-btn update-primary"
        assert not set(red) & set(offer["style"]), offer["style"]
    else:
        assert offer["className"] == "update-btn"
        assert {k: offer["style"].get(k) for k in red} == red
    dialog = out["confirmCalls"][0]
    assert (dialog["danger"], dialog["focusCancel"]) == (not neutral, True)


def _run_without_locale(scenario, tmp_path):
    """The same harness with no i18n loaded, so ``t`` is absent and the inline fallbacks render."""
    snippet = tmp_path / "ui_snippet.js"
    snippet.write_text(harness._ui_snippet(), encoding="utf-8")
    no_i18n = tmp_path / "no_i18n.js"
    no_i18n.write_text("", encoding="utf-8")
    result = subprocess.run(
        [harness.NODE, "-e", harness._HARNESS, json.dumps(scenario), str(no_i18n), str(snippet)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"node harness failed: {result.stderr or result.stdout}"
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "w,g,managed",
    [(0, 1, False), (0, 2, False), (1, 1, False), (1, 2, False), (2, 1, True), (0, 1, True), (0, 2, True)],
)
def test_inline_fallbacks_match_the_english_locale(w, g, managed, tmp_path):
    refusal = harness._refusal(
        w + g, target="agent", gateway_restart_managed=managed, webui_owned_runs=w, gateway_runs=g
    )
    scenario = _scenario("agent", refusal, [False])
    with_locale = harness._run(scenario, tmp_path)
    fallback = _run_without_locale(scenario, tmp_path)
    assert fallback["caught"] == []
    assert fallback["afterRefusal"]["offer"]["label"] == with_locale["afterRefusal"]["offer"]["label"]
    assert fallback["confirmCalls"] == with_locale["confirmCalls"]


_LOCALE_PROBE = r"""
const fs = require('fs');
const vm = require('vm');
const ctx = {
  console,
  navigator: { languages: ['en-US'], language: 'en-US' },
  localStorage: { getItem() { return null; }, setItem() {} },
  document: { documentElement: {}, readyState: 'complete', addEventListener() {},
    querySelectorAll() { return []; }, createElement() { return {}; }, getElementById() { return null; } },
  setTimeout() {}, clearTimeout() {},
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const codes = vm.runInContext('Object.keys(LOCALES)', ctx);
const call = (v, ...a) => (v === undefined ? null : String(typeof v === 'function' ? v(...a) : v));
const out = {};
for (const code of codes) {
  const loc = vm.runInContext('LOCALES[' + JSON.stringify(code) + ']', ctx);
  out[code] = {
    title: call(loc.update_force_active_runs_no_runs_title),
    confirm: call(loc.update_force_active_runs_no_runs_confirm),
    message_1: call(loc.update_force_active_runs_no_runs_message, 1),
    message_2: call(loc.update_force_active_runs_no_runs_message, 2),
    interrupt_title: call(loc.update_force_active_runs_title),
    interrupt_confirm: call(loc.update_force_active_runs_confirm),
    gateway_only_1: call(loc.update_force_active_runs_message_agent_gateway_only, 1),
    gateway_only_2: call(loc.update_force_active_runs_message_agent_gateway_only, 2),
  };
}
console.log(JSON.stringify({ codes, locales: out }));
"""


def test_every_locale_has_the_gateway_only_dialog_keys():
    proc = subprocess.run(
        [harness.NODE, "-e", _LOCALE_PROBE, str(harness.I18N_JS)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert len(data["codes"]) == 15
    failures = []
    for code, keys in data["locales"].items():
        missing = [key for key in ("title", "confirm", "message_1", "message_2") if not keys[key]]
        if missing:
            failures.append(f"{code}: missing {missing}")
            continue
        for n in (1, 2):
            message = keys[f"message_{n}"]
            if str(n) not in message or "Gateway" not in message:
                failures.append(f"{code}: message({n}) drops the count or the Gateway: {message!r}")
        for n in (1, 2):
            message = keys[f"gateway_only_{n}"]
            if not message or str(n) not in message or "Gateway" not in message:
                failures.append(f"{code}: gateway-only message({n}) missing or drops the count or the Gateway: {message!r}")
        if keys["title"] == keys["interrupt_title"] or keys["confirm"] == keys["interrupt_confirm"]:
            failures.append(f"{code}: the neutral dialog reuses the interruption title or label")
    assert failures == [], "\n".join(failures)
    en = data["locales"]["en"]
    assert en["gateway_only_1"] == "1 Gateway run is still working. " + GATEWAY_ONLY_TAIL + " Wait for it to finish if you can."
    assert en["gateway_only_2"] == "2 Gateway runs are still working. " + GATEWAY_ONLY_TAIL + " Wait for them to finish if you can."
    assert en["message_1"].startswith("1 Gateway run keeps running")
    assert en["message_2"].startswith("2 Gateway runs keep running")
