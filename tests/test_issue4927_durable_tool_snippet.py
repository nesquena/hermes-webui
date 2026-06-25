"""Regression coverage for #4927 — terminal output / patch diffs must survive a
cold reload in the transparent activity stream.

A settled tool row renders its output/diff from `snippet`. There are two rebuild
paths: the persisted compact summary (`session.tool_calls`, which HAS a bounded
`snippet`) and the raw-assistant-envelope `derived` rebuild (which joins the
result by tool id via `resultsByTid`). On a cold/paginated load the
`resultsByTid` join can miss (id mismatch, recovery-rebuilt turn) — and #4622's
live enrichment only helps when `S.toolCalls` still matches (empty on cold
load). So the derived build must fall back to the persisted `session.tool_calls`
snippet by tid, making the durable record the reliable source.

This is a source-structure guard: the derived rebuild is inlined in
`renderMessages`, so we assert the fallback wiring is present rather than
extracting a standalone function.
"""
from __future__ import annotations

import re
from pathlib import Path

UI_JS = (Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (Path(__file__).parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")


def _slice_derived_rebuild() -> str:
    """Return the renderMessages fallback-rebuild region (resultsByTid block).

    Anchored on `const fallbackToolSources=[];` which is unique to the
    renderMessages derived-rebuild block — `const resultsByTid={};` alone is no
    longer unique (the transparent-stream ordered path added its own at #4932).
    """
    start = UI_JS.index("const fallbackToolSources=[];")
    # The region runs through the _partial_tool_calls derived push; bound it
    # generously so all derived-push sites are included.
    return UI_JS[start:start + 7000]


def test_persisted_snippet_lookup_is_built_from_session_tool_calls():
    """A tid->snippet lookup must be built from S.session.tool_calls."""
    region = _slice_derived_rebuild()
    assert "persistedSnippetByTid" in region, "no persisted-snippet fallback lookup"
    # It must be sourced from the durable persisted summary.
    assert re.search(r"S\.session\s*&&\s*Array\.isArray\(S\.session\.tool_calls\)", region), \
        "persistedSnippetByTid must be built from S.session.tool_calls"
    # It must key by tid and capture the snippet.
    assert re.search(r"persistedSnippetByTid\[\s*ptid\s*\]\s*=", region), \
        "persisted lookup must be keyed by tid"


def test_derived_result_snippet_falls_back_to_persisted():
    """The derived OpenAI-format build must use persistedSnippetByTid as a
    fallback when the live result-message join (resultsByTid) misses."""
    region = _slice_derived_rebuild()
    # The primary OpenAI-format derived push.
    assert "resultsByTid[tid]||persistedSnippetByTid[tid]" in region, (
        "derived resultSnippet must fall back to the persisted snippet by tid "
        "(#4927) so cold-load tool output/diffs don't vanish"
    )


def test_partial_tool_calls_path_also_falls_back():
    """The _partial_tool_calls derived path must also use the persisted fallback."""
    region = _slice_derived_rebuild()
    assert "resultsByTid[tid]||tc.snippet||tc.preview||persistedSnippetByTid[tid]" in region, (
        "the partial-tool-calls derived path must also fall back to the "
        "persisted snippet (#4927)"
    )


def test_all_derived_result_snippet_reads_have_fallback():
    """Every derived-push result-snippet READ (OpenAI top-level, Anthropic
    tool_use content-array, and partial) must include the persisted fallback —
    a missed branch (e.g. the Anthropic content-array push) would still
    cold-reload with an empty body (#4927 gate)."""
    region = _slice_derived_rebuild()
    # Find every `const resultSnippet=resultsByTid[tid]...` read and assert each
    # one chains to persistedSnippetByTid.
    reads = re.findall(r"const resultSnippet=resultsByTid\[tid\][^;]*;", region)
    assert reads, "no resultSnippet reads found in the derived rebuild"
    for r in reads:
        assert "persistedSnippetByTid[tid]" in r, (
            f"derived resultSnippet read missing the persisted fallback (#4927): {r}"
        )


def test_loaded_session_tool_calls_persisted_onto_session():
    """#4927 gate: the cold-load path must persist the messages=1 compact tool
    summary onto S.session.tool_calls, or the renderMessages fallback source is
    empty on exactly the cold-load path it repairs (loadSession keeps the
    messages=0 object whose tool_calls is [])."""
    start = SESSIONS_JS.index("function _syncToolCallsForLoadedMessages(")
    region = SESSIONS_JS[start:start + 1500]
    assert "S.session.tool_calls=sessionToolCalls" in region.replace(" ", ""), (
        "_syncToolCallsForLoadedMessages must copy the loaded sessionToolCalls "
        "onto S.session.tool_calls so the derived-rebuild fallback has a source"
    )
