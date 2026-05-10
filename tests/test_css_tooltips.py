"""
Tests for CSS tooltip changes (issue #1775).

Verifies that custom data-tooltip / has-tooltip markup is applied correctly
across index.html, style.css, and i18n.js — replacing native title="" attributes
with a faster, CSS-driven tooltip system.

Run:
    /root/hermes-agent/venv/bin/python -m pytest tests/test_css_tooltips.py -v
"""

import os
import re
import unittest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(BASE_DIR, "static", "index.html")
STYLE_CSS = os.path.join(BASE_DIR, "static", "style.css")
I18N_JS = os.path.join(BASE_DIR, "static", "i18n.js")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Lightweight HTML tag extractor (stdlib-only)
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<(\w+)([^>]*?)(?:/>|>)", re.DOTALL)


def _extract_tags(html, class_filter=None):
    """Return a list of dicts {tag, attrs_str, line} for tags whose class
    attribute contains all tokens in *class_filter* (if given)."""
    results = []
    for m in _TAG_RE.finditer(html):
        tag = m.group(1)
        attrs_str = m.group(2)
        if class_filter:
            cls_match = re.search(r'class="([^"]*)"', attrs_str)
            if not cls_match:
                continue
            classes = cls_match.group(1).split()
            if not all(tok in classes for tok in class_filter):
                continue
        results.append({"tag": tag, "attrs": attrs_str, "match": m})
    return results


def _has_attr(attrs_str, attr_name):
    """Check if a bare attribute name is present in the attrs string.
    Handles both attr_name and attr_name="..."."""
    return bool(re.search(r"\b" + re.escape(attr_name) + r"(?:=|\s|>)", attrs_str))


def _get_attr(attrs_str, attr_name):
    """Get the value of attr="..." from an attrs string, or None.

    Uses a negative lookbehind to avoid matching 'title' inside
    'data-i18n-title' or similar prefixed attributes.
    """
    # Preceding char must be whitespace or start-of-string — not a letter/hyphen.
    m = re.search(r"(?<![a-zA-Z\-])" + re.escape(attr_name) + r'="([^"]*)"', attrs_str)
    return m.group(1) if m else None


