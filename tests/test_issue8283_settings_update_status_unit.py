"""Regression tests for Settings update status repeating unit (#8283)."""

from pathlib import Path
import re
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_settings_updates_available_does_not_repeat_unit_in_all_locales():
    src = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    matches = re.findall(r"settings_updates_available:\s*'([^']*)'", src)
    assert len(matches) == 15

    for val in matches:
        assert "{count}" in val
        # None of the translations should repeat update / aggiornamento / actualizac / etc.
        assert "update(s)" not in val.lower()
        assert "aggiornamento" not in val.lower()
        assert "actualizaci" not in val.lower()
        assert "atualizaç" not in val.lower()
        assert "mise(s)" not in val.lower()
        assert "bản cập nhật" not in val.lower()


def test_settings_status_formatting_no_unit_repeat():
    # Simulate parts = ['WebUI: 2 releases', 'Agent: 1 update']
    parts = ["WebUI: 2 releases", "Agent: 1 update"]
    template = "{count} available"
    formatted = template.replace("{count}", ", ".join(parts))
    assert formatted == "WebUI: 2 releases, Agent: 1 update available"
    assert "releases update(s)" not in formatted
    assert "updates update(s)" not in formatted


def test_real_format_update_target_status_via_node():
    import shutil
    import subprocess
    import json

    if not shutil.which("node"):
        pytest.skip("node not available")

    ui_src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    i18n_src = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

    # Extract _formatUpdateTargetStatus from ui_src
    start = ui_src.find("function _formatUpdateTargetStatus(label,info){")
    assert start != -1
    end = ui_src.find("function _updateTargetArrowNamesTag", start)
    assert end != -1
    fn_code = ui_src[start:end]

    # Extract all settings_updates_available translations
    matches = re.findall(r"settings_updates_available:\s*'([^']*)'", i18n_src)
    assert len(matches) == 15

    js_script = f"""
{fn_code}
const webuiInfo = {{ release_based: true, current_version: 'v0.51.2', latest_version: 'v0.51.4', behind: 2 }};
const agentInfo = {{ release_based: false, branch: 'main', behind: 1 }};

const p1 = _formatUpdateTargetStatus('WebUI', webuiInfo);
const p2 = _formatUpdateTargetStatus('Agent', agentInfo);
const joined = [p1, p2].join(', ');

const templates = {json.dumps(matches)};
for (const tmpl of templates) {{
  const res = tmpl.replace('{{count}}', joined);
  // Ensure no duplicate word
  if (res.includes('releases update') || res.includes('update(s) update(s)')) {{
    throw new Error('Repeated unit found in formatted string: ' + res);
  }}
}}
console.log('OK');
"""

    proc = subprocess.run(["node", "-e", js_script], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "OK"

