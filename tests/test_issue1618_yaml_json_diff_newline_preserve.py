"""Tests for issue #1618 / #1463 — YAML/JSON code blocks render flattened.

Bug shape (live-verified in the browser May 04 2026):

    ```yaml
    foo:
      bar: 1
      baz:
    ```

renders as a single line `foo:  bar: 1  baz:` with no newlines, while:

    ```yml
    foo:
      bar: 1
      baz:
    ```

renders correctly multi-line. PR #1516 (v0.50.279) shipped a CSS-only fix
targeting Prism token white-space; the rule is in `style.css` and reaches
the browser, but the bug persists because the actual newline destruction
happens earlier in the pipeline, before Prism runs.

Root cause:
  - PR #484 (v0.50.237, JSON/YAML tree-viewer) routes those two languages
    through `<div class="code-tree-wrap">…<pre class="tree-raw-view">`
    instead of bare `<pre>`.
  - The `_pre_stash` regex at static/ui.js:1914 matched only literal `<pre>`
    with NO attributes (`<pre>[\\s\\S]*?<\\/pre>`).
  - `<pre class="tree-raw-view">` doesn't match → falls through to the
    paragraph wrap pass which replaces `\\n` with `<br>`.
  - By the time Prism runs and the CSS rule applies, the `\\n` characters
    that the rule was meant to preserve are already gone.

Same bug affects:
  - `lang === 'yaml'` (issue #1463 / #1618 — the canonical case)
  - `lang === 'json'` (same code path at static/ui.js:1621)
  - `lang === 'diff'` / `lang === 'patch'` (`<pre class="diff-block">`,
    same shape, same regex miss — emits at static/ui.js:1619)

Fix: relax the `_pre_stash` regex to accept any attribute on `<pre>`:
    `<pre>[\\s\\S]*?<\\/pre>`  →  `<pre[^>]*>[\\s\\S]*?<\\/pre>`

These tests pin both the source-level invariant (regex shape) and the
end-to-end behavior via a node-driver that exercises the actual
static/ui.js renderMd() function.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")


# ─────────────────────────────────────────────────────────────────────────
# § A — Source-string invariants (run without node, fast)
# ─────────────────────────────────────────────────────────────────────────


def test_pre_stash_regex_matches_pre_with_attributes():
    """static/ui.js _pre_stash regex must match <pre> with ANY attributes.

    The narrow shape `<pre>[\\s\\S]*?<\\/pre>` (literal <pre> with no
    attributes) misses every <pre class="..."> emitted by the JSON/YAML
    tree-viewer pass and the diff/patch coloring pass — those blocks fall
    through to paragraph wrap, which converts \\n to <br>.
    """
    src = UI_JS_PATH.read_text(encoding="utf-8")

    # The fix introduces `<pre[^>]*>` (any attributes) in the _pre_stash regex.
    # The exact regex line is documented in static/ui.js:1914.
    assert "<pre[^>]*>[\\s\\S]*?<\\/pre>" in src, (
        "_pre_stash regex must use <pre[^>]*> to match <pre> with any attributes "
        "(#1463/#1618). The narrow shape <pre>[\\s\\S]*?<\\/pre> misses every "
        "<pre class=\"tree-raw-view\"> from the JSON/YAML tree-viewer (PR #484) "
        "and <pre class=\"diff-block\"> from diff/patch — newlines inside those "
        "blocks fall through to paragraph wrap and become <br> tags."
    )

    # Defense against accidental regression: the literal-only shape must NOT
    # be present anywhere in the _pre_stash region of the file.
    pre_stash_idx = src.find("const _pre_stash=[]")
    assert pre_stash_idx > 0, "_pre_stash declaration not found"
    pre_stash_line = src[pre_stash_idx:pre_stash_idx + 1500]
    assert "<pre>[\\s\\S]*?<\\/pre>" not in pre_stash_line, (
        "_pre_stash regex must not contain the literal-<pre>-only shape — "
        "use <pre[^>]*> to match attributes."
    )


def test_pre_stash_still_captures_pre_header_and_optional_div():
    """The fix must keep the rest of the _pre_stash regex intact —
    specifically the optional <div class="pre-header"> prefix and the
    mermaid-block / katex-block alternation."""
    src = UI_JS_PATH.read_text(encoding="utf-8")

    pre_stash_idx = src.find("const _pre_stash=[]")
    pre_stash_block = src[pre_stash_idx:pre_stash_idx + 1500]

    assert '(<div class="pre-header">[\\s\\S]*?<\\/div>)?<pre[^>]*>' in pre_stash_block, (
        "Optional <div class=\"pre-header\"> prefix must still precede the "
        "<pre[^>]*> match"
    )
    assert '<div class="(mermaid-block|katex-block)"' in pre_stash_block, (
        "Mermaid/katex block alternation must remain in the regex"
    )


# ─────────────────────────────────────────────────────────────────────────
# § B — Behavioural tests via node-driver (skipped if node not on PATH)
# ─────────────────────────────────────────────────────────────────────────

pytestmark_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# Reuses the same driver shape as tests/test_renderer_js_behaviour.py.
_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }) };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
eval(extractFunc('_matchBacktickFenceLine'));
eval(extractFunc('_isBacktickFenceClose'));
eval(extractFunc('renderMd'));

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("issue1618_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _render(driver_path, markdown: str) -> str:
    """Run renderMd against the actual ui.js and return the rendered HTML."""
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return result.stdout


def _extract_pre_inner(html: str) -> str:
    """Extract the content of the first <pre ...>...</pre> block."""
    import re
    m = re.search(r"<pre[^>]*>([\s\S]*?)</pre>", html)
    if not m:
        return ""
    return m.group(1)


# ── The core regression: YAML newlines must survive ────────────────────


@pytestmark_node
def test_yaml_block_preserves_newlines(driver_path):
    """YAML code blocks must render multi-line, not flatten to a single line.

    This is the exact symptom Zixim reported on #1618: a YAML block renders
    with all newlines collapsed to spaces. The fix is the relaxed _pre_stash
    regex; without it, the block falls through to paragraph wrap and \\n
    becomes <br> inside <code>, which Prism then can't recover from.
    """
    md = "```yaml\nfoo:\n  bar: 1\n  baz:\n    - 2\n    - 3\n```"
    out = _render(driver_path, md)

    # The block must end up wrapped in code-tree-wrap (PR #484's shape)
    assert "code-tree-wrap" in out, (
        "YAML blocks should still route through the tree-viewer wrapper"
    )

    # Inner <pre>...</pre> must contain literal \n characters (preserved
    # newlines), NOT <br> tags.
    pre_inner = _extract_pre_inner(out)
    assert pre_inner, f"No <pre> block found in rendered output: {out!r}"
    assert "\n" in pre_inner, (
        f"YAML <pre> block lost its newlines (#1463/#1618).  "
        f"<pre> inner content: {pre_inner!r}.  "
        f"Likely cause: _pre_stash regex doesn't match <pre class=\"tree-raw-view\">, "
        f"so the block falls through to the paragraph wrap pass which converts \\n to <br>."
    )
    assert "<br>" not in pre_inner, (
        f"YAML <pre> block contains <br> tags — newlines were converted by paragraph "
        f"wrap.  This means the _pre_stash regex did not capture the block.  "
        f"<pre> inner content: {pre_inner!r}"
    )


@pytestmark_node
def test_json_block_preserves_newlines(driver_path):
    """JSON code blocks have the same shape as YAML (PR #484) and must also
    preserve newlines."""
    md = '```json\n{\n  "a": 1,\n  "b": [2, 3]\n}\n```'
    out = _render(driver_path, md)

    assert "code-tree-wrap" in out
    pre_inner = _extract_pre_inner(out)
    assert pre_inner
    assert "\n" in pre_inner, (
        f"JSON <pre> block lost newlines.  Inner: {pre_inner!r}"
    )
    assert "<br>" not in pre_inner


@pytestmark_node
def test_diff_block_preserves_newlines(driver_path):
    """Diff/patch blocks emit <pre class=\"diff-block\"> (static/ui.js:1619).
    Same regex-miss shape as YAML/JSON. Newlines must survive."""
    md = "```diff\n-removed line\n+added line\n unchanged\n```"
    out = _render(driver_path, md)

    assert "diff-block" in out
    pre_inner = _extract_pre_inner(out)
    assert pre_inner
    assert "\n" in pre_inner, (
        f"Diff <pre> block lost newlines.  Inner: {pre_inner!r}"
    )
    assert "<br>" not in pre_inner


@pytestmark_node
def test_yml_alias_already_worked_still_works(driver_path):
    """Sanity check: ` ```yml ` (the Prism alias) renders bare <pre> and
    was never affected by the bug. This must continue to work after the
    regex relaxation."""
    md = "```yml\nfoo:\n  bar: 1\n```"
    out = _render(driver_path, md)
    pre_inner = _extract_pre_inner(out)
    assert "\n" in pre_inner
    assert "<br>" not in pre_inner


@pytestmark_node
def test_bash_block_unaffected_baseline(driver_path):
    """Sanity: bash blocks emit bare <pre> and were never affected by the bug.
    They must continue to render correctly post-fix."""
    md = "```bash\necho one\necho two\n```"
    out = _render(driver_path, md)
    pre_inner = _extract_pre_inner(out)
    assert "\n" in pre_inner
    assert "<br>" not in pre_inner


# ── End-to-end Zixim-scenario reproducer ───────────────────────────────


@pytestmark_node
def test_yaml_block_renders_multiline_html_shape(driver_path):
    """The specific shape Zixim reported: 5-line YAML block must produce
    exactly 5 newline-separated logical lines in the <pre> inner content.

    Pre-fix this collapsed to a single space-joined string. Post-fix the
    line count should equal the original input line count.
    """
    md = "```yaml\nname: hermes\nport: 8787\nfeatures:\n  - chat\n  - tasks\n```"
    out = _render(driver_path, md)

    pre_inner = _extract_pre_inner(out)
    # Split on \n to count rendered lines. Empty trailing line tolerated.
    rendered_lines = [l for l in pre_inner.split("\n") if l.strip()]

    assert len(rendered_lines) == 5, (
        f"YAML block should preserve 5 lines, got {len(rendered_lines)}: {rendered_lines}.  "
        f"Full <pre> inner content: {pre_inner!r}"
    )


# ── Mermaid/katex blocks unaffected ────────────────────────────────────


@pytestmark_node
def test_mermaid_block_unaffected_by_regex_relaxation(driver_path):
    """Mermaid blocks come through a different alternation in the same regex
    (`<div class=\"(mermaid-block|katex-block)\"...`). Confirm they still get
    captured into _pre_stash and aren't paragraph-wrapped."""
    md = "```mermaid\ngraph TD\n  A --> B\n  B --> C\n```"
    out = _render(driver_path, md)

    # Mermaid block emits <div class="mermaid-block"> (no <pre>).
    assert "mermaid-block" in out
    # The mermaid div should not be wrapped in <p>...</p>.
    assert "<p><div class=\"mermaid-block\"" not in out
    # Internal newlines inside data-mermaid-id should not be relevant —
    # mermaid content is in the data-attr / esc()'d innerText. But the
    # surrounding paragraph-wrap-bypass MUST still work.
    assert "<p>" not in out or out.find("<p>") > out.find("mermaid-block"), (
        "Mermaid block should bypass paragraph wrap"
    )
