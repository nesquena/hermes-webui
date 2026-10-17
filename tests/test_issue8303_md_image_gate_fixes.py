"""PR #8303 maintainer gate review — 3 required fixes + 3 smaller fixes.

Reproduces every finding from the ``nesquena`` gate review at head
``84f7ac0e`` against the REAL ``renderMd()`` / ``_mdImageHtml()`` in
``static/ui.js`` (Node driver, same pattern as
``tests/test_issue7634_md_image_split_lines.py``) and the REAL
``/api/media`` handler in ``api/routes.py`` (in-process handler call, same
pattern as ``tests/test_media_inline.py``).

Required fixes pinned here:

1. [CORE/security] A bare local image path must request a mode the SERVER
   enforces: canonical containment inside the owning profile's
   ``cache/images`` root(s), symlinks resolved, served through an anchored
   open. A client-side prefix check alone is not enough — a sibling
   ``cache/images`` directory outside ``HERMES_HOME`` and a symlink inside
   the cache pointing at ``HERMES_HOME/private.png`` both used to load.
2. [SILENT] ``\\`![cat](/.../cache/images/a.png)\\`` (an inline-code example)
   must stay literal text — it used to fire an image request and leave empty
   code text because code spans were restored before the image passes ran.
3. [SILENT] The generated artifact card must survive lists, tables and
   headings — ``inlineMd()``'s ``SAFE_INLINE`` pass used to escape part of
   the fragment so the card printed as raw HTML.

Smaller fixes pinned here:

* A REFUSED bare path returns the ORIGINAL source text (so
  ``![**bold**](/images/logo.png)`` does not print literal ``<strong>``).
* The unreachable ``~`` branch in ``_mdImageHtml`` is removed.
* ``test_image_in_list_item_split_across_lines_renders`` (in the #7634 file)
  now actually exercises a list item instead of a paragraph after a list.
"""

from __future__ import annotations

import itertools
import os
import pathlib
import shutil
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

_counter = itertools.count()

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

# Minimal valid 1x1 transparent PNG (67 bytes).
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

CACHE_PNG = "/home/user/.hermes/cache/images/mai_20260918_101400_2248f18b.png"
CACHE_PNG_ENC = (
    "api/media?path=%2Fhome%2Fuser%2F.hermes%2Fcache%2Fimages%2F"
    "mai_20260918_101400_2248f18b.png"
)


# ── Node driver: runs the REAL renderMd() from static/ui.js ──────────────────
# Same extraction strategy as tests/test_issue7634_md_image_split_lines.py:
# pull the production functions out of ui.js and execute them, so these
# assertions cannot drift from the shipped renderer.

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
  eval(extractFunc(name));
}

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("md_image_gate_8303") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _render(driver_path: str, markdown: str) -> str:
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS)],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


def _ui_js_source() -> str:
    return UI_JS.read_text(encoding="utf-8")


def _md_image_html_source() -> str:
    src = _ui_js_source()
    start = src.index("function _mdImageHtml(")
    depth = 1
    i = src.index("{", start) + 1
    while depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


# ── Fix 1: the request must ask for a SERVER-enforced generated-image mode ──
# A client-side "contains cache/images" prefix check cannot be trusted: the
# client cannot know the server's HERMES_HOME, so the server has to enforce
# the containment for this mode.

def test_bare_cache_path_asks_for_server_enforced_generated_mode(driver_path):
    html = _render(driver_path, f"![cat]({CACHE_PNG})")
    assert CACHE_PNG_ENC in html
    assert "generated=1" in html, (
        "a bare local cache-image path must request a mode the SERVER enforces; "
        "the plain path= request only runs /api/media's broad root allowlist"
    )


def test_generated_mode_is_requested_only_for_bare_cache_paths(driver_path):
    """The mode must not widen anything else: https/file:// keep the plain form."""
    for markdown in ("![cat](https://example.com/cat.png)",
                     "![cat](file:///tmp/chart.png)"):
        html = _render(driver_path, markdown)
        assert "generated=1" not in html, markdown


# ── Fix 1 (server side): the route enforces containment + anchored open ─────

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
    """Call the real /api/media handler against an isolated Hermes home.

    ``workspace`` is reported as the active workspace so the whole fixture
    tree is inside an /api/media root — exactly the maintainer's setup, where
    an unrelated ``/tmp/.../cache/images/a.png`` loaded because /tmp is an
    unconditional root. That is the ONLY reason a sibling cache directory
    outside HERMES_HOME used to be reachable.
    """
    from api import routes

    handler = _Handler()
    with mock.patch.dict(
        __import__("os").environ,
        {"HERMES_HOME": str(home), "MEDIA_ALLOWED_ROOTS": ""},
    ), mock.patch(
        "api.auth.is_auth_enabled", lambda: False
    ), mock.patch(
        "api.workspace.get_last_workspace", lambda: str(workspace)
    ):
        routes._handle_media(handler, SimpleNamespace(query=query, path="/api/media"))
    return handler


