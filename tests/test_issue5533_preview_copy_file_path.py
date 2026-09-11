from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    brace = src.index("{", start)
    depth = 0
    in_string = ""
    escape = False
    for idx in range(brace, len(src)):
        ch = src[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_string:
                in_string = ""
            continue
        if ch in "'\"`":
            in_string = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name} body did not close")


def _compact(src: str) -> str:
    return "".join(src.split())


def test_preview_toolbar_has_copy_relative_path_button():
    assert 'id="btnCopyPreviewRelPath"' in INDEX
    assert 'onclick="copyPreviewRelativePath()"' in INDEX
    assert 'data-i18n="copy_relative_path"' in INDEX
    assert "Copy relative path" in INDEX


def test_preview_copy_relative_path_uses_current_preview_path():
    body = _function_body(WORKSPACE_JS, "copyPreviewRelativePath")
    compact = _compact(body)

    assert "_previewCurrentPath" in body
    assert "_normalizeWorkspaceRelPath(_previewCurrentPath)" in body
    assert "api('/api/file/path'" not in body
    assert "constrel=_normalizeWorkspaceRelPath(_previewCurrentPath)||_previewCurrentPath" in compact


def test_preview_copy_relative_path_disables_button_while_request_is_in_flight():
    body = _function_body(WORKSPACE_JS, "copyPreviewRelativePath")
    compact = _compact(body)

    guard = "if(btn&&btn.disabled)return;"
    disable = "if(btn)btn.disabled=true;"
    enable = "finally{if(btn)btn.disabled=false;}"
    assert "$('btnCopyPreviewRelPath')" in body
    assert guard in compact
    assert disable in compact
    assert enable in compact
    assert compact.index(guard) < compact.index(disable)
    assert compact.index(disable) < compact.index("_normalizeWorkspaceRelPath")


def test_preview_copy_relative_path_reuses_clipboard_fallback_and_toasts():
    body = _function_body(WORKSPACE_JS, "copyPreviewRelativePath")
    assert "typeof _copyTextWithFallback==='function'" in body
    assert "_copyTextWithFallback(rel,t('path_copied'),t('path_copy_failed'))" in body
    assert "navigator.clipboard.writeText(rel)" in body
    assert "document.execCommand('copy')" in body
    assert "t('path_copied')" in body
    assert "t('path_copy_failed')" in body


def test_tree_context_menu_keeps_absolute_copy_and_adds_relative_copy():
    assert "copyPathItem.textContent=t('copy_file_path')" in UI_JS
    assert "copyRelPathItem.textContent=t('copy_relative_path')" in UI_JS
    assert "const rel=_normalizeWorkspaceRelPath(item.path)||item.path" in UI_JS
    assert "_copyTextWithFallback(rel,t('path_copied'),t('path_copy_failed'))" in UI_JS


def test_preview_toolbar_keeps_copy_button_from_shrinking_path_layout():
    assert ".preview-path #btnCopyPreviewRelPath" in STYLE
    selector_start = STYLE.index(".preview-path #btnCopyPreviewRelPath")
    selector_block = STYLE[selector_start : STYLE.index("}", selector_start) + 1]
    assert "flex-shrink:0" in selector_block
    assert "white-space:nowrap" in selector_block


def test_preview_copy_button_is_accessible_and_icon_only_on_narrow_pane():
    """The preview-header copy button must stay accessible when its text label is
    hidden on a narrow pane (#5548 icon-only fold-in): it carries an aria-label,
    its label span is class-tagged, and a narrow-width media query hides that label.
    """
    import re
    # The button carries an explicit aria-label (screen-reader name survives label-hide).
    assert 'id="btnCopyPreviewRelPath"' in INDEX
    btn = INDEX[INDEX.index('id="btnCopyPreviewRelPath"'):]
    btn = btn[: btn.index("</button>")]
    assert 'aria-label="Copy relative path"' in btn
    assert 'class="preview-btn-label"' in btn
    # Localized tooltip + accessible name (WCAG 2.5.3): the icon-only state must not
    # leave a Russian/German user with an English tooltip/screen-reader name.
    assert 'data-i18n-title="copy_relative_path"' in btn
    assert 'data-i18n-aria-label="copy_relative_path"' in btn
    # A narrow-PANE container query (right panel, not viewport) hides the label
    # (icon-only), keeping the glyph — so it fires on pane resize even on desktop.
    assert re.search(
        r"@container\s+rightpanel[^{]*max-width:\s*520px[^{]*\{[^}]*"
        r"\.preview-path\s+#btnCopyPreviewRelPath\s+\.preview-btn-label\s*\{\s*display:\s*none",
        STYLE,
    ), "expected a @container rightpanel query hiding the copy-button label on a narrow pane"


def test_preview_toolbar_has_copy_content_button():
    assert 'id="btnCopyPreviewContent"' in INDEX
    assert 'onclick="copyPreviewContent()"' in INDEX
    assert 'data-i18n="copy_file_contents"' in INDEX
    assert "Copy file contents" in INDEX


