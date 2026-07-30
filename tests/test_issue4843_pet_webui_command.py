"""Regression tests for the WebUI /pet handoff."""

import json
from pathlib import Path
import subprocess
import tempfile
import textwrap


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _run_pet_js(
    *,
    status,
    adapter_status=None,
    status_throws=False,
    hook_result=None,
    hook_throws=False,
    command="/pet feed tuna",
):
    hook_setup = ""
    if hook_throws:
        hook_setup += textwrap.dedent(
            """
            ctx.window.__hermesHandlePetSlashCommand = async payload => {
              hookCalls.push(payload);
              throw new Error('hook failed');
            };
            """
        )
    elif hook_result is not None:
        hook_setup += textwrap.dedent(
            f"""
            ctx.window.__hermesHandlePetSlashCommand = async payload => {{
              hookCalls.push(payload);
              return {json.dumps(hook_result)};
            }};
            """
        )

    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const hookCalls = [];
        const consoleErrors = [];
        const captureConsole = {{
          ...console,
          error: (...args) => {{
            consoleErrors.push(args.map(arg => String(arg && arg.message ? arg.message : arg)).join(' '));
          }},
        }};
        const window = {{
          __HERMES_WEBUI_DESKTOP_COMPANION_STATUS__: {json.dumps(adapter_status)},
        }};
        window.window = window;
        const ctx = {{
          console: captureConsole,
          window,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: key => key,
          api: async path => {{
            if (path === '/api/extensions/status') {{
              if ({json.dumps(status_throws)}) throw new Error('extensions api failed');
              return {json.dumps(status)};
            }}
            throw new Error('unexpected api path: ' + path);
          }},
        }};
        {hook_setup}
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        (async () => {{
          const result = await vm.runInContext(`(async () => {{ return await handlePetSlashCommand({json.dumps(command)}, {{name:'pet'}}); }})()`, ctx);
          process.stdout.write(JSON.stringify({{result, hookCalls, consoleErrors}}));
        }})().catch(err => {{
          console.error(err && err.stack || err);
          process.exit(1);
        }});
        """
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run(["node", str(script_path)], check=True, capture_output=True, text=True)
    finally:
        script_path.unlink(missing_ok=True)
    return json.loads(proc.stdout)


def _run_send_js(*, command, status, adapter_status=None, hook_result=None, hook_throws=False):
    hook_setup = ""
    if hook_throws:
        hook_setup += textwrap.dedent(
            """
            ctx.window.__hermesHandlePetSlashCommand = async payload => {
              throw new Error('hook failed');
            };
            """
        )
    elif hook_result is not None:
        hook_setup += textwrap.dedent(
            f"""
            ctx.window.__hermesHandlePetSlashCommand = async payload => {{
              return {json.dumps(hook_result)};
            }};
            """
        )
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const msgInput = {{
          value: {json.dumps(command)},
          style: {{}},
          scrollHeight: 0,
          addEventListener(){{}},
          removeEventListener(){{}},
          focus(){{}},
          blur(){{}},
          setSelectionRange(){{}},
        }};
        const commandExecCalls = [];
        const genericEl = {{
          addEventListener(){{}},
          removeEventListener(){{}},
          classList: {{ add(){{}}, remove(){{}} }},
          style: {{}},
          dataset: {{}},
          value: '',
          textContent: '',
          innerHTML: '',
        }};
        const ctx = {{
          console,
          window: {{
            __HERMES_WEBUI_DESKTOP_COMPANION_STATUS__: {json.dumps(adapter_status)},
            addEventListener(){{}},
            requestAnimationFrame(cb){{ return 1; }},
          }},
          document: {{
            addEventListener(){{}},
            getElementById(id){{ return id === 'msg' ? msgInput : genericEl; }},
            querySelector(){{ return null; }},
          }},
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: key => key,
          S: {{
            busy: false,
            session: null,
            pendingFiles: [],
            messages: [],
            activeProfile: 'default',
            activeStreamId: null,
          }},
          INFLIGHT: {{}},
          _pendingSelections: [],
          _sendInProgress: false,
          _sendInProgressSid: null,
          _composerTextWithPendingSelections(){{ return msgInput.value; }},
          _flushSelectionBlocksToComposer(){{}},
          _dismissHandoffHint(){{}},
          _clearStaleBusyStateBeforeSend(){{ return false; }},
          _clearComposerAfterQueuedSelectionSend(){{}},
          _chatPayloadModelState(){{ return {{ model: '', model_provider: '' }}; }},
          queueSessionMessage(){{}},
          updateQueueBadge(){{}},
          renderTray(){{}},
          showToast(){{}},
          renderMessages(){{}},
          renderSessionList(){{}},
          autoResize(){{}},
          hideCmdDropdown(){{}},
          syncTopbar(){{}},
          setBusy(){{}},
          setComposerStatus(){{}},
          setStatus(){{}},
          updateSendBtn(){{}},
          clearOptimisticSessionStreaming(){{}},
          newSession: async () => {{
            ctx.S.session = {{ session_id: 'sid-1', title: 'New Chat' }};
          }},
          $: id => {{
            if (id === 'msg') return msgInput;
            return genericEl;
          }},
          api: async (path, options) => {{
            if (path === '/api/commands') return {{
              commands: [
                {{
                  name: 'pet',
                  description: 'Desktop Companion command',
                  category: 'Tools',
                  aliases: [],
                  cli_only: true,
                  gateway_only: false
                }},
                {{
                  name: 'browser',
                  description: 'Attach browser tools',
                  category: 'Tools',
                  aliases: ['browse'],
                  cli_only: true,
                  gateway_only: false
                }}
              ]
            }};
            if (path === '/api/extensions/status') return {json.dumps(status)};
            if (path === '/api/commands/exec') {{
              commandExecCalls.push(JSON.parse(options.body).command);
              return {{ output: 'unexpected exec' }};
            }}
            throw new Error('unexpected api path: ' + path);
          }},
        }};
        ctx.window.window = ctx.window;
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        {hook_setup}
        vm.runInContext({json.dumps(MESSAGES_JS)}, ctx);
        (async () => {{
          await vm.runInContext('send()', ctx);
          process.stdout.write(JSON.stringify({{
            messages: ctx.S.messages,
            commandExecCalls,
            remainingInput: msgInput.value,
          }}));
        }})().catch(err => {{
          console.error(err && err.stack || err);
          process.exit(1);
        }});
        """
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run(["node", str(script_path)], check=True, capture_output=True, text=True)
    finally:
        script_path.unlink(missing_ok=True)
    return json.loads(proc.stdout)


