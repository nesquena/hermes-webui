"""Regression coverage for the function-name × window-attached-config collision class.

This test guards against a specific failure mode that has caused two
brick-class regressions (v0.51.106 #2715 `_pinnedSessionsLimit`, v0.51.117
#2771 `_inflightStateLimits`):

  - Some module declares `function foo(){...}` at top level. Since the
    WebUI ships classic (non-module) scripts via `<script defer>`, top-
    level function declarations attach to `window` as `window.foo`.
  - Another module later does `window.foo = {...}` (or `= 8`, etc).
  - Boot order makes the assignment win, so by the time anyone tries
    `foo()` they're calling an Object/Number → `TypeError: foo is not a
    function`.

This is hard to spot in code review because the function and the config
object live in different files and the name choice is locally innocuous.

The test below scans static JS for any top-level `function foo()` decl
whose name also appears as the target of `window.foo = <non-function-
non-identifier>`. False-positive shape (which we deliberately exclude):
re-binding a function reference onto `window` (`window.foo = foo;` or
`window.foo = function(){...};`) — this is the normal "expose to global"
pattern.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_JS = sorted(
    p for p in (REPO_ROOT / "static").glob("*.js")
    if not p.name.endswith(".min.js")
)


# Top-of-line: `function NAME(`
TOP_LEVEL_FN_RE = re.compile(r"^function\s+([A-Za-z_\$][A-Za-z_\$0-9]*)\s*\(", re.MULTILINE)

# `window.NAME = <rhs>` where rhs is the next non-space chunk.
# We only care about the *value shape* of rhs. Lookahead must be long enough
# to see `function(` (8 chars) even after some whitespace; we use 32 chars to
# be safe against newlines and odd indenting.
WINDOW_ASSIGN_RE = re.compile(
    r"window\.([A-Za-z_\$][A-Za-z_\$0-9]*)\s*=\s*([^=].{0,32})",
    re.DOTALL,
)


def _classify_rhs(rhs: str) -> str:
    """Classify the right-hand side of `window.X = rhs`.

    Returns one of:
      - 'function'   — explicit `function(` literal (named or anonymous)
      - 'identifier' — bare identifier (almost certainly a function reference)
      - 'object'     — `{...}` object literal (THE BUG SHAPE — function got
                       replaced by config object)
      - 'number'     — numeric literal (ALSO BUG SHAPE — #2715 was this)
      - 'arrow'      — `() =>` / `x =>` arrow function (benign re-bind)
      - 'other'      — anything else; treat as suspicious to be safe
    """
    rhs = rhs.lstrip()
    if rhs.startswith("function"):
        return "function"
    if rhs.startswith("{"):
        return "object"
    # Arrow function: `(args) =>` or `x =>`
    if rhs.startswith("(") and "=>" in rhs:
        return "arrow"
    if re.match(r"[A-Za-z_\$][A-Za-z_\$0-9]*\s*=>", rhs):
        return "arrow"
    # Numeric literal: int or decimal
    if re.match(r"-?\d", rhs):
        return "number"
    # Bare identifier reference: looks like `_foo;` or `_foo,` or `_foo ` etc.
    # Allow ||, &&, ? chains too (e.g. `window.X = window.X || false;`).
    if re.match(r"[A-Za-z_\$][A-Za-z_\$0-9]*[\s;,)|&?.]", rhs):
        return "identifier"
    return "other"


def test_no_top_level_function_shadowed_by_window_object_assignment():
    """Catch the v0.51.106 / v0.51.117 collision class before it ships.

    See #2715 (`_pinnedSessionsLimit` function shadowed by `window._pinnedSessionsLimit = <int>`)
    and #2771 (`_inflightStateLimits` function shadowed by
    `window._inflightStateLimits = {...}`). Both broke entire user
    workflows for everyone on the affected version.
    """
    # Collect all top-level function names across every static JS file.
    fn_names: dict[str, list[str]] = {}
    for js_file in STATIC_JS:
        src = js_file.read_text(encoding="utf-8")
        for m in TOP_LEVEL_FN_RE.finditer(src):
            fn_names.setdefault(m.group(1), []).append(js_file.name)

    # Find every window.NAME = <rhs> assignment, classify the rhs.
    collisions: list[str] = []
    for js_file in STATIC_JS:
        src = js_file.read_text(encoding="utf-8")
        for m in WINDOW_ASSIGN_RE.finditer(src):
            name, rhs_snippet = m.group(1), m.group(2)
            if name not in fn_names:
                continue
            kind = _classify_rhs(rhs_snippet)
            if kind in {"function", "identifier", "arrow"}:
                continue  # benign exposure of a function to global scope.
            # 'object', 'number', and 'other' are the BUG shapes.
            collisions.append(
                f"In {js_file.name}: `window.{name} = ...` (rhs={kind!r}) "
                f"shadows the top-level `function {name}()` declared in "
                f"{', '.join(fn_names[name])}. This will cause "
                f"`TypeError: {name} is not a function` once boot.js's "
                f"assignment overwrites the function. See #2715, #2771."
            )

    assert not collisions, (
        "Function-name × window-config collision detected — this is the "
        "brick-class regression shape from #2715 and #2771:\n  - "
        + "\n  - ".join(collisions)
    )


def test_inflight_state_limits_no_longer_collides_with_window_config():
    """Issue-pinned regression for #2771 specifically.

    Confirms the function rename landed and the old colliding name is gone.
    """
    ui_js = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    boot_js = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")

    # The window-attached config still exists (we deliberately kept this name).
    assert "window._inflightStateLimits={" in boot_js, (
        "boot.js should still expose the config under the documented name."
    )

    # The function must use the renamed identifier.
    assert "function _getInflightStateLimits()" in ui_js, (
        "ui.js should declare the limit-reader as `_getInflightStateLimits()` "
        "to avoid the #2771 collision."
    )

    # The old colliding name must not appear as a function declaration anywhere.
    assert "function _inflightStateLimits(" not in ui_js, (
        "`function _inflightStateLimits()` is the colliding name from #2771 "
        "and must not be reintroduced."
    )

    # Every call site uses the new name.
    assert "_inflightStateLimits()" not in ui_js, (
        "Stale call sites to the old function name `_inflightStateLimits()` "
        "remain in ui.js (#2771). Update them to `_getInflightStateLimits()`."
    )
