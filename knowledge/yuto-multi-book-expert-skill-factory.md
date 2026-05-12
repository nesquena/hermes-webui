# Yuto Multi-Book Expert Skill Factory

Created: 2026-05-12 JST
Status: v0.1 implemented scaffold + operating playbook
Purpose: Let Kei drop in books/documents; Yuto classifies them, groups multiple books into versioned expert systems, verifies frameworks before promotion, and lets the team call the right expert skill/lane.

Related: `book-to-course`, `hermes-agent-skill-authoring`, [[yuto-memory-capture-policy]], [[yuto-team-feature-audit-2026-05-12]], [[yuto-team-lanes-reuse-playbook]], [[systems-thinking-ready-reference]], [[agentmemory-deep-dive]], [[source-cocoindex]]

Reference note for previous audit: [[yuto-team-feature-audit-2026-05-12]]

## 1. Decision

Yes: Yuto should own this system for Kei and the team.

But the system must not be:

```text
book dump -> model summary -> fake expert
```

It must be:

```text
verified source -> framework extraction -> multi-book comparison -> versioned expert blueprint -> diagnose/apply/update-scout skills -> team receipts -> reviewed promotion
```

## 2. System Name

```text
Yuto Multi-Book Expert Skill Factory
```

Short form:

```text
Book Expert Factory
```

## 3. Core Principles

1. Books provide canon/frameworks, not current facts.
2. Web/papers/official sources provide current updates, not random framework drift.
3. Multiple books must be compared by role: primary framework, complementary method, critique/risk lens, applied checklist.
4. Yuto verifies before promotion.
5. Team workers use the right expert/lane through routing rules, not by guessing.
6. Version every expert blueprint and every source candidate.
7. Store paraphrased frameworks and traceability, not long copyrighted passages.
8. No legal/forensic/medical/financial authority claims without human/expert gate.

## 4. Implemented v0.1 Scaffold

Files:

```text
tools/book_expert_factory.py
tests/test_book_expert_factory.py
```

CLI:

```bash
python tools/book_expert_factory.py classify /path/to/book-or-notes.txt --title "Book Title" --author "Author"
python tools/book_expert_factory.py register /path/to/book-or-notes.txt --title "Book Title" --author "Author"
python tools/book_expert_factory.py hierarchy --json
```

Registry default:

```text
knowledge/book-expert-factory/sources/
```

Current v0.1 capabilities:

- classify local `.txt`, `.md`, text-extractable `.pdf`, and text-based `.epub` samples;
- assign an initial expert domain;
- estimate skillability: high/medium/low;
- recommend skill split: diagnose/apply;
- register source metadata with SHA-256;
- mark every source as `candidate_unverified_framework` by default;
- write source hierarchy and verification flags;
- create multi-book expert blueprint objects in code.

Important: v0.1 classification is heuristic. It is a triage gate, not final truth.

## 5. Source Hierarchy

Use this when books and current web sources conflict:

```text
1. user_context
2. canon_books_frameworks
3. official_current_sources
4. primary_research_or_cases
5. credible_secondary_sources
6. team_receipts_and_verified_outputs
7. model_inference
```

Rule:

```text
Books govern reusable method.
Official/current sources govern current facts.
If they conflict, state the conflict and route to Yuto/human review.
```

## 6. Book Roles in a Multi-Book Expert

Every book in an expert set must have a role:

| Role | Meaning | Example |
|---|---|---|
| primary_framework | main method | The Mom Test for customer discovery |
| complementary_method | adds a useful lens | Lean Startup for experiment loop |
| critique_risk_lens | catches failure modes | Crossing the Chasm for segment risk |
| applied_checklist | turns method into repeatable output | checklist/workbook-style source |
| current_update_source | web/paper/official update | official docs, current market pages |

Do not let four books all compete as equal authorities.

## 7. Expert Blueprint Shape

Each expert should become a versioned blueprint:

```yaml
expert_id: startup-strategy-expert
version: 0.1.0
status: blueprint_unverified
canon_books:
  - title: The Mom Test
    role: primary_framework
  - title: Lean Startup
    role: complementary_method
  - title: Good Strategy Bad Strategy
    role: strategy_lens
skills:
  - startup-strategy-expert-diagnose
  - startup-strategy-expert-apply
  - startup-strategy-expert-update-scout
source_policy:
  book_role: canon/framework base
  web_role: current facts/update layer
verification_gate:
  required_before_promote: true
```

## 8. Skill Split

For every high-value expert:

### Diagnose

