"""Regression coverage for #7227 — model picker must refetch /api/models on open.

The composer picker hydrated /api/models once per page load and re-used that
snapshot forever: `window._modelDropdownReady` kept the *settled* boot promise,
so `toggleModelDropdown()` → `_ensureModelDropdownReady()` returned it and
never re-asked the server. Models that only exist in a provider's overflow
bucket (`extra_models`, e.g. OpenRouter `stealth/ox-alpha` added after boot)
searched as "No models found" until a hard refresh.

The fix lives in static/boot.js: the hydration starters track whether the
cached promise has settled. A *pending* hydration is still deduped (boot +
first open join it), but once it has settled the next open of the picker drops
the snapshot and refetches the current catalog.

These tests execute the real starter block extracted from static/boot.js under
node with a stubbed `_hydrateModelDropdown`, asserting the fetch/refetch
behavior directly, plus source-level wiring checks.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is required to execute the model catalog refetch harness",
)


_BLOCK_START = "let _modelCatalogHydrationSettled=false;"
# The three statements right after the starters are part of the block under
# test: they wire the exported hooks exactly as boot.js does at runtime.
_WIRE_END = "window._ensureModelDropdownReady=_startModelDropdown;"


def _extract_starter_block() -> str:
    """Pull the model-dropdown hydration starters out of static/boot.js.

    The block spans `let _modelCatalogHydrationSettled=false;` through the
    `window._ensureModelDropdownReady=_startModelDropdown;` wiring statement,
    and is self-contained apart from `_hydrateModelDropdown` (stubbed by the
    node driver). Keeping the extraction anchored on the settled-flag marker
    means the harness exercises the exact shipped code instead of a copy.
    """
    start = BOOT_JS.index(_BLOCK_START)
    end_marker = _WIRE_END
    end = BOOT_JS.index(end_marker, start) + len(end_marker)
    block = BOOT_JS[start:end]
    # Sanity: the extracted block must be balanced (no stray braces).
    depth = 0
    for ch in block:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    assert depth == 0, "extracted starter block braces unbalanced"
    return block


_DRIVER = r"""
const fs = require('fs');
const scenario = JSON.parse(process.argv[2] || '{}');
const blockSource = fs.readFileSync(process.argv[3], 'utf8');

function makeDeferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

async function runScenario() {
  globalThis.window = globalThis;
  window._modelDropdownReady = null;

  // Stub the catalog hydration (populateModelDropdown + session reconcile in
  // boot.js). Each call creates a NEW deferred hydration, mirroring a real
  // /api/models fetch. The wrapped promise replicates the real
  // _hydrateModelDropdown tail: on failure the window._modelDropdownReady slot
  // is cleared so the next open retries instead of serving a dead promise.
  const hydrations = [];
  const _hydrateModelDropdown = () => {
    const defer = makeDeferred();
    const wrapped = defer.promise.catch((err) => {
      window._modelDropdownReady = null;
      throw err;
    });
    hydrations.push({ defer, promise: wrapped });
    return wrapped;
  };
  // Referenced by _startBootModelDropdown's boot hydration only; unused in the
  // open/refetch path under test (the driver never exercises 401 redirects).
  const _redirectBootModelDropdownIfUnauth = () => false;

  eval(blockSource); // declares the starters and wires window.* hooks

  const boot = window._startBootModelDropdown;
  const ensure = window._ensureModelDropdownReady;
  const trace = [];

  const record = async (label, ret) => {
    // Identify which hydration (1-based) the returned promise belongs to.
    let hyd = 0;
    for (let i = 0; i < hydrations.length; i++) {
      if (hydrations[i].promise === ret) { hyd = i + 1; break; }
    }
    trace.push({ label, hydCount: hydrations.length, returnedHyd: hyd });
    // Flush microtasks so settle-trackers attached by the starters run.
    await Promise.resolve();
    await Promise.resolve();
  };

  for (const op of scenario.ops || []) {
    if (op.settle === true || op.settle === false) {
      const h = hydrations[hydrations.length - 1];
      if (op.settle === true) h.defer.resolve({ ok: true });
      else h.defer.reject(new Error('catalog unavailable'));
      await record(op.label, h.promise);
      continue;
    }
    const ret = op.boot ? boot() : ensure();
    await record(op.label, ret);
  }
  process.stdout.write(JSON.stringify(trace));
}

