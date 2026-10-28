"""
#8033: Windows drive-letter file:/// URIs resolve to the wrong path.

Root cause (maintainer-confirmed 2026-10-07):
- ``file:///C:/Users/me/report.md`` parses (browser and node ``URL.pathname``)
  to ``/C:/Users/me/report.md`` — the file-URI grammar keeps the leading
  separator before the drive letter.
- Both client converters forward that leading slash to the backend: the
  settled renderer (static/ui.js ``_inlineMediaHtmlForRef``,
  ``decodeURIComponent(u.pathname||...)``) and the streaming converter
  (static/messages.js ``_smdLinkHref``, ``href.replace(/^file:\\/\\//i,'')``).
- The backend choke point is ``_handle_media`` in api/routes.py:
  ``target = Path(raw_path).resolve()``. On Windows ``Path('/C:/x')`` is
  root-relative and resolves to the wrong location.

Fix (server-OS-gated, per maintainer review 2026-10-14):
- Server: ``_normalize_media_request_path`` strips exactly ONE URI-style
  leading separator when it sits immediately before a drive letter
  (regex ``^[\\/][A-Za-z]:[\\/]``) — and ONLY on a Windows server
  (``_is_windows_server``). On POSIX the separator is preserved: stripping
  ``/C:/report.png`` there would turn an absolute path into a cwd-relative
  ``C:/report.png`` that can alias a file inside an allowed workspace. POSIX
  and UNC paths never match the shape and are returned unchanged. No generic
  leading-slash removal.
- Clients: the once-decoded pathname is forwarded to the backend AS-IS (the
  previous client-side one-separator strip is gone — only the server knows
  its own OS semantics, so only it may convert); non-local file authorities
  (UNC: ``file://server/share/...``) are explicitly rejected, never silently
  rewired to a local path. Streaming and settled policies are identical.

These are behavioural tests (pure Python for the server helper, node-driven
for the real client converters). The old static source-string pins in
tests/test_media_inline.py cannot catch this bug.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import PureWindowsPath
from types import SimpleNamespace
from unittest import mock

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
MESSAGES_JS_PATH = REPO_ROOT / "static" / "messages.js"

NODE = shutil.which("node")

# Node is required only by the two client-converter test classes (they eval the
# real JS functions in a node VM). The server-side helper and route tests are
# pure Python and must keep running on a node-less environment, so the skip is
# scoped to those classes instead of the whole module.
_NODE_REQUIRED = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# 1-pixel transparent PNG (67 bytes)
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ── Server: _normalize_media_request_path (pure Python) ───────────────────────

class TestNormalizeMediaRequestPath:
    """Behavioral coverage for the server-side Windows drive-letter strip.

    The strip is gated on the server being Windows (``_is_windows_server``),
    so every strip assertion pins the gate open (``windows=True``) and the
    POSIX assertions pin it closed (``windows=False``). The tests are
    therefore platform-independent and pass on both POSIX and Windows CI.
    """

    @staticmethod
    def _norm(raw, *, windows=False):
        from api import routes
        with mock.patch.object(routes, "_is_windows_server", return_value=windows):
            return routes._normalize_media_request_path(raw)

    # ① upper/lowercase drive letters — Windows server only
    def test_uppercase_drive_letter_strips_one_leading_separator(self):
        assert self._norm("/C:/Users/me/report.md", windows=True) == "C:/Users/me/report.md"

    def test_lowercase_drive_letter_strips_one_leading_separator(self):
        assert self._norm("/c:/Users/me/report.md", windows=True) == "c:/Users/me/report.md"

    def test_backslash_uri_style_separator_stripped(self):
        assert self._norm("\\C:\\Users\\me\\report.md", windows=True) == "C:\\Users\\me\\report.md"
        assert self._norm("/C:\\Users\\me\\report.md", windows=True) == "C:\\Users\\me\\report.md"

    # ① encoded space / literal percent: helper is shape-only, no percent-decoding
    def test_encoded_space_kept_literal(self):
        assert self._norm("/C:/Users/me/my%20report.md", windows=True) == "C:/Users/me/my%20report.md"

    def test_literal_percent_kept_literal(self):
        assert self._norm("/C:/Users/me/100%25.png", windows=True) == "C:/Users/me/100%25.png"

    # ③ POSIX paths are untouched
    def test_posix_absolute_path_unchanged(self):
        for p in ("/home/me/x", "/home/me/x.png", "/tmp/x.png", "/"):
            assert self._norm(p) == p

    # UNC paths are untouched (they never match the drive-letter shape)
    def test_unc_backslash_path_unchanged(self):
        assert self._norm("\\\\server\\share\\x.png") == "\\\\server\\share\\x.png"

    def test_unc_forward_slash_path_unchanged(self):
        assert self._norm("//server/share/x.png") == "//server/share/x.png"

    # drive-relative 'C:foo' (no separator after colon) is not a drive-letter URI
    def test_drive_relative_colon_path_unchanged(self):
        assert self._norm("/C:foo") == "/C:foo"

    # already-clean drive path and empty input
    def test_clean_drive_path_unchanged(self):
        assert self._norm("C:/Users/me/report.md") == "C:/Users/me/report.md"

    def test_empty_string_unchanged(self):
        assert self._norm("") == ""

    # exactly ONE separator is stripped, not a generic leading-slash removal.
    # A double separator ('//C:/x') is NOT the drive-letter URI shape (the
    # separator must sit immediately before the drive letter), so it is
    # preserved — a generic leading-slash removal would wrongly eat it.
    def test_double_separator_not_a_drive_uri_shape(self):
        assert self._norm("//C:/x", windows=True) == "//C:/x"

    # #8033 must-fix (maintainer 2026-10-14): the conversion only runs on a
    # Windows server. On POSIX, '/C:/...' must stay absolute — stripping it
    # would alias '<cwd>/C:/...' inside an allowed workspace.
    def test_posix_drive_letter_shape_stays_absolute(self):
        raw = "/C:/report.png"
        assert self._norm(raw, windows=False) == raw  # separator preserved
        resolved = pathlib.Path(self._norm(raw, windows=False)).resolve()
        assert resolved.is_absolute()
        assert resolved == pathlib.Path("/C:/report.png")  # stays at the root
        assert resolved != pathlib.Path("C:/report.png").resolve()  # never cwd-relative

    def test_drive_letter_strip_only_happens_on_windows_server(self):
        raw = "/C:/Users/me/report.md"
        assert self._norm(raw, windows=True) == "C:/Users/me/report.md"
        assert self._norm(raw, windows=False) == raw

    def test_pure_windows_path_drive_semantics(self):
        """PureWindowsPath proves the bug and the fix at the Windows semantic layer.

        Pre-fix shape '/C:/Users/me/report.md' is drive='' root='\\' and NOT
        absolute (no drive). After normalization (Windows server) the drive is
        'C:' and the path is absolute on the C: drive.
        """
        buggy = PureWindowsPath("/C:/Users/me/report.md")
        assert buggy.drive == ""
        assert buggy.root == "\\"
        assert not buggy.is_absolute()

        fixed = PureWindowsPath(self._norm("/C:/Users/me/report.md", windows=True))
        assert fixed.drive == "C:"
        assert fixed.root == "\\"
        assert fixed.is_absolute()
        assert str(fixed) == r"C:\Users\me\report.md"

    def test_pure_windows_path_posix_shape_unchanged(self):
        posix = PureWindowsPath(self._norm("/home/me/x"))
        assert str(posix) == r"\home\me\x"  # still a plain rooted path
        assert posix.drive == ""


# ── Server: _handle_media wiring (production calls the helper) ────────────────

class _CaptureHandler:
    """Minimal http.server handler stub (mirrors tests/test_media_inline.py)."""

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


class TestHandleMediaWiresNormalization:
    """/api/media must run drive-letter normalization BEFORE Path resolution."""

    def test_handle_media_consults_normalizer_and_serves_posix_unchanged(self):
        """A servable POSIX path passes through the helper untouched and serves.

        Deterministic end-to-end wiring proof: the helper is invoked with the
        exact raw path, and (because it is shape-only) the POSIX path still
        resolves inside an allowed root.

        The file is built inside the isolated allowed WORKSPACE (mocked via
        get_last_workspace) instead of /tmp, so this 200-path proof runs on
        every platform — including ones whose temp dir is not an allowed
        media root (maintainer follow-up 2026-10-14: no more skip).
        """
        from api import routes

        with tempfile.TemporaryDirectory() as home:
            hermes_home = pathlib.Path(home) / ".hermes"
            hermes_home.mkdir(parents=True)
            ws = hermes_home / "workspace"
            ws.mkdir()
            png = ws / "shot.png"
            png.write_bytes(_PNG_BYTES)

            handler = _CaptureHandler()
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}), \
                 mock.patch.object(routes, "get_last_workspace", lambda: str(ws)), \
                 mock.patch("api.auth.is_auth_enabled", lambda: False), \
                 mock.patch.object(
                     routes,
                     "_normalize_media_request_path",
                     wraps=routes._normalize_media_request_path,
                 ) as spy:
                routes._handle_media(
                    handler,
                    SimpleNamespace(
                        query=f"path={urllib.parse.quote(str(png))}&inline=1",
                        path="/api/media",
                    ),
                )

            assert spy.call_count == 1
            assert spy.call_args.args[0] == str(png)
            assert handler.status == 200
            assert handler.body == _PNG_BYTES

    def test_handle_media_drive_letter_shape_never_aliases_workspace(self):
        """A '/C:/...' request must never resolve into an allowed workspace.

        Maintainer must-fix (2026-10-14): the pre-fix normalizer stripped the
        leading separator on every OS, turning the absolute '/C:/report.png'
        into the cwd-relative 'C:/report.png'. With the server cwd inside an
        allowed workspace, a decoy at '<ws>/C:/report.png' was served. The
        strip is now gated on the server being Windows, so the request fails
        closed: the raw path still reaches the helper, the resolved target
        stays absolute at '/C:/report.png', and the decoy bytes are never
        served.

        The server-OS gate is pinned to POSIX (windows=False) so the alias
        proof is deterministic on every CI platform, and os.getcwd is pinned
        to the allowed workspace so a (buggy) strip would land exactly on the
        decoy.
        """
        from api import routes

        with tempfile.TemporaryDirectory() as home:
            hermes_home = pathlib.Path(home) / ".hermes"
            hermes_home.mkdir(parents=True)
            ws = hermes_home / "workspace"
            ws.mkdir()
            # Decoy at the exact spot a buggy POSIX strip would resolve to:
            # '<cwd>/C:/report.png' with cwd pinned to the allowed workspace.
            decoy_dir = ws / "C:"
            decoy_dir.mkdir()
            decoy = decoy_dir / "report.png"
            decoy.write_bytes(_PNG_BYTES)

            handler = _CaptureHandler()
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}), \
                 mock.patch.object(routes, "get_last_workspace", lambda: str(ws)), \
                 mock.patch("api.auth.is_auth_enabled", lambda: False), \
                 mock.patch("os.getcwd", return_value=str(ws)), \
                 mock.patch.object(
                     routes,
                     "_is_windows_server",
                     return_value=False,
                 ), \
                 mock.patch.object(
                     routes,
                     "_normalize_media_request_path",
                     wraps=routes._normalize_media_request_path,
                 ) as spy:
                routes._handle_media(
                    handler,
                    SimpleNamespace(
                        query="path=%2FC%3A%2Freport.png&inline=1",  # /C:/report.png
                        path="/api/media",
                    ),
                )

            assert spy.call_count == 1
            assert spy.call_args.args[0] == "/C:/report.png"  # raw path reaches helper
            assert handler.status != 200  # fail closed, POSIX cannot serve a drive path
            assert handler.body != _PNG_BYTES  # the allowed-cwd decoy is never served

    def test_handle_media_serves_after_windows_normalization(self):
        """When the helper does its Windows job, the whole production chain
        (normalize → Path → resolve → allowed-root → serve) succeeds.

        On POSIX we cannot resolve a real 'C:/...' path, so the helper is
        simulated returning a genuine allowed-root path — exactly what it
        produces on a Windows server for '/C:/...' inputs.
        """
        from api import routes

        with tempfile.TemporaryDirectory() as home:
            hermes_home = pathlib.Path(home) / ".hermes"
            hermes_home.mkdir(parents=True)
            ws = hermes_home / "workspace"
            ws.mkdir()
            png = ws / "report.png"
            png.write_bytes(_PNG_BYTES)

            handler = _CaptureHandler()
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}), \
                 mock.patch.object(routes, "get_last_workspace", lambda: str(ws)), \
                 mock.patch("api.auth.is_auth_enabled", lambda: False), \
                 mock.patch.object(
                     routes,
                     "_normalize_media_request_path",
                     return_value=str(png),
                 ):
                routes._handle_media(
                    handler,
                    SimpleNamespace(
                        query="path=%2FC%3A%2FUsers%2Fme%2Freport.png&inline=1",
                        path="/api/media",
                    ),
                )

            assert handler.status == 200
            assert handler.body == _PNG_BYTES


# ── Client converters: node-driven behavioural tests ──────────────────────────

def _extract_fn(src, name):
    """Return the text of a top-level `function <name>(...){...}` block."""
    m = re.search(rf"function {re.escape(name)}\s*\(", src)
    if not m:
        return None
    start = m.start()
    brace_pos = src.index("{", m.end())
    depth = 1
    pos = brace_pos + 1
    while pos < len(src) and depth > 0:
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        pos += 1
    return src[start:pos]


_SETTLED_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
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
eval(extractFunc('_mediaKindForName'));
eval(extractFunc('_inlineMediaHtmlForRef'));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _PDF_EXTS=/\.pdf$/i;
const _HTML_EXTS=/\.(html?|htm)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm|oga)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
const _CSV_EXTS=/\.csv$/i;
const _EXCALIDRAW_EXTS=/\.excalidraw$/i;
function _dataImageHtml(){ return ''; }
function _mediaPlayerHtml(){ return ''; }
function _remoteImageAllowed(){ return true; }
const cases = __CASES__;
const out = {};
for (const c of cases) {
  const html = _inlineMediaHtmlForRef(c);
  const m = html.match(/api\/media\?path=([^"&]+)/);
  out[c] = {
    html: html,
    pathParam: m ? decodeURIComponent(m[1]) : null,
    hasMediaRoute: /api\/media/.test(html),
  };
}
console.log(JSON.stringify(out));
"""

