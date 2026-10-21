"""PR #8303 — second maintainer gate (review of head ``e795138e``).

Six findings, reproduced here against the REAL ``renderMd()`` in
``static/ui.js`` (Node driver, same extraction strategy as
``tests/test_issue7634_md_image_split_lines.py``) and the REAL
``/api/media`` handler in ``api/routes.py`` (in-process handler call, same
pattern as ``tests/test_media_inline.py``).

1. [CORE] Alt-text HTML bypasses the sanitizer. A refused bare path
   returns the ORIGINAL markdown source, and that source was stashed as a
   "trusted" fragment and restored AFTER the tag sanitizer — so raw HTML
   from the alt text reached ``innerHTML`` unsanitized.
2. [CORE] ``?generated=1`` skipped the ordinary media authorization, so
   it could serve files plain mode refuses (a cache directory symlinked
   outside every allowed root) and non-image types.
3. [SILENT] A backtick example split over two lines became a live
   artifact card: the code-span stash only matched single-line spans while
   the image regex accepts one line break.
4. [SILENT] Internal stash tokens (math, ``MEDIA:``) leaked into the alt
   text and download name of a generated card.
5. [SILENT] A code span inside an image's alt text corrupted the ``<img>``
   tag instead of rendering one clean image.
6. [NEW] Malformed image syntax backtracked quadratically and stalled the
   renderer.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

_counter = itertools.count()

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

CACHE_PNG = "/home/user/.hermes/cache/images/mai_20260918_101400_2248f18b.png"


# ── Node driver: runs the REAL renderMd() from static/ui.js ──────────────────

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = {
  createElement: () => ({ innerHTML: '', textContent: '' }),
  baseURI: 'http://127.0.0.1:8787/',
  addEventListener: () => {},
  querySelectorAll: () => [],
  readyState: 'complete',
};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function _sessionUrlForSid(sid) { return '/session/' + encodeURIComponent(String(sid || '')); }
const t = k => k;
const li = () => '';
const S = {};
function _remoteImageAllowed() { return true; }
function _remoteImagePlaceholderHtml(u) { return `<a href="${esc(u)}">[img]</a>`; }
function _mediaPlayerHtml(k, s, n) { return `<${k} src="${esc(s)}"></${k}>`; }

for (const name of ['_DATA_IMAGE_RE', '_DATA_IMAGE_SVG_RE', '_DATA_IMAGE_MAX_LEN',
                    '_IMAGE_EXTS', '_PDF_EXTS', '_HTML_EXTS', '_SVG_EXTS',
                    '_AUDIO_EXTS', '_VIDEO_EXTS', '_CSV_EXTS',
                    '_EXCALIDRAW_EXTS']) {
  const m = src.match(new RegExp('const ' + name + '=([^\\n]*);'));
  if (!m) throw new Error(name + ' const not found in ui.js');
  globalThis[name] = eval('(' + m[1] + ')');
}

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

for (const name of ['_mediaKindForName', '_isSafeDataImageUri', '_dataImageHtml',
                    '_mdImageHtml', '_mdLocalImagePathRef', '_mediaTokenParts',
                    '_inlineMediaHtmlForRef', '_matchBacktickFenceLine',
                    '_isBacktickFenceClose', 'renderMd']) {
  if (src.search(new RegExp('function\\s+' + name + '\\s*\\(')) < 0) continue;
  eval(extractFunc(name));
}

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => {
  // --bench <repeats>: time N renders of the same input, print elapsed ms.
  const bench = process.argv.indexOf('--bench');
  if (bench > -1) {
    const repeats = parseInt(process.argv[bench + 1], 10) || 1;
    const t0 = process.hrtime.bigint();
    let out = '';
    for (let i = 0; i < repeats; i++) out = renderMd(buf);
    const ms = Number(process.hrtime.bigint() - t0) / 1e6;
    process.stdout.write(JSON.stringify({ ms: ms, html: out }));
    return;
  }
  process.stdout.write(renderMd(buf));
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("md_image_gate_8303_r2") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _render(driver_path: str, markdown: str) -> str:
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS)],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


def _bench_ms(driver_path: str, markdown: str, repeats: int = 5) -> float:
    """Milliseconds for ``repeats`` renders of ``markdown`` (in-process)."""
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS), "--bench", str(repeats)],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    import json

    return float(json.loads(result.stdout)["ms"])


# ── Finding 1: alt-text HTML must not bypass the tag sanitizer ───────────────

_ALT_HTML_XSS = (
    '![<span><img src=x onerror="window.__gateProbe=2"></span>](/images/logo.png)'
)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_refused_alt_html_is_sanitized_not_stashed(driver_path):
    """A refused path returns the original source, which must be sanitized.

    The source text is model-controlled: the alt may carry raw HTML. It must
    go through the tag sanitizer like any other text, never be restored
    after it as a "trusted" fragment.
    """
    html = _render(driver_path, _ALT_HTML_XSS)
    assert "onerror" not in html, html
    assert "<img src=x" not in html, html
    # The visible text is still there — the path was refused, not dropped.
    assert "/images/logo.png" in html, html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "markdown",
    [
        _ALT_HTML_XSS,
        "- item\n  " + _ALT_HTML_XSS,
        "## heading\n\n" + _ALT_HTML_XSS,
    ],
    ids=["paragraph", "list_item", "heading"],
)
def test_refused_alt_html_is_sanitized_in_every_context(driver_path, markdown):
    html = _render(driver_path, markdown)
    assert "onerror" not in html, html
    assert "<img src=x" not in html, html


# ── Finding 3: a backtick example split over two lines stays literal ────────


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_backtick_example_split_over_two_lines_is_not_a_live_image(driver_path):
    """``\\`![cat]`` + newline + ``(file:///tmp/a.png)`` is a code example.

    The image regex accepts one line break between ``]`` and ``(``; the
    code-span stash must protect the example from it, exactly as it does
    for a single-line example.
    """
    html = _render(driver_path, "`![cat]\n(file:///tmp/a.png)`")
    assert "api/media?path=" not in html, html
    assert "<img" not in html, html
    assert "<code>" in html, html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_backtick_cache_example_split_over_two_lines_is_not_a_live_image(
    driver_path,
):
    """Same for a bare cache path, which must not request ``generated=1``."""
    html = _render(driver_path, f"`![cat]\n({CACHE_PNG})`")
    assert "api/media?path=" not in html, html
    assert "generated=1" not in html, html
    assert "<img" not in html, html


# ── Finding 4: internal stash tokens must not leak into alt / download ───────


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_math_token_does_not_leak_into_the_generated_card(driver_path):
    """``![$x$]`` + newline + ``(file:///tmp/a.png)``.

    The math stash token must be resolved before the card is built, so the
    alt text and download name carry the math SOURCE, never the raw token.
    """
    html = _render(driver_path, "![$x$]\n(file:///tmp/a.png)")
    assert "\x00M" not in html, html
    assert "alt=\"x\"" in html, html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_media_token_does_not_leak_into_the_generated_card(driver_path):
    html = _render(driver_path, "![MEDIA:/tmp/b.png]\n(file:///tmp/a.png)")
    assert "\x00D" not in html, html
    assert "MEDIA:/tmp/b.png" in html, html


# ── Finding 5: a code span in the alt text renders one clean image ──────────


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "markdown",
    [
        "![`code`](https://example.com/a.png)",
        "- item\n  ![`code`](https://example.com/a.png)",
        "> ![`code`](https://example.com/a.png)",
        "| image |\n| --- |\n| ![`code`](https://example.com/a.png) |",
    ],
    ids=["paragraph", "list_item", "blockquote", "table_cell"],
)
def test_code_span_in_image_alt_renders_one_clean_img(driver_path, markdown):
    html = _render(driver_path, markdown)
    assert html.count("<img") == 1, html
    # The broken output leaked the code fragment as text after the tag.
    assert "code</code>" not in html, html
    assert "alt=" in html, html


