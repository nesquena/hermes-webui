from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).parent.parent
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")


def test_memory_panel_includes_external_memory_section():
    assert "key: 'external_memory'" in PANELS_JS
    assert "External Memory" in PANELS_JS
    assert "loadExternalMemoryReview(true)" in PANELS_JS


def test_external_memory_review_ui_uses_backend_endpoints():
    assert "/api/external-memory/providers" in PANELS_JS
    assert "/api/external-memory/candidates?" in PANELS_JS
    assert "state=candidate" in PANELS_JS
    assert "state=approved" in PANELS_JS
    assert "/api/external-memory/search?provider=" in PANELS_JS
    assert "/api/external-memory/candidates/${encodeURIComponent(id)}/approve" in PANELS_JS
    assert "/api/external-memory/candidates/${encodeURIComponent(id)}/reject" in PANELS_JS
    assert "/api/external-memory/candidates/${encodeURIComponent(id)}/edit" in PANELS_JS


def test_external_memory_review_ui_exposes_review_actions_and_metadata():
    for text in ["Candidates (", "Approved (", "Search results (", "Edit", "Approve", "Reject", "Delete"]:
        assert text in PANELS_JS
    assert "qdrant_point_id" in PANELS_JS
    assert "confidence" in PANELS_JS


def test_external_memory_review_ui_fetches_max_review_page_to_avoid_hidden_candidates():
    assert "state=candidate&limit=500" in PANELS_JS
    assert "state=approved&limit=500" in PANELS_JS


def test_external_memory_review_ui_uses_api_totals_for_section_counts():
    assert "candidateTotal" in PANELS_JS
    assert "approvedTotal" in PANELS_JS
    assert "Candidates (${candidateTotal})" in PANELS_JS
    assert "Approved (${approvedTotal})" in PANELS_JS
    assert "Showing ${candidates.length} of ${candidateTotal} pending candidates." in PANELS_JS


def test_external_memory_review_ui_uses_scrollable_review_lists():
    assert "externalMemoryCandidateList" in PANELS_JS
    assert "externalMemoryApprovedList" in PANELS_JS
    assert "overflow-y:auto" in PANELS_JS
    assert "max-height:55vh" in PANELS_JS


def test_external_memory_review_ui_shows_empty_state_without_builtin_provider():
    assert "No external memory providers configured yet" in PANELS_JS
    assert "external_memory_providers.json" in PANELS_JS
    assert "No providers configured" in PANELS_JS


def test_external_memory_review_ui_shows_semantic_format_guidance():
    assert "Semantic format" in PANELS_JS
    assert "&lt;Scope/Subject&gt; &lt;durable relation&gt; &lt;object/behavior&gt;" in PANELS_JS
    assert "one standalone sentence" in PANELS_JS