# ===========================================================================
# 1. index.html — has-tooltip coverage
# ===========================================================================
class TestIndexHTMLTooltipCoverage(unittest.TestCase):
    """Parse static/index.html and verify tooltip class/attribute coverage."""

    @classmethod
    def setUpClass(cls):
        cls.html = _read(INDEX_HTML)

    # -- helpers -------------------------------------------------------------
    def _find(self, *class_tokens):
        return _extract_tags(self.html, class_filter=class_tokens)

    # -- rail-btn ------------------------------------------------------------
    def test_rail_btn_has_tooltip_class(self):
        """Every .rail-btn element must carry the has-tooltip class."""
        rail_btns = self._find("rail-btn")
        self.assertGreater(len(rail_btns), 0, "No .rail-btn elements found")
        for btn in rail_btns:
            cls_val = _get_attr(btn["attrs"], "class")
            self.assertIn(
                "has-tooltip",
                cls_val,
                f".rail-btn missing has-tooltip class: ...{cls_val[:120]}",
            )

    def test_rail_btn_has_data_tooltip(self):
        """Every .rail-btn element must have data-tooltip attribute."""
        for btn in self._find("rail-btn"):
            self.assertIsNotNone(
                _get_attr(btn["attrs"], "data-tooltip"),
                ".rail-btn missing data-tooltip attribute",
            )

    def test_rail_btn_no_native_title(self):
        """No .rail-btn element should use native title="" attribute."""
        for btn in self._find("rail-btn"):
            self.assertIsNone(
                _get_attr(btn["attrs"], "title"),
                '.rail-btn still has native title="" — should use data-tooltip',
            )

    # -- sidebar-nav .nav-tab ------------------------------------------------
    def _get_sidebar_nav_section(self):
        """Extract the inner HTML of the <div class="sidebar-nav">...</div>."""
        m = re.search(
            r'<div\s+class="sidebar-nav"[^>]*>(.*?)</div>',
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m, 'Could not find <div class="sidebar-nav"> in index.html'
        )
        return m.group(1)

    def test_sidebar_nav_tabs_have_tooltip_class(self):
        """Every .nav-tab inside sidebar-nav must carry has-tooltip class."""
        section = self._get_sidebar_nav_section()
        nav_tabs = _extract_tags(section, class_filter=["nav-tab"])
        self.assertGreater(len(nav_tabs), 0, "No .nav-tab elements in sidebar-nav")
        for tab in nav_tabs:
            cls_val = _get_attr(tab["attrs"], "class")
            self.assertIn(
                "has-tooltip",
                cls_val,
                f"sidebar-nav .nav-tab missing has-tooltip: ...{cls_val[:120]}",
            )

    def test_sidebar_nav_tabs_have_data_tooltip(self):
        """Every .nav-tab inside sidebar-nav must have data-tooltip attribute."""
        section = self._get_sidebar_nav_section()
        for tab in _extract_tags(section, class_filter=["nav-tab"]):
            self.assertIsNotNone(
                _get_attr(tab["attrs"], "data-tooltip"),
                "sidebar-nav .nav-tab missing data-tooltip attribute",
            )

    def test_sidebar_nav_tabs_no_native_title(self):
        """No .nav-tab inside sidebar-nav should use native title=\"\"."""
        section = self._get_sidebar_nav_section()
        for tab in _extract_tags(section, class_filter=["nav-tab"]):
            self.assertIsNone(
                _get_attr(tab["attrs"], "title"),
                'sidebar-nav .nav-tab still has native title="" — should use data-tooltip',
            )

    # -- panel-head-btn ------------------------------------------------------
    def test_panel_head_btn_has_tooltip_class(self):
        """Every .panel-head-btn element must carry has-tooltip class."""
        btns = self._find("panel-head-btn")
        self.assertGreater(len(btns), 0, "No .panel-head-btn elements found")
        for btn in btns:
            cls_val = _get_attr(btn["attrs"], "class")
            self.assertIn(
                "has-tooltip",
                cls_val,
                f".panel-head-btn missing has-tooltip class: ...{cls_val[:120]}",
            )

    def test_panel_head_btn_has_data_tooltip(self):
        """Every .panel-head-btn element must have data-tooltip attribute."""
        for btn in self._find("panel-head-btn"):
            self.assertIsNotNone(
                _get_attr(btn["attrs"], "data-tooltip"),
                ".panel-head-btn missing data-tooltip attribute",
            )

    def test_panel_head_btn_no_native_title(self):
        """No .panel-head-btn element should use native title=\"\"."""
        for btn in self._find("panel-head-btn"):
            self.assertIsNone(
                _get_attr(btn["attrs"], "title"),
                '.panel-head-btn still has native title="" — should use data-tooltip',
            )

    # -- has-tooltip ↔ data-tooltip consistency -----------------------------
    def test_has_tooltip_also_has_data_tooltip(self):
        """Every element with has-tooltip class must also have data-tooltip."""
        all_ht = _extract_tags(self.html, class_filter=["has-tooltip"])
        self.assertGreater(len(all_ht), 0, "No .has-tooltip elements found at all")
        for el in all_ht:
            self.assertIsNotNone(
                _get_attr(el["attrs"], "data-tooltip"),
                "Element with has-tooltip is missing data-tooltip attribute",
            )


# ===========================================================================
# 2. style.css — class definitions
# ===========================================================================
class TestStyleCSSTooltipClasses(unittest.TestCase):
    """Parse static/style.css and verify .has-tooltip CSS rules."""

    @classmethod
    def setUpClass(cls):
        cls.css = _read(STYLE_CSS)

    def test_has_tooltip_class_defined(self):
        """The .has-tooltip base class must be defined."""
        self.assertRegex(
            self.css,
            r"\.has-tooltip\s*\{",
            ".has-tooltip class not found in CSS",
        )

    def test_has_tooltip_after_uses_attr_data_tooltip(self):
        """.has-tooltip::after must use content:attr(data-tooltip)."""
        self.assertRegex(
            self.css,
            r"\.has-tooltip::after\s*\{[^}]*content:\s*attr\(data-tooltip\)",
            ".has-tooltip::after does not use content:attr(data-tooltip)",
        )

    def test_has_tooltip_bottom_defined(self):
        """The .has-tooltip--bottom modifier class must be defined."""
        self.assertRegex(
            self.css,
            r"\.has-tooltip--bottom\s*(?:::[\w-]+)?\s*\{",
            ".has-tooltip--bottom class not found in CSS",
        )

    def test_hover_and_focus_visible_trigger_opacity(self):
        """Both :hover and :focus-visible must trigger opacity on ::after."""
        # Look for a rule that combines both selectors
        hover_match = re.search(
            r"\.has-tooltip:hover::after\s*\{[^}]*opacity",
            self.css,
        )
        focus_match = re.search(
            r"\.has-tooltip:focus-visible::after\s*\{[^}]*opacity",
            self.css,
        )
        # Also accept combined selectors: .has-tooltip:hover::after,.has-tooltip:focus-visible::after
        if not hover_match:
            combined = re.search(
                r"\.has-tooltip:hover::after\s*,\s*\.has-tooltip:focus-visible::after\s*\{[^}]*opacity",
                self.css,
            )
            self.assertTrue(
                combined,
                ":hover does not trigger opacity on .has-tooltip::after",
            )
        if not focus_match and not (
            hover_match
            and re.search(
                r"\.has-tooltip:focus-visible::after",
                self.css,
            )
        ):
            self.fail(
                ":focus-visible does not trigger opacity on .has-tooltip::after",
            )

    def test_prefers_reduced_motion_exists(self):
        """A prefers-reduced-motion media query must exist for .has-tooltip."""
        self.assertRegex(
            self.css,
            r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{[^}]*\.has-tooltip",
            "No prefers-reduced-motion media query found for .has-tooltip",
        )