runScenario().catch((error) => {
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("issue7227-refetch-driver") / "driver.js"
    path.write_text(_DRIVER, encoding="utf-8")
    return str(path)


def _run(driver_path, ops):
    block = _extract_starter_block()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".js", delete=False) as handle:
        handle.write(block)
        block_path = handle.name
    try:
        process = subprocess.run(
            [NODE, driver_path, json.dumps({"ops": ops}), block_path],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        Path(block_path).unlink(missing_ok=True)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip())
    return json.loads(process.stdout)


def test_pending_hydration_dedupes_and_settled_one_refetches(driver_path):
    """#7227 core: opening the picker after boot hydration settled refetches.

    Boot starts hydration #1. While it is pending, a second boot call and an
    `ensure` (picker open) must JOIN it (dedupe — no duplicate /api/models
    fetch, preserving the first-open cache). After hydration #1 settles, the
    next `ensure` must start hydration #2 (fresh catalog), and a later open
    starts #3 — the snapshot is never served forever.
    """
    trace = _run(driver_path, [
        {"label": "boot-start", "boot": True},
        {"label": "boot-again-pending", "boot": True},
        {"label": "open-while-pending", "boot": False},
        {"label": "boot-settles", "settle": True},
        {"label": "open-after-settle", "boot": False},
        {"label": "refetch-settles", "settle": True},
        {"label": "open-again", "boot": False},
    ])
    by_label = {t["label"]: t for t in trace}

    # Pending hydration dedupe: three early calls share hydration #1.
    assert by_label["boot-start"]["hydCount"] == 1
    assert by_label["boot-again-pending"]["hydCount"] == 1
    assert by_label["boot-again-pending"]["returnedHyd"] == 1
    assert by_label["open-while-pending"]["hydCount"] == 1
    assert by_label["open-while-pending"]["returnedHyd"] == 1

    # Once the boot hydration settled, opening the picker must refetch.
    assert by_label["open-after-settle"]["hydCount"] == 2
    assert by_label["open-after-settle"]["returnedHyd"] == 2

    # And every later open keeps refetching (no permanent settled cache).
    assert by_label["open-again"]["hydCount"] == 3
    assert by_label["open-again"]["returnedHyd"] == 3


def test_boot_restore_after_settle_does_not_double_fetch(driver_path):
    """The boot path (saved-session restore) keeps its dedupe semantics.

    `_startBootModelDropdown()` may run again after the initial boot hydration
    settled (session-restore path in the boot IIFE); it must return the settled
    hydration instead of starting another fetch.
    """
    trace = _run(driver_path, [
        {"label": "boot-start", "boot": True},
        {"label": "boot-settles", "settle": True},
        {"label": "boot-restore-after-settle", "boot": True},
    ])
    by_label = {t["label"]: t for t in trace}
    assert by_label["boot-start"]["hydCount"] == 1
    assert by_label["boot-restore-after-settle"]["hydCount"] == 1
    assert by_label["boot-restore-after-settle"]["returnedHyd"] == 1


def test_failed_hydration_is_not_cached(driver_path):
    """A rejected hydration must not stick: the next open retries the fetch."""
    trace = _run(driver_path, [
        {"label": "boot-start", "boot": True},
        {"label": "boot-fails", "settle": False},
        {"label": "open-after-failure", "boot": False},
    ])
    by_label = {t["label"]: t for t in trace}
    assert by_label["boot-start"]["hydCount"] == 1
    assert by_label["open-after-failure"]["hydCount"] == 2
    assert by_label["open-after-failure"]["returnedHyd"] == 2


def test_boot_wires_refetch_hooks_used_by_picker_open():
    """Source wiring: boot.js exports the starters; toggleModelDropdown calls
    the ensure hook on open, and a settled snapshot is no longer returned as-is
    (the settled-flag gate is present in the dedupe guard).
    """
    assert "window._ensureModelDropdownReady=_startModelDropdown;" in BOOT_JS
    assert "window._startBootModelDropdown=_startBootModelDropdown;" in BOOT_JS
    assert "!_modelCatalogHydrationSettled" in BOOT_JS
    assert "window._modelDropdownReady=null;" in BOOT_JS

    # toggleModelDropdown must keep invoking the ensure hook when opened so the
    # refetch actually runs on picker open (ui.js — untouched by this fix).
    toggle_start = UI_JS.index("async function toggleModelDropdown(")
    toggle_body = UI_JS[toggle_start:toggle_start + 1500]
    assert "window._ensureModelDropdownReady" in toggle_body

    # populateModelDropdown re-renders the dropdown when it is open, so a
    # refetch that lands while the picker is open shows the fresh catalog
    # (incl. extra_models) without reopening.
    populate_start = UI_JS.index("async function populateModelDropdown(")
    populate_tail = UI_JS[populate_start:populate_start + 12000]
    assert "dd.classList.contains('open')" in populate_tail
