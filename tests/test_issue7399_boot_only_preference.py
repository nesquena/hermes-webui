"""Regression tests for #7399 (re-review at 37d366bcf208, inline 4661287885).

The picker catalog hydration passes ``preferProfileDefaultOnFreshBoot:true``
from EVERY starter. ``_reconcileModelDropdownSelection`` then treats a plain
picker reopen (no session loaded, boot already settled) as a fresh boot and
re-applies the profile/server default, discarding the model the user currently
has selected in the composer while the saved empty-composer override still
holds that other choice.

Contract under test: the fresh-boot preference belongs to the BOOT caller only.
The picker-open starter must start a catalog hydration without it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const BOOT = fs.readFileSync(process.argv[2], 'utf8');

function extractConst(name) {
  const re = new RegExp('const\\s+' + name + '\\s*=[\\s\\S]*?=>\\s*\\{');
  const m = BOOT.search(re);
  if (m < 0) throw new Error('const ' + name + ' not found');
  const start = m;
  const arrowRel = BOOT.slice(m).search(/=>\s*\{/);
  if (arrowRel < 0) throw new Error(name + ': arrow body not found');
  let i = m + arrowRel + 2;
  while (BOOT[i] !== '{') i++;
  let depth = 0;
  let quote = null;
  for (; i < BOOT.length; i++) {
    const ch = BOOT[i];
    if (quote) {
      if (ch === '\\') { i++; continue; }
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') { quote = ch; continue; }
    if (ch === '/' && BOOT[i + 1] === '/') {
      const nl = BOOT.indexOf('\n', i);
      i = nl < 0 ? BOOT.length : nl;
      continue;
    }
    if (ch === '/' && BOOT[i + 1] === '*') {
      const end = BOOT.indexOf('*/', i);
      i = end < 0 ? BOOT.length : end + 1;
      continue;
    }
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) { i++; break; }
    }
  }
  let end = i;
  while (end < BOOT.length && /[\s;]/.test(BOOT[end])) end++;
  if (BOOT[end - 1] === ';') return BOOT.slice(start, end);
  return BOOT.slice(start, end) + ';';
}

const FRAGS = [
  '_catalogHydrationProfile',
  '_trackModelCatalogHydration',
  '_hydrateModelDropdown',
  '_startModelDropdown',
  '_startBootModelDropdown',
].map(extractConst).join('\n');

const calls = [];
globalThis.S = {activeProfile: 'default', session: null};
globalThis.window = {_modelDropdownReady: null, _defaultModel: null, _activeProvider: null};
globalThis.localStorage = {getItem: function () { return null; }, removeItem: function () {}};
globalThis.$ = function () { return null; };
globalThis._redirectBootModelDropdownIfUnauth = function () { return false; };
globalThis._readPersistedModelState = function () { return null; };
globalThis.syncModelChip = function () {};
globalThis.syncTopbar = function () {};
globalThis.syncReasoningChip = function () {};
globalThis.populateModelDropdown = function (opts) {
  calls.push(opts || {});
  return Promise.resolve();
};

const factory = new Function(
  FRAGS + '\nreturn {startOpen:_startModelDropdown,startBoot:_startBootModelDropdown};'
);
const h = factory();

function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }

(async function () {
  await h.startBoot();
  await tick();
  await tick();
  await h.startOpen();
  await tick();
  await tick();
  const boot = calls[0] || null;
  const open = calls[1] || null;
  process.stdout.write(JSON.stringify({
    callCount: calls.length,
    bootPrefersDefault: !!(boot && boot.preferProfileDefaultOnFreshBoot),
    bootHasRedirect: !!(boot && typeof boot.redirectIfUnauth === 'function'),
    openOptsPresent: !!open,
    openPrefersDefault: !!(open && open.preferProfileDefaultOnFreshBoot),
  }));
})().catch(function (err) {
  console.error(err && err.stack || err);
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("issue7399_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _run(driver_path):
    result = subprocess.run(
        [NODE, driver_path, str(BOOT_JS_PATH)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "node driver failed:\nSTDOUT=%s\nSTDERR=%s" % (result.stdout, result.stderr)
        )
    return json.loads(result.stdout)


def test_boot_hydration_requests_profile_default_precedence(driver_path):
    out = _run(driver_path)
    assert out["bootPrefersDefault"] is True, (
        "the boot path must still ask for the profile/server default on fresh boot"
    )
    assert out["bootHasRedirect"] is True, "the boot path must still pass its 401 redirect"


def test_picker_open_hydration_does_not_inherit_the_boot_preference(driver_path):
    out = _run(driver_path)
    # Control: the reopen really started a second hydration, so the assertion
    # below measures the option and not a deduped no-op.
    assert out["callCount"] == 2, "picker reopen did not start its own hydration: %r" % (out,)
    assert out["openOptsPresent"] is True
    assert out["openPrefersDefault"] is False, (
        "an ordinary picker reopen must not re-apply the fresh-boot default "
        "over the model the user has selected"
    )
