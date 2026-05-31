import re
from pathlib import Path


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _opening_tag_with_id(markup, element_id):
    match = re.search(rf"<[a-zA-Z][^>]*\bid=[\"']{re.escape(element_id)}[\"'][^>]*>", markup)
    assert match, f"opening tag for {element_id} not found"
    return match.group(0)


def test_memory_skill_review_markup_exists_after_commitment_chip_and_popover():
    html = _read("static/index.html")

    assert 'id="operatorMemorySkillReviewChip"' in html
    assert 'id="operatorMemorySkillReviewPopover"' in html
    assert 'id="operatorMemorySkillReviewList"' in html
    assert 'id="operatorMemorySkillReviewRefresh"' in html
    chip_tag = _opening_tag_with_id(html, "operatorMemorySkillReviewChip")
    refresh_tag = _opening_tag_with_id(html, "operatorMemorySkillReviewRefresh")
    assert 'onclick="toggleOperatorMemorySkillReview' in chip_tag
    assert 'onclick="refreshOperatorMemorySkillReview' in refresh_tag
    assert html.index('id="operatorCommitmentChip"') < html.index('id="operatorMemorySkillReviewChip"') < html.index('id="composerMobileConfigBtn"')
    assert html.index('id="operatorCommitmentPopover"') < html.index('id="operatorMemorySkillReviewPopover"')


def test_memory_skill_review_script_loaded_after_commitments_before_boot():
    html = _read("static/index.html")

    assert html.index("static/operator_commitments.js") < html.index("static/operator_memory_skill_review.js") < html.index("static/boot.js")


def test_memory_skill_review_shows_local_only_no_apply_safety_copy():
    html = _read("static/index.html").lower()
    js = _read("static/operator_memory_skill_review.js").lower()
    combined = html + "\n" + js

    assert "local review only" in combined
    assert "no apply" in combined
    assert "would_execute:false" in combined


def test_memory_skill_review_js_uses_review_queue_endpoints_only():
    js = _read("static/operator_memory_skill_review.js")
    compact = "".join(js.split())
    allowed_endpoints = {
        "/api/operator/memory-skill-review",
        "/api/operator/memory-skill-review/decision",
    }

    for endpoint in allowed_endpoints:
        assert f"'{endpoint}'" in js or f'"{endpoint}"' in js
    quoted_endpoints = set(re.findall(r"[\"'`](/api/[^\"'`\s]+)[\"'`]", js))
    assert quoted_endpoints
    assert quoted_endpoints <= allowed_endpoints
    api_substrings = set(re.findall(r"/api/[^\s\"'`),;]+", js))
    assert api_substrings
    assert api_substrings <= allowed_endpoints
    assert "api(" in compact
    for token in ["fetch(", "XMLHttpRequest", "newRequest", "sendBeacon"]:
        assert token not in compact
    assert "http://" not in js
    assert "https://" not in js
    assert "method:'POST'" in compact or 'method:"POST"' in compact


def test_memory_skill_review_js_no_direct_memory_skill_mutation_apply_kanban_chat_cron_goal_or_background_tokens():
    js = _read("static/operator_memory_skill_review.js")
    compact = "".join(js.split())
    forbidden = [
        "/api/memory/write",
        "/api/skills/save",
        "/api/skills/delete",
        "/api/skills/toggle",
        "/apply",
        "would_execute:true",
        "/api/chat",
        "/api/chat/start",
        "/api/kanban/",
        "/api/kanban/dispatch",
        "/api/cron",
        "/api/crons",
        "/api/goal",
        "/api/background",
        "/background",
        "background",
        "runKanbanDispatcher",
        "nudgeKanbanDispatcher",
        "createKanbanTask",
        "updateKanbanTask",
        "addKanbanComment",
        "send(",
        "sendMessage",
        "startChat",
        "submitChat",
        "window.send",
        "submitMemorySave",
        "saveSkillForm",
        "deleteCurrentSkill",
        "toggleSkill",
        "setInterval",
        "setTimeout",
        "EventSource",
        "WebSocket",
        "Worker(",
        "navigator.sendBeacon",
        ".innerHTML",
        ".outerHTML",
        "insertAdjacentHTML",
        "renderMd(",
        "eval(",
        "Function(",
    ]
    for token in forbidden:
        assert token.replace(" ", "") not in compact


def test_memory_skill_review_values_use_text_content_not_inner_html_or_markdown():
    js = _read("static/operator_memory_skill_review.js")

    assert ".textContent" in js
    assert "document.createElement" in js
    for token in [
        ".innerHTML",
        ".outerHTML",
        "insertAdjacentHTML",
        "renderMd(",
        "renderMarkdown(",
        "marked(",
        "markdownToHtml",
    ]:
        assert token not in js


def test_memory_skill_review_manual_refresh_no_polling_timers_or_eventsource():
    js = _read("static/operator_memory_skill_review.js")

    assert "function toggleOperatorMemorySkillReview" in js
    assert "function refreshOperatorMemorySkillReview" in js
    assert "function renderOperatorMemorySkillReview" in js
    assert "window.toggleOperatorMemorySkillReview" in js
    assert "window.refreshOperatorMemorySkillReview" in js
    assert not re.search(r"(?m)^\s*refreshOperatorMemorySkillReview\s*\(\s*\)\s*;?\s*$", js)
    assert not re.search(r"(?m)^\s*window\.refreshOperatorMemorySkillReview\s*\(\s*\)\s*;?\s*$", js)
    for token in ["setInterval", "setTimeout", "EventSource", "WebSocket", "Worker(", "navigator.sendBeacon"]:
        assert token not in js
    if "DOMContentLoaded" in js:
        after_dom_ready = js.split("DOMContentLoaded", 1)[1]
        assert "refreshOperatorMemorySkillReview" not in after_dom_ready
        assert "toggleOperatorMemorySkillReview" not in after_dom_ready


def test_memory_skill_review_renders_required_review_fields_and_invalid_states():
    js = _read("static/operator_memory_skill_review.js")

    for field in [
        "proposed_change",
        "source_evidence",
        "classification",
        "durability",
        "stale_risk",
        "expires_at",
        "decision",
        "rollback",
        "previous_content",
        "would_execute",
        "invalid",
        "stale",
        "unknown",
        "issues",
    ]:
        assert field in js
    for class_name in [
        "operator-memory-skill-review-card",
        "operator-memory-skill-review-diff",
        "operator-memory-skill-review-evidence",
        "operator-memory-skill-review-decision",
        "operator-memory-skill-review-invalid",
        "operator-memory-skill-review-no-exec",
    ]:
        assert class_name in js


def test_memory_skill_review_css_has_card_diff_evidence_decision_invalid_and_mobile_rules():
    css = _read("static/style.css")

    for selector in [
        ".operator-memory-skill-review-popover",
        ".operator-memory-skill-review-list",
        ".operator-memory-skill-review-card",
        ".operator-memory-skill-review-diff",
        ".operator-memory-skill-review-evidence",
        ".operator-memory-skill-review-decision",
        ".operator-memory-skill-review-invalid",
        ".operator-memory-skill-review-no-exec",
        ".operator-memory-skill-review-action",
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
    assert any("operator-memory-skill-review" in section for section in mobile_sections)
