"""Regression tests for issue #1618 — YAML/tree code blocks preserve newlines."""

import json
import pathlib
import subprocess
import textwrap


REPO = pathlib.Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


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


def _render_md(markdown: str) -> dict:
    """Run the real static/ui.js renderMd() implementation in Node."""
    render_md = _extract_function(UI_JS, "renderMd")
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
    ) + "\n" + render_md + textwrap.dedent(
        r'''
        const out=renderMd(process.argv[1]);
        const codeMatch=out.match(/<code[^>]*>([\s\S]*?)<\/code>/);
        console.log(JSON.stringify({
          html: out,
          codeHtml: codeMatch ? codeMatch[1] : null,
          hasCodeNewlines: !!(codeMatch && codeMatch[1].includes('\n')),
          hasCodeBreaks: !!(codeMatch && codeMatch[1].includes('<br>')),
        }));
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


def test_yaml_tree_raw_view_preserves_newlines_inside_code_block():
    rendered = _render_md(
        """```yaml
foo:
  bar: 1
  baz:
    - 2
    - 3
```"""
    )

    assert 'class="language-yaml"' in rendered["html"]
    assert 'class="tree-raw-view"' in rendered["html"]
    assert rendered["hasCodeNewlines"], rendered["html"]
    assert not rendered["hasCodeBreaks"], rendered["html"]


def test_diff_pre_with_class_is_stashed_before_paragraph_wrapping():
    rendered = _render_md(
        """```diff
@@ heading
+ added
- removed
```"""
    )

    assert 'class="diff-block"' in rendered["html"]
    assert rendered["hasCodeNewlines"], rendered["html"]
    assert not rendered["hasCodeBreaks"], rendered["html"]
