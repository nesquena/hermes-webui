"""Tests for the desktop opt-in lazy transcript render (content-visibility).

Feature: extend the touch-only (#5637) content-visibility optimization to
desktop as an OPT-IN, gated behind ``html[data-lazy-render="enabled"]`` —
the SillyTavern-style "don't render the whole transcript" lever for large
sessions, WITHOUT the #4343 / #6717 JS measurement-oscillation, because it
uses pure browser layout-skip (no JS height-estimation) and keeps the full
DOM (Ctrl+F unaffected for rendered rows).

Safety contract (mirrors the #5637 rationale):
  * Scope to USER rows only. Assistant rows can hold multi-thousand-px
    tool-result blocks; a flat ``contain-intrinsic-size`` cannot reserve their
    real off-screen height, which collapses ``scrollHeight`` and force-clamps
    ``scrollTop`` (jump-to-top). User rows are short and size-predictable.
  * Live-streaming rows stay fully rendered.
  * Opt-in and default-OFF: the rule only applies under the data attribute,
    which is only set by the runtime toggle / persisted setting.

These tests fail on the unpatched tree and pass once the CSS rule + boot.js
hook are applied.

Run:
    ./scripts/test.sh tests/test_desktop_lazy_render.py -v
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")

ATTR = 'data-lazy-render="enabled"'


def _block_containing(text: str, anchor: str) -> str:
    """Return the CSS rule block whose selector line contains *anchor*."""
    start = text.index(anchor)
    # find the selector start (line start before the anchor)
    sel_start = text.rindex("\n", 0, start) + 1
    brace = text.index("{", start)
    depth = 0
    for i in range(brace, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[sel_start : i + 1]
    raise AssertionError(f"rule block for {anchor!r} not found")


# ---------------------------------------------------------------------------
# CSS: the opt-in desktop rule
# ---------------------------------------------------------------------------
def test_desktop_lazy_render_rule_is_gated_by_data_attr():
    """There must be a desktop rule scoped to :root[data-lazy-render=enabled]
    targeting user rows with content-visibility:auto."""
    anchor = f':root[{ATTR}] .msg-row[data-role="user"]'
    assert anchor in STYLE_CSS, (
        "missing desktop lazy-render rule: expected a selector "
        f"'{anchor}' in style.css"
    )
    block = _block_containing(STYLE_CSS, anchor)
    assert "content-visibility: auto" in block
    assert "contain-intrinsic-size" in block, (
        "lazy-render rule must declare contain-intrinsic-size so off-screen "
        "user rows reserve scroll height (else scrollHeight collapses)"
    )


def test_desktop_lazy_render_excludes_assistant_rows():
    """The opt-in rule must NOT lazy-render assistant rows — tall tool-result
    content cannot be height-estimated (the #5637 jump-back vector)."""
    # The gated selector must target user rows, never a bare .msg-row or
    # [data-role="assistant"].
    bad_user_scoped = f':root[{ATTR}] .msg-row[data-role="assistant"]'
    bad_bare = f':root[{ATTR}] .msg-row {{'
    assert bad_user_scoped not in STYLE_CSS, (
        "desktop lazy-render must not lazy-render assistant rows"
    )
    assert bad_bare not in STYLE_CSS, (
        "desktop lazy-render must not be applied to ALL rows (bare .msg-row)"
    )


def test_desktop_lazy_render_keeps_live_streaming_visible():
    """Live-streaming rows must stay fully rendered under the opt-in."""
    assert f':root[{ATTR}] #liveAssistantTurn' in STYLE_CSS, (
        "missing live-streaming opt-out under the lazy-render gate"
    )


# ---------------------------------------------------------------------------
# JS: the runtime toggle / persistence hook
# ---------------------------------------------------------------------------
def test_boot_sets_data_attr_from_setting():
    """boot.js must read settings.lazy_render and set the data attribute."""
    assert "s.lazy_render===true" in BOOT_JS, (
        "boot.js must read the lazy_render setting"
    )
    assert "dataset.lazyRender" in BOOT_JS, (
        "boot.js must set document.documentElement.dataset.lazyRender"
    )


def test_boot_exposes_runtime_toggle():
    """boot.js must expose window._setLazyRender for live toggling."""
    assert "window._setLazyRender" in BOOT_JS, (
        "boot.js must expose a window._setLazyRender(on) runtime toggle"
    )
