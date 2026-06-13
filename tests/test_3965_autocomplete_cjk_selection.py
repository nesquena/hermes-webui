"""Regression tests for PR #3965: autocomplete CJK prefix + selection preservation.

Covers:
- _parseSlashAutocomplete carries beforeSlash on all return paths (commands + subargs)
- getSlashAutocompleteMatches assigns module-level state vars (P1-1 fix)
- onmousedown has prefix-preserving reassembly for both command and subarg items
- Multi-slash check fires before subArgs branch (P1-2 fix)
- boot.js uses indexOf('/') not startsWith('/') (CJK fix)
- boot.js fallback uses slashIdx+1 not hardcoded 1
- subarg return path carries beforeSlash/prefix (Follow-up 2 fix)
- onmousedown isSubArg branch uses c.parent+c.value not c.name (Follow-up 2 fix)
"""
from pathlib import Path
import re

COMMANDS_JS = (Path(__file__).parent.parent / "static" / "commands.js").read_text(encoding="utf-8")
BOOT_JS = (Path(__file__).parent.parent / "static" / "boot.js").read_text(encoding="utf-8")


def _function_block(src, name):
    marker = re.search(rf"(^|\n)(?:async\s+)?function\s+{re.escape(name)}\(", src)
    assert marker is not None, f"{name}() not found"
    start = marker.start()
    next_marker = re.search(r"\n(?:function\s+\w+\(|async\s+function\s+\w+\()", src[start + 1:])
    end = start + 1 + next_marker.start() if next_marker else len(src)
    return src[start:end]


# ── P1-1 fix: state vars must be wired ─────────────────────────────────────

def test_get_slash_autocomplete_matches_assigns_state_vars():
    block = _function_block(COMMANDS_JS, "getSlashAutocompleteMatches")
    assert "_currentAutocompleteBeforeSlash=" in block, (
        "getSlashAutocompleteMatches must assign _currentAutocompleteBeforeSlash "
        "so onmousedown can read a populated value"
    )
    assert "_currentAutocompletePrefix=" in block, (
        "getSlashAutocompleteMatches must assign _currentAutocompletePrefix"
    )


def test_onmousedown_has_prefix_preserving_branch():
    block = _function_block(COMMANDS_JS, "showCmdDropdown")
    assert "_currentAutocompleteBeforeSlash" in block, (
        "showCmdDropdown/onmousedown must read _currentAutocompleteBeforeSlash"
    )
    assert "_b+'/'" in block, (
        "onmousedown must have prefix-preserving reassembly: _b + '/' + ..."
    )


# ── P1-2 fix: multi-slash before subArgs branch ────────────────────────────

def test_multi_slash_check_before_subargs_branch():
    block = _function_block(COMMANDS_JS, "_parseSlashAutocomplete")
    # Use code-level identifiers (not comment text) to compare positions
    last_slash_pos = block.find("const lastSlash=raw.lastIndexOf")
    subarg_source_pos = block.find("const subArgSource=")
    assert last_slash_pos >= 0, "const lastSlash=raw.lastIndexOf not found in _parseSlashAutocomplete"
    assert subarg_source_pos >= 0, "const subArgSource= not found in _parseSlashAutocomplete"
    assert last_slash_pos < subarg_source_pos, (
        "Multi-slash lastIndexOf('/') check must appear before subArgSource= assignment "
        "so inputs like /think /karpathy are handled correctly"
    )


# ── Commands return paths carry beforeSlash ────────────────────────────────

def test_parse_slash_autocomplete_carries_beforeSlash_on_commands_paths():
    block = _function_block(COMMANDS_JS, "_parseSlashAutocomplete")
    # Count how many return statements include beforeSlash
    returns_with_beforeslash = re.findall(r"return\s*\{[^}]*beforeSlash\s*:", block)
    assert len(returns_with_beforeslash) >= 2, (
        "_parseSlashAutocomplete must carry beforeSlash on at least 2 return paths "
        "(multi-slash and single-slash commands paths)"
    )


# ── Follow-up 2: subargs return carries beforeSlash/prefix ────────────────

def test_subargs_return_carries_beforeSlash():
    block = _function_block(COMMANDS_JS, "_parseSlashAutocomplete")
    # Use DOTALL to match the full multi-property return object
    subargs_return = re.search(r"return\s*\{kind:'subargs'.*?;", block, re.DOTALL)
    assert subargs_return is not None, "kind:'subargs' return not found"
    subargs_return_text = subargs_return.group(0)
    assert "beforeSlash" in subargs_return_text, (
        "subargs return must include beforeSlash so CJK prefix is preserved "
        "when user selects a sub-argument (e.g. /think budget)"
    )
    assert "prefix" in subargs_return_text, (
        "subargs return must include prefix"
    )


def test_onmousedown_subarg_branch_uses_parent_and_value():
    block = _function_block(COMMANDS_JS, "showCmdDropdown")
    # The isSubArg branch inside if(_b||_p) must use c.parent and c.value, not just c.name
    assert "c.parent" in block, (
        "onmousedown must reference c.parent for subarg reconstruction"
    )
    # The prefix-preserving branch must handle isSubArg with c.parent+' '+c.value
    assert "isSubArg" in block and "c.parent" in block and "c.value" in block, (
        "onmousedown prefix-preserving branch must handle isSubArg using "
        "c.parent + ' ' + c.value (not just c.name)"
    )


# ── boot.js CJK + fallback offset fixes ───────────────────────────────────

def test_boot_js_uses_slashIdx_not_startsWith():
    input_handler_pos = BOOT_JS.find("addEventListener('input'")
    assert input_handler_pos >= 0, "input event listener not found in boot.js"
    handler_section = BOOT_JS[input_handler_pos:input_handler_pos + 600]
    assert "startsWith('/')" not in handler_section, (
        "boot.js input handler must not use startsWith('/') — breaks CJK prefix"
    )
    assert "slashIdx=text.indexOf('/')" in BOOT_JS, (
        "boot.js must use slashIdx=text.indexOf('/')"
    )


def test_boot_js_fallback_uses_slashIdx_offset():
    assert "text.slice(slashIdx+1)" in BOOT_JS, (
        "boot.js fallback branch must use text.slice(slashIdx+1) not text.slice(1) "
        "— when slashIdx>0 the old offset 1 strips leading non-slash characters"
    )
