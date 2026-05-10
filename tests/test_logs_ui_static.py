import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
INDEX = (REPO / "static" / "index.html").read_text(encoding="utf-8")
PANELS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
I18N = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", src)
    assert match, f"{name}() not found"
    brace = src.find("{", match.end())
    assert brace != -1, f"{name}() has no body"
    depth = 1
    i = brace + 1
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    while i < len(src) and depth:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in "'\"`":
            in_string = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() body did not close"
    return src[brace + 1 : i - 1]


def test_logs_tab_is_wired_between_insights_and_settings_in_rail_and_mobile_nav():
    rail = INDEX[
        INDEX.index('data-panel="insights"') : INDEX.index('<div class="rail-spacer"')
    ]
    assert 'data-panel="logs"' in rail
    assert rail.index('data-panel="insights"') < rail.index('data-panel="logs"')

    mobile_start = INDEX.index('class="sidebar-nav"')
    mobile_end = INDEX.index("<!-- Settings button mirrored here for mobile")
    mobile_nav = INDEX[mobile_start:mobile_end]
    assert 'data-panel="logs"' in mobile_nav
    assert mobile_nav.index('data-panel="insights"') < mobile_nav.index(
        'data-panel="logs"'
    )

    assert 'id="panelLogs"' in INDEX
    assert 'id="mainLogs"' in INDEX
    assert "tab_logs" in I18N


def test_logs_panel_fetches_allowlisted_api_and_exposes_controls():
    load_fn = _function_body(PANELS, "loadLogs")
    render_fn = _function_body(PANELS, "_renderLogs")
    selected_file_fn = _function_body(PANELS, "_selectedLogsFile")
    selected_tail_fn = _function_body(PANELS, "_selectedLogsTail")
    assert "api('/api/logs" in load_fn or 'api("/api/logs' in load_fn
    assert "logsFile" in selected_file_fn and "logsTail" in selected_tail_fn
    assert "agent" in INDEX and "errors" in INDEX and "gateway" in INDEX
    assert 'value="200" selected' in INDEX
    assert 'value="100"' in INDEX and 'value="500"' in INDEX and 'value="1000"' in INDEX
    assert "logsWrap" in INDEX
    assert "logsCopyAll" in INDEX
    assert "logsAutoRefresh" in INDEX
    assert "navigator.clipboard.writeText" in PANELS
    assert "logs-copy" in INDEX


def test_logs_autorefresh_runs_only_while_logs_tab_is_visible_and_enabled():
    start_fn = _function_body(PANELS, "_startLogsAutoRefresh")
    stop_fn = _function_body(PANELS, "_stopLogsAutoRefresh")
    assert "if (nextPanel === 'logs') await loadLogs();" in PANELS
    assert "_syncLogsAutoRefresh();" in PANELS
    assert "_logsAutoRefreshTimer" in PANELS
    assert "setInterval" in start_fn and "5000" in start_fn
    assert "_currentPanel !== 'logs'" in start_fn
    assert "clearInterval" in stop_fn


def test_logs_severity_coloring_prioritizes_explicit_log_level_before_message_text():
    severity_fn = _function_body(PANELS, "_logLineSeverityClass")
    # A WARNING message can legitimately contain words like "provider error";
    # color by the explicit level token, not by incidental message text.
    assert severity_fn.index("log-line-warning") < severity_fn.index("log-line-error")


def test_logs_severity_coloring_and_monospace_wrap_css_are_present():
    css_min = re.sub(r"\s+", "", CSS)
    assert ".logs-output{" in css_min
    assert "font-family" in css_min and "monospace" in css_min
    assert ".logs-output.wrap" in css_min and "white-space:pre-wrap" in css_min
    for cls in (
        "log-line-error",
        "log-line-warning",
        "log-line-info",
        "log-line-debug",
    ):
        assert f".{cls}" in css_min


def test_logs_source_fixtures_do_not_bake_private_log_content():
    combined = "\n".join(
        (REPO / path).read_text(encoding="utf-8")
        for path in (
            "tests/test_logs_endpoint.py",
            "tests/test_logs_ui_static.py",
            "static/index.html",
            "static/panels.js",
        )
    )
    assert "/home/" + "michael/.hermes/logs" not in combined
    for name in ("agent", "gateway", "errors"):
        assert name + ".log:" not in combined
