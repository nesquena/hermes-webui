# Yuto Team Lanes

Created: 2026-05-11 JST

Purpose:
- Reusable lane manifests for Yuto's least-privilege AI harness team.
- Source playbook: [[yuto-team-lanes-reuse-playbook]].
- Use these as workflow contracts before mapping to Hermes `delegate_task`, Workspace roster, Codex/Claude workers, or local LLM workers.

Rules:
- Yuto remains the control plane and final verifier.
- Use the smallest sufficient lane set.
- Reader lanes cannot write.
- Writer lanes cannot read raw untrusted docs directly.
- QA verifies before Yuto claims completion.
- Human review gates legal/forensic/high-risk conclusions.

Current lane manifests:
- `evidence-doc-reader.yaml`
- `japan-compliance-checker.yaml`
- `forensic-reviewer.yaml`
- `report-writer.yaml`
- `qa-critic.yaml`
- `researcher-source-reader.yaml`
- `code-implementation-worker.yaml`
- `steering-examples.yaml`

Validator / measurement:

```bash
cd /Users/kei/kei-jarvis
python tools/yuto_team_lanes.py --json
python tools/yuto_team_lanes.py --summary-receipts --json
```

Receipt log:
- `/Users/kei/kei-jarvis/knowledge/yuto-team-lane-receipts.jsonl`
- First 10 receipts collected on 2026-05-11; mostly retrospective from already-verified Yuto work.
- Use 3-5 more prospective lane-assisted tasks before promoting runtime wrappers.

Related: [[source-anthropic-financial-services-agent-team]], [[source-ai-harness-teams]], [[security]], [[ai-legal-japan-research-target]]
