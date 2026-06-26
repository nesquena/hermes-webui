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
    _palette_to_css,
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
    assert out.startswith(":root{")
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
    override_pos = html.rfind(":root{--bg:#FAF7F0")        # our override
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