def test_pet_help_routes_to_install_guidance_when_companion_is_missing():
    result = _run_pet_js(
        status={"enabled": False, "extensions": [], "gallery_installed": {}},
        adapter_status=None,
    )

    assert result["result"]["handled"] is False
    message = result["result"]["message"]
    assert "Desktop Companion is not installed yet." in message
    assert "Settings -> Extensions -> Gallery -> Desktop Companion" in message
    assert "https://github.com/franksong2702/hermes-webui-desktop-companion#after-gallery-install" in message
    assert "Desktop Companion app" in message


def test_pet_help_routes_to_enable_guidance_when_companion_is_disabled():
    result = _run_pet_js(
        status={
            "enabled": True,
            "extensions": [
                {
                    "id": "desktop-companion",
                    "name": "Desktop Companion",
                    "effective_enabled": False,
                    "user_disabled": True,
                    "status": "user_disabled",
                }
            ],
        },
        adapter_status=None,
    )

    assert result["result"]["handled"] is False
    message = result["result"]["message"]
    assert "Desktop Companion is installed but disabled." in message
    assert "Enable it in Settings -> Extensions" in message
    assert "Desktop Companion app" in message
    assert "https://github.com/franksong2702/hermes-webui-desktop-companion#after-gallery-install" in message


def test_pet_help_routes_to_reload_and_start_guidance_when_adapter_status_is_missing():
    result = _run_pet_js(
        status={
            "enabled": True,
            "extensions": [
                {
                    "id": "desktop-companion",
                    "name": "Desktop Companion",
                    "effective_enabled": True,
                    "user_disabled": False,
                    "status": "enabled",
                }
            ],
        },
        adapter_status=None,
    )

    assert result["result"]["handled"] is False
    message = result["result"]["message"]
    assert "adapter status is not loaded yet" in message
    assert "Reload WebUI if you just enabled it" in message
    assert "Desktop Companion app" in message