def test_preview_copy_content_uses_current_preview_raw_content():
    body = _function_body(WORKSPACE_JS, "copyPreviewContent")
    compact = _compact(body)

    assert "_previewCurrentPath" in body
    assert "_previewRawContent" in body
    assert "typeof_previewRawContent!=='string'" in compact
    # The staleness guard must compare the cached content's path against the
    # currently-previewed path so opening file B after file A cannot copy A's
    # stale text (finding #1). Both operands must appear in the same guard.
    assert "_previewRawContentPath!==_previewCurrentPath" in compact
    assert "constcontent=_previewRawContent;" in compact


def test_preview_copy_content_fails_when_content_not_available():
    body = _function_body(WORKSPACE_JS, "copyPreviewContent")
    compact = _compact(body)

    guard = "if(typeof_previewRawContent!=='string'||_previewRawContentPath!==_previewCurrentPath){"
    fallback_toast = "showToast(t('content_not_available'));"
    assert guard in compact
    assert fallback_toast in compact
    guard_idx = compact.index(guard)
    assert compact.index(fallback_toast, guard_idx) == guard_idx + len(guard)
    assert compact.index(fallback_toast) < compact.index("constcontent=_previewRawContent;")
    assert "return;" in compact[compact.index(fallback_toast):compact.index(fallback_toast) + len(fallback_toast) + 10]


def test_preview_copy_content_disables_button_while_request_is_in_flight():
    body = _function_body(WORKSPACE_JS, "copyPreviewContent")
    compact = _compact(body)

    guard = "if(btn&&btn.disabled)return;"
    disable = "if(btn)btn.disabled=true;"
    enable = "finally{if(btn)btn.disabled=false;}"
    assert "$('btnCopyPreviewContent')" in body
    assert guard in compact
    assert disable in compact
    assert enable in compact
    assert compact.index(guard) < compact.index(disable)
    assert compact.index(disable) < compact.index("_previewRawContent")


def test_preview_copy_content_reuses_clipboard_fallback_and_toasts():
    body = _function_body(WORKSPACE_JS, "copyPreviewContent")
    # copyPreviewContent must delegate to the shared clipboard helper and NOT
    # carry its own duplicated inline navigator.clipboard/execCommand fallback
    # (finding #4 — the helper always exists, so the inline block was dead code).
    assert "_copyTextWithFallback(content,t('content_copied'),t('content_copy_failed'))" in body
    assert "navigator.clipboard.writeText(content)" not in body
    assert "document.execCommand('copy')" not in body
    assert "t('content_copied')" in body
    assert "t('content_copy_failed')" in body

    # renderCodePreviewContent must cache the raw text + its path so the button
    # copies the currently-previewed code file rather than stale md/csv text
    # (finding #1).
    render = _function_body(WORKSPACE_JS, "renderCodePreviewContent")
    render_compact = _compact(render)
    assert "_previewRawContent=content;" in render_compact
    assert "_previewRawContentPath=path;" in render_compact


def test_preview_toolbar_keeps_copy_content_button_from_shrinking_path_layout():
    assert ".preview-path #btnCopyPreviewContent" in STYLE
    selector_start = STYLE.index(".preview-path #btnCopyPreviewContent")
    selector_block = STYLE[selector_start : STYLE.index("}", selector_start) + 1]
    assert "flex-shrink:0" in selector_block
    assert "white-space:nowrap" in selector_block


def test_preview_copy_content_button_is_accessible_and_icon_only_on_narrow_pane():
    """The preview-header copy-content button must stay accessible when its text
    label is hidden on a narrow pane (#5548 icon-only fold-in): it carries an
    aria-label, its label span is class-tagged, and a narrow-width media query
    hides that label.
    """
    import re
    # The button carries an explicit aria-label (screen-reader name survives label-hide).
    assert 'id="btnCopyPreviewContent"' in INDEX
    btn = INDEX[INDEX.index('id="btnCopyPreviewContent"'):]
    btn = btn[: btn.index("</button>")]
    assert 'aria-label="Copy file contents"' in btn
    assert 'class="preview-btn-label"' in btn
    # Localized tooltip + accessible name (WCAG 2.5.3): the icon-only state must not
    # leave a Russian/German user with an English tooltip/screen-reader name.
    assert 'data-i18n-title="copy_file_contents"' in btn
    assert 'data-i18n-aria-label="copy_file_contents"' in btn
    # A narrow-PANE container query (right panel, not viewport) hides the label
    # (icon-only), keeping the glyph — so it fires on pane resize even on desktop.
    assert re.search(
        r"@container\s+rightpanel[^{]*max-width:\s*520px[^{]*\{[\s\S]*?"
        r"\.preview-path\s+#btnCopyPreviewContent\s+\.preview-btn-label\s*\{\s*display:\s*none",
        STYLE,
    ), "expected a @container rightpanel query hiding the copy-content-button label on a narrow pane"
