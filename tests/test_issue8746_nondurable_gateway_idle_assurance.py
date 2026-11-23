"""#8746 GW-IDLE-ASSURANCE-F1: a counted non-durable Gateway run is not an idle Gateway.

The Agent update refusal splits the counted identity set into ``webui_owned_runs`` and
``gateway_runs``. A Gateway-backend identity whose durable handoff mark is absent is
counted as WebUI-owned, so ``gateway_runs`` is ``0`` for it. That number describes what a
managed restart can reattach; it is not evidence that the Gateway is idle -- the Gateway
is still executing that run, the WebUI just cannot prove the restart would resume it.

The WebUI-only confirmation used to render "No Gateway run is active" for that shape,
which is an absolute inactivity assurance for a Gateway that is demonstrably working.
These tests compose the real registration, the real census, the real refusal producer and
the production frontend copy, so the assurance cannot come back silently.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import api.config as cfg
import api.gateway_chat as gateway_chat
import api.run_drain as run_drain
import api.updates as upd
from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
I18N_JS = ROOT / "static" / "i18n.js"
NODE = shutil.which("node")

_UI_FUNCTIONS = (
    ("function", "_activeRunsUpdateText"),
    ("function", "_renderActiveRunsForceOffer"),
)

# Drives the production offer builder, clicks the force button and returns the
# confirmation the user would actually read.
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
  document: { documentElement: {}, createElement: (tag) => makeEl(tag) },
  $: (id) => findById(root, id),
  __dialog: null,
  showConfirmDialog: async (opts) => { ctx.__dialog = opts; return false; },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(i18nSrc, ctx);
vm.runInContext(uiSnippet, ctx);

(async () => {
  ctx._renderActiveRunsForceOffer(scenario.target, scenario.res, async () => {});
  const btn = findById(root, 'btnForceUpdateActiveRuns');
  if (!btn) { console.log(JSON.stringify({ error: 'no force button' })); return; }
  const label = btn.textContent;
  await btn.onclick();
  const dialog = ctx.__dialog || {};
  console.log(JSON.stringify({ label, title: dialog.title || null, message: dialog.message || null }));
})();
"""


def _ui_snippet(tmp_path, name):
    ui_src = UI_JS.read_text(encoding="utf-8")
    content = "\n".join(
        extract_function(ui_src, fn, prefix=prefix) for prefix, fn in _UI_FUNCTIONS
    )
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _confirm_message(tmp_path, payload):
    snippet = _ui_snippet(tmp_path, "snippet_nondurable_gateway.js")
    scenario = {"target": "agent", "res": payload}
    proc = subprocess.run(
        [NODE, "-e", _HARNESS_JS, json.dumps(scenario), str(I18N_JS), str(snippet)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not out.get("error"), out
    assert isinstance(out.get("message"), str) and out["message"], out
    return out


class _Registries:
    """Save/restore the live run registries around a composed scenario."""

    def __enter__(self):
        with cfg.ACTIVE_RUNS_LOCK:
            self.active = dict(cfg.ACTIVE_RUNS)
            cfg.ACTIVE_RUNS.clear()
        with cfg.STREAMS_LOCK:
            self.streams = dict(cfg.STREAMS)
            cfg.STREAMS.clear()
        return self

    def __exit__(self, *exc):
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.clear()
            cfg.ACTIVE_RUNS.update(self.active)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.clear()
            cfg.STREAMS.update(self.streams)
        return False


@pytest.mark.skipif(NODE is None, reason="node required for ui.js harness")
def test_nondurable_gateway_run_is_not_an_idle_gateway(tmp_path, monkeypatch):
    """Counted Gateway identity without a durable mark: the copy may not claim idleness."""
    monkeypatch.setattr(
        gateway_chat, "gateway_run_durably_resumable", lambda stream_id: False
    )

    with _Registries():
        cfg.register_active_run("GND", backend="gateway")

        # The live count is one and the identity really is a Gateway run.
        assert run_drain._gateway_run_durably_resumable("GND") is False
        blocker = upd._update_blocker_snapshot("agent")
        assert blocker["active_runs"] == 1
        identities = blocker["_blocking_run_identities"]
        assert [row["stream_id"] for row in identities] == ["GND"]
        assert [row["backend"] for row in identities] == ["gateway"]
        assert run_drain.classify_run_identities(identities)["gateway"] == []

        for managed in (True, False):
            monkeypatch.setattr(
                upd, "gateway_lifecycle_capability", lambda profile=None, m=managed: {"managed": m}
            )
            res = upd._restart_blocked_response("agent", blocker)

            # Accepted contract untouched: no counted durable/resumable Gateway run.
            assert res["gateway_runs"] == 0
            assert res["webui_owned_runs"] == 1
            assert res["active_runs"] == 1
            assert res["webui_owned_runs"] + res["gateway_runs"] == res["active_runs"]
            assert "_blocking_run_identities" not in res

            out = _confirm_message(tmp_path, res)
            message = out["message"]

            # The absolute inactivity assurance is what the gate charged.
            assert "No Gateway run is active" not in message, message
            assert "No Gateway runs are active" not in message, message
            # WebUI-owned-only wording: the confirmation says nothing about the
            # Gateway being idle, running or restarted.
            assert "Gateway" not in message, message
            assert "WebUI" in message, message
            assert "1" in message, message


@pytest.mark.skipif(NODE is None, reason="node required for ui.js harness")
def test_durable_gateway_run_still_names_the_gateway(tmp_path, monkeypatch):
    """Positive control: a counted durable Gateway run keeps the Gateway wording."""
    monkeypatch.setattr(
        gateway_chat, "gateway_run_durably_resumable", lambda stream_id: True
    )

    with _Registries():
        cfg.register_active_run("GDUR", backend="gateway")

        blocker = upd._update_blocker_snapshot("agent")
        assert blocker["active_runs"] == 1

        for managed in (True, False):
            monkeypatch.setattr(
                upd, "gateway_lifecycle_capability", lambda profile=None, m=managed: {"managed": m}
            )
            res = upd._restart_blocked_response("agent", blocker)
            assert res["gateway_runs"] == 1
            assert res["webui_owned_runs"] == 0
            assert res["active_runs"] == 1
            assert res["webui_owned_runs"] + res["gateway_runs"] == res["active_runs"]

            out = _confirm_message(tmp_path, res)
            assert "Gateway" in out["message"], out["message"]


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
  out[code] = typeof loc.update_force_active_runs_message_agent_webui_only === 'function'
    ? loc.update_force_active_runs_message_agent_webui_only(2)
    : null;
}
console.log(JSON.stringify({ codes, messages: out }));
"""


@pytest.mark.skipif(NODE is None, reason="node required for locale probe")
def test_webui_only_copy_never_asserts_gateway_state_in_any_locale():
    """The WebUI-only confirmation is scoped to WebUI-owned runs in all 15 locales."""
    proc = subprocess.run(
        [NODE, "-e", _LOCALE_PROBE_JS, str(I18N_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert len(data["codes"]) == 15

    for code, message in data["messages"].items():
        assert isinstance(message, str) and message, f"{code}: missing webui-only copy"
        assert "2" in message, f"{code}: dropped the run count: {message}"
        assert "WebUI" in message, f"{code}: dropped the WebUI scope: {message}"
        assert "Gateway" not in message, (
            f"{code}: the WebUI-only confirmation still speaks about the Gateway: {message}"
        )
