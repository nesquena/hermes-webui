"""Static regression coverage for Mermaid diagram lightbox wiring."""

from pathlib import Path


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
        assert "function _openMermaidLightbox(svgEl) {" in src
        assert "const clone = svgEl.cloneNode(true);" in src
        assert "clone.classList.add('mermaid-lightbox-svg');" in src

    def test_mermaid_lightbox_reuses_existing_modal_chrome(self):
        src = _ui_js()
        assert "lb.className = 'img-lightbox';" in src
        assert "cls.className = 'img-lightbox-close';" in src
        assert "cls.onclick = () => _closeImgLightbox(lb);" in src


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
        assert ".mermaid-rendered svg{max-width:100%;height:auto;cursor:zoom-in;}" in src

    def test_lightbox_svg_uses_modal_viewport_limits(self):
        src = _style_css()
        rule = ".img-lightbox .mermaid-lightbox-svg{max-width:90vw;max-height:90vh;"
        assert rule in src