@pytest.fixture
def image_cache_home():
    """Isolated HERMES_HOME with a real cache image + a symlink escape attempt.

    Also builds, OUTSIDE the home, a sibling ``cache/images`` directory —
    exactly the shape that used to load with a bare ``path=`` request.
    """
    from tests.conftest import TEST_WORKSPACE

    base = TEST_WORKSPACE / f".genimg-media-{os.getpid()}-{next(_counter)}"
    home = base / "hermes-home"
    cache_images = home / "cache" / "images"
    cache_images.mkdir(parents=True)
    base.mkdir(parents=True, exist_ok=True)

    # A real generated image inside the owning profile's image cache.
    (cache_images / "ok.png").write_bytes(PNG_1X1)

    # A secret next to the cache that must never be reachable through it.
    private = home / "private.png"
    private.write_bytes(PNG_1X1)

    # A symlink INSIDE the cache pointing at that secret.
    link = cache_images / "leak.png"
    try:
        link.symlink_to(private)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows
        shutil.rmtree(base, ignore_errors=True)
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    # A sibling cache/images directory OUTSIDE HERMES_HOME, with its own image.
    sibling_images = base / "elsewhere" / "cache" / "images"
    sibling_images.mkdir(parents=True)
    (sibling_images / "a.png").write_bytes(PNG_1X1)

    yield SimpleNamespace(
        base=base,
        workspace=base,
        home=home,
        ok=cache_images / "ok.png",
        private=private,
        link=link,
        sibling=sibling_images / "a.png",
    )
    shutil.rmtree(base, ignore_errors=True)


@pytest.mark.parametrize("which", ["link", "sibling"])
def test_generated_image_mode_refuses_outside_image_cache(image_cache_home, which):
    """Refused server-side, for BOTH the symlink and the sibling cache dir."""
    import urllib.parse

    target = getattr(image_cache_home, which)
    query = f"path={urllib.parse.quote(str(target))}&generated=1"
    handler = _serve(
        query, workspace=image_cache_home.workspace, home=image_cache_home.home
    )
    assert handler.status == 403, (
        f"{which} must be refused in generated-image mode; got {handler.status}"
    )
    assert PNG_1X1 not in handler.body


def test_generated_image_mode_serves_the_owning_profile_cache(image_cache_home):
    """The feature itself must keep working: a real cached image still serves."""
    import urllib.parse

    query = f"path={urllib.parse.quote(str(image_cache_home.ok))}&generated=1"
    handler = _serve(
        query, workspace=image_cache_home.workspace, home=image_cache_home.home
    )
    assert handler.status == 200
    assert "image/png" in handler.headers.get("content-type", "")
    assert handler.body == PNG_1X1


def test_generated_image_mode_narrows_only_itself(image_cache_home):
    """The sibling directory is still reachable through the plain mode.

    Proves the containment is enforced by the NEW mode rather than by
    globally narrowing /api/media (which would break MEDIA: artifacts).
    """
    import urllib.parse

    query = f"path={urllib.parse.quote(str(image_cache_home.sibling))}"
    handler = _serve(
        query, workspace=image_cache_home.workspace, home=image_cache_home.home
    )
    assert handler.status == 200


def test_generated_image_mode_ignores_session_token_grants(image_cache_home):
    """A MEDIA: token in a transcript must not unlock generated-image mode."""
    import urllib.parse

    from api import routes

    session = SimpleNamespace(
        messages=[
            {
                "role": "assistant",
                "content": f"MEDIA:{image_cache_home.sibling}",
            }
        ]
    )
    handler = _Handler()
    with mock.patch.dict(
        os.environ,
        {"HERMES_HOME": str(image_cache_home.home), "MEDIA_ALLOWED_ROOTS": ""},
    ), mock.patch("api.auth.is_auth_enabled", lambda: False), mock.patch(
        "api.workspace.get_last_workspace",
        lambda: str(image_cache_home.workspace),
    ), mock.patch.object(
        routes, "get_session", return_value=session
    ):
        query = (
            f"path={urllib.parse.quote(str(image_cache_home.sibling))}"
            "&session_id=s-8303&generated=1"
        )
        routes._handle_media(
            handler, SimpleNamespace(query=query, path="/api/media")
        )
    assert handler.status == 403


