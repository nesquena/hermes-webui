# Source Trail — Anthropic Financial Services Agent Team Patterns

Checked: 2026-05-11 JST
Source: https://github.com/anthropics/financial-services

## Conclusion

The repo is not just finance content. It is a reusable pattern library for building governed domain-agent teams:

```text
vertical plugin skills -> self-contained named agents -> managed-agent cookbooks -> leaf workers -> schema validation -> human sign-off/handoffs
```

For Yuto, the most valuable patterns are:

1. one source of truth for agent prompt + skills, served through multiple runtime surfaces;
2. self-contained named workflow agents;
3. least-privilege leaf workers;
4. exactly one `Write` holder when artifacts are produced;
5. untrusted document isolation;
6. schema-validated worker outputs;
7. hard-allowlisted cross-agent handoffs;
8. explicit “not guaranteed / human decides” disclaimers;
9. manifest linting and drift checks;
10. steering examples as reusable task triggers.

Do not adopt the finance domain or Claude Managed Agents API directly. Borrow the team architecture for Yuto's legal/forensic/research/coding lanes.

## Verified repo facts

From GitHub API on 2026-05-11:

- Repo: `anthropics/financial-services`
- URL: https://github.com/anthropics/financial-services
- Created: 2026-02-23
- Updated: 2026-05-11
- Last pushed: 2026-05-09
- Stars observed: 19,298
- Forks observed: 2,503
- Open issues observed: 111
- License: Apache-2.0
- Languages: Python, Shell, JavaScript
- Root files/dirs observed: `README.md`, `CLAUDE.md`, `.claude-plugin`, `plugins`, `managed-agent-cookbooks`, `scripts`, `claude-for-msft-365-install`

## What the repo says it is

README positioning:

> Reference agents, skills, and data connectors for the financial-services workflows we see most — investment banking, equity research, private equity, and wealth management.

It ships the same source through two surfaces:

- Claude Cowork plugin.
- Claude Managed Agents API template behind a user's workflow engine.

Critical disclaimer from README:

- Nothing constitutes investment/legal/tax/accounting advice.
- Agents draft analyst work product for review by qualified professionals.
- They do not make investment recommendations, execute transactions, bind risk, post to a ledger, or approve onboarding.
- Every output is staged for human sign-off.

Yuto relevance:

- This is exactly the posture Kei's legal/forensic AI needs: draft/organize/prepare, not decide/advise/execute.

## Repository architecture

From `CLAUDE.md` and root README:

```text
plugins/
  agent-plugins/               # named agents, self-contained plugin each
  vertical-plugins/            # skill + command bundles by FSI vertical
  partner-built/               # partner-authored plugins
managed-agent-cookbooks/       # one deploy cookbook per named agent
claude-for-msft-365-install/   # admin tooling for Microsoft 365 add-in
scripts/                       # deploy/check/validate/orchestrate/sync scripts
```

Agent plugin shape:

```text
plugins/agent-plugins/<slug>/
  .claude-plugin/plugin.json
  agents/<slug>.md             # canonical system prompt
  skills/                      # vendored skill copies
```

Managed cookbook shape:

```text
managed-agent-cookbooks/<slug>/
  agent.yaml
  subagents/*.yaml
  steering-examples.json
  README.md
```

Key rule from `CLAUDE.md`:

- Edit skill sources in `vertical-plugins/`.
- Run `scripts/sync-agent-skills.py` to propagate into agent bundles.
- Run `python3 scripts/check.py` before committing to lint manifests, refs, JSON/YAML, and bundled-skill drift.

Yuto relevance:

- Yuto needs this same split:
  - `worker-lane` source prompt/contract as truth;
  - runtime-specific wrappers for Hermes delegate_task, Workspace roster, local LLM reviewer, Codex/Claude coding worker;
  - lint/drift check so roster, skills, and runbooks do not diverge.

## Named agents observed

Root README lists end-to-end workflow agents:

- `pitch-agent`
- `meeting-prep-agent`
- `market-researcher`
- `earnings-reviewer`
- `model-builder`
- `valuation-reviewer`
- `gl-reconciler`
- `month-end-closer`
- `statement-auditor`
- `kyc-screener`

Pattern:

