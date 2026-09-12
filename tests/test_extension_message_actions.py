"""Behavioral contract tests for the scoped extension message-action API."""

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
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_message_action_registration_presentation_and_limits():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const assert = require('assert');
        const store = new Map();
        global.window = {{
          __HERMES_EXTENSION_CONFIG__: {{
            extensions: [
              {{id: 'alpha.ext', name: 'Alpha'}},
              {{id: 'beta.ext', name: 'Beta'}},
              {{id: 'gamma.ext', name: 'Gamma'}},
            ]
          }},
          localStorage: {{
            getItem(key) {{ return store.has(key) ? store.get(key) : null; }},
            setItem(key, value) {{ store.set(key, String(value)); }},
            removeItem(key) {{ store.delete(key); }}
          }}
        }};
        eval(fs.readFileSync({str(EXTENSION_SETTINGS_JS)!r}, 'utf8'));

        const runtime = window.HermesExtensionSettings;
        const alpha = window.hermesExt.register('alpha.ext');
        const beta = window.hermesExt.register('beta.ext');
        const gamma = window.hermesExt.register('gamma.ext');
        assert.ok(alpha.messages);
        assert.strictEqual(window.hermesExt.messages, undefined,
          'legacy globals must not gain registration authority');

        const pressedContexts = [];
        let alphaPressed = true;
        const unregisterAlpha = alpha.messages.registerAction({{
          id: 'pin',
          label: 'Toggle pin',
          icon: 'pin',
          roles: ['assistant'],
          getPressed(context) {{ pressedContexts.push(context); return alphaPressed; }},
          onInvoke() {{}},
        }});
        const unregisterBeta = beta.messages.registerAction({{
          id: 'bookmark',
          label: 'Bookmark',
          icon: 'bookmark',
          onInvoke() {{}},
        }});
        assert.strictEqual(typeof unregisterAlpha, 'function');
        assert.strictEqual(typeof unregisterBeta, 'function');
        assert.strictEqual(gamma.messages.registerAction({{
          id: 'third', label: 'Third', icon: 'pin', onInvoke() {{}},
        }}), null, 'a third page-level action is rejected');
        assert.strictEqual(alpha.messages.registerAction({{
          id: 'pin', label: 'Duplicate', icon: 'pin', onInvoke() {{}},
        }}), null, 'duplicate identity does not replace the first registration');

        for (const descriptor of [
          null,
          {{id: '', label: 'Bad', icon: 'pin', onInvoke() {{}}}},
          {{id: 'bad id', label: 'Bad', icon: 'pin', onInvoke() {{}}}},
          {{id: 'bad-icon', label: 'Bad', icon: '<svg>', onInvoke() {{}}}},
          {{id: 'bad-role', label: 'Bad', icon: 'pin', roles: ['tool'], onInvoke() {{}}}},
          {{id: 'no-handler', label: 'Bad', icon: 'pin'}},
          {{get id() {{ throw new Error('hostile getter'); }}}},
        ]) assert.strictEqual(alpha.messages.registerAction(descriptor), null);

        const assistant = runtime._messageActionsForContext({{
          sessionId: 's-1', messageIndex: 9, role: 'assistant'
        }});
        assert.deepStrictEqual(assistant.map(action => [action.extensionId, action.id, action.pressed]), [
          ['alpha.ext', 'pin', true],
          ['beta.ext', 'bookmark', false],
        ]);
        assert.strictEqual(Object.isFrozen(assistant[0]), true);
        assert.strictEqual(Object.isFrozen(pressedContexts[0]), true);
        assert.deepStrictEqual(Object.keys(pressedContexts[0]).sort(), ['messageIndex', 'role', 'sessionId']);

        alphaPressed = false;
        assert.strictEqual(alpha.messages.invalidateActions(), true);
        assert.strictEqual(runtime._messageActionsForContext({{
          sessionId: 's-1', messageIndex: 9, role: 'assistant'
        }})[0].pressed, false, 'extension-owned state can request a Core refresh');

        const user = runtime._messageActionsForContext({{
          sessionId: 's-1', messageIndex: 8, role: 'user'
        }});
        assert.deepStrictEqual(user.map(action => action.extensionId), ['beta.ext']);

        assert.strictEqual(unregisterAlpha(), true);
        assert.strictEqual(unregisterAlpha(), false);
        assert.strictEqual(alpha.messages.invalidateActions(), false);
        assert.strictEqual(gamma.messages.registerAction({{
          id: 'third', label: 'Third', icon: 'pin', onInvoke() {{}},
        }}) instanceof Function, true, 'unregister releases the global slot');
        """
    )
    _run_node(script)


def test_message_action_async_pressed_fallback_consumes_rejection():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const assert = require('assert');
        const store = new Map();
        const unhandled = [];
        process.on('unhandledRejection', error => unhandled.push(error));
        global.window = {{
          __HERMES_EXTENSION_CONFIG__: {{
            extensions: [{{id: 'alpha.ext', name: 'Alpha'}}]
          }},
          localStorage: {{
            getItem(key) {{ return store.has(key) ? store.get(key) : null; }},
            setItem(key, value) {{ store.set(key, String(value)); }},
            removeItem(key) {{ store.delete(key); }}
          }}
        }};
        eval(fs.readFileSync({str(EXTENSION_SETTINGS_JS)!r}, 'utf8'));

        const alpha = window.hermesExt.register('alpha.ext');
        alpha.messages.registerAction({{
          id: 'pin', label: 'Toggle pin', icon: 'pin',
          getPressed() {{ return Promise.reject(new Error('invalid async pressed')); }},
          onInvoke() {{}},
        }});

        const actions = window.HermesExtensionSettings._messageActionsForContext({{
          sessionId: 's-1', messageIndex: 3, role: 'assistant'
        }});
        assert.strictEqual(actions[0].pressed, false,
          'unsupported async presentation still fails closed');
        await new Promise(resolve => setImmediate(resolve));
        assert.deepStrictEqual(unhandled, [],
          'the fallback must consume a rejected async presentation result');
        """
    )
    _run_node("(async () => {\n" + script + "\n})().catch(error => { console.error(error); process.exit(1); });")


