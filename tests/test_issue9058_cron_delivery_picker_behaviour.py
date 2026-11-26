"""Browserless behaviour coverage for the per-form cron delivery picker (#9058, #9067).

Runs the real ``_populateCronDeliverOptions`` from ``static/panels.js`` in Node with
stubbed ``$``/``api``/``S``/``document``, so the source-text checks in
``test_issue9058_cron_delivery_plugin_platforms.py`` are backed by behaviour.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for the cron picker harness")

_DRIVER = r"""
const vm = require('vm');
const fs = require('fs');
const scenario = JSON.parse(process.argv[2]);
const fnSource = fs.readFileSync(process.argv[3], 'utf8');

function makeSelect() {
  return {
    disabled: false,
    children: [],
    set innerHTML(html) {
      this.children = [];
      const m = String(html).match(/value="([^"]*)"/);
      if (m) this.children.push({ value: m[1], textContent: 'fallback', selected: false });
    },
    appendChild(o) { this.children.push(o); },
    prepend(o) { this.children.unshift(o); },
    querySelector(q) {
      const m = String(q).match(/^option\[value="(.*)"\]$/);
      return m ? (this.children.find((c) => c.value === m[1]) || null) : null;
    },
  };
}

const original = makeSelect();
let current = original;
const ctx = {
  S: { activeProfile: scenario.profile || 'default' },
  CSS: { escape: (s) => s },
  document: { createElement: () => ({ value: '', textContent: '', selected: false }) },
  calls: 0,
};
ctx.$ = (id) => (id === 'cronFormDeliver' ? current : null);
ctx.api = async (url) => {
  if (url !== '/api/crons/delivery-options') throw new Error('unexpected url ' + url);
  ctx.calls += 1;
  const step = scenario.replies[ctx.calls - 1];
  if (!step) throw new Error('unexpected extra fetch #' + ctx.calls);
  await new Promise((resolve) => setTimeout(resolve, 1));
  if (step.during === 'switch') ctx.S.activeProfile = step.to;
  if (step.during === 'replace') current = makeSelect();
  if (step.fail) throw new Error('HTTP 503');
  return step.reply;
};
vm.createContext(ctx);
vm.runInContext(fnSource + '\nthis.__populate = _populateCronDeliverOptions;', ctx);
Promise.resolve(ctx.__populate(scenario.selected, true)).then(() => {
  const chosen = original.children.find((c) => c.selected) || original.children[0] || null;
  process.stdout.write(JSON.stringify({
    calls: ctx.calls,
    values: original.children.map((c) => c.value),
    selected: chosen ? chosen.value : null,
    disabled: original.disabled,
  }));
});
"""


def _function_source(panels_js: str) -> str:
    start = panels_js.index("async function _populateCronDeliverOptions(")
    end = panels_js.index("\nasync function ", start + 10)
    return panels_js[start:end]


def _run(tmp_path, scenario, panels_js=None):
    src = panels_js if panels_js is not None else (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    fn = tmp_path / "populate.js"
    fn.write_text(_function_source(src), encoding="utf-8")
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(driver), json.dumps(scenario), str(fn)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _platforms(*values):
    return {"platforms": [{"value": v, "label": v.title()} for v in values]}


def test_failed_load_keeps_the_saved_value_selected(tmp_path):
    out = _run(tmp_path, {"selected": "telegram", "replies": [{"fail": True}]})
    assert out == {"calls": 1, "values": ["telegram", "local"], "selected": "telegram", "disabled": False}


def test_same_profile_reply_fetches_once_and_selects_the_saved_value(tmp_path):
    out = _run(tmp_path, {"selected": "telegram", "replies": [{"reply": _platforms("local", "telegram")}]})
    assert out == {"calls": 1, "values": ["local", "telegram"], "selected": "telegram", "disabled": False}


def test_saved_value_missing_from_the_list_is_kept(tmp_path):
    out = _run(tmp_path, {"selected": "zzgone", "replies": [{"reply": _platforms("local")}]})
    assert out == {"calls": 1, "values": ["zzgone", "local"], "selected": "zzgone", "disabled": False}


def test_profile_change_in_flight_refetches_and_fills_from_the_current_profile(tmp_path):
    out = _run(tmp_path, {
        "selected": "local",
        "replies": [
            {"during": "switch", "to": "beta", "reply": _platforms("local", "zzalpha")},
            {"reply": _platforms("local", "zzbeta")},
        ],
    })
    assert out == {"calls": 2, "values": ["local", "zzbeta"], "selected": "local", "disabled": False}


def test_reply_for_a_replaced_form_leaves_it_untouched(tmp_path):
    out = _run(tmp_path, {"selected": "telegram", "replies": [{"during": "replace", "reply": _platforms("local", "telegram")}]})
    assert out == {"calls": 1, "values": [], "selected": None, "disabled": True}
