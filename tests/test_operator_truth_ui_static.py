import re
from pathlib import Path


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _css_rule(css, selector):
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}", css)
    return match.group("body") if match else ""


def _container_block(css, query):
    match = re.search(rf"@container\s+{re.escape(query)}\s*\{{", css)
    if not match:
        return ""
    start = match.end() - 1
    depth = 0
    for index in range(start, len(css)):
        char = css[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1:index]
    return ""


def test_operator_truth_markup_exists_in_composer_footer():
    html = _read("static/index.html")

    assert 'id="operatorTruthStrip"' in html
    assert 'id="operatorTruthSummaryChip"' in html
    assert 'id="operatorTruthBoardChip"' in html
    assert 'id="operatorTruthScratchChip"' in html
    assert html.index('id="composerWorkspaceGroup"') < html.index('id="operatorTruthStrip"') < html.index('id="composerMobileConfigBtn"')


def test_operator_truth_script_is_loaded_after_ui_before_boot():
    html = _read("static/index.html")

    assert html.index("static/ui.js") < html.index("static/operator_truth.js") < html.index("static/boot.js")
    assert html.index("static/workspace.js") < html.index("static/operator_truth.js")
    assert html.index("static/panels.js") < html.index("static/operator_truth.js")


def test_operator_truth_js_uses_relative_api_endpoint():
    js = _read("static/operator_truth.js")

    assert "'/api/operator/truth'" in js or '"/api/operator/truth"' in js
    assert "http://" not in js
    assert "https://" not in js


def test_operator_truth_js_has_live_stale_unknown_states():
    js = _read("static/operator_truth.js")

    assert "state-live" in js
    assert "state-stale" in js
    assert "state-unknown" in js


def test_operator_truth_fetch_failure_sets_unknown_not_live():
    js = _read("static/operator_truth.js")

    assert "function setOperatorTruthUnknown" in js
    assert "Truth unknown" in js
    unknown_body = js[js.index("function setOperatorTruthUnknown") :]
    assert "state-live" not in unknown_body.split("function ", 1)[0]


def test_operator_truth_refresh_is_throttled():
    js = _read("static/operator_truth.js")

    assert "OPERATOR_TRUTH_TTL_MS" in js
    assert "30000" in js
    assert "_operatorTruthInFlight" in js
    assert "_operatorTruthLastFetchAt" in js


def test_operator_truth_refresh_cache_is_keyed_to_context():
    js = _read("static/operator_truth.js")

    assert "function operatorTruthContextKey" in js
    assert "_operatorTruthLastFetchKey" in js
    assert "_operatorTruthInFlightKey" in js
    assert "contextKey === _operatorTruthInFlightKey" in js
    assert "contextKey === _operatorTruthLastFetchKey" in js
    assert "requestKey !== operatorTruthContextKey()" in js
    assert "_profileDefaultWorkspace" in js
    assert "_profileSwitchWorkspace" in js


def test_operator_truth_payload_values_use_text_content_not_inner_html():
    js = _read("static/operator_truth.js")

    assert ".textContent" in js
    assert ".innerHTML" not in js


def test_sync_topbar_hooks_operator_truth_refresh_guarded():
    ui = _read("static/ui.js")

    assert "typeof refreshOperatorTruth === 'function'" in ui
    assert "refreshOperatorTruth({reason:'syncTopbar'})" in ui or 'refreshOperatorTruth({ reason: "syncTopbar" })' in ui


def test_kanban_board_load_refreshes_operator_truth_guarded():
    panels = _read("static/panels.js")
    load_boards = panels[panels.index("async function loadKanbanBoards") : panels.index("function _kanbanSafeColor")]

    assert "typeof refreshOperatorTruth === 'function'" in load_boards
    assert "kanban-board-load" in load_boards
    assert load_boards.index("_kanbanCurrentBoard") < load_boards.index("refreshOperatorTruth")


def test_operator_truth_mobile_css_collapses_secondary_chips():
    css = _read("static/style.css")

    assert ".operator-truth-strip" in css
    assert ".operator-truth-chip-secondary" in css
    assert "state-live" in css
    assert "state-stale" in css
    assert "state-unknown" in css
    assert "max-width:640px" in css or "composer-footer (max-width:" in css
    assert ".operator-truth-chip-secondary{display:none" in css.replace(" ", "") or ".operator-truth-chip-secondary { display: none" in css


def test_operator_truth_controls_wrap_in_composer_footer_instead_of_horizontal_treadmill():
    css = _read("static/style.css")
    compact = css.replace(" ", "")

    composer_left = _css_rule(css, ".composer-left").replace(" ", "")
    strip_raw = _css_rule(css, ".operator-truth-strip")
    strip = strip_raw.replace(" ", "")
    mid_width = _container_block(css, "composer-footer (max-width: 1100px)").replace(" ", "")

    assert "flex-wrap:wrap" in composer_left
    assert "overflow-x:visible" in composer_left
    assert "display:flex" in strip
    assert "flex-wrap:wrap" in strip
    assert "flex:1 1" in strip_raw
    assert "max-width:100%" in strip
    assert "@containercomposer-footer(max-width:1100px)" in compact
    assert ".operator-truth-strip" in mid_width
    assert "flex-basis:100%" in mid_width
    assert "order:10" in mid_width