# ── Finding 6: malformed image syntax must not stall the renderer ───────────


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_malformed_image_syntax_does_not_stall_the_renderer(driver_path):
    """An unclosed image target must not backtrack quadratically.

    The scheme branch of the image regex used to accept trailing spaces
    while the closing ``)`` was also allowed to consume them, so the
    engine tried every way to split the whitespace — quadratic in the
    number of spaces. Rendering must stay roughly linear.
    """
    small = "![a](https://example.test/a" + " " * 8000
    large = "![a](https://example.test/a" + " " * 32000

    t_small = _bench_ms(driver_path, small, repeats=3)
    t_large = _bench_ms(driver_path, large, repeats=3)

    # Absolute budget: the quadratic version needs seconds here, the fixed
    # one milliseconds. Generous enough to survive a loaded CI machine.
    assert t_large < 1500.0, (
        f"rendering the malformed image took {t_large:.0f}ms "
        f"(small={t_small:.0f}ms) — the image regex is backtracking"
    )
    # Scaling budget: 4x the input must not cost ~16x the time.
    assert t_large < max(10.0 * t_small, t_small + 250.0), (
        f"render time grew from {t_small:.0f}ms to {t_large:.0f}ms "
        "for a 4x larger input — quadratic backtracking"
    )


# ── Finding 2: the server enforces generated-image mode ─────────────────────
#
# /api/media grants a few roots unconditionally (/tmp among them), so a
# plain TemporaryDirectory() is never "outside every allowed root" — and
# neither is one under REPO_ROOT when the checkout itself lives in /tmp
# (git worktrees, CI scratch dirs). The fixture location is therefore
# verified against those roots rather than assumed.

