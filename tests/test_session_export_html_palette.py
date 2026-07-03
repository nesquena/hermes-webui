"""Tests for `api.session_export_html` palette injection.

These guard the contract that:
  1. A palette captured from the live WebUI (getComputedStyle) flows through the
     export and overrides the inlined fallback so the file matches the user's
     active theme + skin.
  2. The palette is sanitised so a hostile client cannot break out of the
     `<style>` block via CSS injection.
  3. When no palette is supplied (CLI / direct API consumers), the existing
     dark/light fallback still renders unchanged.
"""
from __future__ import annotations

from api.session_export_html import (
    _content_to_text,
    _neutralize_remote_images,
    _palette_to_css,
    _render_markdown,
    render_session_html,
)


def _fake_session() -> dict:
    return {
        "session_id": "abc123",
        "title": "Palette test",
        "model": "gpt-test",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello back"},
        ],
    }


# ---------------------------------------------------------------- sanitiser ---


def test_palette_to_css_accepts_hex_rgb_and_color_mix() -> None:
    out = _palette_to_css({
        "bg": "#FAF7F0",
        "accent": "rgb(184, 134, 11)",
        "border": "color-mix(in srgb, #000 60%, transparent)",
    })
    assert out.startswith(":root,:root.dark{")
    assert "--bg:#FAF7F0;" in out
    assert "--accent:rgb(184, 134, 11);" in out
    assert "color-mix" in out


def test_palette_to_css_strips_invalid_names_and_values() -> None:
    # Bad name (contains ;{}) is dropped; bad value (style-break attempt) is dropped.
    out = _palette_to_css({
        "; }body{display:none": "#fff",      # malicious name
        "bg": "red; }body{display:none",      # malicious value
        "border": "#abc",                     # legit, must survive
    })
    assert "display:none" not in out
    assert ";}body{" not in out
    # The legit entry still makes it through, even when paired with hostile siblings.
    assert "--border:#abc;" in out


def test_palette_to_css_empty_input_returns_empty_string() -> None:
    assert _palette_to_css({}) == ""
    assert _palette_to_css(None) == ""  # type: ignore[arg-type]


def test_palette_to_css_caps_value_length() -> None:
    # A pathologically long value should be rejected rather than embedded.
    out = _palette_to_css({"bg": "#" + ("a" * 200)})
    assert out == ""


def test_palette_to_css_drops_fetchable_url_values() -> None:
    # A value carrying a fetchable url() must be dropped: the charset has no
    # `/`, `:`, `'` or `"`, so a real `url(http://...)` / `url(/path)` can't
    # survive — pinning that so the no-outbound-request guarantee can't
    # silently regress if the charset is ever widened.
    out = _palette_to_css({
        "bg": "url(http://evil.example/x.png)",   # protocol-bearing
        "fg": "url(//evil.example/x.png)",          # scheme-relative
        "accent": "url(/leak/signed-url)",          # path-bearing
        "border": "url('http://evil.example/y')",   # quoted
        "ok": "#abc",                                # legit sibling survives
    })
    assert "evil.example" not in out
    assert "url(http" not in out
    assert "url(//" not in out
    assert "url(/" not in out
    # The one safe entry still makes it through.
    assert "--ok:#abc;" in out


# ---------------------------------------------------------------- end-to-end ---


def test_render_without_palette_uses_builtin_fallback() -> None:
    html = render_session_html(_fake_session(), theme="dark")
    # Built-in dark palette is present; no extra `:root{...}` override was appended.
    assert "--bg:#0D0D1A" in html  # dark fallback
    # The inlined CSS contains exactly one bare `:root{` (the light defaults) and
    # one `:root.dark{` (the dark overrides). No `palette_css` override block.
    assert html.count(":root{") == 1
    assert html.count(":root.dark{") == 1
    assert '<html lang="en" class="dark">' in html


def test_render_with_palette_appends_override_after_builtin() -> None:
    palette = {"bg": "#FAF7F0", "text": "#1A1610", "accent": "#B8860B"}
    html = render_session_html(_fake_session(), theme="light", palette=palette)
    builtin_pos = html.find(":root{--bg:#FEFCF7")          # light fallback marker
    override_pos = html.rfind(":root,:root.dark{--bg:#FAF7F0")  # our override
    assert builtin_pos > 0, "light fallback should still be inlined"
    assert override_pos > builtin_pos, (
        "palette override must come AFTER the built-in CSS so it actually wins; "
        f"builtin@{builtin_pos} override@{override_pos}"
    )
    assert "--accent:#B8860B;" in html
    # No <html class="dark"> when theme=light.
    assert 'class="dark"' not in html


def test_render_with_hostile_palette_drops_bad_entries_but_keeps_safe_ones() -> None:
    html = render_session_html(
        _fake_session(),
        theme="light",
        palette={"bg": "red;}body{display:none", "border": "#abc"},
    )
    assert "display:none" not in html
    assert "--border:#abc;" in html


