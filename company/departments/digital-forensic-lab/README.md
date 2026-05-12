# Digital Forensic Lab — Phase 0

Status: synthetic/internal-only usable lab with source-bound growth map.

## Use for

- synthetic evidence-preservation workflow rehearsal;
- chain-of-custody templates;
- evidence register schemas;
- reliability and red-line checks;
- source-grounded learning modules from Kei's registered AI/forensics/legal/cyber books.

## Do not use for

- real victim/case data;
- final forensic/legal/security/compliance claims;
- evidence modification;
- external action;
- offensive cyber activity.

## Knowledge binding

Primary map:

- `company/departments/digital-forensic-lab/knowledge-source-map.yaml`

Roadmap:

- `company/departments/digital-forensic-lab/learning-roadmap.md`

The map binds the lab to Book Expert Factory sources including:

- `Artificial Intelligence and Digital Forensics`
- `Digital Defence`
- `Artificial Intelligence and the Rule of Law`
- `AI in Legal Tech`
- `Agentic AI for Cybersecurity`
- `Securing the Digital Realm`
- `Startup Technical Guide: AI Agents`
- `Securing the Digital Frontier`
- `Forensic Investigation of Smart Digital Devices`
- `Practical Digital Forensics`

Boundary: these sources are `candidate_unverified_framework`. They can train synthetic templates, questions, validators, and learning modules. They do not authorize real case handling or final forensic/legal claims.

## Growth loop

read source candidate -> extract framework questions -> convert to synthetic template -> run validator -> record receipt -> review failures -> update playbook/tests -> Scout report.

## Working library

Primary index:

- `company/departments/digital-forensic-lab/working-library/library-index.yaml`

The working library is the lab's own source-grounded Phase 0 library. It contains:

- 10 verified source-extract files with source metadata path, local asset path, SHA-256 verification, short evidence anchors, framework questions, reusable rules, contribution mapping, and explicit promotion blockers;
- 5 internal controls for preservation, chain of custody, AI reliability, legal/governance boundary, and AgentOps learning-loop operation;
- 3 synthetic learning-cycle receipts in `working-library/learning-loop-receipts.jsonl`;
- a Scout-facing report contract in `working-library/scout-report.yaml`.

Boundary: the working library proves learning-loop discipline and source-grounded synthetic controls. It does not prove real forensic competence, legal authority, court admissibility, or production readiness.

## Validator

```bash
python tools/digital_forensic_lab.py --json
```

Validator checks include:

- synthetic-only lab boundary;
- no real case data;
- no final conclusion;
- custody events and receipt refs;
- receipt has no external actions or policy violations;
- workforce links include 3 digital forensic personnel and 1 department lead;
- knowledge-source-map exists, references registered sources, has learning modules, and defines a growth loop.
