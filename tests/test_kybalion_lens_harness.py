from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import kybalion_lens_harness  # noqa: E402


def test_validate_current_kybalion_lens_docs():
    result = kybalion_lens_harness.validate_all(ROOT / "knowledge")

    assert result.ok, result.errors
    assert "Yuto Scout / Memory Scout" in result.roles_found
    assert "Researcher / Source Reader" in result.roles_found
    assert "no worker may use the lens to override" in "\n".join(result.guardrails_found).lower()


def test_detects_missing_scout_role(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "kybalion-yuto-practice-experiments.md").write_text(
        """# Kybalion → Yuto Practice Experiments

## Scope across Yuto team

| Lane / role | Kybalion micro-check | Practical use | Hard limit |
|---|---|---|---|
| Yuto Control | full negative check + selected principle | route | no identity inflation |
| Researcher / Source Reader | Mentalism + Correspondence | frame source | analogy is not evidence |

## Global guardrails

No worker may use the lens to override evidence, safety gates, tool boundaries, or Kei approval.
Workers use only the one relevant micro-check for their lane.
""",
        encoding="utf-8",
    )
    (knowledge / "yuto-team-lanes-reuse-playbook.md").write_text(
        "Reference: [[kybalion-yuto-practice-experiments]].\nNo worker may use the lens to override evidence.\n",
        encoding="utf-8",
    )
    (knowledge / "yuto-memory-scout.md").write_text(
        "Scout reports candidates only. Scout does not edit memory/KG/skills.\nRelated lens: [[kybalion-yuto-practice-experiments]].\n",
        encoding="utf-8",
    )

    result = kybalion_lens_harness.validate_all(knowledge)

    assert not result.ok
    assert any("missing required role" in error and "Yuto Scout / Memory Scout" in error for error in result.errors)


def test_detects_missing_scout_read_only_guardrail(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "kybalion-yuto-practice-experiments.md").write_text(
        """# Kybalion → Yuto Practice Experiments

## Scope across Yuto team

| Lane / role | Kybalion micro-check | Practical use | Hard limit |
|---|---|---|---|
| Yuto Control | full negative check + selected principle | route | no identity inflation |
| Yuto Scout / Memory Scout | Vibration + Rhythm + Cause/Effect | detect drift | read-only reports; no promotion/editing |
| Researcher / Source Reader | Mentalism + Correspondence | frame source | analogy is not evidence |
| Evidence Doc Reader | Vibration + Cause/Effect | preserve source | no authenticity/legal conclusions |
| Compliance Checker | Polarity + Cause/Effect | boundary tension | no case-specific legal advice |
| Forensic Reviewer | Cause/Effect + Vibration | provenance | do not alter original evidence |
| Report Writer / Scribe | Generative Duality | write feedback | no raw untrusted-doc rewrite as fact |
| QA Critic / Reviewer | Negative check + Polarity | catch pattern-forcing | critique only unless asked |
| Code Implementation Worker | Rhythm + Cause/Effect | build verify | no broad refactor without scope |
| Cron/background jobs | Vibration + Rhythm | monitor cycles | final/failure reports only unless urgent |

## Global guardrails

No worker may use the lens to override evidence, safety gates, tool boundaries, or Kei approval.
Workers use only the one relevant micro-check for their lane.
""",
        encoding="utf-8",
    )
    (knowledge / "yuto-team-lanes-reuse-playbook.md").write_text(
        "Reference: [[kybalion-yuto-practice-experiments]].\nNo worker may use the lens to override evidence.\n",
        encoding="utf-8",
    )
    (knowledge / "yuto-memory-scout.md").write_text(
        "Related lens: [[kybalion-yuto-practice-experiments]].\n",
        encoding="utf-8",
    )

    result = kybalion_lens_harness.validate_all(knowledge)

    assert not result.ok
    assert any("scout" in error.lower() and "read-only" in error.lower() for error in result.errors)
