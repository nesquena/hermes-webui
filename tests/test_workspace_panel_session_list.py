"""Regression tests for two related sidebar/panel UI fixes.

1. Workspace panel header collapse priority — as the right panel narrows,
   the git-badge must vanish first, the "Workspace" label second, and the
   icon buttons last. Previously all three compressed simultaneously
   because `.panel-header` used `justify-content:space-between` with no
   flex-shrink ratios or container queries.

2. Project color dot truncation — the dot used to be appended INSIDE the
   `.session-title` span (which is `overflow:hidden;text-overflow:ellipsis`),
   so the dot got clipped along with long titles. Fix moves the dot to a
   flex sibling in `.session-title-row` between title and timestamp, and
   moves `.session-time` from `position:absolute` to flex flow so the
   title's `flex:1` bound stops at the timestamp's left edge.
"""

import pathlib

REPO = pathlib.Path(__file__).parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _extract_js_function_body(src: str, name: str) -> str:
    start = src.find(f"function {name}(")
    assert start >= 0, f"function {name} not found"
    brace = src.find("{", start)
    assert brace >= 0, f"function {name} body not found"
    depth = 1
    i = brace + 1
    while depth > 0 and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"function {name} body did not close"
    return src[start:i]


# ── Bug 1: workspace panel header collapse priority ──────────────────────────


