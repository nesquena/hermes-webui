from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "project-os-phase1-topology.md"
CONTRACTS = (REPO_ROOT / "docs" / "CONTRACTS.md").read_text(encoding="utf-8")
TESTING = (REPO_ROOT / "TESTING.md").read_text(encoding="utf-8")
EXTENSION_JS = (REPO_ROOT / "extensions" / "project-os" / "project-os-extension.js").read_text(encoding="utf-8")


def test_project_os_phase1_topology_doc_exists():
    assert DOC.exists(), "docs/project-os-phase1-topology.md must exist for Project OS phase-1 topology guidance"


def test_project_os_phase1_topology_doc_pins_live_topology_and_review_routes():
    content = DOC.read_text(encoding="utf-8")
    required_terms = [
        "default",
        "ops",
        "builder",
        "manual implementation lane",
        "reserve-only",
        "recurring cron/background ownership",
        "reviewer profile",
        "Claude Design",
        "docs/UIUX-GUIDE.md",
        "DESIGN.md",
    ]
    missing = [term for term in required_terms if term not in content]
    assert missing == []


def test_contracts_and_testing_link_to_project_os_phase1_topology_doc():
    assert "project-os-phase1-topology.md" in CONTRACTS, (
        "docs/CONTRACTS.md must route Project OS topology/assignment changes to the topology doc"
    )
    assert "project-os-phase1-topology.md" in TESTING, (
        "TESTING.md must point contributors at the Project OS topology contract"
    )


def test_project_os_control_plane_prompts_embed_phase1_topology_contract():
    required_terms = [
        "Live phase-1 topology is default/ops/builder only",
        "do not introduce a live reviewer profile",
        "Treat builder as the active manual implementation lane",
        "Route code review through default or builder",
        "Claude Design first",
    ]
    missing = [term for term in required_terms if term not in EXTENSION_JS]
    assert missing == []
