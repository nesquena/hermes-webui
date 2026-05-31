from pathlib import Path


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def test_operator_proposal_markup_exists_near_truth_strip():
    html = _read("static/index.html")

    assert 'id="operatorProposalChip"' in html
    assert 'id="operatorProposalPopover"' in html
    assert 'id="operatorProposalList"' in html
    assert html.index('id="operatorTruthStrip"') < html.index('id="operatorTruthScratchChip"') < html.index('id="operatorProposalChip"')
    assert html.index('id="operatorProposalChip"') < html.index('id="composerMobileConfigBtn"')


def test_operator_proposal_chip_uses_proposals_label_not_actions_alias():
    html = _read("static/index.html")
    js = _read("static/operator_proposals.js")

    assert 'id="operatorProposalLabel">Proposals</span>' in html
    assert '<div class="operator-proposal-title">Proposals</div>' in html
    assert 'id="operatorProposalLabel">Actions</span>' not in html
    assert "count ? 'Proposals ' + count : 'Proposals'" in js
    assert "count ? 'Actions ' + count : 'Actions'" not in js


def test_operator_proposals_script_loaded_after_truth_before_boot():
    html = _read("static/index.html")

    assert html.index("static/operator_truth.js") < html.index("static/operator_proposals.js") < html.index("static/boot.js")


def test_operator_proposals_js_uses_relative_get_endpoint_only():
    js = _read("static/operator_proposals.js")
    compact = js.replace(" ", "")

    assert "'/api/operator/proposals'" in js or '"/api/operator/proposals"' in js
    assert "http://" not in js
    assert "https://" not in js
    assert "method:'POST'" not in compact
    assert 'method:"POST"' not in compact
    assert "fetch('/api/operator/proposals'" not in js


def test_operator_proposals_payload_values_use_text_content_not_inner_html():
    js = _read("static/operator_proposals.js")

    assert ".textContent" in js
    assert ".innerHTML" not in js


def test_operator_proposals_never_executes_chat_kanban_cron_or_send():
    js = _read("static/operator_proposals.js")
    forbidden = [
        "/api/chat/start",
        "/api/chat",
        "/api/kanban/dispatch",
        "/api/kanban/tasks",
        "/api/cron",
        "cron",
        "send(",
        "sendMessage",
        "submitKanban",
        "createKanban",
        "dispatch",
    ]
    for token in forbidden:
        assert token not in js


def test_operator_proposals_fetch_is_manual_not_boot_polling():
    js = _read("static/operator_proposals.js")

    assert "function toggleOperatorProposals" in js
    assert "function refreshOperatorProposals" in js
    assert "window.toggleOperatorProposals" in js
    assert "DOMContentLoaded" not in js or "refreshOperatorProposals" not in js.split("DOMContentLoaded", 1)[1]
    assert "setInterval" not in js
    assert "setTimeout(refreshOperatorProposals" not in js


def test_operator_proposals_draft_writes_composer_without_sending():
    js = _read("static/operator_proposals.js")
    body = js[js.index("function draftOperatorProposal") :]

    assert "msg.value" in body
    assert "proposal.handoff_prompt" in body
    assert "focus()" in body
    assert "updateSendBtn" in body
    assert "send(" not in body
    assert "/api/chat" not in body


def test_operator_proposals_commitment_promote_is_draft_only_until_form_submit():
    js = _read("static/operator_proposals.js")
    compact = js.replace(" ", "")

    assert "Promote to commitment" in js
    assert "openOperatorCommitmentPromote" in js
    assert "/api/operator/commitments/promote" not in js
    assert "method:'POST'" not in compact
    assert 'method:"POST"' not in compact
    for token in [
        "/api/kanban/",
        "/api/chat/start",
        "/api/chat",
        "/api/cron",
        "/api/crons",
        "/api/goal",
        "runKanbanDispatcher",
        "nudgeKanbanDispatcher",
        "createKanbanTask",
        "send(",
        "sendMessage",
    ]:
        assert token not in js


def test_operator_proposals_css_has_popover_cards_and_mobile_rules():
    css = _read("static/style.css")

    assert ".operator-proposal-popover" in css
    assert ".operator-proposal-card" in css
    assert ".operator-proposal-actions" in css
    assert ".operator-proposal-no-exec" in css
    assert "max-width:640px" in css or "composer-footer (max-width:" in css
