from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXTENSION_JS = (REPO / "extensions" / "project-os" / "project-os-extension.js").read_text(encoding="utf-8")


def test_project_os_global_slash_shortcut_does_not_steal_native_textarea_input():
    start = EXTENSION_JS.index("function onGlobalKeydown(event) {")
    end = EXTENSION_JS.index("if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === \"k\") {", start)
    keydown = EXTENSION_JS[start:end]
    slash_branch_start = keydown.index("if (\n      slashPressed &&")
    slash_branch = keydown[slash_branch_start:]
    assert "!inEditable" in slash_branch
    assert "(!inEditable || !insideExtension)" not in slash_branch
    assert 'focusFloatingComposer({ insertText: "/" });' in slash_branch


def test_project_os_extension_only_handles_slash_inside_its_own_composer_editable():
    start = EXTENSION_JS.index("function onGlobalKeydown(event) {")
    end = EXTENSION_JS.index("if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === \"k\") {", start)
    keydown = EXTENSION_JS[start:end]
    assert 'if (slashPressed && (isComposerEditable(target) || isComposerEditable(activeElement))) {' in keydown
    assert 'insertTextAtCursor(composer, "/");' in keydown
