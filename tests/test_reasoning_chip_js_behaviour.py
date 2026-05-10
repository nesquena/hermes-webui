"""Behavioural tests that drive the actual `_applyReasoningChip()` from
static/ui.js via node, not just a regex over the source.

The static checks in test_reasoning_chip_btw_fixes.py confirm the *shape*
of the function (no `display='none'`, the right toggle call exists, etc.)
but they pass even if a runtime detail is wrong — e.g. if `inactive` were
inverted, or `_normalizeReasoningEffort` mishandled whitespace, or the
label fell through to a wrong value for an unknown input.

This file pins the actual rendered output for every effort state so the
chip's None/Default visibility cannot silently regress.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function makeEl() {
  return {
    style: {},
    classList: {
      _set: new Set(),
      add(c){this._set.add(c)},
      remove(c){this._set.delete(c)},
      toggle(c, on){
        const want = on === undefined ? !this._set.has(c) : Boolean(on);
        if (want) this._set.add(c); else this._set.delete(c);
      },
      contains(c){return this._set.has(c)},
    },
    dataset: {},
    title: '',
    textContent: '',
    querySelectorAll(){return []},
  };
}

const els = {
  composerReasoningWrap: makeEl(),
  composerReasoningLabel: makeEl(),
  composerReasoningChip: makeEl(),
  composerReasoningDropdown: makeEl(),
};
els.composerReasoningWrap.style.display = 'none'; // mirrors the HTML default

global.window = {};
global.document = {
  createElement: () => makeEl(),
  addEventListener: () => {},
  querySelectorAll: () => [],
  querySelector: () => null,
};
global.$ = id => els[id] || null;
global.api = () => ({ then: () => ({ catch: () => {} }), catch: () => {} });

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
eval(extractFunc('_normalizeReasoningEffort'));
eval(extractFunc('_formatReasoningEffortLabel'));
eval(extractFunc('_highlightReasoningOption'));
eval(extractFunc('_applyReasoningChip'));

const input = JSON.parse(process.argv[3]);
_applyReasoningChip(input);
const result = {
  display: els.composerReasoningWrap.style.display,
  label:   els.composerReasoningLabel.textContent,
  inactive: els.composerReasoningChip.classList.contains('inactive'),
  title:   els.composerReasoningChip.title,
};
process.stdout.write(JSON.stringify(result));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("reasoning_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _apply(driver_path, value):
    """Run _applyReasoningChip(value) against the actual ui.js."""
    import json as _json

    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH), _json.dumps(value)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return _json.loads(result.stdout)


# ─────────────────────────────────────────────────────────────────────────────
# The chip MUST stay visible for every effort state (issue #1068).  This used
# to be hidden for !eff and 'none', and the source-regex tests in
# test_reasoning_chip_btw_fixes.py verify the literal `display='none'` is gone
# — but only a behavioural check confirms the wrap actually receives `''`.
# ─────────────────────────────────────────────────────────────────────────────


class TestChipAlwaysVisible:
    def test_empty_string_shows_chip_with_default_label(self, driver_path):
        out = _apply(driver_path, "")
        assert out["display"] == "", f"empty effort must show the chip: {out}"
        assert out["label"] == "Default"
        assert out["inactive"] is True

    def test_null_shows_chip_with_default_label(self, driver_path):
        out = _apply(driver_path, None)
        assert out["display"] == ""
        assert out["label"] == "Default"
        assert out["inactive"] is True

    def test_none_shows_chip_with_none_label(self, driver_path):
        """The bug from #1068 — 'none' must NOT hide the chip."""
        out = _apply(driver_path, "none")
        assert out["display"] == "", (
            f"'none' must show the chip (the regression that started #1068): {out}"
        )
        assert out["label"] == "None"
        assert out["inactive"] is True

    def test_low_shows_chip_active(self, driver_path):
        out = _apply(driver_path, "low")
        assert out["display"] == ""
        assert out["label"] == "low"
        assert out["inactive"] is False

    def test_high_shows_chip_active(self, driver_path):
        out = _apply(driver_path, "high")
        assert out["display"] == ""
        assert out["inactive"] is False


class TestNormalizationEdgeCases:
    """Pin the input-normalisation contract so it can't silently shift."""

    def test_uppercase_normalises(self, driver_path):
        # Even though the API and slash command use lowercase, defensive
        # normalisation matters — copy/paste of an uppercase value or a
        # mis-cased server response shouldn't break the chip.
        out = _apply(driver_path, "NONE")
        assert out["label"] == "None"
        assert out["inactive"] is True

    def test_whitespace_trimmed(self, driver_path):
        out = _apply(driver_path, "  none  ")
        assert out["label"] == "None"
        assert out["inactive"] is True

    def test_unknown_value_falls_through_visible(self, driver_path):
        # Defensive: unknown effort still shows the chip rather than hiding.
        out = _apply(driver_path, "banana")
        assert out["display"] == ""
        assert out["label"] == "banana"
        assert out["inactive"] is False


class TestTitleAttributeAccessibility:
    """The chip's `title` is the hover tooltip and a screen-reader hint —
    confirm it always carries the current state in human-readable form."""

    def test_title_has_default_label_for_unset(self, driver_path):
        out = _apply(driver_path, "")
        assert out["title"] == "Reasoning effort: Default"

    def test_title_has_none_label_for_none(self, driver_path):
        out = _apply(driver_path, "none")
        assert out["title"] == "Reasoning effort: None"

    def test_title_has_active_label_for_high(self, driver_path):
        out = _apply(driver_path, "high")
        assert out["title"] == "Reasoning effort: high"
