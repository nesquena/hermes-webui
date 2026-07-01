import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_update_busy_hint_bar_exists():
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert "_updateBusyHintBar" in src


def test_handle_busy_hint_pill_exists():
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert "_handleBusyHintPill" in src


def test_busy_hint_bar_in_html():
    src = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "busyHintBar" in src


def test_busy_hint_bar_css():
    src = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert ".busy-hint-bar" in src
    assert ".busy-hint-bar[hidden]" in src
    assert ".busy-hint-pill" in src
    assert ".busy-hint-pill[hidden]" in src


def test_busy_hint_pills_have_actions():
    src = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for action in ["interrupt", "queue", "steer", "new_chat"]:
        assert f'data-action="{action}"' in src, f"Missing pill for action: {action}"
