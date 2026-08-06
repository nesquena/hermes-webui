"""Runtime tests for browser-local extension settings."""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).parent.parent
EXTENSION_SETTINGS_JS = ROOT / "static" / "extension_settings.js"


def _run_node(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for extension settings runtime tests")
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_extension_settings_runtime_normalizes_persists_resets_and_clears():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const assert = require('assert');
        const store = new Map();
        global.window = {{
          __HERMES_EXTENSION_CONFIG__: {{
            extensions: [{{
              id: 'demo.ext',
              name: 'Demo',
              storage_owned: true,
              settings_schema: [
                {{key: 'flag', type: 'boolean', default: false}},
                {{key: 'mode', type: 'enum', options: ['compact', {{value: 'full', label: 'Full'}}], default: 'compact'}},
                {{key: 'count', type: 'integer', default: 2}},
                {{key: 'secret', type: 'string', sensitive: true, default: 'x'}},
                {{key: 'bad', type: 'enum', options: [{{label: 'missing value'}}]}},
                {{key: 'flag', type: 'boolean', default: true}}
              ]
            }}, {{
              id: 'denied.ext',
              name: 'Denied',
              storage_owned: false,
              settings_schema: [{{key: 'flag', type: 'boolean', default: false}}],
            }}]
          }},
          localStorage: {{
            getItem(key) {{ return store.has(key) ? store.get(key) : null; }},
            setItem(key, value) {{ store.set(key, String(value)); }},
            removeItem(key) {{ store.delete(key); }}
          }}
        }};
        eval(fs.readFileSync({str(EXTENSION_SETTINGS_JS)!r}, 'utf8'));

        const settings = window.HermesExtensionSettings.settingsForExtension('demo.ext');
        assert.deepStrictEqual(settings.schema.map(field => field.key), ['flag', 'mode', 'count']);
        assert.deepStrictEqual(settings.values, {{flag: false, mode: 'compact', count: 2}});
        assert.strictEqual(window.hermesExt.settings.forExtension('demo.ext').get('mode'), 'compact');
        assert.deepStrictEqual(settings.setAll({{flag: true, mode: 'compact', count: 2}}).values, {{flag: true, mode: 'compact', count: 2}});
        assert.deepStrictEqual(JSON.parse(store.get('hermes.ext.settings.demo.ext')), {{flag: true}});
        store.set('hermes.ext.settings.demo.ext', JSON.stringify({{flag: false, unknown: 'kept', bad: 'x'}}));
        assert.deepStrictEqual(settings.values, {{flag: false, mode: 'compact', count: 2}});
        assert.deepStrictEqual(settings.overrides, {{}});
        assert.strictEqual(store.has('hermes.ext.settings.demo.ext'), false);
        store.set('hermes.ext.settings.demo.ext', 'not-json');
        assert.deepStrictEqual(settings.values, {{flag: false, mode: 'compact', count: 2}});
        assert.deepStrictEqual(settings.overrides, {{}});
        assert.strictEqual(store.has('hermes.ext.settings.demo.ext'), false);
        store.set('hermes.ext.settings.demo.ext', JSON.stringify(['bad']));
        assert.deepStrictEqual(settings.values, {{flag: false, mode: 'compact', count: 2}});
        assert.deepStrictEqual(settings.overrides, {{}});
        assert.strictEqual(store.has('hermes.ext.settings.demo.ext'), false);
        assert.strictEqual(settings.set('mode', 'invalid').ok, false);
        assert.deepStrictEqual(settings.reset(), {{flag: false, mode: 'compact', count: 2}});
        assert.strictEqual(store.has('hermes.ext.settings.demo.ext'), false);

        const storage = window.HermesExtensionSettings.storageForExtension('demo.ext');
        assert.strictEqual(window.hermesExt.storage.forExtension('demo.ext').set('note', 'local'), true);
        assert.strictEqual(storage.get('note'), 'local');
        assert.strictEqual(store.has('hermes.ext.storage.demo.ext'), true);
        assert.strictEqual(store.has('hermes.ext.settings.demo.ext'), false);
        assert.strictEqual(storage.clear(), true);
        assert.strictEqual(store.has('hermes.ext.storage.demo.ext'), false);

        const deniedSettings = window.HermesExtensionSettings.settingsForExtension('denied.ext');
        assert.strictEqual(deniedSettings.setAll({{flag: true}}).ok, false);
        assert.strictEqual(store.has('hermes.ext.settings.denied.ext'), false);

        const deniedStorage = window.HermesExtensionSettings.storageForExtension('denied.ext');
        assert.strictEqual(deniedStorage.set('note', 'blocked'), false);
        assert.strictEqual(store.has('hermes.ext.storage.denied.ext'), false);

        window.HermesExtensionSettings.primeFromStatus({{
          extensions: [{{
            id: 'unknown.ext',
            name: 'Unknown',
            storage_owned: true,
            settings_schema: [{{key: 'flag', type: 'boolean', default: false}}],
          }}]
        }});

        const unknownSettings = window.HermesExtensionSettings.settingsForExtension('unknown.ext');
        assert.strictEqual(unknownSettings.setAll({{flag: true}}).ok, false);
        assert.strictEqual(store.has('hermes.ext.settings.unknown.ext'), false);

        window.HermesExtensionSettings.primeFromStatus({{
          extensions: [{{
            id: 'demo.ext',
            name: 'Demo',
            storage_owned: true,
            settings_schema: [{{key: 'evil', type: 'string', default: ''}}],
          }}, {{
            id: 'denied.ext',
            name: 'Denied',
            storage_owned: false,
            settings_schema: [{{key: 'flag', type: 'boolean', default: false}}],
          }}]
        }});

        const reprobe = window.HermesExtensionSettings.settingsForExtension('demo.ext');
        assert.deepStrictEqual(reprobe.schema.map(field => field.key), ['flag', 'mode', 'count']);
        assert.strictEqual(reprobe.set('evil', 'owned').ok, true);
        assert.strictEqual(reprobe.get('evil'), undefined);
        assert.strictEqual(store.has('hermes.ext.settings.demo.ext'), false);
        """
    )
    _run_node(script)