- Each named agent owns a complete workflow.
- Each bundles its skills.
- Each has a managed-agent cookbook with leaf workers.
- Some tasks hand off to another named agent through an allowlisted event loop, not direct free-form cross-calling.

Yuto relevance:

- Instead of generic “researcher/reviewer/coder” only, Yuto should define named workflow agents/lane contracts for common Kei work:
  - `legal-evidence-researcher`
  - `case-packet-formatter`
  - `forensic-checklist-reviewer`
  - `japan-compliance-boundary-checker`
  - `code-implementation-worker`
  - `output-qa-reviewer`

## Managed agent cookbook pattern

From `managed-agent-cookbooks/README.md`:

- Same source as Cowork plugin; cookbook is deploy manifest for `POST /v1/agents`.
- `agent.yaml` uses a canonical system file, skills, and callable agents.
- Leaf workers are `subagents/*.yaml`.
- `steering-examples.json` provides example steering events.
- Bold leaf in the README table is the only worker with `Write`.
- `callable_agents` supports one delegation level: orchestrator can call workers; workers cannot call further subagents.

Yuto relevance:

- This matches Hermes current delegation config: one-level child workers are a good default.
- We should explicitly avoid nested autonomous delegation unless there is strong evidence.

## Least-privilege worker pattern

Example: `pitch-agent`

- Orchestrator has read/grep/glob and read-only data MCPs.
- `researcher` has read-only data connectors.
- `modeler` can run sandboxed Bash but does not write the final workbook.
- `deck-writer` is the only leaf with Write and has no external connectors.

Example: `gl-reconciler`

- Reader touches untrusted counterparty/custodian statements with Read/Grep only.
- Orchestrator touches trusted internal MCPs, no Write.
- Critic independently re-verifies breaks against trusted sources.
- Resolver is the only Write holder and never opens outsider files.

Example: `kyc-screener`

- `doc-reader` touches untrusted onboarding docs with Read/Grep only and no MCP access.
- `rules-engine` runs trusted screening via read-only MCP.
- `escalator` is the only Write holder and never opens onboarding docs directly.

Yuto relevance:

For Kei's legal/forensic AI team:

```text
untrusted evidence/doc reader: read-only, no write, no external actions, structured output only
legal/compliance checker: reads validated extracted facts + statutes/checklists, no write to evidence
forensic reviewer: reads hash/provenance/timeline, no mutation of originals
report writer: only write-holder, never reads raw untrusted docs directly
Yuto: orchestrator/verifier, no direct mutation of original evidence
```

## Schema validation pattern

`scripts/validate.py`:

- Validates worker output JSON against JSON Schema.
- Exits 0 on valid, 1 on invalid.
- Repo notes that CMA API does not enforce structured output today, so the deploy harness validates between reader subagent and orchestrator.

Subagent YAML examples include `output_schema` with:

- required fields;
- `additionalProperties: false`;
- max string lengths;
- regex character allowlists;
- max array sizes;
- enums for status/cause.

Yuto relevance:

- For legal/forensic ingestion, schema validation is mandatory before Yuto uses extracted evidence facts.
- This protects against prompt injection, oversized output, malformed references, and “free text instructions” smuggled from untrusted documents.

## Handoff pattern

`scripts/orchestrate.py`:

- It is reference only, not production.
- It watches output for `handoff_request` blobs.
- It hard-allowlists target agents.
- It schema-validates payloads.
- It warns that model text output can be influenced by untrusted documents; production should prefer typed tool calls/events the model cannot fake by quoting text.

Yuto relevance:

- Yuto should not allow arbitrary worker-to-worker handoffs.
- Cross-lane handoffs should be:
  - explicit;
  - allowlisted;
  - schema-validated;
  - routed by Yuto or a small script, not free-form model text.

## Validation and drift checks

`scripts/check.py` checks:

1. YAML parse under `managed-agent-cookbooks/`.
2. JSON parse for plugin manifests / marketplace / steering examples.
3. agent prompt frontmatter has `name` and `description`.
4. `system.file`, `skills[].path`, `callable_agents[].manifest` references resolve.
5. each managed-agent directory has `agent.yaml`, `README.md`, and `steering-examples.json`.
6. agent-plugin bundled skills match vertical-plugin source; if not, run `scripts/sync-agent-skills.py`.

