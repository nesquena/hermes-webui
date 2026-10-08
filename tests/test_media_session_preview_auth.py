"""Regression coverage for session-scoped MEDIA artifact URLs (#7294)."""
from pathlib import Path


UI_JS = Path("static/ui.js").read_text(encoding="utf-8")


def _function_body(name, *, until):
    start = UI_JS.index(f"function {name}")
    end = UI_JS.index(until, start)
    return UI_JS[start:end]


def test_shared_media_url_helper_keeps_session_snap_and_actions_together():
    helper = _function_body("_mediaPreviewUrl", until="\n\nfunction buildCsvTablePreview")
    assert "api/media?path=" in helper
    assert "_mediaSessionQuery()" in helper
    assert "opts.snap" in helper
    assert "opts.inline" in helper
    assert "opts.download" in helper


def test_diff_and_excalidraw_fetches_use_session_scoped_media_url():
    diff = _function_body("loadDiffInline", until="\n\nconst CSV_MAX_SIZE")
    assert "fetch(_mediaPreviewUrl(path,{snap:snap||undefined}))" in diff
    assert "fetch('api/media?path='" not in diff

    excalidraw = _function_body("loadExcalidrawInline", until="\n\nlet _excalidrawScriptLoaded")
    assert "fetch(_mediaPreviewUrl(path,{snap:snap||undefined}))" in excalidraw
    assert "_mediaPreviewUrl(path,{download:true,snap:snap||undefined})" in excalidraw
    assert "fetch('api/media?path='" not in excalidraw


def test_pdf_and_html_action_links_keep_session_id():
    pdf = _function_body("loadPdfInline", until="\n\n// ── HTML inline preview")
    assert "publicMediaUrl" not in pdf
    assert "const mediaUrl=_mediaPreviewUrl(path,{snap:snap||undefined})" in pdf
    assert "_mediaPreviewUrl(path,{download:true,snap:snap||undefined})" in pdf

    html = _function_body("loadHtmlInline", until="\n\nfunction renderMermaidBlocks")
    assert "publicMediaUrl" not in html
    assert "const mediaUrl=_mediaPreviewUrl(path,{snap:snap||undefined})" in html
    assert "_mediaPreviewUrl(path,{inline:true,snap:snap||undefined})" in html
    assert "_mediaPreviewUrl(path,{download:true,snap:snap||undefined})" in html
