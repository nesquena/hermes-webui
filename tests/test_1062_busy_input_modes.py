"""Regression tests for busy_input_mode (PR #1062, closes #720).

Pins the wiring for the three modes (queue / interrupt / steer):
- The setting key + default + enum validation in api/config.py
- Three slash commands registered in static/commands.js
- send()'s busy branch reads window._busyInputMode and dispatches
- Boot initializes window._busyInputMode from settings
- 17 new i18n keys present in all 6 locale blocks

Issue: #720 (configurable busy-input behaviour)
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


# ── Backend: setting registration + enum validation ─────────────────────


class TestBusyInputModeSetting:
    """The new setting key must be registered with a default and enum validator."""

    def test_default_is_queue(self):
        """Default value preserves existing queue behaviour for users who don't touch the setting."""
        assert '"busy_input_mode": "queue"' in CONFIG_PY, (
            "_DEFAULT_SETTINGS must include busy_input_mode='queue' so existing users see no change"
        )

    def test_enum_validator_present(self):
        """_SETTINGS_ENUM_KEYS must validate busy_input_mode against {queue, interrupt, steer}."""
        # Find the entry inside the enum dict (a set literal as the value)
        idx = CONFIG_PY.find('"busy_input_mode": {')
        assert idx >= 0, "busy_input_mode entry missing from _SETTINGS_ENUM_KEYS"
        block = CONFIG_PY[idx : idx + 200]
        assert '"queue"' in block and '"interrupt"' in block and '"steer"' in block, (
            "busy_input_mode enum must contain {queue, interrupt, steer}"
        )


# ── Frontend: slash commands ─────────────────────────────────────────────


class TestSlashCommandRegistration:
    """The three new slash commands must be registered in COMMANDS array."""

    def test_queue_command_registered(self):
        assert "name:'queue'" in COMMANDS_JS and "fn:cmdQueue" in COMMANDS_JS

    def test_interrupt_command_registered(self):
        assert "name:'interrupt'" in COMMANDS_JS and "fn:cmdInterrupt" in COMMANDS_JS

    def test_steer_command_registered(self):
        assert "name:'steer'" in COMMANDS_JS and "fn:cmdSteer" in COMMANDS_JS

    def test_all_three_busy_commands_are_no_echo(self):
        """All three busy commands must set noEcho:true so the slash invocation
        is not echoed as a visible user bubble.  Without noEcho, /queue causes a
        double-bubble: the raw slash text appears, then the queued message appears
        again when the drain fires.
        """
        for name in ("queue", "interrupt", "steer"):
            idx = COMMANDS_JS.find(f"name:'{name}'")
            assert idx >= 0, f"{name} not registered"
            block = COMMANDS_JS[idx : idx + 250]
            assert "noEcho:true" in block, (
                f"/{name} registration must set noEcho:true — "
                "without it the command text is echoed as a user bubble, causing duplicates"
            )


