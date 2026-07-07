"""Regression tests for default_message_mode (PR #1062, closes #720).

Pins the wiring for the three modes (queue / interrupt / steer):
- The setting key + default + enum validation in api/config.py
- Three slash commands registered in static/commands.js
- send()'s busy branch reads window._defaultMessageMode and dispatches
- Boot initializes window._defaultMessageMode from settings
- 17 new i18n keys present in all 6 locale blocks

Issue: #720 (configurable busy-input behaviour)
"""
from pathlib import Path

from tests.helpers import source_between as _source_between

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

    def test_default_is_steer(self):
        """Default value resolves to steer for users who don't touch the setting."""
        assert '"default_message_mode": "steer"' in CONFIG_PY, (
            "_DEFAULT_SETTINGS must include default_message_mode='steer' so new users see the steer default"
        )

    def test_enum_validator_present(self):
        """_SETTINGS_ENUM_KEYS must validate default_message_mode against {queue, interrupt, steer}."""
        # Find the entry inside the enum dict (a set literal as the value)
        idx = CONFIG_PY.find('"default_message_mode": {')
        assert idx >= 0, "default_message_mode entry missing from _SETTINGS_ENUM_KEYS"
        block = CONFIG_PY[idx:idx + 200]
        assert '"queue"' in block and '"interrupt"' in block and '"steer"' in block, (
            "default_message_mode enum must contain {queue, interrupt, steer}"
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
            block = COMMANDS_JS[idx:idx + 250]
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
        body = COMMANDS_JS[idx:idx + 600]
        assert "if(!S.busy)" in body, "/queue must have an if(!S.busy) guard that routes to send()"

    def test_cmd_interrupt_calls_cancel_stream(self):
        idx = COMMANDS_JS.find("async function cmdInterrupt(")
        assert idx >= 0
        body = COMMANDS_JS[idx:idx + 1300]  # expanded: idle-fallback block added before the busy path
        assert "queueSessionMessage" in body, "/interrupt must queue the new message before cancelling"
        assert "cancelStream" in body, "/interrupt must call cancelStream() so the drain re-sends"

    def test_cmd_steer_delegates_to_try_steer(self):
        """/steer delegates to _trySteer which calls /api/chat/steer with
        a non-destructive fallback. The fallback path is exercised by tests
        in test_real_steer.py — this test just pins the delegation."""
        idx = COMMANDS_JS.find("async function cmdSteer(")
        assert idx >= 0
        body = COMMANDS_JS[idx:idx + 800]
        # cmdSteer delegates to _trySteer; fallback must not queue+cancel.
        assert "_trySteer" in body, "cmdSteer must call _trySteer to use the real /api/chat/steer endpoint"
        # The shared helper must contain the non-destructive fallback path.
        helper_idx = COMMANDS_JS.find("async function _trySteer(")
        assert helper_idx >= 0, "_trySteer helper must exist"
        helper_body = _source_between(COMMANDS_JS, "async function _trySteer(", "\nasync function cmdTitle")
        assert "queueSessionMessage" not in helper_body
        assert "cancelStream" not in helper_body
        assert "inp.value" in helper_body
        assert "if(result&&result.accepted)" in helper_body
        assert "S.pendingFiles=_remaining" in helper_body
        # Toast should differ from interrupt to signal it's the steer path
        assert "_steerFailureMessageKey" in helper_body or "steer_fail_" in helper_body


# ── send() busy branch ───────────────────────────────────────────────────

    def test_slash_commands_clear_pending_files(self):
        """Queue/interrupt clear S.pendingFiles after enqueuing; steer failure
        preserves staged files so the user can choose the next explicit action.

        cmdQueue and cmdInterrupt call queueSessionMessage themselves and clear
        S.pendingFiles directly. cmdSteer delegates to _trySteer. _trySteer no
        longer clears files on failure because it no longer falls back to
        cancel-and-queue behavior.
        """
        # cmdQueue and cmdInterrupt clear pendingFiles directly
        for fn_name in ("cmdQueue", "cmdInterrupt"):
            idx = COMMANDS_JS.find(f"function {fn_name}(")
            assert idx >= 0, f"{fn_name} not found"
            body = COMMANDS_JS[idx:idx + 800]
            assert "S.pendingFiles=[]" in body, (
                f"{fn_name} must clear S.pendingFiles after queueSessionMessage"
            )
            assert "renderTray()" in body, (
                f"{fn_name} must call renderTray() after clearing pendingFiles"
            )
        # cmdSteer delegates to _trySteer; the helper clears files only on
        # accepted steer, and (post-#5459-gate) removes ONLY the delivered files
        # by identity so files staged during the upload await are preserved. The
        # fallback path restores the draft and keeps staged files available.
        try_body = _source_between(COMMANDS_JS, "async function _trySteer(", "\nasync function cmdTitle")
        accepted_idx = try_body.find("if(result&&result.accepted)")
        failure_idx = try_body.find("// Do not fall back to interrupt")
        # Identity-based removal of the delivered snapshot on accepted steer.
        clear_idx = try_body.find("S.pendingFiles=_remaining", accepted_idx)
        assert accepted_idx >= 0, "_trySteer must branch on accepted steer responses"
        assert clear_idx > accepted_idx, "accepted steer should clear the delivered staged files"
        assert "_delivered=new Set(pendingFilesSnapshot)" in try_body, (
            "accepted steer must remove only the delivered files by identity, preserving newly staged ones"
        )
        assert failure_idx > clear_idx, "staged files must not be cleared in the failure path"
        assert "renderTray()" in try_body[failure_idx:]


class TestBusySendButton:
    """The composer send button must remain usable for busy-input actions."""

    def test_update_send_btn_uses_single_primary_action_button(self):
        idx = UI_JS.find("function updateSendBtn()")
        assert idx >= 0, "updateSendBtn() not found"
        body = UI_JS[idx:UI_JS.find("function setBusy", idx)]
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

    def test_composer_primary_action_accounts_for_all_default_message_modes(self):
        idx = UI_JS.find("function getComposerPrimaryAction()")
        assert idx >= 0, "getComposerPrimaryAction() not found"
        body = UI_JS[idx:UI_JS.find("function _setComposerPrimaryButtonIcon", idx)]
        assert "return 'stop'" in body, "busy/no-draft + active stream must map to stop"
        assert "return 'queue'" in body, "queue mode and unavailable steer/interrupt fallbacks must map to queue"
        assert "return 'interrupt'" in body, "interrupt mode with an active stream must map to interrupt"
        assert "return 'steer'" in body, "steer mode with active stream support must map to steer"
        assert "window._defaultMessageMode||'steer'" in body, "helper must respect the Default message mode setting"
        assert "_getExplicitBusyCommandAction(msg&&msg.value)" in body, (
            "explicit /queue, /interrupt, and /steer drafts must override the Default message mode for button visuals"
        )

    def test_explicit_busy_commands_override_button_visual_action(self):
        idx = UI_JS.find("function _getExplicitBusyCommandAction(")
        assert idx >= 0, "_getExplicitBusyCommandAction() not found"
        body = UI_JS[idx:UI_JS.find("function getComposerPrimaryAction", idx)]
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

    def test_send_refreshes_primary_button_after_clearing_active_stream_id(self):
        """send() must call updateSendBtn after resetting activeStreamId for a new turn.

        getComposerPrimaryAction maps to Stop only when S.activeStreamId is set; after
        nulling the id, btnSend must refresh so a stale Stop icon cannot linger until
        the next composer input event.
        """
        send_start = MESSAGES_JS.find("async function send(")
        assert send_start >= 0, "send() not found in messages.js"
        send_end = MESSAGES_JS.find("const LIVE_STREAMS={}", send_start)
        assert send_end > send_start, "could not find end of send() body"
        send_body = MESSAGES_JS[send_start:send_end]
        marker = "S.activeStreamId = null;  // will be set after stream starts"
        mpos = send_body.find(marker)
        assert mpos >= 0, "send() must reset activeStreamId before chat/start"
        window = send_body[mpos : mpos + 200]
        assert "updateSendBtn" in window, (
            "send() must call updateSendBtn() after clearing activeStreamId "
            "so btnSend state matches the pending-start phase"
        )

    def test_send_refreshes_primary_button_after_chat_start_stream_id(self):
        """send() must call updateSendBtn after streamId is assigned and before attachLiveStream.

        setBusy(true) runs before the API request with activeStreamId still null.  The
        Stop affordance must be refreshed as soon as we have streamId so it cannot be
        skipped by optional post-start UI failures.
        """
        send_start = MESSAGES_JS.find("async function send(")
        assert send_start >= 0, "send() not found in messages.js"
        send_end = MESSAGES_JS.find("const LIVE_STREAMS={}", send_start)
        assert send_end > send_start, "could not find end of send() body"
        send_body = MESSAGES_JS[send_start:send_end]
        api_idx = send_body.find("const startData=await api('/api/chat/start'")
        assert api_idx >= 0, "send() should issue /api/chat/start"
        catch_idx = send_body.find("}catch(e){", api_idx)
        assert catch_idx >= 0, "send() should have API error catch after /api/chat/start"
        assign = "S.activeStreamId = streamId;"
        apos = send_body.find(assign)
        assert apos >= 0, "send() must assign S.activeStreamId from startData"
        assert apos > catch_idx, (
            "send() must assign S.activeStreamId only after /api/chat/start succeeds"
        )
        update_idx = send_body.find("updateSendBtn();", apos)
        assert update_idx >= 0, "send() must call updateSendBtn() after assigning streamId"
        assert update_idx > apos, (
            "send() should call updateSendBtn() after S.activeStreamId is assigned"
        )
        attach_idx = send_body.find("attachLiveStream(activeSid, streamId, uploadedNames);")
        assert attach_idx > update_idx, (
            "send() should refresh primary button before opening SSE stream attach"
        )
        optional_idx = send_body.find("_runOptionalPostStartUiStep('post-start ui/bookkeeping'", apos)
        assert optional_idx >= 0, "send() should run optional post-start UI/bookkeeping"
        assert optional_idx > update_idx, (
            "send() must call updateSendBtn() before entering optional post-start helper so optional failures "
            "cannot skip it."
        )


class TestSendBusyBranchDispatch:
    """send()'s busy block must read window._defaultMessageMode and branch accordingly."""

    def test_send_reads_default_message_mode(self):
        # The send() function should read window._defaultMessageMode in the busy block
        send_idx = MESSAGES_JS.find("async function send(")
        assert send_idx >= 0
        # Bound the window to the actual send() body (up to the LIVE_STREAMS decl
        # that immediately follows it) rather than an arbitrary char count, so
        # unrelated additions near the top of send() don't push the assertion
        # target out of a fixed window (#5472 caught this).
        send_end = MESSAGES_JS.find("const LIVE_STREAMS=", send_idx)
        assert send_end > send_idx, "could not bound send() body"
        send_body = MESSAGES_JS[send_idx:send_end]
        assert "_defaultMessageMode" in send_body, (
            "send() must read window._defaultMessageMode in the S.busy branch"
        )

    def test_send_calls_cancel_stream_on_interrupt(self):
        send_idx = MESSAGES_JS.find("async function send(")
        assert send_idx >= 0
        send_end = MESSAGES_JS.find("const LIVE_STREAMS=", send_idx)
        assert send_end > send_idx, "could not bound send() body"
        send_body = MESSAGES_JS[send_idx:send_end]
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

    def test_send_busy_steer_preserves_files_when_steer_not_delivered(self):
        """Busy-mode steer must only clear staged files after delivered steer.

        A failed steer restores the draft and leaves the active stream running,
        so staged files must remain available for the user's next explicit
        Queue or Interrupt action.
        """
        send_idx = MESSAGES_JS.find("async function send(")
        assert send_idx >= 0, "send() not found"
        steer_idx = MESSAGES_JS.find("defaultMessageMode==='steer'", send_idx)
        assert steer_idx >= 0, "busy steer branch not found"
        branch_end = MESSAGES_JS.find("} else if(defaultMessageMode==='interrupt')", steer_idx)
        assert branch_end > steer_idx, "busy steer branch end not found"
        branch = MESSAGES_JS[steer_idx:branch_end]
        assert "await _trySteer(text, /*explicitSteer=*/false)" in branch
        assert "_trySteer captures the owner session/files before awaiting uploads" in branch
        assert "_trySteer clears staged files only after /api/chat/steer accepts" in branch
        assert "_clearComposerDraft(S.session.session_id,text" not in branch
        try_body = _source_between(COMMANDS_JS, "async function _trySteer(", "\nasync function cmdTitle")
        accepted_idx = try_body.find("if(result&&result.accepted)")
        failure_idx = try_body.find("// Do not fall back to interrupt")
        clear_idx = try_body.find("S.pendingFiles=_remaining", accepted_idx)
        assert accepted_idx >= 0 and clear_idx > accepted_idx
        assert "_clearComposerDraft(ownerSid,_steerRestoreText(originalMsg,explicitSteer),pendingFilesSnapshot)" in try_body
        assert failure_idx > clear_idx, "failed steer must leave staged files intact"

    def test_reentrant_send_does_not_queue_staged_files_while_steer_uploads(self):
        """Repeated Enter during steer upload must not double-send staged files.

        _trySteer uploads with clearPending=false, so S.pendingFiles intentionally
        stays populated until the steer endpoint accepts. The early reentrant
        guard must therefore be text-only; file-only busy submissions are handled
        by the normal busy branch after the first send call owns the turn.
        """
        send_idx = MESSAGES_JS.find("async function send(")
        assert send_idx >= 0, "send() not found"
        guard_start = MESSAGES_JS.find("if (_sendInProgress)", send_idx)
        guard_end = MESSAGES_JS.find("_sendInProgress = true", guard_start)
        assert guard_start >= 0 and guard_end > guard_start, "send() reentrant guard not found"
        guard = MESSAGES_JS[guard_start:guard_end]
        assert "if(_text && _targetSid)" in guard
        assert "S.pendingFiles.length" not in guard
        assert "files:[...S.pendingFiles]" in guard

    def test_steer_upload_is_cached_and_delivered_files_removed_by_identity(self):
        """#5459 gate fixes: (1) a failed-steer RETRY reuses the cached upload
        instead of re-uploading the same File objects; (2) accepted steer removes
        ONLY the delivered files by identity, preserving files staged during the
        upload/API await."""
        try_body = _source_between(COMMANDS_JS, "async function _steerTextWithPendingFiles(", "\nasync function cmdTitle")
        # (1) upload cache keyed by session + file signature, reused on retry,
        # invalidated on delivery.
        assert "_steerUploadCache" in try_body
        assert "_steerFilesSignature(" in try_body
        assert "_steerUploadCache={sid:ownerSid,sig,paths}" in try_body
        assert "_steerUploadCache=null" in try_body
        # (2) identity-based removal of only the delivered snapshot.
        assert "_delivered=new Set(pendingFilesSnapshot)" in try_body
        assert "S.pendingFiles=_remaining" in try_body


    def test_slash_commands_intercepted_before_busymode_routing(self):
        """Busy-control slash commands (/steer /interrupt /queue /yolo) must be
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
        intercept_idx = MESSAGES_JS.find("'steer','interrupt','queue','terminal','goal','yolo'", busy_start)
        busymode_idx = MESSAGES_JS.find("_defaultMessageMode||'steer'", busy_start)
        assert intercept_idx >= 0, (
            "send() must intercept /steer /interrupt /queue /terminal /goal /yolo before the busyMode "
            "routing block — otherwise they queue instead of executing immediately"
        )
        assert intercept_idx < busymode_idx, (
            "The slash-command intercept must come BEFORE the busyMode routing "
            "so /steer executes while the agent is running, not after the turn ends"
        )
        intercept_block = MESSAGES_JS[intercept_idx:busymode_idx]
        assert "'yolo'" in intercept_block, (
            "The busy-mode slash-command allowlist must include /yolo"
        )

    def test_steer_intercept_calls_handler_directly(self):
        """The busy-intercept must dispatch via _bc.fn(_pc.args), not queue the text."""
        send_idx = MESSAGES_JS.find("async function send(")
        busy_start = MESSAGES_JS.find("S.busy||compressionRunning", send_idx)
        intercept_idx = MESSAGES_JS.find("'steer','interrupt','queue','terminal','goal','yolo'", busy_start)
        assert intercept_idx >= 0
        # Get the intercept block (up to the next busyMode assignment)
        busymode_idx = MESSAGES_JS.find("_defaultMessageMode||'steer'", busy_start)
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
        intercept_idx = MESSAGES_JS.find("'steer','interrupt','queue','terminal','goal','yolo'", busy_start)
        busymode_idx = MESSAGES_JS.find("_defaultMessageMode||'steer'", busy_start)
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
        """Boot success path initialises window._defaultMessageMode from settings.

        #5167/#5170: the assignment routes through _persistDefaultMessageMode()
        so the resolved value is mirrored into localStorage (the synchronous
        source the eager default reads) while still being the single source of
        truth once /api/settings resolves. #5145 renamed the setting and reads
        the legacy busy_input_mode key as a back-compat fallback.
        """
        assert "window._defaultMessageMode=_persistDefaultMessageMode(s.default_message_mode||s.busy_input_mode)" in BOOT_JS

    def test_boot_init_fallback_path(self):
        """Boot fallback path (settings load failed) keeps the persisted preference.

        #5167/#5170: instead of hard-clobbering to a default, the catch path
        re-reads the persisted mirror so a saved 'steer'/'interrupt'/'queue'
        still applies when the server is unreachable. This must NOT regress to
        a hardcoded default that ignores the saved choice.
        """
        assert "window._defaultMessageMode=_readPersistedDefaultMessageMode()" in BOOT_JS

    def test_panels_load_save_apply(self):
        assert "settingsDefaultMessageMode" in PANELS_JS, "panels.js must load the setting"
        assert "body.default_message_mode" in PANELS_JS, "saveSettings must include default_message_mode in body"
        assert "_persistDefaultMessageMode(body.default_message_mode||body.busy_input_mode)" in PANELS_JS, (
            "_applySavedSettingsUi must propagate default_message_mode to the global "
            "and persist it (so the next reload's eager default honors the save) (#5167/#5170)"
        )

    def test_index_html_dropdown_has_three_options(self):
        idx = INDEX_HTML.find('id="settingsDefaultMessageMode"')
        assert idx >= 0
        block = INDEX_HTML[idx:idx + 800]
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
        "settings_label_default_message_mode",
        "settings_desc_default_message_mode",
        "settings_default_message_mode_queue",
        "settings_default_message_mode_interrupt",
        "settings_default_message_mode_steer",
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
        assert total >= 17 * 6, (
            f"Total i18n occurrences = {total}; expected ≥ {17*6}"
        )