def test_message_action_invocation_pending_failure_and_quarantine():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const assert = require('assert');
        const store = new Map();
        const changes = [];
        const failures = [];
        const resolvers = [];
        let calls = 0;
        let lastContext;
        global.window = {{
          __HERMES_EXTENSION_CONFIG__: {{
            extensions: [{{id: 'alpha.ext', name: 'Alpha'}}]
          }},
          localStorage: {{
            getItem(key) {{ return store.has(key) ? store.get(key) : null; }},
            setItem(key, value) {{ store.set(key, String(value)); }},
            removeItem(key) {{ store.delete(key); }}
          }}
        }};
        eval(fs.readFileSync({str(EXTENSION_SETTINGS_JS)!r}, 'utf8'));

        const runtime = window.HermesExtensionSettings;
        runtime._onMessageActionChange(change => changes.push(change));
        const alpha = window.hermesExt.register('alpha.ext');
        alpha.messages.registerAction({{
          id: 'pin', label: 'Toggle pin', icon: 'pin',
          getPressed() {{ return {{then() {{}}}}; }},
          onInvoke(context) {{
            calls += 1;
            lastContext = context;
            return new Promise(resolve => {{ resolvers.push(resolve); }});
          }},
        }});

        const context = {{sessionId: 's-1', messageIndex: 3, role: 'assistant', text: 'Visible text'}};
        assert.strictEqual(runtime._invokeMessageAction('alpha.ext', 'pin', context, {{
          onError(error) {{ failures.push(error); }}
        }}), true);
        assert.strictEqual(runtime._invokeMessageAction('alpha.ext', 'pin', context), false,
          'same target is suppressed while pending');
        assert.strictEqual(runtime._invokeMessageAction('alpha.ext', 'pin', {{...context, messageIndex: 4}}), true,
          'a different target may run concurrently');
        assert.strictEqual(calls, 2);
        assert.strictEqual(Object.isFrozen(lastContext), true);
        assert.deepStrictEqual(lastContext, {{...context, messageIndex: 4}});
        assert.strictEqual(runtime._messageActionsForContext(context)[0].pending, true);

        resolvers.forEach(resolve => resolve());
        await Promise.resolve();
        assert.ok(changes.some(change => change.reason === 'pending'));
        assert.strictEqual(runtime._messageActionsForContext(context)[0].pending, false);

        alpha.messages.registerAction({{
          id: 'throws', label: 'Throws', icon: 'pin',
          onInvoke() {{ throw new Error('boom'); }},
        }});
        assert.strictEqual(runtime._invokeMessageAction('alpha.ext', 'throws', context, {{
          onError(error) {{ failures.push(error); }}
        }}), true);
        assert.strictEqual(failures.at(-1).message, 'boom');

        runtime.primeFromStatus({{extensions: []}});
        assert.deepStrictEqual(runtime._messageActionsForContext(context), []);
        assert.strictEqual(alpha.messages.registerAction({{
          id: 'revive', label: 'Revive', icon: 'pin', onInvoke() {{}},
        }}), null, 'uninstalled IDs stay quarantined until reload');
        """
    )
    _run_node("(async () => {\n" + script + "\n})().catch(error => { console.error(error); process.exit(1); });")


def test_old_invocation_cannot_settle_replacement_registration():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const assert = require('assert');
        const store = new Map();
        global.window = {{
          __HERMES_EXTENSION_CONFIG__: {{
            extensions: [{{id: 'alpha.ext', name: 'Alpha'}}]
          }},
          localStorage: {{
            getItem(key) {{ return store.has(key) ? store.get(key) : null; }},
            setItem(key, value) {{ store.set(key, String(value)); }},
            removeItem(key) {{ store.delete(key); }}
          }}
        }};
        eval(fs.readFileSync({str(EXTENSION_SETTINGS_JS)!r}, 'utf8'));

        const runtime = window.HermesExtensionSettings;
        const alpha = window.hermesExt.register('alpha.ext');
        const resolvers = [];
        let calls = 0;
        function descriptor() {{
          return {{
            id: 'pin', label: 'Toggle pin', icon: 'pin',
            onInvoke() {{
              calls += 1;
              return new Promise(resolve => {{ resolvers.push(resolve); }});
            }},
          }};
        }}
        const context = {{sessionId: 's-1', messageIndex: 3, role: 'assistant', text: 'Visible text'}};

        const unregister = alpha.messages.registerAction(descriptor());
        assert.strictEqual(runtime._invokeMessageAction('alpha.ext', 'pin', context), true);
        assert.strictEqual(unregister(), true);
        assert.strictEqual(alpha.messages.registerAction(descriptor()) instanceof Function, true);
        assert.strictEqual(runtime._invokeMessageAction('alpha.ext', 'pin', context), true);
        assert.strictEqual(runtime._messageActionsForContext(context)[0].pending, true);

        resolvers[0]();
        await Promise.resolve();
        assert.strictEqual(runtime._messageActionsForContext(context)[0].pending, true,
          'an old invocation cannot clear replacement registration pending state');
        assert.strictEqual(runtime._invokeMessageAction('alpha.ext', 'pin', context), false,
          'the replacement invocation remains duplicate-suppressed');
        assert.strictEqual(calls, 2);

        resolvers[1]();
        await Promise.resolve();
        assert.strictEqual(runtime._messageActionsForContext(context)[0].pending, false);
        """
    )
    _run_node("(async () => {\n" + script + "\n})().catch(error => { console.error(error); process.exit(1); });")