class TestSlashCommandHandlers:
    """The three handler functions must guard properly and call cancelStream where appropriate."""

    def test_cmd_queue_handles_idle_state(self):
        """/queue when idle now sends the message normally instead of showing an
        error toast.  The if(!S.busy) guard must still exist — it routes to the
        idle-send path rather than the queue path."""
        idx = COMMANDS_JS.find("async function cmdQueue(")
        assert idx >= 0
        body = COMMANDS_JS[idx : idx + 600]
        assert "if(!S.busy)" in body, (
            "/queue must have an if(!S.busy) guard that routes to send()"
        )

    def test_cmd_interrupt_calls_cancel_stream(self):
        idx = COMMANDS_JS.find("async function cmdInterrupt(")
        assert idx >= 0
        body = COMMANDS_JS[
            idx : idx + 1300
        ]  # expanded: idle-fallback block added before the busy path
        assert "queueSessionMessage" in body, (
            "/interrupt must queue the new message before cancelling"
        )
        assert "cancelStream" in body, (
            "/interrupt must call cancelStream() so the drain re-sends"
        )

    def test_cmd_steer_delegates_to_try_steer(self):
        """/steer delegates to _trySteer which calls /api/chat/steer with
        a queue+cancel fallback. The fallback path is exercised by tests
        in test_real_steer.py — this test just pins the delegation."""
        idx = COMMANDS_JS.find("async function cmdSteer(")
        assert idx >= 0
        body = COMMANDS_JS[idx : idx + 800]
        # cmdSteer now delegates to _trySteer; the fallback (queueSessionMessage
        # + cancelStream) lives inside _trySteer.
        assert "_trySteer" in body, (
            "cmdSteer must call _trySteer to use the real /api/chat/steer endpoint"
        )
        # The shared helper must contain the fallback path
        helper_idx = COMMANDS_JS.find("async function _trySteer(")
        assert helper_idx >= 0, "_trySteer helper must exist"
        helper_body = COMMANDS_JS[helper_idx : helper_idx + 1500]
        assert "queueSessionMessage" in helper_body
        assert "cancelStream" in helper_body
        # Toast should differ from interrupt to signal it's the steer path
        assert "cmd_steer_fallback" in helper_body or "steer_fallback" in helper_body

    # ── send() busy branch ───────────────────────────────────────────────────

    def test_slash_commands_clear_pending_files(self):
        """All three busy command handlers must clear S.pendingFiles (directly
        or via _trySteer) after enqueuing, so staged files are not duplicated.

        cmdQueue and cmdInterrupt call queueSessionMessage themselves and clear
        S.pendingFiles directly.  cmdSteer delegates to _trySteer.  The fallback/interrupt path clears
        S.pendingFiles inside _trySteer; the success path returns early and
        send() handles the post-await clear.  Either way files are not
        duplicated — we verify by checking _trySteer body for the clearing.
        """
        # cmdQueue and cmdInterrupt clear pendingFiles directly
        for fn_name in ("cmdQueue", "cmdInterrupt"):
            idx = COMMANDS_JS.find(f"function {fn_name}(")
            assert idx >= 0, f"{fn_name} not found"
            body = COMMANDS_JS[idx : idx + 800]
            assert "S.pendingFiles=[]" in body, (
                f"{fn_name} must clear S.pendingFiles after queueSessionMessage"
            )
            assert "renderTray()" in body, (
                f"{fn_name} must call renderTray() after clearing pendingFiles"
            )
        # cmdSteer delegates to _trySteer; that helper clears pendingFiles
        idx_try = COMMANDS_JS.find("function _trySteer(")
        assert idx_try >= 0, "_trySteer not found"
        try_body = COMMANDS_JS[idx_try : idx_try + 1200]
        assert "S.pendingFiles=[]" in try_body, (
            "_trySteer must clear S.pendingFiles in its fallback path — "
            "without this, files are lost on steer→interrupt fallback"
        )
        assert "renderTray()" in try_body, (
            "_trySteer must call renderTray() after clearing pendingFiles"
        )


class TestBusySendButton:
    """The composer send button must remain usable for busy-input actions."""

    def test_update_send_btn_uses_single_primary_action_button(self):
        idx = UI_JS.find("function updateSendBtn()")
        assert idx >= 0, "updateSendBtn() not found"
        body = UI_JS[idx : UI_JS.find("function setBusy", idx)]
        assert "getComposerPrimaryAction()" in body, (
            "updateSendBtn must derive icon/color/enabled state from one composer-primary action helper"
        )
        assert "btn.dataset.action=action" in body, (
            "btnSend should expose its current action for CSS, tests, and accessibility"
        )
        assert "btn.classList.toggle('stop',action==='stop')" in body, (
            "busy/no-draft state should turn the single primary button into the red stop action"
        )
        assert "btn.style.display=''" in body, (
            "the single primary button should remain visible while busy; it becomes Stop when there is no draft"
        )

    def test_composer_primary_action_accounts_for_all_busy_input_modes(self):
        idx = UI_JS.find("function getComposerPrimaryAction()")
        assert idx >= 0, "getComposerPrimaryAction() not found"
        body = UI_JS[idx : UI_JS.find("function _setComposerPrimaryButtonIcon", idx)]
        assert "return 'stop'" in body, "busy/no-draft + active stream must map to stop"
        assert "return 'queue'" in body, (
            "queue mode and unavailable steer/interrupt fallbacks must map to queue"
        )
        assert "return 'interrupt'" in body, (
            "interrupt mode with an active stream must map to interrupt"
        )
        assert "return 'steer'" in body, (
            "steer mode with active stream support must map to steer"
        )
        assert "window._busyInputMode||'queue'" in body, (
            "helper must respect the Busy input mode setting"
        )
        assert "_getExplicitBusyCommandAction(msg&&msg.value)" in body, (
            "explicit /queue, /interrupt, and /steer drafts must override the Busy input mode for button visuals"
        )

    def test_explicit_busy_commands_override_button_visual_action(self):
        idx = UI_JS.find("function _getExplicitBusyCommandAction(")
        assert idx >= 0, "_getExplicitBusyCommandAction() not found"
        body = UI_JS[idx : UI_JS.find("function getComposerPrimaryAction", idx)]
        assert "name==='queue'" in body and "return 'queue'" in body, (
            "typing /queue <message> should show the queue/list-end button even in another busy mode"
        )
        assert "name==='steer'" in body and "return 'steer'" in body, (
            "typing /steer <message> should show the steer/compass button even when the global mode is queue"
        )
        assert "name==='interrupt'" in body and "return 'interrupt'" in body, (
            "typing /interrupt <message> should show the interrupt/skip-forward button even in another busy mode"
        )
        assert "if(!args) return null" in body, (
            "partial slash commands without a payload should not override the primary button while the user is still typing"
        )

    def test_send_button_click_uses_primary_action_handler(self):
        assert "function handleComposerPrimaryAction()" in UI_JS, (
            "btnSend click should route through a primary action handler so Stop can cancel instead of sending"
        )
        assert "handleComposerPrimaryAction" in BOOT_JS, (
            "boot.js should wire btnSend to handleComposerPrimaryAction(), not directly to send()"
        )


