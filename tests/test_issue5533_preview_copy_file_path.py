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


def test_reset_text_preview_copy_state_hides_button_and_clears_cache():
    """Greptile auto-review (PR #6957): a failed md/code/csv load must not leave
    the copy-content button visible in a stale state carried over from the
    previously-previewed file. resetTextPreviewCopyState() must clear the raw
    content cache (and its path) and hide the button.
    """
    body = _function_body(WORKSPACE_JS, "resetTextPreviewCopyState")
    compact = _compact(body)

    assert "_previewRawContent=''" in compact
    assert "_previewRawContentPath=''" in compact
    assert "$('btnCopyPreviewContent')" in body
    assert "btn.style.display='none'" in compact


def test_reset_text_preview_copy_state_guards_against_stale_request_ownership():
    """Greptile P1 (PR #6957, r3768442266): if file A's request fails after file B
    has already become the current preview, A's stale catch must not clobber B's
    cached content or hide B's copy button. resetTextPreviewCopyState() takes the
    owning path and skips the reset when that path no longer matches the current
    preview.
    """
    body = _function_body(WORKSPACE_JS, "resetTextPreviewCopyState")
    compact = _compact(body)

    assert "resetTextPreviewCopyState(ownerPath,previewGen)" in compact
    guard = "if(ownerPath&&_previewCurrentPath!==ownerPath)return;"
    assert guard in compact
    # The guard must run before the cache/button are cleared.
    assert compact.index(guard) < compact.index("_previewRawContent=''")


def test_reset_text_preview_copy_state_guards_against_stale_generation():
    """Maintainer review (PR #6957 comment 5272907466): a path-equality guard alone
    cannot distinguish two overlapping openFile() calls for the SAME path — request
    A for notes.md, request B for notes.md, B succeeds, A then fails must not clear
    B's fresh cache/button. resetTextPreviewCopyState() must additionally reject a
    stale generation via previewGenerationIsStale(), and that check must run before
    the cache/button are cleared.
    """
    body = _function_body(WORKSPACE_JS, "resetTextPreviewCopyState")
    compact = _compact(body)

    guard = "if(previewGenerationIsStale(previewGen))return;"
    assert guard in compact
    assert compact.index(guard) < compact.index("_previewRawContent=''")


def test_preview_generation_counter_exists_and_openfile_captures_it():
    """The preview-open generation counter mirrors the existing workspace-tree
    generation pattern (_wsTreeGen / bumpWorkspaceTreeGen, used by loadDir()) so
    that overlapping openFile() calls can be told apart. openFile() must capture
    the generation immediately after the DOWNLOAD_EXTS early return, before any
    other state is touched.
    """
    assert "let _previewGen = 0;" in WORKSPACE_JS
    assert "function bumpPreviewGeneration(){" in WORKSPACE_JS
    assert "function previewGenerationIsStale(previewGen){" in WORKSPACE_JS

    compact = _compact(WORKSPACE_JS)
    assert "constpreviewGen=bumpPreviewGeneration();" in compact

    download_guard = "if(DOWNLOAD_EXTS.has(ext)){downloadFile(path);return;}"
    assert download_guard in compact
    capture = "constpreviewGen=bumpPreviewGeneration();"
    capture_idx = compact.index(capture)
    assert compact.index(download_guard) < capture_idx
    # Nothing else in openFile() writes preview state before the generation is captured.
    assert capture_idx < compact.index("_previewServerEditable=null;", capture_idx)


