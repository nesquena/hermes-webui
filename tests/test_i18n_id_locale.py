import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_locales_with_node():
    if shutil.which("node") is None:
        pytest.skip("node not available")
    script = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync('static/i18n.js', 'utf8') + '\nglobalThis.__LOCALES = LOCALES;';
const ctx = {
  window: {},
  document: {documentElement:{setAttribute(){},style:{}}, body:{classList:{toggle(){}}}, querySelectorAll(){return []}},
  localStorage: {getItem(){return null}, setItem(){}},
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);
const locales = ctx.__LOCALES;
const enKeys = Object.keys(locales.en).sort();
const idKeys = Object.keys(locales.id || {}).sort();
const missing = enKeys.filter(k => !idKeys.includes(k));
console.log(JSON.stringify({
  locales: Object.keys(locales),
  enCount: enKeys.length,
  idCount: idKeys.length,
  idLabel: locales.id && locales.id._label,
  idSpeech: locales.id && locales.id._speech,
  missing,
  sample: locales.id && locales.id.settings_plugins_title,
}));
"""
    proc = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30, check=True)
    import json
    return json.loads(proc.stdout)


def test_indonesian_locale_exists_and_covers_english_keys():
    data = _load_locales_with_node()
    assert "id" in data["locales"]
    assert data["idLabel"] == "Bahasa Indonesia"
    assert data["idSpeech"] == "id-ID"
    assert data["idCount"] == data["enCount"]
    assert data["missing"] == []
    assert data["sample"] in {"Plugin", "Plugin"}


def test_no_todo_translate_markers_left_in_i18n_source():
    src = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    assert "TODO: translate" not in src
