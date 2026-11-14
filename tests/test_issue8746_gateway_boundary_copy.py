"""#8746: gateway restart boundary qualification and run type counts.

1. Endpoints compartilhados: profiles sem endpoint próprio resolvem o mesmo
   Gateway endpoint, e o restart managed avisa que interrompe runs de outros
   profiles com o mesmo endpoint ("including runs started from other profiles
   that use the same Gateway endpoint") e NUNCA contém "in other profiles are
   outside that restart".
2. Endpoints separados: profile com endpoint próprio resolve URL diferente; a copy
   continua qualificando o endpoint do Gateway e não promete parar runs de outro
   endpoint.
3. Contagens por tipo: webui_owned_runs e gateway_runs no payload da recusa.
4. Zero runs de Gateway: sem "(WebUI and Gateway)" e sem nota de Gateway.
5. Invariante unmanaged: nunca afirma que um run é interrompido e continuado.
6. Capacidade unproven: "The WebUI cannot prove it owns the Gateway".
7. Rótulo do force no frontend: deriva do que realmente interrompe.
8. Locales: 4 chaves em 15 idiomas, assinaturas e interpolações corretas.
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

import api.config as cfg
import api.gateway_chat as gateway_chat
import api.updates as upd
from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
I18N_JS = ROOT / "static" / "i18n.js"
NODE = shutil.which("node")


# ── 1. Endpoints compartilhados ──────────────────────────────────────────────


def test_shared_gateway_endpoints_warn_including_other_profiles(monkeypatch):
    """Profiles without own endpoint share Gateway; refusal warns about shared endpoint."""
    monkeypatch.setattr(upd, "get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": True}
    )
    monkeypatch.setattr(
        gateway_chat, "gateway_run_durably_resumable", lambda stream_id: True
    )

    # Endpoints compartilhados: alpha e beta resolvem par idêntico
    ep_alpha = gateway_chat._gateway_endpoint_for_profile("alpha")
    ep_beta = gateway_chat._gateway_endpoint_for_profile("beta")
    assert ep_alpha == ep_beta

    with cfg.ACTIVE_RUNS_LOCK:
        old_active = dict(cfg.ACTIVE_RUNS)
        cfg.ACTIVE_RUNS.clear()
    with cfg.STREAMS_LOCK:
        old_streams = dict(cfg.STREAMS)
        cfg.STREAMS.clear()

    try:
        cfg.register_active_run("webui-1", backend="legacy", profile="alpha")
        cfg.register_active_run("gw-beta", backend="gateway", profile="beta")

        blocker = upd._update_blocker_snapshot("agent")
        # #8660: the blocking census is one pass with no durability read, so the
        # per-kind split is built by the refusal, not carried by the snapshot.
        assert blocker["active_runs"] == 2
        assert "webui_owned_runs" not in blocker
        assert "gateway_runs" not in blocker

        res = upd._restart_blocked_response("agent", blocker)
        assert res["gateway_restart_managed"] is True
        assert res["webui_owned_runs"] == 1
        assert res["gateway_runs"] == 1
        message = res["message"]

        assert "including runs started from other profiles that use the same Gateway endpoint" in message
        assert "in other profiles are outside that restart" not in message
        assert "outside that restart" not in message
        assert "outside the restart" not in message
        assert "only the WebUI runs are settled as interrupted" in message
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.clear()
            cfg.ACTIVE_RUNS.update(old_active)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.clear()
            cfg.STREAMS.update(old_streams)


# ── 2. Endpoints separados (controle) ─────────────────────────────────────────


def test_separate_gateway_endpoints_qualifies_gateway_this_profile_uses(monkeypatch):
    """When beta configures its own base URL, it resolves differently; copy is endpoint-scoped."""
    monkeypatch.setattr(upd, "get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": True}
    )

    def _custom_endpoint(profile_name):
        if profile_name == "beta":
            return ("http://127.0.0.1:9999", "custom-key")
        return ("http://127.0.0.1:8642", "")

    monkeypatch.setattr(gateway_chat, "_gateway_endpoint_for_profile", _custom_endpoint)
    assert gateway_chat._gateway_endpoint_for_profile("alpha") != gateway_chat._gateway_endpoint_for_profile("beta")

    blocker = {
        "active_runs": 2,
        "active_streams": 2,
        "webui_owned_runs": 1,
        "gateway_runs": 1,
        "restart_blocked": True,
    }
    res = upd._restart_blocked_response("agent", blocker)
    message = res["message"]

    assert (
        "Updating the Agent also restarts the Gateway this profile uses and stops every "
        "run that Gateway is executing, including runs started from other profiles that "
        "use the same Gateway endpoint; only the WebUI runs are settled as interrupted."
    ) in message
    assert "in other profiles are outside that restart" not in message
    assert "outside that restart" not in message
    assert "outside the restart" not in message


# ── 3. Contagens por tipo no snapshot e payload ──────────────────────────────


def test_counts_by_type_in_snapshot_and_payload(monkeypatch):
    """1 WebUI run + 2 Gateway runs (alpha and beta) -> webui_owned_runs=1, gateway_runs=2, active_runs=3."""
    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": True}
    )
    monkeypatch.setattr(
        gateway_chat, "gateway_run_durably_resumable", lambda stream_id: True
    )

    with cfg.ACTIVE_RUNS_LOCK:
        old_active = dict(cfg.ACTIVE_RUNS)
        cfg.ACTIVE_RUNS.clear()
    with cfg.STREAMS_LOCK:
        old_streams = dict(cfg.STREAMS)
        cfg.STREAMS.clear()

    try:
        cfg.register_active_run("webui-1", backend="legacy", profile="alpha")
        cfg.register_active_run("gw-alpha", backend="gateway", profile="alpha")
        cfg.register_active_run("gw-beta", backend="gateway", profile="beta")

        blocker = upd._update_blocker_snapshot("agent")
        assert blocker["active_runs"] == 3
        assert "gateway_runs" not in blocker

        res = upd._restart_blocked_response("agent", blocker)
        assert res["webui_owned_runs"] == 1
        assert res["gateway_runs"] == 2
        assert res["active_runs"] == 3
        assert "3 agent runs are active (WebUI and Gateway)" in res["message"]
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.clear()
            cfg.ACTIVE_RUNS.update(old_active)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.clear()
            cfg.STREAMS.update(old_streams)


# ── 4. Zero runs de Gateway ──────────────────────────────────────────────────


def test_zero_gateway_runs_omits_parentheses_and_gateway_note(monkeypatch):
    """1 WebUI run, 0 Gateway runs -> detail without parentheses, empty gateway note."""
    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": True}
    )

    with cfg.ACTIVE_RUNS_LOCK:
        old_active = dict(cfg.ACTIVE_RUNS)
        cfg.ACTIVE_RUNS.clear()
    with cfg.STREAMS_LOCK:
        old_streams = dict(cfg.STREAMS)
        cfg.STREAMS.clear()

    try:
        cfg.register_active_run("webui-1", backend="legacy", profile="alpha")

        blocker = upd._update_blocker_snapshot("agent")
        assert blocker["active_runs"] == 1
        assert "gateway_runs" not in blocker

        res = upd._restart_blocked_response("agent", blocker)
        assert res["webui_owned_runs"] == 1
        assert res["gateway_runs"] == 0
        assert res["active_runs"] == 1

        assert "1 agent run is active" in res["message"]
        assert "(WebUI and Gateway)" not in res["message"]
        assert "Updating the Agent also restarts the Gateway" not in res["message"]
        assert "stops every run" not in res["message"]
        assert "Gateway runs keep running" not in res["message"]
        assert "interrupt the 1 WebUI run and restart the Gateway this profile uses." in res["message"]
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.clear()
            cfg.ACTIVE_RUNS.update(old_active)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.clear()
            cfg.STREAMS.update(old_streams)


# ── 5. Invariante unmanaged ──────────────────────────────────────────────────


def test_unmanaged_never_claims_same_run_is_both_interrupted_and_kept(monkeypatch):
    """1 WebUI + 2 Gateway, managed: False -> force sentence specifies 1 WebUI interrupted, Gateway runs keep running."""
    monkeypatch.setattr(
        upd, "gateway_lifecycle_capability", lambda profile=None: {"managed": False, "reason": "external_gateway"}
    )

    blocker = {
        "active_runs": 3,
        "active_streams": 3,
        "webui_owned_runs": 1,
        "gateway_runs": 2,
        "restart_blocked": True,
    }
    res = upd._restart_blocked_response("agent", blocker)
    message = res["message"]

    assert "Cannot update agent while 3 agent runs are active (WebUI and Gateway)." in message
    assert (
        "The Gateway this profile uses is managed outside the WebUI, "
        "so updating the Agent does not restart it and its Gateway runs keep running."
    ) in message
    assert (
        "Wait for the response to finish and retry, or force the update to "
        "interrupt the 1 WebUI run; the Gateway runs keep running."
    ) in message

    # Invariant: the 2 Gateway runs are NOT claimed as interrupted
    assert "interrupt the 3" not in message
    assert "interrupt the active run" not in message
    assert "interrupt the 2" not in message


# ── 6. Capacidade unproven ───────────────────────────────────────────────────


def test_unproven_capability_mentions_cannot_prove_ownership(monkeypatch):
    """managed: False, reason: local_gateway_unproven -> copy explains inability to prove ownership."""
    monkeypatch.setattr(
        upd,
        "gateway_lifecycle_capability",
        lambda profile=None: {"managed": False, "reason": "local_gateway_unproven"},
    )

    blocker = {
        "active_runs": 2,
        "active_streams": 2,
        "webui_owned_runs": 1,
        "gateway_runs": 1,
        "restart_blocked": True,
    }
    res = upd._restart_blocked_response("agent", blocker)
    assert res["gateway_restart_managed"] is False
    assert (
        "The WebUI cannot prove it owns the Gateway behind this profile's endpoint, "
        "so it will not restart it and its Gateway runs keep running."
    ) in res["message"]

    # Also test when capability raises an exception
    def _broken(profile=None):
        raise RuntimeError("capability check failed")

    monkeypatch.setattr(upd, "gateway_lifecycle_capability", _broken)
    res_err = upd._restart_blocked_response("agent", blocker)
    assert res_err["gateway_restart_managed"] is False
    assert "The WebUI cannot prove it owns the Gateway" in res_err["message"]


# ── 7. Rótulo do force no frontend (Node harness) ───────────────────────────


_UI_FUNCTIONS = (
    ("function", "_activeRunsUpdateText"),
    ("function", "_renderActiveRunsForceOffer"),
)

_HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');
const scenario = JSON.parse(process.argv[1]);
const i18nSrc = fs.readFileSync(process.argv[2], 'utf8');
const uiSnippet = fs.readFileSync(process.argv[3], 'utf8');

const root = { id: '__root__', children: [], parentNode: null };
function makeEl(tag, id) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(), id: id || '',
    style: {}, dataset: {}, children: [], parentNode: null, disabled: false, onclick: null,
    _text: '',
  };
  Object.defineProperty(el, 'textContent', {
    get() { return el._text + el.children.map((c) => c.textContent).join(''); },
    set(v) {
      for (const c of el.children) c.parentNode = null;
      el.children = [];
      el._text = String(v);
    },
  });
  el.appendChild = (c) => {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = el; el.children.push(c); return c;
  };
  el.removeChild = (c) => { el.children = el.children.filter((x) => x !== c); c.parentNode = null; return c; };
  el.remove = () => { if (el.parentNode) el.parentNode.removeChild(el); };
  return el;
}
function mount(el) { el.parentNode = root; root.children.push(el); return el; }

const errorEl = mount(makeEl('div', 'updateError'));
errorEl.style.display = 'none';

function findById(node, id) {
  for (const c of node.children || []) {
    if (c.id === id) return c;
    const hit = findById(c, id);
    if (hit) return hit;
  }
  return null;
}

const ctx = {
  console,
  document: {
    documentElement: {},
    createElement: (tag) => makeEl(tag),
  },
  $: (id) => findById(root, id),
  showConfirmDialog: async () => false,
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(i18nSrc, ctx);
vm.runInContext(uiSnippet, ctx);

ctx._renderActiveRunsForceOffer(scenario.target, scenario.res, async () => {});
const btn = findById(root, 'btnForceUpdateActiveRuns');
console.log(JSON.stringify({ label: btn ? btn.textContent : null }));
"""