class TestWorkspacePanelCollapsePriority:

    def test_rightpanel_is_a_size_container(self):
        """The right panel must declare itself as an inline-size container so
        its descendants can run @container queries against the panel's width."""
        # Look at the .rightpanel rule body
        idx = STYLE_CSS.find(".rightpanel{")
        assert idx >= 0, ".rightpanel rule not found"
        rule = STYLE_CSS[idx: idx + 1200]
        assert "container-type:inline-size" in rule, (
            ".rightpanel must declare container-type:inline-size for the "
            "header collapse-priority @container queries to work."
        )
        assert "container-name:rightpanel" in rule, (
            ".rightpanel should be named 'rightpanel' so descendants can "
            "scope @container queries explicitly."
        )

    def test_panel_header_no_longer_uses_space_between(self):
        """`justify-content:space-between` was the root cause of the
        simultaneous-shrink behaviour. The header now uses `gap` and
        `margin-left:auto` on `.panel-actions` to push them right."""
        idx = STYLE_CSS.find(".panel-header{")
        rule = STYLE_CSS[idx: STYLE_CSS.find("}", idx) + 1]
        assert "justify-content:space-between" not in rule, (
            "panel-header still uses justify-content:space-between — that "
            "compresses all three children simultaneously."
        )
        assert "gap:6px" in rule
        # Note: `.panel-header` was changed from overflow:hidden to overflow:visible
        # in #1775 so its tooltip pseudo-elements can escape the header bar
        # (otherwise the workspace-panel header tooltips like "New file" get
        # clipped). The title-text ellipsis is preserved by the inner span
        # `.workspace-panel-title-group > span:first-child` which has its own
        # overflow:hidden + text-overflow:ellipsis. So we check that EITHER
        # the parent uses overflow:hidden (legacy) or that the inner span
        # handles its own ellipsis (current).
        if "overflow:hidden" not in rule:
            inner_span_idx = STYLE_CSS.find(".workspace-panel-title-group > span:first-child{")
            assert inner_span_idx != -1, (
                ".panel-header lost overflow:hidden but no inner span "
                "rule (.workspace-panel-title-group > span:first-child) handles the "
                "title-text ellipsis as a fallback."
            )
            inner_rule = STYLE_CSS[inner_span_idx: STYLE_CSS.find("}", inner_span_idx) + 1]
            assert "overflow:hidden" in inner_rule and "text-overflow:ellipsis" in inner_rule, (
                ".workspace-panel-title-group > span:first-child must own the ellipsis "
                "behaviour now that the parent is overflow:visible."
            )

    def test_panel_actions_pushed_right_and_never_shrinks(self):
        """`.panel-actions` must have flex-shrink:0 and margin-left:auto so
        the icon buttons stay visible no matter how narrow the panel gets,
        and they sit at the right edge once `space-between` is removed."""
        idx = STYLE_CSS.find(".panel-actions{")
        rule = STYLE_CSS[idx: STYLE_CSS.find("}", idx)]
        assert "flex-shrink:0" in rule, (
            ".panel-actions must not shrink — icons are the priority."
        )
        assert "margin-left:auto" in rule, (
            ".panel-actions must use margin-left:auto to push to the right "
            "now that justify-content:space-between is gone."
        )

    def test_workspace_label_shrinks_with_ellipsis(self):
        """The "Workspace" label must shrink with ellipsis truncation."""
        # Find the rule
        sel = ".workspace-panel-title-group > span:first-child"
        idx = STYLE_CSS.find(sel)
        assert idx >= 0, f"Selector {sel!r} not found in style.css"
        rule = STYLE_CSS[idx: STYLE_CSS.find("}", idx)]
        assert "text-overflow:ellipsis" in rule
        assert "min-width:0" in rule

    def test_git_badge_uses_second_row(self):
        """`.git-badge` should sit beneath the title/action row."""
        idx = STYLE_CSS.find(".git-badge{")
        rule = STYLE_CSS[idx: STYLE_CSS.find("}", idx)]
        assert "grid-column:1 / -1" in rule
        assert "grid-row:2" in rule

    def test_container_query_hides_git_badge_first(self):
        """At narrow widths the git badge gets `display:none` BEFORE the
        label is hidden — git badge first."""
        # The container query block for hiding git badge
        assert "@container rightpanel (max-width: 220px)" in STYLE_CSS, (
            "Missing @container rule to hide .git-badge at narrow widths"
        )
        # Find the block and check git-badge is targeted
        idx = STYLE_CSS.find("@container rightpanel (max-width: 220px)")
        block = STYLE_CSS[idx: idx + 200]
        assert ".git-badge{display:none" in block

    def test_container_query_hides_label_at_narrower_width(self):
        """The label hides at a NARROWER threshold than the git badge —
        confirms collapse priority order."""
        assert "@container rightpanel (max-width: 160px)" in STYLE_CSS
        idx = STYLE_CSS.find("@container rightpanel (max-width: 160px)")
        block = STYLE_CSS[idx: idx + 200]
        assert ".workspace-panel-title-group{display:none" in block

    def test_breakpoints_in_correct_order(self):
        """Sanity: the git-badge breakpoint (220px) must be wider than the
        label breakpoint (160px). Otherwise the label would vanish first."""
        # Both queries exist — extract numeric thresholds
        import re
        matches = re.findall(
            r"@container rightpanel \(max-width:\s*(\d+)px\)", STYLE_CSS
        )
        assert len(matches) >= 2
        thresholds = [int(m) for m in matches]
        # First threshold (git badge) must be larger than label threshold
        assert thresholds[0] > thresholds[1], (
            f"Git badge breakpoint ({thresholds[0]}px) must be wider than "
            f"label breakpoint ({thresholds[1]}px) so git-badge hides first."
        )


# ── Bug 2: project color dot placement ───────────────────────────────────────


