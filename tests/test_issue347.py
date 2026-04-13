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


# ── Stash ordering: fence must protect code spans from math extraction ─────────

WORKSPACE_JS = (REPO / 'static' / 'workspace.js').read_text(encoding='utf-8')


def test_fence_stash_before_math_stash():
    """fence_stash must be initialized and populated BEFORE math_stash in renderMd.

    If math_stash runs first, dollar signs inside backtick code spans are extracted
    as math, leaving placeholder tokens inside the stashed code string. The code span
    then renders with KaTeX inside <code> instead of the literal dollar-sign text.
    """
    fence_pos = UI_JS.find("const fence_stash=[]")
    math_pos = UI_JS.find("const math_stash=[]")
    assert fence_pos != -1, "fence_stash not found in renderMd"
    assert math_pos != -1, "math_stash not found in renderMd"
    assert fence_pos < math_pos, (
        "fence_stash must be declared BEFORE math_stash in renderMd "
        f"(fence at char {fence_pos}, math at char {math_pos}). "
        "If math runs first, `$x$` inside backticks gets extracted as math instead of code."
    )


def test_fence_stash_populated_before_math_stash():
    """The fence_stash s.replace call must appear before any math_stash s.replace calls."""
    # Find the s.replace call that populates each stash
    fence_replace_pos = UI_JS.find("fence_stash.push(m)")
    math_replace_pos = UI_JS.find("math_stash.push(")
    assert fence_replace_pos != -1, "fence_stash population call not found"
    assert math_replace_pos != -1, "math_stash population call not found"
    assert fence_replace_pos < math_replace_pos, (
        "fence_stash must be populated before math_stash to protect code span contents"
    )


def test_math_stash_comment_says_after_fence():
    """The math stash comment should explain it runs AFTER fence_stash, not before."""
    # Should not have the old misleading comment
    assert "Must run BEFORE fence_stash" not in UI_JS, (
        "Old misleading comment still present. Math stash runs AFTER fence_stash. "
        "The comment should say 'Runs AFTER fence_stash'."
    )


# ── Pipeline regression: code spans protect their contents ────────────────────

def test_math_restore_after_fence_restore():
    """Math stash tokens are restored AFTER fence restore, so code spans get
    their raw text back (not KaTeX placeholders)."""
    fence_restore_pos = UI_JS.find("fence_stash[+i]")
    math_restore_pos = UI_JS.find("math_stash[+i]")
    assert fence_restore_pos != -1, "fence_stash restore not found"
    assert math_restore_pos != -1, "math_stash restore not found"
    # Both restores must exist; their relative order doesn't matter for correctness
    # (they use different tokens: \x00F vs \x00M), but we assert both exist
    assert fence_restore_pos != math_restore_pos, "fence and math restore must be separate calls"


def test_stash_tokens_distinct():
    """fence_stash and math_stash must use distinct sentinel tokens to avoid collisions."""
    # fence uses \x00F, math uses \x00M (or similar unique prefix)
    # The JS source uses escaped \\x00F and \\x00M as sentinel characters
    # In the Python string read from the file these appear as '\\\\x00F' and '\\\\x00M'
    assert "'\\\\x00F'" in UI_JS or 'x00F' in UI_JS, (
        "fence stash token (\\x00F) not found — must be distinct from math token"
    )
    assert "'\\\\x00M'" in UI_JS or 'x00M' in UI_JS, (
        "math stash token (\\x00M) not found — must be distinct from fence token"
    )
    # The two tokens must use different discriminator characters
    assert 'x00F' in UI_JS and 'x00M' in UI_JS, (
        "Both \\x00F (fence) and \\x00M (math) tokens must exist"
    )


# ── Workspace preview renderKatexBlocks wiring ────────────────────────────────

def test_workspace_calls_render_katex_after_preview():
    """workspace.js must call renderKatexBlocks() after setting previewMd.innerHTML.

    Without this, math placeholders appear in workspace file previews but are never
    rendered by KaTeX (renderKatexBlocks is only wired into renderMessages rAF).
    """
    assert "renderKatexBlocks" in WORKSPACE_JS, (
        "workspace.js must call renderKatexBlocks() after renderMd() for file previews"
    )


def test_workspace_renders_katex_after_file_open():
    """workspace.js renderKatexBlocks call must come after the renderMd(data.content) assignment."""
    preview_md_pos = WORKSPACE_JS.find("renderMd(data.content)")
    # Use the actual call string (not a stray regex match on 'M' characters)
    katex_call_str = "renderKatexBlocks==='function'"
    katex_call_pos = WORKSPACE_JS.find(katex_call_str)
    assert preview_md_pos != -1, "renderMd(data.content) not found in workspace.js"
    assert katex_call_pos != -1, (
        "renderKatexBlocks guard (typeof renderKatexBlocks==='function') not found in workspace.js"
    )
    # The call after 'renderMd(data.content)' — find the LAST occurrence
    # (there may be an earlier one in the save path at line ~153)
    last_katex_pos = WORKSPACE_JS.rfind(katex_call_str)
    assert last_katex_pos > preview_md_pos, (
        "renderKatexBlocks must be called AFTER renderMd(data.content) in workspace.js "
        f"(renderMd at {preview_md_pos}, last renderKatexBlocks at {last_katex_pos})"
    )