def test_render_dark_mode_palette_overrides_builtin_dark() -> None:
    """Regression: captured palette must win in dark mode (the default theme).

    _CSS defines :root.dark{…} at specificity (0,0,2,0).  The palette override
    must match or beat that specificity so it actually applies when the export is
    rendered with <html class="dark">.  Without the fix the palette was silently
    ignored and the export fell back to the generic dark theme.
    """
    palette = {"bg": "#112233", "text": "#EEDDCC", "accent": "#FF5500"}
    html = render_session_html(_fake_session(), theme="dark", palette=palette)
    # The exported HTML uses dark mode.
    assert '<html lang="en" class="dark">' in html
    # The palette override block is present with both selectors.
    assert ":root,:root.dark{" in html
    # The captured values appear in the override block (not just the fallback).
    assert "--bg:#112233;" in html
    assert "--text:#EEDDCC;" in html
    assert "--accent:#FF5500;" in html
    # The override comes AFTER the built-in :root.dark block so source-order wins.
    builtin_dark_pos = html.find(":root.dark{--bg:#0D0D1A")
    override_pos = html.find(":root,:root.dark{--bg:#112233")
    assert builtin_dark_pos > 0
    assert override_pos > builtin_dark_pos, (
        "palette override must come after built-in :root.dark so it wins; "
        f"builtin_dark@{builtin_dark_pos} override@{override_pos}"
    )


def test_palette_to_css_drops_expression_values() -> None:
    """expression() is IE-only CSS-eval; must be rejected as defense-in-depth."""
    out = _palette_to_css({
        "bg": "expression(alert(1))",
        "fg": "Expression( document.cookie )",
        "accent": "EXPRESSION(0)",
        "ok": "#abc",  # legit sibling survives
    })
    assert "expression" not in out.lower()
    assert "--ok:#abc;" in out


# ----------------------------------------------------- self-contained images ---


def _image_content(url: str) -> list:
    return [
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "image_url": {"url": url}},
    ]


def test_remote_image_url_is_flattened_to_inert_text() -> None:
    # A remote http(s) image must NOT become a Markdown image (which would
    # render as <img src="http..."> and fire a network request on open, leaking
    # a signed/private URL). It is flattened to an inline-code placeholder that
    # still records the URL so the transcript isn't lossy.
    remote = "https://example.com/private/signed.png?sig=secret"
    flat = _content_to_text(_image_content(remote))
    assert "![image]" not in flat                 # not an active image
    assert f"`[image: {remote}]`" in flat          # inert, URL preserved


def test_data_uri_image_is_kept_as_inline_image() -> None:
    # data: URIs are already self-contained (no network), so they stay as a real
    # Markdown image and render offline.
    data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    flat = _content_to_text(_image_content(data_uri))
    assert f"![image]({data_uri})" in flat


def test_remote_image_never_appears_as_active_img_in_full_html() -> None:
    # End-to-end guard regardless of whether markdown_it is installed: the
    # rendered document must not contain an <img> pointing at the remote URL.
    remote = "https://example.com/leak.png?token=abc"
    html = render_session_html(
        {"session_id": "img1", "title": "t",
         "messages": [{"role": "user", "content": _image_content(remote)}]},
        theme="dark",
    )
    assert f'src="{remote}"' not in html
    assert remote in html  # still present as inert text


# ------------------------------------------- markdown-image-in-text (P1 #4968) ---
#
# The multimodal-part flattening above only covers structured image_url content.
# A *text* message body carrying Markdown image syntax — ![alt](https://...) —
# is rendered by markdown_it into an active <img src="https://...">, which fires
# a network request on open and leaks signed/private URLs. _neutralize_remote_images
# closes that path post-render. These tests pin it.


def test_text_markdown_remote_image_is_neutralized() -> None:
    remote = "https://example.com/private.png?sig=secret"
    out = _render_markdown(f"here is a leak ![x]({remote})")
    # Core security invariant holds in BOTH environments:
    #  - markdown_it present  -> <img> rendered then neutralized to placeholder
    #  - markdown_it absent    -> fallback escapes into <pre>, never an <img>
    assert "<img" not in out                     # no active image element
    assert f'src="{remote}"' not in out           # remote src never emitted
    assert remote in out                          # URL kept as inert text
    from api.session_export_html import _MD
    if _MD is not None:
        assert "[image:" in out                   # shown via placeholder


def test_text_markdown_data_uri_image_is_kept() -> None:
    # data: images are already embedded and offline-safe. With markdown_it they
    # render as an active <img>; without it the fallback keeps the data: URI as
    # escaped text. Either way the URI is preserved and no remote fetch occurs.
    data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    out = _render_markdown(f"embedded ![x]({data_uri})")
    assert data_uri in out
    from api.session_export_html import _MD
    if _MD is not None:
        assert "<img" in out


def test_neutralize_handles_single_quoted_and_uppercase_src() -> None:
    # Defensive: the regex must catch single-quoted src and tag-case variants,
    # not just the canonical markdown_it output.
    assert "<img" not in _neutralize_remote_images("<img src='http://h/x.png'>")
    assert "<img" not in _neutralize_remote_images('<IMG SRC="http://h/x.png">')
    # data: survives regardless of quoting/case.
    kept = _neutralize_remote_images("<IMG src='data:image/png;base64,AAAA'>")
    assert "<IMG" in kept or "<img" in kept


def test_text_markdown_remote_image_absent_from_full_html() -> None:
    # End-to-end: a remote markdown image in a plain-text turn must not survive
    # as an <img> in the exported document.
    remote = "https://example.com/leak2.png?token=xyz"
    html = render_session_html(
        {"session_id": "md-img", "title": "t",
         "messages": [{"role": "assistant", "content": f"see ![pic]({remote})"}]},
        theme="dark",
    )
    assert f'src="{remote}"' not in html
    assert remote in html  # inert text placeholder
