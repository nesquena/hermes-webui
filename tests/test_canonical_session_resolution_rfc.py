from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RFC = ROOT / "docs" / "rfcs" / "canonical-session-resolution.md"
RFC_INDEX = ROOT / "docs" / "rfcs" / "README.md"
CONTRACTS = ROOT / "docs" / "CONTRACTS.md"


def test_canonical_session_resolution_rfc_is_indexed():
    assert RFC.exists(), "canonical session resolution RFC must exist"

    rel = "docs/rfcs/canonical-session-resolution.md"
    rfc_index = RFC_INDEX.read_text(encoding="utf-8")
    contracts = CONTRACTS.read_text(encoding="utf-8")

    assert "canonical-session-resolution.md" in rfc_index
    assert rel in contracts


def test_canonical_session_resolution_contract_names_entrypoints_and_outputs():
    text = RFC.read_text(encoding="utf-8")

    required_terms = [
        "URL route",
        "query parameter",
        "localStorage",
        "sidebar",
        "pre_compression_snapshot",
        "canonical_visible_session_id",
        "continuation_session_id",
        "parent_session_id",
        "direct session open",
        "browser boot restore",
    ]

    missing = [term for term in required_terms if term not in text]
    assert missing == []
