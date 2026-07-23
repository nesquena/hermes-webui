"""Regression coverage for public-share local media isolation (#6126)."""

import base64
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest


OMITTED_ATTACHMENT = "[*Local attachment omitted from public share*]"
PNG_DATA_URI = "data:image/png;base64,iVBORw0KGgo="


def _render_md_with_node(raw: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for renderMd parity coverage")

    ui_path = Path(__file__).resolve().parents[1] / "static" / "ui.js"
    script = r"""
const fs = require('fs');
const vm = require('vm');
const uiPath = process.argv[1];
const raw = JSON.parse(fs.readFileSync(0, 'utf8'));
const noop = () => {};
const classList = () => ({add: noop, remove: noop, toggle: noop, contains: () => false});
const element = (tag = 'div') => ({
  tagName: String(tag).toUpperCase(),
  style: {setProperty: noop, removeProperty: noop},
  classList: classList(),
  dataset: {},
  children: [],
  appendChild: noop,
  remove: noop,
  setAttribute: noop,
  removeAttribute: noop,
  addEventListener: noop,
  removeEventListener: noop,
  querySelector: () => null,
  querySelectorAll: () => [],
  innerHTML: '',
  textContent: '',
  value: '',
  disabled: false,
  hidden: false,
});
const storage = {getItem: () => null, setItem: noop, removeItem: noop, clear: noop};
const document = {
  baseURI: 'http://localhost/',
  body: element('body'),
  documentElement: element('html'),
  createElement: element,
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: noop,
  removeEventListener: noop,
};
const window = {
  __HERMES_CONFIG__: {},
  document,
  navigator: {onLine: true},
  location: {href: 'http://localhost/'},
  localStorage: storage,
  sessionStorage: storage,
  addEventListener: noop,
  removeEventListener: noop,
  matchMedia: () => ({matches: false, addEventListener: noop, removeEventListener: noop}),
  fetch: () => Promise.reject(new Error('unused')),
  requestAnimationFrame: cb => setTimeout(cb, 0),
  cancelAnimationFrame: clearTimeout,
  _botName: 'Hermes',
};
const matchMedia = window.matchMedia;
const context = {
  console,
  window,
  document,
  navigator: window.navigator,
  location: window.location,
  localStorage: storage,
  sessionStorage: storage,
  URL,
  URLSearchParams,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame: window.requestAnimationFrame,
  cancelAnimationFrame: window.cancelAnimationFrame,
  matchMedia,
  MutationObserver: class { observe() {} disconnect() {} },
  ResizeObserver: class { observe() {} disconnect() {} },
  IntersectionObserver: class { observe() {} disconnect() {} },
  Math,
  Date,
  JSON,
  RegExp,
  String,
  Number,
  Boolean,
  Array,
  Object,
  Promise,
  Error,
  encodeURIComponent,
  decodeURIComponent,
  encodeURI,
  decodeURI,
  __raw: raw,
};
context.globalThis = context;
window.window = window;
window.globalThis = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(uiPath, 'utf8'), context, {filename: uiPath});
vm.runInContext(
  "_inlineMediaHtmlForRef = ref => `__MEDIA_REF__${ref}__`; __rendered = renderMd(__raw);",
  context
);
process.stdout.write(String(context.__rendered));
"""
    result = subprocess.run(
        [node, "-e", script, str(ui_path)],
        input=json.dumps(raw),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        pytest.fail(result.stderr or result.stdout)
    return result.stdout


def test_public_share_snapshot_omits_local_media_references(monkeypatch):
    import api.shares as shares

    monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://hermes.example.test")

    class Session:
        pass

    session = Session()
    session.title = "Local media share"
    session.workspace = "/private/workspace"
    session.messages = [
        {"role": "user", "content": "Show the generated files."},
        {
            "role": "assistant",
            "content": (
                "Unix MEDIA:/private/workspace/output.png\n"
                "File URI MEDIA:file:///tmp/report.pdf\n"
                "Windows MEDIA:C:\\Users\\alice\\result.png\n"
                "Bare file:///tmp/data.csv\n"
                "Markdown [report](file:///tmp/report.pdf)\n"
                "Image ![chart](file:///tmp/chart.png)\n"
                "Autolink <file:///tmp/log.txt>\n"
                "Loopback MEDIA:http://localhost:8787/api/media?path=/tmp/loopback.png\n"
                "Private MEDIA:http://192.168.1.20/internal.png\n"
                "Authenticated MEDIA:https://hermes.example.test/api/media?path=/tmp/private.png\n"
                "Media subpath MEDIA:https://hermes.example.test/app/api/media/download?id=private\n"
                "Encoded MEDIA:https://hermes.example.test/app/%61pi/media?path=/tmp/private.png\n"
                "Wildcard dot MEDIA:https://127.0.0.1.nip.io/internal.png\n"
                "Wildcard dash MEDIA:https://app.192-168-1-20.sslip.io/internal.png\n"
                "Public MEDIA:https://cdn.example.test/image.png"
            ),
        },
    ]

    snapshot = shares.build_share_snapshot(session)
    content = snapshot["messages"][1]["content"]

    assert content.count(OMITTED_ATTACHMENT) == 14
    assert "file://" not in content
    assert "MEDIA:/" not in content
    assert "MEDIA:C:" not in content
    assert "localhost:8787" not in content
    assert "192.168.1.20" not in content
    assert "hermes.example.test/api/media" not in content
    assert "/api/media/download" not in content
    assert "hermes.example.test/app/api/media" not in content
    assert "hermes.example.test/app/%61pi/media" not in content
    assert "127.0.0.1.nip.io" not in content
    assert "app.192-168-1-20.sslip.io" not in content
    assert "/private/workspace" not in content
    assert "MEDIA:https://cdn.example.test/image.png" in content


def test_public_share_snapshot_omits_browser_normalized_private_media_urls(monkeypatch):
    import api.shares as shares

    monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://hermes.example.test")

    class Session:
        pass

    private_refs = [
        ("abbreviated loopback", "http://127.1/private.png"),
        ("three-part loopback", "http://127.0.1/private.png"),
        ("octal loopback", "http://0177.0.0.1/private.png"),
        ("padded octal loopback", "http://127.00.00.01/private.png"),
        ("hex loopback", "http://0x7f.1/private.png"),
        ("abbreviated private", "http://10.1/private.png"),
        ("ipv6 loopback", "http://[::1]/private.png"),
        ("ipv4-mapped private ipv6", "http://[::ffff:192.168.1.1]/private.png"),
        ("ipv4-mapped loopback ipv6", "http://[::ffff:127.0.0.1]/private.png"),
        ("invalid bracketed host", "http://[v1.1]/private.png"),
        ("percent encoded loopback host", "http://%31%32%37.0.0.1/private.png"),
        ("invalid percent encoded host", "http://%ff/private.png"),
        ("fullwidth loopback host", "http://１２７.０.０.１/private.png"),
        ("backslash media path", "https://hermes.example.test\\api\\media?path=/tmp/private.png"),
        (
            "dot segment media path",
            "https://hermes.example.test/api/foo/../media?path=/tmp/private.png",
        ),
        (
            "double encoded media path",
            "https://hermes.example.test/app/%2561pi/media?path=/tmp/private.png",
        ),
    ]

    session = Session()
    session.title = "Adversarial media share"
    session.workspace = "/private/workspace"
    session.messages = [
        {
            "role": "assistant",
            "content": "\n".join(f"{label} MEDIA:{ref}" for label, ref in private_refs),
        },
    ]

    snapshot = shares.build_share_snapshot(session)
    content = snapshot["messages"][0]["content"]

    assert content.count(OMITTED_ATTACHMENT) == len(private_refs)
    for _label, ref in private_refs:
        assert ref not in content
    assert "127.1" not in content
    assert "0177.0.0.1" not in content
    assert "0x7f.1" not in content
    assert "%31%32%37.0.0.1" not in content
    assert "%ff" not in content
    assert "１２７.０.０.１" not in content
    assert "[::1]" not in content
    assert "::ffff:192.168.1.1" not in content
    assert "::ffff:127.0.0.1" not in content
    assert "[v1.1]" not in content
    assert "\\api\\media" not in content
    assert "/api/foo/../media" not in content
    assert "%2561pi" not in content


def test_public_share_snapshot_preserves_inert_file_uri_code_regions():
    import api.shares as shares

    session = SimpleNamespace(
        title="Code literal share",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "Inline `file:///fixture-not-redacted/example.txt` stays code.\n"
                    "```text\n"
                    "file:///fixture-not-redacted/fenced.txt\n"
                    "```\n"
                    "<pre>file:///fixture-not-redacted/raw-pre.txt</pre>\n"
                    "Bare file:///fixture-not-redacted/bare.txt"
                ),
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert "`file:///fixture-not-redacted/example.txt`" in content
    assert "```text\nfile:///fixture-not-redacted/fenced.txt\n```" in content
    assert "<pre>file:///fixture-not-redacted/raw-pre.txt</pre>" in content
    assert f"Bare {OMITTED_ATTACHMENT}" in content
    assert content.count(OMITTED_ATTACHMENT) == 1


def test_public_share_snapshot_preserves_renderer_inert_file_uri_parser_differentials():
    import api.shares as shares

    crlf_fence = "```text\r\nfile:///fixture-not-redacted/crlf.txt\r\n```\r\nAfter"
    entity_pre = "&lt;pre&gt;file:///fixture-not-redacted/entity.txt&lt;/pre&gt;"
    blockquote_fence = (
        "> ```text\n"
        "> file:///fixture-not-redacted/quoted.txt\n"
        "> ```"
    )
    session = SimpleNamespace(
        title="Parser differential code share",
        messages=[
            {
                "role": "assistant",
                "content": f"{blockquote_fence}\n{crlf_fence}\n{entity_pre}",
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert blockquote_fence in content
    assert crlf_fence in content
    assert entity_pre in content
    assert content.count(OMITTED_ATTACHMENT) == 0


def test_public_share_snapshot_preserves_nested_and_entity_blockquote_fence_literals():
    import api.shares as shares

    nested_blockquote_fence = (
        ">> ```text\n"
        ">> file:///fixture-not-redacted/nested-quote.txt\n"
        ">> ```"
    )
    entity_blockquote_fence = (
        "&gt; ```text\n"
        "&gt; file:///fixture-not-redacted/entity-quote.txt\n"
        "&gt; ```"
    )
    mixed_blockquote_fence = (
        ">&gt; ```text\n"
        ">&gt; file:///fixture-not-redacted/mixed-quote.txt\n"
        ">&gt; ```"
    )
    valid_entity_pre = "&lt;pre&gt;file:///fixture-not-redacted/valid-pre.txt&lt;/pre&gt;"
    valid_upper_entity_pre = (
        "&lt;PRE&gt; file:///fixture-not-redacted/upper-pre.txt &lt;/PRE&gt;"
    )
    session = SimpleNamespace(
        title="Nested blockquote share",
        messages=[
            {
                "role": "assistant",
                "content": (
                    f"{nested_blockquote_fence}\n"
                    f"{entity_blockquote_fence}\n"
                    f"{mixed_blockquote_fence}\n"
                    f"{valid_entity_pre}\n"
                    f"{valid_upper_entity_pre}"
                ),
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert nested_blockquote_fence in content
    assert entity_blockquote_fence in content
    assert mixed_blockquote_fence in content
    assert valid_entity_pre in content
    assert valid_upper_entity_pre in content
    assert content.count(OMITTED_ATTACHMENT) == 0


def test_public_share_snapshot_omits_mixed_depth_blockquote_fence_file_uris():
    import api.shares as shares

    shallower_payload = (
        ">> ```text\n"
        "> file:///private-not-known/depth-mismatch.png\n"
        ">> ```"
    )
    shallower_closer = (
        ">> ```text\n"
        ">> file:///private-not-known/closer-mismatch.png\n"
        "> ```"
    )
    deeper_closer = (
        "> ```text\n"
        "> file:///private-not-known/deeper-close-leak.png\n"
        ">> ```"
    )
    unmatched_outer = (
        "```text\n"
        "> ```text\n"
        "> file:///private-not-known/shadowed-by-outer.png\n"
        "> ```"
    )
    mixed_crlf_deeper_closer = (
        "&gt; ```text\r\n"
        "&gt; file:///private-not-known/mixed-crlf.png\r\n"
        ">&gt; ```"
    )
    lone_cr_deeper_closer = (
        "> ```text\r"
        "> file:///private-not-known/lone-cr.png\r"
        ">> ```"
    )
    session = SimpleNamespace(
        title="Title >> ```text\n> file:///private-not-known/title-depth.png\n>> ```",
        messages=[
            {
                "role": "assistant",
                "content": (
                    f"{shallower_payload}\n"
                    f"{shallower_closer}\n"
                    f"{deeper_closer}\n"
                    f"{unmatched_outer}\n"
                    f"{mixed_crlf_deeper_closer}\n"
                    f"{lone_cr_deeper_closer}"
                ),
            }
        ],
    )

    snapshot = shares.build_share_snapshot(session)
    content = snapshot["messages"][0]["content"]
    title = snapshot["title"]

    assert "file://" not in content
    assert "private-not-known" not in content
    assert "depth-mismatch" not in content
    assert "closer-mismatch" not in content
    assert "deeper-close-leak" not in content
    assert "shadowed-by-outer" not in content
    assert "mixed-crlf" not in content
    assert "lone-cr" not in content
    assert f"> {OMITTED_ATTACHMENT}" in content
    assert f">> {OMITTED_ATTACHMENT}" in content
    assert content.count(OMITTED_ATTACHMENT) == 6
    assert "file://" not in title
    assert "private-not-known" not in title
    assert "title-depth" not in title
    assert title.count(OMITTED_ATTACHMENT) == 1


def test_public_share_blockquote_stashing_matches_render_md_file_activation():
    active_shapes = [
        (
            "> ```text\n"
            "> file:///private-not-known/deeper-close-oracle.png\n"
            ">> ```"
        ),
        (
            "```text\n"
            "> ```text\n"
            "> file:///private-not-known/shadowed-oracle.png\n"
            "> ```"
        ),
        (
            "&gt; ```text\r\n"
            "&gt; file:///private-not-known/entity-crlf-oracle.png\r\n"
            ">&gt; ```"
        ),
        (
            "> ```text\r"
            "> file:///private-not-known/lone-cr-oracle.png\r"
            ">> ```"
        ),
    ]
    inert_shape = (
        ">&gt; ```text\n"
        ">&gt; file:///fixture-not-redacted/mixed-inert-oracle.txt\n"
        ">&gt; ```"
    )

    for raw in active_shapes:
        rendered = _render_md_with_node(raw)
        assert "__MEDIA_REF__file:///private-not-known/" in rendered

    rendered = _render_md_with_node(inert_shape)
    assert "__MEDIA_REF__" not in rendered
    assert "file:///fixture-not-redacted/mixed-inert-oracle.txt" in rendered


def test_public_share_snapshot_omits_parser_divergent_active_file_uri_shapes():
    import api.shares as shares

    session = SimpleNamespace(
        title="Title `label\rfile:///private-not-known/title-cr-leak.png`",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "Inline `label\rfile:///private-not-known/message-cr-leak.png`\n"
                    "Malformed &lt;pre file:///private-not-known/malformed-pre.txt&lt;/pre&gt;\n"
                    "Case &LT;pre&gt; file:///private-not-known/case-pre.txt &LT;/pre&gt;"
                ),
            }
        ],
    )

    snapshot = shares.build_share_snapshot(session)
    content = snapshot["messages"][0]["content"]
    title = snapshot["title"]

    assert "file://" not in content
    assert "private-not-known" not in content
    assert "message-cr-leak" not in content
    assert "malformed-pre" not in content
    assert "case-pre" not in content
    assert content.count(OMITTED_ATTACHMENT) == 3
    assert "file://" not in title
    assert "private-not-known" not in title
    assert "title-cr-leak" not in title
    assert title.count(OMITTED_ATTACHMENT) == 1


def test_public_share_snapshot_redacts_known_paths_inside_code_regions():
    import api.shares as shares

    session = SimpleNamespace(
        title="Known path share",
        workspace="/sensitive/workspace",
        messages=[
            {
                "role": "assistant",
                "content": "Inline `file:///sensitive/workspace/example.txt` stays inert.",
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert "`file://[redacted-path]/example.txt`" in content
    assert "/sensitive/workspace" not in content
    assert content.count(OMITTED_ATTACHMENT) == 0


def test_public_share_snapshot_omits_media_file_tokens_before_code_protection():
    import api.shares as shares

    session = SimpleNamespace(
        title="Code media share",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "Inline `MEDIA:file:///private-not-known/inline.png`.\n"
                    "```text\n"
                    "MEDIA:file:///private-not-known/fenced.png\n"
                    "```\n"
                    "<pre>MEDIA:file:///private-not-known/raw-pre.png</pre>"
                ),
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert f"`{OMITTED_ATTACHMENT}`" in content
    assert f"```text\n{OMITTED_ATTACHMENT}\n```" in content
    assert f"<pre>{OMITTED_ATTACHMENT}</pre>" in content
    assert "MEDIA:file://" not in content
    assert "private-not-known" not in content
    assert content.count(OMITTED_ATTACHMENT) == 3


def test_public_share_snapshot_preserves_safe_data_images_only():
    import api.shares as shares

    session = SimpleNamespace(
        title="Data image share",
        messages=[
            {
                "role": "assistant",
                "content": (
                    f"Safe MEDIA:{PNG_DATA_URI}\n"
                    "Unsafe MEDIA:data:text/html;base64,PHNjcmlwdD48L3NjcmlwdD4="
                ),
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert f"MEDIA:{PNG_DATA_URI}" in content
    assert "data:text/html" not in content
    assert content.count(OMITTED_ATTACHMENT) == 1


def test_public_share_snapshot_omits_text_bearing_svg_data_images():
    import api.shares as shares

    sentinel = "private-note-7391"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        f"<text>{sentinel}</text>"
        "</svg>"
    )
    svg_data_uri = "data:image/svg+xml;base64," + base64.b64encode(
        svg.encode("utf-8")
    ).decode("ascii")
    session = SimpleNamespace(
        title="SVG data share",
        messages=[
            {
                "role": "assistant",
                "content": f"SVG MEDIA:{svg_data_uri}",
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    if "data:image/svg+xml;base64," in content:
        encoded = content.split("data:image/svg+xml;base64,", 1)[1].split()[0]
        decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
        assert sentinel not in decoded
    assert sentinel not in content
    assert svg_data_uri not in content
    assert content == f"SVG {OMITTED_ATTACHMENT}"


def test_public_share_snapshot_scopes_api_media_path_to_trusted_same_origin(monkeypatch):
    import api.shares as shares

    monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://hermes.example.test")

    session = SimpleNamespace(
        title="API media path share",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "External MEDIA:https://cdn.example.test/api/media/image.png\n"
                    "Same-origin MEDIA:https://hermes.example.test/api/media?path=/tmp/private.png\n"
                    "Relative MEDIA:/api/media?path=/tmp/private.png"
                ),
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert "MEDIA:https://cdn.example.test/api/media/image.png" in content
    assert "hermes.example.test/api/media" not in content
    assert "MEDIA:/api/media" not in content
    assert content.count(OMITTED_ATTACHMENT) == 2


def test_public_share_snapshot_preserves_unrelated_scheme_and_public_media_file_query():
    import api.shares as shares

    public_media = "MEDIA:https://cdn.example.test/image.png?source=file:///label"
    session = SimpleNamespace(
        title="Approved public media share",
        messages=[
            {
                "role": "assistant",
                "content": f"profile://alice/settings\n{public_media}",
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert "profile://alice/settings" in content
    assert public_media in content
    assert content.count(OMITTED_ATTACHMENT) == 0


def test_public_share_snapshot_bracketed_local_media_has_clean_placeholder():
    import api.shares as shares

    session = SimpleNamespace(
        title="Bracket media share",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "[MEDIA:/tmp/private.png]\n"
                    "[MEDIA:file:///tmp/private.png]\n"
                    "Public [MEDIA:https://cdn.example.test/image.png]"
                ),
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]
    lines = content.splitlines()

    assert lines[0] == OMITTED_ATTACHMENT
    assert lines[1] == OMITTED_ATTACHMENT
    assert "Public [MEDIA:https://cdn.example.test/image.png]" in content
    assert "[[" not in content
    assert content.count(OMITTED_ATTACHMENT) == 2


def test_public_share_snapshot_fails_closed_on_bracket_wrapped_ipv6_media():
    import api.shares as shares

    session = SimpleNamespace(
        title="IPv6 media share",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "Public [MEDIA:https://[2001:4860:4860::8888]/x.png]\n"
                    "Private [MEDIA:http://[::1]/private.png]\n"
                    "Mapped private [MEDIA:http://[::ffff:192.168.1.1]/mapped.png]"
                ),
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]
    lines = content.splitlines()

    assert lines[0] == f"Public {OMITTED_ATTACHMENT}"
    assert lines[1] == f"Private {OMITTED_ATTACHMENT}"
    assert lines[2] == f"Mapped private {OMITTED_ATTACHMENT}"
    assert "2001:4860:4860::8888" not in content
    assert "/x.png]" not in content
    assert "/private.png]" not in content
    assert "/mapped.png]" not in content
    assert content.count(OMITTED_ATTACHMENT) == 3


def test_public_share_snapshot_does_not_absorb_nested_local_media_inside_public_url():
    import api.shares as shares

    session = SimpleNamespace(
        title="Nested media share",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "Outer MEDIA:https://cdn.example.test/x"
                    "[MEDIA:file:///private-not-known/secret.png]"
                ),
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert "MEDIA:https://cdn.example.test/x" in content
    assert "file://" not in content
    assert "private-not-known" not in content
    assert content.count(OMITTED_ATTACHMENT) == 1


def test_public_share_snapshot_splits_adjacent_nested_media_inside_code_regions():
    import api.shares as shares

    session = SimpleNamespace(
        title="Adjacent media share",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "`MEDIA:https://cdn.example.test/x"
                    "MEDIA:file:///private-not-known/watch-secret.png`\n"
                    "```text\n"
                    "MEDIA:https://cdn.example.test/path?next="
                    "MEDIA:file:%2F%2Fprivate-not-known/encoded.png\n"
                    "```\n"
                    "<pre>MEDIA:https://cdn.example.test/raw"
                    "MEDIA:/private-not-known/raw.png</pre>"
                ),
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert f"`MEDIA:https://cdn.example.test/x{OMITTED_ATTACHMENT}`" in content
    assert (
        f"```text\nMEDIA:https://cdn.example.test/path?next={OMITTED_ATTACHMENT}\n```"
        in content
    )
    assert f"<pre>MEDIA:https://cdn.example.test/raw{OMITTED_ATTACHMENT}</pre>" in content
    assert "MEDIA:file://" not in content
    assert "MEDIA:file:%2F%2F" not in content
    assert "MEDIA:/private-not-known" not in content
    assert "private-not-known" not in content
    assert "watch-secret" not in content
    assert "encoded.png" not in content
    assert "raw.png" not in content
    assert content.count(OMITTED_ATTACHMENT) == 3


def test_public_share_snapshot_matches_renderer_code_fence_grammar():
    import api.shares as shares

    session = SimpleNamespace(
        title="Fence parity share",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "~~~text\n"
                    "file:///private-not-known/tilde.png\n"
                    "~~~\n"
                    "```bad`info\n"
                    "file:///private-not-known/info.png\n"
                    "```"
                ),
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert f"~~~text\n{OMITTED_ATTACHMENT}\n~~~" in content
    assert f"```bad`info\n{OMITTED_ATTACHMENT}\n```" in content
    assert "private-not-known" not in content
    assert content.count(OMITTED_ATTACHMENT) == 2


def test_public_share_snapshot_fails_closed_before_renderer_sees_bracketed_ipv6_media():
    import api.shares as shares

    share_js = (Path(__file__).resolve().parents[1] / "static" / "share.js").read_text(
        encoding="utf-8"
    )
    ui_js = (Path(__file__).resolve().parents[1] / "static" / "ui.js").read_text(
        encoding="utf-8"
    )
    assert "renderMd(String(msg.content||''))" in share_js
    assert r"MEDIA:([^\s\)\]]+)" in ui_js

    session = SimpleNamespace(
        title="Renderer parity share",
        messages=[
            {
                "role": "assistant",
                "content": "Public [MEDIA:https://[2001:4860:4860::8888]/x.png]",
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert content == f"Public {OMITTED_ATTACHMENT}"
    assert "MEDIA:https://[" not in content
    assert "2001:4860:4860::8888" not in content


def test_public_share_snapshot_malformed_port_fails_closed():
    import api.shares as shares

    assert shares._is_public_media_url("https://public.example.test:99999/api/media/x.png") is False

    session = SimpleNamespace(
        title="Bad port share",
        messages=[
            {
                "role": "assistant",
                "content": "Bad MEDIA:https://public.example.test:99999/api/media/x.png",
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert content == f"Bad {OMITTED_ATTACHMENT}"


def test_public_media_url_preserves_global_browser_ipv4_literals():
    import api.shares as shares

    assert str(shares._parse_browser_ipv4_literal("8.8")) == "8.0.0.8"
    assert shares._is_public_media_url("http://8.8/x.png") is True
    assert shares._is_public_media_url("http://10.1/private.png") is False

    session = SimpleNamespace(
        title="Legacy IPv4 share",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "Global MEDIA:http://8.8/x.png\n"
                    "Private MEDIA:http://10.1/private.png"
                ),
            }
        ],
    )

    content = shares.build_share_snapshot(session)["messages"][0]["content"]

    assert "MEDIA:http://8.8/x.png" in content
    assert "10.1/private.png" not in content
    assert content.count(OMITTED_ATTACHMENT) == 1


def test_public_media_url_rejects_browser_invalid_bracketed_hosts_like_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for WHATWG URL parity coverage")

    import api.shares as shares

    refs = [
        "http://[v1.1]/x.png",
        "http://user@[v1.1]/x.png",
        "http://[garbage]/x.png",
    ]
    result = subprocess.run(
        [
            node,
            "-e",
            (
                "for (const ref of process.argv.slice(1)) {"
                "try { new URL(ref); console.log('ok'); }"
                "catch (_) { console.log('error'); }"
                "}"
            ),
            *refs,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["error", "error", "error"]
    for ref in refs:
        assert shares._is_public_media_url(ref) is False


def test_public_share_snapshot_sanitizes_title_media_references():
    import api.shares as shares

    class Session:
        pass

    local_title_session = Session()
    local_title_session.title = "MEDIA:file:///tmp/title-secret.png"
    local_title_session.messages = [{"role": "assistant", "content": "shareable"}]

    local_snapshot = shares.build_share_snapshot(local_title_session)

    assert local_snapshot["title"] == OMITTED_ATTACHMENT
    assert "file://" not in local_snapshot["title"]
    assert "/tmp/title-secret.png" not in local_snapshot["title"]

    public_title_session = Session()
    public_title_session.title = "Title MEDIA:https://cdn.example.test/title.png"
    public_title_session.messages = [{"role": "assistant", "content": "shareable"}]

    public_snapshot = shares.build_share_snapshot(public_title_session)

    assert public_snapshot["title"] == "Title MEDIA:https://cdn.example.test/title.png"


def test_public_share_snapshot_splits_adjacent_nested_media_in_title():
    import api.shares as shares

    session = SimpleNamespace(
        title=(
            "Title MEDIA:https://cdn.example.test/x"
            "MEDIA:/private-not-known/watch-title.png "
            "Query MEDIA:https://cdn.example.test/q?next="
            "MEDIA:file:%2F%2Fprivate-not-known/title-encoded.png"
        ),
        messages=[{"role": "assistant", "content": "shareable"}],
    )

    title = shares.build_share_snapshot(session)["title"]

    assert "MEDIA:https://cdn.example.test/x" in title
    assert "MEDIA:https://cdn.example.test/q?next=" in title
    assert "MEDIA:/private-not-known" not in title
    assert "MEDIA:file:%2F%2F" not in title
    assert "private-not-known" not in title
    assert "watch-title" not in title
    assert "title-encoded" not in title
    assert title.count(OMITTED_ATTACHMENT) == 2


def test_authenticated_media_route_stays_private(monkeypatch):
    import api.auth as auth

    assert "/api/media" not in auth.PUBLIC_PATHS

    class Handler:
        def __init__(self):
            self.status = None
            self.headers = []
            self.wfile = BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.headers.append((name, value))

        def end_headers(self):
            pass

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: None)
    monkeypatch.setattr(auth, "ensure_trusted_auth_session", lambda _handler: None)

    handler = Handler()
    allowed = auth.check_auth(
        handler,
        SimpleNamespace(path="/api/media", query="path=%2Fprivate%2Foutput.png"),
    )

    assert allowed is False
    assert handler.status == 401
    assert b"Authentication required" in handler.wfile.getvalue()
