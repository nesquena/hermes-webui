"""Grok / xAI reasoning surface: effort cap + off-switch contract (issue #6437).

Covers the review findings on PR #6497:

* one strict version parse drives every Grok decision (no drifting regexes),
* the ladder cap holds on BOTH xAI credential lanes (`xai`, `xai-oauth`),
* unversioned `grok-4`/`grok4`/`grok-4-fast` are capped, `grok-4.6+` is not,
* lookalikes (`grok-45`, `grok-4.5x`) never masquerade as grok-4.5,
* Grok 4.x cannot disable reasoning, so the "None" option must be hidden in the
  composer and a stored 'none' must coerce to the provider default.

The last one is exercised behaviourally against the real `static/ui.js` through
node (same driver style as test_reasoning_chip_js_behaviour.py) — no source
regexes, the option's actual `style.display` is asserted.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from api import config as cfg

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

CAPPED = ["low", "medium", "high"]
FULL = ["minimal", "low", "medium", "high", "xhigh", "max"]

# Models whose ladder must stop at 'high' (Grok 4.0-4.5, incl. the unversioned
# ids) vs models that keep xhigh/max (Grok 4.6+).
CAPPED_MODELS = [
    "grok-4.5",
    "grok-4.5-latest",
    "x-ai/grok-4.5",
    "@xai:grok-4.5",
    "Grok-4.5",
    "grok-4.5-mini",
    "grok-4-5",
    "grok-4",
    "grok4",
    "grok-4-fast",
    "grok-4-0709",  # release stamp, not minor=709
]
UNCAPPED_MODELS = ["grok-4.6", "grok-4.7", "grok-4.6-fast", "grok-4.6-0712"]
NON_REASONING_GROK = ["grok-3", "grok-3-mini", "grok-beta", "grok-2", "grok-1"]
LOOKALIKES = ["grok-45", "grok45", "grok-4.5x", "grok4x", "notgrok-4.5"]


# ── one parse, one profile: no drifting regexes ─────────────────────────────


def test_profile_and_supports_never_drift():
    """`_candidate_supports_reasoning` must agree with the profile for every id."""
    for model in CAPPED_MODELS + UNCAPPED_MODELS + NON_REASONING_GROK + LOOKALIKES:
        profile = cfg._grok_reasoning_profile(model)
        supports = cfg._candidate_supports_reasoning(model)
        assert (profile is not None) == supports, model
        if profile is None:
            continue
        assert profile["supports"] is supports, model


def test_resolved_ladder_matches_profile_ceiling_on_both_lanes():
    """The cap must follow the model, on `xai` AND the OAuth lane `xai-oauth`.

    `_resolve_provider_alias("xai-oauth")` does not collapse to `xai`, so the
    lane reaches the filter verbatim — a provider check that only listed `xai`
    left the OAuth lane with the full ladder.
    """
    assert cfg._resolve_provider_alias("xai-oauth") != "xai"
    for provider in ("xai", "xai-oauth"):
        for model in CAPPED_MODELS:
            efforts = cfg.resolve_model_reasoning_efforts(model, provider_id=provider)
            assert efforts == CAPPED, f"{model}@{provider} must cap at high: {efforts}"
        for model in UNCAPPED_MODELS:
            efforts = cfg.resolve_model_reasoning_efforts(model, provider_id=provider)
            assert "xhigh" in efforts and "max" in efforts, f"{model}@{provider}: {efforts}"


def test_unversioned_grok4_ids_are_capped():
    """`grok-4` / `grok4` / `grok-4-fast` used to fall through the 4.5 regex."""
    for model in ("grok-4", "grok4", "grok-4-fast"):
        assert cfg._grok_reasoning_profile(model)["max_effort"] == "high", model
        assert "xhigh" not in cfg.resolve_model_reasoning_efforts(
            model, provider_id="xai"
        ), model


def test_release_stamp_is_not_read_as_minor_version():
    """`grok-4-0709` is a snapshot of grok-4, not grok-4.709 (which would be 4.6+)."""
    assert cfg._grok_reasoning_profile("grok-4-0709")["max_effort"] == "high"
    assert cfg._grok_reasoning_profile("grok-4.6-0712")["max_effort"] == "max"


def test_grok_46_and_later_keep_full_ladder():
    assert cfg._grok_reasoning_profile("grok-4.6")["max_effort"] == "max"
    assert cfg.resolve_model_reasoning_efforts("grok-4.6", provider_id="xai") == FULL
    # a future minor keeps the wider ladder rather than being silently capped
    assert cfg._grok_reasoning_profile("grok-5")["max_effort"] == "max"


def test_lookalike_ids_never_get_the_grok_45_ceiling():
    """`grok-45` is not 4.5 (multi-digit major) and `grok-4.5x` is not 4.5.

    The strict parse rejects the attached alphanumerics instead of falling back
    to the bare major, so neither id is capped *as if* it were grok-4.5 — and a
    rejected id exposes no ladder at all rather than a wrong one.
    """
    for model in LOOKALIKES:
        assert cfg._grok_reasoning_profile(model) is None, model
        assert cfg.resolve_model_reasoning_efforts(model, provider_id="xai") == [], model


def test_pre_grok4_models_are_not_reasoning_capable():
    for model in NON_REASONING_GROK:
        assert cfg._candidate_supports_reasoning(model) is False, model
        assert cfg.resolve_model_reasoning_efforts(model, provider_id="xai") == [], model


# ── the off switch: status flag, coercion, and the composer option ──────────


def test_grok_status_reports_no_thinking_toggle():
    """xAI has no disable signal, so the flag that hides "None" must be false."""
    status = cfg.get_reasoning_status(model_id="grok-4.5", provider_id="xai")
    assert status["supports_reasoning_effort"] is True
    assert status["supported_efforts"] == CAPPED
    assert status["supports_thinking_toggle"] is False


def test_grok_oauth_lane_status_reports_no_thinking_toggle():
    status = cfg.get_reasoning_status(model_id="grok-4.5", provider_id="xai-oauth")
    assert status["supported_efforts"] == CAPPED
    assert status["supports_thinking_toggle"] is False


def test_status_keeps_toggle_for_non_grok_reasoning_models(monkeypatch):
    monkeypatch.setattr(
        cfg, "_load_yaml_config_file", lambda *a, **k: {"agent": {"reasoning_effort": "xhigh"}}
    )
    status = cfg.get_reasoning_status(model_id="gpt-5.5", provider_id="openai-codex")
    assert status["supports_thinking_toggle"] is True


def test_stored_none_coerces_to_provider_default_for_grok(monkeypatch):
    """A 'none' saved before the chip stopped offering it must not reach xAI."""
    for provider in ("xai", "xai-oauth"):
        assert cfg.coerce_reasoning_effort_for_model(
            "none", model_id="grok-4.5", provider_id=provider
        ) == "", provider
    # non-grok models keep the real disable signal
    assert cfg.coerce_reasoning_effort_for_model(
        "none", model_id="gpt-5.5", provider_id="openai-codex"
    ) == "none"


def test_status_coerces_stale_none_to_default_for_grok(monkeypatch):
    monkeypatch.setattr(
        cfg, "_load_yaml_config_file", lambda *a, **k: {"agent": {"reasoning_effort": "none"}}
    )
    status = cfg.get_reasoning_status(model_id="grok-4.5", provider_id="xai")
    assert status["reasoning_effort"] == ""


def test_status_coerces_stale_max_down_to_high_for_grok_45(monkeypatch):
    monkeypatch.setattr(
        cfg, "_load_yaml_config_file", lambda *a, **k: {"agent": {"reasoning_effort": "max"}}
    )
    for provider in ("xai", "xai-oauth"):
        status = cfg.get_reasoning_status(model_id="grok-4.5", provider_id=provider)
        assert status["reasoning_effort"] == "high", provider
    # grok-4.6 legitimately keeps 'max'
    status = cfg.get_reasoning_status(model_id="grok-4.6", provider_id="xai")
    assert status["reasoning_effort"] == "max"


# ── composer: the "None" option is hidden when there is no off switch ───────

_OPTIONS = ["", "none", "minimal", "low", "medium", "high", "xhigh", "max"]

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

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

const options = JSON.parse(process.argv[3]).map(function (effort) {
  return { dataset: { effort: effort }, style: { display: 'unset' } };
});

const dropdown = {
  querySelectorAll: function () { return options; },
};

global.window = {};
global.document = {
  createElement: function () { return {}; },
  addEventListener: function () {},
  querySelectorAll: function () { return []; },
  querySelector: function () { return null; },
};
global.$ = function (id) {
  return id === 'composerReasoningDropdown' ? dropdown : null;
};

eval(extractFunc('_applyReasoningOptions'));

const input = JSON.parse(process.argv[4]);
if (Object.prototype.hasOwnProperty.call(input, 'toggle')) {
  _applyReasoningOptions(input.efforts, input.toggle);
} else {
  // legacy call shape: older backends send no toggle flag at all
  _applyReasoningOptions(input.efforts);
}

const out = {};
options.forEach(function (opt) {
  out[opt.dataset.effort === '' ? '(default)' : opt.dataset.effort] = opt.style.display;
});
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("reasoning_options_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
class TestReasoningOptionsVisibility:

    def _run(self, driver_path, efforts, toggle=_OPTIONS):
        payload = {"efforts": efforts}
        if toggle is not _OPTIONS:
            payload["toggle"] = toggle
        result = subprocess.run(
            [NODE, driver_path, str(UI_JS_PATH), json.dumps(_OPTIONS), json.dumps(payload)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"node driver failed: {result.stderr}")
        return json.loads(result.stdout)

    def test_none_hidden_when_model_cannot_disable_reasoning(self, driver_path):
        """grok-4.5: ladder low|medium|high, no off switch → no "None" option."""
        out = self._run(driver_path, CAPPED, toggle=False)
        assert out["none"] == "none", out
        assert out["(default)"] == "", out
        for effort in CAPPED:
            assert out[effort] == "", out
        for hidden in ("minimal", "xhigh", "max"):
            assert out[hidden] == "none", out

    def test_none_shown_when_model_has_a_toggle(self, driver_path):
        out = self._run(driver_path, CAPPED, toggle=True)
        assert out["none"] == "", out

    def test_none_shown_when_backend_sends_no_toggle_flag(self, driver_path):
        out = self._run(driver_path, CAPPED)
        assert out["none"] == "", out

    def test_default_option_always_visible_even_without_ladder(self, driver_path):
        """GLM-4.5-5.1 on native zai: empty ladder + toggle → Default + None only."""
        out = self._run(driver_path, [], toggle=True)
        assert out["(default)"] == "", out
        assert out["none"] == "", out
        assert out["low"] == "none" and out["high"] == "none", out

    def test_grok_46_full_ladder_keeps_none_hidden(self, driver_path):
        out = self._run(driver_path, FULL, toggle=False)
        for effort in FULL:
            assert out[effort] == "", out
        assert out["none"] == "none", out
