from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_html_helper_escapes_interpolations_and_is_used_for_workspace_artifacts():
    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    workspace = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    assert "function html(strings,...values)" in ui
    assert "?esc(values[i])" in ui
    assert "items.map(item => html`" in workspace


def test_no_raw_console_log_or_debug_in_static_js_except_guard():
    offenders = []
    for path in (ROOT / "static").glob("*.js"):
        src = path.read_text(encoding="utf-8")
        for needle in ("console.log(", "console.debug("):
            idx = src.find(needle)
            while idx != -1:
                line = src.count("\n", 0, idx) + 1
                line_text = src.splitlines()[line - 1]
                if "window.__HERMES_DEBUG__" not in line_text:
                    offenders.append(f"{path.name}:{line}:{needle}")
                idx = src.find(needle, idx + 1)
    assert not offenders
