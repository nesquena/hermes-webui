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
import os
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
    _attrs: {},
    setAttribute(k, v){this._attrs[k] = String(v)},
    getAttribute(k){return this._attrs[k]},
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
var _profileTransitionReasoningContext = null;

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
        capture_output=True, text=True, timeout=30,
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
        assert out["label"] == "Low"
        assert out["inactive"] is False

    def test_high_shows_chip_active(self, driver_path):
        out = _apply(driver_path, "high")
        assert out["display"] == ""
        assert out["label"] == "High"
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
        assert out["label"] == "Banana"
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
        assert out["title"] == "Reasoning effort: High"


# ─────────────────────────────────────────────────────────────────────────────
# supports_thinking_toggle (ZAI GLM-4.5–5.1): the chip must STAY visible when
# supported_efforts is empty but the model still accepts the thinking on/off
# toggle. Without this, the gate regresses GLM-4.5/4.6/5.0/5.1 users who today
# can toggle thinking on/off (#6219 round-2 review). GLM-4.7 (forced thinking)
# passes supports_thinking_toggle:false and the chip hides as intended.
# ─────────────────────────────────────────────────────────────────────────────


_DRIVER_META_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function makeEl() {
    return {
    style: {},
    _attrs: {},
    setAttribute(k, v){this._attrs[k] = String(v)},
    getAttribute(k){return this._attrs[k]},
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
els.composerReasoningWrap.style.display = 'none';

global.window = {};
global.document = {
  createElement: () => makeEl(),
  addEventListener: () => {},
  querySelectorAll: () => [],
  querySelector: () => null,
};
global.$ = id => els[id] || null;
global.api = () => ({ then: () => ({ catch: () => {} }), catch: () => {} });
var _profileTransitionReasoningContext = null;

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
eval(extractFunc('_applyReasoningOptions'));
eval(extractFunc('_applyReasoningChip'));

const input = JSON.parse(process.argv[3]);
_applyReasoningChip(input.effort, input.meta);
const result = {
  display: els.composerReasoningWrap.style.display,
  label:   els.composerReasoningLabel.textContent,
};
process.stdout.write(JSON.stringify(result));
"""


@pytest.fixture(scope="module")
def driver_meta_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("reasoning_meta_driver") / "driver_meta.js"
    p.write_text(_DRIVER_META_SRC, encoding="utf-8")
    return str(p)


def _apply_meta(driver_meta_path, effort, meta):
    """Run _applyReasoningChip(effort, meta) against the actual ui.js."""
    import json as _json
    payload = _json.dumps({"effort": effort, "meta": meta})
    result = subprocess.run(
        [NODE, driver_meta_path, str(UI_JS_PATH), payload],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node meta driver failed: {result.stderr}")
    return _json.loads(result.stdout)


class TestSupportsThinkingToggleVisibility:
    """#6219 round-2: empty effort ladder must NOT hide the chip when the model
    still accepts the thinking on/off toggle (GLM-4.5–5.1 on native zai)."""

    def test_empty_efforts_with_toggle_stays_visible(self, driver_meta_path):
        """GLM-4.6 (thinking-only tier): empty ladder + toggle=True → chip stays."""
        out = _apply_meta(
            driver_meta_path,
            "",
            {"supported_efforts": [], "supports_thinking_toggle": True},
        )
        assert out["display"] == "", (
            "GLM-4.5–5.1 accept thinking:{type:enabled|disabled} per Z.AI docs; "
            f"the chip must stay visible for the On/None control: {out}"
        )

    def test_empty_efforts_forced_thinking_hides_chip(self, driver_meta_path):
        """GLM-4.7 (forced thinking): empty ladder + toggle=False → chip hides."""
        out = _apply_meta(
            driver_meta_path,
            "",
            {"supported_efforts": [], "supports_thinking_toggle": False},
        )
        assert out["display"] == "none", (
            "glm-4.7 forces thinking on — neither toggle nor ladder should be "
            f"offered; chip must hide: {out}"
        )

    def test_effort_ladder_stays_visible_regardless_of_toggle(self, driver_meta_path):
        """GLM-5.2 (full ladder): effort ladder present → chip stays visible
        whether or not the toggle flag is set (effort implies toggle)."""
        out = _apply_meta(
            driver_meta_path,
            "max",
            {"supported_efforts": ["minimal", "low", "medium", "high", "xhigh", "max"],
             "supports_thinking_toggle": True},
        )
        assert out["display"] == ""
        out2 = _apply_meta(
            driver_meta_path,
            "max",
            {"supported_efforts": ["minimal", "low", "medium", "high", "xhigh", "max"],
             "supports_thinking_toggle": False},
        )
        assert out2["display"] == "", (
            "effort ladder alone is sufficient to show the chip regardless of toggle"
        )

    def test_default_toggle_undefined_keeps_prior_behavior(self, driver_meta_path):
        """When supports_thinking_toggle is absent (legacy responses), the chip
        visibility must follow the prior contract: empty efforts → hide."""
        out = _apply_meta(driver_meta_path, "", {"supported_efforts": []})
        # No supports_thinking_toggle in meta → undefined → treated as true →
        # chip stays visible. This is the conservative default: a missing flag
        # must not newly hide the chip for older response shapes.
        assert out["display"] == "", (
            "absent supports_thinking_toggle defaults to visible (prior behavior "
            f"for legacy responses without the field): {out}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Round-3 #6219: the thinking toggle must be TWO-WAY for GLM-4.5–5.1 (thinking
# tier). The dropdown must expose both Default (on = clear override) and None
# (off). The earlier round-2 fix kept the chip visible but the only rendered
# option was "None" — so a user could turn thinking OFF but never back ON.
# These tests drive the actual _applyReasoningOptions against a simulated
# dropdown containing all 8 options from static/index.html (Default/none/
# minimal/low/medium/high/xhigh/max) and pin which are visible per tier.
# ─────────────────────────────────────────────────────────────────────────────


_DRIVER_OPTIONS_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function makeOption(effort) {
    return {
      style: {display: ''},
      dataset: {effort: effort},
      classList: {
        _set: new Set(),
        add(c){this._set.add(c)},
        remove(c){this._set.delete(c)},
        toggle(c, on){ const w = on === undefined ? !this._set.has(c) : Boolean(on); if(w) this._set.add(c); else this._set.delete(c); },
        contains(c){return this._set.has(c)},
      },
      _attrs: {},
      setAttribute(k, v){this._attrs[k] = String(v)},
      getAttribute(k){return this._attrs[k]},
    };
}

function makeEl() {
    return {
      style: {},
      _attrs: {},
      setAttribute(k, v){this._attrs[k] = String(v)},
      getAttribute(k){return this._attrs[k]},
      classList: {
        _set: new Set(),
        add(c){this._set.add(c)}, remove(c){this._set.delete(c)},
        toggle(c, on){ const w = on === undefined ? !this._set.has(c) : Boolean(on); if(w) this._set.add(c); else this._set.delete(c); },
        contains(c){return this._set.has(c)},
      },
      dataset: {},
      title: '', textContent: '',
    };
}

// Options mirror static/index.html's composerReasoningDropdown exactly.
const options = ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map(makeOption);
const optionEls = options;

const els = {
  composerReasoningWrap: makeEl(),
  composerReasoningLabel: makeEl(),
  composerReasoningChip: makeEl(),
  composerReasoningDropdown: { querySelectorAll(sel){ return sel === '.reasoning-option' ? optionEls : []; } },
};
els.composerReasoningWrap.style.display = 'none';

global.window = {};
global.document = {
  createElement: () => makeEl(),
  addEventListener: () => {},
  querySelectorAll: () => [],
  querySelector: () => null,
};
global.$ = id => els[id] || null;
global.api = () => ({ then: () => ({ catch: () => {} }), catch: () => {} });
var _profileTransitionReasoningContext = null;

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
eval(extractFunc('_applyReasoningOptions'));
eval(extractFunc('_applyReasoningChip'));

const input = JSON.parse(process.argv[3]);
_applyReasoningChip(input.effort, input.meta);
const visible = optionEls
  .filter(o => o.style.display !== 'none')
  .map(o => o.dataset.effort === '' ? 'Default' : o.dataset.effort);
process.stdout.write(JSON.stringify({visible: visible}));
"""


@pytest.fixture(scope="module")
def driver_options_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("reasoning_options_driver") / "driver_options.js"
    p.write_text(_DRIVER_OPTIONS_SRC, encoding="utf-8")
    return str(p)


def _visible_options(driver_options_path, effort, meta):
    """Return the list of visible dropdown labels after _applyReasoningChip."""
    import json as _json
    payload = _json.dumps({"effort": effort, "meta": meta})
    result = subprocess.run(
        [NODE, driver_options_path, str(UI_JS_PATH), payload],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node options driver failed: {result.stderr}")
    return _json.loads(result.stdout)["visible"]


class TestTwoStateToggleControl:
    """#6219 round-3: GLM-4.5–5.1 (thinking tier) must have BOTH Default (on)
    and None (off) — the toggle is two-way, not off-only."""

    def test_thinking_tier_offers_both_default_and_none(self, driver_options_path):
        """GLM-4.6: empty ladder + toggle=True → Default + None both visible."""
        visible = _visible_options(
            driver_options_path,
            "",
            {"supported_efforts": [], "supports_thinking_toggle": True},
        )
        assert "Default" in visible, (
            f"Default (re-enable thinking) must be shown for thinking-tier models; "
            f"got {visible}"
        )
        assert "none" in visible, (
            f"None (disable thinking) must be shown; got {visible}"
        )
        # No effort levels for the thinking-only tier.
        for level in ["minimal", "low", "medium", "high", "xhigh", "max"]:
            assert level not in visible, (
                f"thinking-tier must not show effort level {level}; got {visible}"
            )

    def test_effort_tier_offers_default_none_and_ladder(self, driver_options_path):
        """GLM-5.2: full ladder + toggle → Default, None, AND all 6 levels."""
        visible = _visible_options(
            driver_options_path,
            "high",
            {"supported_efforts": ["minimal", "low", "medium", "high", "xhigh", "max"],
             "supports_thinking_toggle": True},
        )
        assert "Default" in visible
        assert "none" in visible
        for level in ["minimal", "low", "medium", "high", "xhigh", "max"]:
            assert level in visible, f"effort tier must show {level}; got {visible}"

    def test_off_then_on_round_trip_options(self, driver_options_path):
        """Simulate the user flow: Default → click None → click Default.
        At every step, BOTH Default and None must remain visible."""
        meta_toggle = {"supported_efforts": [], "supports_thinking_toggle": True}
        # Start: Default selected (effort=''), chip shows Default+None
        v1 = _visible_options(driver_options_path, "", meta_toggle)
        # User clicks None (effort='none')
        v2 = _visible_options(driver_options_path, "none", meta_toggle)
        # User clicks Default again (effort='')
        v3 = _visible_options(driver_options_path, "", meta_toggle)
        for i, v in enumerate([v1, v2, v3], 1):
            assert "Default" in v and "none" in v, (
                f"step {i}: both Default and None must remain visible throughout "
                f"the round trip; got {v}"
            )
