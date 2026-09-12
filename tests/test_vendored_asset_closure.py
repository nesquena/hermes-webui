"""Asset-closure tests for vendored xterm and Prism assets.

Ensures every locally referenced script, stylesheet, and Prism grammar
is actually committed. Prevents HTML references from pointing at a
missing file or a future _PRISM_LANG_MAP addition from silently
restoring a network request or producing a same-origin 404.

The grammar closure test derives its values from the production maps
in static/workspace.js rather than maintaining a separate allowlist.
"""
import os
import re

import pytest


# --- Test 1: all local src/href targets in index.html exist on disk ---

def _local_asset_paths():
    """Parse local src= and href= values from index.html (skip http/https)."""
    with open("static/index.html") as f:
        src = f.read()
    # Remove inline script bodies to avoid matching JS string literals,
    # but preserve opening tags so src= attributes are still captured.
    cleaned = re.sub(
        r'(<script\b[^>]*>).*?</script>', r'\1</script>', src, flags=re.DOTALL | re.IGNORECASE
    )
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


# --- Test 2: Prism grammar closure - derived from production workspace.js ---

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
    "scss": ["css"],
    "sass": ["css"],
    "less": ["css"],
}

# Prism language aliases - these map to another grammar file and don't
# have their own component. The autoloader handles this internally.
_PRISM_ALIASES = {
    "xml": "markup",
}


def _parse_production_lang_values():
    """Extract unique non-empty language values from workspace.js maps."""
    with open("static/workspace.js") as f:
        src = f.read()

    # Extract _PRISM_LANG_MAP object literal
    m1 = re.search(r'const _PRISM_LANG_MAP\s*=\s*\{([^}]+)\}', src)
    assert m1, "_PRISM_LANG_MAP not found in workspace.js"

    # Extract _PRISM_BASENAME_LANG_MAP object literal
    m2 = re.search(r'const _PRISM_BASENAME_LANG_MAP\s*=\s*\{([^}]+)\}', src)
    assert m2, "_PRISM_BASENAME_LANG_MAP not found in workspace.js"

    # Parse all string values from both maps
    values = set()
    for body in (m1.group(1), m2.group(1)):
        # Match quoted values after colons: 'value' or "value"
        for val in re.findall(r":\s*['\"]([^'\"]*)['\"]", body):
            if val:  # skip empty string (plaintext, no grammar)
                values.add(val)

    return values


_COMPONENTS_DIR = "static/vendor/prismjs/1.29.0/components"


def _all_required_grammars():
    """Expand production map values and their deps into a full set of files needed."""
    lang_values = _parse_production_lang_values()
    required = set()
    stack = list(lang_values)
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
        f"Language '{lang}' is referenced by _PRISM_LANG_MAP or "
        f"_PRISM_BASENAME_LANG_MAP (or as a dependency) but the "
        f"corresponding component file is not committed."
    )
