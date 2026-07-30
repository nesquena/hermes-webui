"""Regression checks for Issue #4713: plugin badge and sort-order alignment.

Focus:
- _pluginActivationState must check is_active_provider===true before the
  activation string branch so the sort bucket always matches the badge shown
  by _buildPluginCard (Greptile P2 finding on PR #4848).
- _buildPluginCard must show the active-provider badge when is_active_provider
  is true, regardless of the activation string.
"""
import re
from pathlib import Path


PANELS_JS = (Path(__file__).parent.parent / "static" / "panels.js").read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    marker = re.search(rf"(^|\n)(?:async\s+)?function\s+{re.escape(name)}\(", src)
    assert marker is not None, f"{name}() not found"
    start = marker.start()
    next_marker = re.search(r"\n(?:function\s+\w+\(|async\s+function\s+\w+\()", src[start + 1:])
    end = start + 1 + next_marker.start() if next_marker else len(src)
    return src[start:end]


def test_activation_state_checks_is_active_provider_before_activation_string():
    """is_active_provider===true must be checked before the activation branch."""
    block = _function_block(PANELS_JS, "_pluginActivationState")
    pos_early = block.find("is_active_provider===true")
    pos_branch = block.find("activation==='exclusive'")
    assert pos_early != -1, "_pluginActivationState must check is_active_provider===true"
    assert pos_branch != -1, "_pluginActivationState must have the exclusive|provider branch"
    assert pos_early < pos_branch, (
        "is_active_provider===true check must precede the activation==='exclusive' branch"
    )


def test_activation_state_early_guard_returns_provider():
    """The early is_active_provider===true guard must return 'provider'."""
    block = _function_block(PANELS_JS, "_pluginActivationState")
    match = re.search(r"is_active_provider===true\)?\s*return\s*'provider'", block)
    assert match is not None, (
        "_pluginActivationState must return 'provider' when is_active_provider===true"
    )


def test_build_plugin_card_uses_is_active_provider_boolean():
    """_buildPluginCard must read plugin.is_active_provider when it's a boolean."""
    block = _function_block(PANELS_JS, "_buildPluginCard")
    assert "is_active_provider" in block, (
        "_buildPluginCard must reference plugin.is_active_provider"
    )


def test_activation_state_disabled_plugin_with_active_provider_sorts_active():
    """Ensure badge and sort agree: is_active_provider===true wins over activation='disabled'.

    Static code check verifying the fix structure rather than a runtime
    execution, since panels.js runs in a browser context.
    """
    block = _function_block(PANELS_JS, "_pluginActivationState")
    lines = block.splitlines()
    early_return_line = None
    branch_line = None
    for i, line in enumerate(lines):
        if "is_active_provider===true" in line and "return 'provider'" in line:
            early_return_line = i
        if early_return_line is None and "is_active_provider===true" in line:
            if i + 1 < len(lines) and "return 'provider'" in lines[i + 1]:
                early_return_line = i
        if "activation==='exclusive'" in line:
            branch_line = i
    assert early_return_line is not None, "early is_active_provider===true return not found"
    assert branch_line is not None, "exclusive|provider branch not found"
    assert early_return_line < branch_line, (
        "is_active_provider===true return must precede the exclusive|provider branch"
    )