# ===========================================================================
# 3. i18n.js — data-tooltip sync
# ===========================================================================
class TestI18NTooltipSync(unittest.TestCase):
    """Parse static/i18n.js and verify data-tooltip sync in data-i18n-title handler."""

    @classmethod
    def setUpClass(cls):
        cls.js = _read(I18N_JS)

    def test_data_tooltip_synced_in_i18n_title_handler(self):
        """The data-i18n-title handler must also sync data-tooltip attribute."""
        # Find the data-i18n-title forEach block
        block_match = re.search(
            r"document\.querySelectorAll\(\s*'\[data-i18n-title\]'\s*\)"
            r"\.forEach\s*\(\s*el\s*=>\s*\{(.*?)\}\s*\)",
            self.js,
            re.DOTALL,
        )
        self.assertIsNotNone(
            block_match,
            "Could not find data-i18n-title forEach handler in i18n.js",
        )
        block = block_match.group(1)
        # Must reference setAttribute('data-tooltip', ...) or data-tooltip sync
        self.assertRegex(
            block,
            r"setAttribute\s*\(\s*['\"]data-tooltip['\"]",
            "data-i18n-title handler does not sync data-tooltip attribute",
        )

    def test_sync_only_fires_when_both_present(self):
        """The data-tooltip sync must guard on el.hasAttribute('data-tooltip')."""
        block_match = re.search(
            r"document\.querySelectorAll\(\s*'\[data-i18n-title\]'\s*\)"
            r"\.forEach\s*\(\s*el\s*=>\s*\{(.*?)\}\s*\)",
            self.js,
            re.DOTALL,
        )
        self.assertIsNotNone(block_match, "Could not find data-i18n-title handler")
        block = block_match.group(1)
        # Must guard with hasAttribute('data-tooltip')
        self.assertRegex(
            block,
            r"el\.hasAttribute\s*\(\s*['\"]data-tooltip['\"]\s*\)",
            "data-tooltip sync does not guard on hasAttribute('data-tooltip')",
        )

    def test_native_title_cleared_when_custom_tooltip_present(self):
        """When the element has a custom data-tooltip, i18n.js must NOT also
        set el.title (otherwise the slow ~1.5s native browser tooltip co-fires
        alongside the fast custom CSS tooltip — exactly the bug #1775 reports).
        It must explicitly removeAttribute('title') so any stale runtime
        value gets dropped."""
        block_match = re.search(
            r"document\.querySelectorAll\(\s*'\[data-i18n-title\]'\s*\)"
            r"\.forEach\s*\(\s*el\s*=>\s*\{(.*?)\}\s*\)",
            self.js,
            re.DOTALL,
        )
        self.assertIsNotNone(block_match, "Could not find data-i18n-title handler")
        block = block_match.group(1)
        self.assertRegex(
            block,
            r"removeAttribute\s*\(\s*['\"]title['\"]\s*\)",
            "data-i18n-title handler must clear el.title when data-tooltip is "
            "present so the native ~1.5s tooltip does not co-fire alongside "
            "the fast custom CSS tooltip (#1775).",
        )

    def test_native_title_path_preserved_for_non_tooltip_elements(self):
        """Elements that opt OUT of custom tooltips (no data-tooltip attribute)
        must still get el.title from data-i18n-title — falling back gracefully
        to the native tooltip rather than rendering nothing."""
        block_match = re.search(
            r"document\.querySelectorAll\(\s*'\[data-i18n-title\]'\s*\)"
            r"\.forEach\s*\(\s*el\s*=>\s*\{(.*?)\}\s*\)",
            self.js,
            re.DOTALL,
        )
        self.assertIsNotNone(block_match, "Could not find data-i18n-title handler")
        block = block_match.group(1)
        self.assertIn(
            "el.title",
            block,
            "data-i18n-title handler must still assign el.title for "
            "elements without data-tooltip (non-rail, non-nav surfaces).",
        )