class TestSendBusyBranchDispatch:
    """send()'s busy block must read window._busyInputMode and branch accordingly."""

    def test_send_reads_busy_input_mode(self):
        # The send() function should read window._busyInputMode in the busy block
        send_idx = MESSAGES_JS.find("async function send(")
        assert send_idx >= 0
        # Look in the first ~3000 chars of send() for the busy mode read
        send_body = MESSAGES_JS[send_idx : send_idx + 3000]
        assert "_busyInputMode" in send_body, (
            "send() must read window._busyInputMode in the S.busy branch"
        )

    def test_send_calls_cancel_stream_on_interrupt(self):
        send_idx = MESSAGES_JS.find("async function send(")
        send_body = MESSAGES_JS[send_idx : send_idx + 3000]
        # The interrupt branch must call cancelStream
        assert "cancelStream" in send_body
        # And queue before cancel (otherwise the drain has nothing to pick up)
        # Verify the order textually: queueSessionMessage appears before cancelStream
        # within the busy block's interrupt branch
        cancel_idx = send_body.find("cancelStream")
        queue_idx = send_body.find("queueSessionMessage")
        assert queue_idx >= 0 and cancel_idx >= 0
        assert queue_idx < cancel_idx, (
            "queueSessionMessage must run before cancelStream so the drain "
            "after setBusy(false) picks up the queued message"
        )

    def test_slash_commands_intercepted_before_busymode_routing(self):
        """The three busy-control slash commands (/steer /interrupt /queue) must be
        intercepted at the TOP of the busy block — before the busyMode routing — so
        they execute immediately while the agent is running.

        Without this intercept, typing /steer while busy queues the text as a plain
        message.  When it drains after the turn ends there is no active stream, so
        cmdSteer says "No active task to stop." and the steer is lost entirely.
        """
        send_idx = MESSAGES_JS.find("async function send(")
        assert send_idx >= 0, "send() not found"
        # Look in the first 500 chars of the busy block for the intercept
        busy_start = MESSAGES_JS.find("S.busy||compressionRunning", send_idx)
        assert busy_start >= 0, "busy block not found"
        # The intercept must appear BEFORE the busyMode assignment
        intercept_idx = MESSAGES_JS.find("'steer','interrupt','queue'", busy_start)
        busymode_idx = MESSAGES_JS.find("_busyInputMode||'queue'", busy_start)
        assert intercept_idx >= 0, (
            "send() must intercept /steer /interrupt /queue before the busyMode "
            "routing block — otherwise they queue instead of executing immediately"
        )
        assert intercept_idx < busymode_idx, (
            "The slash-command intercept must come BEFORE the busyMode routing "
            "so /steer executes while the agent is running, not after the turn ends"
        )

    def test_steer_intercept_calls_handler_directly(self):
        """The busy-intercept must dispatch via _bc.fn(_pc.args), not queue the text."""
        send_idx = MESSAGES_JS.find("async function send(")
        busy_start = MESSAGES_JS.find("S.busy||compressionRunning", send_idx)
        intercept_idx = MESSAGES_JS.find("'steer','interrupt','queue'", busy_start)
        assert intercept_idx >= 0
        # Get the intercept block (up to the next busyMode assignment)
        busymode_idx = MESSAGES_JS.find("_busyInputMode||'queue'", busy_start)
        intercept_block = MESSAGES_JS[intercept_idx:busymode_idx]
        assert "_bc.fn(_pc.args)" in intercept_block, (
            "The intercept must call the command handler directly via _bc.fn(_pc.args)"
        )
        assert "return;" in intercept_block, (
            "The intercept must return after dispatching so send() does not also queue"
        )

    def test_steer_intercept_clears_input_before_await(self):
        """The intercept must clear $('msg').value BEFORE awaiting the handler.

        Without the sync clear, the input field still shows '/steer foo' after
        the steer fires. If the user presses Enter again (a common reflex while
        waiting for the toast), send() re-runs and either re-fires the command
        or — once the turn ended — drops a confusing 'No active task to stop.'
        """
        send_idx = MESSAGES_JS.find("async function send(")
        busy_start = MESSAGES_JS.find("S.busy||compressionRunning", send_idx)
        intercept_idx = MESSAGES_JS.find("'steer','interrupt','queue'", busy_start)
        busymode_idx = MESSAGES_JS.find("_busyInputMode||'queue'", busy_start)
        intercept_block = MESSAGES_JS[intercept_idx:busymode_idx]
        clear_idx = intercept_block.find("$('msg').value=''")
        await_idx = intercept_block.find("await _bc.fn")
        assert clear_idx >= 0, (
            "The intercept must clear $('msg').value (so the field doesn't keep "
            "showing /steer foo after the command fires)"
        )
        assert await_idx >= 0, "await _bc.fn(...) must be present in the intercept"
        assert clear_idx < await_idx, (
            "$('msg').value='' must be cleared BEFORE awaiting the handler — "
            "otherwise a reflexive Enter press during the await re-fires the command"
        )


