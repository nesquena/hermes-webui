"""#7770: plugin-provided skills are read-only in the Skills panel.

A plugin-registered skill lives in the plugin package, not in the profile's
local ``skills/`` tree. The Skills panel's Edit control would write only
``SKILL.md`` into the local tree, creating an incomplete copy that shadows the
plugin's real skill and drops its linked files; the Delete control could only
ever 404 while the plugin kept serving the skill. Both controls are therefore
hidden for plugin-sourced rows.

The real ``static/panels.js`` is exercised here: the two functions that decide
the header buttons are lifted out of the file and driven in a ``node`` context
with a stubbed DOM, so the assertion is on behaviour rather than on the
presence of a string in the source.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")

# The harness below drives static/panels.js through `node`, so skip the module
# rather than failing with FileNotFoundError on a Python-only checkout.
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_function(source: str, header: str) -> str:
    """Return the full source of the JS function whose declaration is *header*."""
    start = source.index(header)
    depth = 0
    for index in range(source.index("{", start), len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function for header {header!r}")


def _run_panels_harness() -> dict:
    """Drive the real panels.js skill-header code with a stubbed DOM."""
    row_is_plugin = _extract_function(PANELS_JS, "function _skillRowIsPlugin(")
    header_buttons = _extract_function(PANELS_JS, "function _setSkillHeaderButtons(")

    script = textwrap.dedent(
        f"""
        const nodes = {{
          btnEditSkillDetail: {{ style: {{ display: '' }} }},
          btnDeleteSkillDetail: {{ style: {{ display: '' }} }},
          btnCancelSkillDetail: {{ style: {{ display: '' }} }},
          btnSaveSkillDetail: {{ style: {{ display: '' }} }},
          mainSkills: {{ querySelector: () => null }},
        }};
        function $(id) {{ return nodes[id] || null; }}
        let _skillsData = [];
        let _currentSkillIsPlugin = false;
        {row_is_plugin}
        {header_buttons}
        function render(row) {{
          _skillsData = row ? [row] : [];
          _currentSkillIsPlugin = _skillRowIsPlugin(row && row.name);
          _setSkillHeaderButtons('read');
          return {{
            edit: nodes.btnEditSkillDetail.style.display,
            del: nodes.btnDeleteSkillDetail.style.display,
          }};
        }}
        const pluginRow = render({{ name: 'watchplugin:helper', source: 'plugin' }});
        const localRow = render({{ name: 'local-skill' }});
        console.log(JSON.stringify({{ pluginRow, localRow }}));
        """
    )
    completed = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


def test_plugin_row_hides_edit_and_delete():
    """A plugin-sourced skill must not offer Edit or Delete."""
    result = _run_panels_harness()

    assert result["pluginRow"]["edit"] == "none", result
    assert result["pluginRow"]["del"] == "none", result


def test_local_row_keeps_edit_and_delete():
    """A directory skill keeps the Edit and Delete controls."""
    result = _run_panels_harness()

    assert result["localRow"]["edit"] == "", result
    assert result["localRow"]["del"] == "", result