def test_pet_help_routes_to_connect_guidance_when_adapter_is_not_connected():
    result = _run_pet_js(
        status={
            "enabled": True,
            "extensions": [
                {
                    "id": "desktop-companion",
                    "name": "Desktop Companion",
                    "effective_enabled": True,
                    "user_disabled": False,
                    "status": "enabled",
                }
            ],
        },
        adapter_status={"connected": False},
    )

    assert result["result"]["handled"] is False
    message = result["result"]["message"]
    assert "local app is not connected yet" in message
    assert "Start or connect the Desktop Companion app" in message
    assert "Setup guide:" in message


def test_pet_help_hands_off_to_desktop_companion_hook_when_connected():
    result = _run_pet_js(
        status={
            "enabled": True,
            "extensions": [
                {
                    "id": "desktop-companion",
                    "name": "Desktop Companion",
                    "effective_enabled": True,
                    "user_disabled": False,
                    "status": "enabled",
                }
            ],
        },
        adapter_status={"connected": True},
        hook_result={"handled": True, "message": "mascot handled"},
        command="/pet   feed  tuna  ",
    )

    assert result["result"] == {"handled": True, "message": "mascot handled"}
    assert result["hookCalls"] == [
        {
            "command": "/pet   feed  tuna  ",
            "args": "feed tuna",
            "source": "webui-slash-command",
            "metadata": {"name": "pet"},
        }
    ]


def test_pet_help_preserves_explicit_falsy_hook_message_values():
    result = _run_pet_js(
        status={
            "enabled": True,
            "extensions": [
                {
                    "id": "desktop-companion",
                    "name": "Desktop Companion",
                    "effective_enabled": True,
                    "user_disabled": False,
                    "status": "enabled",
                }
            ],
        },
        adapter_status={"connected": True},
        hook_result={"handled": True, "message": 0},
        command="/pet count snacks",
    )

    assert result["result"] == {"handled": True, "message": "0"}
    assert result["hookCalls"] == [
        {
            "command": "/pet count snacks",
            "args": "count snacks",
            "source": "webui-slash-command",
            "metadata": {"name": "pet"},
        }
    ]


def test_pet_help_treats_truthy_hook_result_as_handled():
    result = _run_pet_js(
        status={
            "enabled": True,
            "extensions": [
                {
                    "id": "desktop-companion",
                    "name": "Desktop Companion",
                    "effective_enabled": True,
                    "user_disabled": False,
                    "status": "enabled",
                }
            ],
        },
        adapter_status={"connected": True},
        hook_result="mascot handled",
        command="/pet wave",
    )

    assert result["result"] == {"handled": True, "message": ""}
    assert result["hookCalls"] == [
        {
            "command": "/pet wave",
            "args": "wave",
            "source": "webui-slash-command",
            "metadata": {"name": "pet"},
        }
    ]


def test_pet_help_routes_to_status_error_when_extension_status_api_fails():
    result = _run_pet_js(
        status={"enabled": False, "extensions": []},
        status_throws=True,
        adapter_status=None,
    )

    assert result["result"]["handled"] is False
    assert result["result"]["message"] == (
        "Desktop Companion status is unavailable right now.\n\n"
        "Reload WebUI or check your connection, then retry /pet."
    )


