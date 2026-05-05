from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_workspace_file_name_click_stops_before_dblclick_rename():
    """Clicking a file name must not bubble to the row open handler before dblclick rename."""
    name_start = UI_JS.index("const nameEl=document.createElement('span');")
    dblclick_idx = UI_JS.index("nameEl.ondblclick=(e)=>", name_start)
    click_idx = UI_JS.find("nameEl.onclick=(e)=>e.stopPropagation();", name_start, dblclick_idx)

    assert click_idx != -1, (
        "workspace file-tree name span must stop click propagation before its dblclick "
        "rename handler so the row openFile() click does not win the first click"
    )


def test_workspace_file_row_click_still_opens_file_preview():
    """Only the name span should swallow clicks; the rest of the file row still opens preview."""
    assert "el.onclick=async()=>openFile(item.path);" in UI_JS
