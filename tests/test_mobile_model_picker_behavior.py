"""Behavioral coverage for the mobile composer model picker.

These tests execute the real positioning helper from ``static/ui.js`` under a
small DOM/viewport harness.  Source-presence assertions alone missed the
Settings-picker selector regression and the titlebar clipping case in PR #6026.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
CSS_PATH = REPO_ROOT / "static" / "style.css"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const input = JSON.parse(process.argv[3]);

function extractFunction(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1;
  i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

const dd = {
  style: Object.assign({}, input.initialStyle || {}),
  scrollHeight: input.menuHeight || 0,
  offsetHeight: input.offsetHeight || 0,
  offsetWidth: input.offsetWidth || 0,
  classList: {contains: name => name === 'open'},
};
const anchor = {
  offsetParent: {},
  getBoundingClientRect: () => input.anchor,
};
const mobileAction = {
  getBoundingClientRect: () => input.anchor,
};
const footer = {
  clientWidth: input.footerWidth || 0,
  getBoundingClientRect: () => input.footer,
};
const titlebar = input.titlebarBottom == null
  ? null
  : {getBoundingClientRect: () => ({bottom: input.titlebarBottom})};

const rafQueue = [];
const positionCalls = [];
global.window = {
  innerWidth: input.innerWidth || 0,
  innerHeight: input.innerHeight || 0,
  visualViewport: input.visualViewport,
  matchMedia: () => ({matches: Boolean(input.phone)}),
};
global.requestAnimationFrame = callback => {
  rafQueue.push(callback);
  return rafQueue.length;
};
global.document = {
  querySelector: selector => {
    if (selector === '.composer-footer') return footer;
    if (selector === '.app-titlebar') return titlebar;
    return null;
  },
};
global.$ = id => id === 'composerModelDropdown'
  ? dd
  : id === 'composerModelChip'
    ? anchor
    : id === 'composerMobileModelAction'
      ? mobileAction
      : id === 'composerMobileConfigPanel'
        ? {classList: {contains: () => false}}
        : null;

if (input.mode === 'schedule') {
  global._positionModelDropdown = () => positionCalls.push('position');
  eval("let _modelDropdownRepositionScheduled=false;" + extractFunction('_repositionOpenModelDropdown'));
  _repositionOpenModelDropdown();
  _repositionOpenModelDropdown();
  const queued = rafQueue.length;
  if (rafQueue.length) rafQueue.shift()();
  process.stdout.write(JSON.stringify({queued, calls: positionCalls.length}));
} else {
  eval(extractFunction('_positionModelDropdown'));
  _positionModelDropdown();
  process.stdout.write(JSON.stringify({style: dd.style}));
}
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("mobile_model_picker_driver") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _run(driver_path, case):
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH), json.dumps(case)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


def test_mobile_positioning_respects_visual_viewport_offset_and_titlebar(driver_path):
    result = _run(driver_path, {
        "phone": True,
        "innerWidth": 360,
        "innerHeight": 732,
        "visualViewport": {"width": 360, "height": 300, "offsetLeft": 40, "offsetTop": 20},
        "titlebarBottom": 72,
        "anchor": {"left": 30, "top": 250, "bottom": 270},
        "footer": {"left": 0},
        "footerWidth": 360,
        "menuHeight": 220,
        "offsetHeight": 180,
    })

    assert result["style"]["left"] == "48px"
    assert result["style"]["maxHeight"] == "156px"
    assert result["style"]["top"] == "88px"


def test_mobile_positioning_falls_back_without_visual_viewport(driver_path):
    result = _run(driver_path, {
        "phone": True,
        "innerWidth": 375,
        "innerHeight": 700,
        "visualViewport": None,
        "titlebarBottom": 60,
        "anchor": {"left": 50, "top": 100, "bottom": 130},
        "footer": {"left": 0},
        "footerWidth": 375,
        "menuHeight": 80,
        "offsetHeight": 80,
    })

    assert result["style"]["left"] == "8px"
    assert result["style"]["top"] == "136px"
    assert result["style"]["maxHeight"] == "556px"


def test_desktop_positioning_clears_mobile_inline_geometry(driver_path):
    result = _run(driver_path, {
        "phone": False,
        "innerWidth": 1440,
        "innerHeight": 900,
        "visualViewport": None,
        "anchor": {"left": 50, "top": 100, "bottom": 130},
        "footer": {"left": 20},
        "footerWidth": 300,
        "menuHeight": 80,
        "offsetHeight": 80,
        "offsetWidth": 120,
        "initialStyle": {
            "top": "136px",
            "bottom": "auto",
            "width": "359px",
            "maxWidth": "359px",
            "maxHeight": "554px",
        },
    })

    assert result["style"] == {
        "top": "",
        "bottom": "",
        "width": "",
        "maxWidth": "",
        "maxHeight": "",
        "left": "30px",
    }


def test_visual_viewport_reposition_events_are_coalesced(driver_path):
    result = _run(driver_path, {"mode": "schedule"})
    assert result == {"queued": 1, "calls": 1}


def test_mobile_fixed_rule_targets_composer_picker_only():
    css = CSS_PATH.read_text(encoding="utf-8")
    assert css.count("#composerModelDropdown{position:fixed;") >= 2
    assert ".model-dropdown{position:fixed;" not in css

    base = re.search(
        r"\.model-dropdown\{display:none;position:absolute;",
        css,
    )
    assert base, "the shared model-dropdown base rule must remain absolute"
    assert "#settingsModelDropdown" not in css.split(base.group(0), 1)[0].split("@media", 1)[-1]

    settings = re.search(r"\.settings-model-dropdown\{(?P<body>[^}]*)\}", css)
    assert settings, "the Settings picker rule must remain present"
    assert "position:fixed" not in settings.group("body")
