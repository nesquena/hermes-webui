"""Public shares must not publish inline ``data:`` URIs supplied by message content.

A public share snapshot republishes message text, and ``static/share.js``
renders it with ``renderMd``. Before this fix, a markdown image such as
``![x](data:image/png;base64,<payload>)``, a raw ``<img src="data:...">`` tag or a
``MEDIA:data:...`` token passed into the snapshot unchanged. The always-on
credential redactor cannot see inside base64 or percent-encoding, so a payload
like "valid PNG header + secret text" was published and recoverable from the
public share while the page showed only an image.

The fix replaces every content-supplied ``data:`` URI with a static placeholder
in ``_sanitize_message`` BEFORE the server-side local-media embed runs, so the
embed's own ``<img src="data:...">`` output (verified local image files, #6174)
is never placeholdered, and nothing in the content can pose as that output.
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

import pytest

from api import shares

PLACEHOLDER = "[*Inline attachment omitted from public share*]"
SENTINEL = "HIDDEN-SHARE-PAYLOAD-7f3a9c"
PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
# A real PNG header followed by secret text: passes a magic-byte check and is
# opaque to the credential redactor once base64-encoded.
PAYLOAD = PNG_HEADER + f" {SENTINEL} ".encode() * 4
PAYLOAD_B64 = base64.b64encode(PAYLOAD).decode("ascii")
SVG_B64 = base64.b64encode(
    f'<svg xmlns="http://www.w3.org/2000/svg"><text>{SENTINEL}</text></svg>'.encode()
).decode("ascii")
PCT_PAYLOAD = "%89PNG%0D%0A%1A%0A" + "".join(f"%{b:02X}" for b in SENTINEL.encode())

LOCAL_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
LOCAL_PNG_B64 = base64.b64encode(LOCAL_PNG).decode("ascii")


class _FakeSession:
    def __init__(self, tmp_path: Path, messages):
        self.session_id = "s-share-data-uri"
        self.title = "Share test"
        self.messages = messages
        self.workspace = str(tmp_path / "workspace")
        self.created_at = 0
        self.updated_at = 0


def _session(tmp_path: Path, content, *, role: str = "assistant") -> _FakeSession:
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    (ws / "ok.png").write_bytes(LOCAL_PNG)
    return _FakeSession(
        tmp_path,
        [
            {"role": "user", "content": "render it"},
            {"role": role, "content": content},
        ],
    )


def _snapshot(tmp_path: Path, content, *, role: str = "assistant") -> dict:
    return shares.build_share_snapshot(_session(tmp_path, content, role=role))


def _assert_no_payload(blob: str) -> None:
    assert SENTINEL not in blob
    assert PAYLOAD_B64 not in blob
    # No recoverable fragment of the payload either.
    assert PAYLOAD_B64[8:40] not in blob
    assert SVG_B64[8:40] not in blob
    assert PCT_PAYLOAD[16:48] not in blob


def _uri(kind: str = "png") -> str:
    return {
        "png": f"data:image/png;base64,{PAYLOAD_B64}",
        "upper": f"DATA:IMAGE/PNG;BASE64,{PAYLOAD_B64}",
        "mixed": f"Data:Image/Png;Base64,{PAYLOAD_B64}",
        "svg": f"data:image/svg+xml;base64,{SVG_B64}",
        "no_mime": f"data:;base64,{PAYLOAD_B64}",
        "text": f"data:text/plain;base64,{PAYLOAD_B64}",
        "octet": f"data:application/octet-stream;base64,{PAYLOAD_B64}",
        "html": f"data:text/html;base64,{PAYLOAD_B64}",
        "params": f"data:image/png;name=chart.png;charset=utf-8;base64,{PAYLOAD_B64}",
        "pct": f"data:image/png,{PCT_PAYLOAD}",
        "bare_comma": f"data:,{PCT_PAYLOAD}",
    }[kind]


CONTENT_CASES = {
    # markdown inline images
    "md_png": f"Chart: ![chart]({_uri()}) done",
    "md_upper_scheme": f"![chart]({_uri('upper')})",
    "md_mixed_case": f"![chart]({_uri('mixed')})",
    "md_svg": f"![logo]({_uri('svg')})",
    "md_no_mime": f"![x]({_uri('no_mime')})",
    "md_text_plain": f"![x]({_uri('text')})",
    "md_octet_stream": f"![x]({_uri('octet')})",
    "md_params": f"![x]({_uri('params')})",
    "md_percent_encoded": f"![x]({_uri('pct')})",
    "md_bare_comma": f"![x]({_uri('bare_comma')})",
    "md_angle": f"![x](<{_uri()}>)",
    "md_angle_with_space": f"![x](<data:image/png;base64,AAAA {PAYLOAD_B64}>)",
    "md_whitespace": f"![x](  {_uri()}  )",
    "md_title_double": f'![x]({_uri()} "a title")',
    "md_title_single": f"![x]({_uri()} 'a title')",
    "md_title_paren": f"![x]({_uri()} (a title))",
    "md_angle_title_with_parens": f'![x](<{_uri()}> "t (x)")',
    "md_empty_alt": f"![]({_uri()})",
    "md_alt_with_bang": f"![wow!]({_uri()})",
    "md_alt_with_bracket": f"![a[b]({_uri()})",
    "md_in_table": f"| a | b |\n|---|---|\n| x | ![x]({_uri()}) |",
    "md_in_list": f"- item\n- ![x]({_uri()})",
    "md_in_blockquote": f"> quoted\n> ![x]({_uri()})",
    "md_in_heading": f"## Title ![x]({_uri()})",
    "md_inside_link": f"[![x]({_uri()})](https://example.com)",
    # reference-style images and definitions
    "ref_def": f"![x][ref]\n\n[ref]: {_uri()}",
    "ref_def_angle_title": f'![x][ref]\n\n[ref]: <{_uri()}> "title"',
    "ref_def_collapsed": f"![ref][]\n\n[ref]: {_uri('upper')}",
    # MEDIA: tokens whose target is a data: URL
    "media_png": f"Here: MEDIA:{_uri()}",
    "media_upper": f"MEDIA:{_uri('upper')}",
    "media_svg": f"MEDIA:{_uri('svg')}",
    "media_text": f"MEDIA:{_uri('text')}",
    "media_bold_wrapped": f"**MEDIA:{_uri()}**",
    "media_quoted": f'"MEDIA:{_uri()}".',
    "media_in_inline_code": f"`MEDIA:{_uri()}`",
    # raw / entity-encoded HTML <img>
    "html_img_dq": f'<img src="{_uri()}">',
    "html_img_upper_sq": f"<IMG SRC='{_uri('upper')}' ALT=x>",
    "html_img_unquoted_newline": f"<img\nsrc={_uri()}\nalt=chart>",
    "html_img_entity_escaped": f'&lt;img src="{_uri()}"&gt;',
    "html_img_space_in_src": f'<img src="data:image/png;base64,AAAA {PAYLOAD_B64}">',
    # masquerading as the server-side embed's own output
    "html_img_embed_shape": (
        f'<img src="{_uri()}" class="msg-media-img" alt="ok.png" loading="lazy">'
    ),
    # data: URIs anywhere else in the message
    "md_link": f"[click]({_uri('html')})",
    "bare_prose": f"payload {_uri()} end",
    "inline_code": f"`{_uri()}`",
    "fenced_code": f"```css\nbackground: url({_uri()});\n```",
    "html_source_srcset": f'<picture><source srcset="{_uri()} 1x"></picture>',
}


@pytest.mark.parametrize("case", sorted(CONTENT_CASES))
def test_content_supplied_data_uri_never_reaches_public_snapshot(tmp_path: Path, case):
    snap = _snapshot(tmp_path, CONTENT_CASES[case])
    blob = json.dumps(snap)
    _assert_no_payload(blob)
    content = snap["messages"][-1]["content"]
    assert re.search(r"data:", content, re.IGNORECASE) is None, content
    assert PLACEHOLDER in content


def test_md_image_is_replaced_whole_and_prose_survives(tmp_path: Path):
    content = _snapshot(tmp_path, f"Chart: ![chart]({_uri()} \"t\") done")["messages"][-1]["content"]
    assert content == f"Chart: {PLACEHOLDER} done"


def test_html_img_is_replaced_whole(tmp_path: Path):
    content = _snapshot(tmp_path, f'a <img src="{_uri()}" alt="x"> b')["messages"][-1]["content"]
    assert content == f"a {PLACEHOLDER} b"


@pytest.mark.parametrize(
    "text, expected",
    [
        (f"MEDIA:{_uri()}", PLACEHOLDER),
        (f"**MEDIA:{_uri()}**", f"**{PLACEHOLDER}**"),
        (f'"MEDIA:{_uri()}".', f'"{PLACEHOLDER}".'),
    ],
    ids=["bare", "bold_wrapped", "quoted"],
)
def test_media_data_token_keeps_its_prose_suffix(tmp_path: Path, text, expected):
    assert _snapshot(tmp_path, text)["messages"][-1]["content"] == expected


def test_user_message_is_covered_too(tmp_path: Path):
    snap = _snapshot(tmp_path, f"look ![x]({_uri()})", role="user")
    _assert_no_payload(json.dumps(snap))


def test_data_uri_split_across_text_parts_is_caught(tmp_path: Path):
    uri = _uri()
    parts = [
        {"type": "text", "text": f"![x]({uri[:20]}"},
        {"type": "text", "text": f"{uri[20:]})"},
    ]
    snap = _snapshot(tmp_path, parts)
    _assert_no_payload(json.dumps(snap))
    assert PLACEHOLDER in snap["messages"][-1]["content"]


# --- the server-side local-media embed (#6174) is unaffected -----------------


def test_local_media_embed_survives_beside_placeholdered_content(tmp_path: Path):
    content = _snapshot(
        tmp_path, f"local MEDIA:ok.png and inline ![x]({_uri()})"
    )["messages"][-1]["content"]
    assert f'<img src="data:image/png;base64,{LOCAL_PNG_B64}"' in content
    assert 'alt="ok.png"' in content
    assert PLACEHOLDER in content
    _assert_no_payload(content)


def test_embed_output_is_never_placeholdered(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "ok.png").write_bytes(LOCAL_PNG)
    out = shares._sanitize_message(
        {"role": "assistant", "content": "see MEDIA:ok.png"}, allowed_roots=(ws,)
    )
    assert out is not None
    assert f'<img src="data:image/png;base64,{LOCAL_PNG_B64}"' in out["content"]
    assert PLACEHOLDER not in out["content"]


def test_embed_receives_text_without_content_data_uris(tmp_path: Path, monkeypatch):
    """Ordering pin: the placeholder pass runs BEFORE the embed, so the embed
    only ever adds data: URIs and never has its own output rewritten."""
    seen = []
    real = shares._embed_share_media

    def _spy(text, **kw):
        seen.append(text)
        return real(text, **kw)

    monkeypatch.setattr(shares, "_embed_share_media", _spy)
    _snapshot(tmp_path, f"MEDIA:ok.png ![x]({_uri()}) MEDIA:{_uri()} <img src=\"{_uri()}\">")
    assert seen
    for text in seen:
        assert re.search(r"data:", text, re.IGNORECASE) is None, text
    assert "MEDIA:ok.png" in seen[-1]


def test_direct_embed_call_never_embeds_a_data_token(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    out = shares._embed_share_media(f"x MEDIA:{_uri()} y", allowed_roots=(ws,))
    assert out == f"x {PLACEHOLDER} y"


def test_session_messages_are_not_mutated(tmp_path: Path):
    session = _session(tmp_path, f"![x]({_uri()})")
    shares.build_share_snapshot(session)
    assert session.messages[-1]["content"] == f"![x]({_uri()})"


@pytest.mark.parametrize(
    "text",
    [
        "metadata: the input data: 5 rows, 3 columns",
        "format is data:key,value pairs",
        "see https://example.com/a.png and MEDIA:https://example.com/b.png",
        "![remote](https://example.com/c.png) and <img src=\"https://example.com/d.png\">",
    ],
)
def test_ordinary_text_is_unchanged(tmp_path: Path, text):
    assert _snapshot(tmp_path, text)["messages"][-1]["content"] == text


# --- bounded work on adversarial input ---------------------------------------


@pytest.mark.parametrize(
    "unit",
    [
        "![x](data:",
        "![x](<data:",
        "![",
        "data:",
        "data:;",
        "data:a/a;",
        "<img ",
        "&lt;img ",
        "MEDIA:data:",
        "![x](data:a (",
    ],
)
def test_adversarial_input_is_linear(unit):
    def _elapsed(n: int) -> float:
        # One real data: URI keeps the pass off its no-"data:" fast path; it
        # leads so no closing delimiter follows the adversarial run.
        text = f"![x]({_uri()}) " + unit * n
        start = time.perf_counter()
        out = shares._sanitize_message({"role": "assistant", "content": text})
        elapsed = time.perf_counter() - start
        assert out is not None and PAYLOAD_B64 not in out["content"]
        return elapsed

    _elapsed(2_000)  # warm up
    small = _elapsed(10_000)
    large = _elapsed(40_000)
    # 4x the input must stay far below the ~16x a quadratic scan would cost.
    assert large < max(small * 10, 0.5), (unit, small, large)


# --- Gate W19 (2026-10-22) regressions ------------------------------------------


@pytest.mark.parametrize("ws", [" ", "\n", "\t"], ids=["spaces", "newlines", "tabs"])
def test_whitespace_run_after_md_image_opener_is_linear(ws):
    """`\\(\\s*<?\\s*data:` backtracked quadratically: 32k spaces after "![](" held
    the HTTP worker for seconds (master: 0.1 s)."""

    def _elapsed(n: int) -> float:
        text = f"![x]({_uri()}) " + "![x](" + ws * n + "x data:"
        start = time.perf_counter()
        shares._sanitize_message({"role": "assistant", "content": text})
        return time.perf_counter() - start

    _elapsed(2_000)
    small = _elapsed(10_000)
    large = _elapsed(40_000)
    assert large < max(small * 10, 0.5), (small, large)


@pytest.mark.parametrize(
    "uri",
    [
        f"data:image/png;name=a:b;base64,{PAYLOAD_B64}",
        f'data:image/png;name="chart.png";base64,{PAYLOAD_B64}',
        f"data:image/png;x='a';base64,{PAYLOAD_B64}",
        f"data:text/plain;charset=utf-8;x=1:2:3,{PCT_PAYLOAD}",
    ],
    ids=["colon_param", "dquoted_param", "squoted_param", "colons_pct"],
)
@pytest.mark.parametrize("wrap", ["prose", "md_image", "md_link", "html_img"])
def test_colon_and_quoted_parameter_forms_never_reach_the_snapshot(tmp_path: Path, uri, wrap):
    content = {
        "prose": f"see {uri} end",
        "md_image": f"![x]({uri})",
        "md_link": f"[open]({uri})",
        "html_img": f"<img src='{uri}'>" if '"' in uri else f'<img src="{uri}">',
    }[wrap]
    blob = json.dumps(_snapshot(tmp_path, content))
    _assert_no_payload(blob)
    assert "data:" not in blob.lower()


@pytest.mark.parametrize(
    "content",
    [
        f"![x](data:image/png;base64\n,{PAYLOAD_B64})",
        f"![x](data:image/png\n;base64,{PAYLOAD_B64})",
        f"![x](data:image/png(;base64,{PAYLOAD_B64})",
        f"![x](data:image/png;x=(;base64,{PAYLOAD_B64})",
    ],
    ids=["nl_before_comma", "nl_before_semi", "paren_before_semi", "paren_in_param"],
)
def test_renderer_stub_shapes_never_reach_the_snapshot(tmp_path: Path, content):
    """renderMd matches ![alt](data:image/...) up to the first ')' across
    newlines and shows a 64-character stub when the URI fails its strict check:
    the page would hide the payload while the snapshot JSON published it."""
    snap = _snapshot(tmp_path, content)
    _assert_no_payload(json.dumps(snap))
    assert "data:" not in snap["messages"][-1]["content"].lower()
    assert PLACEHOLDER in snap["messages"][-1]["content"]


def test_title_data_uri_is_placeholdered(tmp_path: Path):
    session = _session(tmp_path, "hello")
    session.title = f"T {_uri()}"
    snap = shares.build_share_snapshot(session)
    _assert_no_payload(json.dumps(snap))
    assert snap["title"] == f"T {PLACEHOLDER}"


@pytest.mark.parametrize(
    "tag",
    [
        '<img src="https://example.com/chart.png" alt="metadata: rows">',
        '<img src="https://example.com/chart.png" alt="data: 5 rows">',
        "<img alt='data: 5 rows' src='https://example.com/chart.png'>",
        '<img src="https://example.com/chart.png" title="data:">',
    ],
    ids=["alt_metadata", "alt_data_prose", "alt_sq_first", "title_bare_scheme"],
)
def test_https_img_with_data_in_text_attributes_is_kept(tmp_path: Path, tag):
    snap = _snapshot(tmp_path, f"see {tag} end")
    assert tag in snap["messages"][-1]["content"]
    assert PLACEHOLDER not in snap["messages"][-1]["content"]


@pytest.mark.parametrize(
    "tag",
    [
        f'<img alt="x" src="{_uri()}">',
        f'<img data-alt="x" src="https://example.com/a.png" srcset="{_uri()} 2x">',
        f'<img data-alt="{_uri()}" src="https://example.com/a.png">',
        f"<img title='a\" src=\"{_uri()}'>",
    ],
    ids=["src_after_alt", "srcset", "unknown_attr", "title_swallowing_src"],
)
def test_img_with_data_outside_text_attributes_never_publishes_payload(tmp_path: Path, tag):
    blob = json.dumps(_snapshot(tmp_path, f"see {tag} end"))
    _assert_no_payload(blob)
    assert "data:" not in blob.lower()


# --- Gate W20 (2026-10-22): a URI-shaped data: inside alt/title -------------------

_NL_URI = f"data:image/png;base64\n,{PAYLOAD_B64}"


@pytest.mark.parametrize(
    "tag",
    [
        f'<img alt="{_uri()}" src="https://example.com/a.png">',
        f'<img alt="{_NL_URI}" src="https://example.com/a.png">',
        f'<img title="{_NL_URI}" src="https://example.com/a.png">',
        f"<img title='a\" src=\"{_NL_URI}'>",
        f"<img title=\"src='{_uri()}'\">",
        f"<img title=\"src='{_NL_URI}'\">",
        f'<img alt="x&quot; src=&quot;{_uri()}">',
        f'<img alt="x&quot; src=&quot;{_NL_URI}">',
        f'<img alt="data: image/png;base64,{PAYLOAD_B64}" src="https://example.com/a.png">',
    ],
    ids=[
        "alt_uri",
        "alt_nl_split",
        "title_nl_split",
        "sq_title_fake_src_nl",
        "title_fake_src",
        "title_fake_src_nl",
        "alt_entity_quote_src",
        "alt_entity_quote_src_nl",
        "alt_space_after_scheme",
    ],
)
def test_uri_shaped_data_in_text_attribute_replaces_whole_tag(tmp_path: Path, tag):
    """The text-attribute allowance is for prose only. Before this, the
    catch-all removed only the URI head up to the newline and the payload
    stayed hidden in the kept alt/title value."""
    content = _snapshot(tmp_path, f"see {tag} end")["messages"][-1]["content"]
    _assert_no_payload(content)
    assert content == f"see {PLACEHOLDER} end"


# --- Gate W21 (2026-10-22): "<" or "&lt;" inside an <img> tag ----------------------


@pytest.mark.parametrize("wrap", ["{}", "<pre>{}</pre>"], ids=["bare", "pre"])
@pytest.mark.parametrize(
    "tag",
    [
        f'<img alt="a<b" src="{_NL_URI}">',
        f'<img alt="a&lt;b" src="{_NL_URI}">',
        f'<img alt="x<y" title="{_NL_URI}" src="https://example.com/a.png">',
        f'<img alt="a<b" src="data:image/png;base64 ,{PAYLOAD_B64}">',
        f'<img alt="a<b" src="{_uri()}">',
        f'&lt;img alt="a&lt;b" src="{_NL_URI}"&gt;',
        f"&lt;img title=&#39;a&quot; src=&quot;{_NL_URI}&#39;&gt;",
    ],
    ids=[
        "lt_alt_src_nl",
        "entity_lt_alt_src_nl",
        "lt_alt_title_nl_https",
        "lt_alt_src_space",
        "lt_alt_src_strict",
        "entity_tag_entity_lt",
        "all_entity_tag",
    ],
)
def test_img_tag_with_lt_inside_is_replaced_whole(tmp_path: Path, tag, wrap):
    """renderMd's tag recognizers stop only at ">", so a "<" inside an
    attribute value must not end the tag early here: the catch-all would strip
    only the URI head and leave the payload in an attribute the renderer drops
    (hidden on the page inside <pre>)."""
    content = _snapshot(tmp_path, wrap.format(tag))["messages"][-1]["content"]
    _assert_no_payload(content)
    assert content == wrap.format(PLACEHOLDER)


@pytest.mark.parametrize(
    "text",
    [
        "<img <" * 40_000,
        "<img " * 40_000 + 'src="data:image/png;base64,AAAA">',
        "&lt;img " * 25_000 + "&gt;",
        "<img " * 40_000,
    ],
    ids=["lt_runs", "openers_then_data", "entity_openers", "unclosed"],
)
def test_img_tag_pass_stays_linear_on_unclosed_openers(text):
    # A leading data: URI keeps the pass off its no-"data:" fast path, so
    # every input really exercises the tag regex.
    text = f"![x]({_uri()}) " + text
    shares._placeholder_inline_data_uris(text[:2_000])  # warm up
    small_text = text[: len(text) // 4]
    start = time.perf_counter()
    shares._placeholder_inline_data_uris(small_text)
    small = time.perf_counter() - start
    start = time.perf_counter()
    shares._placeholder_inline_data_uris(text)
    large = time.perf_counter() - start
    assert large < max(small * 10, 0.5), (small, large)
