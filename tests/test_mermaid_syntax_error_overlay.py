"""
Tests for the "Syntax error in text — mermaid version 10.9.3" overlay bug.

When a mermaid code block contains invalid syntax, mermaid 10.x's render()
function (a) throws AND (b) injects a giant error SVG into <body>. The
existing catch handler only fixed the inline block; the orphan SVG floated
in the DOM and consumed massive vertical space, ruining the chat layout.

Fix:
- pass `suppressErrorRendering: true` to mermaid.initialize() so mermaid
  never injects the global error SVG;
- short-circuit invalid input via `mermaid.parse(code, {suppressErrors: true})`
  before calling render() so render() is not called at all on invalid input;
- defensively remove any orphan element with the render id from the DOM in
  the catch path.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent


def _ui_js() -> str:
    return (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


class TestMermaidSyntaxErrorOverlay:
    def test_initialize_sets_suppress_error_rendering(self):
        """mermaid.initialize() must opt out of the global error overlay SVG."""
        src = _ui_js()
        init_idx = src.find("mermaid.initialize(")
        assert init_idx != -1, "mermaid.initialize() call not found in ui.js"
        # Look only at the initialize call (before the closing `})` of its
        # config object). Find the matching close.
        close = src.find("});", init_idx)
        assert close != -1, "couldn't find end of mermaid.initialize() call"
        block = src[init_idx:close]
        assert "suppressErrorRendering:true" in block, (
            "mermaid.initialize() must include suppressErrorRendering:true to "
            "stop mermaid from injecting a giant 'Syntax error in text' SVG "
            "into <body> on invalid diagrams"
        )

    def test_render_uses_parse_with_suppress_errors(self):
        """We must validate via mermaid.parse({suppressErrors:true}) before render()."""
        src = _ui_js()
        assert "mermaid.parse(" in src, (
            "renderMermaidBlocks must call mermaid.parse() before mermaid.render() "
            "so syntax errors do not trigger the error overlay"
        )
        assert "suppressErrors:true" in src, (
            "mermaid.parse() must be called with {suppressErrors:true} so it "
            "returns false instead of throwing on invalid input"
        )

    def test_catch_path_cleans_up_orphan_error_svg(self):
        """The catch handler must remove orphan mermaid error elements from <body>."""
        src = _ui_js()
        # Heuristic: the cleanup helper is named cleanupOrphanError and is
        # invoked from the catch / parse-failure paths.
        assert "cleanupOrphanError" in src, (
            "renderMermaidBlocks must define and call a cleanup helper that "
            "removes orphan mermaid error elements left in <body>"
        )

    def test_fallback_still_renders_code_block(self):
        """The fallback must still show the original mermaid source as <pre><code>."""
        src = _ui_js()
        assert '<div class="pre-header">mermaid</div>' in src, (
            "Fallback rendering must show a 'mermaid' header so the user knows "
            "the block was a mermaid diagram with invalid syntax"
        )
