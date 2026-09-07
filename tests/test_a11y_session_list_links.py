"""Conversation list rows must be LINKS, not text without a role.

Reported issue: "those sessions are plain text and the user does not know that
they can do anything with them; they should be links".

Measured state before the fix (Edge + CDP, accessibility tree on the running
application): the row was a `DIV` without `role`, without `tabindex`, and
without `href`; in the whole sidebar there were ZERO `role=link` nodes, and 40
Tab presses did not stop on a row even once. The screen reader had no way to
announce that the row did anything (WCAG 4.1.2), and the keyboard had no way to
activate it (WCAG 2.1.1).

State after the fix: the title is `<a href="/session/<id>">`, so NVDA reports
role 19 (LINK) + State.LINKED + State.FOCUSABLE with name and address, and
focus reaches it by Tab. In addition, features a div cannot provide now work:
Ctrl+click / middle button open the conversation in a new tab, and the browser
context menu can copy its address.

The tests are source-level (static) - full behavioral evidence requires a
browser and a live screen reader, and those were run separately during the fix.
Here we pin the contract that must not silently disappear in the next rendering
edit.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
A11Y_JS = (REPO / "static" / "a11y-helpers.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")


class TestSessionTitleIsARealLink:

    def test_title_element_is_an_anchor_not_a_span(self):
        """The role must come from the element, not from an attribute glued on later."""
        assert "const title=document.createElement('a');" in SESSIONS_JS, (
            "The session title must be <a> so the screen reader announces it as a link "
            "(WCAG 4.1.2). The <span> version told the user nothing."
        )
        assert "const title=document.createElement('span');\n    title.className='session-title';" not in SESSIONS_JS

    def test_title_gets_href_pointing_at_the_session_url(self):
        assert "title.setAttribute('href',_sessionUrlForSid(s.session_id))" in SESSIONS_JS, (
            "The link must have href with the address of THIS conversation - otherwise it is a decoy: "
            "opening in a new tab and copying the address will not work."
        )

    def test_session_url_is_actually_served_so_the_link_is_not_a_decoy(self):
        """The href address must be served by the server, not invented."""
        assert 'parsed.path.startswith("/session/")' in ROUTES_PY, (
            "href points to /session/<id>; if the server stops returning the app there, "
            "the link becomes a decoy leading to 404."
        )

    def test_active_row_marks_aria_current(self):
        assert "if(isActive) title.setAttribute('aria-current','page');" in SESSIONS_JS, (
            "The current conversation was marked only with the CSS class 'active'. "
            "For a screen reader the class does not exist - aria-current is required."
        )

    def test_modified_click_is_left_to_the_browser(self):
        """Ctrl/Cmd/Shift/Alt/middle button = new tab, we do not intercept that."""
        idx = SESSIONS_JS.find("title.addEventListener('click'")
        assert idx > 0, "brak obslugi klikniecia na linku tytulu"
        blok = SESSIONS_JS[idx:idx + 600]
        assert "e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||e.button===1" in blok, (
            "A modified click must reach the browser, otherwise we lose opening the conversation "
            "in a new tab - the main gain of a real link."
        )
        assert "return" in blok

    def test_keyboard_activation_opens_session_in_place(self):
        """Keyboard Enter (detail===0) opens the conversation without reload."""
        idx = SESSIONS_JS.find("title.addEventListener('click'")
        blok = SESSIONS_JS[idx:idx + 600]
        assert "e.detail===0" in blok, (
            "Keyboard activation must be recognized (click detail===0) "
            "to open the conversation through the app path instead of reloading the page."
        )
        assert "_openSidebarSession(s)" in blok


class TestSidebarClickablesHaveRoleAndKeyboard:
    """The same defect class outside the row: 7 clickable elements without a role."""

    def test_shared_helpers_exist(self):
        assert "function a11yAsButton(el, opts)" in A11Y_JS
        assert "function a11yAsLink(el, href, opts)" in A11Y_JS
        assert "window.a11yAsButton = a11yAsButton;" in A11Y_JS
        assert "window.a11yAsLink = a11yAsLink;" in A11Y_JS

    def test_button_helper_handles_enter_and_space(self):
        idx = A11Y_JS.find("function a11yAsButton(el, opts)")
        blok = A11Y_JS[idx:A11Y_JS.find("function a11yAsLink", idx)]
        assert "ev.key !== 'Enter' && ev.key !== ' '" in blok
        assert "ev.preventDefault();" in blok, (
            "Space without preventDefault scrolls the page under the user."
        )
        assert "role', 'button'" in blok
        assert "tabindex', '0'" in blok

    def test_key_handler_is_attached_once_per_element(self):
        """The list is rerendered often - double listeners mean double action."""
        assert "dataset.a11yKeyActivated === '1'" in A11Y_JS

    def test_date_group_header_is_a_button_with_expanded_state(self):
        assert "a11yAsButton(hdr,{expanded:!isGroupCollapsed" in SESSIONS_JS, (
            "The group header collapses the list, and the collapsed state was shown only by "
            "a rotated caret - the screen reader needs aria-expanded."
        )

    def test_project_filter_chips_expose_pressed_state(self):
        for call in (
            "a11yAsButton(allChip,{pressed:!_activeProject",
            "a11yAsButton(noneChip,{pressed:_activeProject===NO_PROJECT_FILTER",
            "a11yAsButton(chip,{pressed:p.project_id===_activeProject",
        ):
            assert call in SESSIONS_JS, (
                f"no selected state on the filter chip: {call}"
            )

    def test_remaining_sidebar_toggles_are_buttons(self):
        assert "a11yAsButton(pfToggle)" in SESSIONS_JS
        assert "a11yAsButton(toggle,{pressed:_showArchived})" in SESSIONS_JS
        assert "a11yAsButton(more)" in SESSIONS_JS
        assert "a11yAsButton(toggleBtn,{label:t('session_select_mode')})" in SESSIONS_JS

    def test_helper_calls_are_capability_guarded(self):
        """Node harnesses inject mock DOMs - an unguarded call breaks them."""
        for line in SESSIONS_JS.splitlines():
            if "a11yAsButton(" in line and "function a11yAsButton" not in line:
                assert "typeof a11yAsButton==='function'" in line, (
                    f"helper call without typeof guard: {line.strip()[:90]}"
                )


class TestLinkLooksUnchangedButFocusIsVisible:

    def test_anchor_keeps_the_previous_span_appearance(self):
        """The link must look exactly like the former <span>.

        NOTE about `color`: NOT `inherit`. The first version of this fix used
        `color:inherit` and a visual regression was MEASURED - `inherit` takes
        the color from the PARENT (.session-item has var(--muted)), while the
        former <span class=session-title> had its own rule with var(--text).
        Titles turned gray rgb(192,192,192) instead of rgb(255,248,220), so the
        accessibility fix dimmed the entire conversation list. Therefore it must
        explicitly use var(--text).
        """
        assert "a.session-title{text-decoration:none;color:var(--text);cursor:pointer;}" in STYLE_CSS, (
            "The link must not change the list appearance: no underline, color "
            "var(--text) (NOT inherit - inherit pulls gray from the row)."
        )
        assert "color:inherit" not in STYLE_CSS.split("a.session-title")[1][:120], (
            "color:inherit on the title link dims the list (measured)."
        )
        # The active row has its own accent color and its rule MUST win:
        # .session-item.active .session-title = specificity (0,2,0)
        # a.session-title                     = specificity (0,1,1)
        assert ".session-item.active .session-title{color:var(--accent-text);}" in STYLE_CSS

    def test_focus_indicator_uses_an_opaque_colour(self):
        """--focus-ring has alpha .35 and blends into the background: measured 1.15:1."""
        assert "a.session-title:focus-visible{outline:2px solid var(--accent);" in STYLE_CSS, (
            "The focus indicator must have contrast >=3:1 (WCAG 1.4.11). "
            "var(--focus-ring) is semi-transparent and measured 1.15:1; "
            "var(--accent) measured 11.12:1."
        )
        assert "a.session-title:focus-visible{outline:2px solid var(--focus-ring)" not in STYLE_CSS

    def test_other_sidebar_controls_also_show_focus(self):
        assert ".session-date-header:focus-visible" in STYLE_CSS
        assert ".session-select-toggle:focus-visible" in STYLE_CSS
        assert ".project-chip:focus-visible" in STYLE_CSS


class TestBatchSelectCheckboxNamesTheConversation:
    """Found only in state-coverage measurement, not at rest.

    Batch-selection mode is a separate panel state - at rest there are no
    checkboxes. Measured there: 4 controls without an accessible name (2 fields
    + 2 wrappers). The screen reader announced bare "checkbox, unlabeled", so
    with several rows there was no way to determine which conversation would be
    archived or deleted.
    """

    def test_checkbox_has_accessible_name_with_the_session_title(self):
        assert "cb.setAttribute('aria-label',cbNazwa+': '+(cleanTitle||'Untitled'));" in SESSIONS_JS, (
            "The checkbox must name the SPECIFIC conversation - the label "
            "'Select conversation' alone does not distinguish rows in a batch operation."
        )

    def test_checkbox_label_key_exists_in_the_base_locale(self):
        i18n = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
        assert "session_batch_select_one: 'Select conversation'," in i18n, (
            "t() returns the key itself when it is missing from the locale - without an 'en' entry "
            "the screen reader would read 'session_batch_select_one' to the user."
        )
