"""The #5619 write-target rule must be documented, not only enforced in code.

A review gate flagged the missing docs: a writer that transacts on raw YAML is
a runtime rule with a non-obvious failure mode (baked secrets, cross-profile
leak), and the skills toggle carries a deliberate exception where a scalar
`${VAR}` holding several names is replaced by explicit names. Code comments
alone do not satisfy the repo's "update docs when changing runtime behavior"
requirement, and an undocumented exception reads as a bug to the next author.

These assertions are documentation gates, not behavior tests: the behavior is
pinned by tests/test_issue5619_config_write_authority.py. They fail if the
contract doc is deleted, renamed, or stripped of the rule it exists to record.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "architecture" / "config-write-raw-target.md"
CONTRACTS = ROOT / "docs" / "CONTRACTS.md"


def test_contract_doc_exists_and_is_linked_from_the_contract_index():
    assert DOC.exists(), f"missing write-target contract doc: {DOC}"
    assert "docs/architecture/config-write-raw-target.md" in CONTRACTS.read_text(
        encoding="utf-8"
    )


def test_doc_states_the_raw_write_rule_and_both_failure_modes():
    text = DOC.read_text(encoding="utf-8")

    assert "_load_yaml_config_file_raw" in text
    assert "_save_yaml_config_file" in text
    # the two consequences a future writer must not reintroduce
    assert "bake" in text.lower()  # covers "baked"
    assert "cross-profile" in text.lower()


def test_doc_records_the_scalar_placeholder_exception():
    text = DOC.read_text(encoding="utf-8")

    # the exception itself: a ${VAR} expanding to several names cannot survive
    # a partial edit, so the remaining names are written resolved
    assert "cannot survive" in text
    assert "logger.info" in text
    # and the reason it is not a secret leak, so the exception stays narrow
    assert "skills" in text
