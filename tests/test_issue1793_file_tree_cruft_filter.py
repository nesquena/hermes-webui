"""Regression coverage for #1793 — workspace file-tree cruft filter.

Original v0.51.21 work added an inline "Show hidden files" toggle that sat
permanently between the breadcrumb and the file tree, eating ~32px of
vertical space on every panel view (root, subdir, file preview).

Follow-up UX refinement (this commit) moves the toggle behind a kebab
dropdown in the panel-actions row and surfaces the non-default
"hidden-files-visible" state via a small indicator next to the panel
heading. The original filtering behavior is unchanged; only the affordance
shape moved.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


# ── Original filtering behavior (must stay green) ────────────────────────


def test_workspace_panel_has_show_hidden_files_toggle():
    """File-tree cruft must be recoverable via an explicit user toggle.

    The toggle now lives behind the kebab; the checkbox itself is built by
    `_buildWorkspacePrefsMenu` in ui.js (so it's literally referenced there
    by id), but the existing call site in i18n still resolves the localized
    label.
    """
    assert "toggleWorkspaceHiddenFiles" in UI_JS
    assert 'id="workspaceShowHiddenFiles"' in UI_JS  # built dynamically; id preserved
    assert "workspace_show_hidden_files" in I18N_JS


def test_file_tree_filters_common_cruft_by_default():
    """macOS/Windows/VCS/cache noise should not render by default."""
    assert "WORKSPACE_HIDDEN_FILE_NAMES" in UI_JS
    for name in [
        ".DS_Store",
        "Thumbs.db",
        "Desktop.ini",
        ".git",
        "__pycache__",
        "node_modules",
    ]:
        assert name in UI_JS
    assert "_visibleWorkspaceEntries" in UI_JS
    assert "S.showHiddenWorkspaceFiles" in UI_JS
    assert "_workspaceShouldHideEntry" in UI_JS


def test_hidden_file_toggle_invalidates_tree_render_without_refetch():
    """The toggle should re-render cached entries instead of changing workspace state."""
    assert "function toggleWorkspaceHiddenFiles" in UI_JS
    body_start = UI_JS.index("function toggleWorkspaceHiddenFiles")
    body_end = UI_JS.index("\n}", body_start)
    body = UI_JS[body_start:body_end]
    assert "renderFileTree()" in body
    assert "localStorage.setItem('hermes-workspace-show-hidden-files'" in body


# ── Kebab-affordance UX refinement ───────────────────────────────────────


def test_no_inline_workspace_hidden_toggle_row():
    """The always-on inline `<label class="workspace-hidden-toggle">` row
    must be gone — it ate vertical space below the breadcrumb on every
    panel view. Toggle now lives behind the kebab.
    """
    assert "workspace-hidden-toggle" not in INDEX_HTML, (
        "inline hidden-files row should have been removed in favor of the "
        "kebab menu (#1793 follow-up)"
    )
    # CSS for the inline row should also be gone — leaving stale rules
    # invites future drift where someone re-adds the row and it picks up
    # accidental styling.
    assert ".workspace-hidden-toggle" not in STYLE_CSS


def test_panel_actions_row_has_workspace_prefs_kebab():
    """A kebab button (`btnWorkspacePrefs`) must exist in the workspace
    panel actions row to expose the menu.
    """
    assert 'id="btnWorkspacePrefs"' in INDEX_HTML
    assert 'onclick="toggleWorkspacePrefsMenu(event)"' in INDEX_HTML
    # Tooltip is i18n-aware
    assert 'data-i18n-title="workspace_options"' in INDEX_HTML
    # Kebab carries an accent dot for non-default state
    assert 'id="workspacePrefsDot"' in INDEX_HTML


def test_panel_heading_has_hidden_files_indicator():
    """The non-default "hidden files visible" state must surface as a small
    indicator next to the WORKSPACE heading so users don't forget they
    flipped the pref. Hidden by default via the `hidden` attribute.
    """
    assert 'id="workspaceHiddenIndicator"' in INDEX_HTML
    # The indicator opens the same menu when clicked (no separate code path)
    block = INDEX_HTML[INDEX_HTML.index('id="workspaceHiddenIndicator"') :]
    block = block[: block.index("</span>") + 7]
    assert "toggleWorkspacePrefsMenu" in block
    # Default-hidden so the chip doesn't clutter normal state
    assert " hidden " in block or block.rstrip().endswith("hidden")


def test_kebab_menu_javascript_exists():
    """The dropdown must be self-contained: open/close/position handlers
    follow the canonical floating-menu pattern from
    `_openSessionActionMenu`.
    """
    assert "function toggleWorkspacePrefsMenu" in UI_JS
    assert "function _buildWorkspacePrefsMenu" in UI_JS
    assert "function _closeWorkspacePrefsMenu" in UI_JS
    assert "function _positionWorkspacePrefsMenu" in UI_JS
    # Built menu still contains the canonical input id so existing call
    # sites and the toggle test above keep working.
    build_start = UI_JS.index("function _buildWorkspacePrefsMenu")
    build_end = UI_JS.index("\n}", build_start)
    build_body = UI_JS[build_start:build_end]
    assert 'id="workspaceShowHiddenFiles"' in build_body


def test_kebab_menu_closes_on_escape_and_outside_click():
    """Standard keyboard / click-out close behavior."""
    # Escape closes
    assert "Escape" in UI_JS and "_closeWorkspacePrefsMenu" in UI_JS
    # Outside-click close listener
    assert "_workspacePrefsMenu" in UI_JS
    assert "if(_workspacePrefsMenu.contains(e.target)) return" in UI_JS


def test_indicator_reflects_localStorage_state_on_load():
    """`_syncWorkspaceHiddenToggle` must drive both the dropdown checkbox
    AND the indicator/dot so a page reload with the pref ON shows the
    "hidden visible" indicator without the user having to open the menu.
    """
    sync_start = UI_JS.index("function _syncWorkspaceHiddenToggle")
    sync_end = UI_JS.index("\n}", sync_start)
    body = UI_JS[sync_start:sync_end]
    assert "workspaceHiddenIndicator" in body
    assert "workspacePrefsDot" in body
    # Drives the existing checkbox if it's mounted
    assert "workspaceShowHiddenFiles" in body


def test_kebab_menu_styles_replace_inline_row():
    """CSS must define the kebab dot, indicator, and floating menu — but
    not the legacy inline-row styling (the test above pins removal).
    """
    assert ".workspace-prefs-menu{" in STYLE_CSS
    assert ".workspace-prefs-item{" in STYLE_CSS
    assert ".workspace-hidden-indicator{" in STYLE_CSS
    assert "#btnWorkspacePrefs" in STYLE_CSS


def test_new_i18n_keys_present_in_all_locales():
    """The new copy must exist in every locale block so the kebab menu
    description and indicator chip don't render `undefined` in non-en
    sessions.
    """
    # Total locale blocks today: 9 (en, ja, ru, es, de, zh, zh-Hant, pt, ko)
    n_locales = I18N_JS.count("workspace_show_hidden_files:")
    assert n_locales >= 8, f"unexpected locale count: {n_locales}"
    for key in (
        "workspace_show_hidden_files_desc:",
        "workspace_hidden_files_visible:",
        "workspace_hidden_files_visible_title:",
        "workspace_options:",
    ):
        assert I18N_JS.count(key) == n_locales, (
            f"key {key!r} missing in some locales (expected {n_locales}, "
            f"got {I18N_JS.count(key)})"
        )


# ── #1841 regression: exact non-English translations must be present ─────


def test_workspace_show_hidden_files_translations_are_not_english_fallback():
    """Each non-English locale must carry its own translated string for
    workspace_show_hidden_files — not silently fall back to the English
    "Show hidden files".  Pin the exact expected translations so a
    regression that replaces any of them with the English fallback is
    caught immediately.
    """
    expected = {
        "es": "Mostrar archivos ocultos",
        "ru": "Показывать скрытые файлы",
        "zh": "显示隐藏文件",
        "zh-Hant": "顯示隱藏檔案",
        "pt": "Mostrar arquivos ocultos",
        "ja": "隠しファイルを表示",
        "ko": "숨김 파일 표시",
    }
    for locale, translation in expected.items():
        # Build a source-level needle: the locale block assigns the
        # translated value on a line like
        #   workspace_show_hidden_files: 'Mostrar archivos ocultos',
        # Matching the full assignment avoids false positives from
        # unrelated strings that happen to contain the same words.
        needle = f"workspace_show_hidden_files: '{translation}'"
        assert needle in I18N_JS, (
            f"locale {locale!r}: expected translation needle {needle!r} "
            f"not found in i18n.js — likely fell back to English"
        )