def test_core_message_action_surface_resolves_and_revalidates_visible_context():
    ui_js = UI_JS.read_text(encoding="utf-8")
    start = ui_js.index("function _extensionMessageActionContext")
    end = ui_js.index("let _extensionMessageActionChangeUnsubscribe", start)
    functions = ui_js[start:end]
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        let invoked = null;
        global.window = {{
          HermesExtensionSettings: {{
            _invokeMessageAction(extensionId, actionId, context, options) {{
              invoked = {{extensionId, actionId, context, options}};
              return true;
            }}
          }}
        }};
        const S = {{
          session: {{session_id: 'session-1'}},
          messages: [{{role: 'assistant', content: 'persisted provider payload'}}],
        }};
        function _messageSessionIndexForRawIdx(rawIdx) {{ return 7 + rawIdx; }}
        function esc(value) {{
          return String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
        }}
        function li(icon) {{ return `<svg data-icon="${{icon}}"></svg>`; }}
        function showToast() {{ throw new Error('unexpected failure toast'); }}
        {functions}

        let hidden = false;
        const owner = {{dataset: {{msgIdx: '0', sessionMsgIdx: '7', rawText: 'Visible plain text'}}}};
        const roleOwner = {{dataset: {{role: 'assistant'}}}};
        const slot = {{
          closest(selector) {{
            if (selector.startsWith('[hidden')) return hidden ? {{}} : null;
            if (selector.startsWith('[data-msg-idx')) return owner;
            if (selector === '[data-role]') return roleOwner;
            return null;
          }}
        }};
        const context = _extensionMessageActionContext(slot, true);
        assert.deepStrictEqual(context, {{
          sessionId: 'session-1', messageIndex: 7, role: 'assistant', text: 'Visible plain text'
        }});
        assert.notStrictEqual(context.text, S.messages[0].content,
          'invocation text comes from the Core-rendered visible row');

        owner.dataset.rawText = '';
        S.messages[0] = {{role: 'assistant', content: '', attachments: [{{name: 'diagram.png'}}]}};
        assert.deepStrictEqual(_extensionMessageActionContext(slot, true), {{
          sessionId: 'session-1', messageIndex: 7, role: 'assistant', text: ''
        }}, 'attachment-only settled rows remain eligible with empty visible plain text');
        owner.dataset.rawText = 'Visible plain text';
        S.messages[0] = {{role: 'assistant', content: 'persisted provider payload'}};

        const html = _extensionMessageActionButtonHtml({{
          extensionId: 'alpha.ext', id: 'pin', label: 'Pin <message>', icon: 'pin',
          pressed: true, pending: true,
        }}, context);
        assert.ok(html.includes('aria-pressed="true"'));
        assert.ok(html.includes('aria-busy="true" disabled'));
        assert.ok(html.includes('Pin &lt;message>'));
        assert.ok(html.includes('data-icon="pin"'));

        const button = {{
          disabled: false,
          dataset: {{
            extensionId: 'alpha.ext', extensionActionId: 'pin', sessionId: 'session-1',
            messageIndex: '7', messageRole: 'assistant',
          }},
          closest(selector) {{ return selector === '[data-extension-message-actions]' ? slot : null; }},
        }};
        assert.strictEqual(invokeExtensionMessageAction(button), true);
        assert.deepStrictEqual(invoked.context, context);
        assert.strictEqual(invoked.extensionId, 'alpha.ext');
        assert.strictEqual(invoked.actionId, 'pin');

        button.dataset.messageIndex = '8';
        invoked = null;
        assert.strictEqual(invokeExtensionMessageAction(button), false,
          'a stale button cannot invoke against a newly resolved target');
        assert.strictEqual(invoked, null);

        hidden = true;
        assert.strictEqual(_extensionMessageActionContext(slot, false), null,
          'hidden worklog/anchor rows are ineligible');

        hidden = false;
        const attributes = {{}};
        const existingButton = {{
          dataset: {{extensionId: 'alpha.ext', extensionActionId: 'pin'}},
          disabled: false,
          setAttribute(name, value) {{ attributes[name] = value; }},
        }};
        let replacements = 0;
        const syncSlot = {{
          children: [existingButton],
          closest: slot.closest,
          get innerHTML() {{ return ''; }},
          set innerHTML(_value) {{ replacements += 1; }},
        }};
        window.HermesExtensionSettings._messageActionsForContext = () => [{{
          extensionId: 'alpha.ext', id: 'pin', label: 'Toggle pin', icon: 'pin',
          pressed: true, pending: true,
        }}];
        _syncExtensionMessageActionSlots({{querySelectorAll() {{ return [syncSlot]; }}}});
        assert.strictEqual(replacements, 0, 'pending updates preserve the connected opener');
        assert.strictEqual(existingButton.disabled, true);
        assert.strictEqual(attributes['aria-pressed'], 'true');
        assert.strictEqual(attributes['aria-busy'], 'true');
        assert.deepStrictEqual(existingButton.dataset, {{
          extensionId: 'alpha.ext', extensionActionId: 'pin', sessionId: 'session-1',
          messageIndex: '7', messageRole: 'assistant',
        }});
        """
    )
    _run_node(script)


def test_render_path_reconciles_message_actions_before_session_html_is_cached():
    ui_js = UI_JS.read_text(encoding="utf-8")
    render_start = ui_js.index("function renderMessages(options)")
    render_end = ui_js.index("function _toolDisplayName", render_start)
    render_body = ui_js[render_start:render_end]

    assert "data-extension-message-actions" in render_body
    guarded_sync = "if(typeof _syncExtensionMessageActionSlots==='function') _syncExtensionMessageActionSlots(inner);"
    assert render_body.index(guarded_sync) < render_body.index(
        "const _html=inner.innerHTML;"
    )
    cache_restore = render_body.index("inner.innerHTML=cached.html;")
    cache_return = render_body.index("return;", cache_restore)
    assert guarded_sync in render_body[cache_restore:cache_return]
