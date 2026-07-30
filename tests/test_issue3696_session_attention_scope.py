"""Regression test for #3696 — `_sessionAttentionState is not defined`.

Bug: `_sessionAttentionState` was declared INSIDE `renderSessionListFromCache()`
and relied on "function hoisting", but the separate top-level function
`_sidebarRowHasVisibleMessages` (reached via renderSessionListFromCache ->
_partitionSidebarSessionRows) called it BARE. Function hoisting is scoped to the
enclosing function, so the call threw `ReferenceError: _sessionAttentionState is
not defined` on every sidebar cache-render — the session list went blank
(v0.51.269, regressed by #3672 when _sidebarRowHasVisibleMessages was extracted
to top level).

Fix: hoist `_sessionAttentionState` to top-level (module/global) scope so both the
top-level visibility predicate and the nested per-row renderer can reach it.

This is a structural test (no node/eslint needed, runs in every shard). The
behavioral scope-analysis guard lives in tests/test_static_js_scope_undef.py +
scripts/scope_undef_gate.py, which catch the whole class. This test pins the
specific #3696 invariant cheaply.
"""
import re
from pathlib import Path

SESSIONS_JS = (Path(__file__).resolve().parents[1] / "static" / "sessions.js").read_text(encoding="utf-8")


def _brace_body(src: str, open_brace_idx: int) -> tuple[int, int]:
    """Return (start, end) char offsets of the body delimited by the brace at
    open_brace_idx (exclusive of the braces)."""
    depth = 1
    i = open_brace_idx + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return open_brace_idx + 1, i - 1


def _function_span(src: str, name: str) -> tuple[int, int]:
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"function {name} not found"
    brace = src.find("{", m.end())
    return _brace_body(src, brace)


def test_session_attention_state_is_top_level():
    """`_sessionAttentionState` must be declared at top-level scope (column 0),
    not nested inside another function — otherwise the top-level callers throw
    ReferenceError (#3696)."""
    decls = re.findall(r"^(\s*)function\s+_sessionAttentionState\s*\(", SESSIONS_JS, re.M)
    assert decls, "_sessionAttentionState declaration not found"
    assert len(decls) == 1, f"expected exactly one declaration, found {len(decls)}"
    assert decls[0] == "", (
        "#3696: _sessionAttentionState must be a TOP-LEVEL function (no leading "
        f"indentation), but it is indented {len(decls[0])} spaces (nested). A nested "
        "declaration only hoists within its enclosing function, so top-level callers "
        "like _sidebarRowHasVisibleMessages throw 'ReferenceError: _sessionAttentionState "
        "is not defined'."
    )


def test_session_attention_state_not_nested_in_render_from_cache():
    """Belt-and-suspenders: the definition must NOT live inside the body of
    `renderSessionListFromCache` (where it was when #3696 shipped)."""
    start, end = _function_span(SESSIONS_JS, "renderSessionListFromCache")
    body = SESSIONS_JS[start:end]
    assert "function _sessionAttentionState(" not in body, (
        "#3696: _sessionAttentionState is defined inside renderSessionListFromCache() — "
        "it must be hoisted to top-level so _sidebarRowHasVisibleMessages can call it."
    )


def test_sidebar_visibility_predicate_calls_attention_state():
    """Guard the regression's trigger: _sidebarRowHasVisibleMessages (top-level)
    references _sessionAttentionState. If this call is ever removed the bug can't
    recur, but while it exists the function MUST be top-level (asserted above)."""
    start, end = _function_span(SESSIONS_JS, "_sidebarRowHasVisibleMessages")
    body = SESSIONS_JS[start:end]
    assert "_sessionAttentionState(" in body, (
        "_sidebarRowHasVisibleMessages no longer calls _sessionAttentionState — if this "
        "is intentional, update this test; the #3696 invariant assumes this call exists."
    )