def test_markdown_open_file_failure_resets_copy_state_with_request_owner():
    """The markdown branch of openFile() must call resetTextPreviewCopyState(path,
    previewGen) on load failure, passing its own request's path and generation as
    the owner, so a stale failure can't clobber a newer file's copy-button state
    (Greptile P1 PR #6957 finding r3768442266; generation guard added per
    maintainer review comment 5272907466).
    """
    # openFile()'s default-parameter signature (`opts={}`) breaks the brace-matching
    # _function_body() helper (its own `{}` closes before the real body opens), so
    # this checks the whole-file compact source instead, anchored around the
    # markdown branch's render call and failure catch.
    compact = _compact(WORKSPACE_JS)

    catch_marker = (
        "}catch(e){"
        "if(previewGenerationIsStale(previewGen))return;"
        "resetTextPreviewCopyState(path,previewGen);setStatus(t('file_open_failed'));"
        "}"
    )
    assert catch_marker in compact
    assert "renderMarkdownPreviewContent(data);" in compact
    assert compact.index("renderMarkdownPreviewContent(data);") < compact.index(catch_marker)
    assert "MD_EXTS.has(ext)" in compact
    assert compact.index("MD_EXTS.has(ext)") < compact.index(catch_marker)


def test_csv_and_code_open_file_failures_also_pass_request_owner():
    """The CSV and plain-code/text branches of openFile() must likewise pass their
    own path and generation as the owner to resetTextPreviewCopyState(), so stale
    failures in those branches can't clobber a newer preview either (Greptile P1
    PR #6957 finding r3768442266; generation guard added per maintainer review
    comment 5272907466). All three text-preview failure branches (markdown, csv,
    plain code/text) call resetTextPreviewCopyState(path, previewGen), each guarded
    by an immediately-preceding stale-generation return.
    """
    compact = _compact(WORKSPACE_JS)
    assert compact.count("resetTextPreviewCopyState(path,previewGen);") == 3
    # No call site still uses the old ownerless or generation-less signatures.
    assert "resetTextPreviewCopyState();" not in compact
    assert "resetTextPreviewCopyState(path);" not in compact
    # Every catch block that resets copy state bails out first when stale.
    stale_return_before_reset = (
        "if(previewGenerationIsStale(previewGen))return;"
        "resetTextPreviewCopyState(path,previewGen);"
    )
    assert compact.count(stale_return_before_reset) == 3


def test_openfile_checks_staleness_immediately_after_each_awaited_read():
    """Each of the markdown, csv, and plain-code/text branches must reject a stale
    response immediately after its awaited /api read and before any cache/render
    write, mirroring loadDir()'s `if(...||treeGen!==_wsTreeGen)return;` pattern
    (maintainer review PR #6957 comment 5272907466). This closes the race where
    an old file-A success could render after the user navigated to file B, or a
    stale SUCCESS could overwrite a newer same-path response.
    """
    compact = _compact(WORKSPACE_JS)
    stale_check = "if(previewGenerationIsStale(previewGen))return;"
    # markdown, csv, and plain-code/text branches each have one post-await check
    # in the try body, plus one in the catch — six total, plus one more inside
    # resetTextPreviewCopyState() itself (defense in depth) — seven total.
    assert compact.count(stale_check) == 7

    read_call = "awaitapi(_workspaceRouteForPath(path,'read'));"
    idx = 0
    found = 0
    while True:
        idx = compact.find(read_call, idx)
        if idx == -1:
            break
        after = compact[idx + len(read_call): idx + len(read_call) + len(stale_check)]
        assert after == stale_check, f"expected staleness check immediately after read at {idx}"
        found += 1
        idx += len(read_call)
    assert found == 3


def test_bump_workspace_tree_gen_pattern_is_mirrored_by_preview_generation():
    """Sanity check that the preview generation guard follows the same shape as
    the pre-existing workspace-tree generation guard used by loadDir(), rather
    than diverging into a different mechanism.
    """
    ws_gen = _function_body(WORKSPACE_JS, "bumpWorkspaceTreeGen")
    preview_gen = _function_body(WORKSPACE_JS, "bumpPreviewGeneration")
    normalized_ws_gen = (
        _compact(ws_gen)
        .replace("bumpWorkspaceTreeGen", "bumpPreviewGeneration")
        .replace("_wsTreeGen", "_previewGen")
    )
    assert normalized_ws_gen == _compact(preview_gen)


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
