"""Asset-closure tests for vendored xterm and Prism assets.

Ensures every locally referenced script, stylesheet, and Prism grammar
is actually committed. Prevents HTML references from pointing at a
missing file or a future _PRISM_LANG_MAP addition from silently
restoring a network request or producing a same-origin 404.
"""
import os
import re

import pytest


# --- Test 1: all local src/href targets in index.html exist on disk ---

def _local_asset_paths():
    """Parse local src= and href= values from index.html (skip http/https)."""
    with open("static/index.html") as f:
        src = f.read()
    # Match src="..." and href="..." in actual HTML tags only (not inside <script> blocks)
    # Remove inline script content first to avoid matching JS string literals
    cleaned = re.sub(r'<script[^>]*>.*?</script>', '', src, flags=re.DOTALL)
    refs = re.findall(r'(?:src|href)="([^"]+)"', cleaned)
    return [r for r in refs if not r.startswith(("http://", "https://", "data:"))]


def test_index_html_local_assets_exist():
    """Every local src/href in index.html must resolve to an existing file."""
    missing = []
    for path in _local_asset_paths():
        # Paths are relative to the repo root (where index.html is served from)
        # Strip leading ./ if present
        clean = path.split("?")[0]  # remove cache-busting query strings
        if clean.startswith("./"):
            clean = clean[2:]
        # Skip manifest.json and sw.js - generated at runtime or by build
        if clean in ("manifest.json", "sw.js"):
            continue
        if not os.path.isfile(clean):
            missing.append(clean)
    assert not missing, (
        "index.html references local assets that do not exist:\n"
        + "\n".join(f"  {m}" for m in missing)
    )


# --- Test 2: Prism grammar closure - every _PRISM_LANG_MAP language exists ---

# Autoloader dependency chains from prismjs@1.29.0.
# Only languages that have a prerequisite are listed.
_PRISM_DEPS = {
    "javascript": ["clike"],
    "jsx": ["javascript", "clike", "markup"],
    "tsx": ["jsx", "typescript", "javascript", "clike", "markup"],
    "typescript": ["javascript", "clike"],
    "csharp": ["clike"],
    "cpp": ["c", "clike"],
    "c": ["clike"],
    "java": ["clike"],
    "kotlin": ["clike"],
    "scala": ["java", "clike"],
    "swift": ["clike"],
    "go": ["clike"],
    "rust": ["clike"],
    "php": ["markup-templating", "markup", "clike"],
    "markdown": ["markup"],
}

# Prism language aliases - these map to another grammar file and don't
# have their own component. The autoloader handles this internally.
_PRISM_ALIASES = {
    "xml": "markup",
}

# The language values from _PRISM_LANG_MAP that actually map to a grammar
# (empty string means plaintext - no grammar needed).
_PRISM_LANG_MAP_VALUES = {
    "javascript", "jsx", "typescript", "tsx",
    "python", "ruby", "go", "rust", "java", "kotlin",
    "c", "cpp", "csharp", "swift", "scala",
    "php", "perl", "r", "lua",
    "bash", "powershell",
    "sql", "graphql",
    "json", "yaml", "toml", "xml",
    "markup", "css",
    "markdown",
    "docker",
    "diff",
}

_COMPONENTS_DIR = "static/vendor/prismjs/1.29.0/components"


def _all_required_grammars():
    """Expand _PRISM_LANG_MAP values and their deps into a full set of files needed."""
    required = set()
    stack = list(_PRISM_LANG_MAP_VALUES)
    while stack:
        lang = stack.pop()
        if not lang or lang in required:
            continue
        # Resolve aliases - the alias doesn't have its own file
        resolved = _PRISM_ALIASES.get(lang, lang)
        required.add(resolved)
        for dep in _PRISM_DEPS.get(resolved, []):
            if dep not in required:
                stack.append(dep)
    return required


@pytest.mark.parametrize("lang", sorted(_all_required_grammars()))
def test_prism_grammar_file_exists(lang):
    """Each required Prism grammar must have a vendored component file."""
    path = os.path.join(_COMPONENTS_DIR, f"prism-{lang}.min.js")
    assert os.path.isfile(path), (
        f"Missing vendored Prism grammar: {path}\n"
        f"Language '{lang}' is referenced by _PRISM_LANG_MAP or as a "
        f"dependency but the corresponding component file is not committed."
    )
