"""Behavioural tests for renderMarkdownPreviewContent preprocessing (#5933).

These tests drive the ACTUAL renderMd() from static/ui.js together with the
image-path preprocessing extracted from static/workspace.js via node, so the
full pipeline (preprocessing → renderMd) is exercised end-to-end.

They close the gap noted in R1 finding F5: the existing
TestMarkdownImageSchemeCoverage only feeds pre-resolved /api/file/raw URLs
straight into renderMd, so it never tests the preprocessing step that rewrites
relative ``![alt](./img.png)`` paths.  They also guard F1: image syntax inside
inline-code spans must NOT be rewritten by the preprocessing pass (otherwise
renderMd renders an <img> inside <code>).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
WORKSPACE_JS_PATH = REPO_ROOT / "static" / "workspace.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');

// ── Load renderMd and helpers from ui.js ──────────────────────────────────
const uiSrc = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }), baseURI: 'http://localhost/app/' };
function _sessionUrlForSid(sid) { return '/app/session/' + encodeURIComponent(String(sid || '')); }
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;

function extractFunc(src, name) {
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
eval(extractFunc(uiSrc, '_matchBacktickFenceLine'));
eval(extractFunc(uiSrc, '_isBacktickFenceClose'));
eval(extractFunc(uiSrc, 'renderMd'));

// ── Load renderMarkdownPreviewContent from workspace.js ──────────────────
// It references globals (_previewCurrentPath, S, _normalizeWorkspaceRelPath,
// showPreview, $) which we stub here. renderMd is already defined above.
const wsSrc = fs.readFileSync(process.argv[3], 'utf8');
global._normalizeWorkspaceRelPath = function(p){
  return String(p||'').replace(/\/+/g,'/').replace(/^\.\//,'');
};
global.showPreview = function(){};
global.$ = function(id){ return { innerHTML: '' }; };
global.requestAnimationFrame = function(fn){ if(typeof fn==='function') fn(); };
// _previewCurrentPath and S are set per-test via stdin preamble.
eval(extractFunc(wsSrc, 'renderMarkdownPreviewContent'));

// Read test payload from stdin: lines of "KEY=VALUE" preamble, then "---",
// then the markdown content body.
let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => {
  const sep = buf.indexOf('\n---\n');
  let preamble, body;
  if (sep >= 0) {
    preamble = buf.slice(0, sep);
    body = buf.slice(sep + 5);
  } else {
    preamble = '';
    body = buf;
  }
  // Apply preamble
  for (const line of preamble.split('\n')) {
    const m = line.match(/^(\w+)=(.*)$/);
    if (!m) continue;
    if (m[1] === 'previewCurrentPath') global._previewCurrentPath = m[2];
    else if (m[1] === 'sessionId') global.S = { session: { session_id: m[2] } };
  }
  // renderMarkdownPreviewContent renders into target.innerHTML.
  // We capture the result by giving it a fake element.
  const captureEl = { innerHTML: '' };
  // Parse extra data.* fields from preamble (baseDir=…).
  const data = { content: body, el: captureEl };
  for (const line of preamble.split('\n')) {
    const m = line.match(/^data\.(\w+)=(.*)$/);
    if (m) data[m[1]] = m[2];
  }
  renderMarkdownPreviewContent(data);
  process.stdout.write(captureEl.innerHTML);
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("ws_md_preview_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _render_preview(driver_path, markdown, *, preview_current_path="notes/note.md",
                    session_id="test-sid", base_dir=None):
    """Run renderMarkdownPreviewContent (preprocessing + renderMd) and return HTML."""
    lines = [f"previewCurrentPath={preview_current_path}", f"sessionId={session_id}"]
    if base_dir is not None:
        lines.append(f"data.baseDir={base_dir}")
    payload = "\n".join(lines) + "\n---\n" + markdown
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH), str(WORKSPACE_JS_PATH)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return result.stdout


# ─────────────────────────────────────────────────────────────────────────────
# F1: image syntax inside inline-code spans must NOT be rewritten by the
# preprocessing pass.  Before the fix the regex matched ![alt](./img.png)
# inside backticks, rewrote it to /api/file/raw?…, and renderMd's inlineMd
# image pass then rendered <img> inside <code>.
# ─────────────────────────────────────────────────────────────────────────────

class TestPreprocessingSkipsInlineCode:
    """Preprocessing must not rewrite images inside backtick code spans."""

    def test_inline_code_image_not_rewritten(self, driver_path):
        """`![alt](./img.png)` inside backticks must stay literal text in <code>."""
        out = _render_preview(driver_path, "Run `![alt](./img.png)` now")
        assert "<img" not in out, (
            f"Image syntax inside inline code must NOT be rendered as <img>: {out!r}"
        )
        assert "<code>" in out

    def test_inline_code_image_preserves_relative_path(self, driver_path):
        """The relative path inside inline code must be preserved verbatim."""
        out = _render_preview(driver_path, "See `![alt](./img.png)` here")
        assert "./img.png" in out, (
            f"Relative path inside inline code must be preserved: {out!r}"
        )
        assert "/api/file/raw" not in out or "api/file/raw" not in out.split("<code>")[1].split("</code>")[0], (
            f"Path inside inline code must not be rewritten to /api/file/raw: {out!r}"
        )

    def test_real_image_outside_code_still_rewritten(self, driver_path):
        """A real image outside backticks must still be rewritten and rendered."""
        out = _render_preview(driver_path, "![real](./real.png)")
        assert "<img" in out
        assert "/api/file/raw" in out
        assert "msg-media-img" in out

    def test_mixed_inline_code_and_real_image(self, driver_path):
        """Code image stays literal while a sibling real image renders."""
        md = "Code: `![alt](./img.png)` and real ![real](./real.png)."
        out = _render_preview(driver_path, md)
        # Real image rendered
        assert "<img" in out
        assert "/api/file/raw" in out
        # But the code span must not contain an <img>
        assert "<code><img" not in out, (
            f"<img> must not appear inside <code>: {out!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Maintainer review feedback (nesquena-hermes, PR #5935):
# "There is no test for the triple-backtick fenced block case, even though
# that's the path with its own dedicated regex (the more failure-prone of the
# two). Fenced blocks are also the common way docs show a markdown image
# example."
#
# These tests lock down fenced-block image syntax against the preprocessing
# rewrite, paralleling the inline-code tests above.
# ─────────────────────────────────────────────────────────────────────────────

class TestPreprocessingSkipsFencedCode:
    """Preprocessing must not rewrite images inside triple-backtick fenced blocks."""

    def test_fenced_block_image_not_rewritten(self, driver_path):
        """![alt](./img.png) inside a ```md fence must stay literal."""
        md = "```md\n![alt](./img.png)\n```"
        out = _render_preview(driver_path, md)
        assert "/api/file/raw" not in out, (
            f"Fenced image path must stay literal, not rewritten to /api/file/raw: {out!r}"
        )
        assert "./img.png" in out, (
            f"Relative path inside fenced block must be preserved verbatim: {out!r}"
        )

    def test_fenced_block_image_no_img_tag(self, driver_path):
        """An image inside a fenced block must NOT produce an <img> tag."""
        md = "```md\n![alt](./img.png)\n```"
        out = _render_preview(driver_path, md)
        assert "<img" not in out, (
            f"Image inside fenced block must NOT be rendered as <img>: {out!r}"
        )

    def test_fenced_block_preserves_multiple_images(self, driver_path):
        """Multiple image refs inside a fenced block must all stay literal."""
        md = "```md\n![first](./a.png)\n![second](./b.png)\n```"
        out = _render_preview(driver_path, md)
        assert "/api/file/raw" not in out
        assert "./a.png" in out
        assert "./b.png" in out

    def test_mixed_fenced_block_and_real_image(self, driver_path):
        """Fenced example stays literal while a real sibling image renders.

        This is the mixed case the maintainer requested: proves the stash
        restores correctly and the real image outside the fence still renders.
        """
        md = (
            "```md\n"
            "![example](./img.png)\n"
            "```\n"
            "\n"
            "![real](./real.png)"
        )
        out = _render_preview(driver_path, md)
        # Real image outside fence must render
        assert "<img" in out
        assert "/api/file/raw" in out
        assert "msg-media-img" in out
        # The fenced example path must stay literal (not rewritten)
        assert "path=img.png" not in out.replace("path=real.png", ""), (
            f"Fenced image must not be rewritten, but found /api/file/raw for it: {out!r}"
        )
        assert "./img.png" in out, (
            f"Fenced example path must be preserved verbatim: {out!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# F5: exercise the preprocessing step itself (relative path resolution).
# ─────────────────────────────────────────────────────────────────────────────

class TestPreprocessingResolvesRelativePaths:
    """The preprocessing resolves relative image paths against the preview dir."""

    def test_relative_image_in_subdirectory(self, driver_path):
        """![alt](./img.png) with preview path notes/note.md → notes/img.png."""
        out = _render_preview(driver_path, "![pic](./pic.png)",
                              preview_current_path="notes/note.md")
        assert "<img" in out
        assert "path=notes%2Fpic.png" in out or "path=notes%2F.%2Fpic.png" in out

    def test_relative_image_in_root(self, driver_path):
        """![alt](img.png) with preview path note.md → img.png (no dir prefix)."""
        out = _render_preview(driver_path, "![pic](pic.png)",
                              preview_current_path="note.md")
        assert "<img" in out
        assert "path=pic.png" in out

    def test_https_image_not_rewritten(self, driver_path):
        """https:// images must pass through unchanged."""
        out = _render_preview(driver_path, "![x](https://example.com/x.png)")
        assert "<img" in out
        assert 'src="https://example.com/x.png"' in out
        assert "/api/file/raw" not in out

    def test_already_api_url_not_double_rewritten(self, driver_path):
        """/api/file/raw URLs must not be rewritten again."""
        url = "/api/file/raw?session_id=abc&path=x.png&inline=1"
        out = _render_preview(driver_path, f"![x]({url})")
        assert "<img" in out
        # Should not be double-rewritten
        assert out.count("/api/file/raw") == 1


# ─────────────────────────────────────────────────────────────────────────────
# F2: wiki browser must pass baseDir via data, not rely on stale
# _previewCurrentPath.  renderMarkdownPreviewContent must honor an explicit
# data.baseDir for path resolution, even when _previewCurrentPath is unset or
# points to an unrelated file.
# ─────────────────────────────────────────────────────────────────────────────

PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"


class TestPreviewBaseDirOverride:
    """renderMarkdownPreviewContent must honor data.baseDir for path resolution."""

    def test_basedir_resolves_when_preview_path_unset(self, driver_path):
        """When _previewCurrentPath is empty, data.baseDir must still resolve images.

        This is the wiki browser case: _previewCurrentPath may be empty or stale
        because the user navigated the wiki, not the workspace file tree.
        """
        out = _render_preview(
            driver_path,
            "![pic](./pic.png)",
            preview_current_path="",
            base_dir="wiki/page.md",
        )
        assert "<img" in out, (
            f"data.baseDir must resolve images even when _previewCurrentPath is empty: {out!r}"
        )
        assert "/api/file/raw" in out
        assert "path=wiki%2Fpic.png" in out or "path=wiki%2F.%2Fpic.png" in out

    def test_basedir_overrides_stale_preview_path(self, driver_path):
        """data.baseDir must win over a stale _previewCurrentPath.

        Without the fix, a stale _previewCurrentPath (from the last-previewed
        workspace file) would resolve wiki images against the wrong directory.
        """
        out = _render_preview(
            driver_path,
            "![pic](./pic.png)",
            preview_current_path="unrelated/other.md",
            base_dir="wiki/page.md",
        )
        assert "<img" in out
        # Must resolve against baseDir (wiki/), NOT _previewCurrentPath (unrelated/)
        assert "path=wiki%2Fpic.png" in out or "path=wiki%2F.%2Fpic.png" in out
        assert "unrelated" not in out, (
            f"Stale _previewCurrentPath must NOT be used when data.baseDir is set: {out!r}"
        )

    def test_no_basedir_falls_back_to_preview_path(self, driver_path):
        """Without data.baseDir, _previewCurrentPath is used (backwards compat)."""
        out = _render_preview(
            driver_path,
            "![pic](./pic.png)",
            preview_current_path="fallback/note.md",
        )
        assert "<img" in out
        assert "path=fallback%2Fpic.png" in out or "path=fallback%2F.%2Fpic.png" in out


class TestWikiBrowserPassesBaseDir:
    """The wiki browser caller in panels.js must pass baseDir=path to
    renderMarkdownPreviewContent so images resolve against the wiki page's
    directory, not a stale _previewCurrentPath (#5933 F2)."""

    def test_wiki_open_page_passes_basedir(self):
        """panels.js _wikiBrowserOpenPage must pass baseDir to renderMarkdownPreviewContent."""
        src = PANELS_JS_PATH.read_text(encoding="utf-8")
        # Locate the _wikiBrowserOpenPage call to renderMarkdownPreviewContent.
        marker = "renderMarkdownPreviewContent({content: data.content, el: document.getElementById('wikiBrowserMd')})"
        assert marker not in src, (
            "wiki browser must pass baseDir — found bare renderMarkdownPreviewContent call without baseDir"
        )
        # The call must now include baseDir: path (or baseDir:path).
        assert "baseDir" in src, "wiki browser must pass baseDir to renderMarkdownPreviewContent"

