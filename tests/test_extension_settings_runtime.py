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


def test_hermes_ext_register_runtime_identity_and_reload_lifecycle():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const assert = require('assert');
        const store = new Map();
        let reads = 0;
        let writes = 0;
        let removes = 0;
        global.window = {{
          __HERMES_EXTENSION_CONFIG__: {{
            extensions: [{{
              id: 'alpha.ext',
              name: 'Alpha',
              storage_owned: true,
              settings_schema: [
                {{key: 'enabled', type: 'boolean', default: false}},
                {{key: 'mode', type: 'string', default: 'alpha'}}
              ]
            }}, {{
              id: 'beta.ext',
              name: 'Beta',
              storage_owned: true,
              settings_schema: [
                {{key: 'enabled', type: 'boolean', default: true}},
                {{key: 'mode', type: 'string', default: 'beta'}}
              ]
            }}, {{
              id: 'deferred.ext',
              name: 'Deferred',
              storage_owned: true,
              settings_schema: [
                {{key: 'enabled', type: 'boolean', default: false}},
                {{key: 'mode', type: 'string', default: 'deferred'}}
              ]
            }}]
          }},
          localStorage: {{
            getItem(key) {{ reads += 1; return store.has(key) ? store.get(key) : null; }},
            setItem(key, value) {{ writes += 1; store.set(key, String(value)); }},
            removeItem(key) {{ removes += 1; store.delete(key); }}
          }}
        }};
        eval(fs.readFileSync({str(EXTENSION_SETTINGS_JS)!r}, 'utf8'));

        const forgedMetadata = {{
          id: 'forged.ext',
          name: 'Forged',
          storage_owned: true,
          settings_schema: [{{key: 'owned', type: 'boolean', default: false}}]
        }};
        const forgedSettings = window.HermesExtensionSettings.settingsForExtension(
          'forged.ext', forgedMetadata
        );
        const forgedStorage = window.HermesExtensionSettings.storageForExtension(
          'forged.ext', forgedMetadata
        );
        assert.strictEqual(forgedSettings.trusted, false);
        assert.strictEqual(forgedSettings.set('owned', true).ok, false);
        assert.strictEqual(forgedStorage.set('owned', true), false);
        assert.strictEqual(store.has('hermes.ext.settings.forged.ext'), false);
        assert.strictEqual(store.has('hermes.ext.storage.forged.ext'), false);

        const beforeUnknown = {{reads, writes, removes}};
        assert.strictEqual(window.hermesExt.register(''), null);
        assert.strictEqual(window.hermesExt.register('   unknown.ext   '), null);
        assert.strictEqual(window.hermesExt.register(null), null);
        assert.strictEqual(window.hermesExt.register(42), null);
        assert.strictEqual(window.hermesExt.register({{toString() {{ return 'alpha.ext'; }}}}), null);
        assert.strictEqual(window.hermesExt.register(Symbol('alpha.ext')), null);
        assert.deepStrictEqual({{reads, writes, removes}}, beforeUnknown);
        assert.deepStrictEqual([...store.keys()], []);

        const alpha = window.hermesExt.register('  alpha.ext  ');
        assert.ok(alpha);
        assert.strictEqual(alpha.id, 'alpha.ext');
        assert.strictEqual(Object.isFrozen(alpha), true);
        assert.deepStrictEqual(Object.keys(alpha).sort(), ['events', 'id', 'messages', 'settings', 'storage']);
        assert.deepStrictEqual(alpha.settings.values, {{enabled: false, mode: 'alpha'}});
        assert.strictEqual(alpha.settings.set('enabled', true).ok, true);
        assert.strictEqual(alpha.storage.set('note', 'alpha-note'), true);
        assert.deepStrictEqual(JSON.parse(store.get('hermes.ext.settings.alpha.ext')), {{enabled: true}});
        assert.deepStrictEqual(JSON.parse(store.get('hermes.ext.storage.alpha.ext')), {{note: 'alpha-note'}});

        assert.strictEqual(window.hermesExt.register('alpha.ext'), alpha);
        assert.strictEqual(window.hermesExt.register(' alpha.ext '), alpha);

        const beta = window.hermesExt.register('beta.ext');
        assert.ok(beta);
        assert.strictEqual(beta.id, 'beta.ext');
        assert.notStrictEqual(beta, alpha);
        assert.notStrictEqual(beta.settings, alpha.settings);
        assert.notStrictEqual(beta.storage, alpha.storage);
        assert.strictEqual(beta.storage.set('note', 'beta-note'), true);
        assert.strictEqual(beta.settings.set('enabled', false).ok, true);
        assert.strictEqual(alpha.storage.get('note'), 'alpha-note');
        assert.strictEqual(beta.storage.get('note'), 'beta-note');
        assert.strictEqual(alpha.settings.get('enabled'), true);
        assert.strictEqual(beta.settings.get('enabled'), false);

        const legacySettings = window.HermesExtensionSettings.settingsForExtension('alpha.ext');
        const legacyStorage = window.HermesExtensionSettings.storageForExtension('alpha.ext');
        assert.strictEqual(legacySettings.get('enabled'), true);
        assert.strictEqual(legacyStorage.get('note'), 'alpha-note');
        assert.strictEqual(window.hermesExt.settings.forExtension('alpha.ext').get('mode'), 'alpha');
        assert.strictEqual(window.hermesExt.storage.forExtension('beta.ext').get('note'), 'beta-note');

        window.HermesExtensionSettings.primeFromStatus({{
          extensions: [{{
            id: 'beta.ext',
            name: 'Beta after refresh',
            storage_owned: true,
            settings_schema: [{{key: 'different', type: 'string', default: 'late'}}]
          }}, {{
            id: 'late.ext',
            name: 'Late',
            storage_owned: true,
            settings_schema: [{{key: 'enabled', type: 'boolean', default: false}}]
          }}]
        }});

        const deferred = window.hermesExt.register('deferred.ext');
        assert.ok(deferred);
        assert.deepStrictEqual(deferred.settings.values, {{enabled: false, mode: 'deferred'}});
        assert.strictEqual(deferred.settings.trusted, true);
        assert.strictEqual(deferred.storage.set('note', 'deferred-note'), true);
        assert.strictEqual(deferred.storage.get('note'), 'deferred-note');
        assert.strictEqual(window.hermesExt.settings.forExtension('deferred.ext').trusted, false);

        assert.strictEqual(window.hermesExt.register('alpha.ext'), alpha);
        assert.strictEqual(alpha.settings.get('enabled'), true);
        assert.strictEqual(alpha.storage.get('note'), 'alpha-note');
        assert.strictEqual(window.hermesExt.register('late.ext'), null);
        assert.strictEqual(store.has('hermes.ext.settings.late.ext'), false);
        assert.strictEqual(store.has('hermes.ext.storage.late.ext'), false);
        """
    )
    _run_node(script)


def test_hermes_ext_configure_registration_invocation_and_quarantine():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const assert = require('assert');
        const store = new Map();
        const diagnostics = [];
        const changes = [];
        const replacementButton = {{
          dataset: {{extensionConfigureId: 'alpha.ext'}},
          isConnected: true,
          disabled: false,
          hidden: false,
          focusCount: 0,
          getAttribute(name) {{ return name === 'aria-disabled' ? 'false' : null; }},
          closest() {{ return null; }},
          focus() {{ this.focusCount += 1; }},
        }};
        const installedTab = {{
          isConnected: true,
          disabled: false,
          hidden: false,
          focusCount: 0,
          getAttribute() {{ return null; }},
          closest() {{ return null; }},
          focus() {{ this.focusCount += 1; }},
        }};
        global.document = {{
          querySelectorAll(selector) {{
            return selector === '[data-extension-configure-id]' ? [replacementButton] : [];
          }},
          querySelector(selector) {{
            return selector === '[data-extensions-tab="installed"]' ? installedTab : null;
          }},
        }};
        global.window = {{
          __HERMES_EXTENSION_CONFIG__: {{
            extensions: [{{id: 'alpha.ext', name: 'Alpha'}}, {{id: 'beta.ext', name: 'Beta'}}]
          }},
          localStorage: {{
            getItem(key) {{ return store.has(key) ? store.get(key) : null; }},
            setItem(key, value) {{ store.set(key, String(value)); }},
            removeItem(key) {{ store.delete(key); }}
          }}
        }};
        const originalConsoleError = console.error;
        console.error = (...args) => diagnostics.push(args);

        (async () => {{
          eval(fs.readFileSync({str(EXTENSION_SETTINGS_JS)!r}, 'utf8'));
          const runtime = window.HermesExtensionSettings;
          const alpha = window.hermesExt.register('alpha.ext');
          const beta = window.hermesExt.register('beta.ext');
          assert.ok(alpha);
          assert.ok(beta);
          assert.strictEqual(typeof alpha.settings.registerConfigure, 'function');
          assert.strictEqual(
            window.HermesExtensionSettings.settingsForExtension('alpha.ext').registerConfigure,
            undefined,
            'legacy global accessors must not gain registration authority'
          );
          assert.strictEqual(window.hermesExt.register('unknown.ext'), null);

          const stopChanges = runtime._onConfigureChange(change => changes.push(change));
          let alphaCalls = 0;
          let pendingInsideHandler = false;
          const unregisterAlpha = alpha.settings.registerConfigure(({{opener, restoreFocus}}) => {{
            alphaCalls += 1;
            pendingInsideHandler = runtime._configureStateForExtension('alpha.ext').pending;
            assert.strictEqual(opener.extensionId, 'alpha.ext');
            restoreFocus();
            restoreFocus();
          }});
          assert.strictEqual(typeof unregisterAlpha, 'function');
          assert.strictEqual(alpha.settings.registerConfigure(() => {{}}), null);
          assert.deepStrictEqual(runtime._configureStateForExtension('alpha.ext'), {{available: true, pending: false}});
          assert.ok(changes.some(change => change.id === 'alpha.ext' && change.reason === 'registration'));

          const connectedOpener = {{
            extensionId: 'alpha.ext',
            isConnected: true,
            disabled: false,
            hidden: false,
            focusCount: 0,
            getAttribute() {{ return null; }},
            closest() {{ return null; }},
            focus() {{ this.focusCount += 1; }},
          }};
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: connectedOpener}}), true);
          assert.strictEqual(alphaCalls, 1);
          assert.strictEqual(pendingInsideHandler, true);
          assert.strictEqual(connectedOpener.focusCount, 1);
          assert.deepStrictEqual(runtime._configureStateForExtension('alpha.ext'), {{available: true, pending: false}});

          assert.strictEqual(unregisterAlpha(), true);
          assert.strictEqual(unregisterAlpha(), false);
          assert.deepStrictEqual(runtime._configureStateForExtension('alpha.ext'), {{available: false, pending: false}});

          let releasePending;
          const unregisterPending = alpha.settings.registerConfigure(() => new Promise(resolve => {{ releasePending = resolve; }}));
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: connectedOpener}}), true);
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: connectedOpener}}), false);
          assert.strictEqual(runtime._configureStateForExtension('alpha.ext').pending, true);
          releasePending();
          await Promise.resolve();
          await Promise.resolve();
          assert.strictEqual(runtime._configureStateForExtension('alpha.ext').pending, false);
          assert.strictEqual(connectedOpener.focusCount, 2);
          assert.strictEqual(unregisterPending(), true);

          let restoreExplicit;
          const unregisterExplicit = alpha.settings.registerConfigure(({{restoreFocus}}) => {{
            restoreExplicit = restoreFocus;
          }});
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: connectedOpener}}), true);
          assert.strictEqual(runtime._configureStateForExtension('alpha.ext').pending, true);
          assert.strictEqual(restoreExplicit(), true);
          assert.strictEqual(restoreExplicit(), false);
          assert.strictEqual(runtime._configureStateForExtension('alpha.ext').pending, false);
          assert.strictEqual(connectedOpener.focusCount, 3);
          assert.strictEqual(unregisterExplicit(), true);

          const unregisterMissing = alpha.settings.registerConfigure(() => undefined);
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: connectedOpener}}), true);
          assert.strictEqual(runtime._configureStateForExtension('alpha.ext').pending, true);
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: connectedOpener}}), false);
          assert.strictEqual(connectedOpener.focusCount, 3);
          assert.strictEqual(unregisterMissing(), true);

          const reported = [];
          const unregisterThrowing = alpha.settings.registerConfigure(() => {{ throw new Error('sync boom'); }});
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: connectedOpener, onError: error => reported.push(error.message)}}), true);
          assert.deepStrictEqual(reported, ['sync boom']);
          assert.strictEqual(runtime._configureStateForExtension('alpha.ext').pending, false);
          assert.ok(diagnostics.some(args => String(args[0]).includes('alpha.ext') && String(args[0]).includes('Configure handler failed')));
          assert.strictEqual(unregisterThrowing(), true);

          const rejected = [];
          const unregisterRejected = alpha.settings.registerConfigure(() => Promise.reject(new Error('async boom')));
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: connectedOpener, onError: error => rejected.push(error.message)}}), true);
          await Promise.resolve();
          await Promise.resolve();
          assert.deepStrictEqual(rejected, ['async boom']);
          assert.strictEqual(runtime._configureStateForExtension('alpha.ext').pending, false);
          assert.strictEqual(unregisterRejected(), true);

          const assimilated = [];
          const unregisterBadThenable = alpha.settings.registerConfigure(() => Object.defineProperty({{}}, 'then', {{
            get() {{ throw new Error('then getter boom'); }}
          }}));
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: connectedOpener, onError: error => assimilated.push(error.message)}}), true);
          assert.deepStrictEqual(assimilated, ['then getter boom']);
          assert.strictEqual(runtime._configureStateForExtension('alpha.ext').pending, false);
          assert.strictEqual(unregisterBadThenable(), true);

          const detachedOpener = {{
            extensionId: 'alpha.ext',
            isConnected: false,
            disabled: false,
            hidden: false,
            focusCount: 0,
            getAttribute() {{ return null; }},
            closest() {{ return null; }},
            focus() {{ this.focusCount += 1; }},
          }};
          const unregisterDetached = alpha.settings.registerConfigure(({{restoreFocus}}) => restoreFocus());
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: detachedOpener}}), true);
          assert.strictEqual(detachedOpener.focusCount, 0);
          assert.strictEqual(replacementButton.focusCount, 1);
          assert.strictEqual(installedTab.focusCount, 0);

          let betaCalls = 0;
          const unregisterBeta = beta.settings.registerConfigure(({{restoreFocus}}) => {{ betaCalls += 1; restoreFocus(); }});
          runtime.primeFromStatus({{extensions: [
            {{id: 'alpha.ext', name: 'Alpha', effective_enabled: false}},
            {{id: 'beta.ext', name: 'Beta', effective_enabled: true}}
          ]}});
          assert.deepStrictEqual(runtime._configureStateForExtension('alpha.ext'), {{available: false, pending: false}});
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: connectedOpener}}), false);
          assert.strictEqual(runtime._invokeConfigure('beta.ext', {{opener: connectedOpener}}), true);
          assert.strictEqual(betaCalls, 1, 'another extension remains usable after alpha failures');

          runtime.primeFromStatus({{extensions: [{{id: 'beta.ext', name: 'Beta', effective_enabled: true}}]}});
          assert.deepStrictEqual(runtime._configureStateForExtension('alpha.ext'), {{available: false, pending: false}});
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: connectedOpener}}), false);
          assert.strictEqual(unregisterDetached(), true);
          assert.strictEqual(alpha.settings.registerConfigure(() => {{}}), null, 'uninstalled IDs stay quarantined');

          runtime.primeFromStatus({{extensions: [
            {{id: 'alpha.ext', name: 'Alpha reinstalled', effective_enabled: true}},
            {{id: 'beta.ext', name: 'Beta', effective_enabled: true}}
          ]}});
          assert.deepStrictEqual(runtime._configureStateForExtension('alpha.ext'), {{available: false, pending: false}});
          assert.strictEqual(runtime._invokeConfigure('alpha.ext', {{opener: connectedOpener}}), false);
          assert.strictEqual(unregisterBeta(), true);
          assert.strictEqual(stopChanges(), true);
          assert.strictEqual(stopChanges(), false);
        }})().then(
          () => {{ console.error = originalConsoleError; }},
          error => {{ console.error = originalConsoleError; throw error; }}
        );
        """
    )
    _run_node(script)


