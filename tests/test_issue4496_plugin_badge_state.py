"""Regression coverage for #4496 plugin provider badge state."""

import json
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_disabled_provider_plugin_renders_disabled_badge(tmp_path):
    script = tmp_path / "check_plugin_badge.js"
    script.write_text(
        textwrap.dedent(
            f"""
            const fs = require('fs');
            const assert = require('assert');
            const src = fs.readFileSync({json.dumps(str(REPO_ROOT / "static" / "panels.js"))}, 'utf8');

            function extractFunction(name) {{
              const marker = 'function ' + name;
              const start = src.indexOf(marker);
              if (start < 0) throw new Error('missing function ' + name);
              const brace = src.indexOf('{{', start);
              let depth = 1;
              let i = brace + 1;
              while (depth && i < src.length) {{
                if (src[i] === '{{') depth += 1;
                else if (src[i] === '}}') depth -= 1;
                i += 1;
              }}
              return src.slice(start, i);
            }}

            global.t = (key) => key;
            global.esc = (value) => String(value ?? '');
            global.document = {{
              createElement() {{
                return {{
                  className: '',
                  dataset: {{}},
                  _html: '',
                  set innerHTML(value) {{ this._html = String(value); }},
                  get innerHTML() {{ return this._html; }},
                  querySelector() {{ return null; }},
                }};
              }},
            }};

            eval(extractFunction('_buildPluginCard'));

            const disabled = _buildPluginCard({{
              key: 'memory',
              name: 'Memory',
              activation: 'provider',
              enabled: false,
              hooks: [],
            }}).innerHTML;
            assert(disabled.includes('plugin-card-badge-disabled'), disabled);
            assert(!disabled.includes('plugin-card-badge-provider'), disabled);
            assert(disabled.includes('plugins_disabled'), disabled);

            const enabled = _buildPluginCard({{
              key: 'memory',
              name: 'Memory',
              activation: 'provider',
              enabled: true,
              hooks: [],
            }}).innerHTML;
            assert(enabled.includes('plugin-card-badge-provider'), enabled);
            assert(!enabled.includes('plugin-card-badge-disabled'), enabled);
            assert(enabled.includes('plugins_active_provider'), enabled);
            """
        ),
        encoding="utf-8",
    )

    subprocess.run(["node", str(script)], check=True, cwd=REPO_ROOT)
