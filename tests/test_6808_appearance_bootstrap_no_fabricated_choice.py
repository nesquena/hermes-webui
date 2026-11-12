"""Regression checks for #6808: the boot script must not fabricate a theme choice.

`syncSettings` in static/boot.js deliberately lets the server win on a first
visit, because (its own comment) "empty (new-browser) state is indistinguishable
from a user who chose the defaults". The inline bootstrap in static/index.html
used to defeat that: it resolved a theme with
`localStorage.getItem('hermes-theme')||'dark'` and then wrote the result back
unconditionally, so a brand-new browser stored `hermes-theme=dark` before any
request was made. syncSettings then saw an "explicit" value, ignored the
server's SETTINGS_DEFAULTS, and POSTed the fabricated value back — making the
server-side appearance default unreachable for deployments that change it.

These cases pin the distinction the fix introduces, which nothing covered
before: ABSENT appearance state is not the same as EXPLICIT appearance state.

  - both keys absent  -> no setItem at all (and the dark pre-paint is UNCHANGED)
  - either key present -> both values are normalised and persisted
  - legacy names       -> migration still happens, exactly as before

The third is the reason the guard is on the PAIR rather than per key. The write
is not pointless: it canonicalises legacy names (`solarized` -> dark+poseidon).
With `hermes-theme=solarized` and no `hermes-skin`, a per-key guard would never
write the derived skin and the mapping would be lost on the next load.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "static" / "index.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is required to execute the appearance bootstrap harness",
)


def _bootstrap_script() -> str:
    """The inline appearance bootstrap, lifted out of index.html."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    for m in re.finditer(r"<script>(.*?)</script>", html, re.S):
        body = m.group(1)
        if "hermes-theme" in body and "legacy" in body and "skins" in body:
            return body
    raise AssertionError("appearance bootstrap <script> not found in index.html")


_DRIVER = r"""
const fs = require('fs');
const script = fs.readFileSync(process.argv[2], 'utf8');
const store = JSON.parse(process.argv[3] || '{}');

const writes = [];
globalThis.localStorage = {
  getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem(k, v) { writes.push([k, String(v)]); store[k] = String(v); },
  removeItem(k) { delete store[k]; },
};

const classes = new Set();
globalThis.document = {
  documentElement: {
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
    dataset: {},
  },
  querySelectorAll: () => [],
};
globalThis.window = globalThis;
globalThis.matchMedia = () => ({ matches: false });
globalThis.window.matchMedia = globalThis.matchMedia;

(0, eval)(script);

process.stdout.write(JSON.stringify({
  writes,
  store,
  classes: [...classes],
  skin: globalThis.document.documentElement.dataset.skin || null,
}));
"""


def _run(initial_store: dict) -> dict:
    """Execute the real bootstrap in node against a stubbed localStorage.

    Driver and script go to FILES and are invoked as `node <driver> <script>`.
    Passing them via `node -e ... -- <script>` silently shifts process.argv, so
    the driver eval'd the JSON scenario instead of the bootstrap — and because
    `eval('{}')` is a valid empty block rather than a syntax error, the
    empty-store case passed while executing nothing at all.
    """
    with tempfile.TemporaryDirectory() as td:
        driver = Path(td) / "driver.js"
        script = Path(td) / "bootstrap.js"
        driver.write_text(_DRIVER, encoding="utf-8")
        script.write_text(_bootstrap_script(), encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(driver), str(script), json.dumps(initial_store)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    assert proc.returncode == 0, f"harness failed: {proc.stderr[:400]}"
    out = json.loads(proc.stdout)
    # The harness must have actually run the bootstrap: it always paints.
    assert out["classes"] or out["skin"] or out["writes"], (
        "harness produced no effects at all — it probably executed the wrong "
        "source; check the node argv indices"
    )
    return out


def test_fresh_browser_writes_nothing():
    """The bug: an empty store must not become an 'explicit' user choice."""
    out = _run({})
    assert out["writes"] == [], (
        "a fresh visit must not write appearance keys — those writes are what "
        f"made syncSettings treat the fallback as explicit; got {out['writes']}"
    )
    assert out["store"] == {}


def test_fresh_browser_still_paints_dark():
    """The fix changes persistence only, never the pre-paint result."""
    out = _run({})
    assert "dark" in out["classes"], (
        "the dark pre-paint fallback must be unchanged for an empty store"
    )
    assert out["skin"] is None


@pytest.mark.parametrize(
    "initial,expected_theme,expected_skin",
    [
        ({"hermes-theme": "dark"}, "dark", "default"),
        ({"hermes-skin": "mono"}, "dark", "mono"),
        ({"hermes-theme": "light", "hermes-skin": "mono"}, "light", "mono"),
    ],
)
def test_any_prior_state_still_normalises_and_persists(initial, expected_theme, expected_skin):
    """Either key present is enough to make this an explicit choice."""
    out = _run(dict(initial))
    assert out["store"]["hermes-theme"] == expected_theme
    assert out["store"]["hermes-skin"] == expected_skin
    assert [k for k, _ in out["writes"]] == ["hermes-theme", "hermes-skin"]


@pytest.mark.parametrize(
    "legacy,theme,skin",
    [
        ("solarized", "dark", "poseidon"),
        ("slate", "dark", "slate"),
        ("monokai", "dark", "sisyphus"),
        ("nord", "dark", "slate"),
        ("oled", "dark", "default"),
    ],
)
def test_legacy_theme_migration_survives(legacy, theme, skin):
    """A per-key guard would drop the derived skin here. The pair guard must not.

    Only `hermes-theme` is stored, and the skin is DERIVED from the legacy
    mapping — so the write of `hermes-skin` is the only thing that persists the
    migration.
    """
    out = _run({"hermes-theme": legacy})
    assert out["store"]["hermes-theme"] == theme
    assert out["store"]["hermes-skin"] == skin, (
        f"legacy {legacy!r} must persist its derived skin {skin!r}"
    )


def test_guard_is_on_the_pair_not_per_key():
    """Source assertion, so the intent survives a future refactor of the block."""
    script = _bootstrap_script()
    assert "_hadAppearance" in script
    assert re.search(
        r"_hadAppearance\s*=\s*localStorage\.getItem\('hermes-theme'\)\s*!==\s*null\s*\|\|"
        r"\s*localStorage\.getItem\('hermes-skin'\)\s*!==\s*null",
        script,
    ), "the guard must be true when EITHER appearance key is present"
    assert re.search(r"if\(_hadAppearance\)\{[^}]*setItem\('hermes-theme'", script), (
        "both appearance writes must sit behind the guard"
    )
