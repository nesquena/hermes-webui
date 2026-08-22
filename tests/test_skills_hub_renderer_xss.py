"""Executable DOM regression for Skills Hub action buttons (#6231)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS = REPO_ROOT / "static" / "panels.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start) + 1, depth = 1;
  while (depth && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

class Button {
  constructor(className) { this.className = className; this.listeners = {}; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  click() { if (this.listeners.click) this.listeners.click(); }
}
class Box {
  constructor() { this._html = ''; this.buttons = []; }
  set innerHTML(value) {
    this._html = String(value);
    this.buttons = Array.from(this._html.matchAll(/<button[^>]*class="([^"]*)"[^>]*>/g))
      .map(match => new Button(match[1]));
  }
  get innerHTML() { return this._html; }
  querySelectorAll(selector) {
    const className = selector.slice(1);
    return this.buttons.filter(button => button.className.split(/\s+/).includes(className));
  }
}
const installedBox = new Box();
const resultsBox = new Box();
global.$ = id => id === 'skillsHubInstalledList' ? installedBox : id === 'skillsHubResultsList' ? resultsBox : null;
global.esc = value => String(value == null ? '' : value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.t = key => key;
global.alert = () => { throw new Error('alert executed'); };
const calls = [];
global.scanSkillsHubResult = value => calls.push(['scan', value]);
global.installSkillsHubResult = value => calls.push(['install', value]);
global.updateSkillsHubSkill = value => calls.push(['update', value]);
global.uninstallSkillsHubSkill = value => calls.push(['uninstall', value]);
for (const name of ['_skillsHubTrustBadge', '_skillsHubVerdictBadge', '_skillsHubInstalledIdentifiers',
                    'renderSkillsHubInstalled', 'renderSkillsHubResults']) eval(extractFunc(name));

const payload = ['x', "');", 'alert(1);//'].join('');
global._skillsHubAllowed = true;
global._skillsHubInstalled = [{name: payload, identifier: 'installed/' + payload, trust_level: 'trusted', scan_verdict: 'safe'}];
global._skillsHubResults = [{name: 'result', identifier: payload, description: '', source: 'hub', trust_level: 'community'}];
global._skillsHubScanResults = {};
renderSkillsHubInstalled();
renderSkillsHubResults();
for (const button of installedBox.buttons) button.click();
for (const button of resultsBox.buttons) button.click();
process.stdout.write(JSON.stringify({
  installedHtml: installedBox.innerHTML,
  resultsHtml: resultsBox.innerHTML,
  calls,
  payload,
}));
"""


def test_skills_hub_buttons_do_not_embed_external_values_as_handlers(tmp_path):
    """Real renderer buttons route raw values via listeners without inline JS."""
    assert NODE is not None
    driver = tmp_path / "skills_hub_renderer_driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(PANELS_JS)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    payload = rendered["payload"]

    for html in (rendered["installedHtml"], rendered["resultsHtml"]):
        assert "onclick=" not in html
        assert payload not in html
    assert rendered["calls"] == [
        ["update", payload],
        ["uninstall", payload],
        ["scan", payload],
        ["install", payload],
    ]
