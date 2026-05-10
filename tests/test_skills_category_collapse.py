"""Tests for collapsible skill categories in the Skills panel.

Validates that renderSkills() produces collapsible category headers
with chevron toggles, click handlers, and persisted collapse state.
"""

import os
import re


def _readpanels():
    with open(os.path.join("static", "panels.js")) as f:
        return f.read()


def _readcss():
    with open(os.path.join("static", "style.css")) as f:
        return f.read()


# ── State variable ──────────────────────────────────────────────────────────


class TestCollapseState:
    """A Set must track collapsed categories across re-renders."""

    def test_collapsed_cats_set_exists(self):
        p = _readpanels()
        assert "_collapsedCats" in p, "_collapsedCats Set must exist"
        assert "new Set()" in p, "_collapsedCats must be initialized as Set"

    def test_toggle_function_exists(self):
        p = _readpanels()
        assert "_toggleCatCollapse" in p, "_toggleCatCollapse() function must exist"


# ── renderSkills produces collapsible headers ──────────────────────────────


class TestRenderSkillsCollapse:
    """renderSkills() must render category headers with chevron icons and click handlers."""

    def test_chevron_icon_used_instead_of_folder(self):
        p = _readpanels()
        idx = p.find("function renderSkills(")
        body = p[idx : idx + 2000]
        assert "chevron-right" in body, "Must use chevron-right icon instead of folder"
        assert "li('folder'" not in body, "Must not use folder icon anymore"

    def test_cat_header_has_dataset_cat(self):
        p = _readpanels()
        idx = p.find("function renderSkills(")
        body = p[idx : idx + 2000]
        assert "dataset.cat" in body, "Header must store category in data-cat attribute"

    def test_cat_header_has_click_handler(self):
        p = _readpanels()
        idx = p.find("function renderSkills(")
        body = p[idx : idx + 2000]
        assert "hdr.onclick" in body or "onclick" in body, (
            "Header must have onclick handler"
        )

    def test_collapsed_class_toggled(self):
        p = _readpanels()
        idx = p.find("function renderSkills(")
        body = p[idx : idx + 2000]
        assert "collapsed" in body, "Must apply collapsed class based on state"

    def test_skill_items_hidden_when_collapsed(self):
        p = _readpanels()
        idx = p.find("function renderSkills(")
        body = p[idx : idx + 2000]
        assert "'none'" in body and "style.display" in body, (
            "Skill items must be hidden when category is collapsed"
        )

    def test_chevron_rotation_on_collapse(self):
        p = _readpanels()
        idx = p.find("function renderSkills(")
        body = p[idx : idx + 2000]
        assert "rotate(90deg)" in body, "Chevron must rotate 90deg when expanded"

    def test_renderSkills_preserves_search_query(self):
        """Search query must still be read and applied before grouping."""
        p = _readpanels()
        idx = p.find("function renderSkills(")
        body = p[idx : idx + 500]
        assert "skillsSearch" in body, "Must read search input value"
        assert "toLowerCase().includes(query)" in body, (
            "Must filter by name/description/category"
        )


# ── _toggleCatCollapse DOM manipulation ────────────────────────────────────


class TestToggleCatCollapse:
    """_toggleCatCollapse() must toggle DOM without full re-render."""

    def test_toggles_set_membership(self):
        p = _readpanels()
        idx = p.find("function _toggleCatCollapse(")
        body = p[idx : idx + 500]
        assert "_collapsedCats.has(cat)" in body
        assert "_collapsedCats.delete(cat)" in body
        assert "_collapsedCats.add(cat)" in body

    def test_queries_skills_category_elements(self):
        p = _readpanels()
        idx = p.find("function _toggleCatCollapse(")
        body = p[idx : idx + 800]
        assert ".skills-category" in body, "Must query .skills-category elements"

    def test_matches_by_dataset_cat(self):
        p = _readpanels()
        idx = p.find("function _toggleCatCollapse(")
        body = p[idx : idx + 800]
        assert "header.dataset.cat === cat" in body or "dataset.cat" in body, (
            "Must match category by data attribute"
        )

    def test_toggles_skill_item_display(self):
        p = _readpanels()
        idx = p.find("function _toggleCatCollapse(")
        body = p[idx : idx + 800]
        assert ".skill-item" in body, "Must query .skill-item elements"
        assert "display = collapsed ? 'none'" in body or "style.display" in body, (
            "Must toggle display property"
        )

    def test_toggles_chevron_rotation(self):
        p = _readpanels()
        idx = p.find("function _toggleCatCollapse(")
        body = p[idx : idx + 800]
        assert ".cat-chevron" in body, "Must select chevron element"
        assert "rotate" in body, "Must toggle rotation on chevron"


# ── CSS ────────────────────────────────────────────────────────────────────


class TestCSSClasses:
    """CSS must support collapsible categories."""

    def test_cat_chevron_class(self):
        css = _readcss()
        assert ".cat-chevron" in css, ".cat-chevron class must exist in CSS"

    def test_cat_chevron_has_fixed_size(self):
        css = _readcss()
        m = re.search(r"\.cat-chevron\{[^}]+\}", css)
        assert m, ".cat-chevron rule must exist"
        assert "width" in m.group(), "Chevron must have fixed width"
        assert "flex-shrink" in m.group(), "Chevron must not shrink"

    def test_skills_cat_header_user_select_none(self):
        css = _readcss()
        m = re.search(r"\.skills-cat-header\{[^}]+\}", css)
        assert m, ".skills-cat-header rule must exist"
        assert "user-select" in m.group(), (
            "Header must have user-select:none to prevent text selection on click"
        )

    def test_skills_cat_header_has_cursor_pointer(self):
        css = _readcss()
        m = re.search(r"\.skills-cat-header\{[^}]+\}", css)
        assert m, ".skills-cat-header rule must exist"
        assert "cursor:pointer" in m.group(), "Header must have cursor:pointer"
