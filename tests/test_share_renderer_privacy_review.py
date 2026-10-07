"""Exact-head review regressions through real snapshot and production renderMd."""
from __future__ import annotations

import base64
import copy
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote_from_bytes

import pytest

from tests.test_data_uri_images import _DRIVER_SRC
from tests.test_share_properties import png_bytes
from api import shares
from api.models import Session

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
PRIVATE = "https://webui.example/api/media?path=/tmp/private.png"
PNG_B64 = "data:image/png;base64," + base64.b64encode(png_bytes()).decode()
PNG = "data:image/png," + quote_from_bytes(
    png_bytes(metadata=b"inert file:///tmp/description"), safe=":/"
)


class Images(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.sources.append(dict(attrs).get("src", ""))

    def handle_data(self, data):
        self.text.append(data)


def review_cases():
    cases = []
    for slash in ("///", "////", "//\\"):
        ref = "https:" + slash + "webui.example/api/media?path=/tmp/private.png"
        for form in (f"![a]({ref})", f"MEDIA:{ref}"):
            cases.append((form, None, "slash-folding"))
    for opener in ("![`label]", "[`label]"):
        cases.append((f'{opener}(file:///tmp/f.png) <img src="{PRIVATE}">`', None, "code-opener"))
    cases.append((f'![`label]({PRIVATE}) <img src="{PRIVATE}">`', None, "private-code-opener"))
    for destination in ("file:///tmp/f.png", PRIVATE):
        for prefix, suffix in (("", ""), ("- ", ""), ("> > > ", ""), ("| body |\n|---|\n| ", " |")):
            cases.append((f'{prefix}![<img src="{PRIVATE}">]({destination}){suffix}', None, "inert-image-label"))
    for label, visible in (("A &amp; B", "A & B"), ("A &#60; B", "A < B"), ("A &#96; B", "A ` B")):
        for destination in ("file:///tmp/f.png", PRIVATE):
            cases.append((f'![{label}]({destination})', None, "entity-label:" + visible))
    for ticks in ("`", "``"):
        cases.append((f'![a]({PRIVATE}{ticks}) <img src="{PRIVATE}">{ticks}', None, "destination-code-opener"))
    cases.append((f"`path file:///tmp/private.txt` MEDIA:{PNG_B64} `note`", PNG_B64, "closing-backtick"))
    cases.append(("![a](https://cdn.example/profile:avatar.png)", "https://cdn.example/profile:avatar.png", "profile"))
    cases.append(("![a](https:///cdn.example/a.png)", "https:///cdn.example/a.png", "folded-public"))
    for quote in ('"', "'", ""):
        cases.append((f"<img src={quote}{PNG}{quote}>", PNG, "raw-data-src"))
        cases.append((f"<img src={quote}{PNG}{quote}> file:///tmp/outside.txt", PNG, "raw-data-neighbor"))
    # Link labels cross the same renderer phases as omitted image labels.
    labels = [f'<img src="{PRIVATE}">',
              f'&lt;img src="{PRIVATE}"&gt;',
              f'&amp;lt;img src="{PRIVATE}"&amp;gt;',
              f'&#60;img src="{PRIVATE}"&#62;',
              f'&ltimg src="{PRIVATE}"&gt',
              f'&#x60;<img src="{PRIVATE}">&#x60;']
    contexts = [("", ""), ("- ", ""), ("> > > ", ""),
                ("| body |\n|---|\n| ", " |"), ("`", "`")]
    for label in labels:
        for prefix, suffix in contexts:
            for scheme in ("file", "FILE"):
                cases.append((f'{prefix}[{label}]({scheme}:///tmp/f.png){suffix}',
                              None, "inert-file-link-label"))
    for form in ("![a]({ref})", "MEDIA:{ref}"):
        ref = "https://cdn.example/profile://avatar.png"
        cases.append((form.format(ref=ref), ref, "preserve-public"))
    for quote in ('"', "'", ""):
        ref = "https://cdn.example/profile://avatar.png"
        cases.append((f"<img src={quote}{ref}{quote}>", ref, "preserve-public"))
    for prefix in ("pro", "my", "a+", "a-", "a.", "a0"):
        cases.append((f"see {prefix}file://alice", None, "preserve-public"))
    for body in ("file://`file:///tmp/private.png", "`file:///tmp/private dir/secret.txt`",
                 "file://`", "`file://`", "file://"):
        cases.append((body, None, "file-code-residual"))
    for label, visible in (("A &amp; B", "A & B"), ("A &#96; B", "A ` B")):
        cases.append((f'[{label}](file:///tmp/f.png)', None, "entity-label:" + visible))
    return cases


def rendered_case(body, tmp_path):
    session = Session(session_id="share-review", title="Review", messages=[{"role": "assistant", "content": body}])
    before = copy.deepcopy(vars(session))
    snapshot = shares.build_share_snapshot(session)
    assert vars(session) == before
    content = snapshot["messages"][0]["content"]
    second = Session(session_id="share-review", title="Review", messages=[{"role": "assistant", "content": content}])
    assert shares.build_share_snapshot(second) == snapshot
    driver = tmp_path / "render.js"
    policy = """
global.location = {href: 'https://webui.example/', origin: 'https://webui.example'};
window.__HERMES_CONFIG__ = {imgSrcExtra: ['https://cdn.example']};
for (const name of ['_remoteImageSources', '_remoteImageSourceMatches',
  '_remoteImageAllowed', '_remoteImageReason', '_remoteImagePlaceholderHtml']) {
  eval(extractFunc(name));
}
"""
    driver.write_text(_DRIVER_SRC.replace("let buf = '';", policy + "\nlet buf = '';"))
    markup = subprocess.run(
        [NODE, str(driver), str(ROOT / "static/ui.js")], input=content,
        text=True, capture_output=True, check=True, timeout=30,
    ).stdout
    images = Images()
    images.feed(markup)
    return content, markup, images.sources


@pytest.mark.skipif(NODE is None, reason="Node required for production renderer")
@pytest.mark.parametrize("body,expected,kind", review_cases(), ids=[f"{kind}-{i}" for i, (_, _, kind) in enumerate(review_cases())])
def test_review_snapshot_and_renderer(body, expected, kind, tmp_path):
    content, markup, images = rendered_case(body, tmp_path)
    if expected is None:
        assert not images, (kind, content, markup)
    else:
        assert images == [expected], (kind, content, markup)
    if kind in ("code-opener", "private-code-opener", "destination-code-opener", "closing-backtick", "file-code-residual", "inert-file-link-label"):
        assert content.count("`") == body.count("`")
    if kind.startswith("entity-label:"):
        parsed = Images()
        parsed.feed(markup)
        assert kind.partition(":")[2] in "".join(parsed.text), (content, markup)
        assert content.count("`") == body.count("`")
    if kind in ("profile", "folded-public", "raw-data-src", "preserve-public"):
        assert content == body
    assert "file:///tmp/outside.txt" not in content
    if kind == "file-code-residual":
        assert "file://" not in content.lower()
        assert "secret.txt" not in content


@pytest.mark.parametrize("text", [
    '<img src="data:image/png,%89PNGfile:///inert?invalid"> file:///outside',
    '<img src="data:image/png,%89PNGfile:///inert" src="file:///outside">',
])
def test_raw_data_protection_does_not_exempt_invalid_or_shadowed_src(text):
    content = shares._omit_private_share_media_references(text)
    assert "file://" not in content


@pytest.mark.parametrize("label", ["A &amp; B", "A &#60; B", "A < B", "A ` B"])
def test_plain_title_retains_literal_private_image_label(label):
    session = Session(session_id="plain-share-title", title=f"![{label}]({PRIVATE})", messages=[{"role": "assistant", "content": "hello"}])
    assert shares.build_share_snapshot(session)["title"] == f"![{label}]({shares._PLACEHOLDER})"


@pytest.mark.parametrize("scheme", ["http", "https", "HTTP", "HTTPS", "HtTpS"])
@pytest.mark.parametrize("wrapper", ["{}", "`{}`"])
def test_plain_title_preserves_public_media_scheme_case(scheme, wrapper):
    title = wrapper.format(f"MEDIA:{scheme}://cdn.example/icon.png")
    session = Session(session_id="public-title", title=title,
                      messages=[{"role": "assistant", "content": "hello"}])
    before = copy.deepcopy(vars(session))
    assert shares.build_share_snapshot(session)["title"] == title
    assert vars(session) == before


@pytest.mark.parametrize("scheme", ["http", "https", "HTTP", "HTTPS", "HtTpS"])
@pytest.mark.parametrize("wrapper", ["{}", "`{}`"])
def test_plain_title_omits_private_media_before_scheme_preservation(scheme, wrapper):
    title = wrapper.format(f"MEDIA:{scheme}://webui.example/api/media?path=/tmp/private.png")
    session = Session(session_id="private-title", title=title,
                      messages=[{"role": "assistant", "content": "hello"}])
    assert shares.build_share_snapshot(session)["title"] == shares._PLACEHOLDER


@pytest.mark.parametrize("quote", ['"', "'", ""])
@pytest.mark.parametrize("shadowed", [False, True])
def test_rejected_data_image_scheme_boundary_exception_is_attribute_scoped(quote, shadowed):
    data = "data:image/png,%89PNGfile:///inert?invalid"
    src = f"src={quote}{data}{quote}"
    tail = ' src="file:///outside"' if shadowed else ""
    public = "https://cdn.example/profile://avatar.png"
    text = f"<img {src}{tail}> ![public]({public}) see profile://alice"
    clean = shares._omit_private_share_media_references(text)
    assert "PNGfile://" not in clean
    assert "file:///outside" not in clean
    assert public in clean
    assert "profile://alice" in clean
    assert shares._omit_private_share_media_references(clean) == clean
