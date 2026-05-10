"""Test for PR #1339 — streaming.py must support both single-dict `fallback_model`
and list-form `fallback_providers` config without crashing on `.get()`.

Before the fix, when config had `fallback_providers: [{provider, model, ...}, ...]`,
streaming.py read it as if it were a dict and called `.get('model', '')` on a list,
which would raise `AttributeError: 'list' object has no attribute 'get'`.

The fix makes streaming.py handle both legacy dict form and new list form, picking
the first entry from the list when given a list.
"""

from pathlib import Path

STREAMING_PY = Path(__file__).resolve().parent.parent / "api" / "streaming.py"


def _extract_fallback_block():
    """Return the source range that handles fallback_model/fallback_providers."""
    src = STREAMING_PY.read_text(encoding="utf-8")
    # Locate the resolved-fallback region
    idx = src.find("# Fallback model from profile config")
    assert idx != -1, "Fallback block marker not found in streaming.py"
    end = src.find("# Build kwargs defensively", idx)
    assert end != -1, "End-of-block marker not found"
    return src[idx:end]


def test_fallback_handles_both_dict_and_list_config():
    """Block must read either fallback_model (dict) or fallback_providers (list)."""
    block = _extract_fallback_block()

    # Both keys must be consulted
    assert "fallback_model" in block, (
        "Must still support legacy single-dict fallback_model"
    )
    assert "fallback_providers" in block, (
        "Must support new list-form fallback_providers (PR #1339)"
    )


def test_fallback_list_iteration_picks_first_valid_entry():
    """When given a list, code must pick the first valid dict entry, not call .get on the list."""
    block = _extract_fallback_block()

    # Must isinstance-check before calling .get
    assert "isinstance(_fallback, list)" in block, (
        "Must detect list-form fallback_providers explicitly to avoid AttributeError"
    )
    assert (
        "isinstance(_fallback, dict)" in block or "isinstance(_fallback,dict)" in block
    ), "Must keep legacy single-dict path explicitly"

    # No bare _fallback.get() — every .get() on _fallback must be guarded by an isinstance(_fallback, dict) check.
    # We verify this structurally: every line containing `_fallback.get(` must be inside or preceded by an isinstance(_fallback, dict) gate.
    lines = block.split("\n")
    in_dict_block = False
    for i, line in enumerate(lines):
        if "isinstance(_fallback, dict)" in line:
            in_dict_block = True
        if "_fallback.get(" in line and not in_dict_block:
            # Look back up to 3 lines for the isinstance gate on the same elif/if
            window = "\n".join(lines[max(0, i - 3) : i + 1])
            assert "isinstance(_fallback, dict)" in window, (
                f"Line {i} calls _fallback.get() without a nearby isinstance(_fallback, dict) gate:\n{line}"
            )


def test_fallback_resolved_initialized_to_none():
    """_fallback_resolved must default to None so AIAgent gets an explicit None when no fallback."""
    block = _extract_fallback_block()
    # The variable must be assignable to None at the top of the block
    assert "_fallback_resolved = None" in block, (
        "_fallback_resolved must be initialized to None so callers can rely on its presence"
    )
