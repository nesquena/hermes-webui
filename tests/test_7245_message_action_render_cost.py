"""Release-stage regressions for #7245 (senior review SHOULD-FIX 1 and 2, 2026-10-05).

1. With no registered message action, the per-render slot sync does no per-row context work: it only empties a
   slot that still holds buttons from a retired registration.
2. A message-action change drops only the cached transcript HTML, never the markdown/virtualization caches, and a
   `pending` flip (reconciled in place) drops nothing at all.
Both drive the real code: the runtime from static/extension_settings.js and the slot/listener functions sliced from
static/ui.js.
"""
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

ROOT = Path(__file__).parent.parent
EXTENSION_SETTINGS_JS = ROOT / "static" / "extension_settings.js"
UI_JS = ROOT / "static" / "ui.js"


def _run_node(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for extension message-action runtime tests")
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=20)
    assert result.returncode == 0, result.stderr + result.stdout


def _ui_slice(start_marker: str, end_marker: str) -> str:
    ui_js = UI_JS.read_text(encoding="utf-8")
    start = ui_js.index(start_marker)
    return ui_js[start:ui_js.index(end_marker, start)]


def test_runtime_reports_whether_any_message_action_is_registered():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const assert = require('assert');
        const store = new Map();
        global.window = {{
          __HERMES_EXTENSION_CONFIG__: {{extensions: [{{id: 'alpha.ext', name: 'Alpha'}}]}},
          localStorage: {{
            getItem(key) {{ return store.has(key) ? store.get(key) : null; }},
            setItem(key, value) {{ store.set(key, String(value)); }},
            removeItem(key) {{ store.delete(key); }}
          }}
        }};
        eval(fs.readFileSync({str(EXTENSION_SETTINGS_JS)!r}, 'utf8'));
        const runtime = window.HermesExtensionSettings;
        assert.strictEqual(runtime._hasMessageActions(), false);
        const alpha = window.hermesExt.register('alpha.ext');
        const unregister = alpha.messages.registerAction({{id: 'pin', label: 'Pin', icon: 'pin', onInvoke() {{}}}});
        assert.strictEqual(runtime._hasMessageActions(), true);
        unregister();
        assert.strictEqual(runtime._hasMessageActions(), false);
        """
    )
    _run_node(script)


def test_zero_registration_sync_skips_row_context_and_clears_retired_buttons():
    functions = _ui_slice("function _extensionMessageActionContext", "let _extensionMessageActionChangeUnsubscribe")
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        let registered = false;
        let contextLookups = 0;
        global.window = {{
          HermesExtensionSettings: {{
            _hasMessageActions() {{ return registered; }},
            _messageActionsForContext() {{ contextLookups += 1; return []; }},
          }}
        }};
        const S = {{session: {{session_id: 's-1'}}, messages: []}};
        function _messageSessionIndexForRawIdx(rawIdx) {{ return rawIdx; }}
        function esc(value) {{ return String(value); }}
        function li() {{ return ''; }}
        {functions}

        let closestCalls = 0;
        let cleared = 0;
        const staleSlot = {{
          children: [{{}}],
          closest() {{ closestCalls += 1; return null; }},
          get innerHTML() {{ return '<button></button>'; }},
          set innerHTML(value) {{ assert.strictEqual(value, ''); cleared += 1; }},
        }};
        const selectors = [];
        const scope = {{
          querySelectorAll(selector) {{
            selectors.push(selector);
            return selector.includes(':not(:empty)') ? [staleSlot] : [staleSlot, staleSlot, staleSlot];
          }}
        }};
        _syncExtensionMessageActionSlots(scope);
        assert.deepStrictEqual(selectors, ['[data-extension-message-actions]:not(:empty)']);
        assert.strictEqual(cleared, 1, 'a retired button is removed');
        assert.strictEqual(closestCalls, 0, 'no per-row context resolution without registrations');
        assert.strictEqual(contextLookups, 0);

        registered = true;
        selectors.length = 0;
        _syncExtensionMessageActionSlots(scope);
        assert.deepStrictEqual(selectors, ['[data-extension-message-actions]']);
        assert.ok(closestCalls > 0, 'registered actions still resolve each row');
        """
    )
    _run_node(script)


def test_message_action_change_drops_only_session_html_and_pending_drops_nothing():
    binder = _ui_slice("let _extensionMessageActionChangeUnsubscribe", "\n};\n") + "\n};\n"
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        let listener = null;
        let syncs = 0;
        global.window = {{
          HermesExtensionSettings: {{
            _onMessageActionChange(fn) {{ listener = fn; return () => true; }},
          }}
        }};
        global.document = {{ getElementById() {{ return null; }} }};
        const _sessionHtmlCache = new Map([['s-1', {{html: '<div>cached</div>'}}]]);
        let _sessionHtmlCacheSid = 's-1';
        function clearMessageRenderCache() {{ throw new Error('must not wipe the markdown/height caches'); }}
        function _syncExtensionMessageActionSlots() {{ syncs += 1; }}
        {binder}
        window._bindHermesExtensionMessageActions();
        assert.strictEqual(typeof listener, 'function');
        const baseline = syncs;

        listener({{extensionId: 'alpha.ext', actionId: 'pin', reason: 'pending'}});
        assert.strictEqual(_sessionHtmlCache.size, 1, 'pending keeps cached transcript HTML');
        assert.strictEqual(_sessionHtmlCacheSid, 's-1');
        assert.strictEqual(syncs, baseline + 1, 'pending still reconciles visible slots in place');

        listener({{extensionId: 'alpha.ext', actionId: 'pin', reason: 'registration'}});
        assert.strictEqual(_sessionHtmlCache.size, 0, 'a presentation change drops cached transcript HTML');
        assert.strictEqual(_sessionHtmlCacheSid, null);
        assert.strictEqual(syncs, baseline + 2);
        """
    )
    _run_node(script)
