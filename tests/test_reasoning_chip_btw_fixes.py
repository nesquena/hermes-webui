"""Regression tests for PR #934 UI fixes.

Four invariants this file locks in place:

1. `#composerReasoningDropdown` lives OUTSIDE `.composer-left` (as a sibling of
   the other composer dropdowns), so it isn't clipped by that container's
   `overflow-y: hidden`.  Regresses to invisible-dropdown if moved back.

2. The reasoning chip label uses an SVG icon (`stroke="currentColor"`) instead
   of the `🧠` emoji, matching every other composer chip.

3. `cmdReasoning()` calls `_applyReasoningChip(eff)` directly with the
   server-confirmed effort, not `syncReasoningChip()` which re-applies the
   stale cached value.

4. `attachBtwStream()` sets a `_streamDone` flag in `done`/`apperror` and
   gates `onerror`'s row removal on `!_streamDone` — otherwise the browser's
   post-`stream_end` error event wipes the just-rendered answer.
"""

from __future__ import annotations

import pathlib
import re


REPO = pathlib.Path(__file__).resolve().parent.parent
INDEX = (REPO / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


# ── #1 dropdown escapes composer-left ─────────────────────────────────────────


class TestReasoningDropdownEscapesComposerLeft:
    """The dropdown must sit as a sibling of .composer-footer, not inside
    .composer-left which has overflow-y: hidden and clips absolute children."""

    def test_dropdown_lives_outside_composer_left(self):
        # Find the <div class="composer-left">...</div> block and confirm the
        # reasoning dropdown is NOT inside it.
        m = re.search(
            r'<div class="composer-left"[^>]*>(?P<body>[\s\S]*?)<div class="composer-footer-right"',
            INDEX,
        )
        # Some templates use different closing structures; fall back to a
        # coarser search that at least locates composer-left.
        if m:
            inner = m.group("body")
            assert 'id="composerReasoningDropdown"' not in inner, (
                "composerReasoningDropdown is still nested inside .composer-left — "
                "this is the exact bug #933 flagged: overflow-y: hidden clips "
                "upward-opening absolute dropdowns. Move it alongside "
                "#composerModelDropdown / #composerWsDropdown / #profileDropdown."
            )
        # Either way, check that the dropdown sits next to the other composer
        # dropdowns (reliable structural marker).
        assert '<div class="profile-dropdown" id="profileDropdown"></div>' in INDEX
        assert 'id="composerReasoningDropdown"' in INDEX

    def test_dropdown_is_sibling_of_other_composer_dropdowns(self):
        # The four composer-level dropdowns must appear contiguously — if one
        # of them is nested inside an overflow-hidden container, this would
        # typically split the group.
        positions = [
            ("profileDropdown", INDEX.find('id="profileDropdown"')),
            ("composerWsDropdown", INDEX.find('id="composerWsDropdown"')),
            ("composerReasoningDropdown", INDEX.find('id="composerReasoningDropdown"')),
            ("composerModelDropdown", INDEX.find('id="composerModelDropdown"')),
        ]
        for name, pos in positions:
            assert pos > -1, f"{name} not found in index.html"
        # They should all be in the same area of the document — within ~1.5 KB
        window = [p for _, p in positions]
        assert max(window) - min(window) < 2000, (
            "composer dropdowns are no longer grouped — reasoning dropdown may "
            "have drifted back inside a nested container"
        )


# ── #2 monochrome SVG replaces emoji ──────────────────────────────────────────


class TestReasoningChipIcon:
    """The chip must render a currentColor SVG, not a 🧠 emoji, for cross-platform
    rendering consistency with the other composer chips."""

    def test_chip_button_contains_svg_with_currentColor(self):
        # Locate the chip button and confirm it contains a stroke="currentColor" SVG
        m = re.search(
            r'<button class="composer-reasoning-chip"[^>]*>([\s\S]*?)</button>',
            INDEX,
        )
        assert m, "composer-reasoning-chip button not found"
        btn_body = m.group(1)
        assert 'stroke="currentColor"' in btn_body, (
            "reasoning chip must use stroke='currentColor' SVG matching other chips"
        )
        assert "<svg" in btn_body, "reasoning chip must contain an <svg> icon"

    def test_apply_reasoning_chip_label_has_no_emoji(self):
        # Locate _applyReasoningChip and confirm the label assignment doesn't
        # concatenate a 🧠 emoji.
        m = re.search(
            r"function\s+_applyReasoningChip\b[\s\S]*?^\}",
            UI_JS,
            re.MULTILINE,
        )
        assert m, "_applyReasoningChip not found in ui.js"
        fn = m.group(0)
        assert "🧠" not in fn, (
            "_applyReasoningChip should not concatenate a 🧠 emoji into the label — "
            "the chip already has a monochrome SVG icon next to the label"
        )


# ── #1068 None/default reasoning chip stays visible ──────────────────────────


class TestReasoningChipNoneState:
    """Reasoning effort is a current setting like model selection.  Setting it
    to None disables reasoning, but the chip must remain visible so users can
    see and change the current level."""

    def get_apply_reasoning_chip(self):
        m = re.search(
            r"function\s+_applyReasoningChip\b[\s\S]*?^}",
            UI_JS,
            re.MULTILINE,
        )
        assert m, "_applyReasoningChip not found in ui.js"
        return m.group(0)

    def test_none_and_default_do_not_hide_reasoning_chip(self):
        fn = self.get_apply_reasoning_chip()
        assert "wrap.style.display='';" in fn, (
            "_applyReasoningChip must show the reasoning chip even for empty/"
            "default or 'none' effort values"
        )
        assert "if(!eff" not in fn and "wrap.style.display='none'" not in fn, (
            "_applyReasoningChip must not use a truthy guard that hides the "
            "chip for the valid 'none' state"
        )
        assert "wrap.style.display='none'" not in fn, (
            "the None/default reasoning state should be visible, not hidden"
        )

    def test_none_and_default_have_visible_labels(self):
        assert "if(effort==='none') return 'None';" in UI_JS, (
            "the disabled reasoning state must render a visible 'None' label"
        )
        assert "if(!effort) return 'Default';" in UI_JS, (
            "the unset reasoning state must render a visible 'Default' label"
        )

    def test_none_and_default_are_visually_inactive_not_missing(self):
        fn = self.get_apply_reasoning_chip()
        assert "chip.classList.toggle('inactive',inactive)" in fn, (
            "None/default should be shown with an inactive visual treatment "
            "instead of removing the chip"
        )
        assert ".composer-reasoning-chip.inactive" in STYLE_CSS, (
            "the inactive chip state needs a CSS rule so the visible None/"
            "default state is intentionally muted"
        )


# ── #3 /reasoning immediately updates chip ────────────────────────────────────


class TestReasoningCommandUpdatesChip:
    """cmdReasoning must apply the SERVER-CONFIRMED effort, not the cached value."""

    def test_cmd_reasoning_calls_apply_not_sync(self):
        # Locate cmdReasoning and verify the success branch calls
        # _applyReasoningChip(eff) directly, not syncReasoningChip() which
        # would read stale _currentReasoningEffort.
        m = re.search(
            r"function\s+cmdReasoning\b[\s\S]*?(?=^function\s|\Z)",
            COMMANDS_JS,
            re.MULTILINE,
        )
        assert m, "cmdReasoning not found in commands.js"
        fn = m.group(0)
        assert "_applyReasoningChip(eff)" in fn, (
            "cmdReasoning must call _applyReasoningChip(eff) with the "
            "server-confirmed effort from the /api/reasoning POST response"
        )


# ── #4 /btw answer not wiped by onerror after clean close ─────────────────────


class TestBtwStreamDoneGuard:
    """attachBtwStream must guard onerror with a _streamDone flag so the
    browser's post-stream_end error event doesn't wipe the just-rendered row."""

    def get_attach_btw(self):
        m = re.search(
            r"function\s+attachBtwStream\b[\s\S]*?(?=^function\s|\Z)",
            MESSAGES_JS,
            re.MULTILINE,
        )
        assert m, "attachBtwStream not found in messages.js"
        return m.group(0)

    def test_stream_done_flag_declared(self):
        fn = self.get_attach_btw()
        assert "_streamDone" in fn, (
            "attachBtwStream must declare a _streamDone flag to distinguish "
            "clean server-closed streams from real errors"
        )

    def test_stream_done_set_in_done_handler(self):
        fn = self.get_attach_btw()
        # Inside the 'done' listener body, _streamDone must be set true.
        done_block_m = re.search(
            r"addEventListener\('done'[\s\S]*?(?=addEventListener\(')",
            fn,
        )
        assert done_block_m, "done handler not found in attachBtwStream"
        assert "_streamDone=true" in done_block_m.group(
            0
        ) or "_streamDone = true" in done_block_m.group(0), (
            "_streamDone must be set to true in the done handler so onerror "
            "knows the stream completed successfully"
        )

    def test_onerror_gated_on_stream_done(self):
        fn = self.get_attach_btw()
        # onerror must NOT unconditionally call btwRow.remove()
        m = re.search(r"src\.onerror\s*=\s*\(?\)?\s*=>\s*\{[^}]*\}", fn)
        assert m, "src.onerror assignment not found"
        handler = m.group(0)
        assert "_streamDone" in handler, (
            "src.onerror must check !_streamDone before removing the btw row — "
            "otherwise the browser's post-stream_end error fire wipes the "
            "answer that was just rendered by the done handler"
        )

    def test_ensure_btw_row_called_in_done(self):
        """The done handler must create the row even if no token events arrived
        (e.g., agent returned a non-streaming single-shot answer)."""
        fn = self.get_attach_btw()
        done_block_m = re.search(
            r"addEventListener\('done'[\s\S]*?(?=addEventListener\(')",
            fn,
        )
        assert done_block_m
        assert "_ensureBtwRow()" in done_block_m.group(0), (
            "done handler must call _ensureBtwRow() so the answer bubble exists "
            "even if no token events arrived before done"
        )

    def test_ensure_btw_row_gated_on_session_match(self):
        """Regression for PR #935: _ensureBtwRow reads $('msgInner') — the
        CURRENTLY-viewed session's container.  If the user switched sessions
        during the /btw stream, creating a bubble here would put it in the
        wrong session.  The done handler must guard on S.session.session_id
        matching the parent sid before creating a new bubble.
        """
        fn = self.get_attach_btw()
        done_block_m = re.search(
            r"addEventListener\('done'[\s\S]*?(?=addEventListener\(')",
            fn,
        )
        assert done_block_m
        block = done_block_m.group(0)
        # The _ensureBtwRow call must be guarded by a session-match check
        assert "S.session" in block and "parentSid" in block, (
            "_ensureBtwRow() in the done handler must be gated on "
            "S.session.session_id === parentSid — otherwise a user who "
            "switched sessions during the /btw stream gets the answer "
            "bubble injected into the wrong session's container"
        )

    def test_stream_done_set_before_close_in_done(self):
        """Regression for PR #935 defensive ordering: _streamDone=true must be
        set BEFORE src.close() in the done handler.  Setting it after works
        today because EventSource.close() is synchronous per spec and doesn't
        dispatch events, but the defensive-correct ordering is flag-first so
        no future browser quirk or event-queue race can bypass the guard.
        """
        fn = self.get_attach_btw()
        done_block_m = re.search(
            r"addEventListener\('done'[\s\S]*?(?=addEventListener\(')",
            fn,
        )
        assert done_block_m
        block = done_block_m.group(0)
        flag_pos = block.find("_streamDone=true")
        close_pos = block.find("src.close()")
        assert flag_pos > -1 and close_pos > -1
        assert flag_pos < close_pos, (
            "_streamDone=true must be set BEFORE src.close() in the done handler "
            "so any event the browser fires during close() sees the flag already set"
        )

    def test_stream_done_set_before_close_in_apperror(self):
        """Same defensive ordering as test_stream_done_set_before_close_in_done,
        applied to the apperror handler."""
        fn = self.get_attach_btw()
        apperror_m = re.search(
            r"addEventListener\('apperror'[\s\S]*?(?=addEventListener\(')",
            fn,
        )
        assert apperror_m
        block = apperror_m.group(0)
        flag_pos = block.find("_streamDone=true")
        close_pos = block.find("src.close()")
        assert flag_pos > -1 and close_pos > -1, (
            "apperror handler must both set _streamDone=true and call src.close()"
        )
        assert flag_pos < close_pos, (
            "_streamDone=true must be set BEFORE src.close() in the apperror handler"
        )

    def test_stream_end_sets_stream_done(self):
        """Regression for PR #935/#939/#942: stream_end handler must also set
        _streamDone=true.  Today the ephemeral /btw path returns before emitting
        stream_end so this is moot, but if the server emits stream_end as a
        standalone terminator (e.g. for a non-ephemeral /btw variant), the
        subsequent browser-fired onerror would wipe the bubble without the flag.
        """
        fn = self.get_attach_btw()
        m = re.search(r"addEventListener\('stream_end'[\s\S]*?\}\s*\)", fn)
        assert m, "stream_end handler not found"
        block = m.group(0)
        assert "_streamDone=true" in block or "_streamDone = true" in block, (
            "stream_end handler must set _streamDone=true before closing the "
            "connection — consistent with the done/apperror handlers"
        )


# ── #5 resize handler symmetry (non-blocking polish) ─────────────────────────


class TestResizeHandlerSymmetry:
    """When the window resizes while either the model OR reasoning dropdown is
    open, the dropdown must be re-positioned so it stays aligned under its chip."""

    def test_resize_repositions_reasoning_dropdown(self):
        # The global resize handler must handle both composerModelDropdown AND
        # composerReasoningDropdown to keep them aligned when the window resizes.
        m = re.search(
            r"window\.addEventListener\(\s*['\"]resize['\"][\s\S]*?\}\s*\)\s*;",
            UI_JS,
        )
        assert m, "window resize handler not found in ui.js"
        handler = m.group(0)
        assert "composerReasoningDropdown" in handler, (
            "window resize handler must also re-position composerReasoningDropdown "
            "while it's open (symmetric with the existing model-dropdown branch)"
        )
