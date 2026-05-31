from pathlib import Path


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def test_operator_kanban_markup_exists_inside_kanban_view():
    html = _read("static/index.html")

    assert 'id="operatorKanbanPanel"' in html
    assert 'id="operatorKanbanBody"' in html
    assert 'id="operatorKanbanRefresh"' in html
    assert html.index('id="mainKanban"') < html.index('id="operatorKanbanPanel"') < html.index('id="kanbanTaskPreview"')
    assert "read-only" in html[html.index('id="operatorKanbanPanel"') : html.index('id="kanbanTaskPreview"')].lower()


def test_operator_kanban_script_loaded_after_proposals_before_boot():
    html = _read("static/index.html")

    assert html.index("static/operator_proposals.js") < html.index("static/operator_kanban.js") < html.index("static/boot.js")


def test_operator_kanban_js_uses_get_endpoint_only_and_no_mutation_tokens():
    js = _read("static/operator_kanban.js")
    compact = js.replace(" ", "")

    assert "/api/operator/kanban" in js
    assert "api(" in js
    assert "fetch(" not in js
    assert "http://" not in js
    assert "https://" not in js
    for token in ["method:'POST'", 'method:"POST"', "method:'PATCH'", 'method:"PATCH"', "method:'DELETE'", 'method:"DELETE"']:
        assert token not in compact
    forbidden = [
        "/api/kanban/dispatch",
        "/api/kanban/tasks",
        "/api/chat/start",
        "runKanbanDispatcher",
        "nudgeKanbanDispatcher",
        "createKanbanTask",
        "updateKanbanTask",
        "addKanbanComment",
        "setInterval",
        "setTimeout",
        "EventSource",
        "send(",
        "sendMessage",
    ]
    for token in forbidden:
        assert token not in js


def test_operator_kanban_js_does_not_assign_raw_payload_inner_html():
    js = _read("static/operator_kanban.js")

    assert ".textContent" in js
    assert ".innerHTML" not in js
    assert "payload.innerHTML" not in js
    assert "task.innerHTML" not in js


def test_operator_kanban_refresh_hook_is_best_effort_from_load_kanban():
    panels = _read("static/panels.js")

    assert "refreshOperatorKanban" in panels
    assert "typeof refreshOperatorKanban === 'function'" in panels
    hook = panels[panels.index("refreshOperatorKanban") - 200 : panels.index("refreshOperatorKanban") + 240]
    assert "try" in hook
    assert "catch" in hook


def test_operator_kanban_css_exists_and_has_state_rules():
    css = _read("static/style.css")

    for selector in [
        ".operator-kanban-panel",
        ".operator-kanban-header",
        ".operator-kanban-body",
        ".operator-kanban-counts",
        ".operator-kanban-task-card",
        ".operator-kanban-chip",
        ".operator-kanban-issue",
    ]:
        assert selector in css
    for state in ["state-live", "state-stale", "state-unknown"]:
        assert state in css
    assert "max-width:640px" in css
