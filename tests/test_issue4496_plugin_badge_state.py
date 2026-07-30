"""Regression coverage for #4496 plugin provider badge state."""

import json
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_provider_badge_uses_active_provider_payload_state(tmp_path):
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

            const activeExclusive = _buildPluginCard({{
              key: 'memory',
              name: 'Memory',
              activation: 'exclusive',
              is_active_provider: true,
              enabled: false,
              hooks: [],
            }}).innerHTML;
            assert(activeExclusive.includes('plugin-card-badge-provider'), activeExclusive);
            assert(!activeExclusive.includes('plugin-card-badge-disabled'), activeExclusive);
            assert(activeExclusive.includes('plugins_active_provider'), activeExclusive);

            const legacyFlatKeyExclusive = _buildPluginCard({{
              key: 'noema',
              name: 'Noema',
              activation: 'exclusive',
              enabled: false,
              hooks: [],
            }}).innerHTML;
            assert(legacyFlatKeyExclusive.includes('plugin-card-badge-provider'), legacyFlatKeyExclusive);
            assert(!legacyFlatKeyExclusive.includes('plugin-card-badge-disabled'), legacyFlatKeyExclusive);
            assert(legacyFlatKeyExclusive.includes('plugins_active_provider'), legacyFlatKeyExclusive);

            const inactiveExclusive = _buildPluginCard({{
              key: 'memory',
              name: 'Memory',
              activation: 'exclusive',
              is_active_provider: false,
              enabled: false,
              hooks: [],
            }}).innerHTML;
            assert(inactiveExclusive.includes('plugin-card-badge-disabled'), inactiveExclusive);
            assert(!inactiveExclusive.includes('plugin-card-badge-provider'), inactiveExclusive);
            assert(inactiveExclusive.includes('plugins_disabled'), inactiveExclusive);

            const activeModelProvider = _buildPluginCard({{
              key: 'openrouter',
              name: 'OpenRouter',
              activation: 'provider',
              is_active_provider: true,
              enabled: true,
              hooks: [],
            }}).innerHTML;
            assert(activeModelProvider.includes('plugin-card-badge-provider'), activeModelProvider);
            assert(!activeModelProvider.includes('plugin-card-badge-disabled'), activeModelProvider);
            assert(activeModelProvider.includes('plugins_active_provider'), activeModelProvider);
            """
        ),
        encoding="utf-8",
    )

    subprocess.run(["node", str(script)], check=True, cwd=REPO_ROOT)