Purpose:

- analyze a situation;
- identify gaps;
- compare against frameworks;
- produce questions, risks, missing evidence, and recommended next action.

Output:

```text
Diagnosis
Framework mapping
Conflicts across books
Current-source update needed? yes/no
Questions for Kei
Recommended next action
```

### Apply

Purpose:

- produce a usable artifact;
- turn framework into plan/script/checklist/memo;
- state assumptions and evidence.

Output examples:

- interview script;
- strategy memo;
- negotiation prep;
- productivity plan;
- systems map;
- evidence-prep checklist.

### Update Scout

Purpose:

- check official/current web sources;
- update facts, examples, regulations, tool changes, cases;
- never overwrite canon framework automatically.

Output:

```text
What changed
Source/date
Impact on expert blueprint
Recommended version bump
Yuto review required
```

## 9. Verification Gate Before Promotion

Before a book-derived expert is promoted into a real Hermes skill or team lane, Yuto must verify:

- source file exists and opens;
- title/author/path/hash recorded;
- table of contents or sampled structure extracted;
- framework extracted as paraphrase;
- steps/rules/mistakes/questions trace back to source sections;
- copyright boundary respected;
- conflicts across books are listed;
- web/current sources verified if current facts are used;
- positive activation tests pass;
- negative activation tests pass;
- Yuto/team receipt exists for a real use case.

No verified gate, no promoted expert.

## 10. Versioning Rules

Use semantic versioning for expert blueprints:

```text
0.1.0 = source registered / unverified blueprint
0.2.0 = framework extracted and Yuto-reviewed
0.3.0 = diagnose/apply prompts tested
0.4.0 = update-scout tested with current sources
1.0.0 = used in at least 3 real tasks with receipts and stable outputs
```

Version bump triggers:

| Change | Version bump |
|---|---|
| add source/book | minor |
| fix extraction mistake | patch |
| add update source class | minor |
| change source hierarchy/conflict rule | minor/major depending impact |
| verified 3 live uses | promote to 1.0.0 |

## 11. Team Routing

Yuto controls routing. Workers do not pick experts freely.

Machine-readable routing file:

```text
knowledge/book-expert-factory/team-routing.yaml
```

Validation command:

```bash
python tools/book_expert_factory.py validate-routing
```

Current validated routing:

```text
10 divisions
32 role entries
promotion_owner = executive-control-office
restricted_actions = promote_expert, use_externally
```

Department-level rules:

| Department | Main use of book experts | Can promote? | Human/expert gate |
|---|---|---:|---|
| Executive / Control Office | route, verify, final promotion | yes | promote/external use |
| Global Intelligence | update-scout/current source layer | no | legal/forensic current claims |
| Research & Policy | extract/compare/synthesize frameworks | no | policy/academic claims |
| AI Law & Legal Frontier | legal boundary and current law checks | no | Article 72/APPI/legal claims |
| Digital Forensic Lab | provenance/chain-of-custody lens | no | forensic reliability claims |
| Security Frontier | dual-use/security source safety | no | security/dual-use methods |
| Case & Evidence Ops | apply verified methods to approved/synthetic workflows | no | real case/victim/evidence material |
| Engineering / Product Systems | implement CLI/schemas/tests/wrappers | no | production/deploy/destructive ops |
| Knowledge & Learning Infrastructure | registry, KG, skill drafts, versioning | no | skill promotion/active memory |
| Compliance / Safety / Expert Network | final risk review and expert handoff | no | high-risk/public/expert claims |

Position-level rules live in `team-routing.yaml`. Every role has explicit `allowed_actions`; if the action is not listed, the role must not do it.

Routing examples:

| User/team task | Expert / lane |
|---|---|
| validate customer interview questions | customer-discovery-diagnose |
| rewrite interview script | customer-discovery-apply |
| check company strategy | strategy-diagnose |
| produce strategy memo | strategy-apply |
| analyze a messy system/project | systems-thinking-diagnose |
| produce operating canvas | systems-thinking-apply |
| check latest regulation/market facts | update-scout |

Worker rule:

```text
Worker output = receipt.
Yuto verifies before shared KG/skill promotion.
```

## 12. Safety / Copyright Boundary

Allowed:

- metadata;
- short references;
- paraphrased framework;
- extracted steps written in Yuto/team language;
- traceability to chapter/section names;
- reusable checklists inspired by the method.

Not allowed:

- long copied book passages;
- full chapter reproductions;
- skill that substitutes for buying/reading the book;
- pretending the AI is the author;
- unverified claims about what the author meant;
- using old books as current legal/market facts.

