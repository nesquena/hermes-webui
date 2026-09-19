"""Regression coverage for PR #6517 — transcript tables/code use content width,
and bare Markdown tables gain a horizontal-scroll escape.

Two halves compose:
  * WIDE viewports: a transcript carrying a table/code block widens at BOTH levels —
    the child `.msg-body:has(table|pre|.csv-table-wrap)` lifts the prose measure, and
    the parent `.messages-inner:has(...)` lifts the column ceiling to min(1100px,100%).
    Both use `:has(...)`, which adds a type/class weight, so each out-specifies the
    later plain `.msg-body` / `.messages-inner` max-width rules and wins regardless of
    source order. The review's second-round point — that `.messages-inner` was still
    capped at 780/820/860px by a LATER equal-specificity block winning by source order,
    so the child's 1100px was unreachable — is guarded by `test_messages_inner_*` below:
    a full source-level cascade oracle resolves the actual winner across EVERY matching
    `.messages-inner` declaration, not just presence of one.
  * NARROW viewports: `enhanceMarkdownTables()` wraps each bare table in a
    `.markdown-table-scroll` container (its own overflow-x:auto scroller), so a wide
    table stays horizontally reachable even though the transcript ancestors clip
    overflow-x — closing the "no scroll container / clipped columns" gap. Runtime
    behavior (double-invoke idempotency, filter, stable sort, CSV exclusion) is
    executed against a DOM in tests/test_issue6517_table_enhancer_behavior.py.

Source-level guards, per this repo's convention for CSS/layout regressions (the
runtime layout is viewport-specific and not reproducible in headless CI without a
full browser); the cascade oracle below is the source-level winner-proof the review
asked for in place of a browser computed-layout check.
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


# --------------------------------------------------------------------------- #
# Cascade oracle — resolve the ACTUAL winning `.messages-inner` max-width across
# every matching declaration, not just assert one exists (the review's ask: the
# earlier presence-only test was false-green because a LATER equal-specificity
# `.messages-inner` block won by source order and capped the column at 780/820/860).
# --------------------------------------------------------------------------- #
MSG_MAX = 780  # `--msg-max: 780px`, asserted below so this stays in sync


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _messages_inner_maxwidth_decls(css: str):
    """Every rule whose selector list contains `.messages-inner` and whose body sets
    `max-width`, in source order, with the `@media` conditions it is nested in.
    Returns dicts: {selectors, value, media (list of (kind, px)), order}."""
    css = _strip_css_comments(css)
    decls = []
    media_stack = []  # list of {conds, body_depth}
    net, i, n, order = 0, 0, len(css), 0
    while i < n:
        if css[i].isspace():
            i += 1
            continue
        # opening of an @media block: content sits one level deeper
        m = re.match(r"@media([^{]*)\{", css[i:])
        if m:
            conds = [(mm.group(1), int(mm.group(2)))
                     for mm in re.finditer(r"(min|max)-width\s*:\s*(\d+)px", m.group(1))]
            net += 1
            media_stack.append({"conds": conds, "body_depth": net})
            i += m.end()
            continue
        # a plain, brace-balanced rule: <selectors> { <decls> } (no nested braces)
        m = re.match(r"([^{}@]+)\{([^{}]*)\}", css[i:])
        if m:
            selectors, body = m.group(1), m.group(2)
            if ".messages-inner" in selectors and "max-width" in body:
                val = re.search(r"max-width\s*:\s*([^;]+)", body).group(1).strip()
                media = [c for ms in media_stack for c in ms["conds"]]
                decls.append({"selectors": selectors.strip(), "value": val,
                              "media": media, "order": order})
                order += 1
            i += m.end()
            continue
        # closing brace of an @media block: pop any media now out of scope
        if css[i] == "}":
            net -= 1
            media_stack = [ms for ms in media_stack if ms["body_depth"] <= net]
            i += 1
            continue
        if css[i] == "{":  # unmatched opener (defensive)
            net += 1
        i += 1
    return decls


def _media_applies(media, viewport: int) -> bool:
    for kind, px in media:
        if kind == "min" and viewport < px:
            return False
        if kind == "max" and viewport > px:
            return False
    return True


def _selector_applies(selectors: str, has_wide: bool) -> bool:
    """The subject is a `.messages-inner`. A `:has(...)` variant applies only when the
    transcript carries wide content; a plain `.messages-inner` selector always applies."""
    parts = [p.strip() for p in selectors.split(",")]
    for p in parts:
        if ":has(" in p:
            if has_wide:
                return True
        else:
            return True
    return False


def _selector_specificity(selectors: str):
    # a comma list takes the max specificity among its matching compound selectors
    return max(_specificity(p.strip()) for p in selectors.split(","))


def _winning_maxwidth(css: str, viewport: int, has_wide: bool):
    """The declaration the cascade actually applies: highest specificity, then latest
    source order — exactly the rule the browser uses."""
    applicable = [
        d for d in _messages_inner_maxwidth_decls(css)
        if _media_applies(d["media"], viewport) and _selector_applies(d["selectors"], has_wide)
    ]
    assert applicable, f"no .messages-inner max-width rule applies at {viewport}px (wide={has_wide})"
    return max(applicable, key=lambda d: (_selector_specificity(d["selectors"]), d["order"]))


def _resolve_px(value: str, container: int):
    """Evaluate the winning value to pixels for a given container width. Handles the
    shapes actually used: `var(--msg-max)`, `calc(var(--msg-max) + Npx)`, `min(Apx,100%)`,
    plain `Npx`, and `100%`."""
    v = value.replace("var(--msg-max)", str(MSG_MAX))
    if v.strip() == "100%":
        return container
    m = re.fullmatch(r"min\(\s*(\d+)px\s*,\s*100%\s*\)", v.strip())
    if m:
        return min(int(m.group(1)), container)
    m = re.fullmatch(r"calc\(\s*(\d+)\s*\+\s*(\d+)px\s*\)", v.strip())
    if m:
        return int(m.group(1)) + int(m.group(2))
    m = re.fullmatch(r"(\d+)(?:px)?", v.strip())  # `780px` or bare `780` (from var(--msg-max))
    if m:
        return int(m.group(1))
    raise AssertionError(f"unrecognized max-width value: {value!r}")


def test_cascade_oracle_models_specificity_over_source_order():
    """Self-check: the oracle must reproduce the real cascade — a later equal-specificity
    rule beats an earlier one (source order), but a higher-specificity `:has` rule beats
    a later plain one (specificity dominates). This is exactly the failure mode the review
    described, so the oracle has to get it right to be trustworthy."""
    sample = (
        ".messages-inner { max-width: 100px; }\n"
        ".messages-inner { max-width: 200px; }\n"  # later plain wins by order -> 200
        ".messages-inner:has(.msg-body table) { max-width: min(900px,100%); }\n"  # :has wins by specificity
    )
    assert _resolve_px(_winning_maxwidth(sample, 1500, has_wide=False)["value"], 1500) == 200
    assert _winning_maxwidth(sample, 1500, has_wide=True)["value"] == "min(900px,100%)"


def test_msg_max_is_the_expected_prose_measure():
    assert re.search(r"--msg-max:\s*%dpx" % MSG_MAX, CSS), "MSG_MAX drifted from --msg-max"


def test_messages_inner_widens_for_wide_content_and_keeps_prose_measure():
    """The winning `.messages-inner` width at desktop widths must be the wide-content
    `:has` rule (min(1100px,100%)) when the transcript carries a table/code — and must
    remain the prose measure (780/820/860) when it does not. Presence alone is not
    enough; this resolves the actual cascade winner across every declaration."""
    for viewport in (1400, 1500, 1800, 1920):
        win = _winning_maxwidth(CSS, viewport, has_wide=True)
        assert ":has(" in win["selectors"] and win["value"] == "min(1100px, 100%)", (
            f"at {viewport}px a wide-content transcript should win via the :has rule, "
            f"got {win['selectors']!r} -> {win['value']!r}"
        )
        # and the child body can actually reach ~1100px because the parent now allows it
        assert _resolve_px(win["value"], viewport) >= 1100

    # prose-only transcripts keep the reading measure — no 1100/1200 leak
    assert _resolve_px(_winning_maxwidth(CSS, 1000, has_wide=False)["value"], 1000) == MSG_MAX
    assert _resolve_px(_winning_maxwidth(CSS, 1500, has_wide=False)["value"], 1500) == MSG_MAX + 40
    assert _resolve_px(_winning_maxwidth(CSS, 1920, has_wide=False)["value"], 1920) == MSG_MAX + 80


def test_messages_inner_has_a_single_width_authority_no_dead_early_block():
    """The dead early `@media{.messages-inner{max-width:1100px}}` / `1200px` block was
    removed so there is one auditable authority. Its presence would silently shadow the
    real intent again (and formerly made the false-green presence test pass)."""
    assert not re.search(r"\.messages-inner\{max-width:1100px", CSS)
    assert not re.search(r"\.messages-inner\{max-width:1200px", CSS)
    # every surviving `.messages-inner` max-width value is one of the intended authorities
    intended = {"var(--msg-max)", "calc(var(--msg-max) + 40px)", "calc(var(--msg-max) + 80px)",
                "min(1100px, 100%)", "100%"}
    for d in _messages_inner_maxwidth_decls(CSS):
        assert d["value"] in intended, f"unexpected .messages-inner max-width: {d['value']!r}"


def test_mobile_full_bleed_is_preserved():
    """On a phone the winner resolves to the full container (min(1100px,100%) -> 100%),
    so the pre-existing mobile full-bleed behavior is unchanged."""
    assert _resolve_px(_winning_maxwidth(CSS, 390, has_wide=True)["value"], 390) == 390
    # the explicit mobile rule is still present too
    assert re.search(r"@media \(max-width: 700px\)", CSS)


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