# ── Boot init + settings panel wiring ───────────────────────────────────


class TestBootAndPanelsWiring:
    def test_boot_init_default_path(self):
        """Boot success path initialises window._busyInputMode from settings."""
        assert "window._busyInputMode=(s.busy_input_mode||'queue')" in BOOT_JS

    def test_boot_init_fallback_path(self):
        """Boot fallback path (settings load failed) initialises to safe default."""
        # The fallback should set window._busyInputMode='queue'
        assert "window._busyInputMode='queue'" in BOOT_JS

    def test_panels_load_save_apply(self):
        assert "settingsBusyInputMode" in PANELS_JS, "panels.js must load the setting"
        assert "body.busy_input_mode" in PANELS_JS, (
            "saveSettings must include busy_input_mode in body"
        )
        assert "window._busyInputMode=body.busy_input_mode" in PANELS_JS, (
            "_applySavedSettingsUi must propagate busy_input_mode to the global"
        )

    def test_index_html_dropdown_has_three_options(self):
        idx = INDEX_HTML.find('id="settingsBusyInputMode"')
        assert idx >= 0
        block = INDEX_HTML[idx : idx + 800]
        assert 'value="queue"' in block
        assert 'value="interrupt"' in block
        assert 'value="steer"' in block


# ── i18n locale coverage ─────────────────────────────────────────────────


class TestI18nKeys:
    """All 17 new keys must appear in each of the 6 locale blocks."""

    REQUIRED_KEYS = [
        "cmd_queue",
        "cmd_interrupt",
        "cmd_steer",
        "cmd_queue_no_msg",
        "cmd_queue_not_busy",
        "cmd_queue_confirm",
        "cmd_interrupt_no_msg",
        "cmd_interrupt_confirm",
        "cmd_steer_no_msg",
        "cmd_steer_fallback",
        "busy_steer_fallback",
        "busy_interrupt_confirm",
        "settings_label_busy_input_mode",
        "settings_desc_busy_input_mode",
        "settings_busy_input_mode_queue",
        "settings_busy_input_mode_interrupt",
        "settings_busy_input_mode_steer",
    ]

    def test_each_key_appears_at_least_six_times(self):
        """Each key should appear once per locale (en, ru, es, de, zh, zh-Hant) = 6 occurrences minimum."""
        for key in self.REQUIRED_KEYS:
            count = I18N_JS.count(f"{key}:")
            assert count >= 6, (
                f"i18n key {key!r} appears {count} times; expected ≥6 (one per locale block)"
            )

    def test_key_count_total(self):
        """17 keys × 6 locales = 102 minimum occurrences across the file."""
        total = sum(I18N_JS.count(f"{key}:") for key in self.REQUIRED_KEYS)
        assert total >= 17 * 6, f"Total i18n occurrences = {total}; expected ≥ {17 * 6}"