@pytest.mark.skipif(NODE is None, reason="node required for ui.js harness")
def test_force_label_derived_from_interrupted_runs_in_node(tmp_path):
    """Button label derives from what force actually interrupts."""
    ui_src = UI_JS.read_text(encoding="utf-8")
    snippet_content = "\n".join(
        extract_function(ui_src, name, prefix=prefix) for prefix, name in _UI_FUNCTIONS
    )
    snippet_path = tmp_path / "snippet.js"
    snippet_path.write_text(snippet_content, encoding="utf-8")

    def _run_label(payload):
        scenario = {"target": "agent", "res": payload}
        proc = subprocess.run(
            [NODE, "-e", _HARNESS_JS, json.dumps(scenario), str(I18N_JS), str(snippet_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        return out["label"]

    # active_runs: 3, webui_owned_runs: 1, gateway_runs: 2, gateway_restart_managed: false
    # -> only 1 WebUI run is interrupted -> "Update anyway (interrupts 1 run)"
    label_1 = _run_label({
        "active_runs": 3,
        "webui_owned_runs": 1,
        "gateway_runs": 2,
        "gateway_restart_managed": False,
    })
    assert label_1 == "Update anyway (interrupts 1 run)"

    # webui_owned_runs: 0, gateway_runs: 2, gateway_restart_managed: false
    # -> 0 runs interrupted -> "Update anyway (interrupts nothing)"
    label_0 = _run_label({
        "active_runs": 2,
        "webui_owned_runs": 0,
        "gateway_runs": 2,
        "gateway_restart_managed": False,
    })
    assert label_0 == "Update anyway (interrupts nothing)"

    # managed: true -> all 3 runs interrupted -> "Update anyway (interrupts 3 runs)"
    label_managed = _run_label({
        "active_runs": 3,
        "webui_owned_runs": 1,
        "gateway_runs": 2,
        "gateway_restart_managed": True,
    })
    assert label_managed == "Update anyway (interrupts 3 runs)"


# ── 8. Locales (15 idiomas) ──────────────────────────────────────────────────


_LOCALE_PROBE_JS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const ctx = {
  console,
  document: { documentElement: {}, readyState: 'complete', addEventListener() {}, createElement() { return {}; } },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);

const codes = vm.runInContext('Object.keys(LOCALES)', ctx);
const out = {};
for (const code of codes) {
  const loc = vm.runInContext('LOCALES[' + JSON.stringify(code) + ']', ctx);
  out[code] = {
    message_agent_2_3: typeof loc.update_force_active_runs_message_agent === 'function'
      ? loc.update_force_active_runs_message_agent(2, 3) : null,
    message_agent_external_2_3: typeof loc.update_force_active_runs_message_agent_external === 'function'
      ? loc.update_force_active_runs_message_agent_external(2, 3) : null,
    message_webui_only_2: typeof loc.update_force_active_runs_message_agent_webui_only === 'function'
      ? loc.update_force_active_runs_message_agent_webui_only(2) : null,
    no_runs: typeof loc.update_force_active_runs_no_runs === 'function'
      ? loc.update_force_active_runs_no_runs() : null,
  };
}
console.log(JSON.stringify({ codes, locales: out }));
"""


@pytest.mark.skipif(NODE is None, reason="node required for locale probe")
def test_all_fifteen_locales_keys_and_signatures():
    """All 15 locales have the 4 keys, (2, 3) produce both numbers, no_runs returns string."""
    proc = subprocess.run(
        [NODE, "-e", _LOCALE_PROBE_JS, str(I18N_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert len(data["codes"]) == 15

    for code, keys in data["locales"].items():
        # 1. message_agent(2, 3) contains 2 and 3, and both WebUI and Gateway
        msg_agent = keys["message_agent_2_3"]
        assert msg_agent is not None, f"{code}: missing update_force_active_runs_message_agent"
        assert "2" in msg_agent and "3" in msg_agent, f"{code}: missing numbers in message_agent: {msg_agent}"
        assert "WebUI" in msg_agent, f"{code}: missing WebUI in message_agent"
        assert "Gateway" in msg_agent, f"{code}: missing Gateway in message_agent"
        assert "outside that restart" not in msg_agent, f"{code}: contains forbidden text"
        assert "outside the restart" not in msg_agent, f"{code}: contains forbidden text"

        # 2. message_agent_external(2, 3) contains 2 and 3, and both WebUI and Gateway
        msg_ext = keys["message_agent_external_2_3"]
        assert msg_ext is not None, f"{code}: missing update_force_active_runs_message_agent_external"
        assert "2" in msg_ext and "3" in msg_ext, f"{code}: missing numbers in message_agent_external: {msg_ext}"
        assert "WebUI" in msg_ext, f"{code}: missing WebUI in message_agent_external"
        assert "Gateway" in msg_ext, f"{code}: missing Gateway in message_agent_external"

        # 3. message_webui_only(2) contains 2, mentions WebUI, does NOT claim Gateway is running
        msg_wo = keys["message_webui_only_2"]
        assert msg_wo is not None, f"{code}: missing update_force_active_runs_message_agent_webui_only"
        assert "2" in msg_wo, f"{code}: missing number in message_webui_only"
        assert "WebUI" in msg_wo, f"{code}: missing WebUI in message_webui_only"

        # 4. no_runs() returns a non-empty string
        no_runs = keys["no_runs"]
        assert isinstance(no_runs, str) and len(no_runs) > 0, f"{code}: empty no_runs"
