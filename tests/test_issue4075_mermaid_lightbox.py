"""Static regression coverage for Mermaid diagram lightbox wiring."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "static" / "ui.js"
STYLE = ROOT / "static" / "style.css"


def _ui_js() -> str:
    return UI.read_text(encoding="utf-8")


def _style_css() -> str:
    return STYLE.read_text(encoding="utf-8")


class TestMermaidLightboxHelper:
    def test_mermaid_lightbox_has_dedicated_helper(self):
        src = _ui_js()
        assert re.search(r"function\s+_mountMermaidViewer\(svgEl,\s*options\s*=\s*\{\}\)\s*\{", src)
        assert re.search(r"function\s+_openMermaidLightbox\(svgEl\)\s*\{", src)
        assert "const viewer = _mountMermaidViewer(clone, {mode:'lightbox'});" in src
        assert "mermaid-lightbox-svg" in src

    def test_mermaid_render_path_mounts_inline_viewer_shell(self):
        src = _ui_js()
        assert "const renderedSvg = block.querySelector('svg');" in src
        assert "if(renderedSvg) _mountMermaidViewer(renderedSvg, {mode:'inline'});" in src

    def test_mermaid_lightbox_reuses_existing_modal_chrome(self):
        src = _ui_js()
        assert "img-lightbox" in src
        assert "img-lightbox-close" in src
        assert "_closeImgLightbox(lb)" in src

    def test_mermaid_lightbox_rewrites_cloned_svg_ids(self):
        src = _ui_js()
        assert "const idMap = new Map();" in src
        assert "const idPrefix = 'mermaid-lightbox-'" in src
        assert "replace(/url\\(#([^)]+)\\)/g" in src

    def test_mermaid_lightbox_rewrites_embedded_style_selectors(self):
        src = _ui_js()
        assert "clone.querySelectorAll('style').forEach(styleEl => {" in src
        assert "styleText = styleText.replace(new RegExp(`url\\\\(#${escapedId}\\\\)`" in src
        assert "styleText = styleText.replace(new RegExp(`(^|[^\\\\w-])#${escapedId}(?=$|[^\\\\w-])`" in src


class TestDocumentClickDelegate:
    def test_delegate_routes_rendered_mermaid_svgs_before_attach_thumb(self):
        src = _ui_js()
        mermaid_branch = (
            "  const mermaidSvg = e.target.closest('.mermaid-rendered svg');\n"
            "  if(mermaidSvg){ _openMermaidLightbox(mermaidSvg); return; }\n"
        )
        attach_branch = (
            "  img = e.target.closest('.attach-thumb');\n"
            "  if(img && img.tagName === 'IMG'){\n"
        )
        assert mermaid_branch in src
        assert attach_branch in src
        assert src.index(mermaid_branch) < src.index(attach_branch)

    def test_delegate_still_handles_message_images(self):
        src = _ui_js()
        msg_branch = "let img = e.target.closest('.msg-media-img');\n  if(img){ _openImgLightbox(img); return; }"
        assert msg_branch in src


class TestMermaidLightboxCss:
    def test_rendered_mermaid_svg_advertises_zoom(self):
        src = _style_css()
        assert ".mermaid-rendered svg{max-width:100%;height:auto;cursor:default;}" in src

    def test_lightbox_svg_uses_modal_viewport_limits(self):
        src = _style_css()
        assert ".mermaid-viewer--lightbox .mermaid-viewer-viewport{max-width:90vw;max-height:90vh;" in src
        assert ".img-lightbox .mermaid-lightbox-svg{background:var(--code-bg);cursor:default;}" in src
        assert "background:var(--code-bg);" in src


class TestLightboxAria:
    def test_lightboxes_set_aria_modal(self):
        src = _ui_js()
        assert src.count("setAttribute('aria-modal', 'true')") >= 2