def test_pet_help_falls_back_to_unavailable_guidance_when_hook_is_missing():
    result = _run_pet_js(
        status={
            "enabled": True,
            "extensions": [
                {
                    "id": "desktop-companion",
                    "name": "Desktop Companion",
                    "effective_enabled": True,
                    "user_disabled": False,
                    "status": "enabled",
                }
            ],
        },
        adapter_status={"connected": True},
    )

    message = result["result"]["message"]
    assert result["result"]["handled"] is False
    assert "Desktop Companion is installed and connected" in message
    assert "/pet is not available yet in this Desktop Companion version" in message
    assert "Update the Desktop Companion app" in message
    assert "https://github.com/franksong2702/hermes-webui-desktop-companion#after-gallery-install" in message


def test_pet_help_routes_to_hook_error_guidance_when_hook_throws():
    result = _run_pet_js(
        status={
            "enabled": True,
            "extensions": [
                {
                    "id": "desktop-companion",
                    "name": "Desktop Companion",
                    "effective_enabled": True,
                    "user_disabled": False,
                    "status": "enabled",
                }
            ],
        },
        adapter_status={"connected": True},
        hook_throws=True,
    )

    assert result["result"]["handled"] is False
    assert result["result"]["message"] == (
        "Desktop Companion is installed and connected, but it hit an error while handling /pet.\n\n"
        "Check the browser console and the Desktop Companion app, then retry /pet."
    )
    assert any("Desktop Companion /pet hook error:" in entry for entry in result["consoleErrors"])
    assert any("hook failed" in entry for entry in result["consoleErrors"])


def test_pet_slash_intercept_bypasses_generic_agent_execution():
    pet = _run_send_js(
        command="/pet feed tuna",
        status={"enabled": False, "extensions": []},
        adapter_status=None,
    )
    browser = _run_send_js(
        command="/browser open",
        status={"enabled": False, "extensions": []},
        adapter_status=None,
    )

    assert [item["role"] for item in pet["messages"]] == ["user", "assistant"]
    assert "Desktop Companion is not installed yet." in pet["messages"][1]["content"]
    assert "Hermes CLI-only command" not in pet["messages"][1]["content"]
    assert pet["commandExecCalls"] == []
    assert pet["remainingInput"] == ""

    assert [item["role"] for item in browser["messages"]] == ["user", "assistant"]
    assert "`/browser` is a Hermes CLI-only command" in browser["messages"][1]["content"]
    assert browser["commandExecCalls"] == []


def test_pet_send_uses_extension_message_when_hook_returns_one():
    result = _run_send_js(
        command="/pet feed tuna",
        status={
            "enabled": True,
            "extensions": [
                {
                    "id": "desktop-companion",
                    "name": "Desktop Companion",
                    "effective_enabled": True,
                    "user_disabled": False,
                    "status": "enabled",
                }
            ],
        },
        adapter_status={"connected": True},
        hook_result={"handled": True, "message": "mascot handled"},
    )

    assert [item["role"] for item in result["messages"]] == ["user", "assistant"]
    assert result["messages"][1]["content"] == "mascot handled"
    assert result["commandExecCalls"] == []
    assert result["remainingInput"] == ""


def test_pet_send_leaves_chat_reply_to_extension_when_hook_handles_silently():
    result = _run_send_js(
        command="/pet wave",
        status={
            "enabled": True,
            "extensions": [
                {
                    "id": "desktop-companion",
                    "name": "Desktop Companion",
                    "effective_enabled": True,
                    "user_disabled": False,
                    "status": "enabled",
                }
            ],
        },
        adapter_status={"connected": True},
        hook_result=True,
    )

    assert [item["role"] for item in result["messages"]] == ["user"]
    assert result["commandExecCalls"] == []
    assert result["remainingInput"] == ""
