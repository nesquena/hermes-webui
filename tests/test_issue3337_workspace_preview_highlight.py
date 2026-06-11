"""Regression tests for workspace file-preview syntax highlighting (#3337).

#3337 added Prism.js syntax highlighting to the workspace code preview. The
contributor PR shipped no test of its own, and a maintainer browser-test caught
a cross-file highlight-leak bug: Prism.highlightElement() propagates the
language-* class onto the parent <pre>, so previewing a .css file then a .txt
file rendered the plain text with CSS grammar. These tests pin both the feature
and the fix.
"""
from pathlib import Path

WORKSPACE_JS = (Path(__file__).resolve().parent.parent / "static" / "workspace.js").read_text(encoding="utf-8")


def _open_file_body() -> str:
    start = WORKSPACE_JS.index("async function openFile(")
    # The plain code/text branch is the last else block before the function ends.
    return WORKSPACE_JS[start:start + 8000]


def test_workspace_preview_assigns_prism_language_class():
    body = _open_file_body()
    assert "_prismLanguageForPath(path)" in body
    assert "codeEl.className='language-'+lang" in body
    assert "Prism.highlightElement(codeEl)" in body


def test_prism_language_map_covers_common_extensions():
    assert "_PRISM_LANG_MAP" in WORKSPACE_JS
    for ext_token in ["py:'python'", "js:'javascript'", "css:'css'", "json:'json'"]:
        assert ext_token in WORKSPACE_JS, f"language map should include {ext_token}"
    # txt/log/csv intentionally map to '' (no highlighting).
    assert "txt:''" in WORKSPACE_JS


def test_prism_language_path_fallback_covers_extensionless_code_filenames():
    """Common code/config filenames without useful extensions should still
    activate Prism grammar selection in workspace previews (#3365).
    """
    assert "_PRISM_BASENAME_LANG_MAP" in WORKSPACE_JS
    expected = {
        "Dockerfile": "docker",
        "Makefile": "makefile",
        "makefile": "makefile",
        "GNUmakefile": "makefile",
        "CMakeLists.txt": "cmake",
        ".gitignore": "ignore",
        ".dockerignore": "ignore",
    }
    for filename, language in expected.items():
        assert f"{filename.lower()!r}:{language!r}" in WORKSPACE_JS
    assert "base.startsWith('dockerfile.')" in WORKSPACE_JS


def test_plain_text_files_do_not_inherit_prior_file_highlighting():
    """Cross-file leak fix: a plain-text preview after a code preview must not
    inherit the previous file's language. Two guards make this hold:

    1. The <pre> ancestor's stale language-* class is stripped before each render
       (Prism propagates the class to the parent, so it would otherwise leak).
    2. Prism.highlightElement is only called when a non-empty language was
       assigned, so a class-less <code> never walks up to an ancestor class.
    """
    body = _open_file_body()
    # Guard 1: stale language-* token stripped from the <pre> before append.
    assert "pre.className=pre.className.replace(/\\blanguage-\\S+/g,'')" in body, (
        "previewCode <pre> must have stale language-* classes stripped each render"
    )
    # Guard 2: highlightElement gated on a truthy lang.
    assert "if(lang&&typeof Prism!=='undefined'&&typeof Prism.highlightElement==='function')" in body, (
        "Prism.highlightElement must only run when a language was assigned"
    )
