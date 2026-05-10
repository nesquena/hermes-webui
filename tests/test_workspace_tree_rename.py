from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_workspace_file_name_click_does_not_immediately_bubble():
    """Clicking a file name must not synchronously bubble to the row open handler
    before dblclick can fire. The fix originally landed as pure stopPropagation
    (#1698), then evolved to a 300ms debounce that delegates to el.onclick (#1707
    — the pure-stopPropagation form broke single-click activation entirely).

    Either shape satisfies the #1698 invariant. Accept both:
      - pre-#1707 shape: `nameEl.onclick=(e)=>e.stopPropagation();`
      - post-#1707 shape: any `nameEl.onclick=(e)=>{...stopPropagation()...setTimeout...}`
    """
    name_start = UI_JS.index("const nameEl=document.createElement('span');")
    dblclick_idx = UI_JS.index("nameEl.ondblclick=(e)=>", name_start)
    block = UI_JS[name_start:dblclick_idx]

    assert "nameEl.onclick" in block, (
        "workspace file-tree name span must bind nameEl.onclick to prevent the "
        "first click of a dblclick from triggering the row's openFile (#1698)"
    )
    # The bound handler must call stopPropagation (either the original simple form
    # or the post-#1707 debounce form that contains stopPropagation in its body).
    assert "stopPropagation" in block, (
        "nameEl.onclick must call stopPropagation so the row's el.onclick does not "
        "fire on the first click of a dblclick (#1698)"
    )


def test_workspace_file_row_click_still_opens_file_preview():
    """The row-level openFile binding must still exist — the nameEl handler delegates
    to it (post-#1707) or sits beneath it as a pure barrier (pre-#1707)."""
    assert "el.onclick=async()=>openFile(item.path);" in UI_JS