def test_documented_configure_consumer_is_safe_on_older_core_without_register_configure():
    """The EXTENSIONS.md consumer pattern must not throw on an OLDER E0 Core
    build where ``register()`` and ``settings`` exist but ``registerConfigure``
    does not — the optional-chained form ``ext?.settings?.registerConfigure?.()``
    degrades to ``undefined`` instead of a TypeError, so extension init survives.
    Regression for the doc example at docs/EXTENSIONS.md (#7111 gate)."""
    script = textwrap.dedent(
        """
        const assert = require('assert');

        // Simulate an OLDER boot-trusted E0 handle: register() returns an ext
        // whose settings object has NO registerConfigure (the capability the
        // current Core adds). This is exactly what a pre-#7111 Core exposes.
        global.window = {
          hermesExt: {
            register(id) {
              return { id, settings: { get() { return null; }, set() { return false; } } };
            }
          }
        };

        let initReached = false;
        // The documented consumer pattern, verbatim from docs/EXTENSIONS.md.
        const ext = window.hermesExt?.register?.("dictionary-manager");
        const unregister = ext?.settings?.registerConfigure?.(({ opener, restoreFocus }) => {
          // never called on old Core
          return true;
        });
        initReached = true;  // must be reached — no throw above

        assert.strictEqual(initReached, true, 'extension init must survive missing registerConfigure');
        assert.strictEqual(unregister, undefined, 'registerConfigure absent => optional call yields undefined');

        // Sanity: the UNGUARDED form WOULD throw, proving the guard is load-bearing.
        let threw = false;
        try {
          ext.settings.registerConfigure(() => {});
        } catch (e) {
          threw = true;
        }
        assert.strictEqual(threw, true, 'unguarded call is expected to throw on old Core (guard is required)');
        """
    )
    _run_node(script)
