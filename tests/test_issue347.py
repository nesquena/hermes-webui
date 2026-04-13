"""
Tests for GitHub issue #347: KaTeX / LaTeX math rendering in chat and workspace previews.

Structural tests — no server required. Verify:
- renderMd() stashes and restores $..$ and $$...$$ math delimiters
- KaTeX lazy-load function exists and follows the mermaid pattern
- KaTeX JS loaded from CDN with SRI integrity hash
- KaTeX CSS loaded in index.html with SRI hash
- CSS rules present for .katex-block and .katex-inline
- SAFE_TAGS updated to allow <span> (for inline math)
- renderKatexBlocks() is wired into the requestAnimationFrame call
"""
import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
UI_JS   = (REPO / 'static' / 'ui.js').read_text(encoding='utf-8')
INDEX   = (REPO / 'static' / 'index.html').read_text(encoding='utf-8')
CSS     = (REPO / 'static' / 'style.css').read_text(encoding='utf-8')


# ── renderMd pipeline ──────────────────────────────────────────────────────────

def test_display_math_stash_present():
    """renderMd must stash $$...$$ display math before other processing."""
    assert r'\$\$([\s\S]+?)\$\$' in UI_JS or '$$' in UI_JS, \
        'Display math $$..$$ stash regex not found in ui.js'
    # The stash uses \\x00M token
    assert '\\x00M' in UI_JS, 'Math stash token \\x00M not found in renderMd'


def test_inline_math_stash_present():
    """renderMd must stash $..$ inline math."""
    # Inline math regex must be present
    assert 'math_stash' in UI_JS, 'math_stash array not found in renderMd'


def test_katex_block_placeholder_emitted():
    """renderMd restore pass must emit .katex-block divs for display math."""
    assert 'katex-block' in UI_JS, \
        '.katex-block placeholder div not emitted by renderMd restore pass'


def test_katex_inline_placeholder_emitted():
    """renderMd restore pass must emit .katex-inline spans for inline math."""
    assert 'katex-inline' in UI_JS, \
        '.katex-inline placeholder span not emitted by renderMd restore pass'


def test_data_katex_attribute_present():
    """Placeholders must carry data-katex attribute for display/inline distinction."""
    assert 'data-katex' in UI_JS, \
        'data-katex attribute not found — renderKatexBlocks cannot distinguish display from inline'


# ── renderKatexBlocks() ────────────────────────────────────────────────────────

def test_render_katex_blocks_function_exists():
    """renderKatexBlocks() function must exist in ui.js."""
    assert 'function renderKatexBlocks()' in UI_JS, \
        'renderKatexBlocks() function not found in ui.js'


def test_katex_lazy_load_follows_mermaid_pattern():
    """KaTeX must use the same lazy-load pattern as mermaid (load on first use)."""
    assert '_katexLoading' in UI_JS, '_katexLoading flag not found'
    assert '_katexReady' in UI_JS,   '_katexReady flag not found'


def test_katex_js_loaded_from_cdn():
    """KaTeX JS must be loaded from jsdelivr CDN."""
    assert 'katex@0.16' in UI_JS, \
        'KaTeX JS CDN URL not found in ui.js — expected katex@0.16.x'


def test_katex_js_has_sri_hash():
    """KaTeX JS CDN tag must have an SRI integrity hash."""
    # The hash is in the script.integrity assignment
    assert "script.integrity='sha384-" in UI_JS or 'script.integrity="sha384-' in UI_JS, \
        'KaTeX JS SRI integrity hash not found in ui.js'


def test_katex_display_mode_used():
    """renderKatexBlocks must pass displayMode based on data-katex attribute."""
    assert 'displayMode' in UI_JS, \
        'displayMode not passed to katex.render() — display math will render inline'


def test_katex_throw_on_error_false():
    """KaTeX must be configured with throwOnError:false to degrade gracefully."""
    assert 'throwOnError:false' in UI_JS, \
        'throwOnError:false not set — bad LaTeX will throw and break the message'


def test_render_katex_blocks_wired_into_raf():
    """renderKatexBlocks() must be called in the same requestAnimationFrame as renderMermaidBlocks()."""
    # Check that renderKatexBlocks appears somewhere near requestAnimationFrame
    raf_idx = UI_JS.find('requestAnimationFrame')
    # Find the rAF call that also contains renderKatexBlocks
    has_katex_in_raf = any(
        'renderKatexBlocks' in UI_JS[m.start():m.start()+200]
        for m in re.finditer(r'requestAnimationFrame', UI_JS)
    )
    assert has_katex_in_raf, \
        'renderKatexBlocks() not found in any requestAnimationFrame call — math will not render'


# ── index.html ────────────────────────────────────────────────────────────────

def test_katex_css_in_index_html():
    """KaTeX CSS must be loaded in index.html."""
    assert 'katex@0.16' in INDEX, \
        'KaTeX CSS CDN link not found in index.html'


def test_katex_css_has_sri_hash():
    """KaTeX CSS link in index.html must have an SRI integrity hash."""
    assert 'sha384-5TcZemv2l' in INDEX or 'integrity' in INDEX and 'katex' in INDEX, \
        'KaTeX CSS SRI integrity hash not found in index.html'


# ── style.css ─────────────────────────────────────────────────────────────────

def test_katex_block_css_present():
    """.katex-block CSS rule must exist for centered display math."""
    assert '.katex-block' in CSS, \
        '.katex-block CSS rule missing from style.css — display math will have no layout'


def test_katex_inline_css_present():
    """.katex-inline CSS rule must exist."""
    assert '.katex-inline' in CSS, \
        '.katex-inline CSS rule missing from style.css'


def test_katex_block_text_align_center():
    """.katex-block must be text-align:center for display math."""
    assert 'text-align:center' in CSS, \
        'text-align:center not found for .katex-block'


# ── SAFE_TAGS ──────────────────────────────────────────────────────────────────

def test_safe_tags_includes_span():
    """SAFE_TAGS must include <span> to allow .katex-inline spans through the escape pass."""
    # The SAFE_TAGS regex should contain 'span'
    safe_tags_match = re.search(r'SAFE_TAGS\s*=\s*/.*?/i', UI_JS)
    assert safe_tags_match, 'SAFE_TAGS pattern not found in ui.js'
    assert 'span' in safe_tags_match.group(), \
        '<span> not in SAFE_TAGS — inline math spans will be HTML-escaped and rendered as text'