# ── Fix 2: inline-code image examples must stay literal text ────────────────

@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_inline_code_image_example_is_not_a_live_image(driver_path):
    html = _render(driver_path, f"`![cat]({CACHE_PNG})`")
    assert "api/media?path=" not in html
    assert "<img" not in html
    # The example is still shown, as code.
    assert "<code>" in html
    assert CACHE_PNG in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_inline_code_image_example_inside_list_item_is_not_a_live_image(driver_path):
    html = _render(driver_path, f"- show `![cat]({CACHE_PNG})` like this")
    assert "api/media?path=" not in html
    assert "<img" not in html
    assert CACHE_PNG in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_inline_code_image_example_inside_table_cell_is_not_a_live_image(driver_path):
    html = _render(
        driver_path,
        "| syntax | meaning |\n| --- | --- |\n| `![x](%s)` | example |" % CACHE_PNG,
    )
    assert "api/media?path=" not in html
    assert "<img" not in html


# ── Fix 3: the artifact card must survive lists, tables and headings ────────

@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_artifact_card_in_list_item_is_not_escaped_markup(driver_path):
    html = _render(driver_path, f"- item\n  ![cat]({CACHE_PNG})")
    assert 'class="msg-artifact-image"' in html, html
    assert "&lt;span" not in html and "&lt;svg" not in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_artifact_card_in_table_cell_is_not_escaped_markup(driver_path):
    html = _render(
        driver_path,
        "| image |\n| --- |\n| ![cat](%s) |" % CACHE_PNG,
    )
    assert 'class="msg-artifact-image"' in html, html
    assert "&lt;span" not in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_artifact_card_in_heading_is_not_escaped_markup(driver_path):
    html = _render(driver_path, f"## result\n\n![cat]({CACHE_PNG})")
    assert 'class="msg-artifact-image"' in html, html
    assert "&lt;span" not in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_artifact_card_in_plain_paragraph_keeps_its_wrapper(driver_path):
    """The card is dropped in whole, so the sanitizer cannot strip it.

    ``_tag()`` used to rewrite ``<span class="msg-artifact-image">`` (class
    dropped) and delete the download ``<svg>`` entirely, because the card was
    emitted before the tag-sanitizer pass.
    """
    html = _render(driver_path, f"![cat]({CACHE_PNG})")
    assert 'class="msg-artifact-image"' in html, html
    assert "<svg" in html, html
    assert "&lt;span" not in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_https_image_in_list_item_still_renders(driver_path):
    """Regression guard: the stash must not swallow ordinary https images."""
    html = _render(
        driver_path,
        "- item\n  ![cat](https://example.com/cat.png)",
    )
    assert 'src="https://example.com/cat.png"' in html, html
    assert "&lt;img" not in html


# ── Smaller fix 1: a refused bare path returns the ORIGINAL source text ─────

@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_refused_bare_path_keeps_original_source_text(driver_path):
    """``![**bold**](/images/logo.png)`` must not print a literal <strong>.

    Bold has already run by the time _mdImageHtml() is called, so escaping the
    REBUILT markdown leaks the generated tag as visible text.
    """
    html = _render(driver_path, "![**bold**](/images/logo.png)")
    assert "&lt;strong&gt;" not in html, html
    assert "<strong>bold</strong>" in html, html
    assert "api/media?path=" not in html


# ── Smaller fix 2: the dead `~` branch is gone ─────────────────────────────

def test_md_image_html_has_no_dead_tilde_branch():
    body = _md_image_html_source()
    assert "~" not in body, (
        "the ~/ tilde branch is unreachable from renderMd (the image regex only "
        "admits a scheme or a root-relative '/' path) — remove the dead branch"
    )


# ── Smaller fix 3: the #7634 list-item test now exercises a list item ───────

def test_7634_list_item_test_really_exercises_a_list_item():
    """Guards the repaired test in test_issue7634_md_image_split_lines.py."""
    src = (
        REPO_ROOT / "tests" / "test_issue7634_md_image_split_lines.py"
    ).read_text(encoding="utf-8")
    start = src.index("def test_image_in_list_item_split_across_lines_renders(")
    body = src[start:src.index("\ndef ", start + 10)]
    assert "_render(driver_path," in body
    assert '"- ' in body, (
        "the test must render a real list item, not a paragraph after a list"
    )