Yuto relevance:

- Yuto Workspace roster, skill references, and worker-lane docs need a similar linter before we claim “team configured.”

## Adaptation for Yuto Harness Team

### Borrow now

1. Source-of-truth lane contracts.
2. Self-contained workflow agents for recurring work.
3. Steering examples for each lane.
4. Least-privilege worker tool split.
5. One Write-holder rule.
6. Untrusted-input reader isolation.
7. JSON Schema for worker outputs.
8. Critic/reviewer before write/report.
9. Handoff allowlist and typed payloads.
10. Drift-check script for roster/lane docs/skill links.

### Do not borrow yet

1. Finance-specific agents or data connectors.
2. Claude Cowork marketplace packaging.
3. Managed Agents API deployment as primary runtime.
4. Deep Office/Excel/PowerPoint integration unless a product workflow requires it.
5. Standing swarm before worker lanes prove ROI.

## Yuto v0.1 team proposal inspired by the repo

Create small lane specs under a Yuto-owned path, for example:

```text
/Users/kei/kei-jarvis/knowledge/yuto-team-lanes/
  legal-evidence-researcher.yaml
  evidence-doc-reader.yaml
  japan-compliance-checker.yaml
  forensic-reviewer.yaml
  report-writer.yaml
  qa-critic.yaml
  steering-examples.json
```

First three lanes:

### 1. Evidence Doc Reader

Purpose:
- Read untrusted screenshots/PDFs/text exports or synthetic case docs.
- Extract only structured facts.

Tools:
- Read/search only.
- No shell, no write, no browser, no external messaging.

Output:
- JSON only: `source_id`, `claim`, `timestamp`, `evidence_ref`, `uncertainty`, `red_flags`.

### 2. Japan Compliance Checker

Purpose:
- Check extracted workflow against Article 72/APPI/product-language guardrails.

Tools:
- Read trusted notes/checklists only.
- No write except comments/report draft if routed.

Output:
- JSON: `risk`, `boundary`, `reason`, `source_ref`, `required_human_review`.

### 3. Report Writer / Consultation Prep Writer

Purpose:
- Write a lawyer-ready consultation prep packet from already validated facts.

Tools:
- The only Write holder for final report artifacts.
- Never opens raw untrusted docs directly.

Output:
- Markdown/PDF draft with provenance table and review flags.

### 4. QA Critic

Purpose:
- Independently check source trace, schema conformance, legal/forensic disclaimers, and hallucination risk.

Tools:
- Read-only.

Output:
- Findings by severity and pass/fail gate.

## Immediate practical next steps

1. Do not install or deploy this repo into Yuto.
2. Create a small Yuto Team Lane Manifest format inspired by their `agent.yaml` / subagent YAML.
3. Build a tiny validator for lane manifests and worker receipts.
4. Pilot on synthetic/non-sensitive legal evidence packets only.
5. Measure whether the lane split improves accuracy/safety over Yuto alone.

## Source files inspected

- GitHub repo metadata via API.
- `README.md`
- `CLAUDE.md`
- `.claude-plugin/marketplace.json`
- `managed-agent-cookbooks/README.md`
- `managed-agent-cookbooks/pitch-agent/agent.yaml`
- `managed-agent-cookbooks/pitch-agent/subagents/*.yaml`
- `managed-agent-cookbooks/market-researcher/agent.yaml`
- `managed-agent-cookbooks/market-researcher/subagents/*.yaml`
- `managed-agent-cookbooks/gl-reconciler/agent.yaml`
- `managed-agent-cookbooks/gl-reconciler/subagents/*.yaml`
- `managed-agent-cookbooks/kyc-screener/agent.yaml`
- `managed-agent-cookbooks/kyc-screener/subagents/*.yaml`
- `managed-agent-cookbooks/model-builder/agent.yaml`
- `scripts/check.py`
- `scripts/validate.py`
- `scripts/orchestrate.py`
- `scripts/sync-agent-skills.py`

Related: [[source-ai-harness-teams]], [[source-goalbuddy]], [[source-cocoindex]], [[ai-legal-japan-research-target]], [[ai-era-legal-advocacy-company-blueprint]], [[yuto-personal-assistant-operating-system]], [[security]]