_UNCONDITIONAL_MEDIA_ROOTS = (
    pathlib.Path("/tmp"),
    pathlib.Path.home() / ".hermes",
)


def _is_within(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


@contextlib.contextmanager
def _outside_allowed_roots_dir():
    """Yield a temp dir verified to sit outside every unconditional root."""
    for parent in (pathlib.Path.home() / ".cache", REPO_ROOT):
        if any(_is_within(parent, root) for root in _UNCONDITIONAL_MEDIA_ROOTS):
            continue
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        with tempfile.TemporaryDirectory(dir=parent) as outside:
            yield pathlib.Path(outside)
            return
    pytest.skip(
        "no fixture directory available outside the /api/media allowed roots "
        f"({', '.join(str(r) for r in _UNCONDITIONAL_MEDIA_ROOTS)})"
    )


class _Handler:
    """Minimal BaseHTTPRequestHandler stand-in (same as tests/test_media_inline.py)."""

    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = b""

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers[k.lower()] = v

    def end_headers(self):
        pass

    class _W:
        def __init__(self, owner):
            self.owner = owner

        def write(self, b):
            self.owner.body += b

        def flush(self):
            pass

    @property
    def wfile(self):
        return self._W(self)


def _serve(query: str, *, workspace: pathlib.Path, home: pathlib.Path):
    """Call the real /api/media handler against an isolated Hermes home."""
    from api import routes

    handler = _Handler()
    with mock.patch.dict(
        os.environ,
        {"HERMES_HOME": str(home), "MEDIA_ALLOWED_ROOTS": ""},
    ), mock.patch(
        "api.auth.is_auth_enabled", lambda: False
    ), mock.patch(
        "api.workspace.get_last_workspace", lambda: str(workspace)
    ):
        routes._handle_media(handler, SimpleNamespace(query=query, path="/api/media"))
    return handler


@pytest.fixture
def symlinked_cache_home():
    """Isolated HERMES_HOME whose ``cache/images`` is a SYMLINK elsewhere.

    The symlink target sits outside every /api/media root, so plain mode
    refuses it. Generated mode must refuse it too: it is plain mode plus
    the cache-root requirement, never a way around the authorization.
    """
    from tests.conftest import TEST_WORKSPACE

    base = TEST_WORKSPACE / f".genimg-symlink-{os.getpid()}-{next(_counter)}"
    home = base / "hermes-home"
    (home / "cache").mkdir(parents=True)
    base.mkdir(parents=True, exist_ok=True)

    with _outside_allowed_roots_dir() as outside:
        images = outside / "ext" / "imgs"
        images.mkdir(parents=True)
        (images / "a.png").write_bytes(PNG_1X1)
        (images / "notes.txt").write_text("not an image")
        try:
            (home / "cache" / "images").symlink_to(images, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows
            shutil.rmtree(base, ignore_errors=True)
            pytest.skip(f"symlinks unavailable on this platform: {exc}")

        yield SimpleNamespace(
            base=base,
            workspace=base,
            home=home,
            outside=images,
            image=images / "a.png",
            notes=images / "notes.txt",
        )
    shutil.rmtree(base, ignore_errors=True)


def test_generated_mode_refuses_files_plain_mode_refuses(symlinked_cache_home):
    """The symlinked cache dir is outside every allowed root.

    Plain mode already refuses it; generated mode must not be a way around
    the authorization.
    """
    import urllib.parse

    query = f"path={urllib.parse.quote(str(symlinked_cache_home.image))}&generated=1"
    handler = _serve(
        query,
        workspace=symlinked_cache_home.workspace,
        home=symlinked_cache_home.home,
    )
    assert handler.status == 403, (
        "generated mode must not serve a file plain mode refuses; "
        f"got {handler.status}"
    )
    assert PNG_1X1 not in handler.body


def test_generated_mode_refuses_non_image_types(symlinked_cache_home):
    """Generated mode is for images; a text file in the cache is not one."""
    import urllib.parse

    query = f"path={urllib.parse.quote(str(symlinked_cache_home.notes))}&generated=1"
    handler = _serve(
        query,
        workspace=symlinked_cache_home.workspace,
        home=symlinked_cache_home.home,
    )
    assert handler.status == 403, (
        "generated mode must refuse non-image types; got {0}".format(handler.status)
    )
    assert b"not an image" not in handler.body