## 13. Web Update Layer

Web update is allowed but scoped:

Use web for:

- laws/regulations/current official guidance;
- tool/API/product changes;
- recent market data;
- cases/incidents;
- new papers;
- pricing/competitor evidence.

Do not use random blog posts to override canon frameworks unless the update scout explicitly labels it as a critique and Yuto agrees.

## 14. First Expert Sets to Build

Recommended priority:

### 1. Systems Thinking Expert

Use for:

- PhD planning;
- company design;
- product architecture;
- Yuto/team improvement;
- decision analysis.

Skills:

```text
systems-thinking-diagnose
systems-thinking-apply
systems-thinking-update-scout
```

### 2. Startup Discovery / Strategy Expert

Candidate books:

- The Mom Test;
- Lean Startup;
- Good Strategy Bad Strategy;
- Crossing the Chasm or another GTM lens.

Skills:

```text
startup-discovery-diagnose
startup-discovery-apply
startup-discovery-update-scout
```

### 3. AI Harm Evidence Strategy Expert

Candidate sources:

- systems thinking source;
- strategy source;
- eDiscovery / digital evidence sources;
- forensic standards/guidelines;
- Japan official legal/privacy/AI governance sources.

Skills:

```text
ai-harm-evidence-diagnose
ai-harm-evidence-apply
ai-harm-evidence-update-scout
```

## 15. Operational Flow When Kei Drops a Book

```text
Kei drops book/file
-> Yuto verifies file exists and extracts sample/metadata
-> book_expert_factory classify
-> book_expert_factory register
-> Yuto asks only if classification changes action materially
-> source candidate enters registry as unverified
-> Yuto groups with related canon books if domain matches
-> framework extraction note is drafted
-> compare table across books is created
-> diagnose/apply/update-scout blueprint drafted
-> activation tests run
-> first live team task uses it
-> receipt captured
-> useful parts promoted
```

## 16. Receipts

- [[book-expert-factory/receipts/2026-05-12-ai-books-retry-registration]]: registered 5 new AI-Books sources, combined them with 4 existing local-book sources, and wrote v0.1 unverified expert blueprints.
- [[book-expert-factory/receipts/2026-05-12-startup-legal-tech-registration]]: registered Google Cloud Startup Technical Guide: AI Agents and Catherine Casey's AI in Legal Tech, then refreshed combined, HR/org, legal-tech, AI-harm, and agentic-systems unverified blueprints.
- [[book-expert-factory/receipts/2026-05-12-rule-law-web-performance-registration]]: re-verified Multimodal Real-Time AI Agent Systems, registered Artificial Intelligence and the Rule of Law plus Web Performance Engineering in the Age of AI, and wrote/updated legal-tech, AI-harm, agentic-systems, and web-performance unverified blueprints.

## 17. Commands

Classify:

```bash
cd /Users/kei/kei-jarvis
python tools/book_expert_factory.py classify /path/to/book.txt --title "Book Title" --author "Author"
```

Register:

```bash
python tools/book_expert_factory.py register /path/to/book.txt --title "Book Title" --author "Author"
```

View hierarchy:

```bash
python tools/book_expert_factory.py hierarchy --json
```

Run tests:

```bash
python -m pytest tests/test_book_expert_factory.py -q
```

## 17. Current Status

```text
v0.1 implemented:
- classifier/register CLI
- versioned candidate registry
- source hierarchy
- multi-book blueprint function
- validated team/department routing matrix
- tests

not yet implemented:
- PDF TOC rich extraction report
- compare-books command
- extract-framework command
- generate diagnose/apply/update-scout SKILL.md drafts
- activation test runner
- web update scout integration
- promotion to Hermes skill
```

## 18. Next Build Steps

P0:

1. Add `compare` command for multiple registered books.
2. Add `extract-framework` draft format with questions/steps/rules/mistakes.
3. Add activation test template: 5 positive prompts, 3 negative prompts.

P1:

4. Add `blueprint write` command under `knowledge/book-expert-factory/experts/`.
5. Add web update scout receipt format.
6. Add promotion path from blueprint to Hermes skill pair.

P2:

7. Add lightweight UI/Workspace view only if CLI becomes useful first.

## 19. Bottom Line

This system should become Yuto's way to turn books into callable, verified expert lenses for Kei and the team.

The critical rule:

```text
Books become verified expert methods only after Yuto checks source, compares across books, tests activation, and captures real-use receipts.
```
