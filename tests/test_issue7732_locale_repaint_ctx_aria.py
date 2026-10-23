"""#7732 runtime regression: locale repaint must NOT erase the dynamically
owned context aria names.

`static/index.html` gives `#ctxIndicator` and `#composerMobileContextAction` a
*generic* ``data-i18n-aria-label="ctx_label_context_window"``. But their real
accessible name is dynamically owned by ``_syncCtxIndicator()`` /
``_syncMobileCtxDisplay()``, which append the live "42% used · $0.02" metrics.
Before the fix, ``applyLocaleToDOM()`` stamped the generic static label over
the dynamic name, so a ja/zh repaint dropped the percentage/cost metrics.

This test drives the REAL catalog (LOCALES.en/ja/zh) and the REAL
``applyLocaleToDOM`` / ``_syncCtxIndicator`` / ``_syncMobileCtxDisplay``
functions extracted from static/i18n.js and static/ui.js. It asserts that after
a locale repaint both the desktop and mobile context rows KEEP their metrics,
in the repainted locale.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
I18N_JS = REPO_ROOT / "static" / "i18n.js"
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const i18n = fs.readFileSync(process.argv[2], 'utf8');
const ui   = fs.readFileSync(process.argv[3], 'utf8');

function extractBlock(src, marker) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('marker not found: ' + marker);
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

// ---- DOM mock (must exist BEFORE eval(i18n): its boot loadLocale() touches it) ----
function makeEl(attrs) {
  return {
    style: {},
    _attrs: Object.assign({}, attrs || {}),
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return (k in this._attrs) ? this._attrs[k] : null; },
    hasAttribute(k) { return k in this._attrs; },
    removeAttribute(k) { delete this._attrs[k]; },
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, on) {
        const want = on === undefined ? !this._set.has(c) : Boolean(on);
        if (want) this._set.add(c); else this._set.delete(c);
      },
      contains(c) { return this._set.has(c); },
    },
    dataset: {},
    title: '',
    textContent: '',
  };
}

// The two dynamically-owned context controls, each carrying the GENERIC static
// aria attribute exactly as index.html declares it.
const ctxIndicator = makeEl({ 'data-i18n-aria-label': 'ctx_label_context_window' });
const mobileCtx    = makeEl({ 'data-i18n-aria-label': 'ctx_label_context_window' });
ctxIndicator.id = 'ctxIndicator';
mobileCtx.id = 'composerMobileContextAction';

const byId = {
  ctxIndicator: ctxIndicator,
  composerMobileContextAction: mobileCtx,
  ctxIndicatorWrap: makeEl(),
  composerMobileConfigBtn: makeEl(),
  ctxRingValue: makeEl(),
  ctxPercent: makeEl(),
  ctxTooltipUsage: makeEl(),
  ctxTooltipTokens: makeEl(),
  ctxTooltipThreshold: makeEl(),
  ctxTooltipCost: makeEl(),
  ctxTooltipCompress: makeEl(),
  ctxCompressBtn: makeEl(),
  composerMobileContextUsage: makeEl(),
  composerMobileContextTokens: makeEl(),
  composerMobileContextThreshold: makeEl(),
  composerMobileContextCost: makeEl(),
  composerMobileCtxCompressBtn: makeEl(),
  'ctx-arc': makeEl(),
  'ctx-num': makeEl(),
};
byId.ctxIndicatorWrap.style.display = '';

global.$ = id => byId[id] || null;
global.document = {
  documentElement: { lang: 'en' },
  getElementById: id => byId[id] || null,
  createElement: () => makeEl(),
  addEventListener: () => {},
  querySelectorAll: sel => (sel === '[data-i18n-aria-label]' ? [ctxIndicator, mobileCtx] : []),
  querySelector: () => null,
};
global.window = { _botName: 'Hermes' };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.navigator = { languages: ['en'], language: 'en' };

// Global S with the authoritative current usage (the metrics source).
global.S = { lastUsage: JSON.parse(process.argv[4]) };
if (process.argv[6]) global.S.session = JSON.parse(process.argv[6]);
eval('var S = global.S;');

// ---- real i18n: eval the WHOLE file so forward-referenced catalog helpers
// (e.g. _i18nProcessedElapsedEn) are in scope via normal hoisting. Its boot
// call loadLocale() only touches the mocked document/localStorage/navigator. ----
eval(i18n);

// ---- real ui.js pieces: the context sync functions ----
eval(extractBlock(ui, 'function _fmtTokens('));
// `const` does not leak from a direct eval (unlike function declarations), so
// bind the base label value explicitly as a module-level var.
(function () {
  const m = ui.match(/const\s+_MOBILE_CONFIG_BASE_LABEL\s*=\s*(['"])(.*?)\1/);
  if (!m) throw new Error('_MOBILE_CONFIG_BASE_LABEL not found');
  global._MOBILE_CONFIG_BASE_LABEL = m[2];
})();
var _MOBILE_CONFIG_BASE_LABEL = global._MOBILE_CONFIG_BASE_LABEL;
eval(extractBlock(ui, 'function _setCtxCompressButton('));
eval(extractBlock(ui, 'function _syncMobileCtxDisplay('));
// #7732: the shared projection the repaint now delegates to. Extracted
// best-effort so that a build WITHOUT the helper still runs the driver and
// exercises the legacy fallback path — that is what keeps these regressions
// discriminating rather than erroring out on a missing marker.
try { eval(extractBlock(ui, 'function _projectCtxUsageForDisplay(')); } catch (e) {}
eval(extractBlock(ui, 'function _syncCtxIndicator('));

// 1) Establish the dynamic names the way a live usage update does.
_syncCtxIndicator(global.S.lastUsage);
const beforeDesktop = ctxIndicator.getAttribute('aria-label');
const beforeMobile  = mobileCtx.getAttribute('aria-label');

// 2) Locale repaint (settings language change / global repaint).
const lang = process.argv[5] || 'ja';
setLocale(lang);
applyLocaleToDOM();

const afterDesktop = ctxIndicator.getAttribute('aria-label');
const afterMobile  = mobileCtx.getAttribute('aria-label');

// The auto-compress threshold line is the only surface that distinguishes an
// explicit threshold of 0 (line hidden) from a stale positive one (line shown),
// so the driver reports it directly.
const thresholdLine = byId.ctxTooltipThreshold;
const afterThresholdDisplay = thresholdLine ? thresholdLine.style.display : null;
const afterThresholdText = thresholdLine ? thresholdLine.textContent : null;

process.stdout.write(JSON.stringify({
  lang,
  beforeDesktop, afterDesktop,
  beforeMobile, afterMobile,
  afterThresholdDisplay, afterThresholdText,
}));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("ctx_aria_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


USAGE = {
    "last_prompt_tokens": 53760,
    "input_tokens": 60000,
    "output_tokens": 2000,
    "context_length": 128000,
    "estimated_cost": 0.0234,
}


def _repaint(driver_path, lang, usage=USAGE, session=None):
    args = [NODE, driver_path, str(I18N_JS), str(UI_JS), json.dumps(usage), lang]
    if session is not None:
        args.append(json.dumps(session))
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=40,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.parametrize("lang", ["ja", "zh", "en"])
def test_locale_repaint_preserves_dynamic_context_aria_metrics(driver_path, lang):
    out = _repaint(driver_path, lang)

    # The dynamic names were established BEFORE repaint (non-generic, has metrics).
    assert out["beforeDesktop"] != "Context window", out
    assert "% used" in out["beforeDesktop"], out

    # After repaint the desktop row STILL carries the metrics, not the bare label.
    assert out["afterDesktop"] == out["beforeDesktop"], out
    assert "% used" in out["afterDesktop"], out
    assert "$" in out["afterDesktop"], out

    # Mobile twin keeps its dynamic label too.
    assert out["afterMobile"] == out["beforeMobile"], out
    assert "% used" in out["afterMobile"], out


def test_same_locale_repaint_also_preserves_metrics(driver_path):
    # A same-locale repaint (en -> en) must not drop metrics either.
    out = _repaint(driver_path, "en")
    assert out["afterDesktop"] == out["beforeDesktop"], out
    assert "% used" in out["afterDesktop"], out


def test_repaint_uses_current_session_context_limit(driver_path):
    # A model change refreshes S.session.context_length without touching
    # S.lastUsage (static/sessions.js `_resolveSessionModelForDisplaySoon`).
    # The repaint must redraw the percentage from the session's limit, not the
    # stale one still sitting in the usage object.
    out = _repaint(driver_path, "ja", session={"context_length": 600000})
    assert "42%" in out["beforeDesktop"], out
    assert "42%" not in out["afterDesktop"], out
    assert "9%" in out["afterDesktop"], out
    assert "9%" in out["afterMobile"], out


def test_repaint_cold_load_projects_persisted_session_fields(driver_path):
    # Finding 1 — cold load. `Session.compact()` (api/models.py) returns
    # INDIVIDUAL usage fields and never a `last_usage` object, so
    # `static/sessions.js` assigns S.lastUsage = {} on a cold load. The repaint
    # must project those persisted per-field values instead of repainting from
    # an empty usage object and blanking the indicator.
    session = {
        "input_tokens": 60000,
        "output_tokens": 2000,
        "estimated_cost": 0.0234,
        "last_prompt_tokens": 53760,
        "context_length": 128000,
    }
    out = _repaint(driver_path, "ja", usage={}, session=session)
    assert "42%" in out["afterDesktop"], out
    assert "$" in out["afterDesktop"], out
    assert "42%" in out["afterMobile"], out


def test_repaint_keeps_newer_metering_over_stale_session_compression_estimate(driver_path):
    # Finding 3 — freshness. The metering listener (static/messages.js) writes
    # the newest post_compression_context_tokens_estimate into S.lastUsage and
    # never touches S.session, so any stored session copy is STALE. The repaint
    # must not let it clobber the fresher live value.
    usage = {
        "last_prompt_tokens": 53760,
        "context_length": 128000,
        "post_compression_context_tokens_estimate": 60000,
    }
    session = {"post_compression_context_tokens_estimate": 30000}
    out = _repaint(driver_path, "ja", usage=usage, session=session)
    assert "47%" in out["afterDesktop"], out      # 60000 / 128000
    assert "23%" not in out["afterDesktop"], out  # 30000 / 128000 is the stale one
    assert "47%" in out["afterMobile"], out


def test_repaint_honours_explicit_zero_threshold_over_stale_positive(driver_path):
    # Finding 2 — an authoritative threshold of 0 (auto-compress disabled) is a
    # real value, semantically distinct from an absent one. The repaint must
    # apply it instead of leaving the stale positive S.lastUsage threshold up.
    usage = {
        "last_prompt_tokens": 53760,
        "context_length": 128000,
        "threshold_tokens": 60000,
    }
    session = {"threshold_tokens": 0}
    out = _repaint(driver_path, "ja", usage=usage, session=session)
    assert out["afterThresholdDisplay"] == "none", out
    assert not out["afterThresholdText"], out
