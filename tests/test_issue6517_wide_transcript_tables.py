"""Regression coverage for PR #6517 — transcript tables/code use content width,
and bare Markdown tables gain a horizontal-scroll escape.

Two halves compose:
  * WIDE viewports: `.msg-body:has(table|pre|.csv-table-wrap)` lifts the prose 680px
    measure to min(1100px,100%). The review worry was that a later plain `.msg-body`
    rule overrides this "by equal specificity" — it does NOT: `:has(...)` adds a
    type-selector's weight, so the wide rule out-specifies the override and wins
    regardless of source order (proven below by computing specificity).
  * NARROW viewports: `enhanceMarkdownTables()` wraps each bare table in a
    `.markdown-table-scroll` container (its own overflow-x:auto scroller), so a wide
    table stays horizontally reachable even though the transcript ancestors clip
    overflow-x — closing the "no scroll container / clipped columns" gap.

Source-level guards, per this repo's convention for CSS/layout regressions (the
runtime layout is viewport-specific and not reproducible in headless CI without a
full browser).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
MESSAGES = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _enhancer_body() -> str:
    start = MESSAGES.index("function enhanceMarkdownTables(root)")
    end = MESSAGES.index("function _markdownTableText")
    return MESSAGES[start:end]


# --------------------------------------------------------------------------- #
# CSS specificity — small, self-validated calculator (handles the shapes in play)
# --------------------------------------------------------------------------- #
def _specificity(sel: str):
    """Return the (a, b, c) specificity tuple for a simple/compound selector.
    Handles ids, classes, attributes, pseudo-classes, type selectors, and the
    functional pseudo-classes :has()/:is()/:not() (take their argument's
    specificity) and :where() (zero). Sufficient for the selectors this PR uses."""
    a = b = c = 0
    work = sel.strip()

    # functional pseudo-classes that contribute their argument's specificity
    for fn in ("has", "is", "not"):
        for m in re.finditer(r":%s\(([^()]*)\)" % fn, work):
            sa, sb, sc = _specificity(m.group(1))
            a += sa; b += sb; c += sc
        work = re.sub(r":%s\([^()]*\)" % fn, " ", work)
    work = re.sub(r":where\([^()]*\)", " ", work)  # :where -> 0

    a += len(re.findall(r"#[\w-]+", work)); work = re.sub(r"#[\w-]+", " ", work)
    c += len(re.findall(r"::[\w-]+", work)); work = re.sub(r"::[\w-]+", " ", work)  # pseudo-elements
    b += len(re.findall(r"\.[\w-]+", work)); work = re.sub(r"\.[\w-]+", " ", work)
    b += len(re.findall(r"\[[^\]]*\]", work)); work = re.sub(r"\[[^\]]*\]", " ", work)
    b += len(re.findall(r":[\w-]+", work)); work = re.sub(r":[\w-]+", " ", work)  # pseudo-classes
    c += len(re.findall(r"[A-Za-z][\w-]*", work))  # remaining type selectors
    return (a, b, c)


def test_specificity_helper_self_check():
    # sanity: the helper models the rules the cascade actually uses
    assert _specificity(".msg-body:has(table)") == (0, 1, 1)
    assert _specificity(".msg-body") == (0, 1, 0)
    assert _specificity(".msg-body:has(table)") > _specificity(".msg-body")
    assert _specificity("#x") > _specificity(".a.b.c")
    assert _specificity(".markdown-table-scroll>table") == _specificity(".msg-body table")


# --------------------------------------------------------------------------- #
# WIDE half — the width rule is not a dead no-op
# --------------------------------------------------------------------------- #
def test_wide_content_rule_uses_has_and_outspecifies_plain_msg_body_override():
    """The advertised widening must actually win over the later `.msg-body`
    max-width rule — the exact 'overridden by equal specificity' worry."""
    assert ".msg-body:has(table)" in CSS
    assert ".msg-body:has(pre)" in CSS
    assert ".msg-body:has(.csv-table-wrap){max-width:min(1100px,100%);}" in CSS

    wide = _specificity(".msg-body:has(table)")
    # every later top-level plain `.msg-body { ... max-width ... }` rule must lose
    plain_overrides = re.findall(r"(?<![\w.>~+ ])\.msg-body\s*\{[^}]*max-width[^}]*\}", CSS)
    assert plain_overrides, "expected at least one plain .msg-body max-width rule to exist"
    for rule in plain_overrides:
        assert "!important" not in rule, "a plain .msg-body override uses !important — would defeat the :has rule"
    assert wide > _specificity(".msg-body"), (
        "the :has() wide rule must out-specify plain .msg-body so it wins the cascade"
    )


def test_messages_inner_parent_ceiling_allows_wide_content():
    """The child body width is useless if the `.messages-inner` parent still caps it;
    the parent must widen on wide viewports."""
    assert re.search(r"@media\(min-width:1400px\)\{\.messages-inner\{max-width:1100px;\}\}", CSS)
    assert re.search(r"@media\(min-width:1800px\)\{\.messages-inner\{max-width:1200px;\}\}", CSS)


def test_prose_only_messages_keep_the_reading_measure():
    assert ".msg-body{font-family:var(--font-conversation);" in CSS
    assert "max-width:680px;" in CSS  # prose cap still present; wide rule is :has-scoped


# --------------------------------------------------------------------------- #
# NARROW half — bare tables get a real horizontal-scroll container
# --------------------------------------------------------------------------- #
def test_markdown_tables_are_wrapped_in_a_scroll_container():
    body = _enhancer_body()
    assert "document.createElement('div')" in body
    assert "scrollWrap.className='markdown-table-scroll'" in body
    assert "scrollWrap.appendChild(table)" in body
    # skips CSV tables (already wrapped) and does not double-wrap
    assert ".csv-table-wrap" in body
    assert "contains('markdown-table-scroll')" in body


def test_filter_is_pinned_above_the_scroll_area():
    body = _enhancer_body()
    # controls anchor on the wrapper (or the table if unwrapped) and insert before it
    assert "const controlAnchor=" in body
    assert "controlsHost.insertBefore(filter,controlAnchor)" in body


def test_scroll_wrapper_css_is_present_and_wins_source_order():
    assert re.search(r"\.markdown-table-scroll\{[^}]*overflow-x:auto[^}]*\}", CSS)
    assert re.search(r"\.markdown-table-scroll>table\{[^}]*min-width:100%[^}]*\}", CSS)
    assert re.search(r"\.markdown-table-scroll th,\.markdown-table-scroll td\{[^}]*overflow-wrap:normal", CSS)
    # equal specificity vs `.msg-body table` -> the wrapper override must come LATER
    assert CSS.index(".markdown-table-scroll>table{") > CSS.index(".msg-body table{"), (
        ".markdown-table-scroll>table must follow .msg-body table so its width/margin win"
    )


def test_header_nowrap_is_safe_now_that_tables_scroll():
    """The nowrap header rule (kept for one-line headers) is only safe because bare
    tables now have a scroll escape; assert both invariants coexist."""
    assert ".msg-body table th{white-space:nowrap;}" in CSS
    assert re.search(r"\.markdown-table-scroll\{[^}]*overflow-x:auto", CSS)
