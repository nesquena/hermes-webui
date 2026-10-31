"""Regression coverage for composer quota fit priority (#8051)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "static" / "ui.js"
CSS = ROOT / "static" / "style.css"
I18N = ROOT / "static" / "i18n.js"
INDEX = ROOT / "static" / "index.html"
NODE = shutil.which("node")


def test_quota_is_the_first_composer_control_removed():
    ui = UI.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")

    start = ui.index("function _fitComposerFooter(){")
    end = ui.index("\nwindow._fitComposerFooter", start)
    fit = ui[start:end]

    assert "cf-no-quota" in fit
    assert fit.index("cf-no-quota") < fit.index("cf-icons")
    assert ".composer-footer.cf-no-quota .provider-quota-chip" in css
    assert ".composer-footer.cf-icons .provider-quota-chip" in css


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_fit_ladder_keeps_labels_when_hiding_quota_is_enough(tmp_path):
    driver = tmp_path / "fit.js"
    driver.write_text(
        r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function extract(name) {
  const start = src.indexOf('function ' + name + '(){');
  let i = src.indexOf('{', start) + 1, depth = 1;
  while (depth && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
eval(extract('_fitComposerFooter'));

function run(available, initial) {
  const classes = new Set(initial || []);
  const classList = {
    add(...names) { names.forEach(name => classes.add(name)); },
    remove(...names) { names.forEach(name => classes.delete(name)); },
    toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
    contains(name) { return classes.has(name); },
  };
  const left = {
    clientWidth: available,
    get scrollWidth() {
      if (classes.has('cf-burger')) return 300;
      if (classes.has('cf-icons')) return 430;
      if (classes.has('cf-no-quota')) return 590;
      return 720;
    },
  };
  const footer = {
    classList,
    style: {height: '', visibility: ''},
    querySelector(selector) { return selector === '.composer-left' ? left : null; },
    getBoundingClientRect() { return {height: 48}; },
  };
  global.document = {querySelector() { return footer; }};
  _fitComposerFooter();
  return Array.from(classes).sort();
}

process.stdout.write(JSON.stringify({
  full: run(800),
  quotaOnly: run(650),
  icons: run(500),
  burger: run(350),
  expands: run(800, ['cf-no-quota']),
}));
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(driver), str(UI)], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == {
        "full": [],
        "quotaOnly": ["cf-no-quota"],
        "icons": ["cf-icons"],
        "burger": ["cf-burger", "cf-icons"],
        "expands": [],
    }


def test_phone_panel_omits_the_optional_quota_action():
    css = CSS.read_text(encoding="utf-8")
    assert re.search(
        r"@media\(max-width:640px\)\s*\{[\s\S]*?#composerMobileQuotaAction"
        r"\s*\{\s*display:none!important;\s*\}",
        css,
    )


def test_quota_setting_copy_does_not_claim_a_fixed_viewport_gate():
    copies = re.findall(
        r"settings_desc_quota_chip:\s*(['\"])(.*?)\1,",
        I18N.read_text(encoding="utf-8"),
    )
    assert len(copies) == 15
    assert all("1400" not in text for _, text in copies)
    assert "1400" not in INDEX.read_text(encoding="utf-8").split(
        'data-i18n="settings_desc_quota_chip"', 1
    )[1].split("</div>", 1)[0]
