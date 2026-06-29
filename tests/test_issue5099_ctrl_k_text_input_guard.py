"""Static-analysis tests for #5099 (Ctrl+K must not steal kill-line in text fields).

Emacs-adjacent users expect Ctrl+K to kill to end-of-line while the composer
or other editable fields are focused. Cmd/Ctrl+K should still create a new chat
when focus is outside text inputs, matching the existing Ctrl+B guard pattern.
"""
from pathlib import Path

BOOT_JS = (Path(__file__).parent.parent / "static" / "boot.js").read_text(encoding="utf-8")


def _ctrl_k_branch_window() -> str:
    idx = BOOT_JS.find("(e.metaKey||e.ctrlKey)&&e.key==='k'")
    assert idx >= 0, "Cmd/Ctrl+K handler not found in boot.js"
    return BOOT_JS[idx:idx + 900]


class TestIssue5099CtrlKTextInputGuard:
    def test_ctrl_k_skips_text_inputs(self):
        branch = _ctrl_k_branch_window()
        assert "tagName==='INPUT'" in branch
        assert "tagName==='TEXTAREA'" in branch
        assert "isContentEditable" in branch
        assert "if(isText) return" in branch

    def test_ctrl_k_prevent_default_after_text_guard(self):
        branch = _ctrl_k_branch_window()
        guard_idx = branch.find("if(isText) return")
        prevent_idx = branch.find("e.preventDefault()")
        assert guard_idx >= 0 and prevent_idx >= 0, (
            "Ctrl+K must guard text inputs before calling preventDefault()"
        )
        assert guard_idx < prevent_idx, (
            "preventDefault() must not run before the text-input early return"
        )

    def test_ctrl_k_guard_matches_ctrl_b_idiom(self):
        ctrl_b_idx = BOOT_JS.find("(e.key==='b'||e.key==='B')")
        assert ctrl_b_idx >= 0, "Ctrl+B handler not found in boot.js"
        ctrl_b_block = BOOT_JS[max(0, ctrl_b_idx - 250):ctrl_b_idx + 120]
        ctrl_k_block = _ctrl_k_branch_window()
        for needle in (
            "const t=e.target",
            "const isText=t&&",
            "tagName==='INPUT'",
            "tagName==='TEXTAREA'",
            "isContentEditable",
        ):
            assert needle in ctrl_b_block, f"Ctrl+B guard missing {needle!r}"
            assert needle in ctrl_k_block, f"Ctrl+K guard missing {needle!r}"

    def test_ctrl_k_still_creates_new_session_outside_inputs(self):
        branch = _ctrl_k_branch_window()
        assert "newSession()" in branch
        assert "closeMobileSidebar()" in branch