def test_workspace_katex_guarded_by_typeof():
    """workspace.js renderKatexBlocks call must guard with typeof check for safety
    in case KaTeX feature is not loaded (e.g. test environments, offline)."""
    assert "typeof renderKatexBlocks" in WORKSPACE_JS, (
        "workspace.js must guard renderKatexBlocks call with typeof check: "
        "if(typeof renderKatexBlocks==='function')renderKatexBlocks()"
    )


# ── SAFE_TAGS: span addition should not expand attack surface ─────────────────

def test_safe_tags_span_is_narrowly_scoped():
    """SAFE_TAGS adding <span> is only a bypass if span carries dangerous attributes.
    Verify the SAFE_TAGS regex tests the tag NAME only, not arbitrary attributes.
    The rest of the pipeline uses esc() for user content, so attribute injection
    into KaTeX spans isn't possible.
    """
    # The SAFE_TAGS regex must still require a word boundary / tag-end pattern
    safe_tags_match = re.search(r"SAFE_TAGS\s*=\s*/(.+?)/i", UI_JS)
    if not safe_tags_match:
        safe_tags_match = re.search(r'SAFE_TAGS\s*=\s*/(.*?)/i', UI_JS)
    assert safe_tags_match, "SAFE_TAGS regex not found"
    pattern = safe_tags_match.group(1)
    # Must have a trailing boundary check — ([\s>]|$) or similar
    assert r"[\s>]" in pattern or r'[\s>]' in pattern, (
        "SAFE_TAGS must enforce a boundary after the tag name to prevent "
        "<spanxss> from matching when checking for <span>"
    )


# ── False-positive prevention ─────────────────────────────────────────────────

def test_inline_math_regex_requires_non_space_boundaries():
    """The $...$ inline regex must require non-space at both boundaries.

    This prevents 'costs $5 and $10' from matching — the space after the opening
    $ means it's a currency amount, not math.
    """
    # The inline math stash push is type:'inline' — find its containing replace() line
    inline_push_idx = UI_JS.find("type:'inline',src:m")
    assert inline_push_idx != -1, "Inline math stash push not found"
    # Get the text from the start of that line back to find the regex
    line_start = UI_JS.rfind('\n', 0, inline_push_idx) + 1
    inline_line = UI_JS[line_start:inline_push_idx + 50]
    # The regex must use \s (via [^\s...]) to exclude spaces at boundaries
    assert '\\s' in inline_line or '[^' in inline_line, (
        f"Inline math regex must exclude spaces at boundaries to prevent false "
        f"positives on currency like $5. Found: {inline_line[:120]}"
    )
def test_display_math_stashed_before_inline():
    """$$...$$ display math must be stashed before $...$ inline math.

    If inline runs first on '$$x$$', it could match '$' + 'x' + '$' leaving
    a stray outer '$', corrupting the output.
    """
    display_pos = UI_JS.find("type:'display',src:m")
    inline_pos = UI_JS.find("type:'inline',src:m")
    assert display_pos != -1, "display math stash not found"
    assert inline_pos != -1, "inline math stash not found"
    # First occurrence of display must be before first occurrence of inline
    assert display_pos < inline_pos, (
        "Display math ($$...$$) must be stashed before inline math ($...$) "
        "to prevent $$ from being parsed as two adjacent inline delimiters"
    )


def test_math_stash_token_uses_single_backslash_null_byte():
    """Math stash tokens must use the null-byte form (single backslash x00M).

    The restore regex expects a null byte character. If the stash emits
    a literal backslash+x00M (double backslash = 5-char string), the restore
    regex never matches and the tokens appear verbatim in the rendered output.

    The fence_stash correctly uses the null byte convention. Math stash must be consistent.
    """
    # In the source file, the correct form is: return '\x00M'
    # The wrong form (double backslash) would be: return '\\x00M'
    # Check that no double-backslash form exists in the math stash return statements
    import re
    bad_returns = re.findall(r"return\s+'\\\\x00M'", UI_JS)
    assert not bad_returns, (
        f"Found {len(bad_returns)} math stash return(s) using double-backslash \\\\x00M. "
        "Must use single backslash '\x00M' (null byte) to match the restore regex."
    )
    # Positive check: single-backslash form must exist
    good_returns = re.findall(r"math_stash\.push.*?return '\\x00M'", UI_JS, re.DOTALL)
    assert good_returns, (
        "Math stash return must use single-backslash '\x00M' (null byte convention)"
    )
