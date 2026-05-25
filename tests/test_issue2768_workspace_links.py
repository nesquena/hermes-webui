import json
import pathlib
import re
import subprocess
import textwrap


REPO = pathlib.Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 1
    pos = brace + 1
    while depth and pos < len(src):
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        pos += 1
    assert depth == 0, f"could not extract {name}()"
    return src[start:pos]


def _render(markdown: str) -> str:
    js = textwrap.dedent(
        r'''
        const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
        const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
        const _PDF_EXTS=/\.pdf$/i;
        const _SVG_EXTS=/\.svg$/i;
        const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm|oga)$/i;
        const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
        function t(k){ return k; }
        function _mediaPlayerHtml(){ return ''; }
        global.document={baseURI:'http://example.test/'};
        '''
    )
    js += "\n" + _extract_function(UI_JS, "_matchBacktickFenceLine")
    js += "\n" + _extract_function(UI_JS, "_isBacktickFenceClose")
    js += "\n" + _extract_function(UI_JS, "renderMd")
    js += textwrap.dedent(
        r'''
        const input=process.argv[1];
        process.stdout.write(JSON.stringify(renderMd(input)));
        '''
    )
    proc = subprocess.run(
        ["node", "-e", js, markdown],
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return json.loads(proc.stdout)


def test_workspace_markdown_renders_mailto_and_tel_links():
    html = _render("[email](mailto:foo@example.test) and [phone](tel:+15551212)")

    assert '<a href="mailto:foo@example.test" target="_blank" rel="noopener">email</a>' in html
    assert '<a href="tel:+15551212" target="_blank" rel="noopener">phone</a>' in html


def test_workspace_html_iframe_allows_links_to_escape_sandbox():
    iframe = re.search(r'<iframe[^>]+id="previewHtmlIframe"[^>]*>', INDEX_HTML)

    assert iframe, "previewHtmlIframe iframe not found"
    sandbox = re.search(r'sandbox="([^"]+)"', iframe.group(0))
    assert sandbox, "previewHtmlIframe must keep an explicit sandbox"
    assert "allow-scripts" in sandbox.group(1)
    assert "allow-popups" in sandbox.group(1)
    assert "allow-popups-to-escape-sandbox" in sandbox.group(1)


def test_file_raw_inline_html_preview_injects_base_target_blank():
    raw_handler = ROUTES_PY[ROUTES_PY.index("def _handle_file_raw") :]

    assert '<base target="_blank">' in ROUTES_PY
    assert "_serve_inline_html_preview" in raw_handler
    assert "html_inline_ok" in raw_handler