_STREAMING_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
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
eval(extractFunc('_smdLinkHref'));
const cases = __CASES__;
const out = {};
for (const c of cases) {
  const r = _smdLinkHref(c);
  const m = r.match(/api\/media\?path=([^"&]+)/);
  out[c] = {
    result: r,
    pathParam: m ? decodeURIComponent(m[1]) : null,
    hasMediaRoute: /api\/media/.test(r),
  };
}
console.log(JSON.stringify(out));
"""

_SETTLED_CASES = [
    "file:///C:/Users/me/report.png",       # ① uppercase drive
    "file:///c:/Users/me/report.png",       # ① lowercase drive
    "file:///C:/Users/me/my%20report.png",  # ① encoded space (single decode)
    "file:///C:/Users/me/100%25.png",       # ① literal percent (decode once)
    "file:///home/me/x.png",                # ③ POSIX unchanged
    "file://server/share/x.png",            # ② UNC authority → explicit reject
    "file://localhost/C:/x.png",            # localhost authority → local
    "file:///C:/Users/me/report.md",        # non-image drive-letter path
    "file://server:abc/share/x.png",        # ② malformed authority (URL parse
    #                                         throws) → fail-closed reject
]

_STREAMING_CASES = _SETTLED_CASES


def _run_node_driver(driver_src, js_path, cases, tmp_path_factory):
    driver = tmp_path_factory.mktemp("fileuri_driver") / "driver.js"
    driver.write_text(
        driver_src.replace("__CASES__", json.dumps(cases)),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [NODE, str(driver), str(js_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"node driver failed: {completed.stderr}")
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def settled_results(tmp_path_factory):
    return _run_node_driver(_SETTLED_DRIVER, UI_JS_PATH, _SETTLED_CASES, tmp_path_factory)


@pytest.fixture(scope="module")
def streaming_results(tmp_path_factory):
    return _run_node_driver(_STREAMING_DRIVER, MESSAGES_JS_PATH, _STREAMING_CASES, tmp_path_factory)


@_NODE_REQUIRED
class TestSettledFileUriConverter:
    """Real _inlineMediaHtmlForRef from static/ui.js, driven in node.

    The clients forward the once-decoded pathname AS-IS (leading separator
    included): the backend is the only place that knows the server OS and
    therefore the only place that may convert a drive-letter shape.
    """

    def test_uppercase_drive_letter_forwards_once_decoded_pathname(self, settled_results):
        r = settled_results["file:///C:/Users/me/report.png"]
        assert r["hasMediaRoute"]
        assert r["pathParam"] == "/C:/Users/me/report.png"

    def test_lowercase_drive_letter_forwards_once_decoded_pathname(self, settled_results):
        r = settled_results["file:///c:/Users/me/report.png"]
        assert r["pathParam"] == "/c:/Users/me/report.png"

    def test_encoded_space_decoded_once(self, settled_results):
        r = settled_results["file:///C:/Users/me/my%20report.png"]
        assert r["pathParam"] == "/C:/Users/me/my report.png"

    def test_literal_percent_decoded_exactly_once(self, settled_results):
        r = settled_results["file:///C:/Users/me/100%25.png"]
        assert r["pathParam"] == "/C:/Users/me/100%.png"

    def test_posix_path_keeps_leading_slash(self, settled_results):
        r = settled_results["file:///home/me/x.png"]
        assert r["pathParam"] == "/home/me/x.png"

    def test_unc_authority_explicitly_rejected(self, settled_results):
        r = settled_results["file://server/share/x.png"]
        assert not r["hasMediaRoute"], "UNC must never be rewired to api/media"
        assert r["html"] == ""

    def test_malformed_authority_fails_closed(self, settled_results):
        """A file URI whose authority makes URL parsing throw must not fall
        through the catch path into a local media URL (AGENTS.md fail-closed)."""
        r = settled_results["file://server:abc/share/x.png"]
        assert not r["hasMediaRoute"], (
            "a malformed authority must be rejected, not rewired to api/media"
        )
        assert r["html"] == ""

    def test_localhost_authority_treated_as_local(self, settled_results):
        r = settled_results["file://localhost/C:/x.png"]
        assert r["pathParam"] == "/C:/x.png"

    def test_non_image_drive_letter_path_forwards_leading_separator(self, settled_results):
        r = settled_results["file:///C:/Users/me/report.md"]
        assert r["pathParam"] == "/C:/Users/me/report.md"


@_NODE_REQUIRED
class TestStreamingFileUriConverter:
    """Real _smdLinkHref from static/messages.js, driven in node.

    Same forward-as-is policy as the settled converter: the once-decoded
    pathname keeps its leading separator; the server OS gate decides whether
    a drive-letter conversion applies.
    """

    def test_uppercase_drive_letter_forwards_once_decoded_pathname(self, streaming_results):
        r = streaming_results["file:///C:/Users/me/report.png"]
        assert r["hasMediaRoute"]
        assert r["pathParam"] == "/C:/Users/me/report.png"

    def test_lowercase_drive_letter_forwards_once_decoded_pathname(self, streaming_results):
        r = streaming_results["file:///c:/Users/me/report.png"]
        assert r["pathParam"] == "/c:/Users/me/report.png"

    def test_encoded_space_decoded_once(self, streaming_results):
        r = streaming_results["file:///C:/Users/me/my%20report.png"]
        assert r["pathParam"] == "/C:/Users/me/my report.png"

    def test_literal_percent_decoded_exactly_once(self, streaming_results):
        r = streaming_results["file:///C:/Users/me/100%25.png"]
        assert r["pathParam"] == "/C:/Users/me/100%.png"

    def test_posix_path_keeps_leading_slash(self, streaming_results):
        r = streaming_results["file:///home/me/x.png"]
        assert r["pathParam"] == "/home/me/x.png"

    def test_unc_authority_explicitly_rejected(self, streaming_results):
        r = streaming_results["file://server/share/x.png"]
        assert not r["hasMediaRoute"], "UNC must never be rewired to api/media"
        assert r["result"] == "file://server/share/x.png"  # left as inert file:// href

    def test_malformed_authority_fails_closed(self, streaming_results):
        """A file URI whose authority makes URL parsing throw must not fall
        through the catch path into a local media URL (AGENTS.md fail-closed)."""
        r = streaming_results["file://server:abc/share/x.png"]
        assert not r["hasMediaRoute"], (
            "a malformed authority must be rejected, not rewired to api/media"
        )
        assert r["result"] == "file://server:abc/share/x.png"

    def test_localhost_authority_treated_as_local(self, streaming_results):
        r = streaming_results["file://localhost/C:/x.png"]
        assert r["pathParam"] == "/C:/x.png"

    def test_non_image_drive_letter_path_forwards_leading_separator(self, streaming_results):
        r = streaming_results["file:///C:/Users/me/report.md"]
        assert r["pathParam"] == "/C:/Users/me/report.md"

    def test_streaming_and_settled_policies_match(self, settled_results, streaming_results):
        """② UNC policy must be identical across both converters."""
        for uri in ("file://server/share/x.png", "file://localhost/C:/x.png"):
            s = settled_results[uri]
            t = streaming_results[uri]
            assert s["hasMediaRoute"] == t["hasMediaRoute"], uri
