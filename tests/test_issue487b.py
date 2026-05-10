r"""
Regression test for image src URL corruption by the autolink pass.

Bug: the _al_stash before the autolink pass only stashed <a> tags.
<img> tags produced by the ![alt](url) image pass were NOT stashed,
so the autolink regex matched the URL inside src="..." and wrapped it
in <a href="...">url</a>, producing src="<a href="...">url</a>" —
a completely broken image source.

Fix: extend _al_stash regex to also stash <img> tags:
  (<a\b[^>]*>[\s\S]*?<\/a>|<img\b[^>]*>)
"""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text()


# ── Source-level check ────────────────────────────────────────────────────────


def test_al_stash_includes_img_tags():
    """_al_stash regex must stash both <a> and <img> tags to protect src= from autolink."""
    assert "<img\\b[^>]*>" in UI_JS or "<img\\\\b[^>]*>" in UI_JS, (
        "_al_stash should include <img> tag pattern to prevent autolink mangling src= URLs"
    )


# ── Behaviour tests (Python mirror of fixed pipeline) ─────────────────────────

import html as _html


def esc(s):
    return _html.escape(str(s), quote=True)


SAFE_TAGS = re.compile(
    r"^</?(strong|em|code|pre|h[1-6]|ul|ol|li|table|thead|tbody|tr|th|td"
    r"|hr|blockquote|p|br|a|img|div|span)([\s>]|$)",
    re.I,
)


def render_with_image_and_autolink(raw):
    """Simulate the image pass + SAFE_TAGS + _al_stash + autolink pipeline."""
    s = raw
    # Image pass
    s = re.sub(
        r"!\[([^\]]*)\]\((https?://[^\)]+)\)",
        lambda m: (
            f'<img src="{m.group(2).replace(chr(34), "%22")}" '
            f'alt="{esc(m.group(1))}" class="msg-media-img" loading="lazy">'
        ),
        s,
    )
    # SAFE_TAGS
    s = re.sub(
        r"</?[a-zA-Z][^>]*>",
        lambda m: m.group() if SAFE_TAGS.match(m.group()) else esc(m.group()),
        s,
    )
    # _al_stash (fixed: stashes both <a> and <img>)
    al_stash = []
    s = re.sub(
        r"(<a\b[^>]*>[\s\S]*?<\/a>|<img\b[^>]*>)",
        lambda m: al_stash.append(m.group(1)) or f"\x00B{len(al_stash) - 1}\x00",
        s,
    )

    # Autolink
    def autolink(m):
        url = m.group(1)
        trail = url[-1] if url[-1] in ".,;:!?)" else ""
        clean = url[:-1] if trail else url
        return (
            f'<a href="{clean}" target="_blank" rel="noopener">{esc(clean)}</a>{trail}'
        )

    s = re.sub(r'(https?://[^\s<>"\')\]]+)', autolink, s)
    # Restore
    s = re.sub(r"\x00B(\d+)\x00", lambda m: al_stash[int(m.group(1))], s)
    return s


def test_image_src_not_mangled_by_autolink():
    """The URL inside src= of a rendered <img> must not be wrapped in <a> by autolink."""
    url = "https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/280px-PNG_transparency_demonstration_1.png"
    result = render_with_image_and_autolink(f"![alt]({url})")
    assert f'src="{url}"' in result, f"src= URL should be intact, got: {result[:200]}"
    # The URL inside src= must NOT be wrapped in <a>
    src_part = result.split('src="')[1].split('"')[0]
    assert "<a " not in src_part, f"src= must not contain <a> tag, got: {src_part}"
    assert src_part == url, f"src= URL mangled: expected {url}, got {src_part}"


def test_image_tag_renders_as_img():
    """![alt](url) must produce an <img> tag, not a plain link."""
    result = render_with_image_and_autolink(
        "![Test image](https://example.com/img.png)"
    )
    assert "<img " in result, f"Expected <img> tag, got: {result}"
    assert 'src="https://example.com/img.png"' in result
    assert "<a " not in result  # no spurious link wrapper


def test_image_and_link_in_same_paragraph():
    """Image and link in same paragraph must each render correctly without interference."""
    result = render_with_image_and_autolink(
        "See ![logo](https://example.com/logo.png) and visit https://example.com"
    )
    assert "<img " in result, "Image should render"
    assert "<a " in result, "Bare URL should autolink"
    # img src must not contain <a>
    src_part = result.split('src="')[1].split('"')[0]
    assert "<a" not in src_part, f"src= mangled: {src_part}"


def test_image_count_is_one():
    """One ![alt](url) should produce exactly one <img> tag."""
    result = render_with_image_and_autolink("![test](https://example.com/x.png)")
    assert result.count("<img ") == 1, (
        f"Expected 1 <img>, got {result.count('<img ')}: {result}"
    )


def test_multiple_images_not_mangled():
    """Multiple images in one message each get clean src= values."""
    urls = [
        "https://example.com/a.png",
        "https://example.com/b.png",
    ]
    raw = "\n\n".join(f"![img{i}]({url})" for i, url in enumerate(urls))
    result = render_with_image_and_autolink(raw)
    for url in urls:
        assert f'src="{url}"' in result, f"src= for {url} mangled in: {result[:300]}"


def test_image_with_query_string_src_intact():
    """Image URL with & in query string must have & (not &amp;) in src."""
    url = "https://example.com/img?w=100&h=200&fmt=png"
    result = render_with_image_and_autolink(f"![img]({url})")
    assert f'src="{url}"' in result, f"Query string URL mangled: {result[:200]}"
    assert "&amp;" not in result.split('src="')[1].split('"')[0]
