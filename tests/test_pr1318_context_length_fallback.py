"""Regression test for #1318 fallback (#1344 follow-up).

PR #1318 / #1341 / a5c10d5 (in v0.50.246) persisted context_length to the
session when agent.context_compressor was present. But for fresh agents or
interrupted streams, context_compressor may be absent or report 0 — leaving
the context-ring indicator showing 0% even with the writer in place.

This follow-up adds a fallback to agent.model_metadata.get_model_context_length()
that resolves the model's static context window when the compressor didn't.

Sourced from @jasonjcwu's PR #1344, extracted into a focused follow-up.

Tests:
1. Writer block contains the fallback after the compressor block
2. Fallback gates on s.context_length being 0/falsy
3. Fallback uses agent.model + agent.base_url
4. Fallback exception is silently swallowed (older agent builds)
5. Fallback runs before s.save() so the value is persisted
"""
import re
from pathlib import Path

STREAMING = Path(__file__).resolve().parent.parent / "api" / "streaming.py"


def _persistence_block():
    """Return the source range covering the post-merge per-turn save block."""
    src = STREAMING.read_text(encoding="utf-8")
    start = src.find("if _reasoning_text and s.messages:")
    assert start != -1, "Reasoning trace marker not found in streaming.py"
    end = src.find("\n                s.save()", start)
    assert end != -1, "s.save() not found after the reasoning trace marker"
    # Include the s.save() line so we can verify ordering
    end = src.find("\n", end + 1)
    return src[start:end]


def test_fallback_uses_model_metadata():
    """Block must import and call get_model_context_length on missing compressor data."""
    block = _persistence_block()
    assert "from agent.model_metadata import get_model_context_length" in block, (
        "Fallback must import get_model_context_length from agent.model_metadata"
    )
    assert "get_model_context_length(" in block, (
        "Fallback must call get_model_context_length()"
    )


def test_fallback_gates_on_falsy_context_length():
    """Fallback runs only when the compressor didn't populate s.context_length.

    The gate must check s.context_length (not _cc_for_save) — if the compressor
    set context_length but it was 0, we still want the fallback to fire.
    """
    block = _persistence_block()
    # The conditional must reference s.context_length (or getattr(s, 'context_length', 0))
    assert (
        "if not getattr(s, 'context_length'" in block
        or "if not s.context_length" in block
    ), "Fallback must gate on s.context_length being falsy"


def test_fallback_passes_model_and_base_url():
    """Fallback must source the model and base_url from the agent itself."""
    block = _persistence_block()
    # Must reference both agent.model and agent.base_url in the call
    assert "agent, 'model'" in block, "Fallback must read agent.model"
    assert "agent, 'base_url'" in block, "Fallback must read agent.base_url"


def test_fallback_exception_is_swallowed():
    """If get_model_context_length raises (older agent build, network error,
    bad provider config), the fallback must not break s.save()."""
    block = _persistence_block()
    # Must wrap the import + call in try/except
    fallback_section = block[block.find("Fallback"):]
    assert "try:" in fallback_section, "Fallback must use try/except"
    # except Exception: pass-style — old agent builds may not have this helper at all
    assert "except Exception:" in fallback_section, (
        "Fallback must catch broad Exception (older agent builds may not have the helper)"
    )


def test_fallback_runs_before_save():
    """The fallback must mutate s.context_length BEFORE s.save() so the value lands on disk."""
    block = _persistence_block()
    fallback_idx = block.find("get_model_context_length")
    save_idx = block.rfind("s.save()")
    assert fallback_idx != -1 and save_idx != -1
    assert fallback_idx < save_idx, (
        "Fallback must run BEFORE s.save() — otherwise the resolved context_length "
        "is not persisted to the session JSON."
    )


def test_fallback_assigns_context_length_when_resolved():
    """The fallback must assign s.context_length when get_model_context_length returns a non-zero value."""
    block = _persistence_block()
    fallback_section = block[block.find("Fallback"):]
    # Must have an `if _resolved_cl:` guard followed by `s.context_length = _resolved_cl`
    assert "_resolved_cl" in fallback_section, "Fallback must capture the result"
    assert "s.context_length = _resolved_cl" in fallback_section, (
        "Fallback must assign the resolved value to s.context_length"
    )
