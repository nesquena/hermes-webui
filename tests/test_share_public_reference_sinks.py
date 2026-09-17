"""
Public-share URL sinks: ONE fail-closed decision must cover EVERY URL-bearing
token in message content, not only canonical ``MEDIA:`` tokens.

``api/shares.py::_embed_share_media()`` used to substitute only the ``MEDIA:``
pattern, so its whole-reference privacy guard never inspected the other shapes
that reach the public share page. ``static/share.html`` loads ``static/ui.js``
and ``static/share.js::_shareRenderMessages()`` calls the real ``renderMd()``,
whose sinks turn message text into a LIVE URL:

* ``_mdImageHtml()`` routes a bare Markdown ``file://`` image through
  ``_inlineMediaHtmlForRef()``, which emits ``api/media?path=…``.
* ``_markdownHref()`` converts an ordinary Markdown ``file://`` link into
  ``api/media?path=…&inline=1``.
* an http(s) Markdown image is restored as a live ``<img src>``.
* the autolink pass turns a bare http(s) run into ``<a href>``.

Every one of those makes an anonymous viewer's browser issue an authenticated
same-origin request against our own ``/api/media`` route, or round-trips a host
filesystem path into a public snapshot.

Test discipline in this file:

* the REAL ``_embed_share_media()`` runs on every input — no mirrored oracle.
* the composed matrix runs the REAL ``renderMd()`` from ``static/ui.js`` in
  node over the ACTUAL published snapshot text, then asserts on the final sink
  markup. Publication and rendering are chained exactly as production chains
  them.
* positive controls assert byte-for-byte preservation of genuine public assets,
  because over-blocking is also a failure.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import textwrap

import pytest

from api.shares import _PLACEHOLDER, _embed_share_media

REPO = pathlib.Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

NODE = shutil.which("node")


# ── Real renderMd() driver (same brace-depth extraction the repo already uses) ─

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


_RENDER_PRELUDE = textwrap.dedent(
    r"""
    const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
    const _PDF_EXTS=/\.pdf$/i;
    const _SVG_EXTS=/\.svg$/i;
    const _HTML_EXTS=/\.html?$/i;
    const _CSV_EXTS=/\.csv$/i;
    const _EXCALIDRAW_EXTS=/\.excalidraw$/i;
    const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm|oga)$/i;
    const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
    function t(k){ return k; }
    function li(){ return ''; }
    function _mediaPlayerHtml(kind,src){ return `<${kind} src="${esc(src)}"></${kind}>`; }
    function _dataImageHtml(){ return ''; }
    function _isSafeDataImageUri(){ return false; }
    function _mediaKindForName(n){
      const s=String(n||'');
      if(_IMAGE_EXTS.test(s)) return 'image';
      if(_AUDIO_EXTS.test(s)) return 'audio';
      if(_VIDEO_EXTS.test(s)) return 'video';
      return 'file';
    }
    // The share page is served from the share origin, which is what makes a
    // loopback rewrite dangerous there.
    global.window={};
    global.document={baseURI:'https://share.example.test/share/abc123'};
    """
)

_RENDER_FUNCS = (
    "_matchBacktickFenceLine",
    "_isBacktickFenceClose",
    "_mediaPathSrc",
    "_mediaTokenRe",
    "_unquoteMediaRef",
    "_mediaTokenMaxLength",
    "_mediaTokenExceedsMaxLength",
    "_isExternalMediaUrl",
    "_localTargetMarkers",
    "_decodeUrlComponentBounded",
    "_externalMediaUrlHidesLocalTarget",
    "_mdImageHtml",
    "_inlineMediaHtmlForRef",
    "renderMd",
)


def _render_md_many(inputs: list[str]) -> list[str]:
    """Run the REAL renderMd() from static/ui.js over each input, in node."""
    js = _RENDER_PRELUDE
    for name in _RENDER_FUNCS:
        js += "\n" + _extract_function(UI_JS, name)
    js += textwrap.dedent(
        r"""
        const cases=JSON.parse(process.argv[1]);
        process.stdout.write(JSON.stringify(cases.map(c=>renderMd(c))));
        """
    )
    proc = subprocess.run(
        ["node", "-e", js, json.dumps(inputs)],
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    return json.loads(proc.stdout)


# ── The attack matrix ────────────────────────────────────────────────────────
# Every row is a shape a client sink would turn into a live local/authenticated
# URL. `_embed_share_media` must reduce each to exactly one placeholder, and the
# rendered snapshot must carry no live local reference.
_SECRET = "/etc/shadow.png"

ATTACK_CASES = [
    # (label, message content)
    ("canonical MEDIA token", f"look MEDIA:{_SECRET} here"),
    ("bare file:// prose", f"file://{_SECRET}"),
    ("outer md image file://", f"![x](file://{_SECRET})"),
    ("outer md link file://", f"[x](file://{_SECRET})"),
    ("relative api/media image", f"![x](/api/media?path={_SECRET})"),
    ("relative api/media no slash", f"![x](api/media?path={_SECRET})"),
    ("loopback api/media image", f"![x](http://127.0.0.1:8080/api/media?path={_SECRET})"),
    ("loopback api/media bare", f"http://127.0.0.1:8080/api/media?path={_SECRET}"),
    ("cdn host reaching api/media", f"![x](https://cdn.test/api/media?path={_SECRET})"),
    ("nested MEDIA in query", f"![x](https://cdn.test/a.png?src=MEDIA:{_SECRET})"),
    ("nested MEDIA in fragment", f"![x](https://cdn.test/a.png#MEDIA:{_SECRET})"),
    ("nested file:// in query", f"![x](https://cdn.test/a.png?src=file://{_SECRET})"),
    ("nested loopback http:// start", "![x](https://cdn.test/a.png?u=http://127.0.0.1/x.png)"),
    ("nested relative api/media in query", f"![x](https://cdn.test/a.png?next=api/media?path={_SECRET})"),
    ("percent-encoded MEDIA", f"![x](https://cdn.test/a.png?src=%4dEDIA:{_SECRET})"),
    ("double-encoded MEDIA", f"![x](https://cdn.test/a.png?src=%254dEDIA:{_SECRET})"),
    ("residual encoding past the bound", "![x](https://cdn.test/a.png?t=%25252525254dEDIA:/etc/shadow)"),
    ("empty host fails closed", "![x](http:///nohost.png)"),
    ("list item consumer", f"- ![x](file://{_SECRET})"),
    ("nested list consumer", f"  - [x](file://{_SECRET})"),
    ("blockquote consumer", f"> ![x](file://{_SECRET})"),
    ("table cell consumer", f"| col |\n| --- |\n| ![x](file://{_SECRET}) |"),
    ("inline md link in list", f"- see [x](file://{_SECRET}) now"),
]

# Genuine public references. Every one must survive BYTE-FOR-BYTE: over-blocking
# a legitimate public image is a failure too.
PRESERVE_CASES = [
    ("public cdn image", "![ok](https://cdn.test/a.png)"),
    ("public cdn image with harmless query", "![ok](https://cdn.test/a.png?w=800&fmt=webp)"),
    ("public cdn link", "[ok](https://cdn.test/page?x=1&y=2)"),
    ("bare public url in prose", "see https://cdn.test/a.png for more"),
    ("external MEDIA token", "MEDIA:https://cdn.test/a.png"),
    ("public path that merely mentions media", "![ok](https://cdn.test/media/photo.png)"),
    ("harmless percent in query", "![ok](https://cdn.test/a.png?pct=100%25)"),
    ("query naming another public asset", "![ok](https://cdn.test/a.png?next=/images/other.png)"),
    ("query naming another public URL", "![ok](https://cdn.test/a.png?next=https://images.example.test/b.png)"),
]


def _published(text: str) -> str:
    """Publish *text* through the REAL sanitizer with NO allowed roots."""
    return _embed_share_media(text, allowed_roots=())


@pytest.mark.parametrize("label,content", ATTACK_CASES, ids=[c[0] for c in ATTACK_CASES])
def test_attack_shape_is_placeholdered_exactly_once(label, content):
    out = _published(content)
    assert out.count(_PLACEHOLDER) == 1, (
        f"{label}: expected exactly one placeholder, got {out!r}"
    )
    lowered = out.lower()
    for leak in ("file://", "api/media", "media:", "/etc/shadow", "127.0.0.1"):
        assert leak not in lowered, f"{label}: {leak!r} survived publication in {out!r}"


@pytest.mark.parametrize("label,content", PRESERVE_CASES, ids=[c[0] for c in PRESERVE_CASES])
def test_public_reference_is_preserved_byte_for_byte(label, content):
    assert _published(content) == content, f"{label}: public reference was altered"


def test_one_refusal_does_not_restart_inside_its_own_token():
    """A refused token must be consumed WHOLE — the previous blocker on this PR.

    ``MEDIA:https://cdn.test/img/MEDIA:/etc/passwd.png`` carries a nested
    keyword. If the scanner resumed inside the span it just refused, the nested
    ``MEDIA:`` would match as a fresh LOCAL token and produce a second
    placeholder plus a rewritten tail.
    """
    out = _published("MEDIA:https://cdn.test/img/MEDIA:/etc/passwd.png")
    assert out == _PLACEHOLDER, out
    assert out.count(_PLACEHOLDER) == 1, out


def test_local_image_inside_allowed_roots_still_embeds(tmp_path):
    """Positive control on the embed path: a legitimate local image still works."""
    png = tmp_path / "shot.png"
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    )
    out = _embed_share_media(f"before MEDIA:{png} after", allowed_roots=(tmp_path,))
    assert 'src="data:image/png;base64,' in out, out
    assert _PLACEHOLDER not in out, out
    assert str(png) not in out, "the concrete host path must not survive"


def test_multiple_tokens_in_one_message_each_get_one_decision():
    text = (
        f"![bad](file://{_SECRET}) and ![ok](https://cdn.test/a.png) "
        f"and [bad2](/api/media?path={_SECRET})"
    )
    out = _published(text)
    assert out.count(_PLACEHOLDER) == 2, out
    assert "![ok](https://cdn.test/a.png)" in out, out
    assert "file://" not in out and "api/media" not in out, out


# ── Composed matrix: publication → share.js → real renderMd() → final sink ───

@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_composed_pipeline_emits_no_live_local_reference():
    """Chain production: publish, then render with the REAL renderMd().

    ``share.js`` hands ``msg.content`` straight to ``renderMd()``, so rendering
    the PUBLISHED text is exactly what the anonymous viewer's browser does.
    """
    contents = [content for _label, content in ATTACK_CASES]
    published = [_published(c) for c in contents]
    rendered = _render_md_many(published)

    failures = []
    for (label, _content), pub, html in zip(ATTACK_CASES, published, rendered):
        lowered = html.lower()
        for leak in ("api/media", "file://", "media:", "/etc/shadow", "127.0.0.1"):
            if leak in lowered:
                failures.append(f"{label}: {leak!r} in rendered output: {html[:200]!r}")
        # No live element may carry a local or authenticated target.
        if 'src="api/media' in lowered or 'href="api/media' in lowered:
            failures.append(f"{label}: live api/media attribute: {html[:200]!r}")
        if _PLACEHOLDER not in pub:
            failures.append(f"{label}: publication left no placeholder: {pub!r}")
    assert not failures, "composed pipeline leaks:\n" + "\n".join(failures)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_composed_pipeline_still_renders_public_images():
    """The same chain must keep rendering a genuine public asset as an image."""
    published = [_published(c) for _l, c in PRESERVE_CASES]
    rendered = _render_md_many(published)
    by_label = {label: html for (label, _c), html in zip(PRESERVE_CASES, rendered)}

    # The renderer HTML-escapes `&` in an attribute value, so assert on the
    # escaped form the browser actually receives.
    img = by_label["public cdn image with harmless query"]
    assert 'src="https://cdn.test/a.png?w=800&amp;fmt=webp"' in img, img
    assert _PLACEHOLDER not in img, img

    link = by_label["public cdn link"]
    assert 'href="https://cdn.test/page?x=1&amp;y=2"' in link, link

    media_token = by_label["external MEDIA token"]
    assert 'src="https://cdn.test/a.png"' in media_token, media_token


# ── Decode bound: a value still changing at the bound must be REFUSED ────────

def test_decode_probe_reports_a_value_still_changing_at_the_bound():
    """``api.helpers._decode_url_component_bounded`` cannot express this.

    It returns only the decoded string, so a value still mutating at the bound
    is indistinguishable from one that decoded cleanly to something harmless,
    and the caller accepts it. The share boundary needs the verdict.
    """
    from api.share_refs import decode_probe_bounded

    settled, still_changing = decode_probe_bounded("MEDIA:/x")
    assert (settled, still_changing) == ("MEDIA:/x", False)

    _decoded, still_changing = decode_probe_bounded("%25252525254dEDIA:/etc/shadow")
    assert still_changing is True

    # A single literal percent is not "still changing".
    settled, still_changing = decode_probe_bounded("100%25")
    assert (settled, still_changing) == ("100%", False)


def test_nested_local_markers_are_a_superset_of_the_helper_markers():
    """The nested marker scan must not drift from the shared marker tuple.

    ``http://`` / ``https://`` are deliberately NOT markers: a nested absolute
    URL is a candidate to classify, not a verdict. See
    ``test_nested_public_url_is_preserved_and_nested_local_is_refused``.
    """
    from api.helpers import _LOCAL_TARGET_MARKERS
    from api.share_refs import _NESTED_LOCAL_MARKERS

    assert set(_LOCAL_TARGET_MARKERS) <= set(_NESTED_LOCAL_MARKERS)
    assert "api/media" in _NESTED_LOCAL_MARKERS
    for scheme in ("http://", "https://"):
        assert scheme not in _NESTED_LOCAL_MARKERS, (
            f"{scheme!r} as an unconditional marker refuses every public proxy URL"
        )


@pytest.mark.parametrize(
    "value,hides",
    [
        ("https://cdn.test/a.png", False),
        ("https://cdn.test/a.png?w=800&fmt=webp", False),
        ("https://cdn.test/media/photo.png", False),
        ("file:///etc/shadow", True),
        ("FILE:///etc/shadow", True),
        ("/api/media?path=/etc/shadow", True),
        ("api/media?path=/etc/shadow", True),
        ("http:///nohost.png", True),
        ("http://127.0.0.1:8080/img.png", True),
        # A nested PUBLIC URL is a public asset, not a local target. `evil.test`
        # is a public host: this row asserted True before, which locked in an
        # over-block that contradicted the module's preservation contract.
        ("https://cdn.test/a.png?u=https://evil.test/x", False),
        ("https://cdn.test/a.png#api/media?path=/x", True),
        ("mailto:someone@example.test", True),
        ("", True),
    ],
)
def test_public_reference_verdicts(value, hides):
    from api.share_refs import public_reference_hides_local_target

    assert public_reference_hides_local_target(value) is hides, value


# ── Nested absolute URLs: classify the candidate, never refuse its scheme ─────
# A nested absolute URL inside the outer URL's path, query, or fragment is a
# CANDIDATE. Refusing it for carrying a scheme token destroys every legitimate
# image-proxy / redirect URL, so each candidate is classified by the SAME rules
# instead. The matrix covers all three positions in both directions.

_NESTED_PRESERVE_CASES = [
    # (label, value) -- outer public URL carrying a HARMLESS PUBLIC nested URL.
    ("path public", "https://cdn.test/proxy/https://images.example.test/b.png"),
    ("query public", "https://cdn.test/a.png?next=https://images.example.test/b.png"),
    ("query public http", "https://cdn.test/a.png?next=http://images.example.test/b.png"),
    ("fragment public", "https://cdn.test/a.png#https://images.example.test/b.png"),
    ("two public candidates", "https://cdn.test/a.png?a=https://x.test/1.png&b=https://y.test/2.png"),
    ("public with harmless query of its own", "https://cdn.test/a.png?u=https://x.test/b.png?w=800"),
    ("public host merely named evil", "https://cdn.test/a.png?u=https://evil.test/x"),
    ("percent-encoded nested public", "https://cdn.test/a.png?u=https%3A%2F%2Fx.test%2Fb.png"),
]

_NESTED_REJECT_CASES = [
    # (label, value) -- the nested candidate is local, private, or our route.
    ("path file scheme", "https://cdn.test/proxy/file:///etc/passwd.png"),
    ("query file scheme", "https://cdn.test/a.png?next=file:///etc/passwd.png"),
    ("fragment file scheme", "https://cdn.test/a.png#file:///etc/passwd.png"),
    ("path loopback", "https://cdn.test/proxy/http://127.0.0.1/x.png"),
    ("query loopback api/media", "https://cdn.test/a.png?next=http://127.0.0.1/api/media?path=/etc/shadow"),
    ("fragment loopback", "https://cdn.test/a.png#http://127.0.0.1:8080/x.png"),
    ("query rfc1918", "https://cdn.test/a.png?next=http://192.168.1.5/x.png"),
    ("query link-local", "https://cdn.test/a.png?next=http://169.254.1.1/x.png"),
    ("query rfc4193", "https://cdn.test/a.png?next=http://[fd00::1]/x.png"),
    ("query localhost", "https://cdn.test/a.png?next=http://localhost:8787/x.png"),
    ("path relative api/media", "https://cdn.test/api/media?path=/etc/shadow"),
    ("query relative api/media", "https://cdn.test/a.png?next=api/media?path=/etc/shadow"),
    ("fragment nested MEDIA token", "https://cdn.test/a.png#MEDIA:/etc/shadow.png"),
    ("query nested MEDIA token", "https://cdn.test/a.png?src=MEDIA:/etc/shadow.png"),
    ("nested empty host", "https://cdn.test/a.png?next=http:///nohost.png"),
    ("public wrapper around private grandchild",
     "https://cdn.test/a.png?u=https://proxy.test/p?next=http://10.0.0.7/x.png"),
    ("percent-encoded nested loopback",
     "https://cdn.test/a.png?u=http%3A%2F%2F127.0.0.1%2Fx.png"),
]


@pytest.mark.parametrize(
    "label,value", _NESTED_PRESERVE_CASES, ids=[c[0] for c in _NESTED_PRESERVE_CASES]
)
def test_nested_public_candidate_is_preserved(label, value):
    """An outer public URL carrying a harmless public URL is a public asset."""
    from api.share_refs import public_reference_hides_local_target

    assert public_reference_hides_local_target(value) is False, label
    # And the whole token must survive publication byte-for-byte.
    assert _published(f"![ok]({value})") == f"![ok]({value})", label


@pytest.mark.parametrize(
    "label,value", _NESTED_REJECT_CASES, ids=[c[0] for c in _NESTED_REJECT_CASES]
)
def test_nested_local_or_private_candidate_is_refused(label, value):
    from api.share_refs import public_reference_hides_local_target

    assert public_reference_hides_local_target(value) is True, label
    assert _published(f"![x]({value})") == _PLACEHOLDER, label


def test_nested_classification_is_bounded_and_fails_closed_at_the_bound():
    """Recursion is bounded explicitly, and the bound REFUSES.

    Each nested candidate is a strict substring of its parent's probe, so the
    walk terminates on the value alone. The bound is the second guarantee, and
    a candidate we never examined must not be published.
    """
    from api.share_refs import _MAX_NESTED_URL_DEPTH, public_reference_hides_local_target

    def chain(levels: int) -> str:
        hops = "".join(f"https://h{i}.test/p/" for i in range(1, levels + 1))
        return f"https://h0.test/p/{hops}x.png"

    # Every hop is a public host, so anything within the bound is preserved.
    for levels in range(0, _MAX_NESTED_URL_DEPTH + 1):
        assert public_reference_hides_local_target(chain(levels)) is False, levels

    # One hop past the bound is refused even though every host is public.
    assert public_reference_hides_local_target(chain(_MAX_NESTED_URL_DEPTH + 1)) is True

    # A private host buried past the bound is refused too — the bound can only
    # make the answer stricter, never looser.
    deep_private = (
        "https://h0.test/p/https://h1.test/p/https://h2.test/p/"
        "https://h3.test/p/http://127.0.0.1/x.png"
    )
    assert public_reference_hides_local_target(deep_private) is True
