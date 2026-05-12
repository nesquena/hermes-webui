import json
from pathlib import Path

from tools.book_expert_factory import (
    classify_book_text,
    create_expert_blueprint,
    register_book_source,
    source_hierarchy,
    validate_team_routing,
)


def test_classify_book_text_identifies_domain_and_skillability():
    text = """
    Customer interviews, startup idea validation, market risk, product experiments.
    Ask about past behavior, avoid compliments, avoid leading questions, test assumptions.
    """

    result = classify_book_text(title="The Mom Test", text=text)

    assert result["primary_domain"] == "startup-customer-discovery"
    assert result["skillability"] == "high"
    assert "diagnose" in result["recommended_skill_pair"]
    assert result["role_suggestion"] == "customer-discovery-expert"


def test_register_book_source_creates_versioned_candidate_registry(tmp_path):
    book = tmp_path / "mom-test-notes.txt"
    book.write_text(
        "The Mom Test\nCustomer interviews. Avoid leading questions. Ask about past behavior.",
        encoding="utf-8",
    )
    registry = tmp_path / "registry"

    record = register_book_source(book, registry_root=registry, title="The Mom Test", author="Rob Fitzpatrick")

    out = Path(record["path"])
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["schema_version"] == 1
    assert data["title"] == "The Mom Test"
    assert data["source_type"] == "book"
    assert data["verification"]["file_exists"] is True
    assert data["status"] == "candidate_unverified_framework"
    assert data["classification"]["primary_domain"] == "startup-customer-discovery"
    assert data["version"] == "0.1.0"


def test_register_book_source_extracts_epub_samples(tmp_path):
    import zipfile

    book = tmp_path / "agentic-cybersecurity.epub"
    with zipfile.ZipFile(book, "w") as epub:
        epub.writestr(
            "OEBPS/chapter1.xhtml",
            "<html><body><h1>Agentic AI for Cybersecurity</h1><p>forensic evidence, chain of custody, legal preservation, hash, court, and cybersecurity method checklist.</p></body></html>",
        )
    registry = tmp_path / "registry"

    record = register_book_source(book, registry_root=registry, title="Agentic AI for Cybersecurity", author="Omar Santos")

    data = json.loads(Path(record["path"]).read_text())
    assert data["verification"]["sample_extracted"] is True
    assert data["classification"]["primary_domain"] == "legal-forensic-evidence"


def test_create_expert_blueprint_merges_multiple_books_with_web_update_layer():
    books = [
        {
            "title": "The Mom Test",
            "author": "Rob Fitzpatrick",
            "classification": {"primary_domain": "startup-customer-discovery", "role_suggestion": "customer-discovery-expert"},
        },
        {
            "title": "Good Strategy Bad Strategy",
            "author": "Richard Rumelt",
            "classification": {"primary_domain": "strategy", "role_suggestion": "strategy-expert"},
        },
    ]

    blueprint = create_expert_blueprint(
        expert_id="startup-strategy-expert",
        books=books,
        update_sources=["official docs", "recent market pages", "primary research"],
    )

    assert blueprint["expert_id"] == "startup-strategy-expert"
    assert blueprint["version"] == "0.1.0"
    assert blueprint["skills"] == ["startup-strategy-expert-diagnose", "startup-strategy-expert-apply", "startup-strategy-expert-update-scout"]
    assert blueprint["source_policy"]["book_role"] == "canon/framework base"
    assert blueprint["source_policy"]["web_role"] == "current facts/update layer"
    assert blueprint["verification_gate"]["required_before_promote"] is True


def test_source_hierarchy_separates_framework_from_current_facts():
    hierarchy = source_hierarchy()

    assert hierarchy[0] == "user_context"
    assert "canon_books_frameworks" in hierarchy
    assert hierarchy.index("official_current_sources") < hierarchy.index("credible_secondary_sources")
    assert hierarchy[-1] == "model_inference"


def test_team_routing_requires_all_departments_roles_and_promotion_gate():
    routing_path = Path("knowledge/book-expert-factory/team-routing.yaml")

    result = validate_team_routing(routing_path)

    assert result["ok"] is True
    assert result["division_count"] == 10
    assert result["role_count"] >= 30
    assert "promote_expert" in result["restricted_actions"]
    assert result["promotion_owner"] == "executive-control-office"