# ---------------------------------------------------------------------------
# Rail tooltip cascade regression (post-v0.51.17 follow-up)
# ---------------------------------------------------------------------------
class RailTooltipCascadeTests(unittest.TestCase):
    """Pin the cascade fix that lets `.has-tooltip` work on `.rail .nav-tab`.

    Background: the legacy `.nav-tab:hover::after { content: attr(data-label) }`
    rule was paired with a `.rail .nav-tab:hover::after { content: none }` rule
    that suppressed it on the desktop rail. After v0.51.17 migrated rail icons
    to `.has-tooltip`, the suppression rule's specificity (0,3,1) outweighed
    `.has-tooltip:hover::after` (0,2,1), and `content: none` removes the
    pseudo-element entirely — so rail tooltips never appeared. Fix: scope the
    legacy `data-label` tooltip to `.sidebar-nav .nav-tab` only and drop the
    rail suppression rule.
    """

    def setUp(self):
        self.css = _read(STYLE_CSS)

    def test_rail_nav_tab_hover_after_killer_is_gone(self):
        """The `.rail .nav-tab:hover::after { content: none }` rule MUST NOT
        exist — it kills the `.has-tooltip` pseudo-element on rail buttons."""
        # Strip CSS comments first so the test doesn't false-positive on the
        # explanatory note left in place after the rule's removal.
        css_no_comments = re.sub(r"/\*.*?\*/", "", self.css, flags=re.DOTALL)
        pattern = re.compile(
            r"\.rail\s+\.nav-tab:hover:{1,2}after\s*\{[^}]*content\s*:\s*none\s*[;}]",
            re.DOTALL,
        )
        match = pattern.search(css_no_comments)
        self.assertIsNone(
            match,
            f"Found re-added killer rule that nukes rail tooltips: {match.group(0)[:120] if match else ''}",
        )

    def test_legacy_data_label_hover_is_scoped_to_sidebar_nav(self):
        """The legacy `data-label` hover tooltip must be scoped to
        `.sidebar-nav .nav-tab` — otherwise it fires on rail buttons (which
        carry no data-label) and renders an empty styled box on hover."""
        css_no_comments = re.sub(r"/\*.*?\*/", "", self.css, flags=re.DOTALL)
        # The unscoped bug form: `.nav-tab:hover::after { content: attr(data-label) }`
        # at the START of a selector (i.e. after `}` or whitespace+nothing-else).
        # Walk every rule whose selector ends with `.nav-tab:hover::after` and
        # check the prefix that comes before `.nav-tab`. If the prefix is empty
        # or pure whitespace, the rule is unscoped.
        for m in re.finditer(
            r"([^{}]*?)\.nav-tab:hover:{1,2}after\s*\{([^}]*content\s*:\s*attr\(data-label\)[^}]*)\}",
            css_no_comments,
            re.DOTALL,
        ):
            prefix = m.group(1)
            # If the prefix (back to the previous `}` or `;`) is empty or pure
            # whitespace, this is the unscoped bug form.
            # Trim to the part after the last selector-list separator.
            last_sep = max(prefix.rfind("}"), prefix.rfind("\n"), prefix.rfind(","))
            scope_text = (
                prefix[last_sep + 1 :].strip() if last_sep >= 0 else prefix.strip()
            )
            self.assertTrue(
                scope_text,
                "Found unscoped `.nav-tab:hover::after { content: attr(data-label) }` "
                "rule. Must be `.sidebar-nav .nav-tab:hover::after` so it does not "
                "fire on rail buttons that carry no data-label.",
            )

        # Affirmative: the scoped form must exist.
        good_pattern = re.compile(
            r"\.sidebar-nav\s+\.nav-tab:hover:{1,2}after\s*\{[^}]*content\s*:\s*attr\(data-label\)",
            re.DOTALL,
        )
        self.assertIsNotNone(
            good_pattern.search(css_no_comments),
            "Expected `.sidebar-nav .nav-tab:hover::after { content: attr(data-label); ... }` "
            "rule (mobile sidebar fallback tooltip). It went missing.",
        )

    def test_all_rail_buttons_carry_has_tooltip(self):
        """Every `.rail-btn.nav-tab` button must carry `class="has-tooltip"` and
        a non-empty `data-tooltip` attribute. Otherwise the rail tooltip is
        invisible regardless of the cascade fix above."""
        html = _read(INDEX_HTML)
        # Find the rail block: <nav class="rail" ...> ... </nav>
        rail_match = re.search(
            r'<nav class="rail"[^>]*>(.*?)</nav>',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(
            rail_match, "Could not locate <nav class='rail'> in index.html"
        )
        rail_block = rail_match.group(1)

        rail_btn_count = 0
        missing = []
        for m in re.finditer(r"<button\b([^>]*?)>", rail_block):
            attrs = m.group(1)
            if "rail-btn" not in attrs:
                continue
            rail_btn_count += 1
            if "has-tooltip" not in attrs:
                missing.append(("class missing has-tooltip", attrs[:120]))
                continue
            tooltip_attr = re.search(r'data-tooltip="([^"]*)"', attrs)
            if not tooltip_attr or not tooltip_attr.group(1).strip():
                missing.append(("missing or empty data-tooltip", attrs[:120]))

        self.assertGreaterEqual(
            rail_btn_count,
            10,
            f"Expected ≥10 rail buttons (found {rail_btn_count}). Test selector wrong?",
        )
        self.assertEqual(
            missing,
            [],
            "Rail buttons without working tooltip markup:\n  "
            + "\n  ".join(f"{reason}: {attrs}" for reason, attrs in missing),
        )


# ---------------------------------------------------------------------------
# `--bottom-right` variant: anchors tooltip's RIGHT edge to a trigger that sits
# flush with its container's right edge, so the label extends inward instead of
# overflowing past the panel edge. Used by `#btnNewChat`.
# ---------------------------------------------------------------------------
class BottomRightTooltipVariantTests(unittest.TestCase):
    def setUp(self):
        self.css = _read(STYLE_CSS)
        self.html = _read(INDEX_HTML)

    def test_bottom_right_variant_defined(self):
        """`.has-tooltip--bottom-right::after` must exist and right-anchor the
        tooltip (`right: 0` and no `transform: translateX`)."""
        rule = re.search(
            r"\.has-tooltip--bottom-right:{1,2}after\s*\{([^}]*)\}",
            self.css,
            re.DOTALL,
        )
        self.assertIsNotNone(rule, "`.has-tooltip--bottom-right::after` rule missing")
        body = rule.group(1)
        # Must anchor right edge.
        self.assertRegex(
            body, r"right\s*:\s*0", "--bottom-right variant must set right:0"
        )
        # Must clear the inherited `left:` so it doesn't fight with the base rule.
        self.assertRegex(
            body, r"left\s*:\s*auto", "--bottom-right variant must clear left:auto"
        )
        # Must clear the inherited transform (otherwise translateX(-50%) shifts it).
        self.assertRegex(
            body,
            r"transform\s*:\s*none",
            "--bottom-right variant must reset transform:none",
        )

    def test_btn_new_chat_uses_bottom_right_variant(self):
        """`#btnNewChat` sits flush with the chat-panel right edge; its tooltip
        previously overflowed (with `--bottom`, half clips past the panel).
        Must now use `--bottom-right`, NOT `--bottom`."""
        match = re.search(
            r'<button[^>]*\bid="btnNewChat"[^>]*>',
            self.html,
        )
        self.assertIsNotNone(match, "Could not find #btnNewChat button")
        attrs = match.group(0)
        self.assertIn(
            "has-tooltip--bottom-right",
            attrs,
            "#btnNewChat must carry has-tooltip--bottom-right so its tooltip "
            "doesn't overflow the chat-panel right edge.",
        )
        # Must NOT also carry the old --bottom (would conflict).
        self.assertNotRegex(
            attrs,
            r"has-tooltip--bottom(?!-)",
            "#btnNewChat carries both --bottom and --bottom-right; pick one. "
            "The plain --bottom variant centers on left:50% and overflows.",
        )


if __name__ == "__main__":
    unittest.main()
