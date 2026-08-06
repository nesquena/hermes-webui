"""Regression tests for #5759 composer focus shortcut."""

from pathlib import Path


BOOT_JS = (Path(__file__).parent.parent / "static" / "boot.js").read_text(encoding="utf-8")
CHORD = "(e.metaKey||e.ctrlKey)&&!e.altKey&&e.key==='/'"
CTRL_K = "(e.metaKey||e.ctrlKey)&&e.key==='k'"


def _block_from(start: int) -> str:
    depth = 0
    end = start
    for end in range(start, len(BOOT_JS)):
        char = BOOT_JS[end]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return BOOT_JS[start : end + 1]
    raise AssertionError("shortcut block not closed")


def _composer_focus_branch() -> str:
    start = BOOT_JS.index(CHORD)
    return _block_from(start)


def _ctrl_k_branch() -> str:
    start = BOOT_JS.index(CTRL_K)
    return _block_from(start)


def test_composer_focus_chord_uses_cmd_ctrl_slash_and_stays_before_ctrl_k():
    assert CHORD in BOOT_JS
    # Must key off the '/' CHARACTER, never the physical Slash code — on QWERTZ
    # the Slash key produces Ctrl+- (browser zoom-out) and '/' is Shift+7.
    assert "e.code==='Slash'" not in BOOT_JS
    # No shiftKey exclusion — a layout-shifted '/' (e.g. Shift+7) must still match.
    assert "!e.shiftKey&&e.key==='/'" not in BOOT_JS
    assert BOOT_JS.index("(e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey&&(e.key==='b'||e.key==='B')") < BOOT_JS.index(CHORD)
    assert BOOT_JS.index(CHORD) < BOOT_JS.index(CTRL_K)


def test_editable_targets_return_before_prevent_default():
    branch = _composer_focus_branch()
    guard = "const isText=t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable);"
    assert "const t=e.target;" in branch
    assert guard in branch
    guard_idx = branch.index("if(isText) return;")
    prevent_idx = branch.index("e.preventDefault();")
    assert guard_idx < prevent_idx


def test_composer_focus_branch_only_focuses_msg_and_does_not_mutate_session_state():
    branch = _composer_focus_branch()
    assert "const composer=$('msg');" in branch
    assert "composer.focus();" in branch
    assert "newSession()" not in branch
    assert "renderSessionList()" not in branch
    assert "closeMobileSidebar()" not in branch
    assert "send()" not in branch
    assert "clearDraft" not in branch
    assert "draft" not in branch.lower()


def test_mode_matrix_covers_focus_return_shortcut_and_unchanged_ctrl_k():
    branch = _composer_focus_branch()
    ctrl_k = _ctrl_k_branch()

    assert "!e.altKey" in branch
    assert "if(composer){e.preventDefault();composer.focus();}" in branch
    assert "if(isText) return;" in branch
    assert "&&e.altKey&&!e.shiftKey&&(e.key==='/'||e.code==='Slash')" not in BOOT_JS

    assert "if(_currentSessionIsReusableEmptyChat()){" in ctrl_k
    assert "await newSession();await renderSessionList();closeMobileSidebar();$('msg').focus();" in ctrl_k
