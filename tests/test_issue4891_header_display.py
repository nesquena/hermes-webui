"""Tests for PR #4891 — header display management in detail views.

Verifies that all five _set*HeaderButtons functions explicitly manage
header.style.display to bypass the :has(.main-view-title:empty) CSS
re-evaluation bug on mobile PWA.
"""

def _function_body(name: str) -> str:
    with open('static/panels.js') as f:
        src = f.read()
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    paren = src.find("(", start)
    assert paren != -1, f"{name} params not found"
    depth = 0
    for idx in range(paren, len(src)):
        ch = src[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                brace = src.find("{", idx)
                break
    else:
        raise AssertionError(f"{name} params did not terminate")
    assert brace != -1, f"{name} body not found"
    depth = 0
    close = None
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                close = idx
                break
    assert close is not None, f"{name} body did not close"
    return src[brace:close + 1]


HEADER_FUNCTIONS = [
    "_setCronHeaderButtons",
    "_setSkillHeaderButtons",
    "_setMemoryHeaderButtons",
    "_setWorkspaceHeaderButtons",
    "_setProfileHeaderButtons",
]


class TestHeaderDisplayManagement:

    def test_all_functions_have_header_ref(self):
        """Each function should declare a header const."""
        for fn in HEADER_FUNCTIONS:
            body = _function_body(fn)
            assert "header.style.display" in body, (
                f"{fn} is missing header.style.display management"
            )

    def test_read_branch_uses_flex(self):
        """All five functions should set display='flex' in read mode."""
        for fn in HEADER_FUNCTIONS:
            body = _function_body(fn)
            # Check for the pattern in read-branch: 'read' followed by 'flex'
            read_idx = body.find("'read'")
            if read_idx == -1:
                read_idx = body.find('"read"')
            assert read_idx != -1, f"{fn} has no 'read' branch"
            # Find 'flex' after the read branch
            flex_idx = body.find("'flex'", read_idx)
            none_idx = body.find("'none'", read_idx)
            assert flex_idx != -1, (
                f"{fn} read branch does not set header.style.display = 'flex'"
            )
            # flex should come before 'none' in the read branch
            if none_idx != -1:
                assert flex_idx < none_idx, (
                    f"{fn} read branch: 'flex' should appear before 'none'"
                )

    def test_empty_branch_uses_none(self):
        """All five functions should set display='none' in empty/else branch."""
        for fn in HEADER_FUNCTIONS:
            body = _function_body(fn)
            # Check last branch (empty) has 'none'
            last_else = body.rfind("else {")
            if last_else == -1:
                last_else = body.rfind("else{")
            if last_else == -1:
                # Some functions use } else {
                last_else = body.rfind("}else{")
            assert last_else != -1, f"{fn} has no else branch"
            # Find 'none' after the else
            rest = body[last_else:]
            assert "'none'" in rest, (
                f"{fn} else branch does not set header.style.display = 'none'"
            )
