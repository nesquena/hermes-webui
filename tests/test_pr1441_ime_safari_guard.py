"""Regression tests for PR #1441 — IME composition Enter on Safari + broader IME coverage.

Original guard was `e.isComposing` only, which fails on Safari where the committing
keydown for IME composition fires AFTER `compositionend` with `isComposing=false`.
PR #1441 adds two more guards (`keyCode===229` + manual `_imeComposing` flag).

These tests pin the structural shape of the helper so a future cleanup pass that
strips one of the three guards trips a test before shipping.
"""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")


def test_ime_helper_function_exists():
    """The `_isImeEnter` helper must exist and combine all 3 guards."""
    # Helper definition — single function, three guards joined by ||
    pattern = re.compile(
        r"function\s+_isImeEnter\s*\(\s*e\s*\)\s*\{[^}]*"
        r"e\.isComposing"
        r"[^}]*"
        r"e\.keyCode\s*===\s*229"
        r"[^}]*"
        r"_imeComposing"
        r"[^}]*\}",
        re.DOTALL,
    )
    assert pattern.search(BOOT_JS), (
        "_isImeEnter helper must combine e.isComposing, keyCode===229, and "
        "_imeComposing flag (PR #1441)"
    )


def test_compositionstart_sets_manual_flag():
    """A compositionstart listener on #msg must set _imeComposing = true."""
    # The listener registers on the resolved `_c` (i.e. $('msg')) element
    pattern = re.compile(
        r"compositionstart['\"]\s*,\s*(?:\(\s*\)|function\s*\(\s*\))\s*=>?\s*\{?\s*"
        r"_imeComposing\s*=\s*true",
        re.DOTALL,
    )
    assert pattern.search(BOOT_JS), (
        "compositionstart listener must set _imeComposing = true (PR #1441)"
    )


def test_compositionend_resets_flag_on_next_tick():
    """compositionend must reset _imeComposing in a setTimeout(..., 0) — NOT
    synchronously — so Safari's trailing committing-Enter keydown is still
    swallowed (it fires AFTER compositionend).
    """
    pattern = re.compile(
        r"compositionend['\"]\s*,\s*(?:\(\s*\)|function\s*\(\s*\))\s*=>?\s*\{?\s*"
        r"setTimeout\s*\(\s*(?:\(\s*\)|function\s*\(\s*\))\s*=>?\s*\{?\s*"
        r"_imeComposing\s*=\s*false",
        re.DOTALL,
    )
    assert pattern.search(BOOT_JS), (
        "compositionend listener must reset _imeComposing in setTimeout(..., 0) "
        "to handle Safari's post-compositionend trailing Enter (PR #1441)"
    )


def test_blur_resets_imecomposing_flag():
    """The blur listener must also reset _imeComposing so the flag cannot get
    stuck at true if compositionend never fires (focus loss / window blur
    with some IME implementations). Without this, a single missed
    compositionend would brick Enter-to-send until the page is reloaded.

    Added in v0.50.264 stage review per Opus advisor recommendation.
    """
    pattern = re.compile(
        r"['\"]blur['\"]\s*,\s*(?:\(\s*\)|function\s*\(\s*\))\s*=>?\s*\{?\s*"
        r"_imeComposing\s*=\s*false",
        re.DOTALL,
    )
    assert pattern.search(BOOT_JS), (
        "blur listener must reset _imeComposing = false to recover from "
        "missed compositionend (Opus follow-up to PR #1441)"
    )


def test_ime_listeners_null_guard_msg_lookup():
    """The IIFE that registers composition listeners must null-guard $('msg') so
    boot.js does not throw on pages that don't have a #msg textarea (e.g. login,
    onboarding).
    """
    # The IIFE pattern: (()=>{const _c=$('msg');if(!_c)return; ...
    pattern = re.compile(
        r"\(\s*\(\s*\)\s*=>\s*\{\s*const\s+_c\s*=\s*\$\(\s*['\"]msg['\"]\s*\)\s*;\s*"
        r"if\s*\(\s*!\s*_c\s*\)\s*return\s*;",
        re.DOTALL,
    )
    assert pattern.search(BOOT_JS), (
        "Composition-listener IIFE must null-guard $('msg') so non-chat pages "
        "(login, onboarding) don't throw (PR #1441)"
    )


def test_chat_send_enter_uses_helper():
    """The send-Enter path must call _isImeEnter(e), not e.isComposing."""
    # The original was `if(e.isComposing){return;}` inside `if(e.key==='Enter')`.
    # Now it must be `if(_isImeEnter(e)){return;}`.
    pattern = re.compile(
        r"if\s*\(\s*e\.key\s*===\s*['\"]Enter['\"]\s*\)\s*\{\s*"
        r"if\s*\(\s*_isImeEnter\s*\(\s*e\s*\)\s*\)\s*",
        re.DOTALL,
    )
    assert pattern.search(BOOT_JS), (
        "Chat composer send-Enter path must use _isImeEnter(e) helper (PR #1441)"
    )


def test_dropdown_enter_uses_helper():
    """The autocomplete-dropdown Enter path must also use _isImeEnter(e).

    Otherwise IME-confirming Enter inside a slash-command dropdown would
    select the highlighted item instead of just committing the IME candidate.
    """
    pattern = re.compile(
        r"if\s*\(\s*e\.key\s*===\s*['\"]Enter['\"]\s*&&\s*!\s*e\.shiftKey\s*\)\s*\{\s*"
        r"if\s*\(\s*_isImeEnter\s*\(\s*e\s*\)\s*\)\s*",
        re.DOTALL,
    )
    assert pattern.search(BOOT_JS), (
        "Command-dropdown Enter path must use _isImeEnter(e) helper (PR #1441)"
    )
