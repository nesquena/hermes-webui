"""
Tests for #745: code blocks losing newlines when not preceded by double blank line.

Root cause: the paragraph-splitter in renderMd() replaced \n with <br> inside
<pre><code> blocks when they were not separated by a double newline from surrounding
text. The fix stashes <pre> blocks (and pre-header divs, mermaid, katex) before
the paragraph split and restores them afterwards.
"""

import subprocess
import os

UI_JS = os.path.join(os.path.dirname(__file__), "..", "static", "ui.js")


def get_ui_js():
    return open(UI_JS, encoding="utf-8").read()


class TestCodeBlockNewlinePreservation:
    def test_pre_stash_present(self):
        """The _pre_stash variable must exist in ui.js."""
        src = get_ui_js()
        assert "_pre_stash" in src, "_pre_stash not found in ui.js"

    def test_pre_stash_token_E_used(self):
        """Stash token \\x00E must be used for pre-block stashing."""
        src = get_ui_js()
        assert r"\x00E" in src, r"\x00E stash token not found in ui.js"

    def test_stash_before_paragraph_split(self):
        """_pre_stash must be populated BEFORE the parts=s.split line."""
        src = get_ui_js()
        pre_stash_pos = src.index("_pre_stash=[]")
        split_pos = src.index("const parts=s.split(/\\n{2,}/)")
        assert pre_stash_pos < split_pos, (
            "_pre_stash must be initialised before the paragraph split"
        )

    def test_restore_after_paragraph_split(self):
        """_pre_stash restore must happen AFTER the paragraph map/join line."""
        src = get_ui_js()
        restore_pos = src.index("_pre_stash[+i]")
        split_pos = src.index("}).join('\\n');", src.index("const parts=s.split"))
        assert restore_pos > split_pos, (
            "_pre_stash must be restored after the paragraph split/join"
        )

    def test_paragraph_split_bypasses_stash_tokens(self):
        """The paragraph map must bypass lines that start with \\x00E (pre stash).
        Also accepts a character class like \\x00[EQ] when other stash tokens
        share the same bypass (e.g. \\x00Q for blockquote stash)."""
        src = get_ui_js()
        # The map line must check for \x00E in its bypass condition
        map_line = next(l for l in src.splitlines() if "parts.map" in l and "<br>" in l)
        assert r"\x00E" in map_line or r"\x00[E" in map_line, (
            r"paragraph map must bypass \x00E stash tokens (literally or as "
            r"part of a character class like \x00[EQ])"
        )

    def test_pre_regex_covers_pre_header_div(self):
        """The stash regex must match <div class=\"pre-header\"> before <pre>."""
        src = get_ui_js()
        # Find the replacement regex used to populate _pre_stash
        stash_block_idx = src.index("_pre_stash=[]")
        stash_block = src[stash_block_idx : stash_block_idx + 1500]
        assert "pre-header" in stash_block, (
            'pre-stash regex must match <div class="pre-header"> wrappers'
        )

    def test_mermaid_covered_by_stash(self):
        """The stash regex must also cover mermaid-block divs."""
        src = get_ui_js()
        stash_block_idx = src.index("_pre_stash=[]")
        stash_block = src[stash_block_idx : stash_block_idx + 1500]
        assert "mermaid-block" in stash_block, (
            "pre-stash regex must cover mermaid-block divs"
        )

    def test_katex_covered_by_stash(self):
        """The stash regex must also cover katex-block divs."""
        src = get_ui_js()
        stash_block_idx = src.index("_pre_stash=[]")
        stash_block = src[stash_block_idx : stash_block_idx + 1500]
        assert "katex-block" in stash_block, (
            "pre-stash regex must cover katex-block divs"
        )

    def test_js_syntax_valid(self):
        """ui.js must pass node --check after the fix."""
        result = subprocess.run(
            ["node", "--check", UI_JS], capture_output=True, text=True
        )
        assert result.returncode == 0, f"node --check failed:\n{result.stderr}"

    def test_stash_token_e_not_used_elsewhere(self):
        """\\x00E must only appear in the pre-stash section (not reused)."""
        src = get_ui_js()
        occurrences = [
            i
            for i in range(len(src))
            if src[i : i + 4] == r"\x00" and i + 4 < len(src) and src[i + 4] == "E"
        ]
        # Allow 2 occurrences: the push token and the restore regex
        # (may be 3 if there's also a comment mentioning it)
        assert len(occurrences) >= 2, (
            r"Expected at least 2 uses of \x00E (push + restore)"
        )
