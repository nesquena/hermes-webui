"""Regression: composer drop-zone overlay is clean + context-aware (#3424 polish).

The composer drop hint used to be a translucent overlay with hardcoded text
("Drop files to upload to workspace") — when you dragged a workspace file over
the footer, the composer's own controls (textarea, chips, icons) bled through
and collided with the hint text, looking garbled. The fix:
  1. The overlay background is fully opaque (stacked input-bg over --bg), so
     nothing behind it shows through.
  2. The hint text is context-aware: a workspace-file drag (application/ws-path,
     which inserts an @path reference) says "insert workspace reference"; an
     OS-file drag (which attaches the file) says "attach".

Source-contract assertions (the project has no JS DOM-test runtime).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_drop_hint_overlay_is_opaque():
    """The .drop-hint overlay must use an opaque background (stacked over --bg)
    so the composer controls behind it cannot bleed through. A bare translucent
    token like `background:var(--input-bg)` (which is ~0.04 alpha in dark theme)
    is the regression we're guarding against."""
    import re
    m = re.search(r"\.drop-hint\{[^}]*\}", STYLE_CSS)
    assert m, ".drop-hint rule not found"
    rule = m.group(0)
    assert "linear-gradient(var(--input-bg),var(--input-bg)),var(--bg)" in rule, (
        ".drop-hint must use an opaque stacked background so composer controls "
        "don't bleed through during a drag; got: " + rule
    )


def test_drop_hint_has_dedicated_text_span():
    """The hint text lives in its own #dropHintText span so the handler can
    swap it per drag type."""
    assert 'id="dropHintText"' in INDEX_HTML, (
        "index.html must give the drop-hint label its own #dropHintText span"
    )
    # The stale hardcoded copy must be gone.
    assert "Drop files to upload to workspace" not in INDEX_HTML, (
        "the old hardcoded drop-hint text should be replaced by the dynamic span"
    )


def test_drop_hint_text_is_context_aware():
    """The composer dragenter handler sets the hint text based on drag type:
    workspace-file (ws-path) -> insert reference; OS file -> attach."""
    # Find the dragenter handler block.
    idx = PANELS_JS.find("addEventListener('dragenter'")
    assert idx != -1, "composer dragenter handler not found"
    block = PANELS_JS[idx:idx + 700]
    assert "application/ws-path" in block, "handler must distinguish the ws-path drag"
    assert "dropHintText" in block, "handler must update the #dropHintText label"
    assert "workspace reference" in block, "ws-path drag hint must mention inserting a reference"
    assert "attach" in block, "OS-file drag hint must mention attaching"


def test_os_upload_binder_composes_not_overwrites_move_binder():
    """Regression (Codex CORE catch): _bindWorkspaceOsUploadDropTarget must use
    addEventListener, NOT el.on*= property assignment. The move-drop binder
    (_bindWorkspaceMoveDropTarget) and the OS-upload binder run on the SAME
    folder-row / breadcrumb element; if the OS-upload binder assigned el.ondrop
    it would clobber the move handler and a workspace-file drag would fall
    through to the composer (@path insert) instead of moving the file."""
    WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    start = WORKSPACE_JS.find("function _bindWorkspaceOsUploadDropTarget(")
    assert start != -1, "_bindWorkspaceOsUploadDropTarget not found"
    # Bound the slice to the end of THIS function: scan brace depth from the
    # opening '{' so we don't spill into the following top-level code.
    open_brace = WORKSPACE_JS.find("{", start)
    depth = 0
    end = open_brace
    for i in range(open_brace, len(WORKSPACE_JS)):
        c = WORKSPACE_JS[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    body = WORKSPACE_JS[start:end]
    # Must register via addEventListener for drop (composes with move handler)...
    assert "addEventListener('drop'" in body, (
        "OS-upload binder must use addEventListener('drop', ...) so it composes "
        "with the move-drop handler instead of overwriting el.ondrop"
    )
    # ...and must NOT use the clobbering property-assignment form.
    assert "el.ondrop" not in body and "el.ondragover" not in body, (
        "OS-upload binder must not assign el.on* (it would overwrite the move binder)"
    )
