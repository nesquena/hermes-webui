import re
from pathlib import Path


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _device_admin_js():
    return _read("static/operator_device_admin.js")


def _strip_js_strings_and_comments(js):
    js = re.sub(r"//.*", "", js)
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    return re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "''", js, flags=re.DOTALL)


def _top_level_calls(js, function_name):
    cleaned = _strip_js_strings_and_comments(js)
    calls = []
    depth = 0
    for line_number, line in enumerate(cleaned.splitlines(), start=1):
        line_depth = depth
        if re.search(rf"\bfunction\s+{re.escape(function_name)}\s*\(", line):
            pass
        elif line_depth == 0 and re.search(rf"\b{re.escape(function_name)}\s*\(", line):
            calls.append((line_number, line.strip()))
        depth += line.count("{") - line.count("}")
        depth = max(depth, 0)
    return calls


def _function_body(js, function_name):
    start = js.index(f"function {function_name}")
    close_paren = js.index(")", start)
    brace = js.index("{", close_paren)
    depth = 0
    for index in range(brace, len(js)):
        char = js[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return js[brace + 1 : index]
    raise AssertionError(f"function body not found: {function_name}")


def test_device_admin_markup_exists_after_docs_artifacts_chip_and_popover():
    html = _read("static/index.html")

    for element_id in [
        "operatorDeviceAdminChip",
        "operatorDeviceAdminLabel",
        "operatorDeviceAdminPopover",
        "operatorDeviceAdminStatus",
        "operatorDeviceAdminRefresh",
        "operatorDeviceAdminInput",
        "operatorDeviceAdminHost",
        "operatorDeviceAdminAction",
        "operatorDeviceAdminList",
        "operatorDeviceAdminPreview",
    ]:
        assert f'id="{element_id}"' in html

    assert html.index('id="operatorDocsArtifactsChip"') < html.index('id="operatorDeviceAdminChip"') < html.index('id="composerMobileConfigBtn"')
    assert html.index('id="operatorDocsArtifactsPopover"') < html.index('id="operatorDeviceAdminPopover"')


def test_device_admin_script_loaded_after_docs_artifacts_before_boot():
    html = _read("static/index.html")

    assert html.index("static/operator_docs_artifacts.js") < html.index("static/operator_device_admin.js") < html.index("static/boot.js")


def test_device_admin_css_has_popover_card_source_preview_action_states_and_mobile_rules():
    css = _read("static/style.css")

    for selector in [
        ".operator-device-admin-popover",
        ".operator-device-admin-list",
        ".operator-device-admin-card",
        ".operator-device-admin-source",
        ".operator-device-admin-preview",
        ".operator-device-admin-action",
        ".operator-device-admin-blocked",
        ".operator-device-admin-unknown",
    ]:
        assert selector in css

    mobile_starts = [
        match.start()
        for match in re.finditer(r"@media\s*\(\s*max-width\s*:\s*640px\s*\)", css)
    ]
    assert mobile_starts
    mobile_sections = [
        css[start : mobile_starts[index + 1] if index + 1 < len(mobile_starts) else len(css)]
        for index, start in enumerate(mobile_starts)
    ]
    assert any("operator-device-admin" in section for section in mobile_sections)


def test_device_admin_js_shell_exports_manual_functions_without_auto_refresh():
    js = _device_admin_js()

    for name in [
        "toggleOperatorDeviceAdmin",
        "hideOperatorDeviceAdmin",
        "refreshOperatorDeviceAdmin",
        "renderOperatorDeviceAdmin",
        "previewOperatorDeviceAdminAction",
        "initOperatorDeviceAdmin",
    ]:
        assert f"window.{name}" in js

    assert "if (typeof document !== 'undefined') initOperatorDeviceAdmin();" in js
    assert "setInterval" not in js
    assert "EventSource" not in js
    assert "WebSocket" not in js
    assert "Worker(" not in js


def test_device_admin_js_uses_only_device_admin_get_endpoints():
    js = _device_admin_js()
    allowed_endpoints = {"/api/operator/device-admin", "/api/operator/device-admin/preview"}
    quoted_api_paths = set(re.findall(r"[\"'`](/api/[A-Za-z0-9_./-]+)", js))

    assert quoted_api_paths == allowed_endpoints
    assert re.search(r"\bapi\s*\(\s*[\"'`]/api/operator/device-admin\b", js)
    assert not re.search(r"\bfetch\s*\(", js)
    assert "XMLHttpRequest" not in js
    assert "http://" not in js
    assert "https://" not in js
    for forbidden in [
        "POST",
        "PATCH",
        "DELETE",
        "/apply",
        "/approve",
        "/execute",
        "/run",
        "/dispatch",
        "/operator/commitments/promote",
        "/operator/memory-skill-review/decision",
        "/api/kanban",
        "/api/chat",
    ]:
        assert forbidden not in js


def test_device_admin_values_use_text_content_not_inner_html_or_markdown():
    js = _device_admin_js()

    assert ".textContent" in js
    assert "document.createElement" in js
    for forbidden in [
        ".innerHTML",
        ".outerHTML",
        "insertAdjacentHTML",
        "renderMd(",
        "renderMarkdown(",
        "marked(",
        "markdown",
        "eval(",
        "Function(",
    ]:
        assert forbidden not in js


def test_device_admin_manual_only_no_polling_timers_or_background_transports():
    js = _device_admin_js()

    for forbidden in [
        "setInterval",
        "setTimeout",
        "EventSource",
        "WebSocket",
        "Worker(",
        "navigator.sendBeacon",
    ]:
        assert forbidden not in js

    assert "DOMContentLoaded" not in js or not re.search(
        r"DOMContentLoaded[\s\S]*refreshOperatorDeviceAdmin\s*\(",
        js,
    )
    assert _top_level_calls(js, "refreshOperatorDeviceAdmin") == []


def test_device_admin_js_renders_required_catalog_receipt_and_approval_fields():
    js = _device_admin_js()

    for required in [
        "sources",
        "hosts",
        "paths",
        "dry_runs",
        "receipts",
        "issues",
        "approval_model",
        "required_fields",
        "execution_state",
        "would_execute",
        "blocked",
        "unknown",
        "source_path_id",
        "destination_path_id",
        "approval_required",
    ]:
        assert required in js

    assert "Dry-run preview" in js
    assert "No device action was executed" in js
    assert "_operatorDeviceAdminPreviewSeq" in js
    assert re.search(r"previewSeq\s*!==\s*_operatorDeviceAdminPreviewSeq|seq\s*!==\s*_operatorDeviceAdminPreviewSeq", js)


def test_device_admin_list_render_invalidates_inflight_preview_before_resetting_panel():
    js = _device_admin_js()
    body = _function_body(js, "renderOperatorDeviceAdmin")
    invalidation = re.search(r"(?:\+\+_operatorDeviceAdminPreviewSeq|_operatorDeviceAdminPreviewSeq\s*\+=\s*1)", body)

    assert invalidation
    assert body.index("_operatorDeviceAdminPreviewSeq") < body.index("renderOperatorDeviceAdminPreview")


def test_device_admin_first_manual_open_refreshes_even_after_init_default_render():
    js = _device_admin_js()
    toggle_body = _function_body(js, "toggleOperatorDeviceAdmin")
    refresh_body = _function_body(js, "refreshOperatorDeviceAdmin")

    assert "_operatorDeviceAdminHasLoadedPayload" in js
    assert "!_operatorDeviceAdminHasLoadedPayload" in toggle_body
    assert "_operatorDeviceAdminHasLoadedPayload = true" in refresh_body
    assert "!_operatorDeviceAdminLastPayload" not in toggle_body


def test_device_admin_path_cards_render_backend_display_path_field():
    js = _device_admin_js()
    body = _function_body(js, "_operatorDeviceAdminRenderPaths")

    assert "display_path" in body
    assert "item.display_path" in body
    assert "item.path" not in body


def test_device_admin_refresh_captures_filter_query_before_loading_render_mutates_filters():
    js = _device_admin_js()
    body = _function_body(js, "refreshOperatorDeviceAdmin")

    assert "const queryString = _operatorDeviceAdminQueryString();" in body
    assert body.index("const queryString = _operatorDeviceAdminQueryString();") < body.index("renderOperatorDeviceAdmin(_operatorDeviceAdminLoadingPayload()")
    assert "renderOperatorDeviceAdmin(_operatorDeviceAdminLoadingPayload(), {updateFilters: false})" in body
    assert "api('/api/operator/device-admin' + queryString)" in body
