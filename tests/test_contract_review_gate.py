from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
CONTRACTS = ROOT / "docs" / "CONTRACTS.md"


def test_contributing_requires_contract_routing_for_contract_affecting_prs():
    text = CONTRIBUTING.read_text(encoding="utf-8")

    required_terms = [
        "contract-affecting PR",
        "Contract Routing",
        "Contract Change",
        "release batch",
    ]

    missing = [term for term in required_terms if term not in text]
    assert missing == []


def test_contracts_requires_docs_tests_and_pr_body_to_move_together():
    text = CONTRACTS.read_text(encoding="utf-8")

    required_terms = [
        "Contract Change",
        "contract tests",
        "corresponding docs",
        "must not silently redefine",
    ]

    missing = [term for term in required_terms if term not in text]
    assert missing == []


def test_contract_guidance_names_static_coverage_as_advisory_not_enforcement():
    text = CONTRACTS.read_text(encoding="utf-8")

    required_terms = [
        "advisory",
        "not an automated policy gate",
        "does not enforce",
        "PR-body content",
        "release-time",
    ]

    missing = [term for term in required_terms if term not in text]
    assert missing == []