class TestProjectDotPlacement:

    def test_dot_appended_to_title_row_not_title(self):
        """The project dot must be appended to `titleRow` (a flex sibling
        of the title and timestamp), not to the title span (which truncates
        with ellipsis and would clip the dot off long titles)."""
        # Find _renderOneSession body
        body = _extract_js_function_body(SESSIONS_JS, "_renderOneSession")
        # Must append dot to titleRow
        assert "titleRow.appendChild(dot)" in body, (
            "Project dot must be appended to titleRow as a flex sibling, "
            "not inside the truncating title span"
        )
        # Must NOT append dot to title (the truncating span)
        assert "title.appendChild(dot)" not in body, (
            "Old behaviour — dot inside title span gets clipped by the "
            "ellipsis truncation. Dot must live in titleRow instead."
        )

    def test_dot_placed_between_title_and_timestamp(self):
        """The dot is appended AFTER title.appendChild and BEFORE ts append
        — that ordering puts the dot between the title and the timestamp
        in the flex row."""
        body = _extract_js_function_body(SESSIONS_JS, "_renderOneSession")
        title_pos = body.find("titleRow.appendChild(title);")
        dot_pos = body.find("titleRow.appendChild(dot);")
        ts_pos = body.find("titleRow.appendChild(ts);")
        assert title_pos >= 0 and dot_pos >= 0 and ts_pos >= 0
        assert title_pos < dot_pos < ts_pos, (
            f"Order must be title → dot → ts in the title row "
            f"(positions: {title_pos}, {dot_pos}, {ts_pos})"
        )

    def test_session_time_uses_flex_flow_not_absolute(self):
        """`.session-time` must use margin-left:auto in flex flow, not
        position:absolute. Without this the title's flex:1 runs underneath
        the absolute-positioned timestamp and the dot has no anchor."""
        # Get the bare .session-time rule (not .session-time.is-hidden, not
        # .session-item:hover .session-time)
        idx = STYLE_CSS.find(".session-time{")
        rule = STYLE_CSS[idx: STYLE_CSS.find("}", idx)]
        assert "position:absolute" not in rule, (
            ".session-time must not be position:absolute — bug 2 requires "
            "it to live in the flex flow of .session-title-row."
        )
        assert "margin-left:auto" in rule, (
            ".session-time must use margin-left:auto to push to the right "
            "edge of the flex row."
        )

    def test_session_project_dot_no_inline_block_baggage(self):
        """`.session-project-dot` is now a flex sibling — the row's gap:6px
        handles spacing, so the old `margin-left:4px` and
        `vertical-align:middle` are unnecessary and only confuse layout."""
        idx = STYLE_CSS.find(".session-project-dot{")
        assert idx >= 0
        rule = STYLE_CSS[idx: STYLE_CSS.find("}", idx)]
        assert "margin-left:4px" not in rule, (
            "Old margin-left:4px is unnecessary now — gap:6px on "
            ".session-title-row handles spacing"
        )
        assert "vertical-align:middle" not in rule, (
            "vertical-align is meaningless inside flex flow"
        )
        assert "flex-shrink:0" in rule, (
            "Dot must not shrink (would disappear at narrow sidebar widths)"
        )

    def test_session_item_padding_at_rest_no_longer_reserves_86px(self):
        """At rest (no hover, no streaming, no unread), the session item
        no longer reserves 86px for the absolute timestamp — that space
        was wasted now that the timestamp lives in flex flow."""
        # Find the FIRST .session-item{ rule (the desktop one, not the
        # mobile-touch override).
        idx = STYLE_CSS.find(".session-item{padding:8px")
        assert idx >= 0, "Could not find desktop .session-item padding rule"
        rule = STYLE_CSS[idx: STYLE_CSS.find("}", idx)]
        assert "padding:8px 8px" in rule, (
            f"Expected 'padding:8px 8px' for at-rest session items, got: {rule!r}"
        )
        # Mobile also drops from 86px to 40px — the absolute timestamp is
        # gone (now flex-flow), so only the always-visible action button's
        # footprint (26px + 6px gap ≈ 32px, rounded to 40px) needs reservation.
        assert ".session-item{min-height:44px;padding:10px 40px 10px 12px;}" in STYLE_CSS

    def test_session_item_expands_padding_on_hover_and_attention(self):
        """PR #1110: Touch layout-shift fix — :hover removed from the COMBINED
        padding-right selector. Touch devices (iPad, phone) see hover:none so
        they skip the @media (hover:hover) block below. Mouse devices see
        hover:hover and get the padding-right on hover.
        streaming/unread/focus-within/menu-open expand to 40px for all devices."""
        # Touch-safe combined rule (no :hover in this one)
        sel = (
            ".session-item.streaming,.session-item.unread,"
            ".session-item:focus-within,"
            ".session-item.menu-open"
        )
        idx = STYLE_CSS.find(sel)
        assert idx >= 0, (
            "Combined streaming/unread/focus-within/menu-open padding rule not found"
        )
        rule = STYLE_CSS[idx: STYLE_CSS.find("}", idx)]
        assert "padding-right:40px" in rule
        # Desktop hover padding restored via @media (hover:hover) — mouse devices only
        assert "@media (hover:hover)" in STYLE_CSS
        assert ".session-item:hover{padding-right:40px;}" in STYLE_CSS
